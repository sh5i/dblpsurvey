#!/usr/bin/env python3
"""bibgraft -- a convention-preserving, minimal-diff BibTeX editor.

Apply a stream of structured edit operations (ops) to a .bib file, touching ONLY
what the ops reference and rendering new/changed content in the file's OWN format
conventions.  Untouched entries and fields stay byte-for-byte identical.

It is not a normalizer: it never rewrites a file into one house style.  It is op
driven and producer-agnostic -- ops may come from dblplint (DBLP diffing), a style
linter, a hand patch, an LLM, ...  bibgraft knows neither their source nor reason.

Op stream (NDJSON on stdin; one self-contained op per line):

  {"op":"set",          "key":"Yu2021", "field":"year", "value":"2021"}
  {"op":"remove",       "key":"Yu2021", "field":"note"}
  {"op":"add-entry",    "key":"New24",  "value":"@article{New24,\\n ... \\n}"}
  {"op":"remove-entry", "key":"Dup20"}
  {"op":"replace-entry","key":"Old19",  "value":"@inproceedings{Old19,\\n ...\\n}"}

Positions are re-resolved at apply time by (key[,field]); raw byte offsets are not
part of the contract.  An op that cannot be resolved is warned about and skipped.

  bibgraft refs.bib < ops.ndjson              # edit in place
  bibgraft refs.bib -n < ops.ndjson           # print result to stdout, do not write
  bibgraft refs.bib --backup .bak < ops.ndjson# back up refs.bib.bak first
  bibgraft refs.bib --insert < entries.bib    # import raw entries (add-entry each)
  bibgraft refs.bib --insert --overwrite < x  # ... replacing existing keys

Set BIBGRAFT_DEBUG=1 to print the inferred house style to stderr, as bibtex-tidy-style flags
(`--curly`, `--space=2`, `--align=14`, `--sort-fields=...`; `--x-*` are bibgraft-only): one
line for the file, then `style @<key>:` lines for each entry's local overrides.

Library API:
  doc   = bibgraft.parse(text)        # entries with spans + observed formatting
  style = bibgraft.infer(doc, text)   # file-wide conventions + field-order data
  new, applied, skipped = bibgraft.apply(text, ops)
"""

import argparse
import collections
import json
import os
import re
import sys

from bibspan import parse  # span-carrying parse (Field/Entry); bibtexparser lives there

INT_RE = re.compile(r"-?\d+")

# --- convention inference -----------------------------------------------------

def _majority(xs):
    xs = list(xs)
    return collections.Counter(xs).most_common(1)[0][0] if xs else None


def _majority_or(xs, default):
    """The most common element, or `default` only when there is NO data -- so an empty list
    (None) is distinguished from a legitimately-empty inferred value like '' (no-space `=`)."""
    m = _majority(xs)
    return default if m is None else m


def _case_of(names):
    names = [n for n in names if n]
    if not names:
        return "lower"
    if all(n.islower() for n in names):
        return "lower"
    if all(n.isupper() for n in names):
        return "upper"
    if all(n[:1].isupper() and n[1:].islower() for n in names):
        return "title"
    return "asis"


def _apply_case(s, mode):
    return {"lower": s.lower(), "upper": s.upper(),
            "title": s[:1].upper() + s[1:].lower()}.get(mode, s)


def infer(entries, text):
    """File-wide conventions plus the pairwise field-order tallies.  Returns a dict the
    renderer reads; per-entry overrides are layered on at edit time (see _entry_style)."""
    allf = [f for e in entries for f in e.fields]
    nlf = [f for f in allf if f.has_nl]
    ints = [f for f in allf if INT_RE.fullmatch(f.value or "")]
    last_commas = [e.fields[-1].comma_pos is not None for e in entries if e.fields]
    blanks = [max(0, text[a.close_pos + 1:b.start].count("\n") - 1)
              for a, b in zip(entries, entries[1:])]
    before = collections.Counter()
    for e in entries:
        ns = [f.name.lower() for f in e.fields]
        for x in range(len(ns)):
            for y in range(x + 1, len(ns)):
                before[(ns[x], ns[y])] += 1
    return dict(
        newline="\r\n" if "\r\n" in text else "\n",
        delim=_majority_or([f.delim for f in allf if f.delim in ("{", '"')], "{"),
        indent=_majority_or([f.indent for f in nlf], "  "),
        pre_eq=_majority_or([f.pre_eq for f in allf], " "),
        post_eq=_majority_or([f.post_eq for f in allf], " "),
        name_case=_case_of([f.name for f in allf]),
        type_case=_case_of([e.type for e in entries]),
        trailing=bool(_majority(last_commas)) if last_commas else False,
        bare_numbers=bool(ints) and bool(_majority([f.delim == "" for f in ints])),
        blank_lines=_majority(blanks) if blanks else 1,
        aligned=False, eq_col=0, before=before)


def _entry_style(e, fs):
    """File style with this entry's own conventions layered on (entry-local preferred,
    file as fallback) -- the rule in spec section 3."""
    st = dict(fs)
    fns = e.fields
    if not fns:
        return st
    nlf = [f for f in fns if f.has_nl]
    if nlf:
        st["indent"] = _majority_or([f.indent for f in nlf], fs["indent"])
        if nlf[0].lead.count("\r\n"):
            st["newline"] = "\r\n"
    dl = _majority([f.delim for f in fns if f.delim in ("{", '"')])
    if dl:
        st["delim"] = dl
    nc = _case_of([f.name for f in fns])
    if nc != "asis":
        st["name_case"] = nc
    cols = [f.eq_col for f in nlf]
    widths = {len(f.indent) + len(f.name) for f in nlf}
    if nlf and len(set(cols)) == 1 and (len(widths) > 1 or max(len(f.pre_eq) for f in nlf) > 1):
        st["aligned"], st["eq_col"] = True, cols[0]
        st["post_eq"] = _majority_or([f.post_eq for f in nlf], fs["post_eq"])
    else:
        st["aligned"] = False
        st["pre_eq"] = _majority_or([f.pre_eq for f in fns], fs["pre_eq"])
        st["post_eq"] = _majority_or([f.post_eq for f in fns], fs["post_eq"])
    st["trailing"] = fns[-1].comma_pos is not None
    ints = [f for f in fns if INT_RE.fullmatch(f.value or "")]
    if ints:
        st["bare_numbers"] = bool(_majority([f.delim == "" for f in ints]))
    return st


# --- rendering ----------------------------------------------------------------

def _balanced(v):
    depth = 0
    for c in v:
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth < 0:
                return False
    return depth == 0


def _render_value(value, delim, warn):
    value = value or ""
    if delim == "":                              # bare requested
        if INT_RE.fullmatch(value):
            return value
        delim = "{"                              # not an integer -> must wrap
    if delim == '"':
        if '"' not in value and _balanced(value):
            return '"%s"' % value
        delim = "{"                              # unsafe in quotes -> fall back to braces
    if not _balanced(value):
        warn("unbalanced braces in value, kept as-is: %r" % value)
    return "{%s}" % value


def _render_assign(name, value, st, warn):
    nm = _apply_case(name, st["name_case"])
    if st["bare_numbers"] and INT_RE.fullmatch(value or ""):
        delim = ""
    else:
        delim = st["delim"]
    val = _render_value(value, delim, warn)
    if st["aligned"]:
        pad = st["eq_col"] - (len(st["indent"]) + len(nm))
        pre = " " * pad if pad > 0 else " "
    else:
        pre = st["pre_eq"]
    return nm + pre + "=" + st["post_eq"] + val


def _goes_before(a, b, before):
    return before.get((a, b), 0) > before.get((b, a), 0)


def _insert_index(new, names, before):
    """Where field `new` belongs among `names`: scan from the bottom, letting `new` rise
    over each field it should precede, stopping at the first it should follow.  Linear,
    deterministic even if the pairwise tallies are cyclic (spec section 3)."""
    idx = len(names)
    for i in range(len(names) - 1, -1, -1):
        if _goes_before(new, names[i], before):
            idx = i
        else:
            break
    return idx


def _order_fields(pairs, before):
    """Order (name, value) pairs by repeated deterministic insertion (used by add-entry)."""
    ordered, names = [], []
    for p in pairs:
        idx = _insert_index(p[0].lower(), names, before)
        ordered.insert(idx, p)
        names.insert(idx, p[0].lower())
    return ordered


def _render_entry(key, value, fs, warn):
    """Render a brand-new entry from an op's `@type{...}` value, fully normalized to the
    file's conventions (delimiter, indent, field order, trailing comma, type/name case)."""
    sub = parse(value)
    if not sub:
        raise ValueError("value is not a parseable @entry")
    src = sub[0]
    typ = _apply_case(src.type, fs["type_case"])
    nl, indent = fs["newline"], fs["indent"]
    st = dict(fs, aligned=False)
    ordered = _order_fields([(f.name, f.value) for f in src.fields], fs["before"])
    if not ordered:
        return "@%s{%s%s}" % (typ, key, nl)
    lines = []
    for i, (nm, val) in enumerate(ordered):
        last = i == len(ordered) - 1
        comma = "" if (last and not fs["trailing"]) else ","
        lines.append(indent + _render_assign(nm, val, st, warn) + comma)
    return "@%s{%s,%s%s%s}" % (typ, key, nl, nl.join(lines), nl)


# --- inferred-style debug (set BIBGRAFT_DEBUG=1 to dump it) --------------------

def _inferred_order(fs):
    """A representative field order derived from the pairwise tallies (for the debug dump)."""
    names = sorted({x for pair in fs["before"] for x in pair})
    return [n for n, _ in _order_fields([(n, None) for n in names], fs["before"])]

def _style_flags(fs):
    """The inferred conventions rendered as bibtex-tidy-style flags (`--x-*` = bibgraft-only).
    A debugging view of what `infer` decided the file's house style is."""
    f = ["--curly" if fs["delim"] == "{" else "--no-curly",
         "--numeric" if fs["bare_numbers"] else "--no-numeric",
         "--tab" if fs["indent"] == "\t" else "--space=%d" % len(fs["indent"]),
         "--align=%d" % fs["eq_col"] if fs["aligned"] else "--no-align",
         "--trailing-commas" if fs["trailing"] else "--no-trailing-commas",
         "--blank-lines=%d" % fs["blank_lines"] if fs["blank_lines"] else "--no-blank-lines"]
    if fs["name_case"] == "lower" and fs["type_case"] == "lower":
        f.append("--lowercase")
    else:
        f += ["--x-field-case=%s" % fs["name_case"], "--x-entry-case=%s" % fs["type_case"]]
    if not fs["aligned"]:
        f.append("--x-eq=%r" % (fs["pre_eq"] + "=" + fs["post_eq"]))
    if fs["newline"] == "\r\n":
        f.append("--x-crlf")
    order = _inferred_order(fs)
    if order:
        f.append("--sort-fields=%s" % ",".join(order))
    return f

def _debug_style(fs, entries):
    """With BIBGRAFT_DEBUG set, dump (to stderr) the file-wide style, then each entry's local
    overrides -- only the flags that differ from the file -- all as bibtex-tidy-style flags."""
    if not os.environ.get("BIBGRAFT_DEBUG"):
        return
    base = _style_flags(fs)
    sys.stderr.write("bibgraft: style: %s\n" % " ".join(base))
    baseset = set(base)
    for e in entries:
        delta = [f for f in _style_flags(_entry_style(e, fs)) if f not in baseset]
        if delta:
            sys.stderr.write("bibgraft: style @%s: %s\n" % (e.key, " ".join(delta)))


# --- op resolution (each op -> edits against the ORIGINAL text) ----------------

def _set_edit(e, fs, field, value, warn):
    """`set`: replace an existing value span (keeping that field's own delimiter), or
    insert a new field at its inferred order position in the entry's local style."""
    st = _entry_style(e, fs)
    f = e.field(field)
    if f:
        if f.delim == "" and INT_RE.fullmatch(value or ""):
            new = value
        elif f.delim == "":
            new = _render_value(value, st["delim"], warn)     # was bare, no longer an int
        else:
            new = _render_value(value, f.delim, warn)
        return (f.val_start, f.val_end, new)
    return _insert_edit(e, st, fs, field, value, warn)


def _insert_edit(e, st, fs, field, value, warn):
    multiline = any(f.has_nl for f in e.fields) if e.fields else True
    lead = (st["newline"] + st["indent"]) if multiline else " "
    assign = _render_assign(field, value, st, warn)
    tail = "," if st["trailing"] else ""
    if not e.fields:
        pre = "" if e.key_comma is not None else ","
        body = pre + lead + assign + tail + (st["newline"] if multiline else "")
        return (e.close_pos, e.close_pos, body)
    idx = _insert_index(field.lower(), [f.name.lower() for f in e.fields], fs["before"])
    if idx >= len(e.fields):
        last = e.fields[-1]
        if last.comma_pos is not None:
            return (last.comma_pos + 1, last.comma_pos + 1, lead + assign + tail)
        return (last.val_end, last.val_end, "," + lead + assign + tail)
    target = e.fields[idx]
    return (target.lead_start, target.lead_start, lead + assign + ",")


def _remove_entry_edit(text, e):
    """Delete the entry span plus one adjacent blank-line separation (spec section 4)."""
    s, t = e.start, e.close_pos + 1
    m = re.match(r"[ \t]*\r?\n(?:[ \t]*\r?\n)?", text[t:])
    if m:
        t += m.end()
    else:
        m2 = re.search(r"\r?\n[ \t]*$", text[:s])
        if m2:
            s = m2.start()
    return (s, t, "")


def _apply_edits(text, edits):
    for s, e, r in sorted(edits, key=lambda x: (x[0], x[1]), reverse=True):
        text = text[:s] + r + text[e:]
    return text


def apply(text, ops, warn=None):
    """Apply `ops` to `text`.  Returns (new_text, applied, skipped); `skipped` is a list of
    (op, reason).  Resolves every op against the original text, applies in descending offset
    (so earlier edits never shift later spans), and appends new entries at EOF."""
    warn = warn or (lambda m: None)
    entries = parse(text)
    fs = infer(entries, text)
    _debug_style(fs, entries)
    by_key = {}
    for e in entries:
        by_key.setdefault(e.key, e)
    keys = set(by_key)
    removed = set()
    edits, appends, applied, skipped = [], [], [], []

    def live(key):
        return by_key.get(key) if key not in removed else None

    for op in ops:
        op_name = op.get("op")
        key = op.get("key")
        try:
            if op_name == "set":
                e = live(key)
                if not e:
                    skipped.append((op, "no such entry: %s" % key))
                    continue
                edits.append(_set_edit(e, fs, op["field"], op["value"], warn))
                applied.append(op)
            elif op_name == "remove":
                e = live(key)
                if not e:
                    skipped.append((op, "no such entry: %s" % key))
                    continue
                f = e.field(op["field"])
                if not f:
                    skipped.append((op, "no such field: %s.%s" % (key, op["field"])))
                    continue
                edits.append((f.lead_start, f.end, ""))
                applied.append(op)
            elif op_name == "remove-entry":
                e = live(key)
                if not e:
                    skipped.append((op, "no such entry: %s" % key))
                    continue
                edits.append(_remove_entry_edit(text, e))
                removed.add(key)
                applied.append(op)
            elif op_name == "add-entry":
                if key in keys and key not in removed:
                    skipped.append((op, "key exists, use replace-entry: %s" % key))
                    continue
                appends.append(_render_entry(key, op["value"], fs, warn))
                keys.add(key)
                removed.discard(key)
                applied.append(op)
            elif op_name == "replace-entry":
                e = live(key)
                if not e:
                    skipped.append((op, "no such entry: %s" % key))
                    continue
                block = _render_entry(key, op["value"], fs, warn)
                edits.append((e.start, e.close_pos + 1, block))
                applied.append(op)
            else:
                skipped.append((op, "unknown op: %r" % op_name))
        except (KeyError, ValueError) as ex:
            skipped.append((op, "malformed op: %s" % ex))

    out = _apply_edits(text, edits)
    if appends:
        nl = fs["newline"]
        if out and not out.endswith("\n"):
            out += nl
        sep = nl * fs["blank_lines"]
        for block in appends:
            out += (sep if out else "") + block + nl    # no leading blank at start of file
    return out, applied, skipped


# --- CLI ----------------------------------------------------------------------

def _read_ops(stream):
    ops = []
    for ln in stream:
        ln = ln.strip()
        if not ln:
            continue
        try:
            ops.append(json.loads(ln))
        except json.JSONDecodeError as ex:
            sys.stderr.write("bibgraft: skipping bad op line: %s\n" % ex)
    return ops


def _insert_ops(raw, existing, overwrite, warn=None):
    warn = warn or (lambda m: None)
    ops = []
    for e in parse(raw):
        block = raw[e.start:e.close_pos + 1]
        if e.key in existing:
            if overwrite:
                ops.append({"op": "replace-entry", "key": e.key, "value": block})
            else:
                warn("key exists, skipping (use --overwrite): %s" % e.key)
        else:
            ops.append({"op": "add-entry", "key": e.key, "value": block})
    return ops


def main():
    ap = argparse.ArgumentParser(description="Convention-preserving, minimal-diff .bib editor")
    ap.add_argument("file")
    ap.add_argument("-n", "--dry-run", action="store_true",
                    help="print the result to stdout instead of editing the file")
    ap.add_argument("--backup", metavar="SUFFIX",
                    help="back up FILE to FILE+SUFFIX before editing (e.g. --backup .bak)")
    ap.add_argument("--insert", action="store_true",
                    help="read raw BibTeX from stdin and import each entry (add-entry)")
    ap.add_argument("--overwrite", action="store_true",
                    help="with --insert, replace existing keys instead of skipping them")
    ap.add_argument("-v", "--verbose", action="store_true",
                    help="report applied / skipped ops on stderr")
    args = ap.parse_args()

    # A missing file is treated as empty -- so `--insert` into a not-yet-existing .bib
    # creates it; an op stream that resolves nothing simply writes nothing.
    text = ""
    if os.path.exists(args.file):
        with open(args.file, encoding="utf-8") as fh:
            text = fh.read()

    if args.insert:
        raw = sys.stdin.read()
        ops = _insert_ops(raw, {e.key for e in parse(text)}, args.overwrite,
                          warn=lambda m: sys.stderr.write("bibgraft: %s\n" % m))
    else:
        ops = _read_ops(sys.stdin)

    warnings = []
    new_text, applied, skipped = apply(text, ops, warn=warnings.append)

    for w in warnings:
        sys.stderr.write("bibgraft: %s\n" % w)
    for op, reason in skipped:
        sys.stderr.write("bibgraft: skipped %s\n" % reason)
    if args.verbose:
        sys.stderr.write("bibgraft: %d applied, %d skipped\n" % (len(applied), len(skipped)))

    if args.dry_run:
        sys.stdout.write(new_text)
    elif new_text != text:
        if args.backup:
            with open(args.file + args.backup, "w", encoding="utf-8") as fh:
                fh.write(text)
        with open(args.file, "w", encoding="utf-8") as fh:
            fh.write(new_text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
