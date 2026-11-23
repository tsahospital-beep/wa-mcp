# Connect an MCP client

`wa-mcp` is an MCP server that speaks over standard input and output (stdio). The client starts it as a
child process, so there is no port to open and nothing to keep running besides the bridge. Any MCP client
that can start a stdio server works. This page shows the setup for the common ones.

## The command

Install uv and give your client the absolute path of the bridge config in place of `CONFIG_FILE`.
The server uses that file to find the message store, encryption settings and bridge API.

```bash
uvx --from wa-mcp-server==0.1.0 --with 'wa-mcp-sdk[encryption,files]' wa-mcp --config "CONFIG_FILE"
```

If you installed the server with pipx, use `wa-mcp --config "CONFIG_FILE"`. For a source checkout
with `uv sync`, use `uv run --directory "REPOSITORY_DIRECTORY" wa-mcp --config "CONFIG_FILE"`.

## Claude Desktop

Download `wa-mcp.mcpb` from the [latest release](https://github.com/tsahospital-beep/wa-mcp/releases/latest)
and open it. Select Install, then choose the bridge's `config.json` in the extension settings.
Claude Desktop needs access to uvx and the bridge's message store. Keep the bridge running for live tools.

For releases without the bundle, including v0.1.0, use manual configuration. Open Settings, then
Developer, then Edit Config to find `claude_desktop_config.json`.

- Windows: `%APPDATA%\Claude\claude_desktop_config.json`
- On macOS, use Finder to open the `~/Library/Application Support/Claude` folder and select the config file.

```json
{
  "mcpServers": {
    "wa-mcp": {
      "command": "uvx",
      "args": [
        "--from", "wa-mcp-server==0.1.0", "--with", "wa-mcp-sdk[encryption,files]",
        "wa-mcp", "--config", "CONFIG_FILE"
      ]
    }
  }
}
```

Quit Claude Desktop completely and start it again.

## Claude Code

```bash
claude mcp add wa-mcp -- uvx --from wa-mcp-server==0.1.0 --with 'wa-mcp-sdk[encryption,files]' wa-mcp --config "CONFIG_FILE"
```

Put `--scope user` before the name to make the server available in every project, for example
`claude mcp add --scope user wa-mcp -- uvx ...`. Use `--scope project` to write a `.mcp.json` file in the
project. Check the result with `claude mcp list`.

## Cursor

Edit `~/.cursor/mcp.json` for all projects, or `.cursor/mcp.json` inside a project. The format is the same
as for Claude Desktop:

```json
{
  "mcpServers": {
    "wa-mcp": {
      "command": "uvx",
      "args": [
        "--from", "wa-mcp-server==0.1.0", "--with", "wa-mcp-sdk[encryption,files]",
        "wa-mcp", "--config", "CONFIG_FILE"
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
      "command": "uvx",
      "args": [
        "--from", "wa-mcp-server==0.1.0", "--with", "wa-mcp-sdk[encryption,files]",
        "wa-mcp", "--config", "CONFIG_FILE"
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
      "command": "uvx",
      "args": [
        "--from", "wa-mcp-server==0.1.0", "--with", "wa-mcp-sdk[encryption,files]",
        "wa-mcp", "--config", "CONFIG_FILE"
      ]
    }
  }
}
```

The tools appear in chat in agent mode.

## Other clients

Any client that supports stdio servers needs three things: the transport `stdio`, the command (`uvx` or the
path of `wa-mcp`) and the arguments. Environment variables can be added with an `env` object, which most
clients support:

```json
{
  "command": "wa-mcp",
  "args": ["--log-level", "INFO"],
  "env": { "WA_MCP_CONFIG": "CONFIG_FILE" }
}
```

`WA_MCP_CONFIG` replaces the `--config` argument. `--log-level` sets the log level of the MCP server. The
variable `WA_MCP_LOG_LEVEL` is a setting of the bridge only, not of the server.

If you enabled encryption, the server needs the passphrase variable as well. Setting it in a client config
file stores the passphrase in plain text in that file, so prefer a client feature that reads it from your
system keychain if there is one.

## Check the connection

Ask the assistant to run `get_bridge_status`. A healthy answer has `"database": true` and, under `bridge`,
`"connected": true` and `"logged_in": true`. `tool_modules_failed` lists optional tool modules that could
not load, with the reason. If the bridge is not running, the tools that only read the
database still answer and the tools that need the bridge report that it is not reachable.

## Troubleshooting

| Symptom | Check |
| --- | --- |
| The client shows no `wa-mcp` tools | Run `uvx --from wa-mcp-server==0.1.0 wa-mcp --help` in a terminal and check the config path in the client. |
| The client cannot find `uvx` | Give the client the absolute path from `command -v uvx` or `where.exe uvx` on Windows. |
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
