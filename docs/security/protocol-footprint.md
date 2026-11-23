# Bridge network requests

The table covers calls the bridge initiates through whatsmeow and the separate avatar HTTP fetch. A message limit counts message parts. It does not bound the number of network packets. "On demand" means the bridge has no separate requests-per-minute limit for that operation.

| Operation | Trigger | Limit enforced by the bridge |
| --- | --- | --- |
| Connect, disconnect, QR pairing, phone-code pairing and logout | Startup, login flow, account mismatch or shutdown | One login flow per account at a time. The pinned library retries transient disconnects. Its first delay can be zero and increases by two seconds per failed attempt. Permanent disconnects are reported separately. |
| Send a message or upload an attachment | Direct, scheduled, embedded or agent send | Shared reservations. Defaults are 20 parts per chat per hour, 60 per account per hour and 300 per account per day. Identical requests are limited to three distinct chat addresses per hour. Each attachment part can require an upload. |
| Chat presence | A paced outgoing part | One composing indicator followed by one pause on the normal completion path. Voice uses the audio indicator. |
| Read receipt | Explicit read, configured auto-read or mark before reply | Up to 200 IDs per explicit request and 20 before a reply, grouped by author. Auto-read handles one live incoming message at a time. All require read permission. |
| History request to the linked phone | A history request with a stored WhatsApp anchor | On demand. The HTTP API requests 50 messages by default, at most 100 per request. |
| Media download and re-upload receipt | An attachment request without a verified local copy | On demand. An expired download can trigger one request to the phone and one download retry. Only one re-upload request for the same chat and message can be outstanding. |
| Profile picture lookup and avatar download | An avatar request | One metadata lookup, then a fetch if a URL exists. On demand. The image limit is 10 MiB. |
| Group metadata | A missing chat title or a check of group admin privileges | One worker per account with 256 queue slots. Successful lookups are cached for one hour. Failed lookups wait 30 minutes before another attempt for that group. |
| Own linked-device list | Connection and periodic security monitoring | First check after 30 seconds, then every six hours while connected. |
| Full app-state fetch | A collection fails an integrity check | One recovery sequence per collection per client instance. Up to three attempts with waits of 1, 2 and 4 seconds and a one-minute timeout per attempt. |
| Reject an incoming call | An enabled auto-reject policy and a matching active direct call | One decision per worker claim. The bridge checks the call state, policy revision and 30-second age limit. |

Whatsmeow manages the websocket, keepalives, keys, acknowledgements, delivery receipts and its own sync traffic. The bridge has no account-wide online timer, presence subscription worker or link-preview crawler. Contact directory refreshes copy local SQLite data. SDK link checks, provider calls and signed webhook deliveries have separate destinations and controls. [Webhook delivery](../guides/webhooks.md) and [agent providers](../guides/agent-connector.md)

## Updating the client

Dependabot checks Go modules weekly. From `bridge/`, use `go list -m -json go.mau.fi/whatsmeow@latest` to compare the upstream revision with `go.mod`. Review the upstream changes, update to the chosen revision with `go get`, and run `go mod tidy`, `go vet ./...`, both Go test modes and `govulncheck ./...`. Run the Python session-schema test against the downloaded module and the pairing payload tests. These checks cover local compatibility. A test account is needed to verify a real link.

The library supplies the protocol version. Device name and platform come from the account's login configuration at pairing. WhatsApp's server-side detection rules are private. Pacing cannot guarantee that an account will avoid restrictions. [Sending settings](../guides/sending.md) and [upstream library](https://github.com/tulir/whatsmeow)
