# Publishing

1. Match the version in `server.json` and its OCI image tag to the version heading in [CHANGELOG.md](CHANGELOG.md). Set the same release version and PyPI pins in `mcpb/manifest.json` and `mcpb/pyproject.toml`.
2. Tag the release commit and push that tag. For version 0.1.0, use `git tag v0.1.0` and `git push origin v0.1.0`.
3. Wait for the Release workflow. It pushes the server and bridge images for Linux amd64 and arm64, then creates the GitHub release with the changelog entry and `wa-mcp.mcpb` attached. Both image tags omit the leading `v`.
4. Check that both packages are public on GHCR. The Registry reads the server image label `io.modelcontextprotocol.server.name`. Its value must match `name` in `server.json`.
5. From the tagged checkout, run `mcp-publisher login github` with an account that controls the namespace, then run `mcp-publisher publish`. See the [Registry OCI requirements](https://modelcontextprotocol.io/registry/package-types#dockeroci-images).

The Registry entry starts a stdio server against a linked bridge. It uses the key file setup in the [Docker guide](docs/guides/server.md). Supply the bridge container name, its data volume and its key file. The session volume belongs to the bridge. The package argument selects the stdio entry point. Compose uses HTTP.
