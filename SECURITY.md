# Security policy

## Reporting a vulnerability

Report problems privately. Do not open a public issue for a security problem.

Use GitHub private vulnerability reporting: open the **Security** tab of this repository and choose
**Report a vulnerability**. Include the version, your operating system, the steps to reproduce and what
an attacker could do. Do not include real chats, phone numbers, tokens or session files. Use
placeholder data.

We aim to answer within a week and to publish a fix and a note in the changelog once the problem is
confirmed and fixed.

## Supported versions

| Version | Supported |
| --- | --- |
| 0.1.x | yes |

Only the latest release receives fixes while the project is below 1.0.

## Threat summary

WA MCP runs on your own machine and keeps your WhatsApp data there. The full description is in
[docs/security.md](docs/security.md). In short:

- The bridge API and the login window listen on 127.0.0.1 only. The API needs a bearer token, and both
  servers refuse requests whose Host header does not match, which blocks DNS rebinding. The login link
  carries a one-time token that is exchanged for a session cookie.
- The store folder is limited to your user (a protected access list on Windows, mode 0700 and 0600 on macOS
  and Linux). One store belongs to one WhatsApp account, and only one bridge can use it at a time.
- Sending is off by default. The MCP tools ask you to approve each send through the client when it supports
  MCP elicitation. Clients without it fall back to `confirm=true`, which the model sets itself and which is
  a weaker check. Recipients must be a JID, a phone number, an alias or an exact address-book name. Files to
  send must be in the allowed folders, and the store folder and the config file are never sent. The bridge
  applies limits, quiet hours and duplicate protection.
- Message text, transcripts, file text, reactions and names are untrusted input. The MCP server tells
  clients not to follow instructions found in them. This reduces prompt injection risk and does not remove
  it.
- Webhooks carry a timestamp and an HMAC-SHA256 signature over the timestamp and the body. Receivers should
  reject timestamps older than 5 minutes.
- Received files are read, never executed. Archives are listed without unpacking. Link fetching accepts
  public `https://docs.google.com` documents only, and the request comes from your own computer.
- Field encryption is on by default. It protects message text, attachment names, quoted text, reactions,
  scheduled text, media keys, transcripts and extracted file text in `store/wa.db`, with a random key kept
  in the OS keystore of the user (Windows DPAPI, macOS Keychain, Linux Secret Service), or with a passphrase
  or a key file. The API token is kept in the keystore too. A copy of the store opened on another computer
  or by another Windows user cannot be decrypted. It does not protect downloads, exports, chat ids, titles
  or timestamps.
- `store/session.db` holds the keys of the linked device and cannot be field-encrypted. It is limited to
  the user, encrypted with Windows EFS by default, and on macOS and Linux it relies on full disk
  encryption. The bridge records a remote logout, another client with the same session keys, a new device
  on the account and a different account at login, and reports them in the status and a `security`
  webhook.
- Anyone who can run code as your user, including an administrator or malware, can read the store, ask
  the keystore for the key and the token, and use the API. Messages deleted for everyone stay in the local
  database. Check **Linked devices** on your phone from time to time.

The threat model, with the tests that cover each point, is in [docs/security/audit.md](docs/security/audit.md).

Out of scope: attacks that need code execution as your user, vulnerabilities in WhatsApp itself, and
account restrictions imposed by WhatsApp.
