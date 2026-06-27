-- SQLite schema for the DBLP database built by `make dblp.db`.
-- Loaded first; then dblp_text.py/.go --format=sql streams the INSERTs; then the
-- fts table is populated (see the GNUmakefile).  One row per entry, flat (no joins):
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
  ee         TEXT,              -- all ee links, space-separated
  crossref   TEXT               -- proceedings key (inproceedings); joins proceedings.key
);

CREATE INDEX idx_year       ON entries(year);
CREATE INDEX idx_venue      ON entries(venue);
CREATE INDEX idx_doi        ON entries(doi);
CREATE INDEX idx_title_norm ON entries(title_norm);
CREATE INDEX idx_crossref   ON entries(crossref);

-- Proceedings volume records (the long conference name lives here, in `title`).
-- Join: entries.crossref = proceedings.key.
-- The kind/ordinal/conf_name/canonical columns are derived by dblp_confname.py as a
-- post-pass (see the GNUmakefile); `title` is the raw DBLP title and is left untouched.
CREATE TABLE proceedings (
  key         TEXT PRIMARY KEY,  -- e.g. conf/icsm/2025
  title       TEXT,              -- raw DBLP title (full name + location, dates) — kept as-is
  booktitle   TEXT,              -- short form / acronym (ICSME)
  year        INTEGER,
  publisher   TEXT,
  isbn        TEXT,
  ee          TEXT,              -- all ee links, space-separated
  url         TEXT,
  kind        TEXT,              -- main | workshop | companion | joint | other   (derived)
  ordinal     INTEGER,           -- edition number, or NULL                       (derived)
  ordinal_src TEXT,              -- title | inferred | none                        (derived)
  conf_name   TEXT,              -- clean series name, e.g. "International Conference on ..." (derived)
  canonical   TEXT               -- assembled, e.g. "Proceedings of the 23rd ... (ICSE 2001)"  (derived)
);

-- Journal full names. DBLP's <article> only carries an ISO-4 abbreviation (e.g.
-- "IEEE Trans. Software Eng."), never the full title, and there is no journal record
-- type to look it up. This is a hand-curated offline map (no network/crawl), keyed on
-- the per-article abbreviation because it is finer than the venue id: venue `ieicet`
-- alone spans four journals, `smr` two (a rename), `corr` three.
-- Seeded at build time from config.yaml's `journal_names:` map (the extractor emits the
-- INSERTs in --format=sql). Join: entries.journal = journals.abbrev.
CREATE TABLE journals (
  abbrev    TEXT PRIMARY KEY,  -- DBLP <journal> string, e.g. "J. Syst. Softw."
  full_name TEXT               -- full title, e.g. "Journal of Systems and Software"
);

-- Full-text search over title + authors (fuzzy/token matching for bib lookup).
-- Populated after the entries are loaded:
--   INSERT INTO fts(key,title,authors) SELECT key,title,authors FROM entries;
CREATE VIRTUAL TABLE fts USING fts5(key UNINDEXED, title, authors);
