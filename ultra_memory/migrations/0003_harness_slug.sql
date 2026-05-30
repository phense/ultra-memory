-- file_slug: the underscore harness FILENAME stem (how the harness loads files +
-- how MEMORY.md links address them). NOT derivable from id (= the hyphenated name:,
-- which drops prefixes, e.g. feedback_email_routing.md → name: email-routing).
-- sort_order: the MEMORY.md line index, so the curated index order (incl. the
-- pinned top lines) survives a roundtrip instead of being re-sorted by id.
ALTER TABLE memories ADD COLUMN file_slug TEXT;
ALTER TABLE memories ADD COLUMN sort_order INTEGER;
