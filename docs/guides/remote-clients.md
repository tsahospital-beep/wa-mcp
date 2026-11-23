# Connect remotely

`wa-mcp serve --http` serves MCP over the Streamable HTTP transport at `/mcp`. Every request needs a
bearer token. Clients get one in one of two ways:

| Way | For | How |
| --- | --- | --- |
| Static token | Claude Code, Cursor, VS Code, Claude Desktop through `mcp-remote`, scripts | `wa-mcp token create --name laptop` prints a token once. The client sends it as `Authorization: Bearer ...`. |
| OAuth 2.1 | claude.ai custom connectors, Claude Desktop connectors, any client that supports MCP authorization | The client registers itself, opens a consent page on your server, and you approve it. |

The examples use `https://wa.example.com`. Set up the server first: [Run on a server](server.md). For a
server on your own machine, `wa-mcp serve --http` listens on `http://127.0.0.1:8765` and needs no TLS.

## Start the HTTP server

```bash
wa-mcp serve --http --config /path/to/config.json                 # 127.0.0.1:8765
wa-mcp serve --http --listen 127.0.0.1:9000                        # another port
```

The server refuses to listen on a non-loopback address over plain HTTP. It starts there only when
`server.public_url` is the https address of a TLS proxy in front of it and `server.trusted_proxies` lists
the address of that proxy (then only the proxy and loopback may connect), or with `--insecure-lan`, which
prints a warning and sends tokens and messages unencrypted over your network.

## Static tokens

```bash
wa-mcp token create --name laptop     # shows the token once
wa-mcp token list                     # id, name, created, last used, revoked
wa-mcp token revoke laptop            # by name or id; the next request with it gets 401
```

In Docker, prefix the commands with `docker compose exec server`. Tokens start with `wam_`. Only a keyed
hash is stored, so a lost token cannot be shown again: revoke it and create a new one. Use one token per
device, so that you can revoke one without touching the others.

### Claude Code

```bash
claude mcp add --transport http wa-mcp https://wa.example.com/mcp \
  --header "Authorization: Bearer wam_..."
```

### Cursor

In `~/.cursor/mcp.json` (or `.cursor/mcp.json` in a project):

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

### VS Code

In `.vscode/mcp.json`. VS Code asks for the token once and keeps it in its secret storage:

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

### Claude Desktop with a token

The `claude_desktop_config.json` file starts local programs. The `mcp-remote` package (Node.js) turns the
remote server into one. Keep the token in the `env` block instead of the argument list:

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

### Check a token with curl

```bash
curl -s https://wa.example.com/mcp \
  -H "Authorization: Bearer wam_..." \
  -H "Content-Type: application/json" -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"curl","version":"1"}}}'
```

A wrong or revoked token answers `401`. Too many requests answer `429` with `Retry-After`.

## OAuth (claude.ai and other connectors)

Turn it on in `config.json` (the Docker config has it on already):

```json
{
  "server": {
    "public_url": "https://wa.example.com",
    "oauth": { "enabled": true, "approval": "link" }
  }
}
```

OAuth needs an https `server.public_url`, or a loopback address for local tests.

### claude.ai custom connector

1. In claude.ai open Settings, Connectors, Add custom connector.
2. Name it, enter `https://wa.example.com/mcp` and add it. Leave the OAuth client fields empty: Claude
   registers itself.
3. Click Connect. A WA MCP page opens: "Allow this app to use your WhatsApp?". It shows the app name, where
   it returns to (`claude.ai`) and a six-character request code.
4. Approve it:
   - with `"approval": "link"` (default): the server log has a line with the same request code and a
     one-time link, for example
     `oauth: approval needed for Claude (request K7PM2Q); open within 10 minutes: https://wa.example.com/oauth/approve?ticket=...`.
     Get it with `docker compose logs server | grep approval`, or print a fresh link with
     `wa-mcp oauth pending`. Open it, check the code, click Allow. The first page continues by itself.
   - with `"approval": "password"`: type the admin password (`WA_MCP_ADMIN_PASSWORD` or
     `WA_MCP_ADMIN_PASSWORD_FILE` on the server) on the page and click Allow.

Connectors added on claude.ai also appear in Claude Desktop and the mobile apps of the same account.

### Which clients may register

`server.oauth.redirect_uri_prefixes` decides which redirect addresses a client may register. The default
allows Claude and local desktop clients only:

```json
[
  "https://claude.ai/api/mcp/auth_callback",
  "https://claude.com/api/mcp/auth_callback",
  "http://localhost:",
  "http://127.0.0.1:",
  "http://[::1]:"
]
```

The two Claude addresses are the callback that Anthropic documents for custom connectors, together with
the claude.com address it may move to. Add the redirect address of another client to the list, for
example a private-use scheme such as `cursor://...`, or set `[]` to allow any https, loopback http or
private-use address. To accept only Claude, leave out the loopback entries.

### Other OAuth clients

Claude Code and VS Code also run the OAuth flow when you add the server without a header; they open the
consent page in your browser and use a redirect address on `127.0.0.1` or `localhost`, which the default
list allows. For a client with another redirect address, add it to `server.oauth.redirect_uri_prefixes`
or use a static token.

### What the server checks

- Discovery: `/.well-known/oauth-protected-resource/mcp` (RFC 9728) names the authorization server, and
  `/.well-known/oauth-authorization-server` (RFC 8414) lists the endpoints. A `401` from `/mcp` points to
  the first document in its `WWW-Authenticate` header.
- Registration (`/register`, RFC 7591): 1 to 5 redirect URIs each, allowed by
  `server.oauth.redirect_uri_prefixes`, without user info or fragment. At most
  `server.oauth.max_clients` approved clients (default 20); clients that were never approved count
  separately, at most 10, each kept for an hour, and the oldest is dropped first. Each address may
  register `server.rate_limit.registrations_per_ip_per_hour` clients (default 10). Registration alone gives
  no access.
- Authorization: code flow with PKCE, `S256` only. The redirect URI must match a registered one exactly
  and must be sent with the request. The `state` value comes back unchanged. The `resource` parameter
  (RFC 8707), when sent, must be this server's `/mcp` address. For a client that was never approved,
  errors are answered by the server itself and never sent to its redirect URI, so the server cannot be
  used as an open redirect.
- Consent: every authorization needs your approval. The request is bound to the browser window that
  opened it by a cookie, and only that window receives the code. The form carries a value derived from
  that cookie, and requests expire after 10 minutes. The page shows the app name, where it returns to,
  when and from which address it registered, and whether it was ever approved. Wrong admin passwords slow
  down that address: from the third one on it waits 30 seconds, doubling each time up to 15 minutes, and
  10 wrong passwords from all addresses within 15 minutes stop password approval for that time. There are
  no ID tokens, so the OpenID Connect `nonce` does not apply.
- Tokens: access tokens live `server.oauth.access_token_minutes` (60), refresh tokens
  `server.oauth.refresh_token_days` (30). Each refresh returns a new pair and spends the old refresh
  token. A spent refresh token or a used code that comes back revokes the whole grant.
- Revocation: `/revoke` (RFC 7009) ends the grant of the token. `wa-mcp oauth revoke CLIENT_ID` removes
  a client and all its tokens.

```bash
wa-mcp oauth list                    # clients, approval state, active grants, last use
wa-mcp oauth pending                 # waiting requests with fresh approval links
wa-mcp oauth revoke 3f0c...          # remove a client and its tokens
```

## Limits and browser access

| Setting | Default | Effect |
| --- | --- | --- |
| `server.rate_limit.per_token_per_minute` | 120 | Requests per token. |
| `server.rate_limit.per_ip_per_minute` | 240 | Requests per client address. |
| `server.rate_limit.auth_per_ip_per_minute` | 20 | OAuth requests, consent form posts and failed bearer checks per address. When used up, `/mcp` refuses that address too. |
| `server.rate_limit.registrations_per_ip_per_hour` | 10 | Client registrations per address. |
| `server.max_request_kb` | 256 | Largest request body. |
| `server.allowed_hosts` | derived | Host headers accepted besides the public host and the loopback names. |
| `server.allowed_origins` | derived | Origin headers accepted besides the public origin. Requests without Origin pass. |
| `server.cors_origins` | `[]` | CORS is off. Add an origin here only for a browser-based client such as the MCP Inspector. |

All keys are in [Configuration](configuration.md#remote-server).
