<#
.SYNOPSIS
Install a speech-to-text backend for wa-mcp from this repository (uv workspace).

.EXAMPLE
scripts\install_whisper.ps1 -Backend faster-whisper -Model large-v3 -Download
-Download fetches the model now; without it the model is fetched on first use.
#>
param(
    [ValidateSet("faster-whisper", "openai-whisper", "whisper.cpp")]
    [string]$Backend = "faster-whisper",
    [string]$Model = "large-v3",
    [switch]$Download
)
$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $PSScriptRoot)

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    throw "uv is required: https://docs.astral.sh/uv/getting-started/installation/"
}
if (-not (Get-Command ffmpeg -ErrorAction SilentlyContinue)) {
    Write-Warning "ffmpeg is not on PATH; install it, for example: winget install Gyan.FFmpeg"
}

function Sync-Extra([string]$Extra) {
    uv sync --all-packages --inexact --extra $Extra
    if ($LASTEXITCODE -ne 0) { throw "uv sync with the $Extra extra failed" }
}

switch ($Backend) {
    "faster-whisper" {
        Sync-Extra "faster-whisper"
        if ($Download) {
            uv run python -c "import sys; from faster_whisper import WhisperModel; WhisperModel(sys.argv[1], device='cpu', compute_type='int8')" $Model
        }
    }
    "openai-whisper" {
        Sync-Extra "openai-whisper"
        if ($Download) {
            uv run python -c "import sys, whisper; whisper.load_model(sys.argv[1], device='cpu')" $Model
        }
    }
    "whisper.cpp" {
        Write-Output "whisper.cpp is a separate program. Download whisper-cli from"
        Write-Output "https://github.com/ggml-org/whisper.cpp/releases and a ggml model (for example ggml-$Model.bin),"
        Write-Output "then set transcription.backend = `"whisper.cpp`" and transcription.whisper_cpp.binary/model in config.json."
        return
    }
}

Write-Output "done: set transcription.backend to `"$Backend`" and transcription.model to `"$Model`" in config.json"
