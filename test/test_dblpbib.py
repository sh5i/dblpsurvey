#!/usr/bin/env python3
"""Tests for dblpbib (offline .bib checker). Builds a tiny fixture dblp.db from the
real schema.sql; stdlib only. Run: python3 test/test_dblpbib.py  (or via `make test`)."""

import os
import sqlite3
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import dblpbib as bc  # noqa: E402

ECOLS = ("key", "type", "venue", "year", "authors", "title", "title_norm",
         "journal", "booktitle", "volume", "number", "pages", "doi", "ee", "crossref")

def _ins_entry(con, **f):
    f.setdefault("title_norm", bc.norm_title(f.get("title", "")))
    vals = [f.get(c, "") for c in ECOLS]
    con.execute("INSERT INTO entries VALUES (%s)" % ",".join("?" * len(ECOLS)), vals)

def build_db(path):
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    with open(os.path.join(ROOT, "schema.sql"), encoding="utf-8") as fh:
        con.executescript(fh.read())
    con.execute("INSERT INTO proceedings(key,title,booktitle,year,kind,ordinal,conf_name,canonical) "
                "VALUES (?,?,?,?,?,?,?,?)",
                ("conf/icse/2020", "Proceedings ...", "ICSE", 2020, "main", 42,
                 "International Conference on Software Engineering",
                 "Proceedings of the 42nd International Conference on Software Engineering (ICSE 2020)"))
    con.execute("INSERT INTO journals(abbrev,full_name) VALUES (?,?)",
                ("IEEE Trans. Software Eng.", "IEEE Transactions on Software Engineering"))
    _ins_entry(con, key="conf/icse/Paper20", type="inproceedings", venue="icse", year=2020,
               authors="Alice B. Smith, Bob Jones", title="A Great Paper on Software Testing",
               booktitle="ICSE", pages="100-110", doi="https://doi.org/10.1/icse20",
               ee="https://doi.org/10.1/icse20", crossref="conf/icse/2020")
    _ins_entry(con, key="journals/tse/Art21", type="article", venue="tse", year=2021,
               authors="Carol Lee 0001, Dan Park", title="Static Analysis for Concurrency Bugs",
               journal="IEEE Trans. Software Eng.", volume="47", number="3", pages="200-220",
               doi="https://doi.org/10.1/tse21", ee="https://doi.org/10.1/tse21")
    # an arXiv preprint and its published sibling (same normalised title) for upgrade tests
    _ins_entry(con, key="journals/corr/abs-2001-00001", type="article", venue="corr", year=2020,
               authors="Eve Adams", title="Neural Methods for Program Repair",
               journal="CoRR", volume="abs/2001.00001", ee="https://arxiv.org/abs/2001.00001")
    _ins_entry(con, key="conf/icse/Pub21", type="inproceedings", venue="icse", year=2021,
               authors="Eve Adams", title="Neural Methods for Program Repair",
               booktitle="ICSE", pages="1-10", doi="https://doi.org/10.1/icse21",
               ee="https://doi.org/10.1/icse21", crossref="conf/icse/2020")
    con.execute("INSERT INTO fts(key,title,authors) SELECT key,title,authors FROM entries")
    con.commit()
    return con


class HelperTests(unittest.TestCase):
    def test_norm_title(self):
        self.assertEqual(bc.norm_title("On H2O & Refactoring!"), "onh2orefactoring")
        self.assertEqual(bc.norm_title("a b  c"), "abc")

    def test_pages(self):
        self.assertEqual(bc.norm_pages("1--12"), "1-12")
        self.assertEqual(bc.norm_pages("1-12"), "1-12")
        self.assertEqual(bc.fmt_pages("1-12"), "1--12")
        self.assertEqual(bc.fmt_pages("5"), "5")             # single page kept as-is

    def test_authors_to_bib(self):
        self.assertEqual(bc.authors_to_bib("Le Yu 0002, Xiapu Luo"), "Le Yu and Xiapu Luo")

    def test_strip_doi(self):
        self.assertEqual(bc.strip_doi("https://doi.org/10.1/x"), "10.1/x")
        self.assertEqual(bc.strip_doi("https://dx.doi.org/10.1/x"), "10.1/x")

    def test_surnames(self):
        self.assertEqual(bc.surnames("Hayashi, Shinpei"), {"hayashi"})
        self.assertEqual(bc.surnames("Shinpei Hayashi"), {"hayashi"})
        self.assertEqual(bc.surnames("Coen De Roover"), {"roover"})
        self.assertEqual(bc.surnames("De Roover, Coen"), {"roover"})

    def test_arxiv_id(self):
        e = bc.parse_bib("@misc{x, title={T}, eprint={2407.19487}}")[0]
        self.assertEqual(bc.arxiv_id(e), "2407.19487")
        e = bc.parse_bib("@article{y, title={T}, journal={CoRR}, volume={abs/cs/0501001}}")[0]
        self.assertEqual(bc.arxiv_id(e), "cs/0501001")

    def test_gen_citekey(self):
        row = {"authors": "Shinpei Hayashi 0001, X Y", "year": 2024}
        self.assertEqual(bc.gen_citekey(row, set()), "Hayashi2024")
        self.assertEqual(bc.gen_citekey(row, {"Hayashi2024"}), "Hayashi2024a")

    def test_parse_and_apply_fixes(self):
        text = "@article{k,\n  title = {Hello},\n  year = {2019}\n}\n"
        e = bc.parse_bib(text)[0]
        self.assertEqual(e.type, "article")
        self.assertEqual(e.key, "k")
        self.assertEqual(e.get("year"), "2019")
        fld = e.fields["year"]
        self.assertEqual(text[fld.start:fld.end], "{2019}")     # offsets span the value token
        fixed = bc.apply_fixes(text, [(fld.start, fld.end, "{2021}")])
        self.assertIn("year = {2021}", fixed)


class CheckTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="dblpbib-test.")
        cls.con = build_db(os.path.join(cls.tmp, "fixture.db"))

    @classmethod
    def tearDownClass(cls):
        cls.con.close()
        import shutil
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def run_one(self, bibstr, **optkw):
        opt = dict(do_fix=False, aggressive=False, fix_venue=False, short=False, allow=None)
        opt.update(optkw)
        e = bc.parse_bib(bibstr)[0]
        return bc.process_entry(self.con, e, bc.Opts(**opt))

    def test_ok(self):
        f, edits = self.run_one(
            "@inproceedings{p,\n author={Alice B. Smith and Bob Jones},\n"
            " title={A Great Paper on Software Testing},\n"
            " booktitle={International Conference on Software Engineering},\n"
            " pages={100--110},\n doi={10.1/icse20},\n year={2020}\n}")
        self.assertEqual(f["status"], "ok")
        self.assertEqual(f["fixes"], [])
        self.assertEqual(f["fills"], [])
        self.assertEqual(edits, [])

    def test_fills_missing_fields(self):
        f, _ = self.run_one(
            "@inproceedings{p,\n author={Alice B. Smith and Bob Jones},\n"
            " title={A Great Paper on Software Testing},\n"
            " booktitle={International Conference on Software Engineering},\n year={2020}\n}")
        self.assertEqual(f["status"], "incomplete")
        filled = {x["field"] for x in f["fills"]}
        self.assertEqual(filled, {"pages", "doi"})

    def test_mismatch_year_is_a_fix(self):
        f, edits = self.run_one(
            "@article{a,\n author={Carol Lee and Dan Park},\n"
            " title={Static Analysis for Concurrency Bugs},\n"
            " journal={IEEE Trans. Software Eng.},\n volume={47},\n number={3},\n"
            " pages={200--220},\n year={2019}\n}", do_fix=True)
        self.assertEqual(f["status"], "mismatch")
        self.assertEqual([(x["field"], x["dblp"]) for x in f["fixes"]], [("year", "2021")])
        self.assertTrue(edits)                                  # --fix emits an edit

    def test_venue_is_review_by_default(self):
        f, _ = self.run_one(
            "@inproceedings{p,\n author={Alice B. Smith and Bob Jones},\n"
            " title={A Great Paper on Software Testing},\n booktitle={Some Wrong Venue},\n"
            " pages={100--110},\n doi={10.1/icse20},\n year={2020}\n}")
        self.assertEqual(f["status"], "review")
        self.assertEqual([x["field"] for x in f["fixes"]], [])
        self.assertIn("booktitle", [x["field"] for x in f["reviews"]])

    def test_venue_fixable_with_fix_venue(self):
        f, edits = self.run_one(
            "@inproceedings{p,\n author={Alice B. Smith and Bob Jones},\n"
            " title={A Great Paper on Software Testing},\n booktitle={Some Wrong Venue},\n"
            " pages={100--110},\n doi={10.1/icse20},\n year={2020}\n}",
            do_fix=True, fix_venue=True)
        self.assertEqual(f["status"], "mismatch")
        bk = [x for x in f["fixes"] if x["field"] == "booktitle"]
        self.assertEqual(len(bk), 1)
        self.assertIn("(ICSE 2020)", bk[0]["dblp"])
        self.assertTrue(edits)

    def test_arxiv_upgrade(self):
        f, edits = self.run_one(
            "@misc{m,\n title={Neural Methods for Program Repair},\n"
            " eprint={2001.00001},\n year={2020}\n}")
        self.assertEqual(f["status"], "upgrade")
        self.assertIn("Neural Methods for Program Repair", f["replacement"])
        self.assertEqual(edits, [])                            # not aggressive -> no edit
        # aggressive swaps the whole entry
        f2, edits2 = self.run_one(
            "@misc{m,\n title={Neural Methods for Program Repair},\n"
            " eprint={2001.00001},\n year={2020}\n}", do_fix=True, aggressive=True)
        self.assertTrue(edits2)

    def test_unknown_and_fuzzy(self):
        f, _ = self.run_one("@article{u, title={Completely Unrelated Quantum Gibberish Xyzzy}, year={2020}}")
        self.assertEqual(f["status"], "unknown")
        f2, _ = self.run_one("@article{g, title={Static Analysis for Quantum Bugs}, year={2020}}")
        self.assertEqual(f2["status"], "fuzzy")
        self.assertTrue(f2["candidates"])

    def test_key_filter_keeps_only_named_field(self):
        # wrong year + missing doi; --key=year keeps the year fix, drops the doi fill
        f, _ = self.run_one(
            "@article{a,\n author={Carol Lee and Dan Park},\n"
            " title={Static Analysis for Concurrency Bugs},\n"
            " journal={IEEE Trans. Software Eng.},\n volume={47},\n number={3},\n"
            " pages={200--220},\n year={2019}\n}", allow={"year"})
        self.assertEqual([x["field"] for x in f["fixes"]], ["year"])
        self.assertEqual(f["fills"], [])

    def test_misc_non_paper_skipped(self):
        f, edits = self.run_one("@misc{d, title={A Dataset}, howpublished={Zenodo}, year={2020}}")
        self.assertIsNone(f)
        self.assertEqual(edits, [])


if __name__ == "__main__":
    unittest.main()
