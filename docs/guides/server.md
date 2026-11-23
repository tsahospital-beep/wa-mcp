# Run on a server

This page puts the bridge and the MCP server on a VPS with Docker, so that MCP clients can reach your
WhatsApp from anywhere, also while your computer is off. Caddy in front of it gets a certificate for your
domain and serves everything over HTTPS. To connect a client afterwards, read
[Connect remotely](remote-clients.md). The security notes are in [Remote mode security](../security/remote.md).

> The images have yet to be built on a real Docker host. On pushes to `main`, CI builds both images
> and checks the preview stack without connecting to WhatsApp.

## What runs

| Container | Listens on | Reachable from |
| --- | --- | --- |
| `bridge` | API on `127.0.0.1:8080`, login page on `8766` | The API only inside its own network namespace. The login page only through Caddy, with a one-time link. |
| `server` | `8765` (MCP over Streamable HTTP, OAuth, `/health`) | Caddy, and `127.0.0.1:8765` on the host. It shares the network namespace of `bridge`, so it reaches the API on 127.0.0.1. Only loopback and `server.trusted_proxies` can reach routes other than `/health`. |
| `caddy` (profile `caddy`) | `80`, `443` | The internet. It forwards `/login/*` to the login page and everything else to the MCP server. |

Both WA MCP containers run as user 65532 with a read-only root file system, no capabilities and
`no-new-privileges`. They share one volume, `wa-data`, mounted at `/data`: the store, downloads and exports.
`session.db`, which holds the keys of the linked device, is in a second volume, `wa-session`, that only the
bridge mounts (`session_file` is `/session/session.db`). The key of the database comes from a Docker secret. Logs rotate at 10 MB, five files per container. The
health check of `server` also asks the bridge, and `server` restarts with `bridge`.

## Before you start

- A Linux VPS with Docker Engine and the Compose plugin.
- A domain or subdomain with an A (or AAAA) record that points to the VPS, for example `wa.example.com`.
- Ports 80 and 443 open in the firewall of the VPS. Keep every other port closed.

## Three steps

1. Get the code, write the settings and make the database key:

   ```bash
   git clone https://github.com/tsahospital-beep/wa-mcp.git
   cd wa-mcp/deploy/docker
   cp env.example .env && chmod 600 .env
   sh secrets/make-key.sh
   ```

   In `.env`, set `WA_MCP_DOMAIN` to your domain and `TZ` to your time zone (quiet hours and schedules use
   it). `make-key.sh` writes `secrets/wa_mcp.key` (32 random bytes, owned by user 65532, mode 0600) and asks
   for sudo to change the owner. Keep a copy of the key outside the server: without it the stored message
   text cannot be read.

2. Build and start:

   ```bash
   docker compose --profile caddy up -d --build
   docker compose ps
   ```

   Wait for `bridge` and `server` to show `healthy` and for Caddy to obtain its certificate.
   `curl https://wa.example.com/health` answers `{"ok": true}`.

3. Link WhatsApp. Ask the bridge for a one-time link to its login page:

   ```bash
   docker compose exec bridge wa-bridge login-url
   ```

   Open the link (it looks like `https://wa.example.com/login/?t=...`) in your browser and scan the QR
   code with WhatsApp on your phone: Settings, Linked devices, Link a device. The link works once. Run the
   command again for a new one. The bridge log shows the QR code too:
   `docker compose logs -f bridge`.

Then connect a client as described in [Connect remotely](remote-clients.md). The short version: for Claude
Desktop, Cursor or Claude Code create a token with
`docker compose exec server wa-mcp token create --name laptop`, and for a claude.ai custom connector use
`https://wa.example.com/mcp` and approve the request with the link from `docker compose logs server`.

## Settings

The images contain [`deploy/docker/config.container.json`](../../deploy/docker/config.container.json) as
`/config/config.json`: store under `/data`, the bridge API on 127.0.0.1, the login page for the proxy, the
MCP server on `0.0.0.0:8765` behind the proxy (`server.trusted_proxies` lists the Docker networks) and OAuth
on with link approval. To change anything, copy it to `deploy/docker/config.json` (ignored by git), edit
the copy and uncomment the `./config.json:/config/config.json:ro` lines of both services. Keep the paths
under `/data` and the two `listen` values. Every key is described in [Configuration](configuration.md).

Environment variables in `.env`:

| Variable | Meaning |
| --- | --- |
| `WA_MCP_DOMAIN` | Required. The public host name. It sets `server.public_url` and `login.public.url`, and Caddy gets its certificate. |
| `TZ` | Time zone of both containers, for example `Europe/Berlin`. Default `UTC`. Quiet hours, scheduled messages and `get_my_day` use it. |
| `WA_MCP_ENCRYPTION_MODE` | `keyfile` (default here, the key from `secrets/wa_mcp.key`) or `passphrase`. See below. |
| `WA_MCP_PASSPHRASE`, `WA_MCP_PASSPHRASE_FILE` | The passphrase, or a file with it, for mode `passphrase`. |
| `WA_MCP_ADMIN_PASSWORD` | Admin password for the OAuth consent page when `server.oauth.approval` is `password`. |
| `WA_MCP_PORT` | Host port of the MCP server on 127.0.0.1, default `8765`. Empty picks a random port. |
| `WA_MCP_LOG_LEVEL` | Log level of the WhatsApp library: `ERROR`, `WARN`, `INFO`, `DEBUG`. |

Secrets can also come from files, which fits Docker secrets: the programs read `WA_MCP_PASSPHRASE_FILE`,
`WA_MCP_ADMIN_PASSWORD_FILE` and `WA_MCP_SERVER_PEPPER_FILE` when the plain variables are empty. The
folder [`deploy/docker/secrets`](../../deploy/docker/secrets/README.md) holds them; git and the image
build ignore everything in it except its README and `make-key.sh`.

## Keys and encryption on a server

A server has no OS keystore (no Windows DPAPI, macOS Keychain or desktop Secret Service), so the
`keystore` mode of `encryption.mode` is not available in the containers. Pick one of these:

| `encryption.mode` | Where the key comes from | Notes |
| --- | --- | --- |
| `keyfile` | The Docker secret `wa_mcp_key` (`secrets/wa_mcp.key`), mounted at `/run/secrets/wa_mcp_key` in both containers | The default of `compose.yaml`. The key is outside the `wa-data` volume, so a copy of the volume alone does not open the message text. The file must belong to user 65532 with mode 0600. |
| `passphrase` | `WA_MCP_PASSPHRASE` or the file in `WA_MCP_PASSPHRASE_FILE`, for both containers | A passphrase in `.env` is visible in the container environment to root on the VPS; a file from a Docker secret is not. |
| `off` | none | Message text is stored in plain form. Use it only with full disk encryption of the VPS. |

Encryption protects message text, names and related fields in `wa.db`. It does not cover `session.db`, which
lets anyone who has it act as your linked device. Treat backups of the `wa-data` volume as secrets.

The MCP server reads names and the LID map from `wa.db`, where the bridge copies them, and does not need
`session.db`. In this Compose file it never sees the file: `session.db` is in the volume `wa-session`, which
only the bridge mounts.

The server keeps its own secrets in `store/server.db`: tokens and OAuth state, all as HMAC-SHA256 keyed with
a pepper from `store/server.key` (created on first start) or from `WA_MCP_SERVER_PEPPER`. Changing the
pepper invalidates all tokens.

## Without Caddy

If the VPS already runs a TLS proxy (nginx, Traefik, a cloud load balancer), start without the profile:

```bash
docker compose up -d --build
```

Point the proxy at `127.0.0.1:8765` and keep the `Host` header (nginx: `proxy_set_header Host $host;`,
plus `proxy_buffering off;` for the event stream). The connection reaches the container from a Docker
network address, which `server.trusted_proxies` in the built-in settings covers. Forward `/login/` to port
8766 of the `bridge` container if you want the login page through the proxy; it is not published on the
host by default.

For a private setup without a domain, reach the server through an SSH tunnel instead:
`ssh -L 8765:127.0.0.1:8765 you@vps`, then use `http://127.0.0.1:8765/mcp` on your computer. The server
still needs `WA_MCP_DOMAIN` for its public address; any name works when only the tunnel is used, and then
`server.oauth` is not usable from claude.ai.

## Try it without WhatsApp

Run the preview before starting the live stack or on a separate host. It shares the live Compose project
and network. The preview uses fake QR codes. Compose mounts the same key file as in the live setup.
Run `sh secrets/make-key.sh` first if you have not created the key.

```bash
WA_MCP_DOMAIN=wa.example.test WA_MCP_PORT= \
  docker compose -f compose.yaml -f compose.preview.yaml up -d --build --wait
docker compose -f compose.yaml -f compose.preview.yaml ps
curl "http://$(docker compose -f compose.yaml -f compose.preview.yaml port bridge 8765)/health"
```

## Updates, backups, logs

- Update: `git pull`, then `docker compose --profile caddy up -d --build`.
- Backup: stop the stack (`docker compose stop`) and copy the `wa-data` volume, for example with
  `docker run --rm -v wa-mcp_wa-data:/data -v "$PWD":/backup busybox tar czf /backup/wa-data.tgz /data`.
  Copy `wa-session` the same way if you want to keep the link (it holds the device keys; encrypt that
  archive), and back up `secrets/wa_mcp.key` separately. Without `wa-session` you link the device again.
- Logs: `docker compose logs -f server` shows approval links and OAuth events, `docker compose logs -f bridge`
  the connection and the QR code. Neither prints tokens, the access log has no query strings, and the HTTP
  client of the server does not log request lines.
- Unlink: remove the device under Linked devices on your phone, then `docker compose down -v` deletes the
  volumes with all data.
