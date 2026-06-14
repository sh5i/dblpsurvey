# dblpsurvey
A quick & fast survey tool with a grep-friendly text file generated from [dblp](https://dblp.org/) database

![](https://i.gyazo.com/3c1e31b89302d81cd1fbdfdf18b3fb89.gif)

## Usage
```
$ dblpsurvey [-k] [-d] [keyword...]
```
Options:
- `-k`: Remove DBLP keys from the output
- `-d`: Remove DOI URLs from the output
- `keyword`: Used as initial keywords when specified

When running `dblpsurvey`, you can select your favorite lines if you have installed incremental search tools such as `peco`.
The results are pasted to the clipboard with `pbcopy`.

## Prerequisites
- Basic commands: `bash`, `curl`, `gzip`, `gunzip`, `realpath`, `perl`, and `make`
- for building the text database, either:
   - [Go](https://go.dev/) — the default, fast extractor (`make`), or
   - [`ruby`](https://www.ruby-lang.org/) — the readable reference extractor (`make EXTRACTOR=ruby`)
- for the SQLite database (`dblp.db`): the [`sqlite3`](https://sqlite.org/) CLI (with FTS5)
- for search: [`fzf`](https://github.com/junegunn/fzf), [`peco`](https://github.com/peco/peco), or `grep`
- (optional) for pasting to the clipboard: `pbcopy`, `xsel`, or `putclip`

## Installation
```
$ git clone https://github.com/sh5i/dblpsurvey.git
$ cd dblpsurvey
$ cp config.yaml.sample config.yaml
# (Edit config.yaml as you like)
$ make
$ sudo make install   # this just does: ln -s $(realpath ./dblpsurvey) /usr/local/bin/
```
The `make` first downloads the DBLP XML database file from https://dblp.org/, then filters it by the preference in `config.yaml` and converts the selected entries to a simple text in a single pass, each line representing a DBLP entry (`<article>` or `<inproceedings>`).
Such a text file is suitable for the grep-based search.
`make` also builds `dblp.db`, a SQLite database for structured and full-text queries (see [Database](#database-dblpdb)).

By default `make` builds and uses a fast Go extractor (`dblp_text.go`).
To avoid installing Go, use the equivalent Ruby extractor instead: `make EXTRACTOR=ruby` (`dblp_text.rb`).
`make test` checks that the two extractors produce identical output.

## Example of config.yaml
```
journals:
  # Enumerate your favorite journals in the DBLP world.
  # Only the <article>s of ID "journals/(journal ID)/*" survive.
  - tse
  - tosem

conferences:
  # Enumerate your favorite conferences in the DBLP world.
  # Only the <inproceedings>s of ID "conf/(conference ID)/*" survive.
  - icse
  - sigsoft
  - kbse

year:
  # Only the entries whose publishing year is in [lower, upper] survive.
  lower: 2005
  upper: 2100
```

## Database (`dblp.db`)
`make` also builds a SQLite database `dblp.db` for structured and full-text queries —
handy for scripts, or for checking/cleaning a `.bib` against DBLP offline.
Build it alone with `make dblp.db` (needs the `sqlite3` CLI with FTS5).

It is one flat table `entries` plus a full-text table `fts`:

| column | notes |
|---|---|
| `key` | DBLP key (primary key), e.g. `conf/icse/SmithB01` |
| `type` | `article` or `inproceedings` |
| `venue` | venue id from the key (`tse`, `icse`, …) |
| `year` | integer |
| `authors` | `Given Family, …` (same order as DBLP) |
| `title` | plain text (markup stripped, entities expanded) |
| `title_norm` | `title` reduced to ASCII `[a-z0-9]`, lowercased — for exact lookup |
| `journal` `booktitle` `volume` `number` `pages` | bibliographic fields |
| `doi` | preferred electronic edition (doi.org if present) |
| `ee` | all electronic-edition links, space-separated |

`fts(key, title, authors)` is an FTS5 index for ranked fuzzy search.

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

-- author with an abbreviated given name ("M. Fowler"): prefix the initial, scope to authors
SELECT e.key, e.authors, e.title FROM fts JOIN entries e USING(key)
WHERE fts MATCH 'authors:(m* fowler)' LIMIT 10;
```

Note: `dblp.db` contains only the venues/years selected by `config.yaml`, as of the last
`make`. An empty result means "not in this subset / not rebuilt yet", **not** necessarily
"does not exist" — out-of-scope or very recent entries still need dblp.org.

