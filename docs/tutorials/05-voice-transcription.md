# 5. Voice transcription

Goal: turn voice notes and audio messages into text on your machine, so that the assistant can read them
and search and task detection can find what was said. This takes about fifteen minutes, plus the model
download.

## Prerequisites

- A client is connected, see [Connect clients](03-connect-clients.md).
- ffmpeg on your `PATH`. It converts every clip to 16 kHz mono WAV. See [Install](01-install.md).
- Free disk space for a model. The default model `large-v3` is the most accurate and the largest.

## Steps

### 1. Check ffmpeg

```bash
ffmpeg -version
```

You should see the version banner. If ffmpeg is installed but not on your `PATH`, set the variable
`WA_MCP_FFMPEG` to the full path of the program, for example `$env:WA_MCP_FFMPEG = "C:\tools\ffmpeg\bin\ffmpeg.exe"`
in PowerShell or `export WA_MCP_FFMPEG=/opt/ffmpeg/bin/ffmpeg` in bash. For an MCP client, put the variable in
the `env` block of the server entry.

### 2. Pick a backend

| `transcription.backend` | What it is | Install |
| --- | --- | --- |
| `faster-whisper` (default) | Whisper models on CTranslate2, on CPU or an NVIDIA GPU. | Install script |
| `openai-whisper` | The reference Whisper on PyTorch. | Install script |
| `whisper.cpp` | The `whisper-cli` program with a ggml model file. | Download it yourself |
| `http` | Any server with the OpenAI `/v1/audio/transcriptions` API. | None |
| `command` | Any program that prints the text of a WAV file. | None |

If you are unsure, take `faster-whisper`. On a computer without a GPU, a smaller model such as `small`
needs much less time and memory than `large-v3`.

### 3. Install it

The install scripts run from the repository folder. They add the packages of a backend to the project
environment and warn when ffmpeg is missing. `-Download` and `--download` fetch the model now instead of at
first use.

Windows PowerShell:

```powershell
scripts\install_whisper.ps1 -Backend faster-whisper -Model small -Download
```

macOS and Linux:

```bash
scripts/install_whisper.sh --backend faster-whisper --model small --download
```

You should see uv install the backend and then the model download finish. For `whisper.cpp` the script only
prints what to download. For `http` and `command` there is nothing to install here.

A plain `uv sync` removes packages that are not in its selection, which includes the backend you just added.
The scripts and the command below use `--inexact`, so the backend stays. Run this after an update:

```bash
uv sync --all-packages --inexact --extra faster-whisper
```

### 4. Set the backend in `config.json`

Create `config.json` from [`config.example.json`](../../config.example.json) if you have none, and set the
`transcription` section. For faster-whisper:

```json
{
  "transcription": {
    "backend": "faster-whisper",
    "model": "small"
  }
}
```

The other backends need a few more keys.

whisper.cpp:

```json
{
  "transcription": {
    "backend": "whisper.cpp",
    "whisper_cpp": { "binary": "/opt/whisper/whisper-cli", "model": "/opt/whisper/ggml-small.bin" }
  }
}
```

A server that speaks the OpenAI transcription API, on your own machine or elsewhere. If the address is not
on your machine, the audio of your voice notes is sent to that server. `api_key_env` names the variable that
holds the key:

```json
{
  "transcription": {
    "backend": "http",
    "http": { "url": "http://127.0.0.1:8000/v1/audio/transcriptions", "model": "whisper-1", "api_key_env": "WA_MCP_STT_API_KEY" }
  }
}
```

Your own program. `{file}` becomes the WAV path and `{language}` the language code or `auto`. The program
prints the text on standard output. It runs without a shell:

```json
{
  "transcription": {
    "backend": "command",
    "command": { "argv": ["my-stt", "--input", "{file}", "--lang", "{language}"] }
  }
}
```

Restart the client so that the MCP server reads the file.

### 5. Check that the backend is ready

Ask the assistant: "Run bridge_status." You should see `transcription_backend` with the name you set and
`transcription_ready` as `true`. If it is `false`, `transcription_hint` says what is missing.

### 6. Transcribe a chat

Ask: "Transcribe the voice notes of the last 7 days in my chat with Alice." The assistant calls
`transcribe_chat_audio` with `chat` and `days`. By default it covers 14 days. Other arguments:

| Argument | Meaning |
| --- | --- |
| `full_archive` | `true` covers every stored message. |
| `limit` | How many clips to process in one call. |
| `force` | `true` transcribes again. Clips that have a transcript are skipped otherwise. |
| `time_budget_seconds` | A call stops after this time. Default 120, at most 600. |

You should see the transcribed clips with their language. When clips are left, the answer says
`complete=false`, gives the number `remaining` and asks you to call again with the same arguments. Do that
until it is complete. Clips that are done are skipped, so repeated calls work through a long archive.

Then ask: "What did Alice say in her last voice note?" The transcript is stored with the message and shown
inline by `list_messages`. Searching for a word finds the voice note too.

### 7. Set the language

By default the backend detects the language of each clip. To force one language, set a code such as `en`:

```json
{
  "transcription": { "language": "en" }
}
```

There is no per-chat setting. Instead, WA MCP learns the usual language of each chat. When a clip is shorter
than 8 seconds, or the detection is unsure (confidence below 0.7), it uses the usual language of that chat,
provided at least three earlier transcripts of the chat were detected with a confidence of 0.8 or more and
at least 60 percent of them share one language. Transcripts that used this hint do not count toward it. You
should see `transcript_lang_from_history` in the message when the hint was used. A fixed
`transcription.language` turns the hint off.

## Troubleshooting

- `ffmpeg not found`. Install ffmpeg and put it on `PATH`, or set `WA_MCP_FFMPEG`.
- `faster-whisper is not installed`. Run the install script again, or the `uv sync --all-packages --inexact
  --extra faster-whisper` command. A plain `uv sync` removes the backend.
- PowerShell refuses to run the script. Start it with
  `powershell -ExecutionPolicy Bypass -File scripts\install_whisper.ps1 -Backend faster-whisper -Model small -Download`,
  or run the `uv sync` command above.
- `cannot load model`. The model name is wrong or the download failed. Check the name in
  `transcription.model` and your network.
- Short voice notes come out in the wrong language. Set `transcription.language` to the language you use
  most, or let the chat's history decide after a few clips.

## Next steps

- [Media and screenshots](06-media-and-screenshots.md): text from photos and screenshots.
- [Voice transcription](../guides/transcription.md): all keys, and the `http` and `command` backends in detail.
- [My day and tasks](04-my-day-and-tasks.md): transcripts count for task detection.
