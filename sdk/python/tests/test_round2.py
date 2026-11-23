"""Regression tests for the second review round (SDK side)."""

from __future__ import annotations

import gzip
import json
import logging
import os
import sqlite3
import stat
import zipfile
from collections.abc import Iterator
from datetime import datetime
from ipaddress import IPv4Address, IPv6Address
from pathlib import Path

import httpx
import pytest
import synth

from wa_mcp import audio, config, db, export, files, fsutil, links, people, transcribe
from wa_mcp.media import MEDIA_KINDS

PUBLIC = ["93.184.216.34"]


def public(_host: str) -> list[str]:
    return PUBLIC


# ms vs seconds, overflow


def test_to_unix_reads_milliseconds() -> None:
    assert db.to_unix(1_767_000_000_000) == 1_767_000_000
    assert db.to_unix("1767000000000") == 1_767_000_000
    assert db.to_unix(1_767_000_000) == 1_767_000_000
    assert db.to_unix(db.MS_THRESHOLD) == db.MS_THRESHOLD
    with pytest.raises(ValueError, match="cannot read time"):
        db.to_unix("99999-01-01")


def test_iso_local_never_raises() -> None:
    assert db.iso_local(10**20) is None
    assert db.iso_local(synth.T0) == datetime.fromtimestamp(synth.T0).astimezone().isoformat(
        timespec="seconds"
    )


# "~" like the bridge


def test_expand_home_matches_the_bridge(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    assert config.expand_home("~") == str(tmp_path)
    assert config.expand_home("~/media") == os.path.join(str(tmp_path), "media")
    assert config.expand_home("~bob/media") == "~bob/media"
    assert config.expand_home("a/~/b") == "a/~/b"
    (tmp_path / "c.json").write_text('{"sending": {"allowed_media_dirs": ["~/media"]}}', encoding="utf-8")
    assert config.load(tmp_path / "c.json").sending.allowed_media_dirs == (tmp_path / "media",)
    assert config.load(tmp_path / "c.json").sending.allow_confirm_flag is False


# exports


def test_exports_are_private_with_relative_paths(store: db.Store, cfg_path: Path) -> None:
    downloads = cfg_path.parent / "downloads"
    saved = downloads / "chat" / "plan.pdf"
    saved.parent.mkdir(parents=True)
    saved.write_bytes(b"%PDF")
    outside = cfg_path.parent / "elsewhere" / "photo.jpg"
    outside.parent.mkdir()
    outside.write_bytes(b"jpg")
    conn = sqlite3.connect(cfg_path.parent / "store" / "wa.db")
    conn.execute("UPDATE chat_messages SET local_path = ? WHERE msg_id = 'A4'", (str(saved),))
    conn.execute(
        "UPDATE chat_messages SET local_path = ?, kind = 'image' WHERE msg_id = 'A3'", (str(outside),)
    )
    conn.commit()
    conn.close()
    res = export.export_chat(synth.ALICE)
    path = Path(res["path"])
    text = path.read_text(encoding="utf-8")
    assert "saved as chat/plan.pdf" in text and "saved as photo.jpg" in text
    assert str(cfg_path.parent) not in text
    assert path.name.isascii() and path.name.startswith("15550100002_s.whatsapp.net_")
    if os.name != "nt":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_export_batches_and_preserves_previous_file(store: db.Store, tmp_path: Path, monkeypatch) -> None:
    with sqlite3.connect(store.path) as conn:
        conn.executemany(
            "INSERT INTO chat_messages(conv,msg_id,sent_at,body) VALUES(?,?,?,?)",
            [(synth.BOB, f"batch{i}", i + 1, f"line {i}") for i in range(810)],
        )
    batches = []
    load = store._derived_many

    def observe(conn, keys):
        batches.append(len(keys))
        return load(conn, keys)

    monkeypatch.setattr(store, "_derived_many", observe)
    target = tmp_path / "transcript.md"
    result = export.export_chat(synth.BOB, path=target)
    content = target.read_bytes()
    assert len(batches) >= 3 and max(batches) == 400
    assert sum(batches) == result["messages"]
    assert content.index(b"> line 0\n") < content.index(b"> line 809\n")

    def fail(*args):
        raise OSError("disk error")

    monkeypatch.setattr(export, "render_message", fail)
    with pytest.raises(OSError, match="disk error"):
        export.export_chat(synth.BOB, path=target)
    assert target.read_bytes() == content


def test_file_stem_keeps_non_latin_names_apart() -> None:
    a, b = fsutil.file_stem("\u0391\u03bb\u03af\u03ba\u03b7"), fsutil.file_stem("\u0392\u03ad\u03c1\u03b1")
    assert a != b and a.startswith("chat_") and a.isascii()
    assert fsutil.file_stem("Book club").startswith("Book_club_")


# audio default output


def test_audio_default_output_goes_to_outgoing(store: db.Store, cfg_path: Path, tmp_path: Path) -> None:
    if not audio.shutil.which("ffmpeg"):
        pytest.skip("ffmpeg not on PATH")
    from test_transcribe import tone

    (tmp_path / "src").mkdir()
    src = tone(tmp_path / "src" / "say.wav", 0.5)
    ogg = audio.to_voice_note(src)
    outgoing = cfg_path.parent / "exports" / "outgoing"
    assert ogg.parent.parent == outgoing and ogg.read_bytes()[:4] == b"OggS"
    assert sorted(p.name for p in src.parent.iterdir()) == ["say.wav"]


# people: failed reads are not cached


def test_people_failed_read_is_retried(
    store: db.Store, cfg_path: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Names come from wa.db (wa_contacts): a broken session file is not even read.
    (cfg_path.parent / "store" / "session.db").write_bytes(b"this is not a database" * 100)
    people.connect(store=store)
    assert people.name_of(synth.ALICE) == "Alice Example"

    def locked() -> None:
        raise db.StoreError("database is locked")

    monkeypatch.setattr(store, "identities", locked)
    people.connect(store=store)
    with caplog.at_level(logging.WARNING, logger="wa_mcp.people"):
        assert people.name_of(synth.ALICE) == "Alice"
    assert any("wa.db" in r.getMessage() for r in caplog.records)
    monkeypatch.undo()
    assert people.name_of(synth.ALICE) == "Alice Example"


# transcription confidence


def test_confidence_comes_from_the_kept_run(tmp_path: Path, store: db.Store) -> None:
    if not audio.shutil.which("ffmpeg"):
        pytest.skip("ffmpeg not on PATH")
    from test_transcribe import Scripted, tone

    res = transcribe.transcribe_file(Scripted(), tone(tmp_path / "short.wav", 2.0), hint="el")
    assert res.language == "el" and res.text == "part-el" and res.confidence == 0.99
    plain = transcribe.transcribe_file(Scripted(), tone(tmp_path / "plain.wav", 2.0))
    assert plain.confidence == 0.4


# kinds


def test_kinds_list_covers_bridge_kinds(store: db.Store) -> None:
    for kind in ("location", "contact", "poll", "unsupported", "ptv"):
        assert kind in db.KINDS
    assert set(MEDIA_KINDS) <= set(db.KINDS)
    assert db.messages(kind=["location", "ptv"]) == []


# files: DTD in any encoding, header read


def _docx(path: Path, xml: bytes) -> Path:
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("word/document.xml", xml)
    return path


@pytest.mark.parametrize("encoding", ["utf-16", "utf-16-le", "utf-16-be", "utf-8"])
def test_dtd_refused_in_any_encoding(tmp_path: Path, encoding: str) -> None:
    xml = '<?xml version="1.0"?><!DOCTYPE d [<!ENTITY x "y">]><d>&x;</d>'.encode(encoding)
    with pytest.raises(files.FileError, match="DTD"):
        files.extract_text(_docx(tmp_path / "e.docx", xml))


def test_extract_reads_only_the_header(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    p = tmp_path / "notes.txt"
    p.write_text("short note", encoding="utf-8")
    calls: list[int] = []
    real = Path.read_bytes

    def counting(self: Path) -> bytes:
        calls.append(1)
        return real(self)

    monkeypatch.setattr(Path, "read_bytes", counting)
    assert files.extract_text(p).text == "short note"
    assert len(calls) == 1  # the text itself, not a second full read for the type check


# links


@pytest.mark.parametrize(
    "host",
    [
        "[::ffff:10.0.0.1]",
        "[::ffff:127.0.0.1]",
        "[64:ff9b::a00:1]",
        "[64:ff9b:1::1]",
        "[::10.0.0.1]",
        "[2002:a00:1::1]",
        "[2001:0:4136:e378:8000:63bf:3fff:fdd2]",
    ],
)
def test_embedded_private_addresses_refused(host: str) -> None:
    with pytest.raises(links.LinkError):
        links.check_url(f"https://{host}/", resolve=public)


def test_public_mapped_address_allowed() -> None:
    _, address = links.check_url("https://[::ffff:8.8.8.8]/", resolve=public)
    assert IPv6Address(address).ipv4_mapped == IPv4Address("8.8.8.8")


def test_export_redirect_to_googleusercontent_only(store: db.Store) -> None:
    seen: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req.headers["host"])
        if req.headers["host"] == "docs.google.com":
            return httpx.Response(
                307, headers={"location": "https://doc-0s-docs.googleusercontent.com/export/x"}
            )
        return httpx.Response(200, text="Exported text", headers={"content-type": "text/plain"})

    link = links.find_doc_links("https://docs.google.com/document/d/AbCdEf123_-x/edit")[0]
    assert links.fetch_doc(link, transport=httpx.MockTransport(handler), resolve=public) == "Exported text"
    assert seen == ["docs.google.com", "doc-0s-docs.googleusercontent.com"]
    with pytest.raises(links.LinkError, match="not allowed"):
        links.guarded_get(
            "https://doc-0s-docs.googleusercontent.com/export/x",
            allowed_hosts=links.DOC_HOSTS,
            redirect_suffixes=links.DOC_REDIRECT_SUFFIXES,
            transport=httpx.MockTransport(handler),
            resolve=public,
        )

    def other(req: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "https://googleusercontent.com.evil.example/x"})

    with pytest.raises(links.LinkError, match="not allowed"):
        links.fetch_doc(link, transport=httpx.MockTransport(other), resolve=public)


class _Stream(httpx.SyncByteStream):
    def __init__(self, data: bytes) -> None:
        self.data = data

    def __iter__(self) -> Iterator[bytes]:
        for i in range(0, len(self.data), 512):
            yield self.data[i : i + 512]


def test_gzip_body_is_capped_after_decompression() -> None:
    bomb = gzip.compress(b"\0" * 5_000_000)

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-encoding": "gzip"}, stream=_Stream(bomb))

    with pytest.raises(links.LinkError, match="larger than 100000 bytes"):
        links.guarded_get(
            "https://example.org/", max_bytes=100_000, transport=httpx.MockTransport(handler), resolve=public
        )

    small = gzip.compress(b"hello there")

    def ok(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-encoding": "gzip"}, stream=_Stream(small))

    got = links.guarded_get("https://example.org/", transport=httpx.MockTransport(ok), resolve=public)
    assert got.body == b"hello there"


def test_check_chat_links(store: db.Store) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        assert req.method == "HEAD"
        return httpx.Response(200)

    res = links.check_chat_links(synth.GROUP, transport=httpx.MockTransport(handler), resolve=public)
    assert res["count"] == 1 and res["links"][0]["ok"] is True and res["links"][0]["msg_id"] == "G1"


def test_analytics_reads_history_sync_state(store: db.Store) -> None:
    from test_review_fixes import FakeClock, SilentBridge

    from wa_mcp import history
    from wa_mcp.insight import analytics

    clock = FakeClock()
    history.sync_all_history(
        SilentBridge(),  # type: ignore[arg-type]
        "2020-01-01",
        "2020-01-01",
        wait_seconds=1,
        sleep=clock.sleep,
        clock=clock,
    )
    analytics.clear_memory_cache()
    result = analytics.analyze("Alice", refresh=True)
    assert '"state": "exhausted"' in json.dumps(result)


# Unicode search shared with the bridge


def test_fold_vectors_shared_with_go(store: db.Store) -> None:
    vectors = synth.REPO / "testdata" / "fold_vectors.json"
    if not vectors.is_file():
        pytest.skip("testdata/fold_vectors.json not present")
    data = json.loads(vectors.read_text(encoding="utf-8"))
    for case in data["fold"]:
        assert case["input"].casefold() == case["folded"], case
    for case in data["search"]:
        assert bool(store._match(case["text"], case["query"].casefold())) is case["match"], case


# hinted transcripts do not feed later hints


def test_hinted_transcripts_do_not_feed_hints(tmp_path: Path, store: db.Store, cfg_path: Path) -> None:
    if not audio.shutil.which("ffmpeg"):
        pytest.skip("ffmpeg not on PATH")
    from test_transcribe import Scripted, tone

    for i in range(3):
        db.put_derived(synth.ALICE, f"H{i}", "transcript", "hinted", lang="el", confidence=0.99, hinted=True)
    assert transcribe.chat_language([synth.ALICE, synth.ALICE_LID]) is None
    row = next(r for r in db.derived_by_source("transcript", conv=synth.ALICE) if r["msg_id"] == "H0")
    assert row["lang"] == "el" and row["lang_hinted"] is True
    conn = sqlite3.connect(cfg_path.parent / "store" / "wa.db")
    assert (
        conn.execute("SELECT lang FROM derived_text WHERE msg_id = 'H0'").fetchone()[0] == "el" + db.HINT_MARK
    )
    conn.close()

    for i in range(3):
        db.put_derived(synth.ALICE, f"D{i}", "transcript", "detected", lang="el", confidence=0.95)
    assert transcribe.chat_language([synth.ALICE, synth.ALICE_LID]) == "el"

    store.cfg.downloads_dir.mkdir(parents=True, exist_ok=True)
    clip = tone(store.cfg.downloads_dir / "short.wav", 2.0)

    class Media:
        def download_media(self, chat: str, msg_id: str) -> dict[str, object]:
            return {"path": str(clip)}

    out = transcribe.transcribe_message(Media(), synth.ALICE, "A3", force=True, backend=Scripted())  # type: ignore[arg-type]
    assert out["lang"] == "el"
    stored = next(r for r in db.derived(synth.ALICE, "A3") if r["source"] == "transcript")
    assert stored["lang"] == "el" and stored["lang_hinted"] is True
    assert stored["confidence"] == 0.99


def test_private_write_replaces_link_and_preserves_previous_on_failure(tmp_path: Path) -> None:
    tmp_path.chmod(0o750)
    original_mode = tmp_path.stat().st_mode
    original = tmp_path / "original"
    target = tmp_path / "report"
    original.write_text("original")
    target.hardlink_to(original)
    fsutil.write_private(target, "new report")
    assert tmp_path.stat().st_mode == original_mode
    assert original.read_text() == "original"
    with pytest.raises(UnicodeEncodeError):
        fsutil.write_private(target, "\ud800")
    assert target.read_text() == "new report"
    assert set(tmp_path.iterdir()) == {target, original}


def test_truncated_compressed_response_is_refused() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, headers={"content-encoding": "gzip"}, stream=_Stream(gzip.compress(b"ok")[:-1])
        )

    with pytest.raises(links.LinkError, match="incomplete"):
        links.guarded_get("https://example.org/", transport=httpx.MockTransport(handler), resolve=public)


def test_compressed_wire_bytes_have_a_limit() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        data = gzip.compress(b"", mtime=0) * 3400
        return httpx.Response(200, headers={"content-encoding": "gzip"}, stream=_Stream(data))

    with pytest.raises(links.LinkError, match="wire limit"):
        links.guarded_get(
            "https://example.org/", max_bytes=8, transport=httpx.MockTransport(handler), resolve=public
        )


def test_all_gzip_members_share_one_limit() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        data = gzip.compress(b"one") + gzip.compress(b"two")
        return httpx.Response(200, headers={"content-encoding": "gzip"}, stream=_Stream(data))

    transport = httpx.MockTransport(handler)
    assert (
        links.guarded_get("https://example.org/", max_bytes=6, transport=transport, resolve=public).body
        == b"onetwo"
    )
    with pytest.raises(links.LinkError, match="larger"):
        links.guarded_get("https://example.org/", max_bytes=5, transport=transport, resolve=public)
