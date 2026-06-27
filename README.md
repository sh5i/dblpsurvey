# dblpsurvey

A grep- and SQL-friendly survey toolkit over the [dblp](https://dblp.org/) bibliography.
`make` downloads the dblp dump once, filters it to the venues you care about, and builds two databases you work with offline:

- a text database (`data/<profile>.txt.gz`) — one line per paper, for fuzzy search with `fzf`/`peco`/`grep`;
- a SQLite database (`data/<profile>.db`) — a flat `entries` table plus a full-text index, for structured/full-text queries and for checking a `.bib` against DBLP.

![dblpsurvey demo](docs/dblpsurvey.gif)

## Tools

All CLIs live in `bin/`.

- **`dblpsurvey`** — search and pick papers; copy them to the clipboard, print them as BibTeX, or insert them straight into a `.bib`.
- **`dblplint`** — check and fix an existing `.bib` against DBLP, offline: correct author/year/pages/volume, fill missing fields, normalize venue names, swap an arXiv preprint for its published version — `git add -p`-style select-then-apply.
- **`dblpcite`** — turn DBLP keys into BibTeX entries (the bridge `dblpsurvey -i` uses; also composable on its own).

Non-ASCII field values (accented author names, …) are escaped to LaTeX macros by default — the portable form for (u)pLaTeX / legacy bibtex; pass `--utf8` (to `dblpcite` or `dblplint`) for raw UTF-8 instead.  DBLP author names are entirely Latin and round-trip completely; Greek/maths characters in titles have no portable macro and are left as UTF-8 (and reported).  The escapes assume `\usepackage[T1]{fontenc}`.

Behind `dblpsurvey -i` / `dblplint --apply` sits `bibgraft`, a convention-preserving, minimal-diff `.bib` editor that inserts/edits entries in your file's own layout.

## Prerequisites

- Basic commands: `bash`, `curl`, `gzip`, `gunzip`, `realpath`, `sed`, and `make`
- to build the text database, either:
  - [Go](https://go.dev/) — the default, faster extractor (`make`; reads its config with `go.yaml.in/yaml/v3`, fetched and pinned via go.mod on first build), or
  - Python 3 — the reference extractor (`make EXTRACTOR=python`; reads its config with the vendored PyYAML submodule, pulled in by the `--recurse-submodules` clone below)
- for the SQLite database: the [`sqlite3`](https://sqlite.org/) CLI (with FTS5) and Python 3 (the conference-name post-pass `dblp_confname.py`, standard library only)
- for the `.bib` tools (`dblplint`, `dblpcite`): Python 3 (standard library only). Their insert/apply path also uses a vendored copy of `bibtexparser` — a git submodule, pulled in by the `--recurse-submodules` clone below (or `git submodule update --init`; `make test` fetches it too).
- for search: [`fzf`](https://github.com/junegunn/fzf), [`peco`](https://github.com/peco/peco), or `grep`
- (optional) to paste to the clipboard: `pbcopy`, `xsel`, or `putclip`

## Quick Start

```
$ git clone --recurse-submodules https://github.com/sh5i/dblpsurvey.git
$ cd dblpsurvey
$ cp config/sample-se.yaml config/default.yaml    # pick a starting profile, then edit it
$ make                                            # download + build data/default.{txt.gz,db}
$ export PATH="$PWD/bin:$PATH"                    # optional: put the tools on your PATH (add to your shell rc)
```

`make` downloads the DBLP XML from <https://dblp.org/>, filters it by `config/default.yaml`, and writes the text and SQLite databases under `data/`.
The two extractors are interchangeable: `src/dblp_text.go` (default) and `src/dblp_text.py` (`make EXTRACTOR=python`, no Go toolchain needed); `make test` checks they produce identical output.

## Searching — `dblpsurvey`

```
$ dblpsurvey [-k] [-d] [-p PROFILE] [-b | -i FILE] [keyword...]
```

- `-k` — strip the DBLP key from each line
- `-d` — strip the DOI URL from each line
- `-p PROFILE` — search another profile's text database (default `default`; see [Profiles](#profiles))
- `-b` — print the picked entries as BibTeX (via `dblpcite`)
- `-i FILE` — insert the picked entries (as BibTeX) into `FILE`, in that file's own conventions
- `keyword...` — initial query

Pick interactively with `fzf` or `peco` (or plain `grep` if neither is installed).
By default the picks are copied to the clipboard.
Each line of the text database is one paper:

```
(journals/smr/Sae-LimHS18) Natthawute Sae-Lim, Shinpei Hayashi, Motoshi Saeki: "Context-based approach to prioritize code smells for prefactoring", J. Softw. Evol. Process., 30, 6, 2018. https://doi.org/10.1002/smr.1886
```

Build a bibliography by inserting picks straight into your `.bib` (the entry is rendered to match the file's indentation, delimiters, and field order):

```
$ dblpsurvey -i refs.bib code smells      # pick -> BibTeX -> inserted into refs.bib
$ dblpsurvey -b code smells               # ... or just print the BibTeX to stdout
```

![dblpsurvey -i → entry appended in your .bib's own conventions](docs/bibtex.gif)

## Checking a `.bib` — `dblplint`

Match each entry in a `.bib` against the SQLite database and propose fixes.
Nothing is applied until you choose (select-then-apply, like `git add -p`); every proposal has a stable id:

![dblplint demo](docs/dblplint.gif)

```
$ dblplint refs.bib                 # read-only report (safe in CI: a missing DB just skips, exit 0)
$ dblplint refs.bib --apply         # pick fixes interactively (fzf/peco; Tab to mark), then apply
$ dblplint refs.bib --apply=all     # apply every proposal (add --safe for the confident ones only)
$ dblplint refs.bib --mute          # silence proposals you've decided to keep, for good
$ dblplint -p ml refs.bib           # check against the 'ml' profile's database
```

Authoritative fields (author, year, pages, volume, number) are corrected from DBLP; missing fields are filled; venue names and a differing DOI are offered for review; an arXiv entry that has since been published is offered a whole-entry swap.
Decisions you want to keep can be silenced durably via an in-file `@comment{dblplint-ignore ...}` block.
See `dblplint --help` for the full set of flags.

For a `.bib` entry with a stale year, a truncated author list, an abbreviated journal, and a missing issue number and DOI, the report groups proposals per entry — `~` a confident correction, `?` a change left to your judgement, `+` a missing field — each tagged with a stable `key:field` id:

```
$ dblplint refs.bib
● Context-based approach to prioritize code smells for prefactoring  SaeLim2018
  ~ edit author     Natthawute Sae-Lim and others → Natthawute Sae-Lim and Shinpei Hayashi and Moto…  SaeLim2018:author
  ~ edit year       2017 → 2018  SaeLim2018:year
  ? edit journal    J. Softw. Evol. Process. → Journal of Software: Evolution and Process  expand to the full name  SaeLim2018:journal
  + add  number     6  SaeLim2018:number
  + add  doi        10.1002/smr.1886  SaeLim2018:doi

summary: 1 mismatch
  apply: dblplint BIB --apply   ·   silence: dblplint BIB --mute
```

`dblplint refs.bib --apply` then lets you pick which of these to accept; the chosen fixes are written back in the file's own layout (via `bibgraft`).
The report's non-zero exit on a mismatch makes it a usable CI gate.

## Configuration

`config/<profile>.yaml` selects what to extract.
Two ready-made samples are shipped under `config/` — copy one to your profile and edit:

- `config/sample-se.yaml` — a curated software engineering (SE) set of journals and conferences (well commented).
- `config/sample-all.yaml` — no filtering (`"*"` matches every venue; the whole of DBLP, huge and slow — see the warning inside).

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

### Profiles

A **profile** `NAME` pairs `config/NAME.yaml` with its own databases `data/NAME.{txt.gz,db}`, so you can keep several surveys side by side — the multi-gigabyte XML download is shared.
`default` is used when no profile is named; export `DBLPSURVEY_PROFILE=NAME` to change that default for `dblpsurvey`/`dblpcite`/`dblplint` (an explicit `-p`/`--profile` still wins).
Add another by writing a new config and building it:

```
# write config/ml.yaml (start from a sample, or copy config/default.yaml), then
$ make PROFILE=ml                            # -> data/ml.txt.gz, data/ml.db
$ dblpsurvey -p ml deep learning             # search the ml text database
$ dblplint --profile ml refs.bib              # check a .bib against the ml database
```

Your profiles (including `default.yaml`) are git-ignored; only the `sample-*.yaml` are tracked.

## Database

`make` builds `data/<profile>.db`, a SQLite database for structured and full-text queries — handy for scripts, or for `dblplint`'s offline checks.
Build it alone with `make data/<profile>.db` (e.g. `make data/default.db`; needs the `sqlite3` CLI with FTS5).
Its core is one flat table `entries` plus a full-text table `fts`, with two small lookup tables (`proceedings`, `journals`) you can join for clean venue/journal names:

| column | notes |
| --- | --- |
| `key` | DBLP key (primary key), e.g. `conf/icse/SmithB01` |
| `type` | `article` or `inproceedings` |
| `venue` | venue id from the key (`tse`, `icse`, ...) |
| `year` | integer |
| `authors` | `Given Family, ...` (same order as DBLP) |
| `title` | plain text (markup stripped, entities expanded) |
| `title_norm` | `title` reduced to ASCII `[a-z0-9]`, lowercased — for exact lookup |
| `journal` `booktitle` `volume` `number` `pages` | bibliographic fields |
| `doi` | preferred electronic edition (doi.org if present) |
| `ee` | all electronic-edition links, space-separated |
| `crossref` | for an `inproceedings`, its proceedings key (joins `proceedings.key`) |

`fts(key, title, authors)` is an FTS5 index for ranked fuzzy search.

DBLP's `<article>` only stores an ISO-4 abbreviation (e.g. `IEEE Trans. Software Eng.`), never the full journal title, so `journals(abbrev, full_name)` is a hand-curated offline map (no network), populated from `journal_names:` in your profile's config — join it on `entries.journal = journals.abbrev`.
Likewise the `proceedings` table carries derived `conf_name`/`canonical` names for conferences, joined on `entries.crossref = proceedings.key`.

```sql
-- structured filter
SELECT title, year FROM entries WHERE venue = 'icse' AND year >= 2020;

-- full-text (fuzzy) search over title + authors, ranked
SELECT e.year, e.venue, e.title FROM fts JOIN entries e USING(key)
WHERE fts MATCH 'refactoring legacy' ORDER BY rank LIMIT 10;

-- exact title lookup: normalize the query title the SAME way (lowercase, keep [a-z0-9])
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
