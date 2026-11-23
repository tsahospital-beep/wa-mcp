# Remote mode security

Remote mode (`wa-mcp serve --http`, the Docker setup in [Run on a server](../guides/server.md)) makes your
WhatsApp reachable over the network. Whoever holds a valid token can read your chats and, when sending is
on, send in your name. This page lists what protects that access, what it assumes and what it does not
cover. The general notes are in [Security and privacy](../security.md).

## Defaults

- `wa-mcp` without arguments still speaks stdio only. HTTP starts only with `serve --http`.
- The HTTP server listens on `127.0.0.1:8765`. A non-loopback address over plain HTTP is refused unless
  `server.public_url` is https (a TLS proxy is declared) or `--insecure-lan` is given, which logs a warning.
- Every `/mcp` request needs a bearer token. There are no tokens until you create one, and OAuth is off
  until `server.oauth.enabled` is true.
- CORS is off. Host and Origin headers are checked against allow-lists.
- The bridge API stays on 127.0.0.1. In Docker the MCP server shares the network namespace of the bridge,
  so the API is not reachable from the proxy or the host.

## Threats and measures

| Threat | Measure | Limits |
| --- | --- | --- |
| Someone on the network reads tokens or messages | TLS at the proxy; plain HTTP only on loopback or with `--insecure-lan`; behind a proxy on a non-loopback address `server.trusted_proxies` is required and every other peer gets 403 | Between the proxy and the server the traffic is plain HTTP on the same host or container network. |
| A stolen static token | One token per device, `wa-mcp token revoke`, `last used` in `token list` | A token does not expire by itself. Revoke tokens you no longer use. |
| A stolen OAuth access token | Lifetime 60 minutes by default, bound to this server's `/mcp` resource | It works until it expires or its grant is revoked. |
| A stolen refresh token | Rotation on every use; a spent refresh token that comes back revokes the grant | The first use by an attacker works if it comes before the next use by the real client. |
| A copy of `server.db` | Tokens, client secrets, codes, approval tickets and consent cookies are stored as HMAC-SHA256 with a pepper kept in `server.key` or `WA_MCP_SERVER_PEPPER` | A copy of the whole store folder includes `server.key`. Keep the pepper in the environment or a secret file to separate them. |
| Guessing tokens | 256 random bits per token; failed bearer checks count against `auth_per_ip_per_minute` and lock the address for `/mcp` | Behind a proxy, set `server.trusted_proxies`, or every client shares the proxy's address. |
| A malicious app registers itself | Only redirect addresses in `server.oauth.redirect_uri_prefixes` (default: Claude and loopback); 10 registrations per address and hour; unapproved clients capped at 10, kept for an hour, oldest dropped; `max_clients` counts approved clients only; registration gives no access. Every authorization needs the owner's approval, and the consent page shows when and from which address the app registered | An owner who approves a request they did not start gives that app access. Check the app name, the return address, the registration details and the request code. |
| Approval without the owner | Password mode: keyed hashes compared in constant time; per address a growing wait from the third wrong password (30 seconds, doubling, up to 15 minutes); 10 failures from all addresses in 15 minutes lock password approval. Link mode: a one-time link with 256 random bits, valid 10 minutes, printed to the server log and by `wa-mcp oauth pending` | In link mode everyone who can read the server log can approve. Keep logs private, or use password mode. |
| Stealing the code in the browser, open redirects | PKCE S256 only, exact redirect URI match, the code is valid 2 minutes and once, the request is bound to the browser that started it; errors for a client that was never approved are answered by the server, not sent to its redirect URI | A redirect URI on a host the attacker controls must be refused at registration, which the default `redirect_uri_prefixes` does. |
| Cross-site requests and DNS rebinding | Host allow-list (421), Origin allow-list (403), no CORS, consent forms with a token derived from a SameSite cookie, `form-action` and `frame-ancestors` in the page policy | Clients without an Origin header pass the Origin check; they still need a token. |
| Large or many requests | `server.max_request_kb` (256), rate limits per token and per address, a limit on open sessions | The limits live in memory and start again after a restart. |
| Secrets and chat ids in logs | The access log has method, path, status and address only. Tokens never appear in logs. The HTTP client of the server logs at WARNING only, so bridge URLs with chat JIDs (phone numbers) are not logged. Caddy access logs are off | Link mode writes approval links to the log on purpose. |
| The login page on the internet | `login.public.url` serves it only with a one-time link from `wa-bridge login-url`, exchanged for a Secure, HttpOnly cookie; it runs only while no device is linked | Whoever opens a fresh link while the bridge waits for a login could link their own phone, and the store would then belong to that account. Open the link right away. |
| A compromised container | Non-root user, read-only root file system, no capabilities, `no-new-privileges`; the database key comes from a Docker secret, outside the data volume; the server reads names from `wa.db`, not from `session.db` | The `server` container reads the store and the bridge API token by design. `session.db` is in the volume `wa-session`, which only the bridge mounts. |

## What is not protected

- Root on the VPS can read the volume, the `.env` file, the passphrase in the container environment and
  the memory of the processes.
- `session.db` is not encrypted. Whoever copies it can act as your linked device until you remove the
  device on your phone.
- Message content you send to a cloud assistant leaves your server under that assistant's terms.
- The MCP server tells clients that message text is untrusted, and send tools ask for approval when the
  client supports it. A remote client that cannot ask needs `sending.allow_confirm_flag`, which lets the
  model approve its own sends. Leave sending off unless you need it.

## Checklist

1. Use the Caddy profile or another TLS proxy. Do not use `--insecure-lan` outside a network you trust.
2. Keep the VPS firewall to ports 80 and 443. Do not publish 8765 or 8766 on a public interface.
3. Create one token per device and revoke the ones you stop using.
4. For OAuth, prefer `"approval": "password"` when other people can read the server logs, and set
   `redirect_uri_prefixes` to the clients you use.
5. Set `WA_MCP_PASSPHRASE` (or a key file) for the database and back up the volume encrypted.
6. Check Linked devices on your phone and `wa-mcp oauth list` from time to time.
