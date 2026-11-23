# Installing WA MCP (instructions for AI agents)

WA MCP is a WhatsApp MCP server. It has two parts: a Python MCP server (`wa-mcp-server` on PyPI) and a Go bridge that links to a WhatsApp account. Start with demo mode: it needs no phone, no bridge and no account, and it runs on fictional data.

## 1. Check prerequisites

- `uv` must be installed. Check with `uv --version`.
- If it is missing, install it:
  - macOS and Linux: `curl -LsSf https://astral.sh/uv/install.sh | sh`
  - Windows (PowerShell): `powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"`
- Python does not need to be installed separately. `uvx` downloads a suitable Python.

## 2. Add the server in demo mode

Add this entry to the MCP settings of the client (for Cline: `cline_mcp_settings.json`):

```json
{
  "mcpServers": {
    "wa-mcp-demo": {
      "command": "uvx",
      "args": ["wa-mcp-server", "--demo"],
      "disabled": false,
      "autoApprove": []
    }
  }
}
```

On Windows, if `uvx` is not found by the client, use the full path from `where uvx` as `command`.

The first start downloads the package and can take up to a minute.

## 3. Verify

1. Call `get_bridge_status`. It returns `"ok": true`. In demo mode the bridge is reported as not reachable, which is expected because the demo does not use it.
2. Call `get_my_day`. It returns open requests, promises that are due and chats waiting for a reply.
3. Call `list_messages` with `{"query": "blue umbrella"}`. It returns one voice note with its transcript.

If all three work, the installation is correct. Tools that need a real WhatsApp account (sending, history sync, media download) answer with a clear demo notice. That is expected.

## 4. Use a real WhatsApp account (optional, only if the user asks)

Real use needs the Go bridge running and linked to the user's phone. This step needs the user: they scan a QR code or enter a pairing code on their phone. Follow `docs/getting-started.md` in this repository, then replace `--demo` with `--config /absolute/path/to/config.json` in the MCP settings.

Important for the user:

- Sending is off by default and stays off unless the user enables it in the config.
- WA MCP uses the unofficial WhatsApp Web protocol. WhatsApp can restrict or ban an account. Tell the user before linking.

## Troubleshooting

- `uvx: command not found`: install uv (step 1) and restart the client so it picks up the new PATH.
- The server times out on first start: the package is still downloading. Wait and retry.
- No tools listed: check that the entry is not `"disabled": true` and that the client was restarted after editing the settings.
