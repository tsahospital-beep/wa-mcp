# Text from photos and screenshots

WA MCP can read the text in images that people send you: screenshots of invoices and chats, photos of
signs and documents. The text is stored with the message, so `list_messages` shows it as `image_text`, search
finds the message by it, and `get_media_text` returns it. Reading runs on your machine with a local OCR
engine. Nothing is sent anywhere unless you also ask for descriptions.

## How it works

1. The attachment is downloaded through the bridge if it is not saved yet.
2. The OCR engine reads the text. Output with fewer than `vision.min_text_chars` letters or digits counts as
   no text, which keeps noise from ordinary photos out of the database.
3. The text is stored in `wa.db` next to the message with source `image` and the language setting used.
   An image without readable text is stored as an empty entry, so it is not read again. With encryption on,
   the text is stored encrypted, like transcripts.
4. With `describe=true`, the image is also sent to the AI provider's vision model and the answer is stored with
   source `image_description` (shown as `image_description`).

Run it from an assistant with the `read_images` tool:

| Argument | Meaning |
| --- | --- |
| `chat` | JID, phone number or name. |
| `since` | Only images newer than this: unix seconds, `YYYY-MM-DD` or ISO 8601. Default: all stored images. |
| `limit` | Images to work on in one call, default 50, at most 200. |
| `describe` | Also describe each image with the AI provider. Needs `ai.provider` and `ai.vision_model`. |
| `force` | Read again, also images that are done. |
| `time_budget_seconds` | Default 120, at most 600. |

Images are processed newest first. Images that are done are skipped. A call stops at `limit` or at the time
budget, then answers with `complete=false`, the number of images `remaining` and a hint to call again with
the same arguments. Repeated calls work through a long archive. Stickers, videos and round videos are not
read.

## OCR engines

Set `vision.ocr` to one of these:

| `vision.ocr` | What it is | Install |
| --- | --- | --- |
| `tesseract` (default) | The `tesseract` program, run without a shell. Many languages. | separate program |
| `rapidocr` | RapidOCR on ONNX Runtime, in the Python process. Its default models read Chinese and English. | extra `ocr` |
| `none` | Reading text is off. `read_images` still works with `describe=true`. | none |

### Tesseract

Install the program and make sure it is on your `PATH`:

| System | Command |
| --- | --- |
| Windows | `winget install UB-Mannheim.TesseractOCR`, then add its folder to `PATH` or set `vision.tesseract_binary` |
| macOS | `brew install tesseract` (add `tesseract-lang` for more languages) |
| Debian and Ubuntu | `sudo apt install tesseract-ocr`, plus `tesseract-ocr-deu` and similar packages for other languages |

`vision.languages` lists the language codes to read, for example `["eng", "deu"]`. Each code needs its
language data file. Tesseract exits with an error that names a missing language, and `read_images`
reports that error for each image.

### RapidOCR

Install the extra in the project environment:

```bash
uv sync --all-packages --inexact --extra ocr
```

The extra pulls in ONNX Runtime and OpenCV, which are large. If the install fails because a dependency has
no wheel for your Python version, pick a version it supports, for example
`uv sync --python 3.13 --all-packages --inexact --extra ocr`. RapidOCR runs inside the server process, so
`vision.timeout_s` does not apply to it, and `vision.languages` is not used.

## Settings

| Key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `vision.ocr` | string | `"tesseract"` | `tesseract`, `rapidocr` or `none`. |
| `vision.languages` | list of strings | `["eng"]` | Tesseract language codes. Several codes are read together. |
| `vision.tesseract_binary` | string | `""` | Path of the `tesseract` program. Empty means look it up on `PATH`. |
| `vision.max_image_mb` | number | `20` | Largest image that is read or sent to a provider, in MiB. |
| `vision.timeout_s` | number | `60` | Seconds for one image with tesseract. |
| `vision.min_text_chars` | integer | `3` | Fewer letters and digits than this count as no text. |

## Checking the setup

`bridge_status` reports `ocr_engine` and `ocr_ready`. When the engine is not usable, `ocr_hint` says what is
missing.

## Privacy

OCR with `tesseract` or `rapidocr` never leaves your machine. `describe=true` sends the image file to the
provider you configured. With a hosted provider that means the company receives your contact's picture, so
use it with care. See [AI features and providers](ai-providers.md).

The text and descriptions are written or influenced by other people. Treat them as untrusted data: the tools
add a note, and the assistant should not follow instructions found in an image.

## Troubleshooting

| Message | Cause and fix |
| --- | --- |
| `tesseract was not found` | Install tesseract and put it on `PATH`, or set `vision.tesseract_binary`. |
| `rapidocr is not installed` | Install the `ocr` extra, see above. |
| `tesseract exited with 1: ... Failed loading language` | The language data for a code in `vision.languages` is not installed. |
| `reading text from images is off` | `vision.ocr` is `none` and `describe` was not set. |
| `above vision.max_image_mb` | The image is larger than the limit. Raise it or skip the image. |
| An image reads as `no_text` | Nothing readable was found. Use `force=true` after changing languages or the engine. |
