# Encryption and keys

The bridge encrypts the message text in `store/wa.db` by default. The key is a random 32-byte store key
that the operating system keeps for your user in the default mode. Reading encrypted fields from a copy
requires access to the key. This page explains the modes, where keys are kept and what to do
in containers and on servers. What the encryption covers, and what it does not, is in
[Security and privacy](../security.md#field-encryption).

## Modes

Set `encryption.mode` in `config.json` (or `WA_MCP_ENCRYPTION_MODE`):

| Mode | Key | Use it for |
| --- | --- | --- |
| `keystore` (default) | A random key in the OS keystore of your user. | Desktops and laptops. Nothing to set up on Windows and macOS. |
| `passphrase` | Argon2id of the passphrase in the variable named by `encryption.passphrase_env`. | Machines without a keystore, when you want to type or inject a passphrase. |
| `keyfile` | A random key in the file `encryption.key_file` (or `WA_MCP_KEY_FILE`). | Containers and servers, for example with a Docker secret. |
| `off` | None. | Only when you protect the disk in another way and need plain text in `wa.db`. |

A store that is already encrypted always opens with the kind of key it was encrypted with, whatever the
mode says, except `off`, which refuses it. The mode decides how a new or plain store is encrypted. A store
from an older version that used `encryption.enabled` and a passphrase keeps working without changes.

## Where the keys are

| System | Store key | API token |
| --- | --- | --- |
| Windows | `store/key.dpapi`, wrapped with DPAPI for your user | `store/api.token.dpapi`, wrapped the same way |
| macOS | Login Keychain, service `wa-mcp`, account `store-key.<id>` | Login Keychain, account `api-token.<id>` |
| Linux | Secret Service through `secret-tool`, attributes `service wa-mcp`, `account store-key.<id>` | Same, `account api-token.<id>` |

`<id>` is the random id in `store/keystore.id`. It is not a secret. It keeps the items of a store when you
move the folder on the same machine. The bridge passes secrets to `security` and `secret-tool` on standard
input, never as program arguments, and never writes them to its log.

The MCP server reads the same items as the same user, so it needs no extra setting. When you set
`api.token` (or `WA_MCP_API_TOKEN`) yourself, that token is used and nothing is stored. To use the token in
a script, run `wa-bridge api-token`.

## The first start

When encryption is enabled and `wa.db` contains plain fields to protect, the bridge encrypts them in place.

1. It writes a plain copy, `wa.db.pre-encryption`, next to the database.
2. It takes an exclusive lock on the file when no other program has it open, encrypts every protected
   field and records pending compaction in one transaction. It opens each encrypted value to check it.
   If the transaction fails, the bridge keeps the original rows and the copy.
3. It overwrites and deletes the copy, and logs how many fields it encrypted. A copy left by a crash is
   deleted at the next start.
4. It rewrites the file, so no plain text is left in free pages or in the write-ahead log. When another
   program keeps the file busy, this step is marked as pending (`protection.compaction` in the status) and
   retried at the next start. Stop the MCP server before the first start to avoid that.

In keystore mode, when the bridge migrates an existing `store/api.token` into the keystore, it overwrites
and deletes the old file.
SSDs and copy-on-write file systems can keep old blocks; full disk encryption covers that.

## Linux without a keystore

`secret-tool` comes with the `libsecret-tools` package (Debian and Ubuntu) or `libsecret` (Fedora, Arch),
and needs a running Secret Service such as GNOME Keyring or KeePassXC in your desktop session. On a server
or under systemd there is usually none. Then the bridge uses, for a new store:

- the passphrase, when the variable named by `encryption.passphrase_env` (or the file named in that
  variable plus `_FILE`) is set, or
- the key file, when `encryption.key_file` is set.

Without either it refuses to start and says which settings to use. It never falls back to no encryption
by itself. The API token needs the keystore too in mode `keystore`: set `api.token`, or
`api.token_file_fallback` to `true` to keep it in `store/api.token`, or use mode `passphrase` or
`keyfile`, which keep the token in that file.

## Containers

Use mode `keyfile` with a secret that only the container user can read. Docker Compose without swarm
mounts a file secret as it is on the host, so give it to the user the containers run as (65532 in the
images in `deploy/docker`) and mode 0400:

```bash
openssl rand -hex 32 > wa_mcp_key
sudo chown 65532 wa_mcp_key
sudo chmod 400 wa_mcp_key
```

```yaml
services:
  wa-bridge:
    environment:
      WA_MCP_ENCRYPTION_MODE: keyfile
      WA_MCP_KEY_FILE: /run/secrets/wa_mcp_key
    secrets: [wa_mcp_key]
secrets:
  wa_mcp_key:
    file: ./wa_mcp_key
```

The MCP server container needs the same secret and variables. On macOS and Linux the bridge and the
server refuse a key file that the group or others can read (mode 0644 or 0444, for example). Keep the key
outside the store folder and outside backups of it. Whoever has both the key file and the store can read
the messages. A passphrase works the same way through `WA_MCP_PASSPHRASE_FILE`. To keep the WhatsApp
session in a volume that only the bridge mounts, set `session_file` to an absolute path such as
`/session/session.db`; the MCP server does not need it. [Run on a server](server.md) shows the complete
setup.

On Windows, a key file must allow your user and SYSTEM only. Files in your profile also allow the
Administrators group, so limit the file first:

```powershell
icacls store.key /inheritance:r /remove:g *S-1-5-32-544 *S-1-3-4 *S-1-5-11 *S-1-5-32-545 *S-1-1-0 /grant:r "${env:USERNAME}:R" SYSTEM:R
```

## Moving a store and recovery

- On the same computer, as the same user, you can move the store folder. Point `store_dir` to the new
  place.
- In keystore mode, a copy on another computer cannot open without the original store key. On Windows,
  the DPAPI file also requires the original user profile. The bridge
  then stops with a message that the store was copied. Link the new machine with a new `store_dir`; the
  phone sends the recent history again.
- If you need to move stores between machines on purpose, use mode `keyfile` or `passphrase` from the
  start and copy the key yourself.
- There is no recovery without the key. Windows loses DPAPI keys when an administrator resets your
  password (a change by yourself is fine) or when the profile is deleted. A deleted keychain item is gone
  too. If losing the local history would hurt, use mode `keyfile` and keep a copy of the key offline.
- There is no command that decrypts a store. Mode `off` refuses an encrypted store.

## Windows EFS for the session

`encryption.efs` (on by default) marks the store folder for Windows EFS and encrypts the files in it,
including `session.db`, the keys of the linked device. This is what `cipher /e` does. EFS is tied to your
Windows account like DPAPI. The first use creates a file encryption certificate, and Windows may suggest
backing it up. Windows Home editions, FAT drives and network shares have no EFS; the bridge then logs that
EFS is not available and carries on. Check a file with `cipher /c store\session.db`.

## Check the state

The startup log shows the database path, schema, encryption mode and key source. It also reports EFS.
Schema 5 added `wa_contacts` and `wa_lid_map`. Field encryption covers the copied contact names.
The LID map remains readable. The bridge writes schema 6 to track multipart sends.
Its minimum reader schema is still 5. The Python reader checks `meta.min_reader_schema`
when opening `wa.db`. `bridge_status` and `GET /api/v1/status` report encryption in `protection`.
