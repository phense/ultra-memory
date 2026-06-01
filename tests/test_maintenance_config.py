"""Tests for ultra_memory.maintenance.config — the project-agnostic config seam."""
from pathlib import Path

from ultra_memory.maintenance.config import MaintenanceConfig, load_config


def test_defaults_no_file_no_env(tmp_path):
    cfg = load_config(project_dir=tmp_path, env={})
    assert isinstance(cfg, MaintenanceConfig)
    assert cfg.db_path == Path.home() / ".ultra-knowledge" / "memory.db"
    assert cfg.wiki_roots == [] and cfg.briefings_dir is None and cfg.probe_corpus is None
    assert cfg.wiki_gateway is None            # no wiki by default → wiki-write beats degrade
    assert cfg.topics == [] and cfg.model == "claude-sonnet-4-6"
    # autonomous posture: beats default ON
    assert cfg.beat_enabled("consolidate") and cfg.beat_enabled("aggressive")
    assert cfg.beat_enabled("synthesize")
    assert cfg.cadence_for("consolidate") == 168 and cfg.cadence_for("aggressive") == 720


def test_toml_file_loaded_and_relative_paths_resolved(tmp_path):
    (tmp_path / ".ultra-memory").mkdir()
    (tmp_path / ".ultra-memory" / "config.toml").write_text(
        '[maintenance]\n'
        'briefings_dir = "briefings"\n'
        'probe_corpus = "tests/fixtures/probes.json"\n'
        'wiki_gateway = "scripts/wiki_lib.py"\n'
        'topics = ["trading", "programming"]\n'
        'model = "claude-opus-4-8"\n'
        '[maintenance.beats]\n'
        'aggressive = false\n'
        '[maintenance.cadence_hours]\n'
        'consolidate = 24\n'
    )
    cfg = load_config(project_dir=tmp_path, env={})
    assert cfg.briefings_dir == tmp_path / "briefings"           # relative → resolved
    assert cfg.probe_corpus == tmp_path / "tests" / "fixtures" / "probes.json"
    assert cfg.wiki_gateway == tmp_path / "scripts" / "wiki_lib.py"   # relative → resolved
    assert cfg.topics == ["trading", "programming"]
    assert cfg.model == "claude-opus-4-8"
    assert cfg.beat_enabled("aggressive") is False               # gated off by the consumer
    assert cfg.beat_enabled("consolidate") is True               # untouched default
    assert cfg.cadence_for("consolidate") == 24


def test_env_overrides_win(tmp_path):
    (tmp_path / ".ultra-memory").mkdir()
    (tmp_path / ".ultra-memory" / "config.toml").write_text(
        '[maintenance]\nmodel = "from-file"\nbriefings_dir = "from-file-dir"\n')
    env = {
        "ULTRA_MEMORY_DB": str(tmp_path / "custom.db"),
        "ULTRA_MEMORY_MODEL": "from-env",
        "ULTRA_MEMORY_BRIEFINGS_DIR": str(tmp_path / "envbriefings"),
        "ULTRA_MEMORY_WIKI_ROOTS": f"{tmp_path/'w1'}{__import__('os').pathsep}{tmp_path/'w2'}",
    }
    cfg = load_config(project_dir=tmp_path, env=env)
    assert cfg.db_path == tmp_path / "custom.db"
    assert cfg.model == "from-env"                               # env beats file
    assert cfg.briefings_dir == tmp_path / "envbriefings"
    assert cfg.wiki_roots == [tmp_path / "w1", tmp_path / "w2"]


def test_malformed_toml_fails_open_to_defaults(tmp_path):
    (tmp_path / ".ultra-memory").mkdir()
    (tmp_path / ".ultra-memory" / "config.toml").write_text("this is { not valid toml ===")
    cfg = load_config(project_dir=tmp_path, env={})
    assert cfg.model == "claude-sonnet-4-6" and cfg.beat_enabled("consolidate")


def test_absolute_paths_kept_absolute(tmp_path):
    (tmp_path / ".ultra-memory").mkdir()
    abs_corpus = tmp_path / "elsewhere" / "probes.json"
    (tmp_path / ".ultra-memory" / "config.toml").write_text(
        f'[maintenance]\nprobe_corpus = "{abs_corpus}"\n')
    cfg = load_config(project_dir=tmp_path, env={})
    assert cfg.probe_corpus == abs_corpus
