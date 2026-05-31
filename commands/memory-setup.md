---
description: Bootstrap the ultra-memory plugin in this project — build the runtime venv, optionally import a legacy memory dir once, stamp the DB ready, and sanity-check. Idempotent; safe to re-run.
---
Set up the ultra-memory runtime. Idempotent — re-running only repairs what is missing.

**Prerequisite:** `uv` on PATH. The first run downloads the embedder model (~bge-small); this is cached afterward.

1. **Confirm the DB path.** It comes from the plugin's `data_db_path` userConfig (injected as `$CLAUDE_PLUGIN_OPTION_DATA_DB_PATH`). Confirm it is an absolute path; create the parent dir if needed.

2. **Build the venv under `$CLAUDE_PLUGIN_DATA/venv` (survives plugin updates) if missing:**
   ```bash
   if [ ! -x "$CLAUDE_PLUGIN_DATA/venv/bin/python" ]; then
     uv venv "$CLAUDE_PLUGIN_DATA/venv" --python 3.13
     uv pip install --python "$CLAUDE_PLUGIN_DATA/venv/bin/python" \
       --directory "$CLAUDE_PLUGIN_ROOT" -e ".[retrieval,mcp]"
   fi
   ```

3. **Optional one-time legacy import** (only if the consumer has a legacy harness memory dir AND it has not been imported). Skip entirely for greenfield consumers. Decide with the helper, then stamp:
   ```bash
   PY="$CLAUDE_PLUGIN_DATA/venv/bin/python"
   export ULTRA_MEMORY_DB="$CLAUDE_PLUGIN_OPTION_DATA_DB_PATH"
   # If a legacy dir is configured, import it (idempotent per-id upsert), then stamp.
   # Greenfield: skip the import; just stamp so db_ready() turns true.
   "$PY" -c "
import os
from ultra_memory import setup, memory_import, memory_lib
db = os.environ['ULTRA_MEMORY_DB']
legacy = os.environ.get('ULTRA_MEMORY_HARNESS_DIR')
if setup.should_import_legacy(db) and legacy and os.path.isdir(legacy):
    conn = memory_lib.open_memory_db(db)
    import datetime
    ts = datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    n = memory_import.import_memory_dir(conn, legacy, index_path=os.path.join(legacy,'MEMORY.md'), ts=ts)
    conn.close()
    print(f'imported {n} legacy memories')
print('stamped' if setup.mark_import_complete(db) else 'already stamped')
"
   ```

4. **Sanity check:** the MCP module imports, the embedder loads, a trial recall returns:
   ```bash
   "$CLAUDE_PLUGIN_DATA/venv/bin/python" -c "import ultra_memory.knowledge_mcp; import fastembed; print('MCP + embedder OK')"
   uv run --directory "$CLAUDE_PLUGIN_ROOT" --python "$CLAUDE_PLUGIN_DATA/venv/bin/python" \
     python -m ultra_memory.memory_cli recall --query "setup smoke" --top-k 1 || true
   ```

Report what was built / imported / skipped. After this, restart Claude Code so the `knowledge` MCP registers.
