"""Run a signed v1 HTTP agent on loopback."""

import hmac
import json
import os
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from wa_mcp.webhooks import SeenDeliveries, verify


def server(port: int, key: str) -> ThreadingHTTPServer:
    seen = SeenDeliveries()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            size = self.headers.get("Content-Length", "")
            if self.path != "/reply" or not size.isdecimal() or int(size) > 256 * 1024:
                self.send_error(400)
                return
            body = self.rfile.read(int(size))
            if not hmac.compare_digest(
                self.headers.get("Authorization", "").encode(), f"Bearer {key}".encode()
            ):
                self.send_error(401)
                return
            verdict = verify(key.encode(), dict(self.headers.items()), body, time.time(), seen)
            if not verdict.accepted:
                self.send_error(401)
                return
            event = json.loads(body)
            reply = (
                {"skip": True}
                if verdict.duplicate
                else {
                    "reply_text": "Thanks for your message."
                    if event["event"] == "message"
                    else "Could you leave a message?"
                }
            )
            data = json.dumps(reply).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


if __name__ == "__main__":
    with server(9002, os.environ["WA_MCP_AGENT_API_KEY"]) as app:
        app.serve_forever()
