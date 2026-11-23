# AI features and providers

By default WA MCP uses no AI model of its own. The assistant you connect (Claude, Cursor, a local model in
your client) does the thinking: it calls the tools, reads the results and writes the answer. Tasks,
analytics and `my_day` use plain rules on your machine.

You can add one optional background provider. It is off until you set `ai.provider`, and it is used by two
features only:

| Feature | What the provider does | Calls |
| --- | --- | --- |
| `my_day` with `summary=true` | Writes a short briefing from the structured result. | 1 per request |
| `read_images` with `describe=true` | Describes an image in up to three sentences. | 1 per image |

Nothing else calls the provider, and the provider cannot call tools. It only returns text, which the server
passes on as untrusted data.

Speech to text is separate and has its own settings, see [Speech to text with the same server](#speech-to-text-with-the-same-server).

## Providers

| `ai.provider` | What it is | Key needed | Default address |
| --- | --- | --- | --- |
| `none` | No provider. The default. | no | none |
| `ollama` | An [Ollama](https://ollama.com) server, usually on your own machine. | no | `http://127.0.0.1:11434` |
| `openai-compatible` | Any server that implements the OpenAI chat completions API: OpenAI, LM Studio, llama.cpp, vLLM and others. | when the server wants one | none, set `ai.base_url` |
| `anthropic` | The Anthropic Messages API. | yes | `https://api.anthropic.com` |

The code speaks plain HTTP with `httpx`. No vendor library is installed.

Settings for a local Ollama server:

```json
{
  "ai": {
    "provider": "ollama",
    "text_model": "llama3.2",
    "vision_model": "llava"
  }
}
```

An OpenAI-compatible server. `base_url` ends with the version part of the path, and the client adds
`/chat/completions`:

```json
{
  "ai": {
    "provider": "openai-compatible",
    "base_url": "http://127.0.0.1:1234/v1",
    "text_model": "my-local-model",
    "vision_model": "my-local-vision-model",
    "api_key_env": "WA_MCP_AI_API_KEY"
  }
}
```

Anthropic:

```json
{
  "ai": {
    "provider": "anthropic",
    "text_model": "claude-haiku-5-5",
    "vision_model": "claude-haiku-5-5",
    "api_key_env": "WA_MCP_AI_API_KEY"
  }
}
```

Model names change over time. Use a name that your provider lists. `text_model` is used for summaries and
`vision_model` for images, so a text-only model is enough when you only want summaries.

### The key

The key is never written to `config.json`. `ai.api_key_env` is the name of an environment variable, by
default `WA_MCP_AI_API_KEY`, and the server reads the key from it for each request. In an MCP client config,
put it in the server's `env` block:

```json
{
  "mcpServers": {
    "wa-mcp": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/wa-mcp", "wa-mcp", "--config", "/path/to/wa-mcp/config.json"],
      "env": { "WA_MCP_AI_API_KEY": "paste-the-key-here" }
    }
  }
}
```

Ollama and most local servers need no key. If the variable is set, it is sent as a bearer token. The
`anthropic` provider refuses to start without a key. Keys are never logged and never appear in error
messages or tool results.

## Costs and limits

Three limits stop a runaway loop before it costs money. They apply to every provider, local ones too.

| Setting | Default | Effect |
| --- | --- | --- |
| `ai.max_calls_per_day` | `200` | Calls per local day across all features. The next call is refused with a message that names the setting. `0` means no limit. |
| `ai.max_input_chars` | `12000` | Largest prompt (system text plus prompt) for one call. Larger input is refused before anything is sent. The summary cuts its list to fit. |
| `ai.timeout_s` | `60` | Seconds to wait for one answer. |

The daily count is kept in `store/insight.db`, in a table that holds only a date and a number. A call is
counted before it is sent, and a call that fails is still counted, because a failed request can be billed.
`read_images` stops cleanly when the limit is reached and reports it in `stopped`. `my_day` still returns
its structured result and puts the reason in `ai_error`.

The answer length is fixed per feature: 600 tokens for a summary and 400 for an image description.
Images are limited by `vision.max_image_mb` (default 20). A hosted provider bills images by size, so
describe a few images first and check the bill before you describe a whole archive. The daily limit does
not count speech to text.

## What leaves your machine

With `provider` set to `none`, nothing. With a provider on your own machine, the data goes to that process
only. With a hosted provider, the data goes to that company under its terms.

| Feature | Sent to the provider |
| --- | --- |
| `my_day` with `summary=true` | The date, the counts, the names of people and groups as the tools show them (a phone number when no name is known), the type and status of each item, due hints, waiting times, and one line of each item's context clipped to 120 characters. Message ids, JIDs and phone numbers are not sent. |
| `read_images` with `describe=true` | The image file itself. The OCR text is not sent. |

No other message text is sent. The tool descriptions say when a call may reach a remote service, and
`bridge_status` shows the configured provider as `ai_provider`.

## Untrusted content

Message text, names and image content are written by other people and can contain instructions aimed at a
model. Every prompt the server sends puts that content inside an `<untrusted_data>` block, removes any tag
of that name from the content, and tells the model in the system prompt to treat the block as material and
to ignore instructions inside it. For images, the system prompt says that text in the picture is part of
the picture. The provider has no tools and cannot send messages.

This reduces the risk and does not remove it. The text that comes back is itself treated as untrusted:
tool results carry the usual note, and your assistant should not follow instructions found in a summary or
description. All prompts live in `sdk/python/wa_mcp/ai/prompts.py`.

## Speech to text with the same server

Voice notes are transcribed by `transcription.backend`, not by `ai.provider`. An OpenAI-compatible server
that offers `/audio/transcriptions` works with the existing `http` backend, so there is no second code path.
Map the settings like this:

```json
{
  "ai": { "provider": "openai-compatible", "base_url": "https://api.example.com/v1", "api_key_env": "WA_MCP_AI_API_KEY" },
  "transcription": {
    "backend": "http",
    "http": {
      "url": "https://api.example.com/v1/audio/transcriptions",
      "model": "whisper-1",
      "api_key_env": "WA_MCP_AI_API_KEY"
    }
  }
}
```

The audio of your voice notes then goes to that server. See [Voice transcription](transcription.md).

## Errors

| Message | Cause and fix |
| --- | --- |
| `no AI provider configured` | `ai.provider` is `none`. Set it, or let your assistant do the work. |
| `set ai.text_model` or `set ai.vision_model` | The provider needs a model name for that task. |
| `set ai.base_url` | The `openai-compatible` provider has no default address. |
| `set the environment variable ...` | The `anthropic` provider needs its key. Add the variable to the server's environment. |
| `the AI provider is not reachable` | The server is not running or the address is wrong. For Ollama, start it with `ollama serve`. |
| `the AI provider refused the credentials` | The key is wrong or has no access to the model. |
| `the AI provider answered 404` | The address or the model name is wrong. For Ollama, run `ollama pull <model>`. |
| `above ai.max_input_chars` | The input is larger than the limit. Raise it, or ask for a smaller day or chat. |
| `ai.max_calls_per_day is N` | The daily limit is used up. Raise it or wait for the next day. |
| `the answer was cut off before any text` | A model that reasons before it answers used its whole output limit. Choose a model without long reasoning for this task. |
