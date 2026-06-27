#!/usr/bin/env python3
"""Tests for dblpdb (the dblp.db domain layer): pure transforms, the Db repository, and
DblpEntry serialization. Stdlib only. Run: python3 test/test_dblpdb.py (or via `make test`)."""

import os
import sqlite3
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
import dblpdb  # noqa: E402

ECOLS = ("key", "type", "venue", "year", "authors", "title", "title_norm",
         "journal", "booktitle", "volume", "number", "pages", "doi", "ee", "crossref")


def _ins(con, **f):
    f.setdefault("title_norm", dblpdb.norm_title(f.get("title", "")))
    con.execute("INSERT INTO entries VALUES (%s)" % ",".join("?" * len(ECOLS)),
                [f.get(c, "") for c in ECOLS])


def build_db():
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    with open(os.path.join(ROOT, "src", "schema.sql"), encoding="utf-8") as fh:
        con.executescript(fh.read())
    con.execute("INSERT INTO journals(abbrev,full_name) VALUES (?,?)",
                ("J. Example Stud.", "Journal of Example Studies"))
    con.execute("INSERT INTO proceedings(key,title,booktitle,year,kind,ordinal,conf_name,canonical) "
                "VALUES (?,?,?,?,?,?,?,?)",
                ("conf/icet/2020", "Proceedings ...", "ICET", 2020, "main", 42,
                 "International Conference on Example Topics",
                 "Proceedings of the 42nd International Conference on Example Topics (ICET 2020)"))
    _ins(con, key="journals/jes/Art21", type="article", venue="jes", year=2021,
         authors="Carol Lee 0001, Dan Park", title="Example Paper About Sample Topics",
         journal="J. Example Stud.", volume="47", number="3", pages="200-220",
         doi="https://doi.org/10.1/jes21", ee="https://doi.org/10.1/jes21")
    _ins(con, key="conf/icet/Paper20", type="inproceedings", venue="icet", year=2020,
         authors="Alice B. Smith, Bob Jones", title="A Great Paper",
         booktitle="ICET", pages="100-110", crossref="conf/icet/2020")
    # two ee whose ids differ only at an underscore position -- exercises LIKE escaping.
    _ins(con, key="journals/x/Under", type="article", venue="x", year=2019,
         title="Underscore", ee="https://ex/10.1/a_c")
    _ins(con, key="journals/x/Other", type="article", venue="x", year=2019,
         title="Other", ee="https://ex/10.1/aXc")
    _ins(con, key="journals/corr/abs-2001-00001", type="article", venue="corr", year=2020,
         authors="Eve Adams", title="Example Methods", ee="https://arxiv.org/abs/2001.00001")
    _ins(con, key="journals/jes/Accent20", type="article", venue="jes", year=2020,
         authors="D\u00fcmmy \u00dcser, T\u00ebst \u00c7ase", title="\u00dcber Example \u03bb-Topics",
         journal="J. Example Stud.", volume="1", number="1", pages="1--2")
    con.execute("INSERT INTO fts(key,title,authors) SELECT key,title,authors FROM entries")
    con.commit()
    return con


class Transforms(unittest.TestCase):
    def test_norm_title_contract(self):
        # The hard-rule normalization, shared with the extractors and the DB.
        self.assertEqual(dblpdb.norm_title("On H2O & Refactoring!"), "onh2orefactoring")
        self.assertEqual(dblpdb.norm_title("a b  c"), "abc")

    def test_norm_pages(self):
        self.assertEqual(dblpdb.norm_pages("1--12"), "1-12")
        self.assertEqual(dblpdb.norm_pages("1,5"), "1-5")        # comparison key, dash-joined

    def test_fmt_pages_only_collapses_real_ranges(self):
        self.assertEqual(dblpdb.fmt_pages("1-12"), "1--12")
        self.assertEqual(dblpdb.fmt_pages("1--12"), "1--12")
        self.assertEqual(dblpdb.fmt_pages("5"), "5")             # single page
        self.assertEqual(dblpdb.fmt_pages("1,5"), "1,5")         # list is NOT a range
        self.assertEqual(dblpdb.fmt_pages("i--xiv"), "i--xiv")   # roman, untouched
        self.assertEqual(dblpdb.fmt_pages(""), "")

    def test_authors_to_bib_strips_disambiguator(self):
        self.assertEqual(dblpdb.authors_to_bib("Alice Tan 0002, Bob Rivera"),
                         "Alice Tan and Bob Rivera")

    def test_strip_doi(self):
        self.assertEqual(dblpdb.strip_doi("https://doi.org/10.1/x"), "10.1/x")
        self.assertEqual(dblpdb.strip_doi("http://dx.doi.org/10.1/x"), "10.1/x")
        self.assertEqual(dblpdb.strip_doi(None), "")

    def test_like_escape(self):
        self.assertEqual(dblpdb.like_escape("10.1/a_c"), r"10.1/a\_c")
        self.assertEqual(dblpdb.like_escape("a%b\\c"), r"a\%b\\c")

    def test_fold_accents_and_latex(self):
        self.assertEqual(dblpdb._fold("Andr\u00e9"), "andre")
        self.assertEqual(dblpdb._fold(r"Andr\'e"), "andre")
        self.assertEqual(dblpdb._fold(r"P{\^a}r{\textcommabelow t}achi"), "partachi")


class DbLookups(unittest.TestCase):
    def setUp(self):
        self.db = dblpdb.Db(build_db())

    def test_by_key(self):
        self.assertEqual(self.db.by_key("journals/jes/Art21").year, "2021")
        self.assertIsNone(self.db.by_key("nope"))

    def test_by_title_normalized(self):
        pub, pre = self.db.by_title("example paper about sample topics!!")
        self.assertEqual([e.key for e in pub], ["journals/jes/Art21"])
        self.assertEqual(pre, [])

    def test_by_doi_exact_and_ee_fallback(self):
        pub, _ = self.db.by_doi("10.1/jes21")            # exact doi column
        self.assertEqual([e.key for e in pub], ["journals/jes/Art21"])

    def test_by_doi_like_underscore_is_literal(self):
        # '_' must match literally (not as a LIKE single-char wildcard): only a_c, never aXc.
        pub, _ = self.db.by_doi("10.1/a_c")
        self.assertEqual([e.key for e in pub], ["journals/x/Under"])

    def test_by_arxiv(self):
        _, pre = self.db.by_arxiv("2001.00001")
        self.assertEqual([e.key for e in pre], ["journals/corr/abs-2001-00001"])

    def test_empty_id_lookups_match_nothing(self):
        # an empty doi/arxiv id must not degrade to LIKE '%%' and match every ee'd row.
        self.assertEqual(self.db.by_doi(""), ([], []))
        self.assertEqual(self.db.by_arxiv(""), ([], []))

    def test_fuzzy(self):
        keys = [r["key"] for r in self.db.fuzzy("Example Sample Topics")]
        self.assertIn("journals/jes/Art21", keys)


class EntrySerialization(unittest.TestCase):
    def setUp(self):
        self.db = dblpdb.Db(build_db())

    def test_venue_forms_article(self):
        full, kind, _ff, _sf, short = self.db.by_key("journals/jes/Art21").venue_forms()
        self.assertEqual(kind, "journal")
        self.assertEqual(full, "Journal of Example Studies")
        self.assertEqual(short, "J. Example Stud.")

    def test_venue_forms_inproceedings_via_crossref(self):
        full, kind, _ff, _sf, _short = self.db.by_key("conf/icet/Paper20").venue_forms()
        self.assertEqual(kind, "main")
        self.assertIn("International Conference on Example Topics", full)

    def test_to_bibtex_article(self):
        out = self.db.by_key("journals/jes/Art21").to_bibtex("Lee2021")
        self.assertTrue(out.startswith("@article{Lee2021,"))
        self.assertIn("journal = {Journal of Example Studies}", out)
        self.assertIn("volume = {47}", out)
        self.assertIn("pages = {200--220}", out)        # range collapsed to house style
        self.assertIn("doi = {10.1/jes21}", out)        # doi.org prefix stripped

    def test_to_bibtex_inproceedings_uses_booktitle(self):
        out = self.db.by_key("conf/icet/Paper20").to_bibtex("conf/icet/Paper20")
        self.assertTrue(out.startswith("@inproceedings{conf/icet/Paper20,"))
        self.assertIn("booktitle =", out)
        self.assertNotIn("journal =", out)


class LatexEscaping(unittest.TestCase):
    def setUp(self):
        self.db = dblpdb.Db(build_db())

    def test_accents_systematic(self):
        L = dblpdb.to_latex
        self.assertEqual(L("\u00e9"), r"{\'{e}}")
        self.assertEqual(L("\u00fc"), r'{\"{u}}')
        self.assertEqual(L("\u00f1"), r"{\~{n}}")
        self.assertEqual(L("\u00e7"), r"{\c{c}}")
        self.assertEqual(L("\u00e8"), "{\\`{e}}")
        self.assertEqual(L("\u00f4"), r"{\^{o}}")
        self.assertEqual(L("\u00e5"), r"{\r{a}}")
        self.assertEqual(L("\u00ef"), r'{\"{\i}}')          # dotless i under an accent
        self.assertEqual(L("\u0161"), r"{\v{s}}")           # caron (title extra)
        self.assertEqual(L("\u0151"), r"{\H{o}}")           # double acute

    def test_atomic_letters(self):
        cases = {"\u00f8": r"{\o}", "\u00df": r"{\ss}", "\u00e6": r"{\ae}", "\u00d8": r"{\O}", "\u00f0": r"{\dh}",
                 "\u00fe": r"{\th}", "\u00de": r"{\TH}", "\u00d0": r"{\DH}", "\u0142": r"{\l}", "\u0131": r"{\i}",
                 "\u0153": r"{\oe}"}
        for ch, mac in cases.items():
            self.assertEqual(dblpdb.to_latex(ch), mac)

    def test_punct_and_ascii_passthrough(self):
        self.assertEqual(dblpdb.to_latex("a\u2013b\u2014c"), "a--b---c")    # en/em dash
        self.assertEqual(dblpdb.to_latex("plain ASCII 0-9!"), "plain ASCII 0-9!")

    def test_maths_left_as_utf8_and_warned(self):
        seen = []
        out = dblpdb.to_latex("\u03b1 \u2208 \u211d \u7a0b", warn=seen.append)
        self.assertEqual(out, "\u03b1 \u2208 \u211d \u7a0b")              # no portable macro -> untouched
        self.assertEqual(set(seen), set("\u03b1\u2208\u211d\u7a0b"))

    def test_author_surface_fully_maps_no_warn(self):
        # the closed Latin author surface must escape with NO warning (authors are complete).
        names = "\u00e9\u00e1\u00f6\u00ed\u00fc\u00f3\u00e4\u00e7\u00e3\u00f1\u00fa\u00e8 \u00f8\u00df\u00e6\u00d8\u00f0\u00de\u00d0\u00c6\u00fe"
        seen = []
        dblpdb.to_latex(names, warn=seen.append)
        self.assertEqual(seen, [])

    def test_to_bibtex_latex_escapes_values(self):
        rec = self.db.by_key("journals/jes/Accent20")
        seen = []
        out = rec.to_bibtex("k", latex=True, warn=seen.append)
        self.assertIn("author = {%s}" % dblpdb.to_latex(rec.authors_bib), out)
        self.assertIn(r'{\"{u}}', out)                 # \u00fc actually escaped
        self.assertEqual(set(seen), {"\u03bb"})             # Greek in the title is left + warned

    def test_to_bibtex_default_is_utf8(self):
        rec = self.db.by_key("journals/jes/Accent20")
        self.assertIn("D\u00fcmmy", rec.to_bibtex("k"))    # default: raw UTF-8, no escaping


if __name__ == "__main__":
    unittest.main()
