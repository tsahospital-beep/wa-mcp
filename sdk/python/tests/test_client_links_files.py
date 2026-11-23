from __future__ import annotations

import hashlib
import json
import sqlite3
import zipfile
from contextlib import closing
from pathlib import Path

import httpx
import pytest
import synth

from wa_mcp import config, db, files, links
from wa_mcp.client import Bridge, BridgeError

PUBLIC = ["93.184.216.34"]


def public(_host: str) -> list[str]:
    return PUBLIC


# client


def test_bridge_client_sends_token_and_parses(tmp_path: Path) -> None:
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        if req.url.path == "/api/v1/status":
            return httpx.Response(200, json={"ok": True, "connected": True, "logged_in": True})
        if req.url.path == "/api/v1/send":
            return httpx.Response(429, json={"ok": False, "error": "limit per_hour reached"})
        if req.url.path == "/api/v1/schedule" and req.method == "POST":
            return httpx.Response(201, json={"ok": True, "id": 7, "next_run": 1})
        return httpx.Response(404, json={"ok": False, "error": "not found"})

    (tmp_path / "store").mkdir()
    (tmp_path / "store" / "api.token").write_text("file-token-abcdef0123456789\n", encoding="utf-8")
    cfg = config.load(tmp_path / "config.json")
    with Bridge(cfg=cfg, transport=httpx.MockTransport(handler)) as b:
        assert b.base_url == "http://127.0.0.1:8080/api/v1"
        assert b.status()["connected"] is True
        assert seen[0].headers["authorization"] == "Bearer file-token-abcdef0123456789"
        with pytest.raises(BridgeError) as err:
            b.send("15550100002", text="hi", reply_to="ABC", mentions=["15550100003"])
        assert err.value.status == 429 and "per_hour" in err.value.message
        body = json.loads(seen[-1].content)
        assert body == {
            "to": "15550100002",
            "text": "hi",
            "reply_to": {"id": "ABC"},
            "mentions": ["15550100003"],
            "human": True,
        }
        assert b.schedule("15550100002", at="2030-01-01T10:00:00Z", text="x")["id"] == 7
        assert json.loads(seen[-1].content)["at"] == "1893492000"
        with pytest.raises(BridgeError) as err:
            b.cancel_scheduled(3)
        assert err.value.status == 404


def test_bridge_unreachable_and_no_token(tmp_path: Path) -> None:
    def boom(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=req)

    cfg = config.load(tmp_path / "config.json")
    with Bridge(cfg=cfg, token="", transport=httpx.MockTransport(boom)) as b, pytest.raises(BridgeError) as e:
        b.status()
    assert "token" in e.value.message
    with (
        Bridge(cfg=cfg, token="t" * 20, transport=httpx.MockTransport(boom)) as b,
        pytest.raises(BridgeError) as e,
    ):
        b.status()
    assert e.value.status == 0 and "not reachable" in e.value.message


# links


@pytest.mark.parametrize(
    "url",
    [
        "http://docs.google.com/document/d/x",
        "https://user:pw@docs.google.com/document/d/x",
        "https://docs.google.com:8443/document/d/x",
        "https://127.0.0.1/",
        "https://[::1]/",
        "https://10.1.2.3/",
        "https://169.254.169.254/latest/meta-data",
        "file:///etc/passwd",
    ],
)
def test_guard_refuses(url: str) -> None:
    with pytest.raises(links.LinkError):
        links.check_url(url, resolve=public)


def test_guard_private_dns_and_host_list() -> None:
    with pytest.raises(links.LinkError, match="private"):
        links.check_url("https://intranet.example/", resolve=lambda h: ["192.168.1.5"])
    with pytest.raises(links.LinkError, match="not allowed"):
        links.check_url("https://evil.example/", allowed_hosts=links.DOC_HOSTS, resolve=public)
    assert links.check_url("https://docs.google.com/x", allowed_hosts=links.DOC_HOSTS, resolve=public)


def test_redirect_to_other_host_is_refused() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "https://evil.example/steal"})

    link = links.find_doc_links("see https://docs.google.com/document/d/AbCdEf123_-x/edit")[0]
    with pytest.raises(links.LinkError, match="not allowed"):
        links.fetch_doc(link, transport=httpx.MockTransport(handler), resolve=public)


def test_size_cap() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x" * 2048)

    with pytest.raises(links.LinkError, match="larger"):
        links.guarded_get(
            "https://docs.google.com/a",
            max_bytes=1024,
            transport=httpx.MockTransport(handler),
            resolve=public,
        )


def test_doc_links_and_fetch(store: db.Store) -> None:
    found = links.find_doc_links(
        "a https://docs.google.com/spreadsheets/d/Sheet1234567/edit#gid=42 and "
        "https://docs.google.com/presentation/d/Slides123456/edit"
    )
    assert [f.export_url for f in found] == [
        "https://docs.google.com/spreadsheets/d/Sheet1234567/export?format=csv&gid=42",
        "https://docs.google.com/presentation/d/Slides123456/export/txt",
    ]

    def handler(req: httpx.Request) -> httpx.Response:
        assert req.headers["host"] == "docs.google.com"
        return httpx.Response(200, text="Chapter one", headers={"content-type": "text/plain"})

    res = links.fetch_shared_docs(synth.GROUP, transport=httpx.MockTransport(handler), resolve=public)
    assert res["fetched"][0]["msg_id"] == "G1"
    assert "Chapter one" in db.derived(synth.GROUP, "G1")[0]["content"]
    assert links.link_status("https://10.0.0.1/")["ok"] is False


# files


def _office(path: Path, parts: dict[str, str]) -> Path:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, xml in parts.items():
            zf.writestr(name, xml)
    return path


W = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
A = 'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"'
X = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'


def test_office_files(tmp_path: Path) -> None:
    docx = _office(
        tmp_path / "a.docx",
        {"word/document.xml": f"<w:document {W}><w:body><w:p><w:r><w:t>Café agenda</w:t></w:r></w:p>"
         "<w:p><w:r><w:t>Item</w:t><w:tab/><w:t>two</w:t></w:r></w:p></w:body></w:document>"},
    )  # fmt: skip
    assert files.extract_text(docx).text == "Café agenda\nItem\ttwo"
    xlsx = _office(
        tmp_path / "b.xlsx",
        {
            "xl/workbook.xml": f'<workbook {X}><sheets><sheet name="Budget"/></sheets></workbook>',
            "xl/sharedStrings.xml": f"<sst {X}><si><t>Snacks</t></si></sst>",
            "xl/worksheets/sheet1.xml": f'<worksheet {X}><sheetData><row><c t="s"><v>0</v></c><c><v>12.5</v></c>'
            "</row></sheetData></worksheet>",
        },
    )
    assert files.extract_text(xlsx).text == "--- sheet Budget ---\nSnacks\t12.5"
    pptx = _office(
        tmp_path / "c.pptx",
        {
            "ppt/slides/slide2.xml": f"<p:sld {A} xmlns:p='p'><a:p><a:r><a:t>Second</a:t></a:r></a:p></p:sld>",
            "ppt/slides/slide1.xml": f"<p:sld {A} xmlns:p='p'><a:p><a:r><a:t>First</a:t></a:r></a:p></p:sld>",
        },
    )
    assert files.extract_text(pptx).text == "--- slide 1 ---\nFirst\n--- slide 2 ---\nSecond"
    evil = _office(tmp_path / "d.docx", {"word/document.xml": '<!DOCTYPE x [<!ENTITY a "aaaa">]><x>&a;</x>'})
    with pytest.raises(files.FileError, match="DTD"):
        files.extract_text(evil)


def _pdf_bytes(text: str) -> bytes:
    stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode()
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = b"%PDF-1.4\n"
    offsets = []
    for i, body in enumerate(objs, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    out += b"".join(b"%010d 00000 n \n" % o for o in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref)
    return out


def test_pdf_txt_html(tmp_path: Path) -> None:
    pdf = tmp_path / "plan.pdf"
    pdf.write_bytes(_pdf_bytes("Meeting at noon"))
    assert "Meeting at noon" in files.extract_text(pdf).text
    html = tmp_path / "p.html"
    html.write_text("<html><script>evil()</script><p>Hello <b>there</b></p></html>", encoding="utf-8")
    assert files.extract_text(html).text == "Hello there"
    with pytest.raises(files.FileError, match="executable"):
        (tmp_path / "x.exe").write_bytes(b"MZ")
        files.extract_text(tmp_path / "x.exe")


def test_zip_inventory_and_bomb(tmp_path: Path, store: db.Store) -> None:
    z = _office(tmp_path / "pack.zip", {"notes.txt": "read me", "setup.exe": "MZ", "docs/readme.md": "# hi"})
    res = files.extract_message(synth.ALICE, "A4", z)
    assert res.source == "archive"
    assert "setup.exe (2 bytes)  [executable, not opened]" in res.text
    assert "--- notes.txt ---\nread me" in res.text
    assert db.derived(synth.ALICE, "A4")[0]["source"] == "archive"

    bomb = tmp_path / "bomb.zip"
    with zipfile.ZipFile(bomb, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("zeros.txt", b"\0" * (8 * 1024 * 1024))
    with pytest.raises(files.FileError, match="ratio"):
        files.extract_text(bomb)
    many = tmp_path / "many.zip"
    with zipfile.ZipFile(many, "w") as zf:
        for i in range(30):
            zf.writestr(f"f{i}.txt", "x")
    with pytest.raises(files.FileError, match="entries"):
        files.extract_text(many, files.Limits(max_members=10))
    with pytest.raises(files.FileError, match="unpacks"):
        files.extract_text(z, files.Limits(max_total_uncompressed=5))


def test_office_numeric_fields_are_bounded(tmp_path: Path) -> None:
    names = ["ppt/slides/slide" + "9" * 5000 + ".xml", "ppt/slides/slide2.xml"]
    assert files._numbered([*names, "ppt/slides/slide\u00b2.xml"], "ppt/slides/slide") == names[::-1]
    sheet = _office(
        tmp_path / "index.xlsx",
        {
            "xl/sharedStrings.xml": "<sst><si><t>first</t></si></sst>",
            "xl/worksheets/sheet1.xml": "<sheet><row><c t='s'><v>000</v></c><c t='s'><v>"
            + "9" * 5000
            + "</v></c></row></sheet>",
        },
    )
    assert files.extract_text(sheet).text.startswith("--- sheet 1 ---\nfirst\t999")


def test_attachment_cache_checks_location_and_link_count(tmp_path: Path, store: db.Store) -> None:
    from wa_mcp import media

    other = tmp_path / "private.txt"
    other.write_text("private")
    assert media.local_file({"local_path": str(other)}) is None
    store.cfg.downloads_dir.mkdir(parents=True, exist_ok=True)
    link = store.cfg.downloads_dir / "link.txt"
    link.hardlink_to(other)
    assert media.local_file({"local_path": str(link)}) is None

    cached = store.cfg.downloads_dir / "voice.ogg"
    cached.write_bytes(b"voice")
    with closing(sqlite3.connect(store.path)) as conn:
        conn.execute(
            "UPDATE chat_messages SET plain_sha256=?, byte_size=? WHERE msg_id='A3'",
            (hashlib.sha256(b"voice").digest(), 5),
        )
        conn.commit()
    row = store.find_message("A3")
    assert row is not None
    row["local_path"] = str(cached)
    assert media.local_file(row) == cached
    cached.write_bytes(b"other")
    assert media.local_file(row) is None


def test_sheet_shared_text_stops_at_output_limit(tmp_path: Path) -> None:
    sheet = _office(
        tmp_path / "shared.xlsx",
        {
            "xl/sharedStrings.xml": "<sst><si><t>" + "abc" * 300 + "</t></si></sst>",
            "xl/worksheets/sheet1.xml": "<sheet><row>" + "<c t='s'><v>0</v></c>" * 50 + "</row></sheet>",
        },
    )
    limit = files.Limits(max_text_chars=100)
    result = files.extract_text(sheet, limit)
    assert result.truncated and len(result.text) == limit.max_text_chars
    with files._open_zip(sheet, limit, "sheet") as archive:
        assert len(files._xlsx(files._Zip(archive, limit))) <= limit.max_text_chars + 1


def test_pinned_redirect_hosts_do_not_share_cookies() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if req.headers["host"] == "first.example":
            return httpx.Response(
                302, headers={"location": "https://second.example/file", "set-cookie": "first=value; Path=/"}
            )
        assert req.headers["host"] == "second.example" and "cookie" not in req.headers
        return httpx.Response(200, content=b"ok")

    assert (
        links.guarded_get(
            "https://first.example/",
            resolve=lambda _: ["93.184.216.34"],
            transport=httpx.MockTransport(handler),
        ).body
        == b"ok"
    )
