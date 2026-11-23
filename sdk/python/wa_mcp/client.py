"""Client for the bridge HTTP API v1 (see docs/reference/openapi.yaml).

The token comes from api.token in config.json, from the OS keystore (encryption.mode
keystore) or from store/api.token, whichever the bridge used. Every failure is raised as BridgeError with
the HTTP status (0 when the bridge is not reachable) and the bridge message.
"""

from __future__ import annotations

from typing import Any

import httpx

from wa_mcp import config as config_mod
from wa_mcp import keystore
from wa_mcp.db import TimeArg, to_unix

# Same as api.MinTokenLen in the bridge.
MIN_TOKEN_LEN = 32

# Seconds per request type. Sends include human-like waits; media may wait for a re-upload.
TIMEOUTS = {"default": 15.0, "send": 180.0, "media": 120.0, "import": 900.0, "connect": 5.0}


class BridgeError(Exception):
    """A bridge request failed. status is the HTTP status, 0 when unreachable."""

    def __init__(self, status: int, message: str, data: dict[str, Any] | None = None) -> None:
        super().__init__(f"bridge error {status}: {message}" if status else message)
        self.status = status
        self.message = message
        self.data = data or {}  # the JSON body of the answer, for example candidates of an import


def _base_url(listen: str) -> str:
    host, _, port = listen.rpartition(":")
    host = host.strip("[]")
    if host in ("", "0.0.0.0", "::"):  # noqa: S104
        host = "127.0.0.1"
    if ":" in host:
        host = f"[{host}]"
    return f"http://{host}:{port}/api/v1"


def load_token(cfg: config_mod.Config) -> str:
    """api.token, else the token the bridge keeps in the OS keystore (encryption.mode keystore),
    else store/api.token (the fallback when no keystore is available)."""
    if cfg.api.token:
        return cfg.api.token
    if cfg.encryption.mode == "keystore":
        try:
            value = keystore.get(cfg.store_dir, keystore.ITEM_API_TOKEN)
        except keystore.KeystoreError:
            value = None
        token = value.decode("ascii", "replace").strip() if value else ""
        if len(token) >= MIN_TOKEN_LEN:
            return token
    try:
        return cfg.token_file.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _when(value: TimeArg) -> str | None:
    ts = to_unix(value)
    return None if ts is None else str(ts)


def _drop_none(data: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in data.items() if v is not None and v not in ([], {})}


class Bridge:
    """Typed access to the bridge API. Use as a context manager or call close()."""

    def __init__(
        self,
        base_url: str | None = None,
        token: str | None = None,
        cfg: config_mod.Config | None = None,
        transport: httpx.BaseTransport | None = None,
        account: str | None = None,
    ) -> None:
        """account picks another entry of "accounts" of the same config file, so the client talks to the
        bridge of that WhatsApp number (its own api.listen and token)."""
        cfg = cfg or config_mod.load()
        if account:
            cfg = cfg.for_account(account)
        self.base_url = (base_url or _base_url(cfg.api.listen)).rstrip("/")
        self._token = token if token is not None else load_token(cfg)
        self._http = httpx.Client(
            base_url=self.base_url,
            timeout=httpx.Timeout(TIMEOUTS["default"], connect=TIMEOUTS["connect"]),
            transport=transport,
            trust_env=False,
        )

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> Bridge:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _call(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
        timeout: str = "default",
    ) -> dict[str, Any]:
        if not self._token:
            raise BridgeError(
                0, "no API token: start the bridge once (it writes store/api.token) or set api.token"
            )
        try:
            resp = self._http.request(
                method,
                path,
                params=_drop_none(params) if params else None,
                json=body,
                headers={"Authorization": f"Bearer {self._token}"},
                timeout=httpx.Timeout(TIMEOUTS[timeout], connect=TIMEOUTS["connect"]),
            )
        except httpx.TimeoutException as exc:
            raise BridgeError(0, f"bridge did not answer in time ({path})") from exc
        except httpx.HTTPError as exc:
            raise BridgeError(0, f"bridge not reachable at {self.base_url}: is wa-bridge running?") from exc
        try:
            data = resp.json()
        except ValueError:
            data = None
        if resp.status_code >= 400 or not isinstance(data, dict) or data.get("ok") is False:
            message = data.get("error") if isinstance(data, dict) else None
            raise BridgeError(
                resp.status_code,
                str(message or resp.reason_phrase or "unexpected answer"),
                data if isinstance(data, dict) else None,
            )
        data["http_status"] = resp.status_code
        return data

    # read

    def status(self) -> dict[str, Any]:
        return self._call("GET", "/status")

    def chats(self, query: str | None = None, limit: int = 50, offset: int = 0) -> list[dict[str, Any]]:
        data = self._call("GET", "/chats", params={"query": query, "limit": limit, "offset": offset})
        return list(data.get("chats") or [])

    def messages(
        self,
        chat: str | None = None,
        after: TimeArg = None,
        before: TimeArg = None,
        query: str | None = None,
        ascending: bool = False,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        params = {
            "chat": chat,
            "after": _when(after),
            "before": _when(before),
            "query": query,
            "order": "asc" if ascending else "desc",
            "limit": limit,
            "offset": offset,
        }
        return list(self._call("GET", "/messages", params=params).get("messages") or [])

    def calls(self, since: TimeArg = None, limit: int = 50) -> list[dict[str, Any]]:
        return list(
            self._call("GET", "/calls", params={"since": _when(since), "limit": limit}).get("calls") or []
        )

    def openapi(self) -> str:
        if not self._token:
            raise BridgeError(0, "no API token")
        try:
            resp = self._http.get("/openapi.yaml", headers={"Authorization": f"Bearer {self._token}"})
        except httpx.HTTPError as exc:
            raise BridgeError(0, f"bridge not reachable at {self.base_url}") from exc
        if resp.status_code >= 400:
            raise BridgeError(resp.status_code, resp.reason_phrase)
        return resp.text

    # act

    def agent_next(self) -> dict[str, Any] | None:
        """Consume the next live message or call reference for this account."""
        event = self._call("POST", "/agent/next").get("event")
        return event if isinstance(event, dict) else None

    def agent_decision(self, decision: dict[str, Any]) -> None:
        self._call("POST", "/agent/reply", body=decision)

    def agent_reject(self, chat: str, call_id: str, revision: str) -> None:
        self._call("POST", "/agent/reject", body={"chat": chat, "id": call_id, "revision": revision})

    def preview_text(self, text: str, mentions: list[str] | None = None) -> dict[str, Any]:
        return self._call("POST", "/send/preview", body={"text": text, "mentions": mentions or []})

    def send(
        self,
        to: str,
        text: str | None = None,
        media_path: str | None = None,
        as_voice: bool = False,
        sticker_path: str | None = None,
        reply_to: dict[str, str] | str | None = None,
        mentions: list[str] | None = None,
        human: bool = True,
        agent_revision: str | None = None,
    ) -> dict[str, Any]:
        """Send through the bridge pipeline. A 202 answer means queued for quiet hours."""
        if isinstance(reply_to, str):
            reply_to = {"id": reply_to}
        body = _drop_none(
            {
                "to": to,
                "agent_revision": agent_revision,
                "text": text or None,
                "media_path": media_path,
                "as_voice": as_voice or None,
                "sticker_path": sticker_path,
                "reply_to": reply_to,
                "mentions": mentions,
            }
        )
        body["human"] = human
        return self._call("POST", "/send", body=body, timeout="send")

    def reply(
        self, to: str, reply_to_id: str, text: str, mentions: list[str] | None = None, human: bool = True
    ) -> dict[str, Any]:
        return self.send(to, text=text, reply_to={"id": reply_to_id}, mentions=mentions, human=human)

    def mark_read(self, chat: str, ids: list[str], author: str | None = None) -> dict[str, Any]:
        return self._call("POST", "/read", body=_drop_none({"chat": chat, "ids": ids, "author": author}))

    def request_history(self, chat: str, count: int = 50, before_id: str | None = None) -> dict[str, Any]:
        count = max(1, min(count, 100))
        return self._call(
            "POST", "/history", body=_drop_none({"chat": chat, "count": count, "before_id": before_id})
        )

    def download_media(self, chat: str, msg_id: str) -> dict[str, Any]:
        return self._call("POST", "/media/download", body={"chat": chat, "id": msg_id}, timeout="media")

    def avatar(self, jid: str) -> dict[str, Any]:
        return self._call("GET", "/avatar", params={"jid": jid}, timeout="media")

    def import_export(
        self,
        path: str,
        chat: str,
        me: str | None = None,
        date_order: str | None = None,
        dry_run: bool = False,
        timezone: str | None = None,
        names: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Import an exported chat file (.txt or .zip) that is inside downloads_dir, exports_dir or
        import.allowed_dirs. Nothing is sent to WhatsApp. See POST /api/v1/import."""
        body = _drop_none(
            {
                "path": path,
                "chat": chat,
                "me": me,
                "date_order": date_order,
                "timezone": timezone,
                "dry_run": dry_run or None,
                "names": names,
            }
        )
        return self._call("POST", "/import", body=body, timeout="import")

    # schedule

    def schedule(
        self,
        to: str,
        at: TimeArg,
        text: str | None = None,
        media_path: str | None = None,
        reply_to: str | None = None,
        mentions: list[str] | None = None,
        recurrence: str = "once",
    ) -> dict[str, Any]:
        when = to_unix(at)
        if when is None:
            raise ValueError("at is required")
        body = _drop_none(
            {
                "to": to,
                "at": str(when),
                "text": text or None,
                "media_path": media_path,
                "reply_to": reply_to,
                "mentions": mentions,
                "recurrence": recurrence,
            }
        )
        return self._call("POST", "/schedule", body=body)

    def list_scheduled(self, status: str | None = None) -> list[dict[str, Any]]:
        return list(self._call("GET", "/schedule", params={"status": status}).get("scheduled") or [])

    def cancel_scheduled(self, scheduled_id: int) -> dict[str, Any]:
        return self._call("DELETE", f"/schedule/{int(scheduled_id)}")
