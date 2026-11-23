# Contributing

Thanks for helping. This file covers the development setup, the checks that CI runs and the commit
style.

## Layout

| Path | Contents |
| --- | --- |
| `bridge/` | Go bridge `wa-bridge` (module `github.com/tsahospital-beep/wa-mcp/bridge`) |
| `sdk/python/` | Python package `wa_mcp` (published name `wa-mcp-sdk`) |
| `server/` | MCP server, package `wa_mcp_server`, console script `wa-mcp` |
| `docs/` | Documentation. `docs/reference/tools.md` is generated |
| `scripts/` | Install scripts and documentation checks |

The repository is a uv workspace. The root `pyproject.toml` ties `sdk/python` and `server` together.

## Setup

Install [Go](https://go.dev/dl/) 1.26 or newer and [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/tsahospital-beep/wa-mcp.git
cd wa-mcp
uv sync                                    # Python packages, including ruff, mypy and pytest
go build -C bridge -o ../wa-bridge ./cmd/wa-bridge    # use ../wa-bridge.exe on Windows
```

The first `go build` downloads the Go modules. `uv sync` creates `.venv`. Note that a plain `uv sync` removes
optional extras that you added earlier. Add `--inexact` to keep them.

## Checks

Run these before you open a pull request. CI runs the same commands.

Go, from `bridge/`:

```bash
gofmt -l .          # prints the files that need formatting; the output must be empty
go vet ./...
go test ./...
```

Python, from the repository root:

```bash
uv run ruff format --check .
uv run ruff check .
uv run mypy --strict
uv run pytest
```

Documentation, from the repository root:

```bash
uv run python scripts/check_links.py              # relative links and images in all Markdown files
uv run python scripts/check_openapi_sync.py       # docs copy equals the embedded spec
uv run python scripts/check_config_example.py     # config.example.json matches the Go and Python defaults (needs Go)
uv run python scripts/gen_tool_reference.py --check
```

## Changing the API, tools or configuration

- The bridge embeds `bridge/internal/api/openapi.yaml`. After editing it, copy it to
  `docs/reference/openapi.yaml`.
- After adding or changing an MCP tool, run `uv run python scripts/gen_tool_reference.py --write`. New
  tools also belong in `EXPECTED_TOOLS` or `INSIGHT_TOOLS` in `server/tests/test_stdio_tools.py` and in
  the `GROUPS` table of the generator script.
- A new configuration key needs a default in `bridge/internal/config/config.go` or
  `sdk/python/wa_mcp/config.py` (or in the insight settings), an entry in `config.example.json` and a row in
  `docs/guides/configuration.md`. `scripts/check_config_example.py` runs the real Go defaults through
  `go run -overlay`, so it needs Go and does not write into the repository.
- The MCP server speaks over stdout. Do not print to stdout in server or SDK code. Ruff reports `print`
  for that reason. Log to stderr.

## Tests and fixtures

- Python tests build a synthetic store with `sdk/python/tests/synth.py`, which applies the Go
  migrations from `bridge/internal/store/store.go`.
- Fixtures contain no personal data. Use the fictional names Alice, Bob and Carol, the group "Book
  club" and numbers in the form `1555010xxxx`. Do not commit real chats, phone numbers, JIDs, tokens,
  session files or screenshots of real conversations.
- New behavior needs a test. Bug fixes need a test that fails without the fix.

## Commit style

- One logical change per commit.
- Subject in the imperative mood, up to 72 characters, with an optional area prefix:
  `bridge: refuse sticker paths outside allowed_media_dirs`, `sdk: ...`, `server: ...`, `docs: ...`,
  `ci: ...`.
- Use the body to explain why the change is needed, not what the diff already shows.

## Pull requests

Describe the change, how you tested it and whether it changes the API, a tool or a configuration key.
Fill in the pull request template. Keep pull requests small enough to review in one sitting.
