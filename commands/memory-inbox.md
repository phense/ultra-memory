---
description: Apply and clear the memory correction inbox — a watched file where Peter types pin/unpin/verify directives between sessions. Run to ingest those deltas into the DB.
---
Drain the correction inbox (directives: `pin <id>`, `unpin <id>`, `verify <id>`; free-text is preserved for manual review, never auto-applied):

```bash
uv run --directory "${CLAUDE_PLUGIN_ROOT}" python -m ultra_memory.memory_cli inbox
```

(Needs `ULTRA_MEMORY_DB` set; the inbox defaults to `<db-dir>/memory_inbox.md`, override with `--path` or `$ULTRA_MEMORY_INBOX`.) Report the JSON summary — `applied` / `notes` / `errors`. Surface any `errors` (e.g. an unknown id) to the user, and mention any preserved `notes` that still need manual handling. Free-text directives that aren't recognised commands are preserved under an "Unprocessed" section of the inbox file, not discarded.
