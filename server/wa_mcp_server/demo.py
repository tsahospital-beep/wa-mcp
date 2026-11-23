"""A self-contained fictional archive for the stdio demo."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from dataclasses import replace
from datetime import date, datetime, time, timedelta
from pathlib import Path

from wa_mcp import config, db

LINK_NOTICE = (
    "Demo mode: this tool needs a linked WhatsApp account. "
    "See https://github.com/tsahospital-beep/wa-mcp/blob/main/docs/getting-started.md."
)

# Only these tools may execute their bodies against the fictional archive.
OFFLINE_TOOLS = {
    "bridge_status",
    "list_accounts",
    "list_chats",
    "search_contacts",
    "get_chat",
    "list_messages",
    "get_message_context",
    "get_last_interaction",
    "list_calls",
    "export_chat",
    "export_period",
    "get_media_text",
    "list_stickers",
    "extract_file_text",
    "read_images",
    "transcribe_chat_audio",
    "check_links",
    "fetch_shared_docs",
    "my_day",
    "chat_analytics",
    "chat_analytics_compare",
    "find_mentions",
    "detect_tasks",
    "tasks_overview",
    "task_detail",
    "which_task",
    "refresh_tasks",
    "project_overview",
    "whats_new",
    "list_scheduled",
    "agent_status",
    "agent_drafts",
}


def cached_media(chat: list[str], source: str, after: db.TimeArg, limit: int) -> db.Row:
    """Count cached results and preview up to limit without opening a media backend."""
    kinds = ["audio", "voice"] if source == "transcript" else ["image"]
    rows = db.messages(conv=chat, kind=kinds, after=after, limit=None)
    derived = db.derived_many([(r["conv"], r["msg_id"]) for r in rows])
    items = [
        {"msg_id": r["msg_id"], "time": r["time"], "text": d["content"], "lang": d["lang"], "cached": True}
        for r in rows
        for d in derived.get((r["conv"], r["msg_id"]), [])
        if d["source"] == source
    ]
    return {
        "demo": True,
        "cached": items[: max(1, min(limit, 200))],
        "already_done": len(items),
        "candidates": len(rows),
        "failed": [],
        "remaining": len(rows) - len(items),
        "complete": len(items) == len(rows),
    }


OWNER = "15550100001@s.whatsapp.net"
MIRA = "15550100101@s.whatsapp.net"
TEO = "15550100102@s.whatsapp.net"
NIA = "15550100103@s.whatsapp.net"
PRESS = "15550100104@s.whatsapp.net"
IRA = "15550100105@s.whatsapp.net"
PRINT = "120363000000000011@g.us"
GARDEN = "120363000000000012@g.us"
FAMILY = "120363000000000013@g.us"

# Each conversation follows six details through planning, setup and the fair itself.
THREADS = (
    (MIRA, "Mira Vale", ("ticket roll", "cash box", "guest list", "receipt pad", "float", "ledger")),
    (TEO, "Teo Lark", ("map stand", "bike trailer", "chalkboard", "footstool", "cones", "bike rack")),
    (NIA, "Nia Reed", ("frame", "easel", "sketchbook", "ink sample", "paper roll", "poster")),
    (PRINT, "Lantern Print Club", ("brayer", "proof", "ink tray", "type case", "press bed", "drying rack")),
    (GARDEN, "Canal Garden", ("seed tray", "watering can", "planter", "herb box", "trellis", "seed tin")),
    (PRESS, "Finch Paper Works", ("sample pack", "stock list", "packing slip", "pallet", "crate", "carton")),
    (FAMILY, "Vale family", ("picnic rug", "thermos", "bread tin", "cooler", "basket", "folding chair")),
    (IRA, "Ira Moss", ("microphone", "headset", "recorder", "music stand", "speaker", "mixing desk")),
)
EXCHANGES = (
    ("I found the {item} for the fair.", "Thanks, I added the {item} to our list."),
    ("The {item} is ready for setup.", "Great, there is space for the {item} by the entrance."),
    ("The {item} is back in storage.", "Thanks for keeping track of the {item}."),
)
DOCUMENT = "Lantern Print Fair\nPaper order 104: 60 cream sheets, 20 blue sheets.\nPickup: west counter."
SIGN = "West entrance opens at nine"
TRANSCRIPT = "Meet me by the west gate. I will be holding a blue umbrella so you can spot me."


def _seed(conn: sqlite3.Connection, cfg: config.Config, today: date) -> None:
    def stamp(days_ago: int, hour: int = 0, minute: int = 0) -> int:
        return int(datetime.combine(today - timedelta(days=days_ago), time(hour, minute)).timestamp())

    def message(
        chat: str,
        mid: str,
        author: str,
        ts: int,
        text: str,
        kind: str = "text",
        source: str = "",
        name: str = "",
        mime: str = "",
    ) -> None:
        conn.execute(
            "INSERT INTO chat_messages (conv,msg_id,author,outgoing,sent_at,kind,body,attachment_name,mime,local_path)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                chat,
                mid,
                author,
                author == OWNER,
                ts,
                kind,
                "" if source else text,
                name or None,
                mime or None,
                str(cfg.downloads_dir / name) if name else None,
            ),
        )
        if source:
            conn.execute(
                "INSERT INTO derived_text (conv,msg_id,source,lang,content,created_at) VALUES (?,?,?,'en',?,?)",
                (chat, mid, source, text, ts),
            )

    conn.execute("INSERT INTO meta VALUES ('own_jid', ?)", (OWNER,))
    for index, (jid, title, items) in enumerate(THREADS):
        group = jid.endswith("@g.us")
        conn.execute("INSERT INTO conversations (jid,title,is_group) VALUES (?,?,?)", (jid, title, group))
        if not group:
            col = "business_name" if jid == PRESS else "full_name"
            conn.execute(f"INSERT INTO wa_contacts (jid,{col}) VALUES (?,?)", (jid, title))
        for turn in range(18):
            incoming, reply = EXCHANGES[turn // len(items)]
            item = items[turn % len(items)]
            peer = (MIRA, TEO, NIA)[turn % 3] if group else jid
            ts = stamp(29 - (27 * turn // 17), 8 + index, turn)
            mid = f"fair-{index}-{turn:02d}"
            if jid == IRA:
                message(jid, mid, peer, ts, incoming.format(item=item), "voice", "transcript")
            else:
                message(jid, mid, peer, ts, incoming.format(item=item))
            message(jid, mid + "-reply", OWNER, ts + 420, reply.format(item=item))

    message(
        PRESS, "paper-order", PRESS, stamp(2, 17), DOCUMENT, "document", "document", "order.txt", "text/plain"
    )
    message(PRINT, "gate-sign", NIA, stamp(2, 18), SIGN, "image", "image", "gate.svg", "image/svg+xml")
    message(GARDEN, "garden-table", TEO, stamp(1, 10), "The seed swap is at table four.")
    conn.execute("UPDATE chat_messages SET edited_at=? WHERE msg_id='garden-table'", (stamp(1, 10, 2),))
    conn.executemany(
        "INSERT INTO reactions (conv,msg_id,reactor,emoji,reacted_at) VALUES (?,'garden-table',?,?,?)",
        [
            (GARDEN, NIA, "\N{THUMBS UP SIGN}", stamp(1, 10, 3)),
            (GARDEN, MIRA, "\N{THUMBS UP SIGN}", stamp(1, 10, 4)),
        ],
    )
    message(TEO, "call-promise", OWNER, stamp(1, 14), "I will call you tomorrow.")
    message(FAMILY, "family-lunch", MIRA, stamp(1, 16), "Gracias por traer el pan para el picnic.")
    message(GARDEN, "garden-herbs", NIA, stamp(1, 16, 30), "Obrigada pelas mudas de manjericão!")
    message(
        NIA,
        "poster-size",
        NIA,
        stamp(1, 17),
        "Do you have the final poster size? Please send me the dimensions.",
    )
    message(
        MIRA,
        "invoice-request",
        MIRA,
        stamp(1, 18),
        "Can you send the invoice by Friday? Please list paper and ink separately.",
    )
    message(IRA, "west-gate", IRA, stamp(0), TRANSCRIPT, "voice", "transcript")
    conn.execute(
        "INSERT INTO call_log (call_id,peer,outgoing,video,started_at,outcome) VALUES ('call-nia',?,0,0,?,'missed')",
        (NIA, stamp(1, 17, 15)),
    )
    conn.execute(
        "UPDATE conversations SET last_activity=(SELECT MAX(sent_at) FROM chat_messages WHERE conv=jid)"
    )


def prepare(root: Path, today: date | None = None) -> config.Config:
    """Reuse one snapshot per local date, with a separate task cache and export directory for each."""
    today = today or date.today()
    directory = root.resolve() / f"wa-mcp-demo-v2-{today.isoformat()}"
    cfg = replace(
        config.isolated_defaults(
            directory,
            {
                "encryption": {"mode": "off", "efs": False},
                "mentions": {"watch": ["invoice"]},
                "tasks": {
                    "projects": [
                        {"id": "fair", "name": "Lantern Print Fair", "keywords": ["invoice", "paper"]}
                    ]
                },
            },
        ),
        demo=True,
    )
    cfg.store_dir.mkdir(parents=True, exist_ok=True)
    # Serialize concurrent clients and roll back the schema and rows together on failure.
    with closing(sqlite3.connect(cfg.wa_db)) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if tables:
            if (
                "meta" not in tables
                or not conn.execute(
                    "SELECT 1 FROM meta WHERE key='demo_date' AND value=?", (today.isoformat(),)
                ).fetchone()
            ):
                raise config.ConfigError(f"expected a fictional demo store at {cfg.wa_db}")
        else:
            schema = Path(__file__).with_name("demo_schema.sql").read_text(encoding="utf-8")
            for statement in schema.split(";"):
                if statement.strip():
                    conn.execute(statement)
            _seed(conn, cfg, today)
            conn.execute("INSERT INTO meta VALUES ('demo_date', ?)", (today.isoformat(),))
        cfg.downloads_dir.mkdir(exist_ok=True)
        svg = f'<svg xmlns="http://www.w3.org/2000/svg" width="420" height="64"><rect width="100%" height="100%" fill="white"/><text x="12" y="40" font-size="24">{SIGN}</text></svg>'
        for name, content in (("order.txt", DOCUMENT), ("gate.svg", svg)):
            path = cfg.downloads_dir / name
            if not path.exists():
                path.write_text(content, encoding="utf-8", newline="\n")
    return cfg
