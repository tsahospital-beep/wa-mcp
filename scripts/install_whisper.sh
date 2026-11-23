#!/usr/bin/env sh
# Install a speech-to-text backend for wa-mcp from this repository (uv workspace).
# Usage: scripts/install_whisper.sh [--backend faster-whisper|openai-whisper|whisper.cpp] [--model NAME] [--download]
# --download fetches the model now; without it the model is fetched on first use.
set -eu

backend="faster-whisper"
model="large-v3"
download=0

while [ $# -gt 0 ]; do
  case "$1" in
    --backend) backend="$2"; shift 2 ;;
    --model) model="$2"; shift 2 ;;
    --download) download=1; shift ;;
    -h|--help) sed -n '2,4p' "$0"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

root="$(cd "$(dirname "$0")/.." && pwd)"
cd "$root"

if ! command -v uv >/dev/null 2>&1; then
  echo "uv is required: https://docs.astral.sh/uv/getting-started/installation/" >&2
  exit 1
fi
if ! command -v ffmpeg >/dev/null 2>&1; then
  echo "note: ffmpeg is not on PATH; install it (for example: apt install ffmpeg, brew install ffmpeg)" >&2
fi

case "$backend" in
  faster-whisper)
    uv sync --all-packages --inexact --extra faster-whisper
    if [ "$download" -eq 1 ]; then
      uv run python -c "import sys; from faster_whisper import WhisperModel; WhisperModel(sys.argv[1], device='cpu', compute_type='int8')" "$model"
    fi
    ;;
  openai-whisper)
    uv sync --all-packages --inexact --extra openai-whisper
    if [ "$download" -eq 1 ]; then
      uv run python -c "import sys, whisper; whisper.load_model(sys.argv[1], device='cpu')" "$model"
    fi
    ;;
  whisper.cpp)
    echo "whisper.cpp is a separate program. Build or download whisper-cli from"
    echo "https://github.com/ggml-org/whisper.cpp, download a ggml model (for example ggml-$model.bin),"
    echo "then set transcription.backend=\"whisper.cpp\" and transcription.whisper_cpp.binary/model in config.json."
    exit 0
    ;;
  *)
    echo "unknown backend: $backend" >&2
    exit 2
    ;;
esac

echo "done: set transcription.backend to \"$backend\" and transcription.model to \"$model\" in config.json"
