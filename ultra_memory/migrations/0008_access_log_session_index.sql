-- R4 FIX 5 — index access_log for the session-end attribution query. Additive,
-- non-destructive: a single CREATE INDEX IF NOT EXISTS, no DROP/RENAME/ALTER, so a
-- restore/replay against an already-shaped DB is idempotent (and a regressed-version
-- crash re-applies cleanly — the index simply already exists).
--
-- `attribution.recalled_units_for_session` filters access_log on
-- `target_kind='memory' AND session_id=? AND rank IS NOT NULL` at session-end (the
-- Stop hook). access_log is the fastest-growing, never-pruned table and had NO index
-- (0001 defined none; 0006/0007 added session_id/rank columns without one), so that
-- query full-scanned. The composite (session_id, target_kind) covers the equality
-- predicates of the query (rank IS NOT NULL is then a cheap residual filter on the
-- narrowed rows). Brings user_version to 8.
--
-- (A future follow-up — NOT built here — is an access_log retention prune; the index
-- bounds the read cost in the meantime.)
CREATE INDEX IF NOT EXISTS idx_access_log_session
  ON access_log(session_id, target_kind);
