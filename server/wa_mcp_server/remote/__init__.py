"""Remote mode: MCP over Streamable HTTP with bearer tokens and OAuth 2.1 (wa-mcp serve --http).

settings  the "server" section of config.json and the HTTPS start guard
store     server.db: static tokens, OAuth clients, requests, codes and tokens, all secrets hashed
oauth     the authorization server provider used by the MCP SDK handlers
consent   the consent page, protected by an admin password or a one-time approval link
guard     Host and Origin allow-lists, rate limits, security headers, access log
http      the ASGI app and the uvicorn runner
cli       wa-mcp serve, token, oauth and health
"""

# First arguments of wa-mcp that start remote mode (wa_mcp_server.app dispatches them to remote.cli).
COMMANDS = ("serve", "token", "oauth", "health")
