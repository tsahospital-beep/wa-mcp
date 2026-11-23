## Summary

What changes and why.

## Type of change

- [ ] Bug fix
- [ ] New feature or tool
- [ ] Documentation
- [ ] Refactoring or tooling

## Compatibility

- [ ] This changes the HTTP API. I updated `bridge/internal/api/openapi.yaml` and copied it to `docs/reference/openapi.yaml`.
- [ ] This adds or changes an MCP tool. I ran `uv run python scripts/gen_tool_reference.py --write`.
- [ ] This adds or changes a configuration key. I updated `config.example.json` and `docs/guides/configuration.md`.
- [ ] None of the above.

## Checks

- [ ] `gofmt -l .` prints nothing, and `go vet ./...` and `go test ./...` pass (in `bridge/`)
- [ ] `uv run ruff format --check .` and `uv run ruff check .` pass
- [ ] `uv run mypy --strict` passes
- [ ] `uv run pytest` passes
- [ ] `uv run python scripts/check_links.py` reports 0 missing
- [ ] New behavior has tests

## Privacy

- [ ] The change contains no real phone numbers, messages, tokens, session files or personal paths.
