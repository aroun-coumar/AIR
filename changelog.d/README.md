# News fragments

One file per user-visible change: `<id>.<type>.md`, where `<type>` is one of `breaking`, `feature`, `fix`, `deprecation`, `doc`, `misc` (see `../towncrier.toml`). At release, `towncrier build --version X.Y.Z` collects them into `CHANGELOG.md` and deletes them.

The current fragments describe the 2026-10-22 research-backlog implementation (ADRs 0012 to 0019).
