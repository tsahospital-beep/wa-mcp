# 6. Media and screenshots

Goal: download attachments, read the text in screenshots and photos with a local OCR engine, and find
messages by what is written in an image. Descriptions of images by an AI model are optional and off by
default. This takes about fifteen minutes.

## Prerequisites

- A client is connected, see [Connect clients](03-connect-clients.md).
- A test image. Send a screenshot with some printed text to your own chat ("message yourself") from the
  phone, so that you do not need a contact's picture.

## Steps

### 1. Download an attachment

Ask: "Download the latest image in my chat with myself." The assistant calls `download_media` with the chat
and the message id. For many files it calls `download_chat_media` with a period and kinds such as `image`,
`video`, `audio`, `voice`, `document` or `sticker`.

You should see the local path of the file. Files go to `downloads_dir/<chat>/`, where the folder name is the
chat address with `@` replaced by `_`, for example `downloads/15550100001_s.whatsapp.net/`. When the
WhatsApp copy has expired, the bridge asks your phone to upload it again and waits up to 40 seconds. The
phone must be online and still have the file. Files larger than `media.max_download_mb` (512) are refused.

### 2. Install an OCR engine

Set `vision.ocr` to one of these. Tesseract is the default.

| `vision.ocr` | What it is | Install |
| --- | --- | --- |
| `tesseract` | The `tesseract` program. Many languages. | A separate program |
| `rapidocr` | RapidOCR on ONNX Runtime, inside the Python process. Its default models read Chinese and English. | The extra `ocr` |
| `none` | Reading text is off. | None |

Tesseract on Windows PowerShell, then add its folder to `PATH` or set `vision.tesseract_binary`:

```powershell
winget install UB-Mannheim.TesseractOCR
```

macOS:

```bash
brew install tesseract
```

Debian and Ubuntu:

```bash
sudo apt install tesseract-ocr
```

For other languages install their data too, for example `tesseract-lang` with Homebrew or
`tesseract-ocr-deu` with apt. RapidOCR needs no program, only the Python extra, which pulls in ONNX Runtime
and OpenCV and is large:

```bash
uv sync --all-packages --inexact --extra ocr
```

You should see `tesseract --version` print a version, or uv finish installing the extra.

### 3. Set the engine and languages

In `config.json`:

```json
{
  "vision": {
    "ocr": "tesseract",
    "languages": ["eng", "deu"]
  }
}
```

`vision.languages` lists the Tesseract language codes. Each code needs its data file. RapidOCR ignores the
list. Restart the client so that the server reads the file, then ask: "Run get_bridge_status." You should see
`ocr_engine` with your choice and `ocr_ready` as `true`. If it is `false`, `ocr_hint` says what is missing.

### 4. Read the text of the images

Ask: "Read the text in the screenshots of my chat with myself." The assistant calls `read_images`:

| Argument | Meaning |
| --- | --- |
| `chat` | A JID, a phone number or a name. |
| `since` | Only newer images: unix seconds, `YYYY-MM-DD` or ISO 8601. Default: all stored images. |
| `limit` | Images per call, 50 by default, at most 200. |
| `force` | `true` reads again, also images that are done. |
| `time_budget_seconds` | Default 120, at most 600. |

Images are read newest first. You should see how many images were read and their text. Output with fewer
than `vision.min_text_chars` (3) letters or digits counts as no text, which keeps noise from ordinary
photos out of the database. When images are left, the answer says `complete=false`. Call again to
continue. Stickers and videos are not read.

### 5. Search by image text

The text is stored with the message, encrypted when encryption is on. Ask: "Which messages mention the
word invoice, including text in screenshots?" The assistant calls `list_messages` with a `query`. You should
see the screenshot among the results, with its text in the field `image_text`. `get_media_text` returns the
stored text of one message.

### 6. Optional: describe images with an AI model

Descriptions need a provider. By default none is set and nothing leaves your machine. To describe images
with a model that runs on your computer through Ollama:

```json
{
  "ai": {
    "provider": "ollama",
    "vision_model": "llava"
  }
}
```

Then ask: "Read and describe the images of my chat with myself." The assistant calls `read_images` with
`describe=true`. You should see a short description in `image_description`. This sends the image to the
provider. With a hosted provider the company receives the picture, so use it with care. Providers, keys and
cost limits are in [AI features and providers](../guides/ai-providers.md).

### 7. Documents work the same way

For a PDF, DOCX, XLSX, PPTX, text or HTML file, ask: "Read the document in this message." The assistant
calls `extract_file_text`, which downloads the file first if needed. Archives (ZIP and RAR) are listed and
never unpacked. The text is shown inline and found by search.

## Troubleshooting

- `tesseract was not found`. Install tesseract and put it on `PATH`, or set `vision.tesseract_binary`.
- `Failed loading language`. The data for a code in `vision.languages` is not installed. Install it or remove
  the code.
- An image reads as `no_text`. Nothing readable was found, or the text was shorter than
  `vision.min_text_chars`. After you change languages or the engine, run `read_images` again with `force=true`.
- `above vision.max_image_mb`. The image is larger than the limit (20 MiB). Raise it or skip the image.
- `rapidocr is not installed`. Run the `uv sync --all-packages --inexact --extra ocr` command. If it fails
  because a dependency has no wheel for your Python version, add `--python 3.13`.

## Next steps

- [Text from photos and screenshots](../guides/images.md): all keys and the privacy notes.
- [Import old exports](07-import-old-exports.md): bring in history that the phone no longer has.
- [Voice transcription](05-voice-transcription.md): the same idea for audio.
