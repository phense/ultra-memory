import json
from ultra_memory import memory_cli, memory_lib


def test_save_roundtrips_a_new_fact(tmp_path):
    db = tmp_path / "m.db"
    memory_lib.open_memory_db(str(db)).close()  # migrate
    body = tmp_path / "body.txt"
    body.write_text("The operator prefers German for conversation.")
    rc = memory_cli.main(
        ["save", "--id", "user_lang_pref", "--type", "user",
         "--title", "User language preference", "--from-file", str(body)],
        db_path=str(db), ts="2026-05-31T00:00:00Z")
    assert rc == 0
    conn = memory_lib.open_memory_db(str(db))
    row = conn.execute(
        "SELECT type, title, body FROM memories WHERE id='user_lang_pref'").fetchone()
    conn.close()
    assert row["type"] == "user"
    assert row["title"] == "User language preference"
    assert "German" in row["body"]


def test_save_defaults_type_reference(tmp_path):
    db = tmp_path / "m.db"
    memory_lib.open_memory_db(str(db)).close()
    body = tmp_path / "b.txt"; body.write_text("a fact")
    memory_cli.main(["save", "--id", "f1", "--title", "Fact 1", "--from-file", str(body)],
                    db_path=str(db), ts="2026-05-31T00:00:00Z")
    conn = memory_lib.open_memory_db(str(db))
    t = conn.execute("SELECT type FROM memories WHERE id='f1'").fetchone()[0]
    conn.close()
    assert t == "reference"
