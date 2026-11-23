# Connect an MCP client

`wa-mcp` is an MCP server that speaks over standard input and output (stdio). The client starts it as a
child process, so there is no port to open and nothing to keep running besides the bridge. Any MCP client
that can start a stdio server works. This page shows the setup for the common ones.

Replace `/path/to/wa-mcp` with the folder of your clone. On Windows write the path with forward slashes,
for example `C:/Users/you/wa-mcp`, so that you do not have to escape backslashes in JSON.

## The command

```text
uv run --directory /path/to/wa-mcp wa-mcp --config /path/to/wa-mcp/config.json
```

`--directory` makes uv use the project environment in that folder. `--config` points the server at the
same `config.json` as the bridge. The file does not have to exist. Without it the defaults apply, and
`store/` is then expected inside the folder that contains the path you give.

Faster alternative that does not need uv on the client's `PATH`: run the installed script directly.

| System | Command |
| --- | --- |
| macOS and Linux | `/path/to/wa-mcp/.venv/bin/wa-mcp` |
| Windows | `C:/path/to/wa-mcp/.venv/Scripts/wa-mcp.exe` |

Both take the same `--config` and `--log-level` arguments.

## Claude Desktop

Open Settings, then Developer, then Edit Config. The file is `claude_desktop_config.json`:

- Windows: `%APPDATA%\Claude\claude_desktop_config.json`
- macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`

```json
{
  "mcpServers": {
    "wa-mcp": {
      "command": "uv",
      "args": [
        "run", "--directory", "/path/to/wa-mcp",
        "wa-mcp", "--config", "/path/to/wa-mcp/config.json"
      ]
    }
  }
}
```

Quit Claude Desktop completely and start it again.

## Claude Code

```bash
claude mcp add wa-mcp -- uv run --directory /path/to/wa-mcp wa-mcp --config /path/to/wa-mcp/config.json
```

Put `--scope user` before the name to make the server available in every project, for example
`claude mcp add --scope user wa-mcp -- uv run ...`. Use `--scope project` to write a `.mcp.json` file in the
project. Check the result with `claude mcp list`.

## Cursor

Edit `~/.cursor/mcp.json` for all projects, or `.cursor/mcp.json` inside a project. The format is the same
as for Claude Desktop:

```json
{
  "mcpServers": {
    "wa-mcp": {
      "command": "uv",
      "args": [
        "run", "--directory", "/path/to/wa-mcp",
        "wa-mcp", "--config", "/path/to/wa-mcp/config.json"
      ]
    }
  }
}
```

Open Cursor Settings and the MCP section to see the server and switch it on.

## Windsurf

Edit `~/.codeium/windsurf/mcp_config.json`. The format is the same as for Claude Desktop:

```json
{
  "mcpServers": {
    "wa-mcp": {
      "command": "uv",
      "args": [
        "run", "--directory", "/path/to/wa-mcp",
        "wa-mcp", "--config", "/path/to/wa-mcp/config.json"
      ]
    }
  }
}
```

Restart Windsurf or refresh the MCP server list in Cascade.

## VS Code

Create `.vscode/mcp.json` in a workspace, or run the command `MCP: Open User Configuration` for a file that
applies to every workspace. VS Code uses the key `servers` and a `type`:

```json
{
  "servers": {
    "wa-mcp": {
      "type": "stdio",
      "command": "uv",
      "args": [
        "run", "--directory", "/path/to/wa-mcp",
        "wa-mcp", "--config", "/path/to/wa-mcp/config.json"
      ]
    }
  }
}
```

The tools appear in chat in agent mode.

## Other clients

Any client that supports stdio servers needs three things: the transport `stdio`, the command (`uv` or the
path of `wa-mcp`) and the arguments. Environment variables can be added with an `env` object, which most
clients support:

```json
{
  "command": "/path/to/wa-mcp/.venv/bin/wa-mcp",
  "args": ["--log-level", "INFO"],
  "env": { "WA_MCP_CONFIG": "/path/to/wa-mcp/config.json" }
}
```

`WA_MCP_CONFIG` replaces the `--config` argument. `--log-level` sets the log level of the MCP server. The
variable `WA_MCP_LOG_LEVEL` is a setting of the bridge only, not of the server.

If you enabled encryption, the server needs the passphrase variable as well. Setting it in a client config
file stores the passphrase in plain text in that file, so prefer a client feature that reads it from your
system keychain if there is one.

## Check the connection

Ask the assistant to run `bridge_status`. A healthy answer has `"database": true` and, under `bridge`,
`"connected": true` and `"logged_in": true`. `tool_modules_failed` lists optional tool modules that could
not load, with the reason. If the bridge is not running, the tools that only read the
database still answer and the tools that need the bridge report that it is not reachable.

## Troubleshooting

| Symptom | Check |
| --- | --- |
| The client shows no `wa-mcp` tools | Run the command from a terminal. `uv run --directory /path/to/wa-mcp wa-mcp --help` should print the usage text. Fix the path or run `uv sync` in the folder. |
| `uv` is not found | GUI applications often have a shorter `PATH` than your terminal. Use the full path of `uv` as the command (`which uv` on macOS and Linux, `where uv` on Windows) or use the `.venv` script. |
| `no database at ...` | The bridge has not run yet in that folder, or `--config` points to a different folder. Start the bridge once and use the same config path. |
| `bridge not reachable` | Start `wa-bridge`. Check that `api.listen` is the same for both programs. |
| `no API token` | The bridge creates the token on first start and keeps it in the OS keystore of your user (or in `store/api.token`). Start the bridge once, as the same user as the client. |
| Errors in the middle of a session | The server writes logs to standard error. Add `"--log-level", "INFO"` to the arguments and read the client's MCP log. |

## Notes on safety

An assistant that can read your chats can also be shown instructions written by other people. The server
tells clients that message text is untrusted data. Keep sending off unless you need it.

When sending is on, the server asks you to approve each send through the client if the client supports MCP
elicitation. It shows the recipient and the content in that question. Clients without elicitation fall back
to the `confirm=true` argument, which the model sets itself and which is therefore a weaker check. See
[Sending](sending.md) and [Security](../security.md).
