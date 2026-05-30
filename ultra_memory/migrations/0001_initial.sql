CREATE TABLE IF NOT EXISTS memories (
  id            TEXT PRIMARY KEY,
  type          TEXT NOT NULL,
  title         TEXT NOT NULL,
  body          TEXT NOT NULL,
  created_at    TEXT, updated_at TEXT, origin_session_id TEXT,
  last_verified TEXT, valid_until TEXT,
  strength      REAL NOT NULL DEFAULT 1.0,
  access_count  INTEGER NOT NULL DEFAULT 0,
  last_accessed TEXT,
  status        TEXT NOT NULL DEFAULT 'active',
  supersedes    TEXT,
  pinned        INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS sessions (
  id           TEXT PRIMARY KEY,
  started_at   TEXT, ended_at TEXT,
  status       TEXT NOT NULL DEFAULT 'active',
  branch       TEXT, cwd TEXT, first_prompt TEXT, summary TEXT,
  commit_shas  TEXT
);

CREATE TABLE IF NOT EXISTS session_events (
  id          INTEGER PRIMARY KEY,
  session_id  TEXT NOT NULL REFERENCES sessions(id),
  ts          TEXT,
  kind        TEXT,
  title       TEXT, detail TEXT,
  files       TEXT, refs TEXT,
  resolved    INTEGER NOT NULL DEFAULT 0,
  event_key   TEXT UNIQUE
);

CREATE TABLE IF NOT EXISTS procedures (
  id TEXT PRIMARY KEY, name TEXT, steps TEXT, trigger TEXT,
  source_sessions TEXT, times_seen INTEGER NOT NULL DEFAULT 1,
  created_at TEXT, updated_at TEXT
);

CREATE TABLE IF NOT EXISTS links (
  src_kind TEXT, src_id TEXT, predicate TEXT, dst_kind TEXT, dst_id TEXT,
  evidence TEXT, confidence REAL, created_at TEXT
);

CREATE TABLE IF NOT EXISTS embeddings (
  target_kind TEXT, target_id TEXT, model_name TEXT, dim INTEGER,
  vector BLOB, content_sha256 TEXT,
  PRIMARY KEY (target_kind, target_id, model_name)
);

CREATE TABLE IF NOT EXISTS access_log (
  id INTEGER PRIMARY KEY, target_kind TEXT, target_id TEXT, ts TEXT, context TEXT
);

CREATE TABLE IF NOT EXISTS audit_log (
  id INTEGER PRIMARY KEY, ts TEXT, op TEXT,
  target_kind TEXT, target_id TEXT, reason TEXT, prior_state TEXT
);

CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);

CREATE INDEX IF NOT EXISTS idx_memories_status ON memories(status);
CREATE INDEX IF NOT EXISTS idx_session_events_session ON session_events(session_id);
CREATE INDEX IF NOT EXISTS idx_links_src ON links(src_kind, src_id);
