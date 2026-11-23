# 3. Connect clients

Goal: let Claude Desktop, Claude Code, Cursor, VS Code or Windsurf use your WhatsApp through the MCP server,
and check that it works. This takes about ten minutes.

The MCP server speaks over standard input and output (stdio). The client starts it as a child process, so
there is no port to open. Remote access over HTTP is a different tutorial: [Connect remotely](11-connect-remotely.md).

## Prerequisites

- The bridge is built and linked, see [Install](01-install.md) and [First login](02-first-login.md).
- The Python packages are installed with `uv sync`.
- One of the clients above.

## Steps

### 1. Find the two paths

The config uses the folder of your clone and the path of `config.json` in it. The file does not have to
exist yet. Without it all defaults apply.

macOS and Linux, from the repository folder:

```bash
pwd
```

Windows PowerShell, from the repository folder. This prints the path with forward slashes, which saves you
from escaping backslashes in JSON:

```powershell
(Get-Location).Path -replace '\\', '/'
```

You should see a path such as `/home/you/wa-mcp` or `C:/Users/you/wa-mcp`. Below, `/path/to/wa-mcp` stands
for it.

### 2. Keep the bridge running

Start it in its own terminal and leave it open:

```bash
./wa-bridge
```

On Windows use `.\wa-bridge.exe`. Reading works from the database even when the bridge is stopped, but live
messages, history requests, media downloads and sending need the bridge.

### 3. Add the server to your client

#### Claude Desktop, Cursor and Windsurf

These three use the same JSON. Only the file differs:

| Client | File |
| --- | --- |
| Claude Desktop | Open Settings, choose Developer, then Edit Config to open the file. |
| Cursor | `~/.cursor/mcp.json` for all projects, or `.cursor/mcp.json` in one project. |
| Windsurf | `~/.codeium/windsurf/mcp_config.json`. |

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

If the file already has other servers, add only the `"wa-mcp"` entry inside `mcpServers`. Save the file.

#### Claude Code

```bash
claude mcp add wa-mcp -- uv run --directory /path/to/wa-mcp wa-mcp --config /path/to/wa-mcp/config.json
```

Add `--scope user` before the name to make it available in every project, for example
`claude mcp add --scope user wa-mcp -- uv run ...`. Check it with:

```bash
claude mcp list
```

#### VS Code

Create `.vscode/mcp.json` in a workspace, or run the command `MCP: Open User Configuration` to make one for
every workspace. VS Code uses the key `servers` and a `type`:

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

### 4. Restart the client

Quit Claude Desktop completely and start it again. In Cursor open Cursor Settings and the MCP section and
switch the server on. In Windsurf restart or refresh the MCP server list in Cascade. You should see `wa-mcp`
in the client's list of servers, with its tools.

### 5. Check the setup

Ask the assistant: "Run get_bridge_status." You should see an answer with `"database": true` and, under
`bridge`, `"connected": true` and `"logged_in": true`. Other fields tell you whether sending is enabled and
whether a transcription backend and an OCR engine are ready. `tool_modules_failed` lists optional tool
modules that could not load, with the reason.

Then try:

- "List my most recent chats."
- "What did I talk about with Alice last week?"
- "What are my tasks today?"

You should see the assistant call tools such as `list_chats`, `list_messages` and `get_my_day`. Most clients
show each call.

## Troubleshooting

- The client shows no `wa-mcp` tools. Run the command from a terminal:
  `uv run --directory /path/to/wa-mcp wa-mcp --help` must print the usage text. Fix the path or run
  `uv sync` in the folder, then restart the client.
- `uv` is not found. Programs started from an icon often have a shorter `PATH` than your terminal. Put the
  full path of `uv` in `command` (`which uv` on macOS and Linux, `where uv` on Windows), or start the
  installed script directly: `/path/to/wa-mcp/.venv/bin/wa-mcp` on macOS and Linux and
  `C:/path/to/wa-mcp/.venv/Scripts/wa-mcp.exe` on Windows. It takes the same `--config` argument.
- The answer says `no database at ...`. The bridge has not run in that folder yet, or `--config` points to a
  different folder. Start the bridge once and use the same config path for both programs.
- The answer says `bridge not reachable`. Start `wa-bridge`. Check that `api.listen` is the same for both
  programs.
- You turned on encryption with a passphrase. The server needs the passphrase variable as well. Add an `env`
  block with `WA_MCP_PASSPHRASE` to the server entry. The default keystore mode needs nothing. See
  [Encryption and keys](../guides/encryption.md).

## Next steps

- [My day and tasks](04-my-day-and-tasks.md): ask what needs you today.
- [Scheduled and human-like sending](08-scheduled-and-human-like-sending.md): turn sending on, safely.
- [Connect an MCP client](../guides/mcp-clients.md): the same setup as a reference, with more clients.
