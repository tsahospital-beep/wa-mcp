# Try WA MCP without a phone

In two minutes you can find an open request and read a voice note in a fictional archive.
You need uv and an MCP client. The demo includes eight chats from the past 30 days.

1. **Connect the demo.** For Claude Code, run the following command in your terminal, then start Claude Code.
   For Claude Desktop, Cursor or VS Code, copy the [config for your client](../../README.md#try-it-without-a-phone).
   Restart Claude Desktop after saving, or enable the server in your editor's MCP settings.

   ```sh
   claude mcp add wa-mcp-demo -- uvx wa-mcp-server --demo
   ```

2. **Ask “What are my tasks today?”** The assistant calls `get_my_day`. Look for Mira Vale's invoice request,
   your promise to call Teo Lark, and a missed call from Nia Reed.

3. **Ask “Search my messages for blue umbrella.”** The assistant calls `list_messages` with
   `{"query":"blue umbrella"}`. The single match is a voice note from Ira Moss in the fair preparations.

4. **Ask “Read me the transcript of that voice note.”** It is already in the search result. Ira says,
   “Meet me by the west gate. I will be holding a blue umbrella so you can spot me.”
   With the default core tools, `get_message_context` also returns the full transcript. With
   `--toolsets all`, try `get_media_text` on Ira's message `west-gate`.

The default archive lives in a temporary folder for the life of the server. Add
`--demo-dir /path/to/demo` to keep the current day's snapshot and its exports across restarts.
The server creates a new dated snapshot when started on another day. Only the demo store uses
unencrypted fictional data. You can also start the installed server with `wa-mcp --demo` or set
`WA_MCP_DEMO=1` in its environment.

For a real inbox, follow [Getting started](../getting-started.md) to link your account.
