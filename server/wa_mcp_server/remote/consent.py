"""Consent pages: the owner approves each OAuth client before it gets a code.

Approval is either the admin password typed on the consent page (server.oauth.approval = "password")
or a one-time approval link that the server prints to its log and that `wa-mcp oauth pending` prints
again ("link"). The browser window that started the flow is bound to the request by a cookie and is the
only one that receives the code: the waiting page reloads itself until the owner decides, from any device.
"""

from __future__ import annotations

import hmac
import html
import logging
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from importlib import resources
from string import Template
from urllib.parse import urlsplit

from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response
from starlette.routing import Route

from wa_mcp_server.common import blocking, clean_label
from wa_mcp_server.remote import store as store_mod
from wa_mcp_server.remote.guard import CLIENT_IP_KEY
from wa_mcp_server.remote.oauth import Provider
from wa_mcp_server.remote.settings import secret_env
from wa_mcp_server.remote.store import RequestRecord

log = logging.getLogger("wa_mcp_server.remote")

COOKIE = "wa_mcp_consent"
# All addresses together: this many wrong passwords within LOCK_SECONDS lock password approval.
LOCK_FAILURES = 10
LOCK_SECONDS = 900
# One address: from the third wrong password on, wait BACKOFF_SECONDS, doubling each time, up to LOCK_SECONDS.
FREE_FAILURES = 2
BACKOFF_SECONDS = 30
# Monotonic clock of the lockouts (tests replace it).
CLOCK: Callable[[], float] = time.monotonic
_TEMPLATE = Template(resources.files("wa_mcp_server.remote").joinpath("web/consent.html").read_text("utf-8"))

_CHECK = (
    '<svg class="check" viewBox="0 0 76 76" aria-hidden="true"><circle cx="38" cy="38" r="38"/>'
    '<path d="M23.5 39.5l9.5 9.5 19.5-20"/></svg>'
)
_WARN = (
    '<svg class="check warn" viewBox="0 0 76 76" aria-hidden="true"><circle cx="38" cy="38" r="38"/>'
    '<path d="M38 24v18M38 51.5v.5"/></svg>'
)


def _e(text: object) -> str:
    return html.escape(str(text), quote=True)


def _age(seconds: int) -> str:
    if seconds < 90:
        return "just now"
    if seconds < 5400:
        return f"{round(seconds / 60)} minutes ago"
    if seconds < 129600:
        return f"{round(seconds / 3600)} hours ago"
    return f"{round(seconds / 86400)} days ago"


@dataclass
class _Strikes:
    count: int = 0
    until: float = 0.0


def _form_source(uri: str) -> str:
    """CSP form-action source for the client redirect that follows a form post."""
    u = urlsplit(uri)
    if u.scheme in ("http", "https"):
        return f"{u.scheme}://{u.netloc}"
    return f"{u.scheme}:"


class Consent:
    def __init__(self, provider: Provider, clock: Callable[[], float] | None = None) -> None:
        self.provider = provider
        self.store = provider.store
        self.settings = provider.settings
        self.secure = self.settings.base_url.startswith("https://")
        self.clock = clock
        self._failures: deque[float] = deque()
        self._strikes: dict[str, _Strikes] = {}

    def routes(self) -> list[Route]:
        return [
            Route("/oauth/consent", self.consent, methods=["GET", "POST"]),
            Route("/oauth/approve", self.approve, methods=["GET", "POST"]),
        ]

    # rendering

    def page(
        self, title: str, content: str, status: int = 200, refresh: int = 0, redirect: str = ""
    ) -> Response:
        form_action = "'self'" + (f" {_form_source(redirect)}" if redirect else "")
        body = _TEMPLATE.substitute(
            title=_e(title),
            content=content,
            refresh=f'<meta http-equiv="refresh" content="{refresh}">\n' if refresh else "",
        )
        return HTMLResponse(
            body,
            status_code=status,
            headers={
                "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; img-src data:; "
                f"form-action {form_action}; frame-ancestors 'none'; base-uri 'none'",
                "Cache-Control": "no-store",
                "X-Frame-Options": "DENY",
            },
        )

    def message(self, title: str, text: str, status: int, ok: bool = False) -> Response:
        return self.page(
            title,
            f'<div class="done">{_CHECK if ok else _WARN}<h2>{_e(title)}</h2><p>{_e(text)}</p></div>',
            status,
        )

    async def _app_box(self, rec: RequestRecord) -> str:
        client = await blocking(self.store.get_client, rec.client_id)
        info = client.info if client else {}
        name = clean_label(str(info.get("client_name") or "Unnamed app"), 60)
        redirect = urlsplit(rec.params["redirect_uri"])
        target = redirect.netloc or redirect.scheme + ":"
        if client is None:
            registered = "unknown"
        else:
            registered = _age(store_mod.now() - client.created_at)
            registered += f" from {client.registered_ip}" if client.registered_ip else ""
            if client.approved_at is None:
                registered += ", never approved before"
        return (
            f'<div class="app"><div class="name">{_e(name)}</div><dl>'
            f"<dt>Returns to</dt><dd>{_e(target)}</dd>"
            f"<dt>Registered</dt><dd>{_e(registered)}</dd>"
            f"<dt>Access</dt><dd>{_e(' '.join(rec.params['scopes']))}</dd>"
            f'<dt>Request</dt><dd><span class="req">{_e(rec.code)}</span></dd></dl></div>'
        )

    @staticmethod
    def _intro() -> str:
        return (
            "<h2>Allow this app to use your WhatsApp?</h2>"
            "<p>The app gets the same access as the MCP tools: it can read your chats and messages, and send "
            "messages when sending is enabled in config.json. Allow only apps you started yourself.</p>"
        )

    # helpers

    def _csrf(self, rec: RequestRecord, cookie: str) -> str:
        return self.store.hash(f"csrf:{rec.id}:{cookie}")

    def _owns(self, rec: RequestRecord, cookie: str | None) -> bool:
        return (
            bool(cookie)
            and rec.browser_hash is not None
            and hmac.compare_digest(rec.browser_hash, self.store.hash(f"browser:{cookie}"))
        )

    def _redirect(self, url: str) -> Response:
        return RedirectResponse(url, status_code=302, headers={"Cache-Control": "no-store"})

    async def _finish(self, rec: RequestRecord, status: str) -> Response | None:
        """Deliver an approved or denied request to the browser that owns it (once)."""
        if not await blocking(self.store.move_request, rec.id, status, "done"):
            return None
        if status == "approved":
            return self._redirect(await blocking(self.provider.issue_code, rec))
        return self._redirect(self.provider.denied_redirect(rec))

    def _wait(self, ip: str) -> int:
        """Seconds until a password attempt is allowed again from ip (0 = now)."""
        t = (self.clock or CLOCK)()
        while self._failures and self._failures[0] < t - LOCK_SECONDS:
            self._failures.popleft()
        if len(self._failures) >= LOCK_FAILURES:
            return int(self._failures[0] + LOCK_SECONDS - t) + 1
        strikes = self._strikes.get(ip)
        return int(strikes.until - t) + 1 if strikes and strikes.until > t else 0

    def _password_ok(self, given: str, ip: str) -> bool:
        expected = secret_env(self.settings.oauth.admin_password_env)
        # Compare keyed hashes, so the time taken does not depend on the length of the password.
        ok = bool(expected) and hmac.compare_digest(
            self.store.hash("admin:" + given), self.store.hash("admin:" + expected)
        )
        if ok:
            self._strikes.pop(ip, None)
            return True
        t = (self.clock or CLOCK)()
        self._failures.append(t)
        strikes = self._strikes.setdefault(ip, _Strikes())
        strikes.count += 1
        if strikes.count > FREE_FAILURES:
            strikes.until = t + min(LOCK_SECONDS, BACKOFF_SECONDS * 2 ** (strikes.count - FREE_FAILURES - 1))
        if len(self._strikes) > 10000:
            self._strikes = {k: v for k, v in self._strikes.items() if v.until > t}
        log.warning("oauth: wrong admin password on the consent page from %s", ip)
        return False

    # /oauth/consent

    async def consent(self, request: Request) -> Response:
        if request.method == "POST":
            form = await request.form()
            rid = str(form.get("request") or "")
        else:
            form = None
            rid = request.query_params.get("request", "")
        rec = await blocking(self.store.get_request, rid) if rid else None
        if rec is None or rec.expires_at < store_mod.now():
            return self.message("Request expired", "Start the connection again in your app.", 404)
        cookie = request.cookies.get(COOKIE)
        new_cookie = ""
        if rec.browser_hash is None and request.method == "GET":
            new_cookie = store_mod.new_secret()
            if await blocking(self.store.bind_browser, rec.id, self.store.hash(f"browser:{new_cookie}")):
                cookie = new_cookie
                rec = await blocking(self.store.get_request, rid) or rec
        if not self._owns(rec, cookie):
            return self.message(
                "Open in another window", "This request belongs to the browser window that started it.", 403
            )
        assert cookie is not None  # noqa: S101 (checked by _owns)
        if rec.status in ("approved", "denied"):
            done = await self._finish(rec, rec.status)
            if done is not None:
                return done
        if rec.status != "pending":
            return self.message("Already finished", "Return to your app.", 409)

        error = ""
        if form is not None:
            csrf = form.get("csrf")
            if (
                not isinstance(csrf, str)
                or not csrf.isascii()
                or not hmac.compare_digest(csrf, self._csrf(rec, cookie))
            ):
                return self.message("Form expired", "Reload the page and try again.", 400)
            if form.get("action") == "deny":
                await blocking(self.store.move_request, rec.id, "pending", "denied")
                return await self._finish(rec, "denied") or self.message("Denied", "Return to your app.", 200)
            if self.settings.oauth.approval != "password":
                return self.message("Approval link needed", "Use the approval link from the server log.", 400)
            ip = str(request.scope.get(CLIENT_IP_KEY) or "unknown")
            wait = self._wait(ip)
            if wait:
                return self.message(
                    "Too many attempts", f"Wrong password too often. Try again in {wait} seconds.", 429
                )
            if self._password_ok(str(form.get("password") or ""), ip):
                if not await blocking(
                    self.store.decide_request, rec, "approved", self.settings.oauth.max_clients
                ):
                    return self.message(
                        "Approval unavailable", "The request changed or the client limit was reached.", 409
                    )
                done = await self._finish(rec, "approved")
                if done is not None:
                    return done
                return self.message("Already finished", "Return to your app.", 409)
            error = '<p class="ferr" role="alert">Wrong password.</p>'

        response = await self._consent_page(rec, cookie, error)
        if new_cookie:
            response.set_cookie(
                COOKIE,
                new_cookie,
                max_age=900,
                path="/oauth/",
                secure=self.secure,
                httponly=True,
                samesite="lax",
            )
        return response

    async def _consent_page(self, rec: RequestRecord, cookie: str, error: str) -> Response:
        box = await self._app_box(rec)
        csrf = _e(self._csrf(rec, cookie))
        hidden = f'<input type="hidden" name="request" value="{_e(rec.id)}"><input type="hidden" name="csrf" value="{csrf}">'
        deny = '<button class="btn ghost" name="action" value="deny">Deny</button>'
        if self.settings.oauth.approval == "password":
            content = (
                f"{self._intro()}{box}"
                f'<form method="post" action="/oauth/consent" autocomplete="off">{hidden}'
                '<label class="lbl2" for="pw">Admin password</label>'
                '<div class="field"><input id="pw" type="password" name="password" autocomplete="current-password" '
                f'required autofocus></div>{error}<div class="row">{deny}'
                '<button class="btn" name="action" value="approve">Allow</button></div></form>'
            )
            return self.page(
                "Allow access", content, status=401 if error else 200, redirect=rec.params["redirect_uri"]
            )
        content = (
            f"{self._intro()}{box}"
            '<div class="wait"><span class="spin" aria-hidden="true"></span><span>Waiting for the owner. Open the '
            f"approval link for request <b>{_e(rec.code)}</b> from the server log, or run "
            "<code>wa-mcp oauth pending</code>. This page continues by itself.</span></div>"
            f'<form method="post" action="/oauth/consent">{hidden}<div class="row">{deny}</div></form>'
        )
        return self.page("Waiting for approval", content, refresh=5, redirect=rec.params["redirect_uri"])

    # /oauth/approve?ticket=...

    async def approve(self, request: Request) -> Response:
        if self.settings.oauth.approval != "link":
            return self.message("Approval link disabled", "Use the password on the consent page.", 403)
        if request.method == "POST":
            form = await request.form()
            ticket = str(form.get("ticket") or "")
            action = str(form.get("action") or "")
        else:
            ticket, action = request.query_params.get("ticket", ""), ""
        rec = await blocking(self.store.request_by_ticket, self.store.hash(ticket)) if ticket else None
        if rec is None or rec.status != "pending" or rec.expires_at < store_mod.now():
            return self.message(
                "Link not valid",
                "This approval link was used or has expired. Run wa-mcp oauth pending for a new one.",
                404,
            )
        if request.method == "GET":
            box = await self._app_box(rec)
            content = (
                f"{self._intro()}{box}"
                f'<form method="post" action="/oauth/approve"><input type="hidden" name="ticket" value="{_e(ticket)}">'
                '<div class="row"><button class="btn ghost" name="action" value="deny">Deny</button>'
                '<button class="btn" name="action" value="approve">Allow</button></div></form>'
            )
            return self.page("Approve access", content, redirect=rec.params["redirect_uri"])
        status = "approved" if action == "approve" else "denied"
        if not await blocking(
            self.store.decide_request, rec, status, self.settings.oauth.max_clients, self.store.hash(ticket)
        ):
            return self.message(
                "Approval unavailable", "The request changed or the client limit was reached.", 409
            )
        log.warning("oauth: request %s %s by the approval link", rec.code, status)
        # The code goes to the window that started the flow (often a popup of the client), on its next reload.
        if status == "approved":
            return self.message(
                "Approved",
                "Return to the window where you started the connection. It continues by itself.",
                200,
                True,
            )
        return self.message("Denied", "The app does not get access.", 200)
