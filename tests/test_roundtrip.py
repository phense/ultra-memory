from ultra_memory import memory_export as mx
from ultra_memory import memory_import as mi
from ultra_memory import memory_lib


def _write(p, name, typ, desc, body, sid="s-1"):
    p.write_text(
        f"---\nname: {name}\ndescription: \"{desc}\"\nmetadata: \n"
        f"  node_type: memory\n  type: {typ}\n  originSessionId: {sid}\n---\n\n{body}\n")


def test_import_export_reimport_roundtrip(tmp_path):
    mem = tmp_path / "memory"
    mem.mkdir()
    _write(mem / "feedback_x.md", "feedback-x", "feedback", "one liner", "Body X.")
    _write(mem / "project_y.md", "project-y", "project", "two liner", "Body Y.")
    (mem / "MEMORY.md").write_text(
        "- [Feedback X](feedback_x.md) — hook X\n"
        "- [Project Y](project_y.md) — hook Y\n")

    db1 = memory_lib.open_memory_db(tmp_path / "a.db")
    mi.import_memory_dir(db1, mem, index_path=mem / "MEMORY.md", ts="2026-05-30T10:00:00")
    out = tmp_path / "export"
    assert mx.export_memory(db1, out, ts="2026-05-30T12:00:00") is True
    db1.close()

    # Re-import the regenerated views into a fresh DB.
    db2 = memory_lib.open_memory_db(tmp_path / "b.db")
    n = mi.import_memory_dir(db2, out / "views", index_path=out / "views" / "MEMORY.md",
                             ts="2026-05-30T13:00:00")
    assert n == 2

    def snap(conn):
        return {r["id"]: (r["type"], r["title"], r["description"], r["index_hook"],
                          r["file_slug"], r["sort_order"], (r["body"] or "").strip())
                for r in conn.execute(
                    "SELECT id, type, title, description, index_hook, file_slug, "
                    "sort_order, body FROM memories")}

    db1b = memory_lib.open_memory_db(tmp_path / "a.db")
    assert snap(db1b) == snap(db2)
    db1b.close()
    db2.close()
