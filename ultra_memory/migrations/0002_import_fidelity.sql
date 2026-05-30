ALTER TABLE memories ADD COLUMN description TEXT;
ALTER TABLE memories ADD COLUMN index_hook TEXT;
ALTER TABLE memories ADD COLUMN node_type TEXT NOT NULL DEFAULT 'memory';
