"""List chats, read the latest messages and download one attachment with the Python SDK.

The bridge must be running and linked. The script reads through the bridge API, so it works with an
encrypted store and needs no key. It changes nothing except for `--download`, which saves one
attachment into `downloads_dir`.

    uv run python examples/python_sdk_basic.py
    uv run python examples/python_sdk_basic.py --config config.json --chats 10
    uv run python examples/python_sdk_basic.py --chat 15550100002 --messages 20 --download

Without `--chat` the script reads the chat with the most recent activity. With `--download` it saves the
newest attachment of that chat and prints the path.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from typing import Any

from wa_mcp import config
from wa_mcp.client import Bridge, BridgeError

MEDIA_KINDS = ("image", "video", "audio", "voice", "document", "sticker")


def when(ts: object) -> str:
    return datetime.fromtimestamp(int(str(ts))).strftime("%Y-%m-%d %H:%M") if ts else "-"


def short(text: object, width: int = 70) -> str:
    one_line = " ".join(str(text or "").split())
    return one_line if len(one_line) <= width else one_line[: width - 3] + "..."


def latest_attachment(messages: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The newest message that has a downloadable attachment. Messages come newest first."""
    return next((m for m in messages if m.get("kind") in MEDIA_KINDS), None)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="List chats and messages with the wa_mcp SDK")
    parser.add_argument("--config", help="path of config.json (default: WA_MCP_CONFIG or ./config.json)")
    parser.add_argument("--chats", type=int, default=5, help="how many chats to list")
    parser.add_argument("--chat", help="JID or phone number of the chat to read")
    parser.add_argument("--messages", type=int, default=5, help="how many messages to read")
    parser.add_argument("--download", action="store_true", help="save the newest attachment of the chat")
    args = parser.parse_args(argv)

    try:
        with Bridge(cfg=config.load(args.config)) as bridge:
            status = bridge.status()
            print(
                f"bridge {status.get('version', '?')}: connected={status.get('connected')} "
                f"logged_in={status.get('logged_in')} account={status.get('account') or '-'}"
            )

            chats = bridge.chats(limit=max(args.chats, 1))
            print(f"\nChats ({len(chats)}):")
            for chat in chats:
                kind = "group" if chat.get("is_group") else "direct"
                print(
                    f"  {when(chat.get('last_activity'))}  {kind:6}  {short(chat.get('title') or chat['jid'], 40)}"
                )

            target = args.chat or (chats[0]["jid"] if chats else None)
            if not target:
                print("\nNo chats yet. Let the bridge sync, then run this again.")
                return 0

            messages = bridge.messages(chat=target, limit=max(args.messages, 1))
            print(f"\nLast {len(messages)} messages, newest first:")
            for m in messages:
                who = "me " if m.get("outgoing") else "them"
                text = m.get("body") or f"[{m.get('kind')}]"
                print(f"  {when(m.get('sent_at'))}  {who}  {short(text)}")

            if args.download:
                media = latest_attachment(bridge.messages(chat=target, limit=200))
                if media is None:
                    print("\nNo attachment among the last 200 messages.")
                    return 0
                saved = bridge.download_media(target, str(media["msg_id"]))
                print(f"\nSaved the {saved.get('kind')} to {saved.get('path')}")
    except BridgeError as exc:
        sys.stderr.write(f"{exc}\n")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
