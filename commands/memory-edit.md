---
description: Correct a memory's body by id, preserving its type/title/other fields. Use when a stored fact is wrong or outdated and needs rewriting.
---
Edit the memory: **$ARGUMENTS** (an id, plus the correction the user described).

1. Compose the corrected full body text and write it to a temp file (avoids shell-escaping prose):
   ```bash
   tmp=$(mktemp); cat > "$tmp" <<'BODY'
   <the corrected body>
   BODY
   ```
2. Apply it through the gateway (type/title/all other fields are preserved; only the body changes):
   ```bash
   uv run --directory "$CLAUDE_PLUGIN_ROOT" --python "$CLAUDE_PLUGIN_DATA/venv/bin/python" \
     python -m ultra_memory.memory_cli edit --id "<id>" --from-file "$tmp"
   rm -f "$tmp"
   ```

(Needs `ULTRA_MEMORY_DB` set.) Confirm the result. The write is redacted + audited like any other gateway write.
