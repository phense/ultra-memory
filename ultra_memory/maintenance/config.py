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
# run effectively every session (throttled by maintain.py's own 20h clock).
_DEFAULT_CADENCE = {"consolidate": 168, "aggressive": 720, "synthesize": 720}
# The autonomous posture (north-star decision 1): beats default ON, governed by the
# wall (decision 2). A consumer can still gate any beat off in its config.
_DEFAULT_BEATS = {"consolidate": True, "aggressive": True, "synthesize": True}
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
    )
