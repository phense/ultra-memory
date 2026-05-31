"""Headless end-to-end smoke test for the knowledge MCP (spec §13: "a headless smoke
test asserts a knowledge tool is invocable", mirroring the IBKR MCP retest).

Launches the real stdio server as a subprocess, drives it via the mcp client
(initialize → list_tools → call_tool), and asserts the type-allowlist holds
end-to-end: an untrusted 'subagent' caller gets the project fact but NOT the user
memory. Skipped if the optional `mcp`/`fastembed` deps (the warm-venv extras) are
absent. Marked slow — the first run loads the embedding model.
"""
import json
import os
import sys

import pytest

pytest.importorskip("mcp")
pytest.importorskip("fastembed")

import anyio  # noqa: E402

from ultra_memory import memory_lib  # noqa: E402


def _seed(db):
    conn = memory_lib.open_memory_db(db)
    memory_lib.save_memory(conn, id="proj-fact", type="project",
                           title="alpha project fact", body="alpha is the project",
                           ts="2026-05-01T00:00:00")
    memory_lib.save_memory(conn, id="user-secret", type="user",
                           title="peter preference", body="a personal preference",
                           ts="2026-05-01T00:00:00")
    conn.close()


@pytest.mark.slow
def test_knowledge_mcp_end_to_end_type_scoped(tmp_path):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    db = tmp_path / "memory.db"
    _seed(db)
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "ultra_memory.knowledge_mcp"],
        env={**os.environ, "ULTRA_MEMORY_DB": str(db),
             "ULTRA_MEMORY_CALLER_CLASS": "subagent"},
    )

    async def _drive():
        with anyio.fail_after(240):  # bound the model-load + handshake
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    tools = await session.list_tools()
                    names = [t.name for t in tools.tools]
                    assert "knowledge_query" in names, names
                    res = await session.call_tool("knowledge_query",
                                                   {"query": "alpha", "top_k": 5})
                    payload = json.loads(res.content[0].text)
                    ids = {r["id"] for r in payload["results"]}
                    assert "proj-fact" in ids, payload
                    assert "user-secret" not in ids, "type-allowlist breached over stdio!"

    anyio.run(_drive)
