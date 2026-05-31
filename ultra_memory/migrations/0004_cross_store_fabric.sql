-- SP-3 cross-store fabric. Additive, non-destructive: ADD COLUMN + CREATE TABLE
-- IF NOT EXISTS only — no DROP/RENAME, so a restore/replay against an
-- already-shaped DB is idempotent (db.py tolerates a re-applied ADD COLUMN). The
-- one data step (topic backfill, D4) is a separate guarded code path
-- (memory_lib.backfill_topic), NOT in this .sql, so the row-touch is gated.

-- topic + provenance on memories (D1, D16).
-- topic NULL = cross-topic / visible-to-all (composes with the §5 wall as
-- `topic IS NULL`); created_by default 'human' = safe-immutable default the §7a
-- provenance gate (SP-7) reads.
ALTER TABLE memories ADD COLUMN topic TEXT;
ALTER TABLE memories ADD COLUMN created_by TEXT NOT NULL DEFAULT 'human';

-- outcome signal on the capture queue + derived weight on the unit (D13, §7a
-- substrate). Both inert this cycle: no writer sets outcome_signal, and
-- outcome_weight defaults 1.0 (multiplicatively inert in unified ranking, D9).
ALTER TABLE session_events ADD COLUMN outcome_signal TEXT;
ALTER TABLE memories ADD COLUMN outcome_weight REAL NOT NULL DEFAULT 1.0;

-- links -> cross-store edge spine (D5). src_kind/dst_kind already exist
-- (memory/knowledge); add the within-kind sub-type so a reader can filter
-- (e.g. dst_type='mechanism') without a join.
ALTER TABLE links ADD COLUMN src_type TEXT;
ALTER TABLE links ADD COLUMN dst_type TEXT;

-- warm unified index (D8): a derived, rebuildable mirror of wiki pages beside the
-- memory tables. Files stay canonical; this is kept current by an idempotent
-- wiki_sync step (SP-3 Stage 5). outcome_weight reserved (inert) like memories.
CREATE TABLE IF NOT EXISTS unified_index (
  slug           TEXT PRIMARY KEY,
  topic          TEXT,
  page_type      TEXT,
  title          TEXT,
  snippet        TEXT,
  frontmatter    TEXT,
  path           TEXT,
  content_sha256 TEXT,
  outcome_weight REAL NOT NULL DEFAULT 1.0,
  updated_at     TEXT
);
CREATE INDEX IF NOT EXISTS idx_unified_topic ON unified_index(topic);

-- cross-store pin space (D7). Memory side keeps memories.pinned; the knowledge
-- side lives here (a wiki page has no row in memories). rehydrate.build_gist
-- (SP-3 Stage 4) unions both into one "## Pinned rules" gist section.
CREATE TABLE IF NOT EXISTS knowledge_pins (
  slug      TEXT PRIMARY KEY,
  topic     TEXT,
  pinned    INTEGER NOT NULL DEFAULT 1,
  reason    TEXT,
  pinned_at TEXT
);

-- topic access binding (D10). Many-to-many agent->topic(s). The per-request
-- identity mechanism (SP-0 spike #7) is UNRESOLVED, so the topic-identity source
-- is the ULTRA_MEMORY_CALLER_TOPIC env-var fallback (locked Stage 0); this table
-- is the persistent binding store SP-3 is forward-compatible with.
CREATE TABLE IF NOT EXISTS agent_topic_bindings (
  agent_name TEXT NOT NULL,
  topic      TEXT NOT NULL,
  created_at TEXT,
  PRIMARY KEY (agent_name, topic)
);

-- dst-side link index (the src-side idx_links_src already exists, 0001:65), so a
-- reverse-edge lookup (memory <- knowledge) is not a full scan.
CREATE INDEX IF NOT EXISTS idx_links_dst ON links(dst_kind, dst_id);
