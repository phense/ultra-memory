---
description: Mark a memory as reconfirmed-true today (stamps last_verified). Use when you've checked that a stored fact still holds, so the staleness signal resets.
---
Verify (reconfirm) the memory: **$ARGUMENTS** (an id).

```bash
uv run --directory "$CLAUDE_PLUGIN_ROOT" --python "$CLAUDE_PLUGIN_DATA/venv/bin/python" \
  python -m ultra_memory.memory_cli verify --id "$ARGUMENTS"
```

(Needs `ULTRA_MEMORY_DB` set.) Confirm the result to the user. This resets the memory's age-based staleness penalty in recall ranking. Use this when a recalled fact shows `"stale": true` and you have confirmed it still holds.
