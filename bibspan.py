"""bibspan -- span-preserving BibTeX parsing on top of the vendored bibtexparser.

bibtexparser's model keeps each field's delimiter but DISCARDS inter-token whitespace
and byte offsets.  We subclass its `Splitter` to capture the offsets it computes while
lexing (and then `.strip()`s away), and rebuild a span-carrying Field/Entry that
downstream tools (bibgraft) can edit with minimal, convention-preserving diffs.

This is the ONLY module that imports or copies bibtexparser code, so all third-party
attribution is localised HERE -- keep it that way when refactoring.

API:
  entries = bibspan.parse(text)   # list[Entry], each with byte spans + format tokens
  pkg     = bibspan.bibtexparser_core()   # the vendored core (splitter/model/...)

--------------------------------------------------------------------------------------
Third-party notice.  The bodies of `_CapturingSplitter._move_to_end_of_entry` and
`_handle_entry` are copied -- with our additions bracketed by `# <bibgraft>` ...
`# </bibgraft>` -- from python-bibtexparser v2.0.0b9 (vendor/bibtexparser, pinned):

    Copyright (c) 2021 Michael Weiss
    MIT License -- full text in vendor/bibtexparser/LICENSE.

The MIT permission notice must travel with these copied portions; if this file is ever
moved out of the repo, ship vendor/bibtexparser/LICENSE (or its text) alongside it.
--------------------------------------------------------------------------------------
"""

import os
import sys

# --- vendored bibtexparser (the tokeniser) ------------------------------------

_VENDORED = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "vendor", "bibtexparser", "bibtexparser")


def bibtexparser_core():
    """Import bibtexparser's *core* (splitter/model/library/exceptions) from the
    vendored submodule WITHOUT running its __init__ -- which pulls in the middleware
    stack and a pylatexenc dependency we neither vendor nor want.  We register a stub
    `bibtexparser` package so the core modules' relative imports resolve, then load
    just those four stdlib-only modules from vendor/.  Returns the stub package (use
    `.splitter.Splitter`, `.library.Library`, `.model`); raises ImportError when the
    submodule is not checked out."""
    import importlib.util
    import types
    if "bibtexparser" in sys.modules:
        return sys.modules["bibtexparser"]
    if not os.path.isdir(_VENDORED):
        raise ImportError("vendored bibtexparser missing -- run "
                          "`git submodule update --init vendor/bibtexparser`")
    pkg = types.ModuleType("bibtexparser")
    pkg.__path__ = [_VENDORED]            # mark it a package so `.model` etc. resolve
    sys.modules["bibtexparser"] = pkg
    for name in ("exceptions", "model", "library", "splitter"):   # dependency order
        spec = importlib.util.spec_from_file_location(
            "bibtexparser." + name, os.path.join(_VENDORED, name + ".py"))
        mod = importlib.util.module_from_spec(spec)
        sys.modules["bibtexparser." + name] = mod   # register before exec for cycles
        spec.loader.exec_module(mod)
        setattr(pkg, name, mod)
    return pkg


# --- span-carrying model ------------------------------------------------------

class Field:
    """One `name = value` assignment, with every byte span and format token we need
    to edit it (or insert a sibling) without disturbing the rest of the entry."""

    def __init__(self, lead, lead_start, name, name_start, pre_eq, post_eq,
                 delim, value, val_start, val_end, comma_pos):
        self.lead = lead                  # whitespace before the name (incl. newline)
        self.lead_start = lead_start      # offset where `lead` begins (after prev comma)
        self.name = name
        self.name_start = name_start
        self.pre_eq = pre_eq              # spaces between name and '='
        self.post_eq = post_eq            # spaces between '=' and value
        self.delim = delim                # '{', '"', or '' (bare)
        self.value = value                # inner value text
        self.val_start = val_start        # span of the value token incl. delimiters
        self.val_end = val_end
        self.comma_pos = comma_pos        # offset of the trailing comma, or None
        nl = lead.rfind("\n")
        self.has_nl = nl >= 0
        self.indent = lead[nl + 1:] if nl >= 0 else ""
        self.end = (comma_pos + 1) if comma_pos is not None else val_end
        self.eq_col = len(self.indent) + len(name) + len(pre_eq)   # column of '='


class Entry:
    def __init__(self, typ, start, open_ch, open_pos, close_pos,
                 key, key_comma, fields_start, fields):
        self.type = typ
        self.start = start                # offset of the leading '@'
        self.open_ch = open_ch            # '{' or '('
        self.open_pos = open_pos
        self.close_pos = close_pos        # offset of the matching close
        self.key = key
        self.key_comma = key_comma        # offset of the comma after the key, or None
        self.fields_start = fields_start
        self.fields = fields

    def field(self, name):
        low = name.lower()
        return next((f for f in self.fields if f.name.lower() == low), None)

    def get(self, name, default=""):
        """The value of field `name` (case-insensitive), or `default` -- a convenience for
        consumers that just want the text; identical to `field(name).value`."""
        f = self.field(name)
        return f.value if f else default


# --- parse: bibtexparser's Splitter, subclassed to keep the spans it discards --

def _split_value(tok):
    """(delim, inner) from a stripped value token that still carries its enclosing:
    `{X}` -> ('{', X); `"X"` -> ('"', X); bare `X` -> ('', X)."""
    if len(tok) >= 2 and tok[0] == "{" and tok[-1] == "}":
        return "{", tok[1:-1]
    if len(tok) >= 2 and tok[0] == '"' and tok[-1] == '"':
        return '"', tok[1:-1]
    return "", tok


# Load bibtexparser HERE, at import.  It is a hard dependency (there is no fallback
# parser), so a missing submodule is a setup error -- the loader raises loudly HERE, where
# `import bibspan` fails fast with an actionable message, rather than limping on to a
# confusing failure at first parse.  Run `git submodule update --init vendor/bibtexparser`
# (or `make` -- the Makefile fetches it).
#
# Because Python runs top-to-bottom (like Ruby), the base class is ready by the time we
# subclass it just below.  We CAN'T write `import bibtexparser.splitter` (we cherry-pick the
# core via a stub package, so the name doesn't exist), but binding the base by running the
# loader first works exactly as expected.  `bp` / `Model` are the handles the copied methods
# reference.
bp = bibtexparser_core()
Model = bp.model
BlockAbortedException = bp.exceptions.BlockAbortedException


class _CapturingSplitter(bp.splitter.Splitter):
    """The span-capturing Splitter.  bibtexparser's `Splitter` owns the lexing; we override
    its two entry methods (copied from the pinned vendor and augmented) so each Field/Entry
    we emit carries its byte spans and the surrounding whitespace.  `bibstr` is the input
    prefixed with one '\\n', so an offset in the original text is `bibstr_index - 1` (`_o`)."""

    def __init__(self, text):
        super().__init__(text)
        self.bg_entries = []          # bibgraft Entry list, in file order

    def _o(self, i):                  # bibstr offset -> original-text offset
        return i - 1

    def _seg(self, a, b):
        """Split bibstr[a:b] into (leading_ws, core, core_start_offset_in_text)."""
        seg = self.bibstr[a:b]
        lead = len(seg) - len(seg.lstrip())
        core = seg.strip()
        return seg[:lead], core, self._o(a + lead)

    # The two methods below are pasted VERBATIM from bibtexparser v2.0.0b9
    # `Splitter`, with exactly three kinds of divergence:
    #   1. blocks bracketed by `# <bibgraft>` ... `# </bibgraft>` -- pure
    #      additions that side-record span/whitespace data; they never change which
    #      marks are consumed, the value returned, or any exception raised (so
    #      upstream's behaviour is intact, and dropping every `<bibgraft>`..`</bibgraft>`
    #      block reproduces the original line-for-line, modulo blank lines).
    #   2. bibtexparser's own Field/Entry/DuplicateFieldKeyBlock/ParserStateException
    #      are qualified (`Model.` / `bp.`) to dodge the name clash with this module's
    #      own Field/Entry.
    #   3. the `def` lines drop upstream's return-type annotations.
    # Everything else (control flow, messages, the stray list-vs-set for the no-field
    # `duplicate_keys`) is kept byte-for-byte so the paste diffs cleanly on upgrade.

    def _move_to_end_of_entry(self, first_key_start):
        """Move to the end of the entry and return the fields and the end index."""
        result = []
        # <bibgraft> -- parallel list of span-carrying Fields
        bg = []
        # </bibgraft>
        keys = set()
        duplicate_keys = set()

        key_start = first_key_start
        while True:
            equals_mark = self._next_mark(accept_eof=False)
            if equals_mark.group(0) == "}":
                # End of entry
                # <bibgraft>
                self._bg_fields = bg
                # </bibgraft>
                return result, equals_mark.end(), duplicate_keys

            if equals_mark.group(0) != "=":
                self._unaccepted_mark = equals_mark
                raise BlockAbortedException(
                    abort_reason="Expected a `=` after entry key, "
                    f"but found `{equals_mark.group(0)}`.",
                    end_index=equals_mark.start(),
                )

            # We follow the convention that the field start line
            #   is where the `=` between key and value is.
            start_line = self._current_line
            key_end = equals_mark.start()
            value_start = equals_mark.end()
            value_end = self._move_to_comma_or_closing_curly_bracket(
                currently_quote_escaped=False, num_open_curls=0
            )

            key = self.bibstr[key_start:key_end].strip()
            value = self.bibstr[value_start:value_end].strip()

            if key in keys:
                duplicate_keys.add(key)

            keys.add(key)
            result.append(Model.Field(start_line=start_line, key=key, value=value))

            # <bibgraft> -- rebuild the whitespace/spans the base just .strip()ped
            # off.  key_start..value_end are offsets into `bibstr` (the input with one
            # leading '\n'); `_o` maps them back to original-text offsets.
            lead, name, name_start = self._seg(key_start, key_end)
            pre_eq = self.bibstr[key_start + len(lead) + len(name):key_end]
            post_eq, vtok, val_start = self._seg(value_start, value_end)
            delim, inner = _split_value(vtok)
            f = Field(lead, self._o(key_start), name, name_start, pre_eq,
                      post_eq, delim, inner, val_start, val_start + len(vtok), None)
            bg.append(f)
            # </bibgraft>

            # If next mark is a comma, continue
            after_field_mark = self._next_mark(accept_eof=False)
            if after_field_mark.group(0) == ",":
                # <bibgraft>
                f.comma_pos = self._o(after_field_mark.start())
                f.end = f.comma_pos + 1
                # </bibgraft>
                key_start = after_field_mark.end()
            elif after_field_mark.group(0) == "}":
                # If next mark is a closing bracket, put it back (will return in next loop iteration)
                self._unaccepted_mark = after_field_mark
                continue
            else:
                self._unaccepted_mark = after_field_mark
                raise BlockAbortedException(
                    abort_reason="Expected either a `,` or `}` after a closed entry field value, "
                    f"but found a {after_field_mark.group(0)} before.",
                    end_index=after_field_mark.start(),
                )

    def _handle_entry(self, m, m_val):
        """Handle entry block. Return end index"""
        start_line = self._current_line
        entry_type = m_val[1:].strip()
        start_bracket_mark = self._next_mark(accept_eof=False)
        if start_bracket_mark.group(0) != "{":
            self._unaccepted_mark = start_bracket_mark
            # Note: The following should never happen, as we check for the "{" in the regex
            raise bp.exceptions.ParserStateException(
                message="matched a regex that should end with `{`, "
                "e.g. `@article{`, "
                "but no closing bracket was found."
            )
        # <bibgraft> -- field list for the no-comma / no-field path below
        self._bg_fields = []
        # </bibgraft>
        comma_mark = self._next_mark(accept_eof=False)
        if comma_mark.group(0) == "}":
            # This is an entry without any comma after the key, and with no fields
            #   Used e.g. by RefTeX (see issue #384)
            key = self.bibstr[m.end() + 1 : comma_mark.start()].strip()
            fields, end_index, duplicate_keys = [], comma_mark.end(), []
            # <bibgraft>
            key_comma, fields_start = None, self._o(comma_mark.start())
            # </bibgraft>
        elif comma_mark.group(0) != ",":
            self._unaccepted_mark = comma_mark
            raise BlockAbortedException(
                abort_reason=f"Expected comma after entry key, but found {comma_mark.group(0)}",
                end_index=comma_mark.end(),
            )
        else:
            self._open_brackets += 1
            key = self.bibstr[m.end() + 1 : comma_mark.start()].strip()
            fields, end_index, duplicate_keys = self._move_to_end_of_entry(comma_mark.end())
            # <bibgraft>
            key_comma, fields_start = self._o(comma_mark.start()), self._o(comma_mark.end())
            # </bibgraft>

        # <bibgraft> -- record the entry with its byte spans (key comma, body close)
        self.bg_entries.append(Entry(
            entry_type, self._o(m.start()), "{", self._o(start_bracket_mark.start()),
            self._o(end_index - 1), key, key_comma, fields_start, self._bg_fields))
        # </bibgraft>

        entry = Model.Entry(
            start_line=start_line,
            entry_type=entry_type,
            key=key,
            fields=fields,
            raw=self.bibstr[m.start() : end_index],
        )

        # If there were duplicate field keys, we return a DuplicateFieldKeyBlock wrapping
        if len(duplicate_keys) > 0:
            return Model.DuplicateFieldKeyBlock(duplicate_keys=duplicate_keys, entry=entry)
        else:
            return entry


def parse(text):
    """Parse @-entries, keeping byte spans and observed formatting.  bibtexparser's
    Splitter does the lexing; our subclass recovers the whitespace/spans it discards.
    @comment/@string/@preamble are skipped (we never touch them); a syntactically
    broken block is skipped (its Entry is simply never recorded)."""
    sp = _CapturingSplitter(text)
    sp.split()
    return sp.bg_entries
