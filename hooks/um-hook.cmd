#!/usr/bin/env bash
# Path-free, fail-OPEN ultra-memory session-hook wrapper (plugin port of um_hook.sh).
# Usage: um-hook.cmd rehydrate|checkpoint|maintain   (hook payload on stdin)
# Resolves everything from env (P1-D1: the userConfig->CLAUDE_PLUGIN_OPTION_* bridge,
# with explicit ULTRA_MEMORY_* fallbacks). NEVER blocks a session: any error -> exit 0.
set -u

HOOK="${1:-}"

# DB path (zero-config): prefer the userConfig-injected option, then an already-set
# env. We do NOT hard-code a default here — when both are empty we leave ULTRA_MEMORY_DB
# unset so the engine resolver (knowledge_mcp.db_path_from_env, via hooks/common.resolve_db_path)
# DERIVES the same default the MCP uses: <CLAUDE_PROJECT_DIR>/data/memory.db, else
# ~/.claude/memory.db (never cwd). The whole plugin stays zero-config-consistent.
export ULTRA_MEMORY_DB="${CLAUDE_PLUGIN_OPTION_DATA_DB_PATH:-${ULTRA_MEMORY_DB:-}}"

# Caller privilege class (fail-closed at the engine; default subagent here).
export ULTRA_MEMORY_CALLER_CLASS="${CLAUDE_PLUGIN_OPTION_CALLER_CLASS:-${ULTRA_MEMORY_CALLER_CLASS:-subagent}}"

# Rehydration gist budget.
export ULTRA_MEMORY_REHYDRATE_BUDGET="${CLAUDE_PLUGIN_OPTION_REHYDRATE_BUDGET:-${ULTRA_MEMORY_REHYDRATE_BUDGET:-2000}}"

# LIVE injection. The engine's rehydrate.main() defaults to shadow=1 (log-only);
# a plugin consumer wants the gist actually injected.
export ULTRA_MEMORY_SHADOW="${ULTRA_MEMORY_SHADOW:-0}"

# Interpreter: the venv /memory-setup builds under CLAUDE_PLUGIN_DATA (survives updates).
PY="${CLAUDE_PLUGIN_DATA:-}/venv/bin/python"
if [ ! -x "$PY" ]; then
  echo "ultra-memory: venv interpreter not found at $PY — run /memory-setup (skipping $HOOK)" >&2
  exit 0
fi

case "$HOOK" in
  rehydrate)  MOD="ultra_memory.hooks.rehydrate" ;;
  checkpoint) MOD="ultra_memory.hooks.checkpoint" ;;
  maintain)   MOD="ultra_memory.maintain" ;;
  *) exit 0 ;;
esac

if [ "$HOOK" = "maintain" ]; then
  "$PY" -c "import sys; from ultra_memory.maintain import main; sys.exit(main())" 2>/dev/null || exit 0
else
  "$PY" -c "
import sys
from $MOD import main
sys.exit(main(sys.stdin, sys.stdout))
" 2>/dev/null || exit 0
fi
