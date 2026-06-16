#!/usr/bin/env python3
"""Check a .bib file against a local DBLP database (dblpsurvey's dblp.db).

For each @article / @inproceedings entry it looks the title up in the database
and reports discrepancies.  Authoritative fields (author, year, pages, volume,
number, doi) are treated as "the database is right -- your .bib may be lying";
venue names (booktitle, journal) are filled in when missing but otherwise left
as-is, because there is no single canonical venue string and in-document
consistency matters; a differing booktitle is rewritten to the official
proceedings name only when --fix-venue is given.

  - exact match (normalised title) -> compare fields, warn on mismatch
  - preprint matched but a published version exists -> suggest an upgrade
  - no exact match -> offer fuzzy (FTS5) candidates
  - nothing -> UNKNOWN, hand off to a human / agent

Default is read-only reporting.  With --fix the authoritative fields are
rewritten in place (venue names left as-is, only filled when missing); add
--fix-venue to also rewrite a differing booktitle to the official proceedings
name.  Run `make format-bib` afterwards to normalise the style.

Usage:
  dblpbib.py [--db PATH] [--fix] [--json] references.bib
The database path may also be given via the DBLP_DB environment variable.
A missing database is treated as "skip" (exit 0) so it never blocks a build.
"""

import argparse
import collections
import contextlib
import json
import os
import re
import sqlite3
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
    """Return (full_name, kind, accepted, acronym) for a row's venue.  For
    inproceedings, follow the crossref to `proceedings` for the full official name
    (e.g. 'Proceedings of the 47th ... (ICSE 2025)'), its kind (main/workshop/...),
    the set of already-acceptable normalised forms (full / series / acronym) and the
    bare acronym (e.g. 'ICSE', volume suffixes stripped).  For articles, the journal."""
    if row["type"] != "inproceedings":
        j = row["journal"]
        return j, "journal", ({_vn(j)} if j else set()), j
    xref = row["crossref"] if "crossref" in row.keys() else ""
    short, cano, conf, kind = row["booktitle"], "", "", "main"
    if xref:
        p = con.execute("SELECT canonical, conf_name, booktitle, kind "
                        "FROM proceedings WHERE key=?", (xref,)).fetchone()
        if p:
            cano, conf = p["canonical"] or "", p["conf_name"] or ""
            short = p["booktitle"] or short
            kind = p["kind"] or "main"
    name = cano or conf or short
    acronym = re.sub(r"\s*\([^)]*\)\s*$", "", short).strip()   # 'ICSE (1)' -> 'ICSE'
    accepted = {_vn(x) for x in (cano, conf, short, acronym) if x}
    return name, kind, accepted, acronym

def to_bibtex(con, citekey, row, short=False):
    """Serialise a DB row as a BibTeX entry, keeping the user's citation key and
    using the full official venue name (or 'Proc. {ACRONYM}' when short)."""
    fields = [("author", authors_to_bib(row["authors"])), ("title", row["title"])]
    if row["type"] == "inproceedings":
        name, _kind, _acc, acronym = venue_forms(con, row)
        fields.append(("booktitle", "Proc. {%s}" % acronym if (short and acronym) else name))
    else:
        fields += [("journal", row["journal"]),
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

def compare(con, entry, row, short=False, fix_venue=False):
    """Classify each field difference into one of three tiers:
      fills   -- field absent from the .bib; safe to add          (--fix adds)
      fixes   -- the .bib is wrong in a direction DBLP corrects    (--fix overwrites)
      reviews -- a real difference, but auto-fixing is unsafe      (warn only)
                 (direction unclear / the .bib is more detailed / not comparable)
    Each review carries a note explaining why it needs a human.
    With fix_venue, a differing booktitle is treated as a fix (official venue name)
    rather than a review."""
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

    # venue name: fill if missing, else recommend the official name as a REVIEW (never
    # auto-corrected -- the venue string has no single right form and in-document
    # consistency is the author's call).
    if entry.type == "article":
        cur, db = entry.get("journal"), row["journal"]
        if db:
            if not cur:
                fills.append(("journal", db))
            elif _vn(cur) != _vn(db):
                reviews.append(("journal", cur, db,
                                "consider the official journal name (kept as is)"))
    elif row["type"] == "inproceedings":
        name, _kind, accepted, acronym = venue_forms(con, row)
        target = "Proc. {%s}" % acronym if (short and acronym) else name
        if short and acronym:
            accepted = accepted | {_vn(target)}    # 'Proc. {ICSE}' is fine in short mode
        cur = entry.get("booktitle")
        if name:
            if not cur:
                fills.append(("booktitle", target))
            elif _vn(cur) not in accepted:
                if fix_venue:
                    fixes.append(("booktitle", cur, target))   # rewrite to the official name
                else:
                    note = ("consider the short venue form" if short
                            else "consider the official venue name") + " (kept as is)"
                    reviews.append(("booktitle", cur, target, note))

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

# Run-wide options derived from the CLI, threaded into process_entry.
Opts = collections.namedtuple("Opts", "do_fix aggressive fix_venue short allow")

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

def process_entry(con, e, opts):
    """Classify one .bib entry against DBLP. Returns (finding, edits): `finding` is a
    result dict (status plus fixes/reviews/fills, or fuzzy candidates), or None if the
    entry is skipped; `edits` are the surgical (start, end, repl) tuples --fix applies."""
    if e.type == "misc" and not is_arxiv(e):
        return None, []                       # non-paper @misc (datasets, manuals): skip
    title = e.get("title")
    pub, pre, matched_by = match_record(con, e)

    if not (pub or pre):                       # no match -> fuzzy candidates, else unknown
        if opts.allow is not None:             # --key scopes to matched entries only
            return None, []
        cands = fuzzy(con, title)
        return dict(key=e.key, status=("fuzzy" if cands else "unknown"), title=title,
                    candidates=[dict(year=c["year"], venue=c["venue"], title=c["title"])
                                for c in cands]), []

    # An arXiv-form entry that is now formally published -> suggest a whole-entry swap.
    published = suggest_publication(e, pub, pre)
    if published and opts.allow is None:
        vshort = published["booktitle"] or published["journal"]
        kind = venue_forms(con, published)[1]
        tag = "" if kind in ("main", "journal") else " [%s]" % kind
        upgrade = "%s %s%s (%s)" % (vshort, published["year"], tag, published["key"])
        replacement = to_bibtex(con, e.key, published, opts.short)
        edits = [(e.start, e.body_close + 1, replacement)] if opts.aggressive else []
        return dict(key=e.key, status="upgrade", title=title, matched_by=matched_by,
                    dblp=published["key"], venue=published["venue"],
                    year=published["year"], fixes=[], reviews=[], fills=[],
                    upgrade=upgrade, replacement=replacement), edits

    # Otherwise verify against the canonical record (published if available, else arXiv).
    row = pub[0] if pub else pre[0]
    fixes, reviews, fills = compare(con, e, row, opts.short, opts.fix_venue)
    if matched_by in ("doi", "arxiv"):         # matched by id -> the title disagrees
        reviews.insert(0, ("title", title, row["title"],
                           "matched by %s but the title differs — an arXiv title may have "
                           "changed across versions, or a typo / wrong key" % matched_by.upper()))
    if opts.allow is not None:                  # --key: keep only the named fields
        fixes = [d for d in fixes if d[0] in opts.allow]
        reviews = [d for d in reviews if d[0] in opts.allow]
        fills = [d for d in fills if d[0] in opts.allow]
    status = ("mismatch" if fixes else "review" if reviews
              else "incomplete" if fills else "ok")
    finding = dict(key=e.key, status=status, title=title, matched_by=matched_by,
                   dblp=row["key"], venue=row["venue"], year=row["year"],
                   fixes=[dict(field=f, bib=a, dblp=b) for f, a, b in fixes],
                   reviews=[dict(field=f, bib=a, dblp=b, note=n) for f, a, b, n in reviews],
                   fills=[dict(field=f, dblp=b) for f, b in fills],
                   upgrade=None, replacement=None)
    edits = []
    if opts.do_fix:                            # apply fixes + fills (already key-filtered)
        for f, _cur, dbval in fixes:
            fld = e.fields[f]
            edits.append((fld.start, fld.end, "{%s}" % dbval))
        for f, dbval in fills:
            edits.append((e.last_field_end(), e.last_field_end(), ",\n  %s = {%s}" % (f, dbval)))
    return finding, edits

# --- main ---------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="Check a .bib file against dblp.db")
    ap.add_argument("bib")
    ap.add_argument("--db", default=os.environ.get("DBLP_DB", ""),
                    help="path to dblp.db (or set DBLP_DB)")
    ap.add_argument("--fix", nargs="?", const="normal", default=None, metavar="LEVEL",
                    help="apply fixes in place. --fix: confirmed field fixes and fills "
                         "only. --fix=aggressive: also rewrite whole entries (e.g. swap "
                         "an arXiv preprint for its published version, changing @type)")
    ap.add_argument("--key", default="", metavar="FIELDS",
                    help="restrict field-level fixes to a comma-separated list, "
                         "e.g. --key=doi,pages (default: all fields)")
    ap.add_argument("--add", action="append", default=[], metavar="[KEY=]ID",
                    help="add an entry fetched from the DB by DOI / arXiv id / DBLP key "
                         "and append it to the .bib (repeatable; KEY sets the cite key, "
                         "otherwise one is generated). With -n, print to stdout instead.")
    ap.add_argument("--short", action="store_true",
                    help="recommend/write venue names in short form 'Proc. {ACRONYM}' "
                         "instead of the full official proceedings title")
    ap.add_argument("--fix-venue", action="store_true",
                    help="with --fix, also rewrite a differing booktitle to the official "
                         "proceedings name (venue strings are otherwise left to the author)")
    ap.add_argument("-n", "--dry-run", action="store_true",
                    help="with --fix, write the fixed .bib to stdout instead of editing "
                         "the file (the report goes to stderr)")
    ap.add_argument("--json", action="store_true", help="emit findings as JSON")
    ap.add_argument("-v", "--verbose", action="store_true",
                    help="also list entries that are OK (hidden by default)")
    ap.add_argument("--no-color", action="store_true", help="disable ANSI colours")
    args = ap.parse_args()

    do_fix = args.fix is not None
    aggressive = do_fix and args.fix.lower().startswith("agg")   # --fix=aggressive
    fix_venue = do_fix and args.fix_venue                        # --fix --fix-venue
    # In dry-run the fixed .bib owns stdout, so the report is routed to stderr.
    report_stream = sys.stderr if (do_fix and args.dry_run) else sys.stdout
    color = (not args.no_color) and report_stream.isatty() and "NO_COLOR" not in os.environ
    # --key limits which fields are considered at all (report and fix); None = all.
    allow = set(s.strip().lower() for s in args.key.split(",") if s.strip()) or None

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

    opts = Opts(do_fix, aggressive, fix_venue, args.short, allow)
    findings, edits = [], []
    counts = dict(ok=0, mismatch=0, review=0, upgrade=0, incomplete=0, fuzzy=0, unknown=0)
    for e in entries:
        finding, ed = process_entry(con, e, opts)
        if finding is None:
            continue
        findings.append(finding)
        counts[finding["status"]] += 1
        edits.extend(ed)

    new_text = apply_fixes(text, edits) if (do_fix and edits) else text
    if do_fix and edits and not args.dry_run:
        with open(args.bib, "w", encoding="utf-8") as f:
            f.write(new_text)

    if args.json:
        print(json.dumps(dict(summary=counts, findings=findings),
                         ensure_ascii=False, indent=2), file=report_stream)
    else:
        with contextlib.redirect_stdout(report_stream):
            report(findings, counts, do_fix, color, args.verbose)

    if do_fix and args.dry_run:              # emit the would-be file to stdout
        sys.stdout.write(new_text)

    # In report mode, exit non-zero on confirmed errors (CI gate); reviews/upgrades
    # are advisory.  In --fix mode we just performed the action, so exit 0.
    return 0 if do_fix else (1 if counts["mismatch"] else 0)

# status -> (emoji, label, ANSI colour code)
STYLE = dict(
    ok=("✅", "OK", "32"), mismatch=("❌", "MISMATCH", "31"),
    review=("⚠️ ", "REVIEW", "33"), upgrade=("⬆️ ", "UPGRADE", "34"),
    incomplete=("➕", "INCOMPLETE", "36"), fuzzy=("🔍", "FUZZY", "35"),
    unknown=("❓", "UNKNOWN", "90"))

def report(findings, counts, fixed, color, verbose):
    def c(code, s):
        return "\033[%sm%s\033[0m" % (code, s) if color else s
    for f in findings:
        if f["status"] == "ok" and not verbose:    # OK entries shown only with -v
            continue
        emoji, label, code = STYLE[f["status"]]
        print("%s %s  %s" % (emoji, c(code + ";1", label), c("1", "[%s]" % f["key"])))
        print("   %s" % f["title"])                       # full title, never truncated
        for d in f.get("fixes", []):                       # red: will be corrected
            print("   %s %s" % (c("31", "✗"), c("1", d["field"])))
            print("       bib : %s" % c("31", d["bib"]))
            print("       dblp: %s" % c("32", d["dblp"]))
        for d in f.get("reviews", []):                     # yellow: human decides
            print("   %s %s  %s" % (c("33", "⚠"), c("1", d["field"]), c("33", d["note"])))
            print("       bib : %s" % d["bib"])
            print("       dblp: %s" % d["dblp"])
        for fl in f.get("fills", []):                      # cyan: missing, can be added
            print("   %s %s %s" % (c("36", "+"), c("1", fl["field"]), c("90", "(missing)")))
            print("       dblp: %s" % c("32", fl["dblp"]))
        if f.get("upgrade"):
            print("   %s" % c("34", "⬆ now published: %s — consider replacing with:"
                                    % f["upgrade"]))
            for line in (f.get("replacement") or "").splitlines():
                print("       %s" % c("34", line))
        for cand in f.get("candidates", []):
            print("   %s %s %-8s %s"
                  % (c("90", "~"), cand["year"], cand["venue"], cand["title"]))
        if f["status"] in ("fuzzy", "unknown"):
            print(c("90", "     (no exact match — may be out of DB scope; check manually)"))
    s, tail = counts, " (fixed in place)" if fixed else ""
    parts = [("%d %s" % (s[k], STYLE[k][1].lower())) for k in
             ("ok", "mismatch", "review", "upgrade", "incomplete", "fuzzy", "unknown")]
    print("\n%s %s%s" % (c("1", "summary:"), ", ".join(parts), tail))

if __name__ == "__main__":
    sys.exit(main())
