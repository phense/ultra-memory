---
description: Recall durable memories from the ultra-memory store matching a query — accumulated project knowledge, past decisions, user preferences. Use before answering anything that depends on remembered context.
---
Recall memories matching: **$ARGUMENTS**

Run (needs `ULTRA_MEMORY_DB` set and the `retrieval` extra installed for the embedder):

```bash
uv run --directory "${CLAUDE_PLUGIN_ROOT}" python -m ultra_memory.memory_cli recall --query "$ARGUMENTS" --top-k 5
```

Then summarize the returned JSON `results` for the user — each hit's `title` + `snippet` + `score`, cited by its `id`. Flag any hit marked `"stale": true` as possibly outdated. This is the human/orchestrator read path; subagents recall through the read-scoped `knowledge` MCP instead.
