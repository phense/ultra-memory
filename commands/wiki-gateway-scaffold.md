---
description: Scaffold a ready-to-edit WikiGateway subclass — a class MyWikiGateway(WikiGateway) stub with all 6 override hooks, their contracts, and the .ultra-memory/config.toml snippet. Use when a project wants its own wiki write-gateway.
---
Scaffold a WikiGateway extension: **$ARGUMENTS** (e.g. a class name + topic + output path).

Run the generator (deterministic, no LLM):

```bash
uv run --directory "$CLAUDE_PLUGIN_ROOT" --python "$CLAUDE_PLUGIN_DATA/venv/bin/python" \
  python -m ultra_memory.wiki_gateway scaffold \
    --out "<scripts/your_wiki.py>" --class-name "<YourWikiGateway>" --topic "<yourtopic>"
```

Then: open the stub, override ONLY the hooks that differ from the defaults (delete the rest — they fall
through to the inherited base), and wire it in `<project>/.ultra-memory/config.toml`
(`wiki_gateway = "<module>:<Class>"`). The `using-wiki-gateway` skill explains each hook's contract.
