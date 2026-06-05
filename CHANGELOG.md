# Changelog

All notable changes to ultra-memory are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); the project uses
[semantic versioning](https://semver.org/). It is **pre-1.0 and pre-public** — the
entries below summarize milestones rather than every commit, and interfaces may still
change between `0.0.x` releases.

## [Unreleased]

## [0.0.5] — 2026-06-06

A QA + hardening pass: full functional walkthrough of every element, then an
exhaustive parallel audit (12 dimensions, per-finding adversarial verification). The
engine and all safety/privilege walls held; the fixes below close two real code
defects and align the docs with reality.

### Security
- **Secret stripper now catches the AWS access-key family.** The `keyword=value`
  redactor required a credential keyword *immediately* before the delimiter, so the
  compound/interior forms `aws_secret_access_key=…`, `aws_access_key_id=…`, and bare
  `access_key=…` slipped through — persisting in cleartext into both `memory.db` and
  the git-committed export. The keyword vocabulary now lists the compound access-key
  forms (plus `private_key` / `credentials`); the no-over-redaction locking suite is
  extended to prove prose is untouched. (Audit D4-1.)

### Added
- A consolidated user + developer handbook at `docs/` (foundations → use → configure
  → extend → develop + a design-notes appendix); the old `docs/{user,developer,reference}`
  split-by-audience pages were folded into it.
- **`graduate_enable` userConfig toggle.** The atomic-graduation beat is now
  disable-able from the `/plugin` UI; the wrapper already honored the option but it was
  undeclared, so the toggle was dead. A reconciliation test now fails if any
  `CLAUDE_PLUGIN_OPTION_*` the wrapper reads lacks a userConfig key (or any declared
  `*_enable` toggle isn't bridged into the wrapper). (Audit D5-1 / D5-3.)

### Fixed
- **`WikiGateway` embed loader pinned to the persistent model cache.** `_get_embed_model`
  loaded fastembed with its default `$TMPDIR/fastembed_cache` rather than
  `persistent_cache_dir()` (which `retrieval_core.default_embedder` already used). On
  macOS the OS purges that temp dir, dangling the ONNX blob so every gateway embed
  (dedup / overlap) died with onnxruntime `NoSuchFile` — the same failure that had
  already bitten the knowledge MCP. The gateway now uses the persistent cache, so the
  model survives temp-dir reaping for all `WikiGateway` consumers.
- **Wiki dedup-merge preserves source attribution on the canonical page.** A merged
  duplicate's `Sources` lived only inside the redirect stub, which `recall()`/wiki_query
  drop — so the attribution fell off the warm retrieval surface even though the bytes
  survived on disk. The dedup apply now threads the canonical's path and concatenates
  the dup's not-already-present source line(s) onto the canonical (deduped, idempotent;
  an LLM-emitted stub with only a slug keeps the prior stub-only behavior). (Audit D10-1.)
- **`detect_graph` synthesis-candidate path is topic-qualified.** The same-source
  cluster detector built a topic-less `<wiki>/synthesis/<slug>.md`, so a verbatim-echo
  `create-page` could file the page under a non-existent `synthesis` topic. The topic
  is now derived from the cluster's own source/member node paths. (Audit D10-2.)

### Changed
- **`unified_recall` embeds the query once.** The memory, knowledge, and `## Signal`
  backends each re-embedded the same query string (up to three identical forward passes
  per recall); the vector is now computed once and threaded into all three. Ranking is
  byte-identical (the parity fences stay green); per-prompt recall-hook latency drops.
  (Audit D2-4.)
- **Docs aligned with reality (FEATURES / README / memory-setup).** Honest,
  showcase-positive framing of outcome-attribution maturity (built + armed, neutral
  weight until outcome signals flow), the `validated_as`→memory graduation link, the
  batched maintenance-call count, and the *armed* (not dry-run-first) self-learning
  posture; the now-real atomic-graduation toggle is surfaced.

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
