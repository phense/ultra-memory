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
# DERIVES the fixed global ~/.ultra-memory/memory.db (the old project-local / ~/.claude
# fallback was retired 2026-06-01). Never cwd. The whole plugin stays zero-config-consistent.
export ULTRA_MEMORY_DB="${CLAUDE_PLUGIN_OPTION_DATA_DB_PATH:-${ULTRA_MEMORY_DB:-}}"

# Caller privilege class (fail-closed at the engine; default subagent here).
export ULTRA_MEMORY_CALLER_CLASS="${CLAUDE_PLUGIN_OPTION_CALLER_CLASS:-${ULTRA_MEMORY_CALLER_CLASS:-subagent}}"

# Rehydration gist budget.
export ULTRA_MEMORY_REHYDRATE_BUDGET="${CLAUDE_PLUGIN_OPTION_REHYDRATE_BUDGET:-${ULTRA_MEMORY_REHYDRATE_BUDGET:-2000}}"

# LIVE injection. The engine's rehydrate.main() defaults to shadow=1 (log-only);
# a plugin consumer wants the gist actually injected.
export ULTRA_MEMORY_SHADOW="${ULTRA_MEMORY_SHADOW:-0}"

# Self-learning opt-OUT toggles (userConfig → engine env). The two enable-flags pass
# through (default unset ⇒ engine default-on). The aggressive pair INVERTS: a UI value
# of 'off'/'0' must SET the kill switch (present ⇒ disabled); anything else leaves it unset.
export SESSION_INGEST_ENABLE="${CLAUDE_PLUGIN_OPTION_SESSION_INGEST_ENABLE:-${SESSION_INGEST_ENABLE:-}}"
export SP8_ATTRIBUTION_ENABLE="${CLAUDE_PLUGIN_OPTION_ATTRIBUTION_ENABLE:-${SP8_ATTRIBUTION_ENABLE:-}}"
_agg="${CLAUDE_PLUGIN_OPTION_AGGRESSIVE_ENABLE:-on}"
case "$_agg" in off|0|false|no) export SP7_AGGRESSIVE_DISABLE=1 ;; esac
_syn="${CLAUDE_PLUGIN_OPTION_SYNTHESIZE_ENABLE:-on}"
case "$_syn" in off|0|false|no) export SP10_SYNTHESIS_DISABLE=1 ;; esac
_grad="${CLAUDE_PLUGIN_OPTION_GRADUATE_ENABLE:-on}"
case "$_grad" in off|0|false|no) export ATOMIC_GRADUATE_DISABLE=1 ;; esac

# Interpreter: the venv /memory-setup builds under CLAUDE_PLUGIN_DATA (survives updates).
PY="${CLAUDE_PLUGIN_DATA:-}/venv/bin/python"
if [ ! -x "$PY" ]; then
  echo "ultra-memory: venv interpreter not found at $PY — run /memory-setup (skipping $HOOK)" >&2
  exit 0
fi

case "$HOOK" in
  rehydrate)  MOD="ultra_memory.hooks.rehydrate" ;;
  checkpoint) MOD="ultra_memory.hooks.checkpoint" ;;
  recall)     MOD="ultra_memory.hooks.recall_prompt" ;;
  maintain)   MOD="ultra_memory.maintain" ;;
  beats)      MOD="ultra_memory.maintenance" ;;
  *) exit 0 ;;
esac

if [ "$HOOK" = "maintain" ]; then
  "$PY" -c "import sys; from ultra_memory.maintain import main; sys.exit(main())" 2>/dev/null || exit 0
elif [ "$HOOK" = "beats" ]; then
  # The throttled heavy-beat dispatcher (consolidate / session_ingest / learnings /
  # aggressive / synthesize). Each beat is per-cadence throttled + fail-open; this
  # whole arm is async in hooks.json and never blocks a session.
  "$PY" -m ultra_memory.maintenance 2>/dev/null || exit 0
else
  "$PY" -c "
import sys
from $MOD import main
sys.exit(main(sys.stdin, sys.stdout))
" 2>/dev/null || exit 0
fi
