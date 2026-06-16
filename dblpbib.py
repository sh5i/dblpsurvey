#!/usr/bin/env python3
"""Check and fix a .bib file against a local DBLP database (dblpsurvey's dblp.db).

For each @article / @inproceedings (and arXiv @misc) it looks the title up in the DB
-- falling back to DOI, then arXiv id -- and proposes changes: authoritative fields
(author, year, pages, volume, number) corrected from the DB, missing fields filled,
venue names and a differing DOI offered too.  Nothing is auto-applied: you choose.

The fix model is select-then-apply (like `git add -p`).  Every proposed change has a
stable id `citekey:field` (or `citekey:@` for an arXiv->published whole-entry swap):

  dblpbib refs.bib                       # read-only report (each proposal shows its id)
  dblpbib refs.bib --plan                # one proposal/line: id <tab> kind <tab> summary
  dblpbib refs.bib --show ID             # one proposal's full before/after (fzf preview)
  dblpbib refs.bib --pick                # choose via fzf (preview) / peco, then apply
  dblpbib refs.bib --plan | fzf -m | dblpbib refs.bib --apply -    # the composable form
  dblpbib refs.bib --apply key:year,key:pages    # apply named ids
  dblpbib refs.bib --apply safe          # non-interactive: all confirmed fixes + fills
  dblpbib refs.bib --add 10.1145/xxxxxxx # fetch an entry from the DB and append it

The DB path may be given via --db or the DBLP_DB environment variable.  A missing
database is treated as "skip" (exit 0) so it never blocks a build.
"""

import argparse
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

def compare(con, entry, row, short=False):
    """Classify each field difference into one of three tiers:
      fills   -- field absent from the .bib; safe to add
      fixes   -- the .bib is wrong in a direction DBLP corrects
      reviews -- a real difference whose direction is the author's call (venue name,
                 a differing DOI, author/given-name detail, ambiguous pages)
    Each review carries a note. process_entry turns all three into selectable proposals;
    the tier only becomes the proposal's `kind` (fix / fill / venue / review)."""
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

    # venue name: fill if missing, else propose the official name as a REVIEW-tier
    # proposal (the venue string has no single right form, so the author chooses).
    if entry.type == "article":
        cur, db = entry.get("journal"), row["journal"]
        if db:
            if not cur:
                fills.append(("journal", db))
            elif _vn(cur) != _vn(db):
                reviews.append(("journal", cur, db, "official journal name"))
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
                reviews.append(("booktitle", cur, target,
                                "short venue form" if short else "official venue name"))

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

def _proposal(e, kind, field, bib, dblp, note=""):
    return dict(id="%s:%s" % (e.key, field), kind=kind, field=field, bib=bib, dblp=dblp,
                note=note, edit=_edit_for(e, field, dblp))

def process_entry(con, e, short=False):
    """Classify one .bib entry against DBLP. Returns a finding dict, or None to skip.
    A matched finding carries `proposals`: each a selectable change with a stable id
    (`citekey:field`, or `citekey:@` for an arXiv->published whole-entry swap), a `kind`
    (fix / fill / venue / review / upgrade), before/after values and a surgical edit.
    `status` summarises the finding; an unmatched one carries fuzzy `candidates`."""
    if e.type == "misc" and not is_arxiv(e):
        return None                            # non-paper @misc (datasets, manuals): skip
    title = e.get("title")
    pub, pre, matched_by = match_record(con, e)

    if not (pub or pre):                       # no match -> fuzzy candidates, else unknown
        cands = fuzzy(con, title)
        return dict(key=e.key, status=("fuzzy" if cands else "unknown"), title=title,
                    matched_by=None, proposals=[],
                    candidates=[dict(year=c["year"], venue=c["venue"], title=c["title"])
                                for c in cands])

    # An arXiv-form entry that is now formally published -> a whole-entry swap proposal.
    published = suggest_publication(e, pub, pre)
    if published:
        kind = venue_forms(con, published)[1]
        tag = "" if kind in ("main", "journal") else " [%s]" % kind
        vshort = published["booktitle"] or published["journal"]
        repl = to_bibtex(con, e.key, published, short)
        prop = dict(id="%s:@" % e.key, kind="upgrade", field="@entry", bib="@" + e.type,
                    dblp="%s %s%s (%s)" % (vshort, published["year"], tag, published["key"]),
                    note="now published — replace the whole entry",
                    replacement=repl, edit=(e.start, e.body_close + 1, repl))
        return dict(key=e.key, status="upgrade", title=title, matched_by=matched_by,
                    proposals=[prop], candidates=[])

    # Otherwise verify against the canonical record (published if available, else arXiv).
    row = pub[0] if pub else pre[0]
    fixes, reviews, fills = compare(con, e, row, short)
    if matched_by in ("doi", "arxiv"):         # matched by id -> the title disagrees
        reviews.insert(0, ("title", title, row["title"],
                           "matched by %s but the title differs — an arXiv title may have "
                           "changed across versions, or a typo / wrong key" % matched_by.upper()))
    props = [_proposal(e, "fix", f, a, b) for f, a, b in fixes]
    props += [_proposal(e, "venue" if f in ("booktitle", "journal") else "review", f, a, b, n)
              for f, a, b, n in reviews]
    props += [_proposal(e, "fill", f, "", b) for f, b in fills]
    status = ("mismatch" if any(p["kind"] == "fix" for p in props)
              else "review" if any(p["kind"] in ("review", "venue") for p in props)
              else "incomplete" if props else "ok")
    return dict(key=e.key, status=status, title=title, matched_by=matched_by,
                proposals=props, candidates=[])

def derive(con, entries, short=False):
    """All findings for the parsed entries (skipped entries dropped)."""
    return [f for f in (process_entry(con, e, short) for e in entries) if f is not None]

def all_proposals(findings):
    return [p for f in findings for p in f.get("proposals", [])]

# --- present / select / apply -------------------------------------------------

def _colorer(color):
    return ((lambda code, s: "\033[%sm%s\033[0m" % (code, s)) if color
            else (lambda code, s: s))

def _trunc(s, n=90):
    s = " ".join(str(s).split())
    return s if len(s) <= n else s[:n - 1] + "…"

def _summary(p):
    if p["kind"] == "fill":
        return "+ %s" % p["dblp"]
    return "%s → %s" % (p["bib"] or "(empty)", p["dblp"])

def _read_ids(spec):
    """IDs for --apply: '-' reads the leading token of each stdin line (fzf/peco output),
    otherwise a comma-separated list."""
    if spec == "-":
        return [re.split(r"\s+", ln.strip(), 1)[0] for ln in sys.stdin if ln.strip()]
    return [s.strip() for s in spec.split(",") if s.strip()]

def _apply_ids(text, props, ids):
    """Apply the proposals named by `ids`. Returns (new_text, applied, missing). If a
    whole-entry upgrade is chosen, its field-level siblings are dropped (they'd conflict)."""
    pmap = {p["id"]: p for p in props}
    chosen = [pmap[i] for i in ids if i in pmap]
    missing = [i for i in ids if i not in pmap]
    upgraded = {p["id"].rsplit(":", 1)[0] for p in chosen if p["kind"] == "upgrade"}
    chosen = [p for p in chosen
              if p["kind"] == "upgrade" or p["id"].rsplit(":", 1)[0] not in upgraded]
    return apply_fixes(text, [p["edit"] for p in chosen]), chosen, missing

def cmd_plan(findings):
    """One selectable proposal per line: id <TAB> kind <TAB> summary. Plain text so it
    pipes cleanly into fzf/peco (the id is the first field, read back by --apply/preview)."""
    for p in all_proposals(findings):
        print("%s\t%s\t%s" % (p["id"], p["kind"], _trunc(_summary(p))))
    return 0

def cmd_show(findings, idstr, color):
    """Full before/after for one proposal (used as the fzf --preview; also handy alone)."""
    c = _colorer(color)
    pmap = {p["id"]: (f, p) for f in findings for p in f.get("proposals", [])}
    if idstr not in pmap:
        print("no such proposal: %s" % idstr)
        return 1
    f, p = pmap[idstr]
    kc = KIND_C.get(p["kind"], "0")
    print("%s  %s" % (c("1", "[%s]" % f["key"]), f["title"]))
    print("%s %s%s" % (c(kc + ";1", p["kind"]), c("1", p["field"]),
                       "  " + c("90", p["note"]) if p["note"] else ""))
    if p["kind"] == "upgrade":
        for line in p["replacement"].splitlines():
            print("  " + c("34", line))
    else:
        if p["bib"]:
            print("  - bib : %s" % c("31", p["bib"]))
        print("  + dblp: %s" % c("32", p["dblp"]))
    return 0

def _write_or_echo(text, applied, bib, dry_run):
    if dry_run:
        sys.stdout.write(text)
    elif applied:
        with open(bib, "w", encoding="utf-8") as f:
            f.write(text)

def cmd_apply(con, entries, text, short, spec, bib, dry_run, color):
    c = _colorer(color)
    props = all_proposals(derive(con, entries, short))
    if spec in ("safe", "all"):
        ids = [p["id"] for p in props if spec == "all" or p["kind"] in ("fix", "fill")]
    else:
        ids = _read_ids(spec)
    new_text, applied, missing = _apply_ids(text, props, ids)
    log = sys.stderr if dry_run else sys.stdout
    for p in applied:
        print(c("32", "✓ %s" % p["id"]), file=log)
    for m in missing:
        print(c("33", "• no such proposal, skipped: %s" % m), file=log)
    if not applied:
        print(c("90", "nothing applied"), file=log)
    _write_or_echo(new_text, applied, bib, dry_run)
    return 0

def cmd_pick(con, entries, text, short, bib, db, dry_run, color):
    props = all_proposals(derive(con, entries, short))
    if not props:
        sys.stderr.write("nothing to pick (no proposals)\n")
        return 0
    plan = "".join("%s\t%s\t%s\n" % (p["id"], p["kind"], _trunc(_summary(p))) for p in props)
    picker = shutil.which("fzf") or shutil.which("peco")
    if not picker:
        sys.stderr.write("--pick needs fzf or peco; or compose: "
                         "dblpbib BIB --plan | <picker> | dblpbib BIB --apply -\n")
        return 1
    if os.path.basename(picker) == "fzf":
        prog = "%s %s" % (shlex.quote(sys.executable), shlex.quote(os.path.abspath(__file__)))
        preview = "%s %s%s --show {1}" % (prog, shlex.quote(bib),
                                          (" --db " + shlex.quote(db)) if db else "")
        cmd = [picker, "-m", "--delimiter", "\t", "--with-nth", "2,3", "--preview", preview]
    else:
        cmd = [picker]
    sel = subprocess.run(cmd, input=plan, capture_output=True, text=True)
    ids = [re.split(r"\s+", ln.strip(), 1)[0] for ln in sel.stdout.splitlines() if ln.strip()]
    if not ids:
        sys.stderr.write("no selection\n")
        return 0
    new_text, applied, _ = _apply_ids(text, props, ids)
    c = _colorer(color)
    for p in applied:
        print(c("32", "✓ %s" % p["id"]), file=(sys.stderr if dry_run else sys.stdout))
    _write_or_echo(new_text, applied, bib, dry_run)
    return 0

# --- main ---------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="Check/fix a .bib file against dblp.db")
    ap.add_argument("bib")
    ap.add_argument("--db", default=os.environ.get("DBLP_DB", ""),
                    help="path to dblp.db (or set DBLP_DB)")
    ap.add_argument("--plan", action="store_true",
                    help="list selectable proposals, one per line "
                         "(id <tab> kind <tab> summary); pipe into fzf/peco")
    ap.add_argument("--show", metavar="ID", default=None,
                    help="show one proposal's full before/after (used as the fzf preview)")
    ap.add_argument("--apply", metavar="IDS", nargs="?", const="-", default=None,
                    help="apply proposals: a comma-separated id list; '-' (or no value) to "
                         "read ids from stdin (fzf/peco output); or 'safe' (all fix+fill) / 'all'")
    ap.add_argument("--pick", action="store_true",
                    help="choose proposals interactively via fzf (with preview) or peco, then apply")
    ap.add_argument("--add", action="append", default=[], metavar="[KEY=]ID",
                    help="add an entry fetched from the DB by DOI / arXiv id / DBLP key "
                         "and append it to the .bib (repeatable; KEY sets the cite key, "
                         "otherwise one is generated). With -n, print to stdout instead.")
    ap.add_argument("--short", action="store_true",
                    help="use the short venue form 'Proc. {ACRONYM}' for venue proposals "
                         "and added entries, instead of the full official proceedings title")
    ap.add_argument("-n", "--dry-run", action="store_true",
                    help="with --apply/--pick, write the result to stdout instead of the file")
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
        return cmd_show(derive(con, entries, args.short), args.show, color)
    if args.plan:
        return cmd_plan(derive(con, entries, args.short))
    if args.apply is not None:
        return cmd_apply(con, entries, text, args.short, args.apply, args.bib,
                         args.dry_run, color)
    if args.pick:
        return cmd_pick(con, entries, text, args.short, args.bib, args.db,
                        args.dry_run, color)

    # default: read-only report
    findings = derive(con, entries, args.short)
    counts = dict(ok=0, mismatch=0, review=0, upgrade=0, incomplete=0, fuzzy=0, unknown=0)
    for f in findings:
        counts[f["status"]] += 1
    if args.json:
        print(json.dumps(dict(summary=counts, findings=findings),
                         ensure_ascii=False, indent=2))
    else:
        report(findings, counts, color, args.verbose)
    return 1 if counts["mismatch"] else 0       # CI gate: confirmed errors fail

# status -> (emoji, label, ANSI colour code); proposal kind -> ANSI colour code
STYLE = dict(
    ok=("✅", "OK", "32"), mismatch=("❌", "MISMATCH", "31"),
    review=("⚠️ ", "REVIEW", "33"), upgrade=("⬆️ ", "UPGRADE", "34"),
    incomplete=("➕", "INCOMPLETE", "36"), fuzzy=("🔍", "FUZZY", "35"),
    unknown=("❓", "UNKNOWN", "90"))
KIND_C = dict(fix="31", venue="33", review="33", fill="36", upgrade="34")

def report(findings, counts, color, verbose):
    c = _colorer(color)
    for f in findings:
        if f["status"] == "ok" and not verbose:    # OK entries shown only with -v
            continue
        emoji, label, code = STYLE[f["status"]]
        print("%s %s  %s" % (emoji, c(code + ";1", label), c("1", "[%s]" % f["key"])))
        print("   %s" % f["title"])                       # full title, never truncated
        for p in f.get("proposals", []):                  # each proposal shows its id
            kc = KIND_C.get(p["kind"], "0")
            print("   %s %s  %s%s" % (c(kc, "•"), c("1", p["field"]), c("90", p["id"]),
                                      "  " + c(kc, p["note"]) if p["note"] else ""))
            if p["kind"] == "upgrade":
                for line in (p.get("replacement") or "").splitlines():
                    print("       %s" % c("34", line))
            else:
                if p["bib"]:
                    print("       bib : %s" % c("31", p["bib"]))
                print("       dblp: %s" % c("32", p["dblp"]))
        for cand in f.get("candidates", []):
            print("   %s %s %-8s %s"
                  % (c("90", "~"), cand["year"], cand["venue"], cand["title"]))
        if f["status"] in ("fuzzy", "unknown"):
            print(c("90", "     (no exact match — may be out of DB scope; check manually)"))
    parts = [("%d %s" % (counts[k], STYLE[k][1].lower())) for k in
             ("ok", "mismatch", "review", "upgrade", "incomplete", "fuzzy", "unknown")]
    print("\n%s %s" % (c("1", "summary:"), ", ".join(parts)))
    if any(counts[k] for k in ("mismatch", "review", "incomplete", "upgrade")):
        print(c("90", "  choose fixes: dblpbib %s --pick   (or --plan | fzf -m | … --apply -)"
                      % "BIB"))

if __name__ == "__main__":
    sys.exit(main())
