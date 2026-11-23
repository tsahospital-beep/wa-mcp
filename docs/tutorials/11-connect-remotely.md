# 11. Connect remotely

Goal: connect MCP clients to the HTTP endpoint of the server, with a static token for Claude Code, Cursor
and VS Code, or with OAuth for a claude.ai custom connector. This takes about twenty minutes.

`wa-mcp serve --http` serves MCP over the Streamable HTTP transport at `/mcp`. Every request needs a bearer
token. You can try everything on your own computer first, where the server listens on `127.0.0.1:8765` and
needs no TLS. For a server on the internet, set it up first with [Server with Docker](10-server-with-docker.md).
The examples use `https://wa.example.com` for that case.

## Prerequisites

- The bridge is linked and running, see [First login](02-first-login.md).
- For a local try: the Python packages from [Install](01-install.md).
- For claude.ai: the server from the previous tutorial, reachable over HTTPS from the internet.

## Steps

### 1. Start the HTTP server

On your own computer, in the repository folder, in a second terminal next to the bridge:

```bash
uv run wa-mcp serve --http --config config.json
```

You should see a log line that the server listens on `127.0.0.1:8765`. The server refuses to listen on a
non-loopback address over plain HTTP. It starts there only when `server.public_url` is the https address of a
TLS proxy in front of it, or with `--insecure-lan`, which prints a warning and sends tokens and messages
unencrypted over your network. In the Docker setup the server is already running.

### 2. Create a token

Create one token per device, so that you can revoke one without touching the others.

```bash
uv run wa-mcp token create --name laptop
```

In Docker, run `docker compose exec server wa-mcp token create --name laptop` instead. You should see a
token that starts with `wam_`. It is shown once. Only a keyed hash is stored, so a lost token cannot be
shown again: revoke it and make a new one. Copy it into your password manager.

### 3. Test the token with curl

macOS and Linux:

```bash
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8765/mcp \
  -H "Content-Type: application/json" -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"curl","version":"1"}}}'
```

Run it once as it is, and once with `-H "Authorization: Bearer wam_..."` added, with your token. You should
see `401` without the token and `200` with it.

Windows PowerShell. Put the body in a file first:

```powershell
'{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"curl","version":"1"}}}' | Set-Content -Encoding ascii init.json
curl.exe -s -o NUL -w "%{http_code}`n" http://127.0.0.1:8765/mcp -H "Content-Type: application/json" -H "Accept: application/json, text/event-stream" -d "@init.json"
curl.exe -s -o NUL -w "%{http_code}`n" http://127.0.0.1:8765/mcp -H "Authorization: Bearer wam_..." -H "Content-Type: application/json" -H "Accept: application/json, text/event-stream" -d "@init.json"
```

You should see `401` for the first call and `200` for the second. Too many requests answer `429` with a
`Retry-After` header.

### 4. Add the token to a client

Claude Code, with the local address or your server's address:

```bash
claude mcp add --transport http wa-mcp https://wa.example.com/mcp \
  --header "Authorization: Bearer wam_..."
```

Cursor, in `~/.cursor/mcp.json` or `.cursor/mcp.json`:

```json
{
  "mcpServers": {
    "wa-mcp": {
      "url": "https://wa.example.com/mcp",
      "headers": { "Authorization": "Bearer wam_..." }
    }
  }
}
```

VS Code, in `.vscode/mcp.json`. VS Code asks for the token once and keeps it in its secret storage:

```json
{
  "inputs": [
    { "type": "promptString", "id": "wa-mcp-token", "description": "WA MCP token", "password": true }
  ],
  "servers": {
    "wa-mcp": {
      "type": "http",
      "url": "https://wa.example.com/mcp",
      "headers": { "Authorization": "Bearer ${input:wa-mcp-token}" }
    }
  }
}
```

The `claude_desktop_config.json` file of Claude Desktop starts local programs. The `mcp-remote` package
(Node.js) turns the remote server into one. Keep the token in the `env` block, not in the argument list:

```json
{
  "mcpServers": {
    "wa-mcp": {
      "command": "npx",
      "args": ["-y", "mcp-remote", "https://wa.example.com/mcp", "--header", "Authorization:Bearer ${WA_MCP_TOKEN}"],
      "env": { "WA_MCP_TOKEN": "wam_..." }
    }
  }
}
```

Then ask the assistant to run `bridge_status`. You should see the same answer as with a local server.

### 5. List and revoke tokens

```bash
uv run wa-mcp token list
uv run wa-mcp token revoke laptop
```

`token list` shows the id, name, creation time, last use and whether the token is revoked. After `revoke`
the next request with that token gets 401. Tokens do not expire on their own, so revoke the ones you no
longer use.

### 6. Turn on OAuth for claude.ai

A claude.ai custom connector uses OAuth 2.1. Add this to `config.json` on the server (the Docker config has
it on already). OAuth needs an https `server.public_url`, or a loopback address for local tests.

```json
{
  "server": {
    "public_url": "https://wa.example.com",
    "oauth": { "enabled": true, "approval": "link" }
  }
}
```

Restart the server. You should see no error, and `https://wa.example.com/.well-known/oauth-authorization-server`
should answer with JSON.

### 7. Add the custom connector

1. In claude.ai open **Settings**, then **Connectors**, then **Add custom connector**.
2. Give it a name and enter `https://wa.example.com/mcp`. Leave the OAuth client fields empty. Claude
   registers itself.
3. Click **Connect**. A WA MCP page opens that asks "Allow this app to use your WhatsApp?". It shows the app
   name, where it returns to (`claude.ai`) and a six-character request code.
4. Approve it. With `"approval": "link"` (the default) the server log has a line with the same request
   code and a one-time link, valid for 10 minutes. In Docker get it with
   `docker compose logs server | grep approval`. Without Docker, or when the link has expired, run
   `uv run wa-mcp oauth pending` for a fresh one. Open the link, check the code and click **Allow**. With
   `"approval": "password"` you type the admin password on the page instead, from `WA_MCP_ADMIN_PASSWORD`.

You should see the connector as connected in claude.ai, and its tools in a conversation. Connectors that you
add on claude.ai also appear in Claude Desktop and the mobile apps of the same account.

To accept only Claude as an OAuth client, limit the redirect addresses:

```json
{ "server": { "oauth": { "redirect_uri_prefixes": ["https://claude.ai/api/mcp/auth_callback"] } } }
```

### 8. Review and remove OAuth clients

```bash
uv run wa-mcp oauth list
uv run wa-mcp oauth revoke CLIENT_ID
```

`oauth list` shows each client, whether it was approved, its active grants and its last use. `revoke` removes
the client and all its tokens. Access tokens live 60 minutes and refresh tokens 30 days by default.

## Troubleshooting

- The server does not start and mentions plain HTTP. A non-loopback address needs an https
  `server.public_url` or `--insecure-lan`. For a try on your computer keep the default `127.0.0.1:8765`.
- Every request gets `401`. The token is wrong, revoked or missing the `Bearer ` prefix. Create a new one
  with `token create`. Repeated failures from one address are rate limited and then refused for a while.
- The approval link has expired. Links work for 10 minutes. Run `wa-mcp oauth pending` for a new one.
- `421` or `403` from behind a proxy. The `Host` and `Origin` headers are checked. Keep the `Host` header and
  set `server.trusted_proxies`. See [Run on a server](../guides/server.md).
- claude.ai cannot reach the server. It connects from the internet, so the address must be reachable
  over HTTPS and `server.public_url` must be that address.

## Next steps

- [Connect remotely](../guides/remote-clients.md): limits, discovery documents and what the server checks.
- [Remote mode security](../security/remote.md): threats, measures and a checklist.
- [Security hardening](13-security-hardening.md): the rest of the setup.
