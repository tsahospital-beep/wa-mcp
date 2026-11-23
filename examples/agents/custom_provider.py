"""A local provider. Select agents.custom_provider:Assistant with examples on PYTHONPATH."""

from wa_mcp.agent import Event, Reply


class Assistant:
    async def reply(self, event: Event) -> Reply:
        if event.event in ("missed_call", "rejected_call"):
            return Reply(reply_text="I missed your call. Could you leave a message?")
        return Reply(reply_text="Thanks for your message.")
