# Documentation

WA MCP is a local bridge and MCP server that lets AI assistants search and read your WhatsApp chats, and
send messages when you allow it. Start with [Getting started](getting-started.md).

## Start here

| Page | What it covers |
| --- | --- |
| [Getting started](getting-started.md) | Install Go and uv, build the bridge, link your account, connect Claude. |
| [Tutorials](tutorials/README.md) | Fourteen lessons cover installation through connecting your own reply API. |
| [FAQ](faq.md) | Syncing, limits, common messages and how to reset. |

## Guides

| Page | What it covers |
| --- | --- |
| [Connect an MCP client](guides/mcp-clients.md) | Claude Desktop, Claude Code, Cursor, Windsurf, VS Code and others. |
| [Configuration](guides/configuration.md) | Every key of `config.json` with type, default and meaning. |
| [Several accounts](guides/accounts.md) | More than one WhatsApp number: config, `run-all`, linking, the `account` argument. |
| [Agent connector](guides/agent-connector.md) | Text and voice replies, provider presets, approvals and the SDK interface. |
| [Sending](guides/sending.md) | Enable sending, confirmation, human-like pacing, limits, quiet hours, scheduled messages. |
| [Import an exported chat](guides/import.md) | Load a WhatsApp "Export chat" file for history older than the phone sync: formats, who is who, duplicates, safety. |
| [Voice transcription](guides/transcription.md) | Backends, install scripts, languages. |
| [Text from photos and screenshots](guides/images.md) | OCR engines, `read_images`, descriptions, limits. |
| [AI features and providers](guides/ai-providers.md) | The optional background provider, keys, costs, limits and what leaves your machine. |
| [Run on a server](guides/server.md) | Docker Compose on a VPS with automatic HTTPS, linking through the proxy, keys and backups. |
| [Connect remotely](guides/remote-clients.md) | MCP over HTTP: tokens, OAuth for claude.ai connectors, Claude Code, Cursor, VS Code, Claude Desktop. |
| [Webhooks](guides/webhooks.md) | Events, payloads, retries, signature checks in Python and Node.js. |
| [Embed the bridge](guides/embedding.md) | Run the bridge inside a Go program with hooks and extra routes; add wa-mcp tools to another MCP server. |

## Reference

| Page | What it covers |
| --- | --- |
| [MCP tools](reference/tools.md) | All tools with arguments. Generated from the running server. |
| [HTTP API](reference/http-api.md) | Endpoints with curl examples. |
| [OpenAPI document](reference/openapi.yaml) | Machine-readable API description, also served by the bridge. |

## Security

| Page | What it covers |
| --- | --- |
| [Security and privacy](security.md) | What is stored, what listens, what is protected and what is not. |
| [Encryption and keys](guides/encryption.md) | Encryption modes, where keys and the token are kept, containers, moving a store, recovery, Windows EFS. |
| [Security audit](security/audit.md) | Threat model with the status of each threat and the tests that cover it. |
| [Remote mode security](security/remote.md) | Threats and measures for the HTTP server, tokens, OAuth and the Docker setup. |
| [Security policy](../SECURITY.md) | How to report a vulnerability. |

## Project

- [Changelog](../CHANGELOG.md)
- [Examples](../examples/README.md): the Python SDK, a webhook receiver and an opt-in auto-reply bot.
- [End-to-end check with a real account](testing/e2e.md)
- [Contributing](../CONTRIBUTING.md)
- [Third-party notices](../THIRD_PARTY_NOTICES.md)
- [License](../LICENSE)
