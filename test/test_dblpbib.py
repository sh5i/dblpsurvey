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
        self.assertEqual(bc.fmt_pages("1-12"), "1--12")
        self.assertEqual(bc.fmt_pages("5"), "5")             # single page kept as-is

    def test_authors_to_bib(self):
        self.assertEqual(bc.authors_to_bib("Le Yu 0002, Xiapu Luo"), "Le Yu and Xiapu Luo")

    def test_strip_doi(self):
        self.assertEqual(bc.strip_doi("https://doi.org/10.1/x"), "10.1/x")
        self.assertEqual(bc.strip_doi("https://dx.doi.org/10.1/x"), "10.1/x")

    def test_surnames(self):
        self.assertEqual(bc.surnames("Hayashi, Shinpei"), {"hayashi"})
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
        self.assertEqual((e.type, e.key, e.get("year")), ("article", "k", "2019"))
        fld = e.fields["year"]
        self.assertEqual(text[fld.start:fld.end], "{2019}")     # offsets span the value token
        self.assertIn("year = {2021}", bc.apply_fixes(text, [(fld.start, fld.end, "{2021}")]))

    def test_read_ids(self):
        self.assertEqual(bc._read_ids("a:b, c:d ,"), ["a:b", "c:d"])


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

    def finding(self, bibstr, short=False):
        return bc.process_entry(self.con, bc.parse_bib(bibstr)[0], short)

    @staticmethod
    def by_field(f):
        return {p["field"]: p for p in f["proposals"]}

    def test_ok(self):
        f = self.finding(
            "@inproceedings{p,\n author={Alice B. Smith and Bob Jones},\n"
            " title={A Great Paper on Software Testing},\n"
            " booktitle={International Conference on Software Engineering},\n"
            " pages={100--110},\n doi={10.1/icse20},\n year={2020}\n}")
        self.assertEqual(f["status"], "ok")
        self.assertEqual(f["proposals"], [])

    def test_fills_are_proposals(self):
        f = self.finding(
            "@inproceedings{p,\n author={Alice B. Smith and Bob Jones},\n"
            " title={A Great Paper on Software Testing},\n"
            " booktitle={International Conference on Software Engineering},\n year={2020}\n}")
        self.assertEqual(f["status"], "incomplete")
        self.assertEqual({k: p["kind"] for k, p in self.by_field(f).items()},
                         {"pages": "fill", "doi": "fill"})

    def test_mismatch_year_is_a_fix(self):
        f = self.finding(
            "@article{a,\n author={Carol Lee and Dan Park},\n"
            " title={Static Analysis for Concurrency Bugs},\n"
            " journal={IEEE Trans. Software Eng.},\n volume={47},\n number={3},\n"
            " pages={200--220},\n year={2019}\n}")
        self.assertEqual(f["status"], "mismatch")
        yr = self.by_field(f)["year"]
        self.assertEqual((yr["kind"], yr["dblp"], yr["id"]), ("fix", "2021", "a:year"))

    def test_venue_diff_is_a_proposal(self):
        f = self.finding(
            "@inproceedings{p,\n author={Alice B. Smith and Bob Jones},\n"
            " title={A Great Paper on Software Testing},\n booktitle={Some Wrong Venue},\n"
            " pages={100--110},\n doi={10.1/icse20},\n year={2020}\n}")
        self.assertEqual(f["status"], "review")
        bk = self.by_field(f)["booktitle"]
        self.assertEqual(bk["kind"], "venue")
        self.assertIn("(ICSE 2020)", bk["dblp"])

    def test_venue_short_form(self):
        f = self.finding(
            "@inproceedings{p,\n title={A Great Paper on Software Testing},\n"
            " booktitle={Some Wrong Venue},\n year={2020}\n}", short=True)
        self.assertEqual(self.by_field(f)["booktitle"]["dblp"], "Proc. {ICSE}")

    def test_apply_only_selected(self):
        text = ("@inproceedings{p,\n author={Alice B. Smith and Bob Jones},\n"
                " title={A Great Paper on Software Testing},\n booktitle={Some Wrong Venue},\n"
                " year={2020}\n}")
        props = self.finding(text)["proposals"]
        new, applied, missing = bc._apply_ids(text, props, ["p:booktitle", "p:nope"])
        self.assertEqual([x["field"] for x in applied], ["booktitle"])
        self.assertEqual(missing, ["p:nope"])
        self.assertIn("(ICSE 2020)", new)
        self.assertNotIn("153--156", new)            # pages fill was NOT selected

    def test_apply_safe_skips_venue_and_review(self):
        text = ("@article{a,\n author={Carol Lee},\n"             # author -> review (subset)
                " title={Static Analysis for Concurrency Bugs},\n"
                " journal={Wrong Journal Name},\n year={2019}\n}")  # journal -> venue, year -> fix
        props = self.finding(text)["proposals"]
        safe = [p["id"] for p in props if p["kind"] in ("fix", "fill")]
        _, applied, _ = bc._apply_ids(text, props, safe)
        fields = {p["field"] for p in applied}
        self.assertIn("year", fields)               # fix applied
        self.assertNotIn("journal", fields)         # venue not in 'safe'
        self.assertNotIn("author", fields)          # review not in 'safe'

    def test_upgrade(self):
        text = ("@misc{m,\n title={Neural Methods for Program Repair},\n"
                " eprint={2001.00001},\n year={2020}\n}")
        f = self.finding(text)
        self.assertEqual(f["status"], "upgrade")
        up = f["proposals"][0]
        self.assertEqual(up["kind"], "upgrade")
        self.assertTrue(up["id"].endswith(":@"))
        self.assertIn("Neural Methods for Program Repair", up["replacement"])
        new, applied, _ = bc._apply_ids(text, f["proposals"], [up["id"]])
        self.assertEqual(len(applied), 1)
        self.assertIn("@inproceedings", new)        # @misc -> @inproceedings (whole-entry swap)

    def test_unknown_and_fuzzy(self):
        self.assertEqual(self.finding(
            "@article{u, title={Completely Unrelated Quantum Gibberish Xyzzy}, year={2020}}")["status"],
            "unknown")
        f = self.finding("@article{g, title={Static Analysis for Quantum Bugs}, year={2020}}")
        self.assertEqual(f["status"], "fuzzy")
        self.assertTrue(f["candidates"])

    def test_misc_non_paper_skipped(self):
        self.assertIsNone(self.finding(
            "@misc{d, title={A Dataset}, howpublished={Zenodo}, year={2020}}"))


if __name__ == "__main__":
    unittest.main()
