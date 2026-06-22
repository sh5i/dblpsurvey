"""dblpdb -- the dblp.db domain layer.

`Db` is a repository over an open dblp.db connection (look records up); `DblpEntry` is a
single `entries` row with the behaviour to read, compare and serialise it (the .bib side is
bibspan's `Entry`; this is the DBLP side).  The free functions are the pure value transforms
(field formatting, name folding) the two classes -- and dblplint's matching -- share.

Used by dblplint (the .bib linter) and, in time, the BibTeX-emitting side of dblpsurvey, so
the rule for turning a DBLP record into a citation lives in ONE place, not inside either CLI.
"""

import re
import sqlite3
import unicodedata


# --- pure value transforms ----------------------------------------------------

def norm_title(s):
    """Title key: keep ASCII [A-Za-z0-9], lowercased -- same rule as dblp.db."""
    return re.sub(r"[^A-Za-z0-9]", "", s).lower()

def norm_pages(s):
    """Reduce a page field to its digit groups joined by '-' (1--12 == 1-12)."""
    return "-".join(re.findall(r"\d+", s or ""))

def fmt_pages(s):
    """House style for a page range: two dash-separated numbers joined by '--' (per bib-guide).
    Only a real range ('1-12', '1--12') collapses; a list ('1,5'), a single page, or a
    non-numeric form is left untouched -- so a comma list isn't turned into a false range."""
    s = s or ""
    m = re.fullmatch(r"\s*(\d+)\s*[-–—]+\s*(\d+)\s*", s)
    return "%s--%s" % (m.group(1), m.group(2)) if m else s

def authors_to_bib(s):
    """DB authors 'Given Family 0001, Given Family, ...' -> 'A and B and ...'."""
    names = [re.sub(r"\s+\d{4}$", "", a.strip()) for a in s.split(",") if a.strip()]
    return " and ".join(names)

def strip_doi(s):
    return re.sub(r"^https?://(dx\.)?doi\.org/", "", s or "", flags=re.I)

def like_escape(s):
    """Escape LIKE metacharacters so a literal id matches literally (use with ESCAPE '\\').
    Without this, a '_' or '%' in a DOI/arXiv id would act as a wildcard and over-match."""
    return re.sub(r"([\\%_])", r"\\\1", s or "")

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


# --- the domain object: one dblp.db `entries` row -----------------------------

class DblpEntry:
    """A DBLP record (one `entries` row) with the behaviour to read and serialise it.
    Distinct from bibspan's `Entry` (a .bib entry) -- this is the DBLP side.  Holds the
    connection so it can resolve venue names from journals/proceedings on demand."""

    def __init__(self, con, row):
        self.con = con
        self.row = row                       # raw sqlite3.Row, for the odd extra field

    @property
    def type(self):
        return self.row["type"]

    @property
    def key(self):
        return self.row["key"]

    @property
    def title(self):
        return self.row["title"]

    @property
    def year(self):
        return str(self.row["year"])

    @property
    def pages(self):
        return self.row["pages"]

    @property
    def authors_bib(self):
        """Authors in BibTeX form ('A and B and ...')."""
        return authors_to_bib(self.row["authors"])

    @property
    def doi(self):
        """The DOI with any doi.org prefix stripped."""
        return strip_doi(self.row["doi"])

    @property
    def is_preprint(self):
        return self.row["venue"] == "corr"

    def venue_forms(self):
        """Return (full, kind, full_forms, short_forms, short) for this row's venue.

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
        row, con = self.row, self.con
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

    def to_bibtex(self, citekey, short=False):
        """Serialize as a BibTeX entry under `citekey`, using the full official venue name
        (or 'Proc. {ACRONYM}' when short)."""
        fields = [("author", self.authors_bib), ("title", self.title)]
        full, _kind, _ff, _sf, short_name = self.venue_forms()
        if self.type == "inproceedings":
            fields.append(("booktitle", short_name if (short and short_name) else full))
        else:
            fields += [("journal", short_name if (short and short_name) else full),
                       ("volume", self.row["volume"]), ("number", self.row["number"])]
        fields += [("pages", fmt_pages(self.pages)), ("year", self.year)]
        if self.doi:
            fields.append(("doi", self.doi))
        body = ",\n".join("  %s = {%s}" % (k, v) for k, v in fields if v)
        return "@%s{%s,\n%s\n}" % (self.type, citekey, body)

    def suggest_key(self, used=()):
        """A <Family><Year> citation key for this record, avoiding collisions in `used`."""
        first = (self.row["authors"] or "anon").split(",")[0]
        toks = re.sub(r"\s+\d{4}$", "", first).split()
        fam = _fold(toks[-1]) if toks else "anon"
        base = (fam[:1].upper() + fam[1:]) + str(self.row["year"] or "")
        key, suffix = base, ord("a")
        while key in used:
            key = base + chr(suffix)
            suffix += 1
        return key


# --- the repository: an open dblp.db ------------------------------------------

class Db:
    """An open dblp.db.  Look records up; get DblpEntry back (fuzzy returns lightweight rows,
    a different SELECT shape, for candidate display)."""

    def __init__(self, con):
        self.con = con

    def _split(self, rows):
        """Partition matched rows into (published, preprints) as DblpEntry lists."""
        pub = [DblpEntry(self.con, r) for r in rows if r["venue"] != "corr"]
        pre = [DblpEntry(self.con, r) for r in rows if r["venue"] == "corr"]
        return pub, pre

    def by_key(self, key):
        """The record with this exact DBLP key, or None."""
        r = self.con.execute("SELECT * FROM entries WHERE key=?", (key,)).fetchone()
        return DblpEntry(self.con, r) if r else None

    def by_title(self, title):
        """(published, preprints) whose normalized title matches `title`."""
        rows = self.con.execute(
            "SELECT * FROM entries WHERE title_norm=?", (norm_title(title),)).fetchall()
        return self._split(rows)

    def by_doi(self, doi):
        """Fallback lookup by DOI (when the title doesn't match): try the indexed doi
        column first, then any electronic-edition link in `ee`."""
        rows = self.con.execute(
            "SELECT * FROM entries WHERE doi = 'https://doi.org/' || ?", (doi,)).fetchall()
        if not rows:
            rows = self.con.execute(
                "SELECT * FROM entries WHERE ee LIKE '%' || ? || '%' ESCAPE '\\'",
                (like_escape(doi),)).fetchall()
        return self._split(rows)

    def by_arxiv(self, axid):
        """Find the DBLP CoRR (arXiv) record for an arXiv id, via its ee link.  Lets us
        match even when the arXiv title changed across versions (always preprints)."""
        rows = self.con.execute("SELECT * FROM entries WHERE venue='corr' AND "
                                "ee LIKE '%' || ? || '%' ESCAPE '\\'",
                                (like_escape(axid),)).fetchall()
        return [], [DblpEntry(self.con, r) for r in rows]

    def fuzzy(self, title, limit=4):
        """Up to `limit` FTS candidates (lightweight year/venue/title/key rows) for a title
        that matched nothing exactly."""
        toks = [t for t in re.findall(r"[A-Za-z0-9]+", title) if len(t) > 2][:8]
        if not toks:
            return []
        q = "title:(" + " OR ".join(toks) + ")"
        try:
            return self.con.execute(
                "SELECT e.year, e.venue, e.title, e.key FROM fts JOIN entries e "
                "USING(key) WHERE fts MATCH ? ORDER BY rank LIMIT ?", (q, limit)).fetchall()
        except sqlite3.OperationalError:
            return []
