# Several accounts

One MCP server can work with more than one WhatsApp number, for example a personal and a work number. Each
number runs its own bridge with its own store folder and API address. The MCP server talks to all of them,
and every tool takes an optional `account` argument.

Without an `accounts` block nothing changes: there is one account, `default`, that uses the top-level
settings and the existing `store` folder. You do not need to link your phone again.

## Add a second number

Add an `accounts` block to `config.json`. The account `default` always exists and keeps the top-level
settings, so you only describe the new number:

```json
{
  "sending": { "enabled": true },
  "accounts": {
    "work": {
      "api": { "listen": "127.0.0.1:8081" },
      "sending": { "limits": { "per_hour": 20, "per_day": 100 } }
    }
  }
}
```

An account other than `default` gets:

| Setting | Value |
| --- | --- |
| `store_dir`, `downloads_dir`, `exports_dir` | A subfolder named after the account, for example `store/work`, unless the block sets the key. |
| `api.listen` | Required. Every account needs its own port. |
| `login.port` | The top-level value unless the block sets it. Ports other than `0` must differ. |
| `sending` | The top-level `sending` with the keys of the block merged over it. |
| Everything else | The top-level value. Webhooks, encryption, receipts, transcription and the insight sections are shared. |

Account names use `a` to `z`, `0` to `9`, `-` and `_`, at most 32 characters. The bridge and the MCP server
refuse a config in which two accounts share a store folder, an API port or a login port. All keys are listed
in [Configuration](configuration.md#accounts).

Sending limits, quiet hours and duplicate checks are counted per number, because every bridge keeps its own
store. A limit of 20 messages per hour for `work` does not use up the limit of `default`.

## Run the bridges

Start one bridge per account:

```bash
./wa-bridge                      # the account "default"
./wa-bridge --account work       # the account "work", in a second terminal
```

Or let one command run all of them:

```bash
./wa-bridge run-all
```

`run-all` checks every account first and stops with a message when a block is wrong. Then it starts one
bridge process per account and prefixes their output with the account name, for example `[work]`. When a
bridge stops, it is started again after 1 second, then 2, 4 and so on up to 60 seconds; after a bridge has
run for a minute the pause starts at 1 second again. Ctrl+C or SIGTERM is passed on to every bridge, which
then shuts down normally. A bridge that is still running after 30 seconds is stopped. `run-all` only signals
and stops the processes it started, by their process IDs.

`wa-bridge accounts` shows every account, its store folder, its API address and whether it runs, is
connected and is linked:

```text
ACCOUNT  STORE       API             STATUS
default  store       127.0.0.1:8080  running, connected, linked, +1 ••• ••• 0101
work     store/work  127.0.0.1:8081  not running, not linked
```

It reads the status from each bridge through its API. For a bridge that does not run it opens the session
file read-only and creates nothing.

`wa-bridge login-url --account work` prints a fresh login link for one account.

## Link each number

A new account shows its own login window the first time its bridge starts. With several accounts the
subtitle names the account, for example "Link a device: work". Click the account name to see the other
accounts and open the login window of another one. The window asks that account's bridge, through its local
API and its own token, for a one-time link and opens it. Nothing else of the other account is read or shown.
The menu says "Already linked" when that number is linked, and "Bridge not running" when its bridge does not
answer.

To look at the window without WhatsApp:

```bash
./wa-bridge --login-preview --account work
```

## Use the accounts from an assistant

The MCP server reads the same `config.json` and needs no extra setup.

- `list_accounts` lists the accounts with `linked`, `bridge_running`, `connected`, the masked number and the
  store folder.
- Every tool takes `account`. Without it a tool works on `default`, for example
  `list_messages(query="invoice", account="work")`.
- `my_day` without `account` covers all accounts. Every group and item then has an `account` field, and
  accounts without a database yet are listed under `skipped`.
- Send tools use the sending settings of the chosen account, and the approval prompt names the account the
  message goes out from.
- The task tracker, analytics cache and sync progress (`insight.db`) are kept per account in its store folder.

With one account the tools return exactly what they returned before, and the server instructions do not
mention accounts.

## Use the accounts from Python

```python
from wa_mcp import accounts, config, db

accounts.connect(config.load("config.json"))
for name in accounts.names():
    with accounts.use(name) as h:
        print(name, h.cfg.store_dir, len(db.messages(query="invoice", limit=5)))
```

`config.load(path, account="work")` and `Config.for_account("work")` return the settings of one account.
`accounts.handles(name)` returns its config, database, name lookups and bridge client, and
`db.use_account(name)` makes the module functions of `db` and `people` work on that account in the current
thread or task.

## Limits

- Environment variables such as `WA_MCP_STORE_DIR` and `WA_MCP_API_LISTEN` change the top-level settings.
  Folders derived from them move with them, and a value in an account block wins.
- `login.public` (the login page behind a proxy, see [Run on a server](server.md)) is one address for the
  whole config, so it suits a single account.
