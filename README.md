# dblpsurvey
A quick & fast survey tool that turns the [dblp](https://dblp.org/) database into a grep-friendly text file — and a queryable SQLite database — scoped to the venues you care about.

![](https://i.gyazo.com/3c1e31b89302d81cd1fbdfdf18b3fb89.gif)

## Usage
```
$ dblpsurvey [-k] [-d] [keyword...]
```
Options:
- `-k`: Remove DBLP keys from the output
- `-d`: Remove DOI URLs from the output
- `keyword`: Used as initial keywords when specified

When running `dblpsurvey`, you can select your favorite lines if you have installed incremental search tools such as `fzf` or `peco`.
The results are pasted to the clipboard with `pbcopy`.

Each line of the searchable database is one entry:
```
(journals/smr/Sae-LimHS18) Natthawute Sae-Lim, Shinpei Hayashi, Motoshi Saeki: "Context-based approach to prioritize code smells for prefactoring", J. Softw. Evol. Process., 30, 6, 2018. https://doi.org/10.1002/smr.1886
```

## Prerequisites
- Basic commands: `bash`, `curl`, `gzip`, `gunzip`, `realpath`, `perl`, and `make`
- for building the text database, either:
   - [Go](https://go.dev/) — the default, faster extractor (`make`), or
   - [`ruby`](https://www.ruby-lang.org/) — the reference extractor (`make EXTRACTOR=ruby`)
- for the SQLite database (`dblp.db`): the [`sqlite3`](https://sqlite.org/) CLI (with FTS5)
- for search: [`fzf`](https://github.com/junegunn/fzf), [`peco`](https://github.com/peco/peco), or `grep`
- (optional) for pasting to the clipboard: `pbcopy`, `xsel`, or `putclip`

## Installation
```
$ git clone https://github.com/sh5i/dblpsurvey.git
$ cd dblpsurvey
$ cp config-se.yaml config.yaml
# (Edit config.yaml as you like)
$ make
$ sudo make install   # this just does: ln -s $(realpath ./bin/dblpsurvey) /usr/local/bin/
```
The `make` first downloads the DBLP XML database file from https://dblp.org/, then filters it by the preference in `config.yaml` and converts the selected entries to a simple text in a single pass, each line representing a DBLP entry (`<article>` or `<inproceedings>`).
Such a text file is suitable for the grep-based search.
`make` also builds `dblp.db`, a SQLite database for structured and full-text queries (see [Database](#database-dblpdb)). Generated files are git-ignored: the data (XML download, `dblp.txt.gz`, `dblp.db`) lives under `data/`, and the compiled extractor (`dblp2text`) under `build/`.

The two extractors are interchangeable: `src/dblp_text.go` (default) and `src/dblp_text.rb` (`make EXTRACTOR=ruby`, no Go toolchain needed). `make test` checks that they produce identical output.

## Configuration
`config.yaml` selects what to extract. Two ready-made presets are shipped — copy one and edit:
- `config-se.yaml` — a curated software-engineering set of journals and conferences (well commented).
- `config-all.yaml` — no filtering (`"*"` matches every venue; the whole of DBLP, huge and slow — see the warning inside).

```yaml
journals:
  # Only <article>s under "journals/<id>/*" survive. Use a single "*" for every journal.
  - tse
  - tosem

conferences:
  # Only <inproceedings>s under "conf/<id>/*" survive. Use a single "*" for every conference.
  - icse
  - sigsoft

# Optional: map a journal's ISO-4 abbreviation (what DBLP stores) to its full title.
# Registered into the `journals` lookup table at build time (see Database below).
journal_names:
  "IEEE Trans. Software Eng.": "IEEE Transactions on Software Engineering"

year:
  # Keep only entries whose publishing year is in [lower, upper].
  lower: 2005
  upper: 2100
```

A config that selects no venues is rejected; use `"*"` if you really want everything.

## Database (`dblp.db`)
`make` also builds `build/dblp.db`, a SQLite database for structured and full-text queries — handy for scripts, or for checking/cleaning a `.bib` against DBLP offline.
Build it alone with `make data/dblp.db` (needs the `sqlite3` CLI with FTS5).

Its core is one flat table `entries` plus a full-text table `fts`, with two small lookup tables (`proceedings`, `journals`) you can join for clean venue/journal names:

| column | notes |
| --- | --- |
| `key` | DBLP key (primary key), e.g. `conf/icse/SmithB01` |
| `type` | `article` or `inproceedings` |
| `venue` | venue id from the key (`tse`, `icse`, …) |
| `year` | integer |
| `authors` | `Given Family, ...` (same order as DBLP) |
| `title` | plain text (markup stripped, entities expanded) |
| `title_norm` | `title` reduced to ASCII `[a-z0-9]`, lowercased — for exact lookup |
| `journal` `booktitle` `volume` `number` `pages` | bibliographic fields |
| `doi` | preferred electronic edition (doi.org if present) |
| `ee` | all electronic-edition links, space-separated |
| `crossref` | for an `inproceedings`, its proceedings key (joins `proceedings.key`) |

`fts(key, title, authors)` is an FTS5 index for ranked fuzzy search.

DBLP's `<article>` only stores an ISO-4 abbreviation (e.g., `IEEE Trans. Software Eng.`), never the full journal title, so `journals(abbrev, full_name)` is a hand-curated offline map (no network), populated from `journal_names:` in `config.yaml` — join it on `entries.journal = journals.abbrev`.
Likewise the `proceedings` table carries derived `conf_name`/`canonical` names for conferences, joined on `entries.crossref = proceedings.key`.

```sql
-- structured filter
SELECT title, year FROM entries WHERE venue = 'icse' AND year >= 2020;

-- full-text (fuzzy) search over title + authors, ranked
SELECT e.year, e.venue, e.title FROM fts JOIN entries e USING(key)
WHERE fts MATCH 'refactoring legacy' ORDER BY rank LIMIT 10;

-- exact title lookup: normalise the query title the SAME way (lowercase, keep [a-z0-9])
SELECT key, year, venue FROM entries WHERE title_norm = 'refactoringimprovingthedesign';

-- match a DOI inside any electronic-edition link (handles arXiv vs publisher links)
SELECT key FROM entries WHERE ee LIKE '%10.1145/3377811%';

-- expand the abbreviated journal to its full title
SELECT e.year, j.full_name, e.title FROM entries e
JOIN journals j ON j.abbrev = e.journal WHERE e.venue = 'tse' AND e.year = 2020;

-- author with an abbreviated given name ("M. Fowler"): prefix the initial, scope to authors
SELECT e.key, e.authors, e.title FROM fts JOIN entries e USING(key)
WHERE fts MATCH 'authors:(m* fowler)' LIMIT 10;
```
