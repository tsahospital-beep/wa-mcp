"""Prepare one bounded Ogg/Opus reply in the usual outgoing folder."""

from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
from pathlib import Path
from urllib.parse import quote

from wa_mcp import audio, config
from wa_mcp.agent import MAX_AUDIO, Reply
from wa_mcp.agent.providers import SetupError, key, request, same_origin


def render(cfg: config.Config, reply: Reply, want_voice: bool) -> Path | None:
    a, v = cfg.agent, cfg.agent.voice
    supplied = bool(reply.reply_audio_url or reply.reply_audio_base64)
    if not supplied and not (want_voice or reply.voice_note):
        return None
    if not supplied and v.provider == "none":
        if reply.voice_note:
            raise ValueError("voice requested without audio or TTS")
        return None
    target = audio.outgoing_path("reply.ogg")
    source = target.parent / "input.audio"
    try:
        if reply.reply_audio_url:
            local = a.provider == "http" and same_origin(a.url, reply.reply_audio_url)
            source.write_bytes(request("GET", reply.reply_audio_url, a.timeout_s, MAX_AUDIO, local=local))
        elif reply.reply_audio_base64:
            source.write_bytes(base64.b64decode(reply.reply_audio_base64, validate=True))
        elif v.provider == "command":
            if not v.argv or not any("{file}" in arg for arg in v.argv):
                raise ValueError("agent.voice.argv needs a program and {file}")
            if os.name == "nt" and Path(shutil.which(v.argv[0]) or v.argv[0]).suffix.lower() in (
                ".bat",
                ".cmd",
            ):
                raise ValueError("use a native TTS executable or an explicit interpreter")
            subprocess.run(
                [arg.replace("{file}", str(source)) for arg in v.argv],
                input=reply.reply_text.encode(),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=a.timeout_s,
                check=True,
            )
        else:
            if v.provider == "elevenlabs":
                if not v.voice_id:
                    raise SetupError("set agent.voice.voice_id")
                url = (
                    (v.url or "https://api.elevenlabs.io/v1/text-to-speech").rstrip("/")
                    + "/"
                    + quote(v.voice_id, safe="")
                )
                header, prefix, vendor_url, vendor_env = (
                    "xi-api-key",
                    "",
                    "https://api.elevenlabs.io",
                    "ELEVENLABS_API_KEY",
                )
                payload = {"text": reply.reply_text, "model_id": v.model_id or "eleven_multilingual_v2"}
            else:
                url = v.url or "https://api.openai.com/v1/audio/speech"
                header, prefix, vendor_url, vendor_env = (
                    "Authorization",
                    "Bearer ",
                    "https://api.openai.com",
                    "OPENAI_API_KEY",
                )
                payload = {
                    "input": reply.reply_text,
                    "model": v.model_id or "tts-1",
                    "voice": v.voice_id or "alloy",
                    "response_format": "mp3",
                }
            custom = not same_origin(url, vendor_url)
            env = v.api_key_env or ("WA_MCP_TTS_API_KEY" if custom else vendor_env)
            item = f"agent-voice-{v.provider}" + ("-custom" if custom else "")
            token = key(cfg, env, item, required=not custom or bool(v.api_key_env))
            headers = {header: prefix + token} if token else {}
            source.write_bytes(
                request(
                    "POST",
                    url,
                    a.timeout_s,
                    MAX_AUDIO,
                    headers={**headers, "Content-Type": "application/json"},
                    content=json.dumps(payload).encode(),
                )
            )
        if source.stat().st_size > MAX_AUDIO:
            raise ValueError("voice response exceeds size limit")
        audio.to_voice_note(source, target)
        if target.stat().st_size > MAX_AUDIO:
            raise ValueError("encoded voice note exceeds size limit")
        source.unlink()
        return target
    except BaseException:
        shutil.rmtree(target.parent, ignore_errors=True)
        raise
