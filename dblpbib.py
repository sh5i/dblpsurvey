#!/usr/bin/env python3
"""Check and fix a .bib file against a local DBLP database (dblpsurvey's dblp.db).

For each @article / @inproceedings (and arXiv @misc) it looks the title up in the DB
-- falling back to DOI, then arXiv id -- and proposes changes: authoritative fields
(author, year, pages, volume, number) corrected from the DB, missing fields filled,
venue names and a differing DOI offered too.  Nothing is auto-applied: you choose.

The fix model is select-then-apply (like `git add -p`).  Every proposed change has a
stable id `citekey:field` (or `citekey:@` for an arXiv->published whole-entry swap):

  dblpbib refs.bib                       # read-only report (each proposal shows its id)
  dblpbib refs.bib --apply               # pick fixes interactively (fzf/peco), then apply
  dblpbib refs.bib --apply=all           # apply every proposal (add --safe for confident only)
  dblpbib refs.bib --oneline | fzf | dblpbib refs.bib --apply=stdin   # compose your own picker
  dblpbib refs.bib --mute                # pick proposals to silence for good (interactively)
  dblpbib refs.bib --mute=all            # silence every still-warning proposal for good
  dblpbib refs.bib --show ID             # one proposal's full before/after (fzf preview)
  dblpbib refs.bib --add 10.1145/xxxxxxx # fetch an entry from the DB and append it
  dblpbib refs.bib -a                    # also show suppressed (ignored) proposals
  dblpbib refs.bib --ignore key:doi      # suppress one proposal for this run (ephemeral)

Proposals you have deliberately decided not to change can be silenced.  An in-file
block (travels with the .bib; @comment is ignored by BibTeX/biblatex) lists selectors
`<keypat>:<fieldlist>`, whitespace-separated (any number per line); `#` or `%` starts a
to-end-of-line comment:

  @comment{dblpbib-ignore
    Yu2021:doi  Yu2021:booktitle,note   # preprint DOI + short venue form kept
    *:doi                               # I always keep my own DOIs  ('*' key = any entry)
    @misc:*                             # out of DB scope            ('@type' = a type)
  }

keypat is a cite key, `*` (any entry) or `@type`; a field is a name, `@` (the whole-entry
replace) or `*` (everything for that entry).  A suppressed proposal is hidden, omitted from
--apply / --pick, and does NOT count toward the CI-failing mismatches; a selector that
matches nothing is reported as stale.

The DB path may be given via --db or the DBLP_DB environment variable.  A missing
database is treated as "skip" (exit 0) so it never blocks a build.
"""

import argparse
import base64
import json
import os
import re
import shlex
import shutil
import sqlite3
import subprocess
import sys
import unicodedata

# --- normalisation helpers (must mirror dblpsurvey's extractor) ---------------

def norm_title(s):
    """Title key: keep ASCII [A-Za-z0-9], lowercased -- same rule as dblp.db."""
    return re.sub(r"[^A-Za-z0-9]", "", s).lower()

def norm_pages(s):
    """Reduce a page field to its digit groups joined by '-' (1--12 == 1-12)."""
    return "-".join(re.findall(r"\d+", s or ""))

def fmt_pages(s):
    """House style for a page range: two numbers joined by '--' (per bib-guide)."""
    nums = re.findall(r"\d+", s or "")
    return "--".join(nums) if len(nums) == 2 else (s or "")

def authors_to_bib(s):
    """DB authors 'Given Family 0001, Given Family, ...' -> 'A and B and ...'."""
    names = [re.sub(r"\s+\d{4}$", "", a.strip()) for a in s.split(",") if a.strip()]
    return " and ".join(names)

def strip_doi(s):
    return re.sub(r"^https?://(dx\.)?doi\.org/", "", s or "", flags=re.I)

def is_truncated(author):
    """True if the author list is explicitly cut short ('and others' / 'et al')."""
    return bool(re.search(r"\bothers\b|\bet\.?\s*al", author, re.I))

def _delatex(s):
    """Strip LaTeX accent/markup so it neither leaks letters nor splits a name.
    {\\textcommabelow t} -> t, {\\^a} -> a, \\'e -> e, then braces removed.  The
    command-plus-space form is handled first so 'P{\\^a}r{\\textcommabelow t}achi'
    collapses to 'Partachi' rather than splitting into 'below'/'t}achi'."""
    s = re.sub(r"\\[a-zA-Z]+\s*", "", s)         # \command (and its trailing space)
    s = re.sub(r"\\.", "", s)                    # \' \" \^ \~ \` ...
    return s.replace("{", "").replace("}", "")

def _fold(tok):
    """A name token reduced to ASCII [a-z0-9]: de-LaTeX then strip accents (NFKD),
    so {\\~a} / André / P{\\^a}r{\\textcommabelow t}achi all fold to plain ASCII."""
    return re.sub(r"[^A-Za-z0-9]", "",
                  unicodedata.normalize("NFKD", _delatex(tok))).lower()

def surnames(s):
    """Set of folded surnames -- the reliable part of a name.  Order/format/
    accent insensitive: 'Hayashi, Shinpei' and 'Shinpei Hayashi' both -> hayashi;
    'Tien N. Nguyen' and 'Nguyen, Tien Nhut' both -> nguyen (given-name detail,
    which differs between sources, is deliberately ignored)."""
    out = set()
    for a in re.split(r"\s+and\s+", s):
        a = a.strip()
        if not a:
            continue
        fam = _delatex(a.split(",")[0] if "," in a else a)  # family part, LaTeX removed
        toks = fam.split()
        if not toks:
            continue
        # last token of the family part -- consistent across "De Roover, Coen"
        # and "Coen De Roover" (both -> roover), and "Hayashi, Shinpei" -> hayashi.
        f = _fold(toks[-1])
        if f and f not in ("others", "etal"):
            out.add(f)
    return out

# --- a small offset-tracking .bib parser --------------------------------------
# We keep byte offsets so --fix can edit individual field values surgically
# without re-serialising (and thus without disturbing untouched fields/layout).

class Field:
    __slots__ = ("name", "value", "start", "end")  # start/end span the value token

    def __init__(self, name, value, start, end):
        self.name, self.value, self.start, self.end = name, value, start, end

class Entry:
    def __init__(self, etype, key, fields, start, body_close):
        self.type = etype          # 'article' | 'inproceedings' | 'misc'
        self.key = key
        self.fields = fields       # dict name -> Field
        self.start = start         # offset of the leading '@'
        self.body_close = body_close  # offset of the entry's closing '}'

    def get(self, name):
        f = self.fields.get(name)
        return f.value if f else ""

    def last_field_end(self):
        return max((f.end for f in self.fields.values()), default=self.body_close)

def _match_brace(text, i):
    depth = 0
    while i < len(text):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return len(text) - 1

def parse_bib(text):
    """Parse @article/@inproceedings/@misc entries, keeping byte offsets for surgical
    --fix edits. Assumes brace- or quote-delimited values (the common generated-.bib
    form); @string macros and value concatenation ('a # b') are not interpreted."""
    entries = []
    for m in re.finditer(r"@(\w+)\s*\{", text):
        etype = m.group(1).lower()
        open_brace = m.end() - 1
        close_brace = _match_brace(text, open_brace)
        body = text[m.end():close_brace]
        base = m.end()
        comma = body.find(",")
        if comma < 0:
            continue
        key = body[:comma].strip()
        fields = {}
        i = comma + 1
        n = len(body)
        while i < n:
            fm = re.match(r"\s*(\w+)\s*=\s*", body[i:])
            if not fm:
                break
            name = fm.group(1).lower()
            j = i + fm.end()
            if j >= n:
                break
            c = body[j]
            if c == "{":
                k = _match_brace(body, j)
                value, vs, ve = body[j + 1:k], j, k + 1
            elif c == '"':
                k = body.find('"', j + 1)
                if k < 0:
                    break
                value, vs, ve = body[j + 1:k], j, k + 1
            else:
                k = j
                while k < n and body[k] not in ",}":
                    k += 1
                value, vs, ve = body[j:k].strip(), j, j + len(body[j:k].rstrip())
            fields[name] = Field(name, value.strip(), base + vs, base + ve)
            i = ve
            nxt = body.find(",", i)
            i = nxt + 1 if nxt >= 0 else n
        # @misc is kept so arXiv preprints can be checked for a published version
        if etype in ("article", "inproceedings", "misc"):
            entries.append(Entry(etype, key, fields, m.start(), close_brace))
    return entries

# --- database lookup ----------------------------------------------------------

def lookup_exact(con, title):
    rows = con.execute(
        "SELECT * FROM entries WHERE title_norm=?", (norm_title(title),)
    ).fetchall()
    published = [r for r in rows if r["venue"] != "corr"]
    preprint = [r for r in rows if r["venue"] == "corr"]
    return published, preprint

def lookup_doi(con, doi):
    """Fallback lookup by DOI (when the title doesn't match): try the indexed doi
    column first, then any electronic-edition link in `ee`."""
    rows = con.execute(
        "SELECT * FROM entries WHERE doi = 'https://doi.org/' || ?", (doi,)).fetchall()
    if not rows:
        rows = con.execute(
            "SELECT * FROM entries WHERE ee LIKE '%' || ? || '%'", (doi,)).fetchall()
    pub = [r for r in rows if r["venue"] != "corr"]
    pre = [r for r in rows if r["venue"] == "corr"]
    return pub, pre

_AXID = r"[a-z\-]+/\d{7}|\d{4}\.\d{4,5}"     # cs/0501001 (old) | 2407.19487 (new)

def arxiv_id(entry):
    """Extract an arXiv identifier from the entry.  Looks at eprint, the DBLP
    'volume = {abs/<id>}' form, and any 'arXiv:<id>' marker in free-text fields
    (the marker is required there so plain DOIs aren't mistaken for ids)."""
    ep = entry.get("eprint").strip()
    if re.fullmatch(r"(%s)(v\d+)?" % _AXID, ep):
        return re.sub(r"v\d+$", "", ep)
    m = re.fullmatch(r"abs/(%s)(v\d+)?" % _AXID, entry.get("volume").strip(), re.I)
    if m:
        return m.group(1)
    blob = " ".join(entry.get(f) for f in ("howpublished", "note", "journal", "doi", "url"))
    m = re.search(r"arxiv[:\s/.]*(%s)" % _AXID, blob, re.I)
    return m.group(1) if m else ""

def lookup_arxiv(con, axid):
    """Find the DBLP CoRR (arXiv) record for an arXiv id, via its ee link.  Lets us
    match even when the arXiv title changed across versions."""
    rows = con.execute("SELECT * FROM entries WHERE venue='corr' AND "
                       "ee LIKE '%' || ? || '%'", (axid,)).fetchall()
    return [], rows                          # arXiv records are always preprints

def is_arxiv(entry):
    """True if the .bib entry is written as an arXiv/CoRR preprint, in any of the
    common styles (eprint/archivePrefix, biblatex eprinttype, journal={CoRR} +
    volume={abs/...}, Google-Scholar journal={arXiv preprint ...}, etc.)."""
    blob = " ".join(entry.get(f) for f in
                    ("journal", "booktitle", "howpublished", "note", "eprint",
                     "archiveprefix", "eprinttype", "series", "volume")).lower()
    return ("arxiv" in blob or "corr" in blob
            or "arxiv" in strip_doi(entry.get("doi")).lower()
            or bool(entry.get("eprint"))
            or entry.get("volume").strip().lower().startswith("abs/"))

def suggest_publication(entry, pub, pre):
    """Return the published DB row to switch to, when an arXiv-form entry ALSO has a
    formal publication (so replacing is appropriate).  None otherwise."""
    if not pub:
        return None                          # no formal publication in the DB
    return pub[0] if (entry.type == "misc" or is_arxiv(entry)) else None

def _vn(s):
    """Normalise a venue string for comparison (ASCII alnum, lowercased)."""
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())

def venue_forms(con, row):
    """Return (full, kind, full_forms, short_forms, short) for a row's venue.

    `full` is the expanded official name; `short` is the abbreviated form.  These are the
    two proposal targets: the default target is `full`, the --short target is `short`.
    `full_forms`/`short_forms` are the normalised spellings already acceptable on each
    side, which makes a venue proposal *directional* -- a spelling sitting on the OTHER
    side is offered expansion (default) or shortening (--short), not just "wrong".

    Articles: `full` is the official journal title (from the `journals` table, keyed by
    DBLP's abbreviation) and `short` the abbreviation; an unmapped journal has the two
    equal, so neither expansion nor shortening is ever proposed.

    Inproceedings: follow the crossref to `proceedings` for the canonical name
    (e.g. 'Proceedings of the 47th ... (ICSE 2025)') and its kind (main/workshop/...);
    `short` is the 'Proc. {ACRONYM}' form (acronym from the booktitle, e.g. 'ICSE')."""
    if row["type"] != "inproceedings":
        j = row["journal"]
        if not j:
            return "", "journal", set(), set(), ""
        r = con.execute("SELECT full_name FROM journals WHERE abbrev=?", (j,)).fetchone()
        full = (r["full_name"] if r else "") or j
        return full, "journal", {_vn(full)}, {_vn(j)}, j
    xref = row["crossref"] if "crossref" in row.keys() else ""
    short_bt, cano, conf, kind = row["booktitle"], "", "", "main"
    if xref:
        p = con.execute("SELECT canonical, conf_name, booktitle, kind "
                        "FROM proceedings WHERE key=?", (xref,)).fetchone()
        if p:
            cano, conf = p["canonical"] or "", p["conf_name"] or ""
            short_bt = p["booktitle"] or short_bt
            kind = p["kind"] or "main"
    full = cano or conf or short_bt
    acronym = re.sub(r"\s*\([^)]*\)\s*$", "", short_bt).strip()   # 'ICSE (1)' -> 'ICSE'
    short = "Proc. {%s}" % acronym if acronym else full
    full_forms = {_vn(x) for x in (cano, conf) if x}
    short_forms = {_vn(x) for x in (short_bt, acronym, short) if x}
    return full, kind, full_forms, short_forms, short

def to_bibtex(con, citekey, row, short=False):
    """Serialise a DB row as a BibTeX entry, keeping the user's citation key and
    using the full official venue name (or 'Proc. {ACRONYM}' when short)."""
    fields = [("author", authors_to_bib(row["authors"])), ("title", row["title"])]
    full, _kind, _ff, _sf, short_name = venue_forms(con, row)
    if row["type"] == "inproceedings":
        fields.append(("booktitle", short_name if (short and short_name) else full))
    else:
        fields += [("journal", short_name if (short and short_name) else full),
                   ("volume", row["volume"]), ("number", row["number"])]
    fields += [("pages", fmt_pages(row["pages"])), ("year", str(row["year"]))]
    doi = strip_doi(row["doi"])
    if doi:
        fields.append(("doi", doi))
    body = ",\n".join("  %s = {%s}" % (k, v) for k, v in fields if v)
    return "@%s{%s,\n%s\n}" % (row["type"], citekey, body)

def resolve_id(con, idstr):
    """Look up a single entry by DOI, arXiv id, or DBLP key (preferring the
    published version for a DOI).  Returns a row or None."""
    s = strip_doi(idstr).strip()
    if re.match(r"10\.\d+/", s):                                   # DOI
        pub, pre = lookup_doi(con, s)
        return pub[0] if pub else (pre[0] if pre else None)
    ax = re.sub(r"^arxiv:", "", idstr.strip(), flags=re.I)
    if re.fullmatch(r"(%s)(v\d+)?" % _AXID, ax):                   # arXiv id
        _, pre = lookup_arxiv(con, re.sub(r"v\d+$", "", ax))
        return pre[0] if pre else None
    if "/" in idstr:                                               # DBLP key
        return con.execute("SELECT * FROM entries WHERE key=?", (idstr.strip(),)).fetchone()
    return None

def gen_citekey(row, used):
    """Generate a <Family><Year> citation key, avoiding collisions in `used`."""
    first = (row["authors"] or "anon").split(",")[0]
    toks = re.sub(r"\s+\d{4}$", "", first).split()
    fam = _fold(toks[-1]) if toks else "anon"
    base = (fam[:1].upper() + fam[1:]) + str(row["year"] or "")
    key, suffix = base, ord("a")
    while key in used:
        key = base + chr(suffix)
        suffix += 1
    return key

def fuzzy(con, title, limit=4):
    toks = [t for t in re.findall(r"[A-Za-z0-9]+", title) if len(t) > 2][:8]
    if not toks:
        return []
    q = "title:(" + " OR ".join(toks) + ")"
    try:
        return con.execute(
            "SELECT e.year, e.venue, e.title, e.key FROM fts JOIN entries e "
            "USING(key) WHERE fts MATCH ? ORDER BY rank LIMIT ?", (q, limit)
        ).fetchall()
    except sqlite3.OperationalError:
        return []

# --- comparison ---------------------------------------------------------------

def name_tokens(s):
    """All folded name tokens (given + family), order/accent-insensitive."""
    out = set()
    for a in re.split(r"\s+and\s+", s):
        for tok in re.split(r"[\s,]+", _delatex(a)):
            f = _fold(tok)
            if f and f not in ("others", "etal"):
                out.add(f)
    return out

def _covered(t, toks):
    """True if t is in toks, or t is an initial matching some token's first letter."""
    return t in toks or (len(t) == 1 and any(x.startswith(t) for x in toks))

def given_detail(bib_tok, db_tok):
    """How the .bib's name detail relates to DBLP's (surnames already match).
    'bib_richer' = the .bib covers everything DBLP has (DBLP may abbreviate);
    'differ'     = the .bib lacks something DBLP spells out (or they conflict)."""
    return "bib_richer" if all(_covered(t, bib_tok) for t in db_tok) else "differ"

def compare(con, entry, row, short=False):
    """Classify each field difference into one of three tiers:
      fills   -- field absent from the .bib; safe to add
      fixes   -- the .bib is wrong in a direction DBLP corrects
      reviews -- a real difference whose direction is the author's call (venue name,
                 a differing DOI, author/given-name detail, ambiguous pages)
    Each review carries a note. process_entry turns all three into selectable proposals
    (op add/edit + a `review` flag): fills -> add, fixes -> confident edit, reviews -> edit?."""
    fixes, reviews, fills = [], [], []

    # author
    cur, db = entry.get("author"), authors_to_bib(row["authors"])
    if not cur:
        fills.append(("author", db))
    elif is_truncated(cur):
        fixes.append(("author", cur, db))                       # "and others" -> full list
    elif surnames(cur) != surnames(db):
        reviews.append(("author", cur, db,
                        "author set differs from DBLP — verify the citation / name forms"))
    elif name_tokens(cur) != name_tokens(db):
        rel = given_detail(name_tokens(cur), name_tokens(db))
        reviews.append(("author", cur, db,
                        "your entry is at least as detailed; DBLP abbreviates a name — kept as is"
                        if rel == "bib_richer" else
                        "given-name detail differs from DBLP — check"))

    # year
    cur = entry.get("year")
    if not cur:
        fills.append(("year", str(row["year"])))
    elif cur.strip() != str(row["year"]):
        fixes.append(("year", cur, str(row["year"])))

    # pages
    cur = entry.get("pages")
    if not cur:
        if row["pages"]:
            fills.append(("pages", fmt_pages(row["pages"])))
    elif row["pages"] and norm_pages(cur) != norm_pages(row["pages"]):
        nc, nd = re.findall(r"\d+", cur), re.findall(r"\d+", row["pages"])
        if len(nc) == 2 and len(nd) == 2:
            fixes.append(("pages", cur, fmt_pages(row["pages"])))   # end-page typo etc.
        else:
            reviews.append(("pages", cur, row["pages"],
                            "DBLP shows '%s' (a single number — likely an article id); "
                            "your value was not confirmed" % row["pages"]
                            if len(nd) < 2 else "page numbers differ — check"))

    # volume / number (articles only)
    if entry.type == "article":
        for fld in ("volume", "number"):
            cur, db = entry.get(fld), str(row[fld] or "")
            if not db:
                continue
            if not cur:
                fills.append((fld, db))
            elif cur.strip() != db:
                fixes.append((fld, cur, db))

    # doi: add if missing, but a *differing* DOI is advisory (preprint vs published?)
    cur, db = entry.get("doi"), strip_doi(row["doi"])
    if db:
        if not cur:
            fills.append(("doi", db))
        elif strip_doi(cur).lower() != db.lower():
            reviews.append(("doi", cur, db, "DOI differs from DBLP — check (edition / preprint?)"))

    # venue name: directional on --short.  Default mode prefers the full official name
    # (a short/abbreviated spelling is offered EXPANSION); --short prefers the short form
    # (a long spelling is offered SHORTENING).  A spelling already on the target side is
    # left untouched; an unrecognised one is a plain venue correction.  Journals live in
    # the journal field, proceedings in booktitle; venue_forms supplies both sides' forms.
    full, _vk, full_forms, short_forms, short_name = venue_forms(con, row)
    if full or short_name:
        vfield = "booktitle" if row["type"] == "inproceedings" else "journal"
        word = "venue" if row["type"] == "inproceedings" else "journal"
        target, accept, other = ((short_name, short_forms, full_forms) if short
                                 else (full, full_forms, short_forms))
        cur = entry.get(vfield)
        if not cur:
            fills.append((vfield, target))
        elif _vn(cur) not in accept:
            if _vn(cur) in other:
                note = "shorten to the abbreviated form" if short else "expand to the full name"
            else:
                note = "short %s form" % word if short else "official %s name" % word
            reviews.append((vfield, cur, target, note))

    return fixes, reviews, fills

# --- fix application (surgical, reverse-order edits) ---------------------------

def apply_fixes(text, edits):
    """edits: list of (start, end, replacement). end==start means insertion."""
    for start, end, repl in sorted(edits, key=lambda e: e[0], reverse=True):
        text = text[:start] + repl + text[end:]
    return text

# --- add mode -----------------------------------------------------------------

def add_entries(con, args, text, entries, color):
    """Fetch entries from the DB by id and append them to the .bib (or stdout)."""
    def c(code, s):
        return "\033[%sm%s\033[0m" % (code, s) if color else s
    out = sys.stderr if args.dry_run else sys.stdout
    used = {e.key for e in entries}
    have_doi = {strip_doi(e.get("doi")).lower() for e in entries if e.get("doi")}
    have_tn = {norm_title(e.get("title")) for e in entries if e.get("title")}

    specs = [s for spec in args.add for s in spec.split(",") if s.strip()]
    blocks = []
    for spec in specs:
        key, sep, idstr = spec.partition("=")
        if not sep:
            key, idstr = "", key
        idstr = idstr.strip()
        row = resolve_id(con, idstr)
        if row is None:
            print(c("31", "✗ not found: %s" % idstr), file=out)
            continue
        d, tn = strip_doi(row["doi"]).lower(), norm_title(row["title"])
        if (d and d in have_doi) or tn in have_tn:
            print(c("33", "• already present, skipped: %s (%s)" % (idstr, row["key"])),
                  file=out)
            continue
        key = key.strip() or gen_citekey(row, used)
        if key in used:
            print(c("31", "✗ cite key already in use: %s" % key), file=out)
            continue
        used.add(key); have_tn.add(tn); have_doi.add(d)
        blocks.append(to_bibtex(con, key, row, args.short))
        print(c("32", "+ added [%s] <- %s" % (key, row["key"])), file=out)

    payload = "".join("\n" + b + "\n" for b in blocks)
    if not blocks:
        return 1
    if args.dry_run:
        sys.stdout.write(payload.lstrip("\n"))
    else:
        sep = "" if (not text or text.endswith("\n")) else "\n"
        with open(args.bib, "a", encoding="utf-8") as f:
            f.write(sep + payload)
    return 0

# --- per-entry processing -----------------------------------------------------

def match_record(con, e):
    """Locate the DBLP record(s) for an entry: try the title, then the DOI, then an
    arXiv id; if only a preprint matched by id, look for a published sibling by its real
    title (the entry's own title may be stale). Returns (pub, pre, matched_by)."""
    title = e.get("title")
    pub, pre = lookup_exact(con, title)
    matched_by = "title"
    if not (pub or pre):                      # title missed: fall back to the DOI
        doi = strip_doi(e.get("doi"))
        if doi:
            pub, pre = lookup_doi(con, doi)
            matched_by = "doi"
    if not (pub or pre):                      # still nothing: try the arXiv id
        axid = arxiv_id(e)
        if axid:
            pub, pre = lookup_arxiv(con, axid)
            matched_by = "arxiv"
    if pre and not pub and matched_by in ("doi", "arxiv"):
        pub, _ = lookup_exact(con, pre[0]["title"])
    return pub, pre, matched_by

def _edit_for(e, field, value):
    """Surgical edit that sets `field` to `value`: replace the value span if the field is
    present, else insert a new field after the last one."""
    if field in e.fields:
        f = e.fields[field]
        return (f.start, f.end, "{%s}" % value)
    pos = e.last_field_end()
    return (pos, pos, ",\n  %s = {%s}" % (field, value))

def _proposal(e, op, field, bib, dblp, note="", review=False):
    return dict(id="%s:%s" % (e.key, field), op=op, review=review, field=field,
                bib=bib, dblp=dblp, note=note, edit=_edit_for(e, field, dblp))

def process_entry(con, e, short=False):
    """Classify one .bib entry against DBLP. Returns a finding dict, or None to skip.
    A matched finding carries `proposals`: each a selectable change with a stable id
    (`citekey:field`, or `citekey:@`), an operation `op` (add a missing field / edit an
    existing one / replace the whole entry) and a `review` flag (True = the author should
    judge it; False = DBLP is authoritative, so it is safe to apply), plus before/after
    values and a surgical edit. `status` summarises the finding; an unmatched one carries
    fuzzy `candidates`."""
    if e.type == "misc" and not is_arxiv(e):
        return None                            # non-paper @misc (datasets, manuals): skip
    title = e.get("title")
    pub, pre, matched_by = match_record(con, e)

    if not (pub or pre):                       # no match -> fuzzy candidates, else unknown
        cands = fuzzy(con, title)
        return dict(key=e.key, type=e.type, status=("fuzzy" if cands else "unknown"),
                    title=title, matched_by=None, proposals=[],
                    candidates=[dict(year=c["year"], venue=c["venue"], title=c["title"])
                                for c in cands])

    # An arXiv-form entry that is now formally published -> a whole-entry replace proposal.
    published = suggest_publication(e, pub, pre)
    if published:
        vkind = venue_forms(con, published)[1]
        tag = "" if vkind in ("main", "journal") else " [%s]" % vkind
        vshort = published["booktitle"] or published["journal"]
        repl = to_bibtex(con, e.key, published, short)
        prop = dict(id="%s:@" % e.key, op="replace", review=True, field="@entry",
                    bib="@" + e.type,
                    dblp="%s %s%s (%s)" % (vshort, published["year"], tag, published["key"]),
                    note="now published — replace the whole entry",
                    replacement=repl, edit=(e.start, e.body_close + 1, repl))
        return dict(key=e.key, type=e.type, status="upgrade", title=title,
                    matched_by=matched_by, proposals=[prop], candidates=[])

    # Otherwise verify against the canonical record (published if available, else arXiv).
    row = pub[0] if pub else pre[0]
    fixes, reviews, fills = compare(con, e, row, short)
    if matched_by in ("doi", "arxiv"):         # matched by id -> the title disagrees
        reviews.insert(0, ("title", title, row["title"],
                           "matched by %s but the title differs — an arXiv title may have "
                           "changed across versions, or a typo / wrong key" % matched_by.upper()))
    props = [_proposal(e, "edit", f, a, b) for f, a, b in fixes]               # confident edit
    props += [_proposal(e, "edit", f, a, b, n, review=True) for f, a, b, n in reviews]
    props += [_proposal(e, "add", f, "", b) for f, b in fills]                 # missing field
    status = ("mismatch" if any(p["op"] == "edit" and not p["review"] for p in props)
              else "review" if any(p["review"] for p in props)
              else "incomplete" if props else "ok")
    return dict(key=e.key, type=e.type, status=status, title=title,
                matched_by=matched_by, proposals=props, candidates=[])

def derive(con, entries, short=False, selectors=()):
    """All findings for the parsed entries (skipped entries dropped); suppressions applied."""
    findings = [f for f in (process_entry(con, e, short) for e in entries) if f is not None]
    apply_suppressions(findings, selectors)
    return findings

def all_proposals(findings):
    return [p for f in findings for p in f.get("proposals", [])]

# --- suppression (the @comment{dblpbib-ignore ...} block) ---------------------
# Selectors are whitespace-separated tokens "<keypat>:<fieldlist>" (any number per line);
# '#' or '%' starts an end-of-line comment (use it for human notes / reasons).
#   keypat    = a cite key | '*' (any entry) | '@type' (any entry of that type)
#   fieldlist = comma-separated: a field name | '@' (whole-entry replace) | '*' (all)
# Read from an in-file @comment block (travels with the .bib) and/or --ignore.
# Split on the LAST ':' (keys may contain ':'); an embedded key glob (e.g. hoge-*)
# is reserved -> a loud error, never a silent no-match.

IGNORE_BLOCK = re.compile(r"@comment\s*\{\s*dblpbib-ignore[ \t]*:?", re.I)

def _tokens(text):
    """Whitespace-separated selector tokens, with '#'/'%' to-end-of-line comments stripped."""
    return [t for line in text.splitlines() for t in line.split("#", 1)[0].split("%", 1)[0].split()]

def _parse_selector(tok):
    """One selector token -> (selector dict, None) or (None, error string)."""
    if ":" not in tok:
        return None, "ignore: missing ':' in %r" % tok
    keypat, fieldstr = tok.rsplit(":", 1)          # LAST ':' (a key may contain ':')
    if not keypat:
        return None, "ignore: empty key in %r" % tok
    if "*" in keypat and keypat != "*":
        return None, "ignore: partial key globs are reserved, not supported: %r" % tok
    items = [x for x in fieldstr.split(",") if x]
    if not items:
        return None, "ignore: no field/sentinel after ':' in %r" % tok
    return dict(keypat=keypat, items=items, sel=tok, count=0), None

def _selectors_from(toks):
    selectors, errors = [], []
    for tok in toks:
        sel, err = _parse_selector(tok)
        (errors.append(err) if err else selectors.append(sel))
    return selectors, errors

def parse_ignores(text):
    """Selectors from every @comment{dblpbib-ignore ...} block. Returns (selectors, errors)."""
    toks = []
    for m in IGNORE_BLOCK.finditer(text):
        toks += _tokens(text[m.end():_match_brace(text, text.index("{", m.start()))])
    return _selectors_from(toks)

def cli_ignores(specs):
    """Selectors from repeated --ignore SEL (same grammar). Returns (selectors, errors)."""
    return _selectors_from([t for spec in specs for t in _tokens(spec)])

def _first_entry_offset(text):
    """Offset of the first real bibliography entry, skipping leading implicit comments and
    any @comment / @string / @preamble blocks (brace-matched, so an @entry written inside a
    comment doesn't fool us). None if the file has no such entry."""
    i = 0
    for m in re.finditer(r"@(\w+)\s*\{", text):
        if m.start() < i:
            continue                               # inside a block we already skipped over
        if m.group(1).lower() in ("comment", "string", "preamble"):
            i = _match_brace(text, m.end() - 1) + 1
            continue
        return m.start()
    return None

def _write_ignores(text, sels):
    """Add selector tokens (one per line) to the @comment{dblpbib-ignore} block.  If the block
    is absent it is created just above the first entry -- below any leading comments / @string /
    @preamble -- so the file header stays on top.  Returns the new text (a minimal edit)."""
    add = "".join("  %s\n" % s for s in sels)
    m = IGNORE_BLOCK.search(text)
    if m:                                          # append before the existing block's close
        close = _match_brace(text, text.index("{", m.start()))
        head = text[:close]
        return head + ("" if head.endswith("\n") else "\n") + add + text[close:]
    block = "@comment{dblpbib-ignore\n" + add + "}\n"
    pos = _first_entry_offset(text)
    if pos is None:                                # no entries -> append at the end
        return text + ("" if not text or text.endswith("\n") else "\n") + "\n" + block
    ls = text.rfind("\n", 0, pos) + 1              # start of the first entry's line
    before = text[:ls]
    sep = "" if not before or before.endswith("\n\n") else "\n"
    return before + sep + block + "\n" + text[ls:]

def _match_entry(sel, key, etype):
    kp = sel["keypat"]
    if kp == "*":
        return True
    if kp.startswith("@"):
        return etype == kp[1:]
    return key == kp

def _match_item(item, p):
    if item == "*":
        return True
    if item == "@":
        return p["id"].rsplit(":", 1)[1] == "@"    # the whole-entry replace proposal
    return p["field"] == item

def apply_suppressions(findings, selectors):
    """Flag suppressed proposals (p['suppressed']) and silenced no-match nags
    (f['nag_suppressed']), and tally each selector's coverage (sel['count'])."""
    for s in selectors:
        s["count"] = 0
    for f in findings:
        for p in f.get("proposals", []):
            p.setdefault("suppressed", False)
        nag = f["status"] in ("fuzzy", "unknown")
        for s in selectors:
            if not _match_entry(s, f["key"], f.get("type", "")):
                continue
            for p in f.get("proposals", []):
                if any(_match_item(it, p) for it in s["items"]):
                    p["suppressed"] = True
                    s["count"] += 1
            if nag and "*" in s["items"] and not f.get("nag_suppressed"):
                f["nag_suppressed"] = True
                s["count"] += 1

def _visible(f):
    return [p for p in f.get("proposals", []) if not p.get("suppressed")]

def _eff_status(f):
    """The finding's status after suppression (only non-suppressed proposals count)."""
    vis = _visible(f)
    if any(p["op"] == "replace" for p in vis):
        return "upgrade"
    if any(p["op"] == "edit" and not p["review"] for p in vis):
        return "mismatch"
    if any(p["review"] for p in vis):
        return "review"
    if vis:
        return "incomplete"
    if f["status"] in ("fuzzy", "unknown") and not f.get("nag_suppressed"):
        return f["status"]
    return "ok"

def tally(findings):
    """Status counts using the post-suppression status, plus a `suppressed` total."""
    counts = dict(ok=0, mismatch=0, review=0, upgrade=0, incomplete=0, fuzzy=0,
                  unknown=0, suppressed=0)
    for f in findings:
        counts[_eff_status(f)] += 1
        counts["suppressed"] += sum(1 for p in f.get("proposals", []) if p.get("suppressed"))
        counts["suppressed"] += 1 if f.get("nag_suppressed") else 0
    return counts

# --- present / select / apply -------------------------------------------------

def _colorer(color):
    return ((lambda code, s: "\033[%sm%s\033[0m" % (code, s)) if color
            else (lambda code, s: s))

def _trunc(s, n=90):
    s = " ".join(str(s).split())
    return s if len(s) <= n else s[:n - 1] + "…"

# operation -> (glyph, label, ANSI colour). A review proposal overrides to a yellow "?".
OP = dict(add=("+", "add", "36"), edit=("~", "edit", "32"), replace=("»", "repl", "35"))

def _op_style(p):
    sym, label, col = OP[p["op"]]
    return ("?", label, "33") if p["review"] else (sym, label, col)

def _op_tag(p):
    return OP[p["op"]][1] + ("?" if p["review"] else "")    # add / edit / edit? / repl

def _summary(p):
    if p["op"] == "add":
        return "+ %s" % p["dblp"]
    return "%s → %s" % (p["bib"] or "(empty)", p["dblp"])

def render_show(f, p, color):
    """Return the full before/after block for one proposal (the fzf preview body)."""
    c = _colorer(color)
    sym, label, col = _op_style(p)
    note = ("  " + c("33" if p["review"] else "90", p["note"])) if p["note"] else ""
    out = ["%s  %s" % (c("1", f["title"]), c("90", f["key"])),
           "%s %s%s" % (c(col + ";1", "%s %s" % (sym, label)), c("1", p["field"]), note)]
    if p["op"] == "replace":
        out += ["  " + c("90", line) for line in p["replacement"].splitlines()]
    else:
        if p["bib"]:
            out.append("  %s %s" % (c("90", "bib "), c("31", p["bib"])))
        out.append("  %s %s" % (c("90", "dblp"), c("32", p["dblp"])))
    return "\n".join(out)

_ANSI = re.compile(r"\x1b\[[0-9;]*m")
_SKIP = ("●", "summary:", "ignore:")           # report header / footer lines carry no id

def _looks_like_id(tok):
    """True if `tok` is shaped like a proposal id: <key>:<field|@>, field lowercase-led."""
    key, sep, field = tok.rpartition(":")
    return bool(sep and key) and (field == "@" or bool(re.fullmatch(r"[a-z][\w.+-]*", field)))

def _line_id(line):
    """The proposal id in one line, tolerating decoration: the first token if it is an id
    (e.g. a hand-typed id), else the trailing id-shaped token (--oneline / report form); None
    for header / summary / blank lines. Lets either layout be piped to apply."""
    s = _ANSI.sub("", line).strip()
    if not s or s.startswith(_SKIP):
        return None
    toks = s.split()
    if _looks_like_id(toks[0]):
        return toks[0]
    hits = [t for t in toks if _looks_like_id(t)]
    return hits[-1] if hits else None

def _read_stdin_ids():
    """Proposal ids from stdin -- one per line where present, decoration ignored -- so you can
    pipe --oneline output OR the plain text report into --apply=stdin / --mute=stdin."""
    return [i for i in (_line_id(ln) for ln in sys.stdin) if i]

def _candidates(findings, safe):
    """Proposals offered for selection: non-suppressed, and -- with `safe` -- only the
    confident (non-review) ones."""
    return [p for p in all_proposals(findings)
            if not p.get("suppressed") and (not safe or not p["review"])]

def _pick(props, findings, color):
    """Interactive multi-select over `props` via fzf (with preview) or peco.  Returns the
    chosen ids, or None if no picker is installed (the caller treats that as an error)."""
    picker = shutil.which("fzf") or shutil.which("peco")
    if not picker:
        sys.stderr.write("interactive mode needs fzf or peco; or compose with a pipe: "
                         "dblpbib BIB --oneline | <picker> | dblpbib BIB --apply=stdin\n")
        return None
    is_fzf = os.path.basename(picker) == "fzf"
    fmap = {p["id"]: f for f in findings for p in f.get("proposals", [])}
    lines = []
    for p in props:
        row = "%s\t%s\t%s" % (p["id"], _op_tag(p), _trunc(_summary(p)))
        if is_fzf:   # embed the rendered preview (base64) -> fzf shows it with no re-invocation
            row += "\t" + base64.b64encode(render_show(fmap[p["id"]], p, True).encode()).decode()
        lines.append(row)
    cmd = ([picker, "-m", "--ansi", "--delimiter", "\t", "--with-nth", "2,3",
            "--preview", "printf %s {4} | base64 --decode", "--preview-window", "down,45%,wrap"]
           if is_fzf else [picker])
    sel = subprocess.run(cmd, input="\n".join(lines) + "\n", capture_output=True, text=True)
    return [re.split(r"\s+", ln.strip(), maxsplit=1)[0] for ln in sel.stdout.splitlines() if ln.strip()]

def _apply_ids(text, props, ids):
    """Apply the proposals named by `ids`. Returns (new_text, applied, missing). If a
    whole-entry upgrade is chosen, its field-level siblings are dropped (they'd conflict)."""
    pmap = {p["id"]: p for p in props}
    chosen = [pmap[i] for i in ids if i in pmap]
    missing = [i for i in ids if i not in pmap]
    replaced = {p["id"].rsplit(":", 1)[0] for p in chosen if p["op"] == "replace"}
    chosen = [p for p in chosen
              if p["op"] == "replace" or p["id"].rsplit(":", 1)[0] not in replaced]
    return apply_fixes(text, [p["edit"] for p in chosen]), chosen, missing

def cmd_oneline(findings, safe, color):
    """Flat one-proposal-per-line view: the same proposal lines as the report (op, field,
    before -> after, id trailing) but without entry-grouping headers or the summary.  Reads
    well, and pipes straight into fzf and on to --apply=stdin.  Suppressed proposals are
    omitted; with --safe only confident (non-review) proposals are listed."""
    c = _colorer(color)
    for p in _candidates(findings, safe):
        _print_proposal(c, p, indent="")
    return 0

def cmd_show(con, entries, short, idstr, color):
    """Full before/after for ONE proposal (the fzf --preview body; also handy alone). Only
    the entry named by the id is re-derived, so the preview stays fast on a large .bib."""
    key = idstr.rsplit(":", 1)[0]
    ent = next((e for e in entries if e.key == key), None)
    f = process_entry(con, ent, short) if ent else None
    p = next((q for q in f["proposals"] if q["id"] == idstr), None) if f else None
    if not p:
        print("no such proposal: %s" % idstr)
        return 1
    print(render_show(f, p, color))
    return 0

def _write_or_echo(text, applied, bib, dry_run):
    if dry_run:
        sys.stdout.write(text)
    elif applied:
        with open(bib, "w", encoding="utf-8") as f:
            f.write(text)

def cmd_apply(con, entries, text, short, mode, safe, bib, dry_run, color, selectors=()):
    """Accept proposals.  mode: 'interactive' (pick via fzf/peco), 'stdin' (ids piped in), or
    'all' (every candidate).  Candidates are non-suppressed (and, with `safe`, only non-review);
    an id named via stdin still applies even if it is suppressed (explicit overrides ignore)."""
    c = _colorer(color)
    log = sys.stderr if dry_run else sys.stdout
    findings = derive(con, entries, short, selectors)
    props = all_proposals(findings)
    if mode == "stdin":
        ids = _read_stdin_ids()
        sup = {p["id"] for p in props if p.get("suppressed")}
        for i in ids:
            if i in sup:
                print(c("33", "• overriding ignore for %s" % i), file=log)
    else:
        cands = _candidates(findings, safe)
        if not cands:
            print(c("90", "nothing to apply"), file=log)
            return 0
        ids = [p["id"] for p in cands] if mode == "all" else _pick(cands, findings, color)
        if ids is None:                          # no picker installed
            return 1
    new_text, applied, missing = _apply_ids(text, props, ids)
    for p in applied:
        print(c("32", "✓ %s" % p["id"]), file=log)
    for m in missing:
        print(c("33", "• no such proposal, skipped: %s" % m), file=log)
    if not applied:
        print(c("90", "nothing applied"), file=log)
    _write_or_echo(new_text, applied, bib, dry_run)
    return 0

def cmd_mute(con, entries, text, short, mode, safe, bib, dry_run, selectors, color):
    """Write proposals into the in-file @comment{dblpbib-ignore} block, silencing them for
    good.  mode: 'interactive' (pick via fzf/peco), 'stdin' (ids piped in), or 'all' (every
    candidate; with `safe`, only the confident/non-review ones)."""
    c = _colorer(color)
    log = sys.stderr if dry_run else sys.stdout
    findings = derive(con, entries, short, selectors)
    known = {p["id"] for p in all_proposals(findings)}
    if mode == "stdin":
        ids = []
        for i in _read_stdin_ids():
            if i in known:
                ids.append(i)
            else:
                print(c("33", "• no such proposal, skipped: %s" % i), file=log)
    else:
        cands = _candidates(findings, safe)
        if not cands:
            print(c("90", "nothing to mute"), file=log)
            return 0
        ids = [p["id"] for p in cands] if mode == "all" else _pick(cands, findings, color)
        if ids is None:                          # no picker installed
            return 1
    have = {s["sel"] for s in parse_ignores(text)[0]}              # dedup against the block
    add = [i for i in dict.fromkeys(ids) if i not in have]        # unique, order-preserving
    if not add:
        print(c("90", "nothing to mute"), file=log)
        return 0
    for s in add:
        print(c("32", "muted %s" % s), file=log)
    _write_or_echo(_write_ignores(text, add), add, bib, dry_run)
    return 0

# --- main ---------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="Check/fix a .bib file against dblp.db")
    ap.add_argument("bib")
    ap.add_argument("--db", default=os.environ.get("DBLP_DB", ""),
                    help="path to dblp.db (or set DBLP_DB)")
    MODES = ("interactive", "stdin", "all")
    ap.add_argument("--oneline", action="store_true",
                    help="flat one-proposal-per-line view of the report (no entry grouping or "
                         "summary); pipe into fzf/peco then --apply=stdin. Honours --safe")
    ap.add_argument("--show", metavar="ID", default=None,
                    help="show one proposal's full before/after (used as the fzf preview)")
    ap.add_argument("--apply", nargs="?", const="interactive", choices=MODES, metavar="MODE",
                    help="apply proposals. MODE: 'interactive' (default; pick via fzf/peco), "
                         "'stdin' (ids piped in -- from --oneline|fzf, or even the plain report), "
                         "or 'all'. With --safe, 'all'/'interactive' offer only confident "
                         "(non-review) proposals.")
    ap.add_argument("--mute", nargs="?", const="interactive", choices=MODES, metavar="MODE",
                    help="silence proposals for good by writing them into the in-file "
                         "@comment{dblpbib-ignore} block. Same MODEs as --apply.")
    ap.add_argument("--safe", action="store_true",
                    help="restrict the recommended set to confident (non-review) proposals "
                         "(affects --oneline and the 'all'/'interactive' modes of --apply/--mute)")
    ap.add_argument("--add", action="append", default=[], metavar="[KEY=]ID",
                    help="add an entry fetched from the DB by DOI / arXiv id / DBLP key "
                         "and append it to the .bib (repeatable; KEY sets the cite key, "
                         "otherwise one is generated). With -n, print to stdout instead.")
    ap.add_argument("--short", action="store_true",
                    help="prefer the short venue form (a journal's abbreviation, a "
                         "conference's 'Proc. {ACRONYM}') instead of the full official name; "
                         "venue proposals then offer shortening rather than expansion")
    ap.add_argument("-n", "--dry-run", action="store_true",
                    help="with --apply/--mute, write the result to stdout instead of the file")
    ap.add_argument("--ignore", action="append", default=[], metavar="SEL",
                    help="suppress proposals matching SEL (key:field[,field] / *:field / "
                         "@type:field); repeatable. Ephemeral; durable rules go in a "
                         "@comment{dblpbib-ignore ...} block in the .bib.")
    ap.add_argument("-a", "--show-suppressed", action="store_true",
                    help="also show suppressed (ignored) proposals, dimmed")
    ap.add_argument("--json", action="store_true", help="emit findings as JSON (report mode)")
    ap.add_argument("-v", "--verbose", action="store_true",
                    help="also list entries that are OK (hidden by default)")
    ap.add_argument("--no-color", action="store_true", help="disable ANSI colours")
    args = ap.parse_args()

    base = (not args.no_color) and "NO_COLOR" not in os.environ
    # --show feeds the fzf preview pane (not a tty), which renders ANSI -> keep colour there.
    color = base and (args.show is not None or sys.stdout.isatty())

    if not args.db or not os.path.exists(args.db):
        sys.stderr.write(
            "dblpbib: no dblp.db (set --db or DBLP_DB); skipping.\n"
            "  Build one with dblpsurvey to enable offline bib verification.\n")
        return 0

    con = sqlite3.connect(args.db)
    con.row_factory = sqlite3.Row
    with open(args.bib, encoding="utf-8") as f:
        text = f.read()
    entries = parse_bib(text)

    if args.add:
        return add_entries(con, args, text, entries, color)
    if args.show is not None:
        return cmd_show(con, entries, args.short, args.show, color)

    selectors, ig_err = parse_ignores(text)         # @comment{dblpbib-ignore ...} + --ignore
    cli_sel, cli_err = cli_ignores(args.ignore)
    selectors += cli_sel
    for e in ig_err + cli_err:
        sys.stderr.write("dblpbib: %s\n" % e)

    if args.oneline:
        return cmd_oneline(derive(con, entries, args.short, selectors), args.safe, color)
    if args.apply is not None:
        return cmd_apply(con, entries, text, args.short, args.apply, args.safe, args.bib,
                         args.dry_run, color, selectors)
    if args.mute is not None:
        return cmd_mute(con, entries, text, args.short, args.mute, args.safe, args.bib,
                        args.dry_run, selectors, color)

    # default: read-only report
    findings = derive(con, entries, args.short, selectors)
    counts = tally(findings)                        # statuses after suppression
    if args.json:
        print(json.dumps(dict(summary=counts, findings=findings),
                         ensure_ascii=False, indent=2))
    else:
        report(findings, selectors, counts, color, args.verbose, args.show_suppressed)
    return 1 if counts["mismatch"] else 0       # CI gate: confirmed (un-suppressed) errors fail

# finding status -> ANSI colour for the leading dot (severity at a glance).
STYLE = dict(ok="32", mismatch="31", review="33", upgrade="35",
             incomplete="36", fuzzy="90", unknown="90")

def _print_proposal(c, p, dim=False, indent="  "):
    """One proposal line (id trailing). `dim` greys out a suppressed proposal with a [ignored]
    tag; `indent` is the leading pad (the report indents under an entry header, --oneline does
    not)."""
    sym, op, col = _op_style(p)
    if dim:
        sym, col = "·", "90"
    head = "%s%s %s %s" % (indent, c(col + ";1", sym), c(col, "%-4s" % op),
                           c("90" if dim else "1", "%-10s" % p["field"]))
    note = "  " + c("90" if dim else "33", _trunc(p["note"], 48)) if p["note"] else ""
    tail = c("90", ("[ignored] " if dim else "") + p["id"])
    if p["op"] == "replace":
        print("%s%s  %s" % (head, note, tail))
    elif p["op"] == "add":
        print("%s %s  %s" % (head, c("90" if dim else "32", _trunc(p["dblp"], 64)), tail))
    else:                          # edit: before -> after (truncated; full text via --show)
        print("%s %s %s %s%s  %s"
              % (head, c("90" if dim else "31", _trunc(p["bib"], 32)), c("90", "→"),
                 c("90" if dim else "32", _trunc(p["dblp"], 48)), note, tail))

def report(findings, selectors, counts, color, verbose, show_suppressed):
    c = _colorer(color)
    for f in findings:
        eff = _eff_status(f)
        vis = _visible(f)
        supp = [p for p in f.get("proposals", []) if p.get("suppressed")]
        nag_live = f["status"] in ("fuzzy", "unknown") and not f.get("nag_suppressed")
        hidden = supp or f.get("nag_suppressed")
        if not vis and not nag_live and not (show_suppressed and hidden) and not (verbose and eff == "ok"):
            continue                               # nothing left to show for this entry
        print("%s %s  %s" % (c(STYLE[eff], "●"), c("1", f["title"]), c("90", f["key"])))
        for p in vis:
            _print_proposal(c, p)
        if show_suppressed:
            for p in supp:
                _print_proposal(c, p, dim=True)
        if nag_live or (show_suppressed and f.get("nag_suppressed")):
            for cand in f.get("candidates", []):
                print("  %s %s %-8s %s"
                      % (c("90", "~"), cand["year"], cand["venue"], cand["title"]))
            tag = "" if nag_live else c("90", " [ignored]")
            print("  %s%s" % (c("90", "(no exact match — may be out of DB scope; check manually)"), tag))
    for s in selectors:                            # stale ignores (unused-noqa equivalent)
        if s["count"] == 0:
            print(c("33", "ignore: matched nothing (stale): %s" % s["sel"]))
    if show_suppressed:
        for s in selectors:
            if s["count"]:
                print(c("90", "ignore: %s → %d" % (s["sel"], s["count"])))
    parts = [("%d %s" % (counts[k], k)) for k in
             ("ok", "mismatch", "review", "upgrade", "incomplete", "fuzzy", "unknown", "suppressed")
             if counts[k]]
    print("\n%s %s" % (c("1", "summary:"), ", ".join(parts) or "nothing to check"))
    if any(counts[k] for k in ("mismatch", "review", "incomplete", "upgrade")):
        print(c("90", "  apply: dblpbib BIB --apply   ·   silence: dblpbib BIB --mute"))

if __name__ == "__main__":
    sys.exit(main())
