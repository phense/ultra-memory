---
description: Run ultra-memory maintenance now — prune old session_events (rolled into session summaries) and refresh the exported views. Manual/explicit only; routine maintenance runs throttled on SessionStart. Pure Python, no LLM.
---
Run memory maintenance once, now (bypasses the throttle).

```bash
ULTRA_MEMORY_DB="$CLAUDE_PLUGIN_OPTION_DATA_DB_PATH" \
ULTRA_MEMORY_MAINTAIN_FORCE=1 \
"$CLAUDE_PLUGIN_DATA/venv/bin/python" -m ultra_memory.maintain
```

This prunes `session_events` older than the retention window (rolling them into the per-session summary first, so nothing is lost) and rewrites the exported views. It uses NO LLM and NO OAuth token. Report the summary line it prints (`{pruned, exported, skipped}`).

Do not call this reflexively — the async SessionStart hook already runs it throttled (≤ once/~20h). Use this only when you explicitly need a fresh export or a forced prune.
