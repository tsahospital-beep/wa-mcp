# 14. Agent connector

Use a local HTTP agent to draft a reply and inspect it before enabling sends.

1. Set `WA_MCP_AGENT_API_KEY` to a shared secret in two terminals. Start `uv run python examples/agents/http_agent.py` in the first terminal.
2. Merge the example's `agent` block into the config your bridge and MCP server use. Replace Alice with a test contact. Leave the mode at its default, `draft`. [HTTP config](../../examples/agents/http.json)
3. Run `uv run wa-mcp agent-test` in the second terminal. Expect a text preview and timings. This uses synthetic input and works before linking a number.
4. Restart the bridge with that config and run `uv run wa-mcp agent`. Have the contact send a text. Open `list_agent_drafts` and inspect the reply. An unlisted contact produces no draft.
5. For voice, configure ElevenLabs or a local command and run `uv run wa-mcp agent-test --voice`. Play the returned Ogg file. To send a reviewed draft, enable sending and use `approve_agent_draft`.
6. Run `pause_agent`, send another test message and check `get_agent_status`. Resume with `resume_agent`.

For another API, model preset or Python class, see the [Agent connector guide](../guides/agent-connector.md).
