"""Tool: bridge and local setup status."""

from typing import Any

from mcp.server.fastmcp import FastMCP

from wa_mcp import transcribe, vision
from wa_mcp.client import BridgeError, load_token
from wa_mcp_server.common import Result, bridge, cfg, hints, load_errors, ok, safe


def register(mcp: FastMCP) -> None:
    @mcp.tool(annotations=hints("Check bridge status", read_only=True))
    @safe
    def bridge_status() -> Result:
        """Check whether one account's bridge is reachable and logged in before requesting history or
        sending. Returns bridge, database, sending_enabled, require_confirm, backend readiness and
        security_events. Report security warnings to the user. Use list_accounts to discover other
        accounts.
        """
        c = cfg()
        info: dict[str, Any] = {
            "database": c.wa_db.is_file(),
            "sending_enabled": c.sending.enabled,
            "require_confirm": c.sending.require_confirm,
            "encryption": c.encryption.enabled,
            "encryption_mode": c.encryption.mode,
            "token_found": bool(load_token(c)),
            "transcription_backend": c.transcription.backend,
        }
        try:
            transcribe.get_backend(c)
            info["transcription_ready"] = True
        except transcribe.BackendUnavailable as exc:
            info["transcription_ready"] = False
            info["transcription_hint"] = str(exc)
        info["ai_provider"] = c.ai.provider
        info["ocr_engine"] = c.vision.ocr
        try:
            vision.get_engine(c)
            info["ocr_ready"] = c.vision.ocr != "none"
        except vision.OcrUnavailable as exc:
            info["ocr_ready"] = False
            info["ocr_hint"] = str(exc)
        try:
            st = bridge().status()
            info["bridge"] = {k: st.get(k) for k in ("connected", "logged_in", "account", "version")}
            info["app_state"] = st.get("app_state") or {}
            info["protection"] = st.get("protection") or {}
            info["security_alert"] = bool(st.get("security_alert"))
            info["security_events"] = list(st.get("security_events") or [])
        except BridgeError as exc:
            info["bridge"] = {"reachable": False, "error": exc.message}
        info["tool_modules_failed"] = dict(load_errors)
        return ok(**info)
