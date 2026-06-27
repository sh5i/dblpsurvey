# coding: utf-8
#
# dblp_confname.py — derive a clean conference name for every `proceedings` row, with no
# LLM and no network. The post-pass of `make dblp.db`:
#
#   python3 dblp_confname.py dblp.db | sqlite3 dblp.db
#
# It reads each record (via the stdlib sqlite3, READ-only) and emits UPDATE statements on
# stdout — the same "emit SQL, pipe to sqlite3" design as the extractors, so the script never
# writes the db itself. The raw `proceedings.title` is left untouched; everything derived goes
# into added columns.
#
# -- THE MODEL ────────────────────────────────────────────────────────────────
# A record (key, booktitle, year, title) is reduced to five ATOMS, stored as columns:
#
#     kind         main | workshop | companion | joint | other    — selects the prefix
#     acronym      venue short tag (= booktitle, e.g. "ICSE")      — reused as-is (the
#                  `booktitle` column already holds it)
#     ordinal      edition number (Integer) or NULL
#     ordinal_src  title | inferred | none                         — provenance of `ordinal`
#     conf_name    the reusable series name ("International Conference on Software Engineering")
#     canonical    the assembled citation string (see `build_canonical_name`):
#
#         PREFIX[kind] + ("Nth " if ordinal) + conf_name + " (" + citation_tag(acronym, year) + ")"
#
#       e.g.  kind="main", ordinal=23, acronym="ICSE", year=2001,
#             conf_name="International Conference on Software Engineering"
#         ->  "Proceedings of the 23rd International Conference on Software Engineering (ICSE 2001)"
#
# `kind` and `acronym` are easy; `ordinal` is read from the title (then gaps are filled,
# below); `conf_name` is the hard part and gets the bulk of the code.
#
# ── HOW `conf_name` IS RECOVERED ──────────────────────────────────────────────
# DBLP titles bury the series name among boilerplate, the acronym tag, the edition number,
# and the place/date. We strip the framing to a "name region", split it on commas into
# SEGMENTS, recognize which SHAPE it is, and normalize to "<qualifiers> <Type> on/of <Topic>":
#
#     direct     "Proceedings of the 9th International Conference on X, ICSE 2009, Place"
#     inverted   "X, 9th International Conference, CAiSE 2009, Place, Proceedings"   (LNCS)
#     mega       "13th National Conf on AI and 8th Innovative Applications Conf ..." (keep #1)
#     satellite  "AAAI Workshop on <topic>", "X@ICSE", companions, "(Workshops)" volumes
#
# ── ORDINAL ──────────────────────────────────────────────────────────────────
# Read from THIS title first. Then `infer_ordinals` fills gaps deterministically per venue
# series (offset = year - ordinal), interpolating/extrapolating only on LOCAL agreement so a
# renumbering or biennial gap is never crossed blindly. No crawling, no LLM guessing.
#
# Measured ~85% exact `conf_name` on the hand-checked SE set; the residue (acronym->full-name
# expansion, joint multi-workshop names) needs world knowledge and is left out here.

import re
import sqlite3
import sys

# ============================================================================
# 1. ASSEMBLY — atoms -> canonical string. The rest of the file is the inverse.
# ============================================================================

PREFIX = {
    "main": "Proceedings of the ", "workshop": "Proceedings of the ",
    "other": "Proceedings of the ", "companion": "Companion Proceedings of the ",
    "joint": "Joint Proceedings of the ",
}


# Return the ordinal suffix for n: 1->"st", 2->"nd", 3->"rd", but 11/12/13->"th".
def ordinal_suffix(n):
    if 11 <= (n % 100) <= 13:
        return "th"
    return {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")


# Build the citation tag body "ACR YEAR": the year is woven into the venue short-name,
# with several tag-only cleanups (the long conf_name is left untouched):
#     "GECCO Companion", 2025       -> "GECCO 2025 Companion"  (qualifier sits after the year)
#     "ECSA (Tracks and Workshops)" -> "ECSA 2026 Tracks and Workshops"  (parens dropped)
#     "SPLC (A)", 2025              -> "SPLC 2025"             (volume marker dropped)
#     "SIGSOFT FSE", 2012           -> "FSE 2012"             (SIG sponsor dropped)
#     "SIGMOD Conference", 2024     -> "SIGMOD 2024"          (trailing "Conference" dropped)
#     "ICSE", 2001                  -> "ICSE 2001"            (plain: year last)
def citation_tag(acronym, year):
    # Drop a SIG-sponsor prefix when another acronym follows ("SIGSOFT FSE"->"FSE",
    # "ESEC/SIGSOFT FSE"->"ESEC/FSE"); a SIG-named venue with nothing after it (SIGCSE) is kept.
    a = re.sub(r"\bSIG[A-Z]+\s+(?=[A-Z]{2,})", "", str(acronym).strip())
    a = re.sub(r"\s+Conference\Z", "", a, flags=re.I)         # "SIGMOD Conference" -> "SIGMOD"
    m = re.match(r"\A(.*\S)\s*\(([^()]*)\)\Z", a)             # trailing "(...)" group
    if m:
        base, inner = m.group(1).strip(), m.group(2).strip()
        if re.match(r"\A[0-9A-Z]{1,3}\Z", inner):            # volume marker (1)/(A) -> drop it
            return "%s %s" % (base, year)
        return "%s %s %s" % (base, year, inner)              # qualifier group -> drop parens
    m = re.match(r"\A(.*\S)\s+(Companion|Workshops?|Forum|Addendum)\Z", a, flags=re.I)
    if m:
        return "%s %s %s" % (m.group(1).strip(), year, m.group(2))  # trailing qualifier word
    return "%s %s" % (a, year)


# Build the canonical name from the atoms — the ONE place formatting lives.
def build_canonical_name(kind, ordinal, name, acronym, year):
    pfx = PREFIX["main"] if (kind == "joint" and re.search(r"\bJoint\b", name, flags=re.I)) else PREFIX[kind]  # avoid "Joint ... Joint"
    ordstr = "%d%s " % (ordinal, ordinal_suffix(ordinal)) if ordinal is not None else ""  # 0 is a valid ordinal: test `is not None`, since a truthiness test would drop it
    return "%s%s%s (%s)" % (pfx, ordstr, name, citation_tag(acronym, year))


# ============================================================================
# 2. ORDINAL WORDS — read "23rd" or "Thirty-Eighth" out of a title.
# ============================================================================

UNIT = {w: i for i, w in enumerate(
    "zeroth first second third fourth fifth sixth seventh eighth ninth tenth "
    "eleventh twelfth thirteenth fourteenth fifteenth sixteenth seventeenth "
    "eighteenth nineteenth".split())}
TENS_C = {"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60,
          "seventy": 70, "eighty": 80, "ninety": 90}              # cardinal tens
TENS_O = {"twentieth": 20, "thirtieth": 30, "fortieth": 40, "fiftieth": 50,
          "sixtieth": 60, "seventieth": 70, "eightieth": 80, "ninetieth": 90}  # ordinal tens
# Build the spelled-ordinal alternation, incl "twenty-third".  dict preserves insertion
# order (3.7+), so the alternation is emitted in a fixed order — units before tens, longest
# compound forms last — which the regex relies on when several alternatives could match.
ORD_WORD = "|".join(
    list(UNIT.keys()) + list(TENS_O.keys()) +
    ["%s-%s" % (a, b) for a in TENS_C.keys() for b in UNIT.keys()])
ORD_START_RE = re.compile(r"\A(\d{1,3}(st|nd|rd|th)|%s)\b" % ORD_WORD, re.I)

ROMAN_RE = re.compile(r"\A(C{0,3})(XC|XL|L?X{0,3})(IX|IV|V?I{0,3})\Z")  # strict roman, 1..399


# Convert a roman numeral to an Integer (None unless a clean roman of length >= 2).
def roman_to_i(r):
    if not ROMAN_RE.match(r) or len(r) < 2:
        return None
    vals = [{"I": 1, "V": 5, "X": 10, "L": 50, "C": 100}[c] for c in r.upper()]
    total = vals[-1]
    for a, b in zip(vals, vals[1:]):
        total += (-a if a < b else a)
    return total


# Find the FIRST edition number in the string (arabic, spelled, or — fallback — roman).
# "First of several" is intentional: for a mega-title the primary conference comes first.
def first_ordinal(s):
    cands = []
    m = re.search(r"\b(\d{1,3})(st|nd|rd|th)\b", s, flags=re.I)
    if m:
        cands.append((m.start(0), int(m.group(1))))
    units = "|".join(UNIT.keys())
    m = re.search(r"\b(%s)[\s-](%s)\b" % ("|".join(TENS_C.keys()), units), s, flags=re.I)
    if m:
        cands.append((m.start(0), TENS_C[m.group(1).lower()] + UNIT[m.group(2).lower()]))
    m = re.search(r"\b(%s)\b" % "|".join(TENS_O.keys()), s, flags=re.I)
    if m:
        cands.append((m.start(0), TENS_O[m.group(1).lower()]))
    m = re.search(r"\b(%s)\b" % units, s, flags=re.I)
    if m:
        cands.append((m.start(0), UNIT[m.group(1).lower()]))
    if not cands:   # roman only as a fallback — never lets it beat a real arabic/spelled ordinal
        for m in re.finditer(r"\b([IVXLC]{2,})\b", s):
            if re.search(r"\b(Part|Volume|Vol|Pt|No|Track|Session|Tier|Type|Class|Grade)\.?\s*\Z",
                         s[0:m.start(0)], flags=re.I):
                continue
            v = roman_to_i(m.group(1))
            if v is not None and 2 <= v <= 399:
                cands.append((m.start(0), v))
                break
    return min(cands, key=lambda c: c[0])[1] if cands else None


# Remove a leading ordinal token ("23rd ", "Thirteenth ") and trailing separators.
def strip_leading_ordinal(s):
    return re.sub(r"\A(\d{1,3}(st|nd|rd|th)|%s)\b[\s.,:-]*" % ORD_WORD, "", s, flags=re.I)


# ============================================================================
# 3. VOCABULARY — words that classify a title fragment.
# ============================================================================

# The event-type noun, and the same noun followed by a preposition ("Conference on ...").
# TYPEON marks a fragment that is ALREADY a complete name; TYPE alone may be type-final.
TYPE_SRC = r"(Conference|Symposium|Workshop|Congress|Colloquium|Meeting|Forum|Summit|School)"
TYPE_RE = re.compile(TYPE_SRC, re.I)
TYPEON_RE = re.compile(r"%s\s+(on|of|for|in)\b" % TYPE_SRC, re.I)
# Any event-type noun, for "is this fragment a name at all?" (broader than TYPE_RE: also
# tracks/sessions). Used by the satellite-naming rule.
NAME_TYPE_RE = re.compile(
    r"\b(Conference|Symposium|Workshop|Congress|Colloquium|Meeting|Forum|Summit|School|"
    r"Track|Competition|Session|Consortium|Tutorial|Challenge|Contest)\b", re.I)
# Leading qualifiers that decorate a bare type ("International Conference") but are not the
# topic — used to decide whether an inverted fragment still needs its topic grafted on.
QUAL = "|".join("International IEEE ACM IFIP Annual Working National European Asia-Pacific "
                "World Joint Int'l SIGSOFT SIGPLAN".split())
# Sponsor/organizer words that can sit BEFORE an edition ordinal ("IEEE 23rd ...",
# "ACM/IEEE 27th ..."); used to drop that ordinal from the reusable series name.
SPONSOR = "IEEE|ACM|IFIP|IET|Annual"

MONTHS = "|".join(
    "January February March April May June July August September October November December "
    "Jan Feb Mar Apr Jun Jul Aug Sep Sept Oct Nov Dec".split())
COUNTRIES = "|".join(
    ["USA", r"U\.S\.A\.?", "UK", r"U\.K\.", "England", "Scotland", "Wales", "Ireland",
     "Australia", "Canada", "China", "Japan", "Italy", "Spain", "France", "Germany",
     "Netherlands", "Korea", "Sweden", "Norway", "Denmark", "Finland", "Austria",
     "Switzerland", "Belgium", "Portugal", "Greece", "Hungary", "Poland", "Czech",
     "Brazil", "Colombia", "Mexico", "India", "Singapore", "Taiwan", "Israel", "Turkey",
     "New Zealand", "Cyprus", "South Africa", "Macau", "Hong Kong"])


# Return True if the segment looks like trailing place/date metadata (so the name ends before it).
# An "Nth ... Conference" segment is NOT metadata even though it starts with a number.
def location_or_date(s):
    if ORD_START_RE.search(s):
        return False
    return bool(
        re.search(r"\b(%s)\b" % MONTHS, s, flags=re.I) or re.search(r"\b(19|20)\d{2}\b", s) or
        re.search(r"\b(%s)\b" % COUNTRIES, s) or re.search(r"\A[A-Z]{2}\Z", s) or
        re.search(r"\A\d", s) or re.search(r"\d+\s*[-–]\s*\d+", s))


# ============================================================================
# 4. KIND — classify the volume. Booktitle signals are reliable; a main venue's
#    acronym never contains words like "Workshop"/"Companion".
# ============================================================================

# Classify the volume (booktitle signals are reliable; see section header).
def classify_volume(booktitle, title):
    b = str(booktitle)
    t = str(title)
    if re.search(r"\bCompanion\b", b, flags=re.I) or re.search(r"\bCompanion\b", t, flags=re.I):
        return "companion"
    if re.match(r"\AJoint Proceedings", t, flags=re.I) or re.search(r"/", b.split("@")[0]):
        return "joint"
    # Satellite events: an "@host" tag, or a satellite word in the booktitle.
    if re.search(r"@|\bWorkshops?\b|\bSymposium\b|\bDoctoral\b|\bEducators?\b|Student Research|\bDemos?\b|\bPosters?\b", b, flags=re.I):
        return "workshop"
    if re.search(r"\bWorkshops?\b", t, flags=re.I):
        return "workshop"
    if re.search(r"\b(Forum|Addendum|Tutorials?)\b", b, flags=re.I) or re.search(r"\bAddendum\b", t, flags=re.I):
        return "other"
    return "main"


# ============================================================================
# 5. NAME — the core. extract_conf_name is a pipeline of the steps below:
#      strip framing -> split into segments -> classify shape -> build -> tidy
# ============================================================================

# Strip the framing around the name: a leading tagline, leading "Proceedings of the",
#     the acronym-year marker and everything after it, a leading "ACR'95:" label, a leading
#     year/ordinal, and trailing ", Proceedings". What remains is the "name region".
def strip_framing(title, acronym):
    t = re.sub(r"\.\Z", "", str(title).strip())
    t = re.sub(r"\A[^,]{0,80}[,–-]\s+(?=Proceedings\b)", "", t, flags=re.I)   # drop a leading tagline ("Forging New Links, Proceedings ..." / "Topic - Proceedings ...")
    t = re.sub(r"[,.\s-]+(held as part of|co-located with|part of)\b.*\Z", "", t, flags=re.I)   # satellite co-location tail
    t = re.sub(r",\s*(Proceedings(,?\s*Part\s+\S+)?|Revised Selected Papers|Companion Volume)\s*\Z", "", t, flags=re.I)

    # Cut at the acronym-year marker ("..., ICSE 2009, ...") — but only if what we KEEP
    # still has a type word. Some titles put the acronym first ("Proceedings KBSE'95, the
    # Tenth ... Conference"), and there the name is AFTER the marker, so we must not cut.
    a0 = re.split(r"[@\s]", str(acronym))[0]
    if a0:
        m = re.search(r"[\s,(]%s\b[^,]*?['’]?\d{2,4}\b" % re.escape(a0), t)
        if m:
            head = t[0:m.start(0)]
            if TYPE_RE.search(head):
                t = head
    t = re.sub(r"[\s,\-]+\Z", "", t.strip())

    t = re.sub(r"\A[A-Z][A-Za-z0-9@\/+'’.\-]*(\s+['’]?\d{2,4})?\s*:\s*", "", t)        # "GECCO :" / "SOAP@PLDI 2021:" label
    t = re.sub(r"\A(An Adjunct to the\s+|Joint\s+|Companion to the\s+|Addendum to the\s+|Adjunct to the\s+|Short Paper\s+|Combined\s+|Workshop\s+|Companion\s+|Addendum\s+|Adjunct\s+)?Proceedings?\s+(of\s+the\s+|of\s+(?:an?\s+)?)?", "", t, flags=re.I)
    t = re.sub(r"\A(19|20)\d{2}\s+", "", t)                                           # leading bare year
    t = strip_leading_ordinal(t).strip()
    return re.sub(r"\Athe\s+", "", t, flags=re.I)


# Split the region into comma segments. First normalize " - " dashes: a dash is an
#     inversion boundary only when an ordinal follows ("Topic - 7th Int Conf"); otherwise
#     it joins a subtitle and stays put. Also drop a leading co-brand before such a dash.
def split_into_segments(region):
    t = re.sub(r"\A[^,]*?\s[-–]\s+(\S.*?%s\s+(?:on|of|for|in)\b)" % TYPE_SRC,
               lambda m: m.group(1), region, flags=re.I)
    t = re.sub(r"\s+[-–]\s+(?=(%s|\d{1,3}(st|nd|rd|th))\b)" % ORD_WORD, ", ", t, flags=re.I)
    out = []
    for s in re.split(r",\s*", t):
        s = re.sub(r"\Athe\s+", "", s.strip(), flags=re.I)
        if s:
            out.append(s)
    return out


# Find the inverted-layout (LNCS) segment — "[ordinal] [quals] Type" when seg0 is a bare topic.
def inverted_segment_index(segments):
    if TYPEON_RE.search(segments[0] if segments else ""):  # seg0 is already a complete name -> not inverted
        return None
    for j, s in enumerate(segments[1:]):
        if ORD_START_RE.search(s) and TYPE_RE.search(s):
            return j + 1
    return None


# Rebuild the name from an inverted layout (segments[index] is the "Nth ... Type" segment).
def build_inverted_name(segments, index):
    typ = strip_leading_ordinal(segments[index]).strip()
    if TYPEON_RE.search(typ):                     # "International Conference on Quality ..." already complete
        return typ
    bare = re.sub(r"\A(%s|\s)+" % QUAL, "", typ, flags=re.I).strip()  # strip "International/IEEE/..." decorators
    if not re.match(r"\A%s\b" % TYPE_SRC, bare, flags=re.I):  # "SEI CSEE Conference" has its own topic -> keep as-is
        return typ
    # Truly bare ("International Conference") — graft on the topic from the leading segments.
    topic = ", ".join(s for s in segments[0:index]
                      if not re.match(r"\A[A-Z][A-Za-z0-9]*(['’]\d{2})?\Z", s))  # drop pure-acronym segments
    if not topic:
        topic = ", ".join(segments[0:index])
    topic = re.sub(r"\A[A-Z][A-Za-z0-9]*(['’]\d{2})?\s*[:.\-]\s+", "", topic).strip()  # drop "OOER'95: " prefix
    return "%s on %s" % (typ, topic)


# Build the name from a direct layout: take seg0, and extend it only across an
# "X, Y, and Z" list (never into place/date metadata).
def build_direct_name(segments):
    name = (segments[0] if segments else "").strip()
    if re.search(r"\band\b", name):                  # the list, if any, already closes inside seg0
        return name
    extra = []
    for s in segments[1:]:
        if location_or_date(s):
            break
        extra.append(s.strip())
        if re.search(r"\band\b", s):
            break
    if extra and re.search(r"\band\b", extra[-1]):
        return ", ".join([name] + extra)
    return name


STOPWORDS = "a an the of on in for and to with at by from as or nor but per via".split()


# Title-case an all-lowercase (ACM-DL style) name: capitalise each word, but keep interior
# stopwords lower and leave any word that already has uppercase (acronyms, "iOS") untouched.
def titlecase_lower(name):
    state = {"first": True}

    def repl(m):
        w = m.group(0)
        lead = state["first"]
        state["first"] = False
        if re.search(r"[A-Z]", w):
            return w
        if not lead and w.lower() in STOPWORDS:
            return w
        return re.sub(r"[a-z]", lambda c: c.group(0).upper(), w, count=1)

    return re.sub(r"\S+", repl, name)


# Clean up an assembled name, and apply the satellite-prefix rule.
def tidy_name(name, kind, parent):
    name = re.sub(r"\s(19|20)\d{2}(?=\s)", " ", re.sub(r"['’]\d{2}\b", "", name))  # drop inline year: "SIGPLAN'95", "ACM 2003"
    name = re.sub(r"[\s,–-]+(19|20)\d{2}\Z", "", name)                          # trailing year ("... Conference 2013")
    name = re.sub(r"[\s,]+(V\.?\s*\d+|Volume\s+\S+)\Z", "", name, flags=re.I)   # trailing "V.1" / "Volume 2"
    if kind != "joint":
        name = re.sub(r"\s*\([A-Z][A-Za-z0-9'’\/+&.\- ]*\)\s*\Z", "", name)     # trailing "(PLDI)"
    name = re.sub(r"\s[-–]\s*[A-Z][A-Za-z0-9&\/+.\-]*\Z", "", name)             # dangling "- ECCV" left after the year
    if kind == "companion":
        name = re.sub(r"\s+Companion\Z", "", name, flags=re.I)                  # companion name = parent name
    name = re.sub(r"\s*[-–:;]+\s*\Z", "", name)                                 # dangling trailing punctuation
    name = re.sub(r"\s{2,}", " ", name).strip()
    # An edition ordinal must not survive in the reusable series name: build_canonical_name
    # re-adds it from the ordinal atom, so keeping one yields "the 23rd IEEE 23rd ...". Drop it
    # whether it sits after a sponsor prefix ("IEEE 23rd International ..." -> "IEEE International
    # ...") or is leading ("31st International ..." from an inverted LNCS title -> "International ...").
    name = re.sub(r"\A((?:%s)(?:[\s\/&]+(?:%s))*)\s+(?:\d{1,3}(?:st|nd|rd|th)|%s)\b[\s.,:-]*" % (SPONSOR, SPONSOR, ORD_WORD),
                  r"\1 ", name, flags=re.I)
    name = strip_leading_ordinal(name).strip()
    if re.match(r"\A[a-z]", name):   # ACM-DL lowercase title -> Title Case
        name = titlecase_lower(name)
    # Topic-only satellite ("Agent Modeling" under conf/aaai) -> "AAAI Workshop on Agent Modeling".
    if kind == "workshop" and parent and name and not NAME_TYPE_RE.search(name) and not name.startswith(parent):
        name = "%s Workshop on %s" % (parent, name)
    return name


# Return the host venue's acronym, used to name topic-only satellites. Only for conf/* (the
# journals/corr workshops are independent and have no useful parent).
def parent_acronym(key):
    return str(key).split("/")[1].upper() if str(key).startswith("conf/") else None


# Recover the series name — the pipeline: strip_framing -> split_into_segments -> classify -> build -> tidy.
def extract_conf_name(key, title, acronym, kind):
    segments = split_into_segments(strip_framing(title, acronym))
    index = inverted_segment_index(segments)
    if index is not None:
        name = build_inverted_name(segments, index)
    else:
        # Skip a leading co-brand segment ("Software Evolution Week, IEEE Conference on ...").
        start = 0
        if not (segments and TYPEON_RE.search(segments[0])):
            ti = next((i for i, s in enumerate(segments) if TYPEON_RE.search(s)), None)
            if ti is not None and ti > 0:
                start = ti
        name = build_direct_name(segments[start:])
        name = re.sub(r"\s+and\s+(%s|\d{1,3}(st|nd|rd|th))\b.*\Z" % ORD_WORD, "", name, flags=re.I)  # mega-title: drop 2nd conference
    return tidy_name(name, kind, parent_acronym(key))


# ============================================================================
# 6. ANALYZE — one proceedings record -> atoms (title-stage ordinal only).
# ============================================================================

# Reduce one proceedings record to its five atoms (the public entry point).
def extract_atoms(key, booktitle, year, title):
    acronym = str(booktitle).strip()
    if not acronym:
        m = re.search(r"[(,]\s*([A-Z][A-Za-z0-9\/+@.'’-]*)\s+['’]?\d{2,4}\b", str(title))
        if m:
            acronym = m.group(1)                              # empty booktitle -> recover tag from "..., SSBSE 2025"
    kind = classify_volume(booktitle, title)
    ord_ = first_ordinal(str(title))
    if kind == "joint":
        # A joint volume bundling >=2 separately-numbered events has no single ordinal; one
        # with a single numbered parent keeps it. (Co-located parent numbers don't count.)
        main = re.split(r"co-located with", str(title), maxsplit=1, flags=re.I)[0]
        events = len(re.findall(
            r"\b(?:\d{1,3}(?:st|nd|rd|th)|%s)\b[^,;:]{0,40}?(?:Conference|Symposium|Workshop|Congress|Colloquium)" % ORD_WORD,
            main, flags=re.I))
        if events >= 2:
            ord_ = None
    return {"acronym": acronym, "ordinal": ord_, "ordinal_src": ("title" if ord_ is not None else "none"),
            "kind": kind, "name": extract_conf_name(key, title, acronym, kind)}


# ============================================================================
# 7. ORDINAL INFERENCE — fill gaps deterministically across a venue series.
#    Group by (venue, full acronym). The event year comes from the KEY (the DB `year` is
#    the publication year and can differ). Fill a gap only by LOCAL evidence: interpolate
#    when bracketing anchors agree on offset; extrapolate only off a run of >=2 same-offset
#    anchors, so a renumbering / biennial gap is not crossed blindly.
# ============================================================================

# Parse the event year from the key ("conf/caise/94" -> 1994, "conf/models/2013jp" -> 2013).
def event_year_from_key(k):
    s = str(k).split("/")[-1]
    m = re.match(r"\A(\d{4})", s)
    if m:
        return int(m.group(1))
    m = re.match(r"\A(\d{2})(?!\d)", s)
    if m:
        return 1900 + int(m.group(1))
    return None


# Fill None ordinals in place. Mutates each record's "ord" and "src" ("src" becomes "inferred").
def infer_ordinals(records):
    for r in records:
        ky = event_year_from_key(r["key"])
        r["ey"] = ky if (ky is not None and abs(ky - r["year"]) <= 3) else r["year"]
    # Group by (venue-prefix, acronym), preserving first-seen order: dict keeps insertion
    # order, and setdefault appends without reordering, so groups stay in input order.
    groups = {}
    for r in records:
        g = ("/".join(str(r["key"]).split("/")[0:2]), str(r["acronym"]).lower())
        groups.setdefault(g, []).append(r)
    for grp in groups.values():
        anchors = sorted(([r["ey"], r["ord"]] for r in grp if r["ord"] is not None),
                         key=lambda a: a[0])
        if not anchors:
            continue
        for r in (r for r in grp if r["ord"] is None):
            y = r["ey"]
            prevs = [a for a in anchors if a[0] < y]
            nxts = [a for a in anchors if a[0] > y]
            prev = prevs[-1] if prevs else None
            nxt = nxts[0] if nxts else None
            off = None
            if prev and nxt and (prev[0] - prev[1]) == (nxt[0] - nxt[1]):
                off = prev[0] - prev[1]                       # interpolation: neighbours agree
            elif prev and not nxt:
                po = prev[0] - prev[1]
                run = 0
                for ay, ao in reversed(prevs):
                    if ay - ao == po:
                        run += 1
                    else:
                        break
                if run >= 2:
                    off = po                                  # forward extrapolation, run>=2
            elif nxt and not prev:
                no = nxt[0] - nxt[1]
                run = 0
                for ay, ao in nxts:
                    if ay - ao == no:
                        run += 1
                    else:
                        break
                if run >= 2:
                    off = no                                  # backward extrapolation, run>=2
            if off is not None:
                r["ord"] = y - off
                r["src"] = "inferred"


# ============================================================================
# 8. DRIVER — read the proceedings table, emit UPDATEs on stdout.
# ============================================================================

# Quote a value for SQL (single quotes doubled).
def sql_quote(s):
    return "'" + str(s).replace("'", "''") + "'"


def main():
    if len(sys.argv) < 2:
        sys.exit("usage: python3 dblp_confname.py dblp.db | sqlite3 dblp.db")
    db = sys.argv[1]
    records = []
    # Open READ-only; the unordered SELECT returns rows in rowid (insert) order, which fixes
    # the order of the emitted UPDATEs.
    con = sqlite3.connect("file:%s?mode=ro" % db, uri=True)
    try:
        cur = con.execute("SELECT key, IFNULL(booktitle,''), IFNULL(year,0), IFNULL(title,'') FROM proceedings")
        for key, booktitle, yr, ti in cur:
            if key is None:
                continue
            yr = int(yr)
            a = extract_atoms(key, str(booktitle), yr, str(ti))
            records.append({"key": key, "year": yr, "booktitle": str(booktitle),
                            "acronym": a["acronym"], "ord": a["ordinal"], "src": a["ordinal_src"],
                            "kind": a["kind"], "name": a["name"]})
    finally:
        con.close()
    infer_ordinals(records)                                          # fill ordinal gaps across series
    out = []
    out.append("BEGIN;")
    for r in records:
        canon = build_canonical_name(r["kind"], r["ord"], r["name"], r["booktitle"], r["year"])
        out.append(
            "UPDATE proceedings SET "
            "kind=%s, "
            "ordinal=%s, "
            "ordinal_src=%s, "
            "conf_name=%s, "
            "canonical=%s "
            "WHERE key=%s;" % (
                sql_quote(r["kind"]),
                (str(r["ord"]) if r["ord"] is not None else "NULL"),
                sql_quote(r["src"]),
                sql_quote(r["name"]),
                sql_quote(canon),
                sql_quote(r["key"]),
            ))
    out.append("COMMIT;")
    sys.stdout.write("\n".join(out) + "\n")


if __name__ == "__main__":
    main()
