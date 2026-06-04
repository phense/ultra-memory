# Changelog

All notable changes to ultra-memory are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); the project uses
[semantic versioning](https://semver.org/). It is **pre-1.0 and pre-public** — the
entries below summarize milestones rather than every commit, and interfaces may still
change between `0.0.x` releases.

## [Unreleased]

### Added
- Public-release scaffolding: this `CHANGELOG.md`, a root `CONTRIBUTING.md`,
  `THIRD-PARTY-LICENSES.md`, and a GitHub Actions test workflow.
- `test_no_hardcoded_paths` extended to the entire tracked markdown publish surface
  (via `git ls-files`), not just the Python package.

### Changed
- Manifests carry author email + `repository`/`homepage` URLs; dropped the stale
  "(local plugin)" descriptor.
- The numbered engineering backlog is no longer tracked in the repo (kept as the
  maintainer's private working doc); the public roadmap is the README *Status* section.

### Fixed
- Removed consumer-specific PII (maintainer email, local home paths, consumer script
  names) from the documentation publish surface; relocated two consumer-flavored design
  docs out of the content-free plugin.
- Corrected DB-path documentation that still described the retired project-local /
  `~/.claude` fallback (the engine resolves the fixed `~/.ultra-memory` store).

## [0.0.3] — 2026-06-04

### Changed
- Renamed the global store path `~/.ultra-knowledge` → `~/.ultra-memory` to match the
  plugin name (a backward-compatibility symlink is left in place).
- Marketing-focused README rewrite with an honest competitor-comparison table.

### Added
- Pluggable maintenance **notifier seam** (`[maintenance] notifier = "module:function"`,
  no-op default, fail-open) so a consumer can wire alerting on maintenance-run errors;
  the plugin ships no transport.

## [0.0.2]

### Added
- Extensible **wiki write-gateway** (`WikiGateway` base class + 6 override hooks + a
  `scaffold` generator + the `using-wiki-gateway` skill) so a consumer can bring its own
  wiki layout.
- Cold-start session-cache backfill onboarding via `/ultra-memory:memory-setup`
  (offer-don't-auto-run, gated on a consumer-declared runner).

### Fixed
- `knowledge` MCP no longer crashes on a fresh install (creates its DB directory before
  connecting).
- Version consistency across `pyproject.toml` and the plugin manifests.

## [0.0.1]

### Added
- Initial engine: two-store memory — a SQLite-canonical **session memory** plus a
  git-tracked Markdown **knowledge wiki** — blended into one ranked `unified_recall`
  over a typed-edge graph.
- Single audited write gateway with twice-applied secret stripping (write + export).
- Recall **privilege boundary** (type + topic axes; fail-closed) and the read-only
  `knowledge` MCP.
- SessionStart rehydration + Stop checkpoint hooks (fail-open) and the throttled,
  pure-Python maintenance pipeline (prune + export + wiki sync).
- The four-beat self-learning loop (consolidate · attribute · self-correct · synthesize)
  behind a code-enforced safety wall.
- **OAuth-only** LLM invariant: refuses to run with an `ANTHROPIC_API_KEY` present.

[Unreleased]: https://github.com/phense/ultra-memory/compare/v0.0.3...HEAD
[0.0.3]: https://github.com/phense/ultra-memory/releases/tag/v0.0.3
