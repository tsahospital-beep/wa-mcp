# Voice transcription

WA MCP can turn voice notes and audio messages into text. Transcripts are stored with the message, shown
inline by `list_messages` and included in search, task detection and analytics. Transcription is optional
and runs on your machine unless you point it at a remote endpoint.

## How it works

1. The attachment is downloaded through the bridge if it is not saved yet.
2. ffmpeg converts it to 16 kHz mono WAV.
3. Long audio is cut into chunks of about `transcription.chunk_seconds` at quiet points.
4. The configured backend turns each chunk into text. The chunks are joined.
5. The text, the language and the language confidence are stored in `wa.db` next to the message. With
   encryption on, the text is stored encrypted.

The language comes from `transcription.language` when you set it. Otherwise the backend detects it from
the first chunk. When the clip is shorter than 8 seconds or the detection is unsure, and earlier
confident transcripts of the same chat mostly share one language, that language is used instead.

Run it from an assistant with the `transcribe_chat_audio` tool. By default it covers the last 14 days of
one chat. `full_archive=true` covers every stored message, `limit` caps how many clips are processed in one
call and `force=true` transcribes again. Clips that already have a transcript are skipped.

A call stops after `time_budget_seconds` (default 120, at most 600). When clips are left it answers with
`complete=false`, the number of clips `remaining` and a hint to call again with the same arguments and
`force=false`. Clips that are done are skipped on the next call, so repeated calls work through a long
archive.

## Requirements

- ffmpeg on your `PATH`, or its path in the environment variable `WA_MCP_FFMPEG`.
- One backend, chosen with `transcription.backend`.

## Backends

| `transcription.backend` | What it is | Install |
| --- | --- | --- |
| `faster-whisper` (default) | Whisper models on CTranslate2. Runs on CPU or an NVIDIA GPU. | extra `faster-whisper` |
| `openai-whisper` | The reference Whisper implementation on PyTorch. | extra `openai-whisper` |
| `whisper.cpp` | The `whisper-cli` program with a ggml model file. | separate download |
| `http` | Any server that implements the OpenAI `/v1/audio/transcriptions` API. | none |
| `command` | Any program that prints the text of a WAV file. | none |

`whisper-cpp` and `whispercpp` are accepted as aliases of `whisper.cpp`. Underscores in the backend name
are read as hyphens.

### faster-whisper and openai-whisper

`transcription.model` names the model. The default is `large-v3`, the most accurate and the largest.
Smaller names such as `medium` or `small` need less memory and time. The model is downloaded on first use
unless you download it ahead of time, see below.

```json
{
  "transcription": {
    "backend": "faster-whisper",
    "model": "large-v3",
    "device": "auto",
    "compute_type": "auto",
    "language": null
  }
}
```

`device` and `compute_type` apply to faster-whisper (`compute_type`, for example `int8` or `float16`) and
`device` also to openai-whisper.

### whisper.cpp

whisper.cpp is a separate program. Download or build `whisper-cli` and a ggml model, then point to them:

```json
{
  "transcription": {
    "backend": "whisper.cpp",
    "whisper_cpp": { "binary": "/opt/whisper/whisper-cli", "model": "/opt/whisper/ggml-large-v3.bin" }
  }
}
```

### Remote or local server (http)

Works with any OpenAI-compatible transcription endpoint, including servers you run yourself. The request
is a multipart upload with the fields `model`, `response_format=verbose_json` and optionally `language`.
If you set `transcription.http.api_key_env` and that variable is set, it is sent as a bearer token.

```json
{
  "transcription": {
    "backend": "http",
    "http": {
      "url": "http://127.0.0.1:8000/v1/audio/transcriptions",
      "model": "whisper-1",
      "api_key_env": "WA_MCP_STT_API_KEY"
    }
  }
}
```

If the URL is not on your machine, the audio of your voice notes is sent to that server. To use the same
server as your AI provider, see [AI features and providers](ai-providers.md#speech-to-text-with-the-same-server).

### Your own command

`transcription.command.argv` is a program and its arguments. `{file}` is replaced by the WAV path and
`{language}` by the language code, or `auto`. The program must print the transcript on standard output.
Set the program name explicitly. On Windows, use an executable or pass a script to its interpreter.
The program runs without a shell and has 30 minutes per chunk.

```json
{
  "transcription": {
    "backend": "command",
    "command": { "argv": ["my-stt", "--input", "{file}", "--lang", "{language}"] }
  }
}
```

## Install a backend

The install scripts run from the repository root. They need uv, add the Python packages of a backend to the
project environment with `uv sync --all-packages --inexact --extra <backend>`, and warn when ffmpeg is
missing.

Windows PowerShell:

```powershell
scripts\install_whisper.ps1 -Backend faster-whisper -Model large-v3 -Download
```

macOS and Linux:

```bash
scripts/install_whisper.sh --backend faster-whisper --model large-v3 --download
```

`-Backend` and `--backend` take `faster-whisper`, `openai-whisper` or `whisper.cpp`. For `whisper.cpp` the
script only prints what to download. `-Download` and `--download` fetch the model now instead of at first
use. Afterwards set `transcription.backend` and `transcription.model` in `config.json`.

The scripts use `--inexact`, so the installed backend stays in place. A plain `uv sync` without that flag
removes packages that are not in its selection, which includes an extra added earlier. Run the same
command by hand when you prefer it:

```bash
uv sync --all-packages --inexact --extra faster-whisper
```

If the install fails because a dependency has no wheel for your Python version, choose a version it
supports, for example `uv sync --python 3.13 --all-packages --inexact --extra faster-whisper`.

## Check that it works

Ask the assistant to run `bridge_status`, or call the tool yourself. The answer includes
`transcription_backend` and `transcription_ready`. When the backend is not ready, `transcription_hint`
says what is missing.

## Troubleshooting

| Message | Cause and fix |
| --- | --- |
| `ffmpeg not found` | Install ffmpeg and put it on `PATH`, or set `WA_MCP_FFMPEG`. |
| `faster-whisper is not installed` | Install the extra, see above. |
| `open() got an unexpected keyword argument 'metadata_errors'` | On Windows this can occur with incompatible PyAV and faster-whisper versions. WA MCP already supplies decoded WAV audio. Pin an `av` version compatible with your installed faster-whisper release, or set `transcription.backend` to `command`. |
| `cannot load model` | The model name is wrong or the download failed. Check the name and your network. |
| `whisper.cpp binary not found` | Set both `transcription.whisper_cpp.binary` and `transcription.whisper_cpp.model` to existing files. |
| Short voice notes come out in the wrong language | Set `transcription.language` to the language you use most. |
