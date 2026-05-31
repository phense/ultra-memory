---
description: Pin (or unpin) a memory by id. Pinned memories are injected into every SessionStart rehydration gist, so pin the rules/facts that must always be in context (e.g. hard rules). Append "unpin" to clear.
---
Pin/unpin the memory: **$ARGUMENTS** (an id, optionally followed by the word `unpin`).

Decide the flag from the argument, then run ONE of:

```bash
uv run --directory "${CLAUDE_PLUGIN_ROOT}" python -m ultra_memory.memory_cli pin --id "<id>"
uv run --directory "${CLAUDE_PLUGIN_ROOT}" python -m ultra_memory.memory_cli pin --id "<id>" --unpin
```

(Needs `ULTRA_MEMORY_DB` set.) Confirm the result line to the user. Pinning is the human-settable knob that controls SessionStart injection — use it deliberately.
