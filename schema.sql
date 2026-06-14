-- SQLite schema for the DBLP database built by `make dblp.db`.
-- Loaded first; then dblp_text.rb/.go --format=sql streams the INSERTs; then the
-- fts table is populated (see the Makefile).  One row per entry, flat (no joins):
-- all ee links are concatenated into `ee` so any of them matches `ee LIKE '%doi%'`.

CREATE TABLE entries (
  key        TEXT PRIMARY KEY,  -- DBLP key, e.g. conf/icse/SmithB01
  type       TEXT,              -- article | inproceedings
  venue      TEXT,              -- venue id from the key (tse, icse, ...)
  year       INTEGER,
  authors    TEXT,              -- "Given Family, Given Family, ..."
  title      TEXT,
  title_norm TEXT,              -- ASCII [a-z0-9] only, lowercased; for exact lookup
  journal    TEXT,
  booktitle  TEXT,
  volume     TEXT,
  number     TEXT,
  pages      TEXT,
  doi        TEXT,              -- preferred ee (doi.org if present, else first)
  ee         TEXT               -- all ee links, space-separated
);

CREATE INDEX idx_year       ON entries(year);
CREATE INDEX idx_venue      ON entries(venue);
CREATE INDEX idx_doi        ON entries(doi);
CREATE INDEX idx_title_norm ON entries(title_norm);

-- Full-text search over title + authors (fuzzy/token matching for bib lookup).
-- Populated after the entries are loaded:
--   INSERT INTO fts(key,title,authors) SELECT key,title,authors FROM entries;
CREATE VIRTUAL TABLE fts USING fts5(key UNINDEXED, title, authors);
