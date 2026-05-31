"""Publish-readiness invariant (spec §3.1): the shipped package is project-AGNOSTIC.

No absolute home path may be hardcoded in `ultra_memory/` source — every path is
config/env-driven (e.g. ULTRA_MEMORY_DB). This guard fails the suite if a
`/Users/<name>` or `/home/<name>` literal ever sneaks into the package, so a leak
is caught long before the (opt-in, publish-last) open-sourcing step.
"""
import pathlib
import re

_HOME_PATH = re.compile(r"/(?:Users|home)/[A-Za-z0-9._-]+")


def test_no_hardcoded_home_paths_in_shipped_code():
    pkg = pathlib.Path(__file__).resolve().parent.parent / "ultra_memory"
    offenders = []
    for p in sorted(pkg.rglob("*.py")):
        if "__pycache__" in p.parts:
            continue
        for lineno, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
            if _HOME_PATH.search(line):
                offenders.append(f"{p.relative_to(pkg.parent)}:{lineno}: {line.strip()}")
    assert not offenders, (
        "Hardcoded home path(s) in shipped ultra_memory/ code — violates the "
        "project-agnostic / publish-ready invariant (§3.1). Move the path to config/env:\n"
        + "\n".join(offenders)
    )


def test_no_hardcoded_home_paths_in_plugin_wiring():
    """The plugin's non-Python wiring (hooks wrapper, command docs, plugin-root
    .mcp.json) is also publish-surface — it must stay project-agnostic."""
    root = pathlib.Path(__file__).resolve().parent.parent
    targets = [
        root / "hooks" / "um-hook.cmd",
        root / ".mcp.json",
        *sorted((root / "commands").glob("*.md")),
    ]
    offenders = []
    for p in targets:
        if not p.exists():
            continue
        for lineno, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
            if _HOME_PATH.search(line):
                offenders.append(f"{p.relative_to(root)}:{lineno}: {line.strip()}")
    assert not offenders, (
        "Hardcoded home path(s) in plugin wiring — violates project-agnostic invariant:\n"
        + "\n".join(offenders)
    )
