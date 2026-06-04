"""Tests for ultra_memory.maintenance.config — the project-agnostic config seam."""
from pathlib import Path

from ultra_memory.maintenance.config import MaintenanceConfig, load_config


def test_defaults_no_file_no_env(tmp_path):
    cfg = load_config(project_dir=tmp_path, env={})
    assert isinstance(cfg, MaintenanceConfig)
    assert cfg.db_path == Path.home() / ".ultra-memory" / "memory.db"
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


def test_wiki_gateway_module_class_spec_not_abs_mangled(tmp_path):
    """A `module:Class` gateway spec must survive load_config as a RAW STRING — never
    `_abs`-mangled into a bogus `<project_dir>/module:Class` path. (M1: the resolver
    needs the literal spec to build the --gateway-class prefix.)"""
    (tmp_path / ".ultra-memory").mkdir()
    (tmp_path / ".ultra-memory" / "config.toml").write_text(
        '[maintenance]\nwiki_gateway = "wiki_lib:TradingWikiGateway"\n')
    cfg = load_config(project_dir=tmp_path, env={})
    assert cfg.wiki_gateway == "wiki_lib:TradingWikiGateway"


def test_wiki_gateway_env_module_class_spec_not_abs_mangled(tmp_path):
    """The env override path also keeps a `module:Class` spec a raw string."""
    cfg = load_config(
        project_dir=tmp_path,
        env={"ULTRA_MEMORY_WIKI_GATEWAY": "mymod:MyGateway"})
    assert cfg.wiki_gateway == "mymod:MyGateway"


def test_wiki_gateway_real_path_still_abs_resolved(tmp_path):
    """A real filesystem path (no `module:Class` colon) is still resolved to absolute —
    the `module:Class` carve-out must not regress the path form."""
    (tmp_path / ".ultra-memory").mkdir()
    (tmp_path / ".ultra-memory" / "config.toml").write_text(
        '[maintenance]\nwiki_gateway = "scripts/wiki_lib.py"\n')
    cfg = load_config(project_dir=tmp_path, env={})
    assert cfg.wiki_gateway == tmp_path / "scripts" / "wiki_lib.py"


# --------------------------------------------------------------------------- #
# Model B / import_learnings migration — the self_learning_files registry seam
# + the learnings (projection-regen) beat defaults.
# --------------------------------------------------------------------------- #

def test_self_learning_files_default_empty(tmp_path):
    """Project-agnostic: with no config the registry is empty (a fresh install has
    no skills to project; the gen-* glob still supplies generated skills)."""
    cfg = load_config(project_dir=tmp_path, env={})
    assert cfg.self_learning_files == []


def test_self_learning_files_parsed_from_toml(tmp_path):
    (tmp_path / ".ultra-memory").mkdir()
    (tmp_path / ".ultra-memory" / "config.toml").write_text(
        '[maintenance]\n'
        'self_learning_files = [\n'
        '  [".claude/skills/backtest/Learnings.md", "backtest"],\n'
        '  [".claude/skills/risk-manager/Learnings.md", "risk-manager"],\n'
        ']\n'
    )
    cfg = load_config(project_dir=tmp_path, env={})
    assert cfg.self_learning_files == [
        (".claude/skills/backtest/Learnings.md", "backtest"),
        (".claude/skills/risk-manager/Learnings.md", "risk-manager"),
    ]


def test_self_learning_files_malformed_entries_skipped(tmp_path):
    """Fail-open: a non-[path, tag] entry is dropped, not a crash."""
    (tmp_path / ".ultra-memory").mkdir()
    (tmp_path / ".ultra-memory" / "config.toml").write_text(
        '[maintenance]\n'
        'self_learning_files = [\n'
        '  [".claude/skills/ok/Learnings.md", "ok"],\n'
        '  ["only-one-element"],\n'
        '  ["a", "b", "c"],\n'
        '  "not-a-list",\n'
        ']\n'
    )
    cfg = load_config(project_dir=tmp_path, env={})
    assert cfg.self_learning_files == [(".claude/skills/ok/Learnings.md", "ok")]


def test_learnings_beat_default_on_weekly(tmp_path):
    cfg = load_config(project_dir=tmp_path, env={})
    assert cfg.beat_enabled("learnings") is True
    assert cfg.cadence_for("learnings") == 168


def test_wiki_maintenance_beat_default_on_daily(tmp_path):
    cfg = load_config(project_dir=tmp_path, env={})
    assert cfg.beat_enabled("wiki_maintenance") is True
    assert cfg.cadence_for("wiki_maintenance") == 24


def test_wiki_schema_table_loaded(tmp_path):
    (tmp_path / ".ultra-memory").mkdir()
    (tmp_path / ".ultra-memory" / "config.toml").write_text(
        '[maintenance.wiki]\n'
        'page_soft_cap_lines = 250\n'
        'atomics_subdir = "atoms"\n'
        'index_types = ["theme-index", "master-index"]\n'
    )
    cfg = load_config(project_dir=tmp_path, env={})
    assert cfg.wiki_schema["page_soft_cap_lines"] == 250
    assert cfg.wiki_schema["atomics_subdir"] == "atoms"
    # the loaded dict feeds load_wiki_schema → a real WikiSchemaConfig
    from ultra_memory.wiki_maintenance.schema_config import load_wiki_schema
    schema = load_wiki_schema(cfg.wiki_schema)
    assert schema.page_soft_cap_lines == 250 and schema.atomics_subdir == "atoms"


def test_wiki_schema_default_empty(tmp_path):
    cfg = load_config(project_dir=tmp_path, env={})
    assert cfg.wiki_schema == {}


def test_wiki_graph_extractor_loaded(tmp_path):
    (tmp_path / ".ultra-memory").mkdir()
    (tmp_path / ".ultra-memory" / "config.toml").write_text(
        '[maintenance]\n'
        'wiki_graph_extractor = ["python3", "scripts/extract.py", "{wiki_root}"]\n'
    )
    cfg = load_config(project_dir=tmp_path, env={})
    assert cfg.wiki_graph_extractor == ["python3", "scripts/extract.py", "{wiki_root}"]


def test_wiki_graph_extractor_default_empty(tmp_path):
    cfg = load_config(project_dir=tmp_path, env={})
    assert cfg.wiki_graph_extractor == []


def test_wiki_linter_loaded(tmp_path):
    (tmp_path / ".ultra-memory").mkdir()
    (tmp_path / ".ultra-memory" / "config.toml").write_text(
        '[maintenance]\nwiki_linter = "my_adapter:lint_findings"\n')
    cfg = load_config(project_dir=tmp_path, env={})
    assert cfg.wiki_linter == "my_adapter:lint_findings"


def test_wiki_linter_default_empty(tmp_path):
    assert load_config(project_dir=tmp_path, env={}).wiki_linter == ""


def test_wiki_merge_decider_loaded(tmp_path):
    (tmp_path / ".ultra-memory").mkdir()
    (tmp_path / ".ultra-memory" / "config.toml").write_text(
        '[maintenance]\nwiki_merge_decider = "judge_adapter:merge_decider"\n')
    cfg = load_config(project_dir=tmp_path, env={})
    assert cfg.wiki_merge_decider == "judge_adapter:merge_decider"


def test_wiki_merge_decider_default_empty(tmp_path):
    assert load_config(project_dir=tmp_path, env={}).wiki_merge_decider == ""
