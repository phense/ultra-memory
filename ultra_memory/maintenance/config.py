"""The project-agnostic maintenance config seam.

A consuming project declares its maintenance config in `<project>/.ultra-memory/
config.toml`; ULTRA_MEMORY_* env vars override individual fields (the same seam
`maintain.py` uses for wiki roots). With NO config file and NO env, every field
falls back to a safe, project-agnostic default — a pure-memory deployment runs the
light beats and skips anything that needs project content (wiki, probe corpus).

Example `.ultra-memory/config.toml`:

    [maintenance]
    briefings_dir = "briefings"        # audit/digest dir, relative to the project
    probe_corpus  = "tests/fixtures/skill_trigger_probes.json"
    wiki_gateway  = "scripts/wiki_lib.py"   # consumer wiki write gateway (None → no wiki)
    topics        = ["trading"]
    model         = "claude-sonnet-4-6"

    [maintenance.beats]                # the autonomous posture: default ON, wall-governed
    consolidate = true
    aggressive  = true
    synthesize  = true

    [maintenance.cadence_hours]        # per-beat throttle (the session-driven clock)
    consolidate = 168                  # weekly
    aggressive  = 720                  # monthly
    synthesize  = 720
"""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from ultra_memory.knowledge_mcp import db_path_from_env

# Default cadences (hours): the heavy LLM beats are conservative; the light beats
# run effectively every session (throttled by maintain.py's own 20h clock). The
# `learnings` projection-regen beat is no-LLM (Tier-1) and weekly — it rebuilds the
# per-skill Learnings.md views + refreshes the Model B gen-skill managed blocks.
_DEFAULT_CADENCE = {"session_ingest": 24, "consolidate": 168, "aggressive": 720,
                    "synthesize": 720, "learnings": 168, "wiki_maintenance": 24}
# The autonomous posture (north-star decision 1): beats default ON, governed by the
# wall (decision 2). A consumer can still gate any beat off in its config. The
# `session_ingest` beat is additionally gated by SESSION_INGEST_ENABLE in its own
# code (default OFF) — the ships-active posture flip is the consumer's explicit step.
_DEFAULT_BEATS = {"session_ingest": True, "consolidate": True, "aggressive": True,
                  "synthesize": True, "learnings": True, "wiki_maintenance": True}
_DEFAULT_MODEL = "claude-sonnet-4-6"

_WIKI_ROOTS_ENV = "ULTRA_MEMORY_WIKI_ROOTS"


@dataclass
class MaintenanceConfig:
    """Resolved, project-agnostic maintenance config. All paths are absolute."""
    project_dir: Path
    db_path: Path
    export_dir: Path
    wiki_roots: list[Path] = field(default_factory=list)
    briefings_dir: Path | None = None          # None → no audit/digest writes
    probe_corpus: Path | None = None           # None → the skill-loop holds (no corpus)
    wiki_gateway: Path | None = None           # None → no wiki (wiki-write beats degrade)
    topics: list[str] = field(default_factory=list)
    model: str = _DEFAULT_MODEL
    beats: dict = field(default_factory=lambda: dict(_DEFAULT_BEATS))
    cadence_hours: dict = field(default_factory=lambda: dict(_DEFAULT_CADENCE))
    # The self-learning registry: (relative Learnings.md path, skill_tag) pairs the
    # `learnings` projection-regen beat rebuilds. CONSUMER-declared (project-agnostic
    # default empty); the gen-* glob supplies generated skills on top of this.
    self_learning_files: list = field(default_factory=list)
    # The wiki-maintenance schema seam: the consumer's `[maintenance.wiki]` overrides,
    # fed to wiki_maintenance.load_wiki_schema. Empty → the reference wiki schema.
    wiki_schema: dict = field(default_factory=dict)
    # The graph extractor command template (consumer-specific tool that builds the
    # graph.sqlite the graph detector queries). Empty → no graph rebuild (query the
    # existing graph if present). `{wiki_root}` placeholders are substituted by the beat.
    wiki_graph_extractor: list = field(default_factory=list)

    def beat_enabled(self, name: str) -> bool:
        return bool(self.beats.get(name, _DEFAULT_BEATS.get(name, False)))

    def cadence_for(self, name: str) -> int:
        return int(self.cadence_hours.get(name, _DEFAULT_CADENCE.get(name, 720)))


def _resolve_wiki_roots(env) -> list[Path]:
    raw = env.get(_WIKI_ROOTS_ENV, "")
    if not raw or not raw.strip():
        return []
    parts: list[str] = []
    for chunk in raw.split(os.pathsep):
        parts.extend(chunk.split(","))
    return [Path(p.strip()) for p in parts if p.strip()]


def _abs(project_dir: Path, value) -> Path | None:
    if value in (None, ""):
        return None
    p = Path(str(value)).expanduser()
    return p if p.is_absolute() else (project_dir / p)


def _parse_self_learning_files(raw_value) -> list:
    """Coerce the TOML `self_learning_files` array-of-arrays into a list of
    (path, tag) tuples. Fail-open per entry: a non-[str, str] pair is dropped (a
    malformed registry line must never crash the whole config load)."""
    out: list = []
    if not isinstance(raw_value, list):
        return out
    for entry in raw_value:
        if (isinstance(entry, (list, tuple)) and len(entry) == 2
                and all(isinstance(x, str) and x.strip() for x in entry)):
            out.append((entry[0], entry[1]))
    return out


def load_config(project_dir=None, env=None) -> MaintenanceConfig:
    """Resolve the maintenance config from `<project_dir>/.ultra-memory/config.toml`
    (if present) + ULTRA_MEMORY_* env overrides + safe defaults. NEVER raises on a
    missing/malformed file — a config error degrades to defaults (fail-open) so a
    fresh install with no config still runs the safe beats."""
    env = os.environ if env is None else env
    project_dir = Path(project_dir or env.get("CLAUDE_PROJECT_DIR") or os.getcwd()).resolve()

    raw: dict = {}
    cfg_path = project_dir / ".ultra-memory" / "config.toml"
    try:
        if cfg_path.is_file():
            with cfg_path.open("rb") as fh:
                raw = (tomllib.load(fh) or {}).get("maintenance", {}) or {}
    except Exception:
        raw = {}  # fail-open to defaults

    db_path = db_path_from_env(env)
    export_dir = Path(env.get("ULTRA_MEMORY_EXPORT_DIR") or (db_path.parent / "memory_export"))

    # env overrides win over the file; the file wins over the hard default.
    briefings = env.get("ULTRA_MEMORY_BRIEFINGS_DIR") or raw.get("briefings_dir")
    corpus = env.get("ULTRA_MEMORY_PROBE_CORPUS") or raw.get("probe_corpus")
    gateway = env.get("ULTRA_MEMORY_WIKI_GATEWAY") or raw.get("wiki_gateway")
    model = env.get("ULTRA_MEMORY_MODEL") or raw.get("model") or _DEFAULT_MODEL
    topics = raw.get("topics") if isinstance(raw.get("topics"), list) else []

    wiki_schema = raw.get("wiki") if isinstance(raw.get("wiki"), dict) else {}
    graph_extractor = raw.get("wiki_graph_extractor")
    graph_extractor = [str(x) for x in graph_extractor] if isinstance(graph_extractor, list) else []

    beats = dict(_DEFAULT_BEATS)
    if isinstance(raw.get("beats"), dict):
        beats.update({k: bool(v) for k, v in raw["beats"].items()})
    cadence = dict(_DEFAULT_CADENCE)
    if isinstance(raw.get("cadence_hours"), dict):
        for k, v in raw["cadence_hours"].items():
            try:
                cadence[k] = int(v)
            except (TypeError, ValueError):
                pass

    return MaintenanceConfig(
        project_dir=project_dir,
        db_path=db_path,
        export_dir=export_dir,
        wiki_roots=_resolve_wiki_roots(env),
        briefings_dir=_abs(project_dir, briefings),
        probe_corpus=_abs(project_dir, corpus),
        wiki_gateway=_abs(project_dir, gateway),
        topics=[str(t) for t in topics],
        model=str(model),
        beats=beats,
        cadence_hours=cadence,
        self_learning_files=_parse_self_learning_files(raw.get("self_learning_files")),
        wiki_schema=wiki_schema,
        wiki_graph_extractor=graph_extractor,
    )
