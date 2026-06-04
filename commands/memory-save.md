---
description: Persist a NEW durable memory — a fact about how the user wants to work, a feedback directive, project state, or a reference. The canonical new-fact write path (wraps memory_lib.save_memory: redacted, transactional, audited). Use whenever you need to remember something durably.
---
Save a new durable memory: **$ARGUMENTS**.

1. Choose a stable lowercase `id` (e.g. `feedback_email_routing`), a `type`
   (`user` | `feedback` | `project` | `reference`), and a short `title`.
   Compose the body and write it to a temp file (avoids shell-escaping prose):
   ```bash
   tmp=$(mktemp); cat > "$tmp" <<'BODY'
   <the memory body>
   BODY
   ```
2. Save it through the gateway (redacted + audited):
   ```bash
   uv run --directory "$CLAUDE_PLUGIN_ROOT" --python "$CLAUDE_PLUGIN_DATA/venv/bin/python" \
     python -m ultra_memory.memory_cli save \
     --id "<id>" --type "<type>" --title "<title>" --from-file "$tmp"
   rm -f "$tmp"
   ```

(Needs `ULTRA_MEMORY_DB` set — the plugin's hook/MCP env provides it.) This is the canonical way to create a new fact: never hand-write a `*.md` file and re-import. To make the fact always-in-context, `/ultra-memory:memory-pin` it afterward. Review the body for secrets before saving (they are auto-stripped, but check).
