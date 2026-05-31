"""Tests for memory_cli — the CLI behind the /memory-* slash commands (spec §14).
Verbs: recall / pin / verify / edit / inbox. Deps are injectable so tests need no
real ULTRA_MEMORY_DB env and no fastembed."""
import json

from ultra_memory import memory_cli, memory_lib


def _seed(tmp_path, **kw):
    db = tmp_path / "m.db"
    conn = memory_lib.open_memory_db(db)
    kw.setdefault("type", "project")
    kw.setdefault("title", "t")
    kw.setdefault("body", "b")
    kw.setdefault("ts", "2026-05-01T00:00:00")
    memory_lib.save_memory(conn, **kw)
    conn.close()
    return db


def _reopen(db):
    return memory_lib.open_memory_db(db)


def test_cli_pin_and_unpin(tmp_path):
    db = _seed(tmp_path, id="x")
    assert memory_cli.main(["pin", "--id", "x"], db_path=str(db), ts="2026-05-02T00:00:00") == 0
    conn = _reopen(db)
    assert conn.execute("SELECT pinned FROM memories WHERE id='x'").fetchone()[0] == 1
    conn.close()
    assert memory_cli.main(["pin", "--id", "x", "--unpin"], db_path=str(db), ts="2026-05-03T00:00:00") == 0
    conn = _reopen(db)
    assert conn.execute("SELECT pinned FROM memories WHERE id='x'").fetchone()[0] == 0
    conn.close()


def test_cli_verify(tmp_path):
    db = _seed(tmp_path, id="x")
    assert memory_cli.main(["verify", "--id", "x"], db_path=str(db), ts="2026-05-05T00:00:00") == 0
    conn = _reopen(db)
    assert conn.execute("SELECT last_verified FROM memories WHERE id='x'").fetchone()[0] == "2026-05-05T00:00:00"
    conn.close()


def test_cli_edit_replaces_body_keeps_type_title(tmp_path):
    db = _seed(tmp_path, id="x", type="feedback", title="keep me", body="old body")
    f = tmp_path / "new.txt"
    f.write_text("the corrected body", encoding="utf-8")
    assert memory_cli.main(["edit", "--id", "x", "--from-file", str(f)],
                           db_path=str(db), ts="2026-05-06T00:00:00") == 0
    conn = _reopen(db)
    row = conn.execute("SELECT type, title, body FROM memories WHERE id='x'").fetchone()
    assert row[0] == "feedback" and row[1] == "keep me" and row[2] == "the corrected body"
    conn.close()


def test_cli_edit_missing_id_returns_nonzero(tmp_path):
    db = _seed(tmp_path, id="x")
    f = tmp_path / "n.txt"
    f.write_text("x", encoding="utf-8")
    assert memory_cli.main(["edit", "--id", "ghost", "--from-file", str(f)],
                           db_path=str(db), ts="2026-05-06T00:00:00") != 0


def test_cli_inbox(tmp_path):
    db = _seed(tmp_path, id="x")
    inbox = tmp_path / "inbox.md"
    inbox.write_text("pin x\n", encoding="utf-8")
    assert memory_cli.main(["inbox", "--path", str(inbox)], db_path=str(db), ts="2026-05-02T00:00:00") == 0
    conn = _reopen(db)
    assert conn.execute("SELECT pinned FROM memories WHERE id='x'").fetchone()[0] == 1
    conn.close()


def test_cli_recall_prints_json(tmp_path, capsys):
    db = _seed(tmp_path, id="alpha", title="alpha", body="alpha fact")
    fake = lambda texts: [[1.0, 0.0, 0.0] for _ in texts]
    rc = memory_cli.main(["recall", "--query", "alpha", "--top-k", "5"],
                         db_path=str(db), embedder=fake, dim=3, ts="2026-05-02T00:00:00")
    assert rc == 0
    out = capsys.readouterr().out
    payload = json.loads(out)
    assert any(r["id"] == "alpha" for r in payload["results"])
