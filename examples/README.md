# Examples

Small programs that use the bridge API and webhooks. Run them from the repository root with `uv run`, after
`uv sync`. Live examples need a running, linked bridge. The agent connection test uses synthetic input.

| File | What it does | Changes anything |
| --- | --- | --- |
| [Agent providers](agents/) | Ready configs, a signed HTTP agent and a local Python provider. See [Agent connector](../docs/guides/agent-connector.md). | `wa-mcp agent-test` only calls the provider. Live sends depend on the selected mode. |
| [`python_sdk_basic.py`](python_sdk_basic.py) | Lists chats, prints the latest messages of one chat and can download one attachment. | Only with `--download`, which saves a file into `downloads_dir`. |
| [`webhook_receiver.py`](webhook_receiver.py) | Receives webhooks, checks the signature, the timestamp window and the delivery id, and prints one line per event. | No. |
| [`auto_reply_bot.py`](auto_reply_bot.py) | Answers allowed contacts by itself. Off until you set `WA_MCP_BOT_ENABLE=yes`. | Yes: it sends WhatsApp messages. Read the warning at the top of the file first. |

## python_sdk_basic.py

```bash
uv run python examples/python_sdk_basic.py
uv run python examples/python_sdk_basic.py --chat 15550100002 --messages 20 --download
```

It reads through the bridge API with `wa_mcp.client.Bridge`, so it works with an encrypted store. See
[HTTP API](../docs/reference/http-api.md) for what the calls return.

## webhook_receiver.py

```bash
export WA_MCP_WEBHOOK_SECRET="$(openssl rand -hex 32)"
uv run python examples/webhook_receiver.py --port 9000
```

In Windows PowerShell set the secret with `$env:WA_MCP_WEBHOOK_SECRET = "..."`. Add the address to the
`webhooks` list in `config.json`, set the same secret for the bridge process and restart the bridge. The
steps are in [Webhooks and the Python SDK](../docs/tutorials/12-webhooks-and-python-sdk.md) and the event
format is in [Webhooks](../docs/guides/webhooks.md).

## auto_reply_bot.py

This program sends messages to real people. Use a test account and a test contact first.

```bash
export WA_MCP_WEBHOOK_SECRET="$(openssl rand -hex 32)"
export WA_MCP_BOT_ALLOW="15550100002"
export WA_MCP_BOT_ENABLE=yes
uv run python examples/auto_reply_bot.py --dry-run
```

`--dry-run` logs each decision and sends nothing. The bot answers only direct chats from the allow-list, never
in quiet hours, at most once per cooldown and a few times per day. It also needs `sending.enabled` in
`config.json`. Automated use of a WhatsApp account may go against the WhatsApp terms of service. See
[Scheduled and human-like sending](../docs/tutorials/08-scheduled-and-human-like-sending.md).

## Checks

The examples are type-checked with `mypy --strict` and linted with ruff like the rest of the repository.
Their tests are in `examples/tests` and use only the loopback interface and fake data:

```bash
uv run pytest examples/tests
```
