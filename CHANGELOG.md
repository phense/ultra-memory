# Changelog

All notable changes to ultra-memory are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); the project uses
[semantic versioning](https://semver.org/). It is **pre-1.0 and pre-public** — the
entries below summarize milestones rather than every commit, and interfaces may still
change between `0.0.x` releases.

## [Unreleased]

### Added
- A consolidated user + developer handbook at `docs/` (foundations → use → configure
  → extend → develop + a design-notes appendix); the old `docs/{user,developer,reference}`
  split-by-audience pages were folded into it.

### Fixed
- **`WikiGateway` embed loader pinned to the persistent model cache.** `_get_embed_model`
  loaded fastembed with its default `$TMPDIR/fastembed_cache` rather than
  `persistent_cache_dir()` (which `retrieval_core.default_embedder` already used). On
  macOS the OS purges that temp dir, dangling the ONNX blob so every gateway embed
  (dedup / overlap) died with onnxruntime `NoSuchFile` — the same failure that had
  already bitten the knowledge MCP. The gateway now uses the persistent cache, so the
  model survives temp-dir reaping for all `WikiGateway` consumers.

## [0.0.4] — 2026-06-04

### Changed
- **Self-learning loop autonomous by default (opt-out).** The four heavy beats
  (session capture, outcome attribution, self-correction, skill synthesis) now ship
  ON by default behind the existing code-enforced safety wall, instead of opt-in /
  default-OFF. A consumer disables any of them with the `*_enable` userConfig toggles
  or the matching opt-out env vars (`SESSION_INGEST_ENABLE=off`, …).
- **Session-lifecycle driver.** The heavy beats are driven from an async SessionStart
  hook (a throttled per-beat clock) rather than requiring an external scheduler;
  fail-open on a fresh / no-git / no-OAuth store.

### Added
- **userConfig opt-out toggles** for every self-learning beat, bridged from the
  install prompt into the engine's env (each defaults ON, set `off` to disable).
- **Optional OS-scheduler offer** at setup (pure launchd/systemd offer helpers) for a
  consumer that prefers a scheduler over the session-driven clock.
- **Honest privacy / cost disclosure** in the README: the loop runs on YOUR Claude
  login (no API key), reads only your local session transcript, and persists only the
  extracted, redacted knowledge.
- Bounded, env-overridable probe budget on the skill-eval gate for the unattended path.
- Public-release scaffolding: this `CHANGELOG.md`, a root `CONTRIBUTING.md`,
  `THIRD-PARTY-LICENSES.md`, and a GitHub Actions test workflow.
- `test_no_hardcoded_paths` extended to the entire tracked markdown publish surface
  (via `git ls-files`), not just the Python package.
- A version-consistency guard locking `pyproject.toml` ↔ `plugin.json` ↔
  `marketplace.json` to the same version string.

### Changed (publish hygiene)
- Manifests carry author email + `repository`/`homepage` URLs; dropped the stale
  "(local plugin)" descriptor.
- The numbered engineering backlog is no longer tracked in the repo (kept as the
  maintainer's private working doc); the public roadmap is the README *Status* section.

### Fixed
- Corrected a stale `maintenance/config.py` comment and `session_ingest.py` docstrings
  that still described the pre-autonomy `SESSION_INGEST_ENABLE` default-OFF posture.
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

[Unreleased]: https://github.com/phense/ultra-memory/compare/v0.0.4...HEAD
[0.0.4]: https://github.com/phense/ultra-memory/compare/v0.0.3...v0.0.4
[0.0.3]: https://github.com/phense/ultra-memory/releases/tag/v0.0.3
