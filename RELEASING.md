# Releasing

Releases are automatic. Do not create tags, GitHub releases, image tags or MCP Registry entries by hand.

1. In one pull request, bump the version in `pyproject.toml` and `server.json` (they must match) and add a non-empty `## [<version>]` section to `CHANGELOG.md`. `scripts/release_check.py` checks this in CI.
2. Merge it to `main`. After the other CI jobs pass, the `release` job tags the container image `ghcr.io/wavix/wavix-mcp-server:<version>`, publishes the version to the MCP Registry, pushes tag `v<version>` and creates the GitHub release from the `CHANGELOG.md` section.

A push to `main` without a version bump releases nothing. If the `release` job fails, fix the cause and re-run it.
