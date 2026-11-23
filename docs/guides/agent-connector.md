# Agent connector

Connect your reply API to WhatsApp. New text messages and transcribed voice notes from allowed chats go to the provider. Draft mode saves its answer locally and works with sending disabled.

## Quick starts

For OpenAI, set `OPENAI_API_KEY` and add this block to `config.json`.

```json
{"agent": {"enabled": true, "provider": "openai", "allow_chats": ["Alice"]}}
```

Run these examples from the repository root. Each config sets `agent.enabled=true`, selects a provider and lists one contact named Alice. Replace Alice with an exact address-book name or JID you control. If you already have a linked account, merge the example's `agent` block into your current config so the bridge keeps its store paths.

Use the same config for the bridge, worker and MCP server. After the connection test, start or restart the bridge in another terminal with that config, then run the worker. The examples use Bash. In PowerShell set environment variables with `$env:VARIABLE = "value"`.

### OpenAI

```bash
export OPENAI_API_KEY="your-key"
export WA_MCP_CONFIG=examples/agents/openai.json
# Set agent.allow_chats to your test contact.
uv run wa-mcp agent-test
uv run wa-mcp agent
```

### OpenAI-compatible

```bash
export WA_MCP_COMPATIBLE_API_KEY="your-key"
export WA_MCP_CONFIG=examples/agents/openai-compatible.json
# Set agent.url to the /v1 base URL and set agent.allow_chats.
uv run wa-mcp agent-test
uv run wa-mcp agent
```

### Anthropic

```bash
export ANTHROPIC_API_KEY="your-key"
export WA_MCP_CONFIG=examples/agents/anthropic.json
# Set agent.allow_chats to your test contact.
uv run wa-mcp agent-test
uv run wa-mcp agent
```

### Local Ollama

```bash
ollama pull llama3.2
export WA_MCP_CONFIG=examples/agents/ollama.json
# Start Ollama and set agent.allow_chats.
uv run wa-mcp agent-test
uv run wa-mcp agent
```

### Your own HTTP agent

```bash
export WA_MCP_AGENT_API_KEY="your-key"
export WA_MCP_CONFIG=examples/agents/http.json
# Set agent.url to the reply endpoint and set agent.allow_chats.
uv run wa-mcp agent-test
uv run wa-mcp agent
```

### ElevenLabs

```bash
export OPENAI_API_KEY="your-text-key" ELEVENLABS_API_KEY="your-voice-key"
export WA_MCP_CONFIG=examples/agents/elevenlabs.json
# Set agent.voice.voice_id and agent.allow_chats.
uv run wa-mcp agent-test --voice
uv run wa-mcp agent
```

### Local TTS command

```bash
ollama pull llama3.2
export WA_MCP_CONFIG=examples/agents/local-tts.json
# Install espeak-ng and ffmpeg, then set agent.allow_chats.
uv run wa-mcp agent-test --voice
uv run wa-mcp agent
```

For a local HTTP round trip, run `uv run python examples/agents/http_agent.py` in another terminal with the same key. A compatible text server without authentication can leave its key unset. The connector discovers its first model through `GET /models` unless you set `ai.text_model`. [HTTP agent example](../../examples/agents/http_agent.py)

## Run and review

| Mode | Behavior |
| --- | --- |
| `draft` (default) | Save a suggestion. Read it with `agent_drafts`. |
| `approve` | Ask in the worker terminal. A headless worker keeps the draft pending for `agent_approve_draft` in your MCP client. |
| `auto` | Submit eligible replies to the bridge. Choosing this mode authorizes unattended sends. |

Set `sending.enabled=true` to send in any mode. The MCP approval tool uses the existing elicitation and confirm-flag fallback, even when other sends have `sending.require_confirm=false`. Voice approvals include a local file to listen to. Text and voice sends use the bridge's pacing, quiet hours, duplicate checks and limits. A quiet-hours queue reports `queued` until its schedule completes. [Sending](sending.md)

`agent_status` shows the mode, pause switch, pending draft count and time of the worker's last successful poll. `agent_pause` persists across restarts and cancels pending agent schedules. A send already handed to WhatsApp may complete. The bridge checks the config revision and pause switch again after pacing and before releasing a queued reply. Editing the config invalidates agent sends queued under its previous revision. `agent_resume` accepts fresh events. Setting `agent.enabled=false` stops the worker on its next check. Restart the bridge after changing bridge settings. Use `agent.dry_run=true` to exercise live generation and save drafts. The `agent_test` MCP tool and `wa-mcp agent-test` CLI send a synthetic sample to the provider. They return a preview, timing and any generated local voice file. The test works before linking a number. Provider usage charges still apply.

Run one worker per number with `wa-mcp agent --account work`. Each account has its own feed, claims, drafts, pause switch and send counters. The bridge holds up to 256 live references. The worker skips references older than two minutes. History sync cannot trigger replies. A stopped or overloaded worker can miss events.

## Settings and privacy

| Setting under `agent` | Behavior |
| --- | --- |
| `allow_chats`, `deny_chats` | Empty allow list permits nobody. Deny takes precedence for both chat and sender. Use a direct JID or a name accepted by the send tools. List a group JID explicitly. Only a WhatsApp mention of the account qualifies. |
| `cooldown_s` | Default 60 seconds per chat, including equivalent phone and LID addresses. Replayed IDs, outgoing messages, recognized bots and quick quotes of outgoing messages are skipped. An unmarked bot running on a personal account can look like a person. |
| `history` | Default 10 prior messages in the chat, up to 100. The worker excludes denied senders and messages with pending or confirmed deletions. Set to 0 for no context. |
| `timeout_s` | Default 30 seconds per provider request, range 1 to 300. |
| `prompt_file` | Absolute path or relative to config.json. Empty uses the packaged neutral prompt. |
| `api_key_env`, `voice.api_key_env` | Environment variable names only. For text presets an explicit `agent.api_key_env` overrides the matching `ai.api_key_env`. The SDK can also read a provisioned per-store OS keystore item named `agent-<preset>` or `agent-voice-<preset>`. |

The text presets use the existing AI clients, input limit and daily budget. OpenAI defaults to `gpt-4o-mini`, Anthropic to `claude-haiku-4-5` and Ollama to `llama3.2`. Set `ai.text_model` for another model. Use `agent.url` to override the endpoint. The system prompt and untrusted conversation JSON occupy separate fields. The connector supplies no model tools. Your own endpoint or installed Python class controls its use of the data. [Default prompt](../../prompts/agent-default.md)

Vendor keys default to the vendor's own origin. An OpenAI or Anthropic preset pointed at a custom origin uses `WA_MCP_CUSTOM_TEXT_API_KEY`. A custom speech endpoint uses `WA_MCP_TTS_API_KEY` and accepts an unset key for local services. The corresponding vendor keystore item gains the suffix `-custom`. You can explicitly select another environment variable with `agent.api_key_env` or `agent.voice.api_key_env`. Text presets inherit an AI credential only when the configured AI provider and endpoint origin match.

The provider receives names, identifiers, message text, transcripts and selected history. TTS receives the proposed reply. Logs and decision webhooks contain metadata. Existing message webhooks retain their normal payloads. Drafts use the message store's field key when encryption is active. An interrupted send can have an unknown outcome. Its claim prevents an automatic retry.

## Voice

The voice provider defaults to `none`. When configured, TTS runs for incoming voice notes, an explicit provider request for voice, or `agent.voice.always=true`. The ElevenLabs preset requires a voice ID and defaults to `eleven_multilingual_v2`. For an OpenAI-compatible speech endpoint set `agent.voice.provider="http-tts"` and `agent.voice.url` to the full `/v1/audio/speech` URL. Its defaults are `tts-1`, voice `alloy` and `OPENAI_API_KEY`. [HTTP speech config](../../examples/agents/http-tts.json) [ElevenLabs speech API](https://elevenlabs.io/docs/api-reference/text-to-speech/convert)

A local program receives UTF-8 text on stdin and writes audio to the argument containing `{file}`. The worker runs the configured argument list directly. The existing ffmpeg converter reads the actual format and writes a mono Ogg/Opus voice note. The usual `WA_MCP_FFMPEG` override applies. [Local TTS config](../../examples/agents/local-tts.json)

## HTTP contract v1

The worker POSTs JSON with `account`, `chat`, `chat_name`, `from`, `from_name`, `message_id`, `text`, `transcript`, `media_kind`, `history`, `language_hint` and `event`. Calls use their call ID as `message_id`. Each history item has `from`, `kind` and `text`. Treat content as data. [Versioned request and response schema](../reference/agent-v1.schema.json)

The request includes `X-WA-MCP-Agent-Version: 1`. The bearer token doubles as the HMAC key. Verify `X-WA-MCP-Timestamp`, `X-WA-MCP-Delivery` and `X-WA-MCP-Signature` against the raw body. The SDK's `wa_mcp.webhooks.verify` checks freshness and duplicate deliveries. [Signature format](webhooks.md#request-format)

Return 2xx JSON such as `{"reply_text":"Thanks for the update."}`, `{"skip":true}` or `{"reply_audio_url":"https://audio.example.org/reply.ogg","voice_note":true}`. For inline audio use `reply_audio_base64`. An optional `audio_mime` describes it. Supplied audio takes precedence over text, which can serve as its preview. Public URLs require HTTPS on its default port. Configured loopback endpoints may use HTTP and a custom port. Downloaded audio receives no credentials. A loopback HTTP agent may return an audio URL on its own origin. Other audio URLs require public HTTPS. Redirects are refused. Caps are 256 KiB per HTTP agent request, 12,000 reply characters and 8 MiB of audio.

## Python providers and hooks

Install a module in the worker environment and set `agent.provider="your_package:Assistant"`. Its class takes no constructor arguments and implements `async reply(event: Event) -> Reply`. Import both types and the `AgentProvider` protocol from `wa_mcp.agent`. For the example put `examples` on `PYTHONPATH` and select `agents.custom_provider:Assistant`. Installed classes and TTS programs run with the worker's OS permissions. [Custom class](../../examples/agents/custom_provider.py)

`Bridge.agent_next()` consumes a live message or call reference. Pass it to `await worker.handle(bridge, reference)` inside the matching `accounts.use(...)` context, or use `await worker.run(bridge, on_incoming=inspect_event, on_decision=inspect_decision)` from `wa_mcp.agent.worker`. To observe alongside the worker, subscribe to webhooks. Incoming messages use `message`. Missed calls use `call` with outcome `missed`. Drafts and deliveries use `agent.reply` with status `drafted`, `sent`, `queued` or `skipped`, the input ID, chat, reason and draft ID when assigned. [Webhooks](webhooks.md)

## Calls

A linked device cannot answer a WhatsApp call by voice. Live call audio requires the official WhatsApp Business Calling API on a business number, outside this connector. [WhatsApp Business Platform](https://developers.facebook.com/documentation/business-messaging/whatsapp/overview)

With `calls.record=true` and `agent.follow_up_calls=true`, a missed or rejected incoming direct call can produce one follow-up in the configured mode. Use `agent.voice.always=true` for spoken follow-ups. In auto mode, `agent.auto_reject=true` rejects an eligible direct call and sends the short `agent.reject_message` through the usual sender. Paused and dry-run workers leave calls alone. Group calls lack a message mention and are skipped.
