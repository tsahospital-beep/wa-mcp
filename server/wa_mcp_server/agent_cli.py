"""Standalone connector worker and offline connection test."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys

from wa_mcp import accounts, config
from wa_mcp.agent import worker
from wa_mcp.agent.providers import diagnostic
from wa_mcp_server import common
from wa_mcp_server.tools.agent import review


def review_terminal(result: worker.Data) -> None:
    if result["reason"] != "approve":
        return
    if not sys.stdin.isatty() or not worker.current().sending.enabled:
        logging.getLogger(__name__).info("approval pending: approve_agent_draft %s", result["draft_id"])
        return
    with review(result["draft_id"]) as (draft, checked):
        prompt = checked.prompt("Send this agent reply?")
        sys.stderr.write(
            "".join(c if c.isprintable() or c == "\n" else " " for c in prompt) + "\nSend? [y/N] "
        )
        answer = sys.stdin.readline()
        if answer.strip().lower() == "y":
            worker.deliver(accounts.bridge(), draft)
        elif answer:
            worker.decline(accounts.bridge(), draft)


def main(argv: list[str]) -> int:
    from wa_mcp_server.app import setup_logging

    parser = argparse.ArgumentParser(prog="wa-mcp " + argv[0])
    parser.add_argument("--config")
    parser.add_argument("--account", default="default")
    if argv[0] == "agent-test":
        parser.add_argument("--voice", action="store_true")
    args = parser.parse_args(argv[1:])
    setup_logging("INFO")
    try:
        common.setup(config.load(args.config))
        with accounts.use(args.account) as h:
            if argv[0] == "agent-test":
                result = asyncio.run(worker.test_connection(h.cfg, args.voice))
                sys.stdout.write(json.dumps(result, ensure_ascii=False) + "\n")
            elif h.cfg.agent.enabled:
                asyncio.run(worker.run(h.bridge, review_terminal))
            else:
                sys.stderr.write("agent.enabled is false\n")
                return 2
    except KeyboardInterrupt:
        return 0
    except Exception as exc:
        sys.stderr.write(diagnostic(exc) + "\n")
        return 1
    finally:
        accounts.close()
    return 0
