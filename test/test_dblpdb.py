#!/usr/bin/env python3
"""Tests for dblpdb (the dblp.db domain layer): pure transforms, the Db repository, and
DblpEntry serialisation. Stdlib only. Run: python3 test/test_dblpdb.py (or via `make test`)."""

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
                ("IEEE Trans. Software Eng.", "IEEE Transactions on Software Engineering"))
    con.execute("INSERT INTO proceedings(key,title,booktitle,year,kind,ordinal,conf_name,canonical) "
                "VALUES (?,?,?,?,?,?,?,?)",
                ("conf/icse/2020", "Proceedings ...", "ICSE", 2020, "main", 42,
                 "International Conference on Software Engineering",
                 "Proceedings of the 42nd International Conference on Software Engineering (ICSE 2020)"))
    _ins(con, key="journals/tse/Art21", type="article", venue="tse", year=2021,
         authors="Carol Lee 0001, Dan Park", title="Static Analysis for Concurrency Bugs",
         journal="IEEE Trans. Software Eng.", volume="47", number="3", pages="200-220",
         doi="https://doi.org/10.1/tse21", ee="https://doi.org/10.1/tse21")
    _ins(con, key="conf/icse/Paper20", type="inproceedings", venue="icse", year=2020,
         authors="Alice B. Smith, Bob Jones", title="A Great Paper",
         booktitle="ICSE", pages="100-110", crossref="conf/icse/2020")
    # two ee whose ids differ only at an underscore position -- exercises LIKE escaping.
    _ins(con, key="journals/x/Under", type="article", venue="x", year=2019,
         title="Underscore", ee="https://ex/10.1/a_c")
    _ins(con, key="journals/x/Other", type="article", venue="x", year=2019,
         title="Other", ee="https://ex/10.1/aXc")
    _ins(con, key="journals/corr/abs-2001-00001", type="article", venue="corr", year=2020,
         authors="Eve Adams", title="Neural Methods", ee="https://arxiv.org/abs/2001.00001")
    con.execute("INSERT INTO fts(key,title,authors) SELECT key,title,authors FROM entries")
    con.commit()
    return con


class Transforms(unittest.TestCase):
    def test_norm_title_contract(self):
        # The hard-rule normalisation, shared with the extractors and the DB.
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
        self.assertEqual(dblpdb.authors_to_bib("Le Yu 0002, Xiapu Luo"),
                         "Le Yu and Xiapu Luo")

    def test_strip_doi(self):
        self.assertEqual(dblpdb.strip_doi("https://doi.org/10.1/x"), "10.1/x")
        self.assertEqual(dblpdb.strip_doi("http://dx.doi.org/10.1/x"), "10.1/x")
        self.assertEqual(dblpdb.strip_doi(None), "")

    def test_like_escape(self):
        self.assertEqual(dblpdb.like_escape("10.1/a_c"), r"10.1/a\_c")
        self.assertEqual(dblpdb.like_escape("a%b\\c"), r"a\%b\\c")

    def test_fold_accents_and_latex(self):
        self.assertEqual(dblpdb._fold("André"), "andre")
        self.assertEqual(dblpdb._fold(r"Andr\'e"), "andre")
        self.assertEqual(dblpdb._fold(r"P{\^a}r{\textcommabelow t}achi"), "partachi")


class DbLookups(unittest.TestCase):
    def setUp(self):
        self.db = dblpdb.Db(build_db())

    def test_by_key(self):
        self.assertEqual(self.db.by_key("journals/tse/Art21").year, "2021")
        self.assertIsNone(self.db.by_key("nope"))

    def test_by_title_normalised(self):
        pub, pre = self.db.by_title("static analysis for concurrency bugs!!")
        self.assertEqual([e.key for e in pub], ["journals/tse/Art21"])
        self.assertEqual(pre, [])

    def test_by_doi_exact_and_ee_fallback(self):
        pub, _ = self.db.by_doi("10.1/tse21")            # exact doi column
        self.assertEqual([e.key for e in pub], ["journals/tse/Art21"])

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
        keys = [r["key"] for r in self.db.fuzzy("Static Analysis Concurrency")]
        self.assertIn("journals/tse/Art21", keys)


class EntrySerialisation(unittest.TestCase):
    def setUp(self):
        self.db = dblpdb.Db(build_db())

    def test_venue_forms_article(self):
        full, kind, _ff, _sf, short = self.db.by_key("journals/tse/Art21").venue_forms()
        self.assertEqual(kind, "journal")
        self.assertEqual(full, "IEEE Transactions on Software Engineering")
        self.assertEqual(short, "IEEE Trans. Software Eng.")

    def test_venue_forms_inproceedings_via_crossref(self):
        full, kind, _ff, _sf, _short = self.db.by_key("conf/icse/Paper20").venue_forms()
        self.assertEqual(kind, "main")
        self.assertIn("International Conference on Software Engineering", full)

    def test_to_bibtex_article(self):
        out = self.db.by_key("journals/tse/Art21").to_bibtex("Lee2021")
        self.assertTrue(out.startswith("@article{Lee2021,"))
        self.assertIn("journal = {IEEE Transactions on Software Engineering}", out)
        self.assertIn("volume = {47}", out)
        self.assertIn("pages = {200--220}", out)        # range collapsed to house style
        self.assertIn("doi = {10.1/tse21}", out)        # doi.org prefix stripped

    def test_to_bibtex_inproceedings_uses_booktitle(self):
        out = self.db.by_key("conf/icse/Paper20").to_bibtex("conf/icse/Paper20")
        self.assertTrue(out.startswith("@inproceedings{conf/icse/Paper20,"))
        self.assertIn("booktitle =", out)
        self.assertNotIn("journal =", out)


if __name__ == "__main__":
    unittest.main()
