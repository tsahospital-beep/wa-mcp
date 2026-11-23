"""Process one live event at a time with account-scoped, durable send claims."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import shutil
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from wa_mcp import accounts, audio, config, db, fsutil, people, transcribe
from wa_mcp.agent import Event, Reply
from wa_mcp.agent.providers import generate
from wa_mcp.agent.voice import render
from wa_mcp.client import Bridge, BridgeError
from wa_mcp.insight import state

log = logging.getLogger(__name__)
Data = dict[str, Any]


def current() -> config.Config:
    cfg = db.store().cfg
    if cfg.demo:
        return cfg
    fresh = config.load(cfg.file, account=cfg.account)
    if (fresh.store_dir, fresh.session_db, fresh.api) != (cfg.store_dir, cfg.session_db, cfg.api):
        raise config.ConfigError("account connection changed. Restart the worker or MCP server")
    return fresh


def switch() -> bytes:
    try:
        return (db.store().cfg.store_dir / "agent.switch").read_bytes()
    except FileNotFoundError:
        return b""


def paused() -> bool:
    control = switch()
    return bool(control and not control.startswith(b"active "))


def revision() -> str:
    cfg = db.store().cfg
    return hashlib.sha256(cfg.file.read_bytes() + b"\x00" + switch()).hexdigest()


def set_paused(value: bool) -> None:
    with state.connect() as conn, state.transaction(conn):
        if value != paused():
            fsutil.write_private(
                db.store().cfg.store_dir / "agent.switch", f"{'paused' if value else 'active'} {time.time()}"
            )
    if value:
        reconcile(current(), accounts.bridge(), cancel=True)


def denied_jids(cfg: config.Config) -> set[str]:
    # An unresolved deny entry must fail closed.
    return {jid for entry in cfg.agent.deny_chats for jid in people.resolve_recipient(entry)}


def allowed(cfg: config.Config, chat: str, sender: str) -> bool:
    if people.server_of(chat) not in ("s.whatsapp.net", "lid", "g.us"):
        return False
    parties = set(people.same_person(chat)) | set(people.same_person(sender))
    if parties.intersection(people.my_jids()):
        return False
    if parties.intersection(denied_jids(cfg)):
        return False
    for entry in cfg.agent.allow_chats:
        if chat.endswith("@g.us") and entry != chat:
            continue
        try:
            if chat in people.resolve_recipient(entry):
                return True
        except LookupError:
            continue
    return False


def decision(bridge: Bridge, ref: Data, status: str, reason: str, draft_id: str = "") -> Data:
    out = {
        "status": status,
        "reason": reason,
        "chat": ref["chat"],
        "message_id": ref["id"],
        "draft_id": draft_id,
    }
    log.info("agent %s %s %s", status, reason, draft_id)
    try:
        bridge.agent_decision(out)
    except BridgeError as exc:
        log.warning("agent decision webhook unavailable (HTTP %d)", exc.status)
    return out


def claim(cfg: config.Config, ref: Data, peer: str) -> tuple[str, str]:
    now = time.time()
    with state.connect() as conn, state.transaction(conn):
        control = switch()
        if control and (not control.startswith(b"active ") or ref["at"] < float(control.split()[1])):
            return "", "paused"
        aliases = people.same_person(ref["chat"])
        params = ",".join("?" for _ in aliases)
        if conn.execute(
            f"SELECT 1 FROM agent_drafts WHERE chat IN ({params}) AND kind=? AND message_id=?",
            (*aliases, ref["kind"], ref["id"]),
        ).fetchone():
            return "", "duplicate"
        recent = conn.execute(
            "SELECT 1 FROM agent_drafts WHERE peer=? AND at>? AND status <> 'cooldown'",
            (peer, now - cfg.agent.cooldown_s),
        ).fetchone()
        draft_id = uuid.uuid4().hex
        status = "cooldown" if recent else "processing"
        changed = conn.execute(
            "INSERT OR IGNORE INTO agent_drafts (id,chat,kind,message_id,peer,at,status) VALUES(?,?,?,?,?,?,?)",
            (draft_id, ref["chat"], ref["kind"], ref["id"], peer, now, status),
        ).rowcount
        if not changed:
            return "", "duplicate"
        return (draft_id, "cooldown") if recent else (draft_id, "")


def inspect_event(cfg: config.Config, ref: Data) -> tuple[Data, str]:
    if not cfg.agent.enabled or paused():
        return {}, "disabled"
    if ref.get("outgoing") or ref.get("bot"):
        return {}, "own_or_bot"
    if abs(time.time() - ref["at"]) > 120:
        return {}, "stale"
    chat, mid = ref["chat"], ref["id"]
    if ref["kind"] == "call":
        if ref.get("group_call") or chat.endswith("@g.us") or not allowed(cfg, chat, chat):
            return {}, "chat_policy"
        if (
            ref["outcome"] == "unknown"
            and cfg.agent.auto_reject
            and cfg.agent.mode == "auto"
            and cfg.sending.enabled
            and not cfg.agent.dry_run
        ):
            return {"author": chat, "kind": "call_offer"}, ""
        if ref["outcome"] not in ("missed", "rejected") or not cfg.agent.follow_up_calls:
            return {}, "call_policy"
        return {"author": chat, "kind": ref["outcome"] + "_call"}, ""
    row = db.message(chat, mid)
    if (
        not row
        or row["outgoing"]
        or row.get("deleted_at")
        or row.get("deleted_unverified")
        or row["kind"] not in ("text", "voice")
    ):
        return {}, "message_type"
    if not allowed(cfg, chat, row["author"]):
        return {}, "chat_policy"
    if chat.endswith("@g.us") and not set(row.get("mentioned") or ()).intersection(people.my_jids()):
        return {}, "no_mention"
    if row.get("reply_to_id"):
        quoted = db.message(chat, row["reply_to_id"])
        if quoted and quoted["outgoing"] and time.time() - quoted["sent_at"] < max(120, cfg.agent.cooldown_s):
            return {}, "recent_outgoing_quote"
    return row, ""


def build_event(cfg: config.Config, bridge: Bridge, ref: Data, row: Data) -> Event:
    chat, mid = ref["chat"], ref["id"]
    transcript = {}
    if row["kind"] == "voice":
        transcript = transcribe.transcribe_message(bridge, chat, mid)
        if not transcript.get("text"):
            raise ValueError("empty transcript")
    history: list[Data] = []
    if cfg.agent.history:
        if ref["kind"] == "message":
            context = db.context(chat, mid, before=cfg.agent.history, after=0)
            history = context["before"] if context else []
        else:
            history = list(reversed(db.messages(conv=chat, before=ref["at"] + 1, limit=cfg.agent.history)))
    denied = denied_jids(cfg) if history else set()
    author = row["author"]
    return Event(
        cfg.account,
        chat,
        people.name_of(chat),
        author,
        people.name_of(author),
        mid,
        row.get("body", ""),
        transcript.get("text", ""),
        row["kind"],
        [
            {"from": "self" if r["outgoing"] else r["author"], "text": r["body"][:2000], "kind": r["kind"]}
            for r in history
            if not r.get("deleted_unverified") and not denied.intersection(people.same_person(r["author"]))
        ],
        transcript.get("lang") or cfg.transcription.language or "",
        "message" if ref["kind"] == "message" else row["kind"],
    )


def pending(draft_id: str = "", limit: int = 50, *, with_audio: bool = True) -> list[Data]:
    result = []
    with state.connect() as conn:
        query = "SELECT * FROM agent_drafts WHERE status='drafted'"
        if draft_id:
            rows = conn.execute(query + " AND id=?", (draft_id,))
        else:
            rows = conn.execute(query + " ORDER BY at DESC LIMIT ?", (max(1, min(100, limit)),))
        for row in rows:
            payload = json.loads(db.store().draft_payload(row["id"], row["payload"], read=True))
            if not with_audio:
                payload["voice_note"] = bool(payload.pop("audio"))
            result.append(
                {
                    "id": row["id"],
                    "message_id": row["message_id"],
                    "chat": row["chat"],
                    "kind": row["kind"],
                    **payload,
                }
            )
    return result


def finish(draft_id: str, status: str) -> None:
    with state.connect() as conn:
        conn.execute(
            "UPDATE agent_drafts SET status=?,at=?,payload='',audio_path=NULL WHERE id=?",
            (status, time.time(), draft_id),
        )


def reconcile(
    cfg: config.Config,
    bridge: Bridge,
    *,
    cancel: bool = False,
    on_decision: Callable[[Data], None] | None = None,
) -> None:
    with state.connect() as conn:
        rows = conn.execute("SELECT * FROM agent_drafts WHERE status='queued'").fetchall()
    if not rows:
        return
    scheduled = db.store().schedule_statuses([row["schedule_id"] for row in rows])
    for row in rows:
        status = scheduled.get(row["schedule_id"])
        if status == "pending" and (cancel or not cfg.agent.enabled or cfg.agent.dry_run or paused()):
            try:
                bridge.cancel_scheduled(row["schedule_id"])
            except BridgeError as exc:
                if exc.status == 409:
                    continue  # The scheduler claimed it. Read the outcome on the next poll.
                raise
            status = "cancelled"
        if status in ("sent", "failed", "cancelled"):
            final = "sent" if status == "sent" else "skipped"
            with state.connect() as conn:
                changed = conn.execute(
                    "UPDATE agent_drafts SET status=?,at=?,audio_path=NULL WHERE id=? AND status='queued'",
                    (final, time.time(), row["id"]),
                ).rowcount
            if not changed:
                continue
            if row["audio_path"]:
                remove_queued_audio(cfg, Path(row["audio_path"]))
            out = decision(bridge, {"id": row["message_id"], "chat": row["chat"]}, final, status, row["id"])
            if on_decision:
                on_decision(out)


def decline(bridge: Bridge, draft: Data) -> None:
    with state.connect() as conn:
        changed = conn.execute(
            "UPDATE agent_drafts SET status='skipped',payload='' WHERE id=? AND status='drafted'",
            (draft["id"],),
        ).rowcount
    if changed:
        decision(
            bridge, {"id": draft["message_id"], "chat": draft["chat"]}, "skipped", "declined", draft["id"]
        )


def remove_queued_audio(cfg: config.Config, path: Path) -> None:
    root = (cfg.exports_dir / "outgoing").resolve()
    if path.name != "reply.ogg" or not path.parent.name.startswith("send-"):
        return
    try:
        if path.parent.resolve() != root / path.parent.name or path.parent.parent.resolve() != root:
            return
        path.unlink(missing_ok=True)
        path.parent.rmdir()
    except FileNotFoundError:
        pass
    except OSError as exc:
        log.warning("queued voice cleanup failed (%s)", type(exc).__name__)


def materialize(encoded: str) -> Path:
    data = base64.b64decode(encoded, validate=True)
    path = audio.outgoing_path("reply.ogg")
    try:
        path.write_bytes(data)
    except OSError:
        shutil.rmtree(path.parent, ignore_errors=True)
        raise
    return path


def deliver(bridge: Bridge, draft: Data, *, automatic: bool = False, expected_revision: str = "") -> Data:
    policy_revision = expected_revision or revision()
    cfg = current()
    draft_id, chat = draft["id"], draft["chat"]
    ref = {"id": draft["message_id"], "chat": chat}
    if (
        policy_revision != revision()
        or not cfg.agent.enabled
        or cfg.agent.dry_run
        or not cfg.sending.enabled
        or (automatic and cfg.agent.mode != "auto")
        or not allowed(cfg, chat, draft["sender"])
    ):
        if automatic:
            finish(draft_id, "skipped")
        return decision(bridge, ref, "skipped", "send_policy", draft_id)
    if paused():
        if automatic:
            finish(draft_id, "skipped")
        return decision(bridge, ref, "skipped", "paused", draft_id)
    with state.connect() as conn:
        claimed = conn.execute(
            "UPDATE agent_drafts SET status='sending' WHERE id=? AND status=?",
            (draft_id, "processing" if automatic else "drafted"),
        ).rowcount
    if not claimed:
        return decision(bridge, ref, "skipped", "already_claimed", draft_id)
    path = None
    try:
        path = materialize(draft["audio"]) if draft["audio"] else None
        res = bridge.send(
            chat,
            text=None if path else draft["text"],
            media_path=str(path) if path else None,
            as_voice=bool(path),
            agent_revision=policy_revision,
            reply_to=ref["id"] if draft["kind"] == "message" else None,
        )
        if res.get("queued"):
            with state.connect() as conn:
                conn.execute(
                    "UPDATE agent_drafts SET status='queued',at=?,payload='',schedule_id=?,audio_path=? WHERE id=?",
                    (time.time(), res["scheduled_id"], str(path) if path else None, draft_id),
                )
            path = None
            return decision(bridge, ref, "queued", "quiet_hours", draft_id)
    except Exception as exc:
        finish(draft_id, "failed")
        reason = f"bridge_{exc.status}" if isinstance(exc, BridgeError) else type(exc).__name__
        return decision(bridge, ref, "skipped", reason, draft_id)
    finally:
        if path:
            shutil.rmtree(path.parent, ignore_errors=True)
    finish(draft_id, "sent")
    return decision(bridge, ref, "sent", "delivered", draft_id)


async def handle(bridge: Bridge, ref: Data, on_incoming: Callable[[Event], None] | None = None) -> Data:
    draft_id = ""
    try:
        policy_revision = revision()
        cfg = current()
        row, reason = inspect_event(cfg, ref)
        if reason:
            return decision(bridge, ref, "skipped", reason)
        draft_id, reason = claim(cfg, ref, people.same_person(ref["chat"])[0])
        if reason:
            return decision(bridge, ref, "skipped", reason, draft_id)
        event = build_event(cfg, bridge, ref, row)
        if policy_revision != revision() or paused():
            finish(draft_id, "skipped")
            return decision(bridge, ref, "skipped", "policy_changed", draft_id)
        if on_incoming:
            on_incoming(event)
        if row["kind"] == "call_offer":
            bridge.agent_reject(ref["chat"], ref["id"], policy_revision)
            reply = Reply(reply_text=cfg.agent.reject_message)
        else:
            reply = await generate(cfg, event)
        if reply.skip or revision() != policy_revision or paused():
            finish(draft_id, "skipped")
            return decision(
                bridge, ref, "skipped", "provider_skip" if reply.skip else "policy_changed", draft_id
            )
        path = render(cfg, reply, cfg.agent.voice.always or event.media_kind == "voice")
        try:
            payload = {
                "text": reply.reply_text,
                "sender": row["author"],
                "audio": base64.b64encode(path.read_bytes()).decode() if path else "",
            }
        finally:
            if path:
                shutil.rmtree(path.parent, ignore_errors=True)
        if cfg.agent.mode == "auto" and not cfg.agent.dry_run:
            return deliver(
                bridge,
                {
                    "id": draft_id,
                    "chat": ref["chat"],
                    "message_id": ref["id"],
                    "kind": ref["kind"],
                    **payload,
                },
                automatic=True,
                expected_revision=policy_revision,
            )
        if policy_revision != revision() or paused():
            finish(draft_id, "skipped")
            return decision(bridge, ref, "skipped", "policy_changed", draft_id)
        stored = db.store().draft_payload(draft_id, json.dumps(payload))
        with state.connect() as conn:
            conn.execute(
                "UPDATE agent_drafts SET status='drafted',at=?,payload=? WHERE id=?",
                (time.time(), stored, draft_id),
            )
        return decision(bridge, ref, "drafted", "dry_run" if cfg.agent.dry_run else cfg.agent.mode, draft_id)
    except Exception as exc:
        if draft_id:
            finish(draft_id, "failed")
        return decision(bridge, ref, "skipped", type(exc).__name__, draft_id)


async def test_connection(cfg: config.Config, voice: bool = False) -> Data:
    start = time.monotonic()
    event = Event(
        cfg.account,
        "sample",
        "Sample chat",
        "sample",
        "Sample sender",
        "sample-message",
        text="Hello, can you help?",
        media_kind="voice" if voice else "text",
        transcript="Hello, can you help?" if voice else "",
    )
    reply = await generate(cfg, event)
    provider_ms = round((time.monotonic() - start) * 1000)
    path = (
        await asyncio.to_thread(render, cfg, reply, voice or cfg.agent.voice.always)
        if not reply.skip
        else None
    )
    return {
        "provider": cfg.agent.provider,
        "preview": reply.reply_text[:500],
        "skip": reply.skip,
        "voice_file": str(path) if path else None,
        "provider_ms": provider_ms,
        "total_ms": round((time.monotonic() - start) * 1000),
    }


async def run(
    bridge: Bridge,
    on_decision: Callable[[Data], None] | None = None,
    *,
    on_incoming: Callable[[Event], None] | None = None,
) -> None:
    last_seen = 0.0
    while True:
        cfg = current()
        try:
            reconcile(cfg, bridge, on_decision=on_decision)
            if not cfg.agent.enabled:
                return
            ref = await asyncio.to_thread(bridge.agent_next)
            if time.monotonic() - last_seen >= 15:
                with state.connect() as conn:
                    state.set_meta(conn, "agent_last_poll", str(int(time.time())))
                last_seen = time.monotonic()
            if ref is not None:
                result = await handle(bridge, ref, on_incoming)
                if on_decision:
                    on_decision(result)
        except BridgeError as exc:
            log.warning("agent bridge unavailable (HTTP %d)", exc.status)
            await asyncio.sleep(3)
