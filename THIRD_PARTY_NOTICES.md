# Third-party notices

WA MCP is released under the [MIT License](LICENSE). It depends on the third-party software listed
below. None of it is copied into this repository. Go modules are fetched by `go build` and Python
packages by `uv sync`, each under its own license.

Licenses were checked on 2026-11-23 against the Go module cache (the `LICENSE` file of each module
version listed in `bridge/go.mod` and linked into `wa-bridge`) and against the package metadata of the
Python packages. Entries that could not be verified that way are marked.

## WhatsApp library

`go.mau.fi/whatsmeow` is used unmodified as a library. `bridge/go.mod` has no `replace` directive for
it. It is licensed under the Mozilla Public License 2.0, and its source is available at
https://github.com/tulir/whatsmeow. The MPL-2.0 applies to the whatsmeow files themselves. The rest of
this repository stays under the MIT License.

## Go modules linked into wa-bridge

| Module | Version | License |
| --- | --- | --- |
| go.mau.fi/whatsmeow | v0.0.0-20261110152934-c88388f983b4 | MPL-2.0 |
| go.mau.fi/util | v0.11.0 | MPL-2.0 |
| go.mau.fi/libsignal | v0.2.2 | GPL-3.0 (see the note below) |
| github.com/glebarez/go-sqlite | v1.23.0 | BSD-3-Clause |
| modernc.org/sqlite | v1.55.0 | BSD-3-Clause |
| modernc.org/libc | v1.74.1 | BSD-3-Clause (third-party notices in the module) |
| modernc.org/mathutil | v1.7.1 | BSD-3-Clause |
| modernc.org/memory | v1.11.0 | BSD-3-Clause |
| github.com/mdp/qrterminal | v1.0.1 | MIT (not verified from the module, see below) |
| github.com/skip2/go-qrcode | v0.0.0-20200617195104-da1b6568686e | MIT |
| rsc.io/qr | v0.2.0 | BSD-3-Clause |
| golang.org/x/crypto | v0.58.0 | BSD-3-Clause |
| golang.org/x/exp | v0.0.0-20261014143641-f0251eb4aa3e | BSD-3-Clause |
| golang.org/x/net | v0.61.0 | BSD-3-Clause |
| golang.org/x/sync | v0.24.0 | BSD-3-Clause |
| golang.org/x/sys | v0.49.0 | BSD-3-Clause |
| golang.org/x/text | v0.43.0 | BSD-3-Clause |
| google.golang.org/protobuf | v1.36.12 | BSD-3-Clause |
| filippo.io/edwards25519 | v1.2.0 | BSD-3-Clause |
| github.com/google/uuid | v1.6.0 | BSD-3-Clause |
| github.com/remyoudompheng/bigfft | v0.0.0-20230129092748-24d4a6f8daec | BSD-3-Clause |
| github.com/coder/websocket | v1.8.15 | ISC |
| github.com/beeper/argo-go | v1.1.2 | MIT |
| github.com/dustin/go-humanize | v1.0.1 | MIT |
| github.com/elliotchance/orderedmap/v3 | v3.1.0 | MIT |
| github.com/mattn/go-colorable | v0.1.14 | MIT |
| github.com/mattn/go-isatty | v0.0.20 | MIT |
| github.com/ncruces/go-strftime | v1.0.0 | MIT |
| github.com/rs/zerolog | v1.35.1 | MIT |
| github.com/vektah/gqlparser/v2 | v2.5.27 | MIT |
| github.com/petermattis/goid | v0.0.0-20260918085751-abfca077860b | Apache-2.0 |

### Notes on individual modules

- `go.mau.fi/libsignal` is licensed under the GNU General Public License version 3 (the `LICENSE` file
  of the module is the GPL-3.0 text). It is a dependency of whatsmeow. This repository does not contain
  or redistribute its source. `wa-bridge` links it statically when you build the bridge. If you
  distribute a compiled `wa-bridge` binary, the GPL-3.0 terms for this component apply to that binary,
  including the license text and an offer of the corresponding source.
- `github.com/mdp/qrterminal` v1.0.1: the module archive contains no license file. The upstream
  repository at https://github.com/mdp/qrterminal publishes an MIT license (checked on 2026-11-23),
  but that is not the text shipped with this module version. Treat this entry as unverified for the
  exact version.
- `modernc.org/sqlite` and `github.com/glebarez/go-sqlite` embed a Go translation of SQLite. SQLite
  itself is in the public domain.

## Python packages

The Python packages are not bundled. `uv sync` installs them from PyPI.

### Runtime dependencies

| Package | Used by | License |
| --- | --- | --- |
| httpx | SDK (bridge client, link fetching, HTTP transcription) | BSD-3-Clause |
| mcp | MCP server | MIT |
| cryptography | optional `encryption` extra | Apache-2.0 OR BSD-3-Clause |
| argon2-cffi | optional `encryption` extra | MIT |
| pypdf | optional `files` extra (PDF text) | BSD-3-Clause |
| rarfile | optional `files` extra (RAR listing) | ISC |
| faster-whisper | optional `faster-whisper` extra | MIT |
| openai-whisper | optional `openai-whisper` extra | MIT |
| rapidocr-onnxruntime | optional `ocr` extra (image text) | Apache-2.0 |

### Dependencies of the MCP package and the base extras

These are installed by `uv sync` as transitive dependencies. Licenses come from the installed package
metadata.

| Package | License |
| --- | --- |
| annotated-types, anyio, attrs, h11, httpx-sse, jsonschema, jsonschema-specifications, pydantic, pydantic-core, pydantic-settings, PyJWT, referencing, rpds-py, typing-inspection | MIT |
| httpcore, idna, python-dotenv, sse-starlette, starlette, uvicorn, click, pycparser | BSD-3-Clause |
| argon2-cffi-bindings | MIT |
| cffi | MIT-0 |
| certifi | MPL-2.0 |
| python-multipart | Apache-2.0 |
| typing-extensions | PSF-2.0 |
| pywin32 (Windows only) | PSF |

### Optional whisper extras

The `faster-whisper` and `openai-whisper` extras install large dependency sets. The direct
dependencies below were checked against PyPI metadata on 2026-11-23.

| Package | License |
| --- | --- |
| ctranslate2 | MIT |
| av | BSD-3-Clause |
| huggingface-hub | Apache-2.0 |
| tokenizers | Apache-2.0 |
| onnxruntime | MIT |
| tqdm | MPL-2.0 AND MIT |
| numba | BSD |
| numpy | BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0 |
| torch | multiple permissive licenses (see its metadata) |
| more-itertools | MIT |
| tiktoken | MIT |
| triton | MIT |

Not verified individually: further transitive dependencies of these extras, including the NVIDIA CUDA
packages that PyTorch can pull in on some platforms. They are installed from their own distributors
under their own licenses and are not redistributed by this project.

### Optional OCR extra

The `ocr` extra installs `rapidocr-onnxruntime` with the direct dependencies below. Licenses come from the
package metadata of the versions installed on 2026-11-23. RapidOCR ships its recognition models inside the
package under its own license (Apache-2.0).

| Package | License |
| --- | --- |
| onnxruntime | MIT |
| opencv-python | Apache-2.0 (its wheels bundle FFmpeg libraries under the LGPL) |
| numpy | BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0 |
| Pillow | MIT-CMU |
| pyclipper | MIT |
| PyYAML | MIT |
| Shapely | BSD-3-Clause |
| six | MIT |
| tqdm | MPL-2.0 AND MIT |
| protobuf, flatbuffers, packaging, colorama | BSD-3-Clause, Apache-2.0, Apache-2.0 OR BSD-2-Clause, BSD-3-Clause |

Not verified individually: further transitive dependencies of ONNX Runtime and OpenCV.

### Development tools

`ruff`, `mypy`, `pytest` and `pytest-cov` are used for development only and are not part of a
release. Their licenses are MIT (`ruff`, `mypy`, `pytest`, `pytest-cov`), with `coverage` under
Apache-2.0.

## Programs used at run time but not bundled

- ffmpeg converts audio and images when you send voice notes or stickers and prepares audio for
  transcription. Install it separately. Its license depends on how it was built.
- whisper.cpp (`whisper-cli`) is an optional separate program for transcription.
- tesseract is an optional separate program that reads text from images (`vision.ocr`). It is licensed under
  Apache-2.0. Install it separately, together with the language data you need.
- The login window opens in an installed Chromium-based browser (Edge, Chrome, Brave or Chromium) when
  one is found, and in the default browser otherwise.

## Container images

`deploy/docker` builds images on your machine from these public base images. Nothing of them is copied
into this repository. An image you build and distribute contains the software of its base image under
that software's licenses, and the bridge image contains `wa-bridge` with the GPL-3.0 note above. The
licenses below are those published by the projects. They were not checked inside the images, because
Docker was not available when this list was written.

| Image | Used for | License |
| --- | --- | --- |
| `golang:1.26-bookworm` | build stage of the bridge only | Go: BSD-3-Clause; Debian packages under their own licenses |
| `gcr.io/distroless/static-debian12:nonroot` | runtime of the bridge | Apache-2.0 (distroless); Debian packages such as ca-certificates and tzdata under their own licenses |
| `ghcr.io/astral-sh/uv:0.13.7` | build stage of the server only (`uv`) | Apache-2.0 OR MIT |
| `python:3.13-slim-bookworm` | build stage and runtime of the server | Python: PSF-2.0; Debian packages under their own licenses |
| `caddy:2.8.4-alpine` | optional HTTPS proxy (profile `caddy`) | Caddy: Apache-2.0; Alpine packages under their own licenses |

With `--build-arg WITH_FFMPEG=true` the server image also contains the Debian `ffmpeg` package, which is
built with GPL components (GPL-2.0-or-later).
