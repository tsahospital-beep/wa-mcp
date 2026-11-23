# 13. Security hardening

Goal: check how your store is protected, close the gaps that the defaults leave, and set up habits for
linked devices, logs and backups. This takes about thirty minutes. The defaults are safe: everything stays
on your machine, the API listens on 127.0.0.1 only, sending is off and message text is encrypted. This
tutorial verifies that and adds the parts that need your action.

## Prerequisites

- The bridge is linked and running, see [First login](02-first-login.md).
- A client is connected, see [Connect clients](03-connect-clients.md).

## Steps

### 1. Check what protects the store

Ask the assistant: "Run bridge_status." Or ask the bridge directly.

macOS and Linux:

```bash
curl -H "Authorization: Bearer $(./wa-bridge api-token)" http://127.0.0.1:8080/api/v1/status
```

Windows PowerShell:

```powershell
curl.exe -H "Authorization: Bearer $(.\wa-bridge.exe api-token)" http://127.0.0.1:8080/api/v1/status
```

You should see a `protection` object, for example:

```json
"protection": {"api_token": "dpapi", "efs": "on", "field_encryption": "keystore", "key_store": "dpapi"}
```

`field_encryption` says how message text in `wa.db` is encrypted. `key_store` and `api_token` say where
the key and the API token are kept: Windows DPAPI, the macOS Keychain or the Linux Secret Service. The start
log of the bridge has the same facts in the line that begins with `database:`. If `field_encryption` shows
`off`, set `encryption.mode` back to `keystore` in `config.json`. The mode `off` leaves message text in
plain form. It refuses a database that is already encrypted.

### 2. Know what the encryption covers

Field encryption covers message text, attachment names, quoted text, reactions, scheduled message text,
media keys, transcripts and extracted file text. It does not cover `session.db` (the keys of the linked
device), downloads, exports, chat ids, titles or timestamps. The key protects against other users and other
computers. It does not protect against a program that runs as you: such a program can ask the keystore for
the key. Read [Security and privacy](../security.md#what-is-not-protected) once.

### 3. Protect the session file

`session.db` lets anyone who holds it act as your linked device until you remove the device on the phone.

Windows. The bridge turns on EFS for the store folder. Check one file:

```powershell
cipher /c store\session.db
```

You should see that the file is encrypted. Windows Home, FAT drives and network shares have no EFS. The
bridge logs that and carries on. Turn on BitLocker as well. Check it in an administrator terminal:

```powershell
manage-bde -status
```

macOS has no per-file encryption for a normal user. Turn on FileVault and check it:

```bash
fdesetup status
```

Linux. Use full disk encryption such as LUKS. A mapped encrypted volume shows `crypt` in the type column:

```bash
lsblk -o NAME,TYPE,FSTYPE,MOUNTPOINT
```

You should see `FileVault is On.` on macOS, and a `crypt` entry on Linux if the system disk is encrypted.

### 4. Check the permissions

At every start the bridge limits the store folder to your user. Look at the result.

macOS and Linux:

```bash
ls -ld store
ls -l store
```

You should see `drwx------` for the folder and `-rw-------` for the files.

Windows PowerShell:

```powershell
icacls store
```

You should see only your account and SYSTEM in the list. Keep `store_dir` inside your home folder: the bridge
warns when it is elsewhere, because the parent folder may be readable by others. Exports and reports are
plain text even when the database is encrypted, so keep `exports_dir` and `downloads_dir` in private
folders too.

### 5. Look at your linked devices

Open WhatsApp on the phone, go to **Linked devices** and look at the list. The bridge appears as `WA MCP`.
Remove entries that you do not recognize. Do this every few weeks.

The bridge also watches for signs that someone else uses the session: another client with the keys of this
session (`session_replaced`), a remote logout (`logged_out`), a new device on your account (`device_added`)
and a different account at login (`account_mismatch`). Ask the assistant to run `bridge_status` and read the
security events, or look at `security_events` in the status answer. You can also receive them as a
`security` webhook, see [Webhooks and the Python SDK](12-webhooks-and-python-sdk.md).

If a warning shows that you cannot explain, remove the device in WhatsApp, stop the bridge, delete the
`store` folder and link again.

### 6. Keep the network small

- Keep `api.listen` on a loopback address (`127.0.0.1:8080`). The API has no TLS, and its token gives full
  control, including sending when it is enabled. The bridge warns when the address is not loopback.
- Do not share the API token. `wa-bridge api-token` prints it, so do not paste its output into chats or
  tickets.
- Use `https` for any webhook that leaves your machine, and keep the secret in `secret_env`, never in
  `config.json`.
- For remote access use one token per device and revoke the ones you stop using, see
  [Connect remotely](11-connect-remotely.md).

### 7. Keep sending narrow

If you use sending, see [Scheduled and human-like sending](08-scheduled-and-human-like-sending.md). The
settings that matter here:

```json
{
  "sending": {
    "enabled": true,
    "allow_confirm_flag": false,
    "allowed_media_dirs": [],
    "limits": { "per_chat_per_hour": 10, "per_hour": 30, "per_day": 100 },
    "quiet_hours": { "enabled": true, "start": "22:00", "end": "08:00", "action": "queue" }
  }
}
```

Message text is written by other people and can contain instructions aimed at your assistant. The server
tells clients to treat it as data, which reduces the risk and does not remove it. Prefer a client that asks
you to approve each send and shows each tool call, and leave `allow_confirm_flag` off. Turn sending off
when you do not need it.

### 8. Read the logs, and keep them private

The bridge logs to its terminal. It never writes the one-time login token, the API token or keys to its
log. The MCP server writes its log to standard error. Add `"--log-level", "INFO"` to its
arguments when you need more detail, and read it in the client's MCP log. The server in HTTP mode writes
approval links for OAuth to its log on purpose when `approval` is `link`, so keep that log private or use
`"approval": "password"`.

Look for lines that start with `warning:`. The bridge prints one for every config key it does not know, for a
`store_dir` outside your home folder, for an `api.listen` that is not loopback and for a webhook without a
secret.

### 9. Back up with care

Stop the bridge first, then copy the store folder. Remember what it holds. The field-encrypted `wa.db` is
unreadable without its key, but `session.db` is not field-encrypted, and an archive made by a tool such as
`tar` or `Compress-Archive` reads the files in clear, also from an EFS folder. Encrypt the archive.

macOS and Linux:

```bash
tar czf - store | gpg --symmetric --cipher-algo AES256 -o store-backup.tgz.gpg
```

Windows PowerShell, with [7-Zip](https://www.7-zip.org/) installed. It asks for the password:

```powershell
7z a -p -mhe=on store-backup.7z store
```

The key in the OS keystore does not travel with a copy. A copy restored on another computer or in another
Windows account cannot be opened, by design, and there is no recovery without the key. If you need
backups that you can restore elsewhere, use `encryption.mode` `keyfile` or `passphrase` from the start, keep a
copy of the key or passphrase offline, and never store it next to the backup. See
[Encryption and keys](../guides/encryption.md).

## Troubleshooting

- `field_encryption` is `off`. `encryption.mode` is `off` in `config.json` or in `WA_MCP_ENCRYPTION_MODE`.
  Remove it or set `keystore`.
- `no OS keystore is available` on Linux. Install `libsecret-tools` and run a Secret Service such as GNOME
  Keyring, or set `encryption.mode` to `passphrase` or `keyfile`. The bridge never falls back to plain text by
  itself.
- `... holds no key for this store`. The store was copied from another computer or user, or its keystore item
  was removed. It cannot be opened here.
- `protection.efs` is not `on` on Windows. The edition of Windows or the drive may not support EFS, and the
  bridge logs why. Use BitLocker.
- The store folder is readable by others. The bridge warns when `store_dir` is outside your home folder.
  Move it inside, or fix the access list of the parent folder.

## Next steps

- [Security and privacy](../security.md): what is stored where, what listens and what is not protected.
- [Security audit](../security/audit.md): the threat model and the tests that cover each point.
- [Encryption and keys](../guides/encryption.md): modes, containers and recovery.
- To report a vulnerability, see [SECURITY.md](../../SECURITY.md).
