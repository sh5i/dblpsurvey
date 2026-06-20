#!/usr/bin/env python3
"""Tests for bibgraft (convention-preserving, minimal-diff .bib editor). Stdlib only.
Run: python3 test/test_bibgraft.py  (or via `make test`)."""

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import bibgraft as bg  # noqa: E402

# brace / 2-space / no trailing comma / lowercase names; field order author<title<year
BRACE = (
    "@article{Yu2021,\n"
    "  author = {Le Yu and Xiapu Luo},\n"
    "  title = {A Great Paper},\n"
    "  journal = {TSE},\n"
    "  year = {2021}\n"
    "}\n\n"
    "@inproceedings{Smith2020,\n"
    "  author = {Alice Smith},\n"
    "  title = {Another Paper},\n"
    "  booktitle = {ICSE},\n"
    "  year = {2020}\n"
    "}\n")

# two entries in different local styles: brace+2sp vs quote+4sp
MIXED = (
    "@article{A,\n"
    "  author = {X},\n"
    "  title = {TA},\n"
    "  year = {2020}\n"
    "}\n\n"
    "@article{B,\n"
    "    author = \"Y\",\n"
    "    title = \"TB\",\n"
    "    year = \"2021\"\n"
    "}\n")


def diff_lines(a, b):
    """The set of lines that differ between two texts (order-insensitive)."""
    la, lb = a.splitlines(), b.splitlines()
    return [x for x in lb if x not in la], [x for x in la if x not in lb]


class ParseInfer(unittest.TestCase):
    def test_parse_spans(self):
        e = bg.parse(BRACE)[0]
        self.assertEqual((e.type, e.key), ("article", "Yu2021"))
        f = e.field("year")
        self.assertEqual(BRACE[f.val_start:f.val_end], "{2021}")
        self.assertEqual([x.name for x in e.fields],
                         ["author", "title", "journal", "year"])

    def test_infer_file_style(self):
        es = bg.parse(BRACE)
        st = bg.infer(es, BRACE)
        self.assertEqual((st["delim"], st["indent"], st["trailing"]), ("{", "  ", False))
        self.assertEqual(st["name_case"], "lower")
        self.assertGreater(st["before"][("author", "year")], 0)

    def test_style_flags_debug(self):
        flags = bg._style_flags(bg.infer(bg.parse(BRACE), BRACE))
        for want in ("--curly", "--space=2", "--no-align", "--no-trailing-commas",
                     "--no-numeric", "--lowercase", "--blank-lines=1"):
            self.assertIn(want, flags)
        order = next(f for f in flags if f.startswith("--sort-fields="))
        self.assertTrue(order.startswith("--sort-fields=author,") and order.endswith(",year"))

    def test_style_flags_quote_bare_tab(self):
        text = '@article{A,\n\tauthor = "X",\n\tyear = 2020\n}\n'
        flags = bg._style_flags(bg.infer(bg.parse(text), text))
        for want in ("--no-curly", "--tab", "--numeric"):
            self.assertIn(want, flags)

    def test_style_flags_entry_local_overrides(self):
        # file is brace+2-space; entry B overrides locally with quote+4-space
        es = bg.parse(MIXED)
        fs = bg.infer(es, MIXED)
        base = set(bg._style_flags(fs))
        b = next(e for e in es if e.key == "B")
        delta = [f for f in bg._style_flags(bg._entry_style(b, fs)) if f not in base]
        self.assertIn("--no-curly", delta)
        self.assertIn("--space=4", delta)


class CoreOps(unittest.TestCase):
    def test_roundtrip_no_ops_is_byte_identical(self):
        out, applied, skipped = bg.apply(BRACE, [])
        self.assertEqual(out, BRACE)
        self.assertEqual((applied, skipped), ([], []))

    def test_set_existing_is_minimal(self):
        out, _, _ = bg.apply(BRACE, [{"op": "set", "key": "Yu2021",
                                      "field": "year", "value": "2022"}])
        added, removed = diff_lines(BRACE, out)
        self.assertEqual(added, ["  year = {2022}"])
        self.assertEqual(removed, ["  year = {2021}"])

    def test_set_missing_appends_in_file_style(self):
        out, _, _ = bg.apply(BRACE, [{"op": "set", "key": "Yu2021",
                                      "field": "doi", "value": "10.1/x"}])
        self.assertIn("  year = {2021},\n  doi = {10.1/x}\n}", out)

    def test_set_missing_respects_field_order(self):
        # 'pages' is known (from another entry) to sit between title and year.
        src = ("@article{A,\n  author = {X},\n  title = {T1},\n"
               "  pages = {1--2},\n  year = {2020}\n}\n"
               "@article{B,\n  author = {Y},\n  title = {T2},\n  year = {2021}\n}\n")
        out, _, _ = bg.apply(src, [{"op": "set", "key": "B",
                                    "field": "pages", "value": "3--4"}])
        b = out.split("@article{B")[1]
        self.assertLess(b.index("title"), b.index("pages"))
        self.assertLess(b.index("pages"), b.index("year"))

    def test_remove_field(self):
        out, _, _ = bg.apply(BRACE, [{"op": "remove", "key": "Yu2021", "field": "journal"}])
        self.assertNotIn("journal", out)
        self.assertIn("  title = {A Great Paper},\n  year = {2021}\n}", out)

    def test_remove_entry(self):
        out, _, _ = bg.apply(BRACE, [{"op": "remove-entry", "key": "Yu2021"}])
        self.assertNotIn("Yu2021", out)
        self.assertTrue(out.startswith("@inproceedings{Smith2020"))

    def test_add_entry_normalises_order_and_style(self):
        op = {"op": "add-entry", "key": "New",
              "value": "@article{New, year={2030}, title={N}, author={Z}}"}
        out, applied, _ = bg.apply(MIXED, [op])
        self.assertEqual(len(applied), 1)
        block = out.split("@article{New,")[1]
        self.assertLess(block.index("author"), block.index("title"))
        self.assertLess(block.index("title"), block.index("year"))
        self.assertIn("  author = {Z},\n", out)          # file style: brace + 2-space
        self.assertTrue(out.rstrip().endswith("}"))

    def test_replace_entry_in_place(self):
        op = {"op": "replace-entry", "key": "Smith2020",
              "value": "@inproceedings{Smith2020, title={Revised}, author={A B}}"}
        out, applied, _ = bg.apply(BRACE, [op])
        self.assertEqual(len(applied), 1)
        self.assertIn("title = {Revised}", out)
        self.assertTrue(out.index("Yu2021") < out.index("Smith2020"))   # stayed in place


class Conventions(unittest.TestCase):
    def test_entry_local_style_wins(self):
        # B is quote + 4-space; a new field in B must follow B, not the file majority.
        out, _, _ = bg.apply(MIXED, [{"op": "set", "key": "B",
                                      "field": "doi", "value": "10.1/y"}])
        self.assertIn('    doi = "10.1/y"', out)

    def test_crlf_preserved(self):
        src = "@article{A,\r\n  author = {X},\r\n  year = {2020}\r\n}\r\n"
        out, _, _ = bg.apply(src, [{"op": "set", "key": "A",
                                    "field": "doi", "value": "10.1/z"}])
        self.assertIn("  year = {2020},\r\n  doi = {10.1/z}\r\n}", out)

    def test_bare_number_convention(self):
        src = "@article{A,\n  title = {T},\n  year = 2020\n}\n"     # bare year
        out, _, _ = bg.apply(src, [{"op": "set", "key": "A", "field": "volume", "value": "7"}])
        self.assertIn("volume = 7", out)                            # new int -> bare too

    def test_quote_fallback_to_braces(self):
        # value unsafe for the entry's quote delimiter -> fall back to braces.
        out, _, _ = bg.apply(MIXED, [{"op": "set", "key": "B",
                                      "field": "note", "value": 'say "hi"'}])
        self.assertIn('note = {say "hi"}', out)


class Robustness(unittest.TestCase):
    def test_idempotent(self):
        ops = [{"op": "set", "key": "Yu2021", "field": "year", "value": "2099"},
               {"op": "add-entry", "key": "New",
                "value": "@article{New, title={N}, author={Z}, year={2030}}"}]
        once, _, _ = bg.apply(BRACE, ops)
        twice, _, sk = bg.apply(once, ops)
        self.assertEqual(once, twice)
        self.assertTrue(any("key exists" in r for _, r in sk))      # add-entry now skipped

    def test_unresolved_ops_skipped_file_otherwise_unchanged(self):
        ops = [{"op": "set", "key": "Nope", "field": "year", "value": "2000"},
               {"op": "remove", "key": "Yu2021", "field": "absent"},
               {"op": "set", "key": "Yu2021", "field": "year", "value": "2022"}]
        out, applied, skipped = bg.apply(BRACE, ops)
        self.assertEqual(len(applied), 1)
        self.assertEqual(len(skipped), 2)
        self.assertIn("year = {2022}", out)
        self.assertIn("Smith2020", out)            # untouched entry intact

    def test_unbalanced_braces_warns(self):
        warns = []
        bg.apply(BRACE, [{"op": "set", "key": "Yu2021", "field": "note", "value": "a{b"}],
                 warn=warns.append)
        self.assertTrue(any("unbalanced" in w for w in warns))

    def test_insert_mode_ops(self):
        raw = "@article{New, title={N}}\n@article{Yu2021, title={Dup}}\n"
        existing = {e.key for e in bg.parse(BRACE)}
        self.assertEqual([o["op"] for o in bg._insert_ops(raw, existing, False)], ["add-entry"])
        both = bg._insert_ops(raw, existing, True)
        self.assertEqual([o["op"] for o in both], ["add-entry", "replace-entry"])


if __name__ == "__main__":
    unittest.main()
