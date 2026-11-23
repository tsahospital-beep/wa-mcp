"""Text from photos and screenshots: OCR engines, storage in derived_text, search, limits and descriptions."""

from __future__ import annotations

import dataclasses
import json
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest
import synth
from vision_data import PNG, SHOT_TEXT, fake_tesseract, on_path

from wa_mcp import ai, config, db, vision
from wa_mcp.ai.openai_compat import OpenAICompatible
from wa_mcp.client import BridgeError


class FakeEngine(vision.Engine):
    name = "fake"

    def __init__(self, texts: dict[str, str] | None = None, default: str = SHOT_TEXT) -> None:
        self.texts = texts or {}
        self.default = default
        self.read_files: list[str] = []

    def check(self) -> None:
        return None

    def read(self, path: Path) -> vision.OcrResult:
        self.read_files.append(path.stem)
        return vision.OcrResult(self.texts.get(path.stem, self.default), "eng")


class FakeBridge:
    """Writes a file for every download, like the real bridge does."""

    def __init__(self, root: Path, fail: BridgeError | None = None) -> None:
        self.root = root
        self.fail = fail
        self.downloads: list[tuple[str, str]] = []

    def download_media(self, conv: str, msg_id: str) -> dict[str, Any]:
        self.downloads.append((conv, msg_id))
        if self.fail is not None:
            raise self.fail
        path = self.root / "downloads" / f"{msg_id}.png"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(PNG)
        return {"path": str(path)}


def add_images(
    root: Path, ids: list[str], saved: bool = True, conv: str = synth.ALICE, kind: str = "image"
) -> None:
    conn = sqlite3.connect(root / "store" / "wa.db")
    for i, msg_id in enumerate(ids):
        path = root / "downloads" / f"{msg_id}.png"
        path.parent.mkdir(parents=True, exist_ok=True)
        if saved:
            path.write_bytes(PNG)
        conn.execute(
            "INSERT INTO chat_messages (conv, msg_id, author, sent_at, body, kind, mime, local_path) "
            "VALUES (?, ?, ?, ?, '', ?, 'image/png', ?)",
            (conv, msg_id, conv, synth.T0 + 1000 + i * 10, kind, str(path) if saved else None),
        )
    conn.commit()
    conn.close()


@pytest.fixture
def root(cfg_path: Path) -> Path:
    return cfg_path.parent


def derived_of(msg_id: str) -> dict[str, str]:
    return {str(r["source"]): str(r["content"]) for r in db.derived(synth.ALICE, msg_id)}


# text cleaning


def test_clean_text() -> None:
    assert (
        vision.clean_text("Invoice 4711\r\n\r\n\r\n\r\nDue   Friday  \f", 3) == "Invoice 4711\n\nDue   Friday"
    )
    assert vision.clean_text(" . - \n ~ ", 3) == ""
    assert vision.clean_text("ab", 3) == "" and vision.clean_text("ab", 2) == "ab"
    assert vision.clean_text("Grüße aus Köln", 3) == "Grüße aus Köln"
    assert len(vision.clean_text("word " * 10000, 3)) == vision.MAX_TEXT_CHARS


# tesseract


def test_tesseract_runs_the_program_without_a_shell(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    folder = tmp_path / "bin"
    fake_tesseract(folder, out="Café menu 4,50\n")
    on_path(monkeypatch, folder)
    image = tmp_path / "my pic; echo hi.png"
    image.write_bytes(PNG)
    engine = vision.Tesseract("", ("eng", "deu"), 30.0)
    engine.check()
    res = engine.read(image)
    assert res.text == "Café menu 4,50\n" and res.lang == "eng+deu"
    argv = json.loads((folder / "argv.json").read_text(encoding="utf-8"))
    assert argv == [str(image.resolve()), "stdout", "-l", "eng+deu"]


def test_tesseract_binary_setting(tmp_path: Path) -> None:
    wrapper = fake_tesseract(tmp_path / "elsewhere")
    engine = vision.Tesseract(str(wrapper), ("eng",), 30.0)
    engine.check()
    assert engine.read(tmp_path / "x.png").text.startswith("Invoice")


def test_tesseract_errors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    engine = vision.Tesseract("", ("eng",), 30.0)
    with pytest.raises(vision.OcrUnavailable, match=r"tesseract was not found.*vision.tesseract_binary"):
        engine.check()
    bad = fake_tesseract(tmp_path / "bad", code=1, err="Failed loading language 'xyz'\nCould not initialize")
    with pytest.raises(vision.OcrError, match=r"exited with 1.*Failed loading language 'xyz'"):
        vision.Tesseract(str(bad), ("xyz",), 30.0).read(tmp_path / "x.png")

    def slow(*args: Any, **kwargs: Any) -> Any:
        raise subprocess.TimeoutExpired("tesseract", 1)

    good = fake_tesseract(tmp_path / "good")
    monkeypatch.setattr(subprocess, "run", slow)
    with pytest.raises(vision.OcrError, match="took longer than 7 seconds"):
        vision.Tesseract(str(good), ("eng",), 7.0).read(tmp_path / "x.png")


def test_get_engine_follows_the_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    folder = tmp_path / "bin"
    fake_tesseract(folder)
    on_path(monkeypatch, folder)

    def make(sub: str, **v: Any) -> config.Config:
        return config.load(synth.make_config(tmp_path / sub, vision=v))

    assert vision.get_engine(make("none", ocr="none")) is None
    engine = vision.get_engine(make("t", ocr="tesseract", languages=["eng", "fra"]))
    assert isinstance(engine, vision.Tesseract) and engine.languages == "eng+fra"
    monkeypatch.setitem(sys.modules, "rapidocr_onnxruntime", None)
    with pytest.raises(vision.OcrUnavailable, match=r"rapidocr is not installed.*--extra ocr"):
        vision.get_engine(make("r", ocr="rapidocr"))


# reading a chat


def test_read_chat_stores_text_and_makes_it_searchable(
    store: db.Store, root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from wa_mcp_server import common

    folder = root / "bin"
    fake_tesseract(folder)
    on_path(monkeypatch, folder)
    add_images(root, ["IMG1", "IMG2"])
    engine = vision.get_engine(store.cfg)
    assert engine is not None
    bridge = FakeBridge(root)
    out = vision.read_chat(bridge, synth.ALICE, engine)  # type: ignore[arg-type]
    assert out["complete"] is True and out["candidates"] == 2 and out["failed"] == []
    assert {r["msg_id"] for r in out["read"]} == {"IMG1", "IMG2"}
    assert out["read"][0]["text"] == "Invoice 4711\n\nDue   Friday" and out["read"][0]["cached"] is False
    row = next(r for r in db.derived(synth.ALICE, "IMG1") if r["source"] == "image")
    assert row["content"] == "Invoice 4711\n\nDue   Friday" and row["lang"] == "eng"
    # search finds the image by its text, in any letter case
    found = db.messages(query="INVOICE 4711")
    assert {m["msg_id"] for m in found} == {"IMG1", "IMG2"}
    shaped = common.shape_messages(found)[0]
    assert shaped["image_text"].startswith("Invoice 4711") and "image_description" not in shaped
    assert not bridge.downloads
    again = vision.read_chat(bridge, synth.ALICE, engine)  # type: ignore[arg-type]
    assert again["read"] == [] and again["already_done"] == 2 and again["complete"] is True
    forced = vision.read_chat(bridge, synth.ALICE, engine, force=True, limit=1)  # type: ignore[arg-type]
    assert len(forced["read"]) == 1 and forced["complete"] is False and "force=false" in forced["resume"]


def test_images_without_text_are_remembered(store: db.Store, root: Path) -> None:
    from wa_mcp_server import common

    add_images(root, ["IMG1", "IMG2", "IMG3"])
    engine = FakeEngine({"IMG1": " . ~ ", "IMG2": "okay then", "IMG3": "ok"})
    bridge = FakeBridge(root)
    out = vision.read_chat(bridge, synth.ALICE, engine)  # type: ignore[arg-type]
    by_id = {r["msg_id"]: r for r in out["read"]}
    assert by_id["IMG1"]["no_text"] is True and by_id["IMG1"]["text"] == ""
    assert "no_text" not in by_id["IMG2"] and by_id["IMG2"]["text"] == "okay then"
    assert by_id["IMG3"]["no_text"] is True  # fewer than min_text_chars letters or digits
    assert derived_of("IMG1") == {"image": ""}
    vision.read_chat(bridge, synth.ALICE, engine)  # type: ignore[arg-type]
    assert sorted(engine.read_files) == ["IMG1", "IMG2", "IMG3"]  # not read twice
    shaped = {m["id"]: m for m in common.shape_messages(db.messages(kind="image"))}
    assert "image_text" not in shaped["IMG1"] and "image_text" not in shaped["IMG3"]
    assert shaped["IMG2"]["image_text"] == "okay then"


def test_downloads_missing_files_and_reports_failures(store: db.Store, root: Path) -> None:
    add_images(root, ["IMG1"], saved=False)
    engine = FakeEngine()
    bridge = FakeBridge(root)
    out = vision.read_chat(bridge, synth.ALICE, engine)  # type: ignore[arg-type]
    assert bridge.downloads == [(synth.ALICE, "IMG1")] and out["read"][0]["msg_id"] == "IMG1"
    add_images(root, ["IMG2", "IMG3"], saved=False)
    down = FakeBridge(root, fail=BridgeError(0, "bridge is not running"))
    out = vision.read_chat(down, synth.ALICE, engine)  # type: ignore[arg-type]
    assert out["read"] == [] and out["complete"] is False and out["stopped"] == "the bridge is not reachable"
    assert out["failed"] == [{"msg_id": "IMG3", "error": "bridge is not running"}]
    expired = FakeBridge(root, fail=BridgeError(410, "media expired"))
    out = vision.read_chat(expired, synth.ALICE, engine)  # type: ignore[arg-type]
    assert [f["msg_id"] for f in out["failed"]] == ["IMG3", "IMG2"] and out["complete"] is True


def test_limit_and_time_budget(store: db.Store, root: Path) -> None:
    add_images(root, ["IMG1", "IMG2", "IMG3"])
    engine = FakeEngine()
    bridge = FakeBridge(root)
    out = vision.read_chat(bridge, synth.ALICE, engine, limit=2)  # type: ignore[arg-type]
    assert [r["msg_id"] for r in out["read"]] == ["IMG3", "IMG2"]  # newest first
    assert out["remaining"] == 1 and out["stopped"] == "limit reached" and out["complete"] is False
    ticks = iter([0.0, 0.0, 0.0, 500.0, 500.0])
    out = vision.read_chat(
        bridge,
        synth.ALICE,
        FakeEngine(),
        after=None,
        force=True,
        budget_seconds=120,
        clock=lambda: next(ticks),
    )  # type: ignore[arg-type]
    assert len(out["read"]) == 2 and out["stopped"] == "time budget used up" and out["remaining"] == 1
    since = vision.read_chat(bridge, synth.ALICE, FakeEngine(), after=synth.T0 + 1015, force=True)  # type: ignore[arg-type]
    assert [r["msg_id"] for r in since["read"]] == ["IMG3"]


def test_size_limit_and_kind_checks(store: db.Store, root: Path) -> None:
    add_images(root, ["IMG1"])
    small = dataclasses.replace(store.cfg, vision=dataclasses.replace(store.cfg.vision, max_image_mb=0.00001))
    out = vision.read_chat(FakeBridge(root), synth.ALICE, FakeEngine(), cfg=small)  # type: ignore[arg-type]
    assert out["read"] == [] and "vision.max_image_mb" in out["failed"][0]["error"]
    with pytest.raises(ValueError, match="not an image"):
        vision.read_message(FakeBridge(root), synth.ALICE, "A3", FakeEngine())  # type: ignore[arg-type]
    with pytest.raises(LookupError):
        vision.read_message(FakeBridge(root), synth.ALICE, "NOPE", FakeEngine())  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="nothing to do"):
        vision.read_chat(FakeBridge(root), synth.ALICE, None)  # type: ignore[arg-type]


# descriptions through the provider


def provider_for(store: db.Store, handler: Any) -> ai.Provider:
    cfg = dataclasses.replace(
        store.cfg.ai,
        provider="openai-compatible",
        base_url="https://x.test/v1",
        text_model="t",
        vision_model="v",
        api_key_env="",
    )
    return OpenAICompatible(cfg, transport=httpx.MockTransport(handler))


def test_describe_stores_a_second_row_and_is_cached(store: db.Store, root: Path) -> None:
    from wa_mcp_server import common

    add_images(root, ["IMG1"])
    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(
            200, json={"choices": [{"message": {"content": "A screenshot of an invoice."}}]}
        )

    provider = provider_for(store, handler)
    engine = FakeEngine()
    bridge = FakeBridge(root)
    out = vision.read_chat(bridge, synth.ALICE, engine, provider)  # type: ignore[arg-type]
    item = out["read"][0]
    assert item["description"] == "A screenshot of an invoice." and item["text"].startswith("Invoice")
    assert derived_of("IMG1") == {
        "image": "Invoice 4711\n\nDue   Friday",
        "image_description": "A screenshot of an invoice.",
    }
    part = seen[0]["messages"][-1]["content"][1]["image_url"]["url"]
    assert part.startswith("data:image/png;base64,") and "untrusted" in seen[0]["messages"][0]["content"]
    shaped = common.shape_messages(db.messages(kind="image"))[0]
    assert shaped["image_description"] == "A screenshot of an invoice." and shaped["image_text"].startswith(
        "Invoice"
    )
    assert {m["msg_id"] for m in db.messages(query="screenshot of an invoice")} == {"IMG1"}
    again = vision.read_chat(bridge, synth.ALICE, engine, provider)  # type: ignore[arg-type]
    assert again["already_done"] == 1 and len(seen) == 1 and engine.read_files == ["IMG1"]
    forced = vision.read_chat(bridge, synth.ALICE, engine, provider, force=True)  # type: ignore[arg-type]
    assert len(forced["read"]) == 1 and len(seen) == 2


def test_describe_only_adds_the_missing_part(store: db.Store, root: Path) -> None:
    add_images(root, ["IMG1"])
    engine = FakeEngine()
    bridge = FakeBridge(root)
    vision.read_chat(bridge, synth.ALICE, engine)  # type: ignore[arg-type]
    provider = provider_for(
        store, lambda r: httpx.Response(200, json={"choices": [{"message": {"content": "A photo."}}]})
    )
    out = vision.read_chat(bridge, synth.ALICE, engine, provider)  # type: ignore[arg-type]
    assert out["read"][0]["description"] == "A photo." and engine.read_files == ["IMG1"]
    only = provider_for(
        store, lambda r: httpx.Response(200, json={"choices": [{"message": {"content": "Again."}}]})
    )
    add_images(root, ["IMG2"])
    out = vision.read_chat(bridge, synth.ALICE, None, only)  # type: ignore[arg-type]
    assert [r["msg_id"] for r in out["read"]] == ["IMG2"] and "text" not in out["read"][0]
    assert derived_of("IMG2") == {"image_description": "Again."}


def test_describe_errors_and_limits(store: db.Store, root: Path) -> None:
    add_images(root, ["IMG1", "IMG2"])
    bridge = FakeBridge(root)
    broken = provider_for(store, lambda r: httpx.Response(500, json={"error": "down"}))
    out = vision.read_chat(bridge, synth.ALICE, None, broken)  # type: ignore[arg-type]
    assert len(out["failed"]) == 2 and "500" in out["failed"][0]["error"] and out["read"] == []

    class Limited(OpenAICompatible):
        def _describe(self, *args: Any) -> str:
            raise ai.AILimitError("the limit ai.max_calls_per_day is 1")

    cfg = provider_for(store, lambda r: httpx.Response(200)).cfg
    out = vision.read_chat(bridge, synth.ALICE, None, Limited(cfg))  # type: ignore[arg-type]
    assert out["complete"] is False and "max_calls_per_day" in out["stopped"] and out["remaining"] == 2


# rapidocr (optional extra)


def test_rapidocr_reads_a_generated_image(tmp_path: Path) -> None:
    pytest.importorskip("rapidocr_onnxruntime", reason="optional extra ocr is not installed")
    pil = pytest.importorskip("PIL.ImageDraw", reason="Pillow is needed to draw the test image")
    from PIL import Image, ImageFont

    img = Image.new("RGB", (640, 160), "white")
    font = ImageFont.load_default(size=56)
    pil.Draw(img).text((20, 40), "Invoice 4711", fill="black", font=font)
    path = tmp_path / "shot.png"
    img.save(path)
    engine = vision.RapidOcr()
    engine.check()
    text = vision.clean_text(engine.read(path).text, 3)
    assert "4711" in text and "nvoice" in text.lower()


def test_rapidocr_failure_is_an_ocr_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    class Boom:
        def __call__(self, path: str) -> Any:
            raise RuntimeError("secret detail")

    engine = vision.RapidOcr()
    monkeypatch.setattr(engine, "check", lambda: None)
    engine._engine = Boom()
    with pytest.raises(vision.OcrError, match="RuntimeError") as err:
        engine.read(tmp_path / "x.png")
    assert "secret detail" not in str(err.value)

    class Lines:
        def __call__(self, path: str) -> Any:
            return [[[[0, 0]], "first", 0.9], [[[0, 1]], "second", 0.8]], 0.1

    engine._engine = Lines()
    assert engine.read(tmp_path / "x.png").text == "first\nsecond"

    class Empty:
        def __call__(self, path: str) -> Any:
            return None, 0.0

    engine._engine = Empty()
    assert engine.read(tmp_path / "x.png").text == ""
