"""dblpdb -- the dblp.db domain layer: normalise/format dblp fields, resolve venue
names, and serialise a DB row to BibTeX.

Shared by dblpbib (the .bib linter, which also serialises a row for an arXiv->published
swap) and, in time, the BibTeX-emitting side of dblpsurvey -- so the rule for turning a
DBLP record into a citation lives in ONE place, not inside either CLI.  Functions take an
open sqlite3 connection (`con`); profile/DB-path resolution stays in the callers.
"""

import re
import unicodedata


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

def _vn(s):
    """Normalize a venue string for comparison (ASCII alnum, lowercased)."""
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())

def venue_forms(con, row):
    """Return (full, kind, full_forms, short_forms, short) for a row's venue.

    `full` is the expanded official name; `short` is the abbreviated form.  These are the
    two proposal targets: the default target is `full`, the --short target is `short`.
    `full_forms`/`short_forms` are the normalized spellings already acceptable on each
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
    """Serialize a DB row as a BibTeX entry, keeping the user's citation key and
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
