#!/usr/bin/env python3
"""Tests for dblpbib (offline .bib checker). Builds a tiny fixture dblp.db from the
real schema.sql; stdlib only. Run: python3 test/test_dblpbib.py  (or via `make test`)."""

import os
import sqlite3
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# dblpbib is an executable CLI with no .py extension, so load it from its path.
from importlib.machinery import SourceFileLoader            # noqa: E402
from importlib.util import module_from_spec, spec_from_loader  # noqa: E402
_loader = SourceFileLoader("dblpbib", os.path.join(ROOT, "bin", "dblpbib"))
bc = module_from_spec(spec_from_loader("dblpbib", _loader))
_loader.exec_module(bc)

ECOLS = ("key", "type", "venue", "year", "authors", "title", "title_norm",
         "journal", "booktitle", "volume", "number", "pages", "doi", "ee", "crossref")

def _ins_entry(con, **f):
    f.setdefault("title_norm", bc.norm_title(f.get("title", "")))
    vals = [f.get(c, "") for c in ECOLS]
    con.execute("INSERT INTO entries VALUES (%s)" % ",".join("?" * len(ECOLS)), vals)

def build_db(path):
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    with open(os.path.join(ROOT, "src", "schema.sql"), encoding="utf-8") as fh:
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
    # an arXiv preprint and its published sibling (same normalized title) for upgrade tests
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

    def test_parse_and_apply_via_bibgraft(self):
        text = "@article{k,\n  title = {Hello},\n  year = {2019}\n}\n"
        e = bc.parse_bib(text)[0]
        self.assertEqual((e.type, e.key, e.get("year")), ("article", "k", "2019"))
        # a proposal carries a bibgraft op; _apply_ids edits only that value, in file style
        props = [bc._proposal(e, "edit", "year", "2019", "2021")]
        new, applied, missing = bc._apply_ids(text, props, ["k:year"])
        self.assertEqual((missing, [p["id"] for p in applied]), ([], ["k:year"]))
        self.assertIn("year = {2021}", new)
        self.assertIn("title = {Hello}", new)                   # untouched field intact

    def test_line_id_oneline_and_report(self):
        # id-first form (e.g. a hand-typed id): first token wins, ignore a colon-y suffix
        self.assertEqual(bc._line_id("Bosu2014:year\tedit\tOld:x → New:y"), "Bosu2014:year")
        # text-report proposal lines: id is the trailing token, decoration ignored
        self.assertEqual(bc._line_id("  ~ edit year       2013 → 2014  Bosu2014:year"),
                         "Bosu2014:year")
        self.assertEqual(bc._line_id("  + add  doi    10.1016/j.jss.x  Bosu2014:doi"), "Bosu2014:doi")
        self.assertEqual(bc._line_id("  » repl @entry  now published  m:@"), "m:@")
        self.assertEqual(bc._line_id("  \x1b[90m~\x1b[0m edit year x Bosu2014:year"),
                         "Bosu2014:year")            # ANSI color stripped
        # header / summary / stale-ignore lines carry no applicable id
        self.assertIsNone(bc._line_id("● Some Title: A Survey  Bosu2014"))
        self.assertIsNone(bc._line_id("summary: 1 mismatch, 2 suppressed"))
        self.assertIsNone(bc._line_id("ignore: matched nothing (stale): Nope:doi"))
        self.assertIsNone(bc._line_id("   "))

    def test_parse_selector(self):
        s, e = bc._parse_selector("Knuth:1984:year")
        self.assertIsNone(e)
        self.assertEqual((s["keypat"], s["items"]), ("Knuth:1984", ["year"]))   # split on LAST colon
        self.assertEqual(bc._parse_selector("a:booktitle,abstract,note")[0]["items"],
                         ["booktitle", "abstract", "note"])             # comma field list
        self.assertEqual(bc._parse_selector("@inproceedings:*")[0]["keypat"], "@inproceedings")
        self.assertIsNotNone(bc._parse_selector("hoge-*:year")[1])      # reserved glob -> error
        self.assertIsNotNone(bc._parse_selector("noColonHere")[1])

    def test_parse_ignores_whitespace_and_comments(self):
        text = ("@comment{dblpbib-ignore\n"
                "  A:year  B:doi   # two selectors on a line, then a comment\n"
                "  *:doi\n"
                "  % a full-line comment\n"
                "}\n@article{A, title={T}, year={2020}}\n")
        sels, errs = bc.parse_ignores(text)
        self.assertEqual([s["sel"] for s in sels], ["A:year", "B:doi", "*:doi"])
        self.assertEqual(errs, [])

    def test_write_ignores_creates_block(self):
        text = "@article{A, title={T}, year={2020}}\n"
        out = bc._write_ignores(text, ["A:year", "A:doi"])
        sels, _ = bc.parse_ignores(out)
        self.assertEqual({s["sel"] for s in sels}, {"A:year", "A:doi"})
        self.assertIn("@article{A,", out)                       # entry preserved
        self.assertEqual(out.count("@comment{dblpbib-ignore"), 1)

    def test_write_ignores_appends_to_existing(self):
        text = "@comment{dblpbib-ignore\n  A:year\n}\n\n@article{A, title={T}, year={2020}}\n"
        out = bc._write_ignores(text, ["B:doi"])
        sels, _ = bc.parse_ignores(out)
        self.assertEqual({s["sel"] for s in sels}, {"A:year", "B:doi"})
        self.assertEqual(out.count("@comment{dblpbib-ignore"), 1)   # appended, not a 2nd block

    def test_write_ignores_below_header(self):
        # the new block goes below leading comments and @string/@comment, above the first entry.
        text = ("% my refs\n% kept by hand\n\n"
                "@comment{a note with an @article{Decoy} inside, brace-skipped}\n\n"
                "@string{me = {Me}}\n\n"
                "@article{A, title={T}, year={2020}}\n")
        out = bc._write_ignores(text, ["A:year"])
        self.assertLess(out.index("% my refs"), out.index("@comment{dblpbib-ignore"))
        self.assertLess(out.index("@string{me"), out.index("@comment{dblpbib-ignore"))
        self.assertLess(out.index("@comment{dblpbib-ignore"), out.index("@article{A,"))
        self.assertEqual({s["sel"] for s in bc.parse_ignores(out)[0]}, {"A:year"})


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

    def test_adds_are_proposals(self):
        f = self.finding(
            "@inproceedings{p,\n author={Alice B. Smith and Bob Jones},\n"
            " title={A Great Paper on Software Testing},\n"
            " booktitle={International Conference on Software Engineering},\n year={2020}\n}")
        self.assertEqual(f["status"], "incomplete")
        self.assertEqual({k: p["op"] for k, p in self.by_field(f).items()},
                         {"pages": "add", "doi": "add"})

    def test_mismatch_year_is_an_edit(self):
        f = self.finding(
            "@article{a,\n author={Carol Lee and Dan Park},\n"
            " title={Static Analysis for Concurrency Bugs},\n"
            " journal={IEEE Trans. Software Eng.},\n volume={47},\n number={3},\n"
            " pages={200--220},\n year={2019}\n}")
        self.assertEqual(f["status"], "mismatch")
        yr = self.by_field(f)["year"]
        self.assertEqual((yr["op"], yr["review"], yr["dblp"], yr["id"]),
                         ("edit", False, "2021", "a:year"))

    def test_venue_diff_is_a_proposal(self):
        f = self.finding(
            "@inproceedings{p,\n author={Alice B. Smith and Bob Jones},\n"
            " title={A Great Paper on Software Testing},\n booktitle={Some Wrong Venue},\n"
            " pages={100--110},\n doi={10.1/icse20},\n year={2020}\n}")
        self.assertEqual(f["status"], "review")
        bk = self.by_field(f)["booktitle"]
        self.assertEqual((bk["op"], bk["review"]), ("edit", True))   # venue diff = a review-edit
        self.assertIn("(ICSE 2020)", bk["dblp"])

    def test_venue_short_form(self):
        f = self.finding(
            "@inproceedings{p,\n title={A Great Paper on Software Testing},\n"
            " booktitle={Some Wrong Venue},\n year={2020}\n}", short=True)
        self.assertEqual(self.by_field(f)["booktitle"]["dblp"], "Proc. {ICSE}")

    FULL_TSE = "IEEE Transactions on Software Engineering"
    ABBR_TSE = "IEEE Trans. Software Eng."

    def _article(self, journal_line, short=False):
        return self.finding(
            "@article{a,\n author={Carol Lee and Dan Park},\n"
            " title={Static Analysis for Concurrency Bugs},\n"
            " %s\n volume={47},\n number={3},\n pages={200--220},\n"
            " doi={10.1/tse21},\n year={2021}\n}" % journal_line, short)

    def test_journal_full_name_accepted_default(self):
        # Default mode prefers the full title; an entry already using it is OK.
        f = self._article("journal={%s}," % self.FULL_TSE)
        self.assertEqual(f["status"], "ok")
        self.assertNotIn("journal", self.by_field(f))

    def test_journal_abbrev_proposed_for_expansion(self):
        # Default mode: the abbreviation is offered EXPANSION to the full title (review).
        j = self.by_field(self._article("journal={%s}," % self.ABBR_TSE))["journal"]
        self.assertEqual((j["op"], j["review"], j["dblp"]), ("edit", True, self.FULL_TSE))
        self.assertIn("expand", j["note"])

    def test_journal_abbrev_accepted_short(self):
        # Under --short the abbreviation is the target, so it is OK.
        f = self._article("journal={%s}," % self.ABBR_TSE, short=True)
        self.assertNotIn("journal", self.by_field(f))

    def test_journal_full_name_proposed_for_shortening(self):
        # Under --short the full title is offered SHORTENING to the abbreviation (review).
        j = self.by_field(self._article("journal={%s}," % self.FULL_TSE, short=True))["journal"]
        self.assertEqual((j["op"], j["review"], j["dblp"]), ("edit", True, self.ABBR_TSE))
        self.assertIn("shorten", j["note"])

    def test_journal_fill_uses_full_name(self):
        # A missing journal is filled with the official full title, not the abbreviation.
        f = self.finding(
            "@article{a,\n title={Static Analysis for Concurrency Bugs},\n year={2021}\n}")
        j = self.by_field(f)["journal"]
        self.assertEqual((j["op"], j["dblp"]), ("add", self.FULL_TSE))

    def test_journal_short_form_uses_abbrev(self):
        # Under --short the proposed/filled journal is DBLP's abbreviation.
        f = self.finding(
            "@article{a,\n title={Static Analysis for Concurrency Bugs},\n year={2021}\n}",
            short=True)
        self.assertEqual(self.by_field(f)["journal"]["dblp"], self.ABBR_TSE)

    def test_venue_acronym_expanded_default(self):
        # Default mode: a bare acronym booktitle is offered EXPANSION to the canonical name.
        f = self.finding(
            "@inproceedings{p,\n author={Alice B. Smith and Bob Jones},\n"
            " title={A Great Paper on Software Testing},\n booktitle={ICSE},\n"
            " pages={100--110},\n doi={10.1/icse20},\n year={2020}\n}")
        bk = self.by_field(f)["booktitle"]
        self.assertEqual((bk["op"], bk["review"]), ("edit", True))
        self.assertIn("(ICSE 2020)", bk["dblp"])
        self.assertIn("expand", bk["note"])

    def test_venue_full_name_shortened(self):
        # Under --short the canonical conference name is offered SHORTENING to 'Proc. {ICSE}'.
        bk = self.by_field(self.finding(
            "@inproceedings{p,\n title={A Great Paper on Software Testing},\n"
            " booktitle={International Conference on Software Engineering},\n"
            " year={2020}\n}", short=True))["booktitle"]
        self.assertEqual(bk["dblp"], "Proc. {ICSE}")
        self.assertIn("shorten", bk["note"])

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
        safe = [p["id"] for p in props if not p["review"]]
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
        self.assertEqual((up["op"], up["review"]), ("replace", True))
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

    # --- suppression (ignore) -------------------------------------------------
    MISMATCH = ("@article{a,\n author={Carol Lee and Dan Park},\n"
                " title={Static Analysis for Concurrency Bugs},\n"
                " journal={IEEE Trans. Software Eng.},\n volume={47},\n number={3},\n"
                " pages={200--220},\n year={2019}\n}")   # year wrong, doi missing, journal abbrev

    def _suppress(self, finding, *sel):
        sels, _ = bc.cli_ignores(list(sel))
        bc.apply_suppressions([finding], sels)
        return finding, sels

    def test_suppress_exact_demotes_status(self):
        f, sels = self._suppress(self.finding(self.MISMATCH), "a:year")
        self.assertTrue(self.by_field(f)["year"]["suppressed"])
        self.assertFalse(self.by_field(f)["doi"]["suppressed"])
        self.assertEqual(bc._eff_status(f), "review")        # year gone -> journal review left
        self.assertEqual(sels[0]["count"], 1)
        t = bc.tally([f])
        self.assertEqual((t["mismatch"], t["suppressed"]), (0, 1))   # CI no longer fails

    def test_suppress_whole_entry(self):
        f, _ = self._suppress(self.finding(self.MISMATCH), "a:*")
        self.assertTrue(all(p["suppressed"] for p in f["proposals"]))
        self.assertEqual(bc._eff_status(f), "ok")

    def test_suppress_multi_field(self):
        f, _ = self._suppress(self.finding(self.MISMATCH), "a:year,doi")
        self.assertTrue(self.by_field(f)["year"]["suppressed"])
        self.assertTrue(self.by_field(f)["doi"]["suppressed"])

    def test_suppress_by_type_matches_only_that_type(self):
        venue = ("@inproceedings{p,\n title={A Great Paper on Software Testing},\n"
                 " booktitle={Some Wrong Venue},\n year={2020}\n}")
        f, _ = self._suppress(self.finding(venue), "@article:booktitle")
        self.assertFalse(self.by_field(f)["booktitle"]["suppressed"])   # entry is inproceedings
        g, _ = self._suppress(self.finding(venue), "@inproceedings:booktitle")
        self.assertTrue(self.by_field(g)["booktitle"]["suppressed"])

    def test_suppress_fuzzy_nag(self):
        f = self.finding("@article{g, title={Static Analysis for Quantum Bugs}, year={2020}}")
        self.assertEqual(f["status"], "fuzzy")
        f, sels = self._suppress(f, "g:*")
        self.assertTrue(f["nag_suppressed"])
        self.assertEqual(bc._eff_status(f), "ok")
        self.assertEqual(sels[0]["count"], 1)

    def test_stale_selector_counts_zero(self):
        f, sels = self._suppress(self.finding(self.MISMATCH), "Nope:year")
        self.assertEqual(sels[0]["count"], 0)
        self.assertEqual(bc._eff_status(f), "mismatch")           # nothing actually suppressed


if __name__ == "__main__":
    unittest.main()
