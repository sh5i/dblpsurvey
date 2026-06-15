# coding: utf-8
#
# dblp_confname.rb — derive a clean conference name for every `proceedings` row, with no
# LLM and no network. It is the final step of `make dblp.db` (see the Makefile):
#
#   ruby dblp_confname.rb dblp.db | sqlite3 dblp.db
#
# It reads each record via the sqlite3 CLI and emits UPDATE statements on stdout — the same
# "emit SQL, pipe to sqlite3" design as the extractors, so no native sqlite gem is needed.
# The raw `proceedings.title` is left untouched; everything derived goes into added columns.
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
# SEGMENTS, recognise which SHAPE it is, and normalise to "<qualifiers> <Type> on/of <Topic>":
#
#     direct     "Proceedings of the 9th International Conference on X, ICSE 2009, Place"
#     inverted   "X, 9th International Conference, CAiSE 2009, Place, Proceedings"   (LNCS)
#     mega       "13th National Conf on AI and 8th Innovative Applications Conf ..." (keep #1)
#     satellite  "AAAI Workshop on <topic>", "X@ICSE", companions, "(Workshops)" volumes
#
# ── ORDINAL ──────────────────────────────────────────────────────────────────
# Read from THIS title first. Then `infer_ordinals!` fills gaps deterministically per venue
# series (offset = year - ordinal), interpolating/extrapolating only on LOCAL agreement so a
# renumbering or biennial gap is never crossed blindly. No crawling, no LLM guessing.
#
# Measured ~85% exact `conf_name` on the hand-checked SE set; the residue (acronym->full-name
# expansion, joint multi-workshop names) needs world knowledge and is left out here. The
# dev/eval harness, the DBLP-index crawler, and the LLM ground truth all live in canon/.

Encoding.default_external = Encoding::UTF_8

# ============================================================================
# 1. ASSEMBLY — atoms -> canonical string. The rest of the file is the inverse.
# ============================================================================

PREFIX = {
  "main" => "Proceedings of the ", "workshop" => "Proceedings of the ",
  "other" => "Proceedings of the ", "companion" => "Companion Proceedings of the ",
  "joint" => "Joint Proceedings of the ",
}

# Return the ordinal suffix for n: 1->"st", 2->"nd", 3->"rd", but 11/12/13->"th".
# @param n [Integer]
# @return [String] the suffix ("st"/"nd"/"rd"/"th")
def ordinal_suffix(n)
  return "th" if (11..13).include?(n % 100)
  { 1 => "st", 2 => "nd", 3 => "rd" }[n % 10] || "th"
end

# Build the citation tag body "ACR YEAR": the year is woven into the venue short-name,
# with several tag-only cleanups (the long conf_name is left untouched):
#     "GECCO Companion", 2025       -> "GECCO 2025 Companion"  (qualifier sits after the year)
#     "ECSA (Tracks and Workshops)" -> "ECSA 2026 Tracks and Workshops"  (parens dropped)
#     "SPLC (A)", 2025              -> "SPLC 2025"             (volume marker dropped)
#     "SIGSOFT FSE", 2012           -> "FSE 2012"             (SIG sponsor dropped)
#     "SIGMOD Conference", 2024     -> "SIGMOD 2024"          (trailing "Conference" dropped)
#     "ICSE", 2001                  -> "ICSE 2001"            (plain: year last)
# @param acronym [String] venue tag, may carry a qualifier
# @param year [Integer, String]
# @return [String] tag body WITHOUT parens (build_canonical_name adds them)
def citation_tag(acronym, year)
  # Drop a SIG-sponsor prefix when another acronym follows ("SIGSOFT FSE"->"FSE",
  # "ESEC/SIGSOFT FSE"->"ESEC/FSE"); a SIG-named venue with nothing after it (SIGCSE) is kept.
  a = acronym.to_s.strip.gsub(/\bSIG[A-Z]+\s+(?=[A-Z]{2,})/, "")
  a = a.sub(/\s+Conference\z/i, "")                          # "SIGMOD Conference" -> "SIGMOD"
  if (m = a.match(/\A(.*\S)\s*\(([^()]*)\)\z/))               # trailing "(...)" group
    base, inner = m[1].strip, m[2].strip
    inner =~ /\A[0-9A-Z]{1,3}\z/ ? "#{base} #{year}"            # volume marker (1)/(A) -> drop it
                                 : "#{base} #{year} #{inner}"    # qualifier group -> drop parens
  elsif (m = a.match(/\A(.*\S)\s+(Companion|Workshops?|Forum|Addendum)\z/i))
    "#{m[1].strip} #{year} #{m[2]}"                            # trailing qualifier word
  else
    "#{a} #{year}"
  end
end

# Build the canonical name from the atoms — the ONE place formatting lives.
# @param kind [String] one of main/workshop/companion/joint/other
# @param ordinal [Integer, nil] edition number, or nil for "no ordinal"
# @param name [String] the reusable series name (conf_name)
# @param acronym [String] the venue tag (booktitle)
# @param year [Integer, String]
# @return [String] the canonical string
# @example
#   build_canonical_name("main", 23, "International Conference on Software Engineering", "ICSE", 2001)
#   #=> "Proceedings of the 23rd International Conference on Software Engineering (ICSE 2001)"
def build_canonical_name(kind, ordinal, name, acronym, year)
  pfx = (kind == "joint" && name =~ /\bJoint\b/i) ? PREFIX["main"] : PREFIX[kind]  # avoid "Joint ... Joint"
  ordstr = ordinal ? "#{ordinal}#{ordinal_suffix(ordinal)} " : ""        # nil ordinal -> no slot at all
  "#{pfx}#{ordstr}#{name} (#{citation_tag(acronym, year)})"
end

# ============================================================================
# 2. ORDINAL WORDS — read "23rd" or "Thirty-Eighth" out of a title.
# ============================================================================

UNIT = %w[zeroth first second third fourth fifth sixth seventh eighth ninth tenth
          eleventh twelfth thirteenth fourteenth fifteenth sixteenth seventeenth
          eighteenth nineteenth].each_with_index.to_h
TENS_C = { "twenty" => 20, "thirty" => 30, "forty" => 40, "fifty" => 50, "sixty" => 60,
           "seventy" => 70, "eighty" => 80, "ninety" => 90 }              # cardinal tens
TENS_O = { "twentieth" => 20, "thirtieth" => 30, "fortieth" => 40, "fiftieth" => 50,
           "sixtieth" => 60, "seventieth" => 70, "eightieth" => 80, "ninetieth" => 90 } # ordinal tens
ORD_WORD = ([UNIT.keys, TENS_O.keys].flatten +
            TENS_C.keys.product(UNIT.keys).map {|a, b| "#{a}-#{b}" }).join("|")  # incl "twenty-third"
ORD_START_RE = /\A(\d{1,3}(st|nd|rd|th)|#{ORD_WORD})\b/i

# Find the FIRST edition number in the string (arabic or spelled).
# "First of several" is intentional: for a mega-title the primary conference comes first.
# @param s [String] a title
# @return [Integer, nil] the edition number, or nil if none found
def first_ordinal(s)
  cands = []
  cands << [$~.begin(0), $1.to_i]                                 if s =~ /\b(\d{1,3})(st|nd|rd|th)\b/i
  units = UNIT.keys.join("|")
  cands << [$~.begin(0), TENS_C[$1.downcase] + UNIT[$2.downcase]] if s =~ /\b(#{TENS_C.keys.join("|")})[\s-](#{units})\b/i
  cands << [$~.begin(0), TENS_O[$1.downcase]]                     if s =~ /\b(#{TENS_O.keys.join("|")})\b/i
  cands << [$~.begin(0), UNIT[$1.downcase]]                       if s =~ /\b(#{units})\b/i
  cands.min_by {|pos, _| pos }&.last
end

# Remove a leading ordinal token ("23rd ", "Thirteenth ") and trailing separators.
# @param s [String]
# @return [String]
def strip_leading_ordinal(s)
  s.sub(/\A(\d{1,3}(st|nd|rd|th)|#{ORD_WORD})\b[\s.,:-]*/i, "")
end

# ============================================================================
# 3. VOCABULARY — words that classify a title fragment.
# ============================================================================

# The event-type noun, and the same noun followed by a preposition ("Conference on ...").
# TYPEON marks a fragment that is ALREADY a complete name; TYPE alone may be type-final.
TYPE_RE   = /(Conference|Symposium|Workshop|Congress|Colloquium|Meeting|Forum|Summit|School)/i
TYPEON_RE = /#{TYPE_RE.source}\s+(on|of|for|in)\b/i
# Any event-type noun, for "is this fragment a name at all?" (broader than TYPE_RE: also
# tracks/sessions). Used by the satellite-naming rule.
NAME_TYPE_RE = /\b(Conference|Symposium|Workshop|Congress|Colloquium|Meeting|Forum|Summit|School|Track|Competition|Session|Consortium|Tutorial|Challenge|Contest)\b/i
# Leading qualifiers that decorate a bare type ("International Conference") but are not the
# topic — used to decide whether an inverted fragment still needs its topic grafted on.
QUAL = %w[International IEEE ACM IFIP Annual Working National European Asia-Pacific
          World Joint Int'l SIGSOFT SIGPLAN].join("|")

MONTHS    = %w[January February March April May June July August September October November December
               Jan Feb Mar Apr Jun Jul Aug Sep Sept Oct Nov Dec].join("|")
COUNTRIES = ["USA", "U\\.S\\.A\\.?", "UK", "U\\.K\\.", "England", "Scotland", "Wales", "Ireland",
             "Australia", "Canada", "China", "Japan", "Italy", "Spain", "France", "Germany",
             "Netherlands", "Korea", "Sweden", "Norway", "Denmark", "Finland", "Austria",
             "Switzerland", "Belgium", "Portugal", "Greece", "Hungary", "Poland", "Czech",
             "Brazil", "Colombia", "Mexico", "India", "Singapore", "Taiwan", "Israel", "Turkey",
             "New Zealand", "Cyprus", "South Africa", "Macau", "Hong Kong"].join("|")

# Return true if the segment looks like trailing place/date metadata (so the name ends before it).
# An "Nth ... Conference" segment is NOT metadata even though it starts with a number.
# @param s [String] a segment
# @return [Boolean]
def location_or_date?(s)
  return false if s =~ ORD_START_RE
  s =~ /\b(#{MONTHS})\b/i || s =~ /\b(19|20)\d{2}\b/ || s =~ /\b(#{COUNTRIES})\b/ ||
    s =~ /\A[A-Z]{2}\z/ || s =~ /\A\d/ || s =~ /\d+\s*[-–]\s*\d+/
end

# ============================================================================
# 4. KIND — classify the volume. Booktitle signals are reliable; a main venue's
#    acronym never contains words like "Workshop"/"Companion".
# ============================================================================

# Classify the volume (booktitle signals are reliable; see section header).
# @param booktitle [String]
# @param title [String]
# @return [String] one of main/workshop/companion/joint/other
def classify_volume(booktitle, title)
  b = booktitle.to_s
  t = title.to_s
  return "companion" if b =~ /\bCompanion\b/i || t =~ /\bCompanion\b/i
  return "joint"     if t =~ /\AJoint Proceedings/i || b.split("@").first.to_s =~ %r{/}
  # Satellite events: an "@host" tag, or a satellite word in the booktitle.
  return "workshop"  if b =~ /@|\bWorkshops?\b|\bSymposium\b|\bDoctoral\b|\bEducators?\b|Student Research|\bDemos?\b|\bPosters?\b/i
  return "workshop"  if t =~ /\bWorkshops?\b/i
  return "other"     if b =~ /\b(Forum|Addendum|Tutorials?)\b/i || t =~ /\bAddendum\b/i
  "main"
end

# ============================================================================
# 5. NAME — the core. extract_conf_name is a pipeline of the steps below:
#      strip framing -> split into segments -> classify shape -> build -> tidy
# ============================================================================

# Strip the framing around the name: a leading tagline, leading "Proceedings of the",
#     the acronym-year marker and everything after it, a leading "ACR'95:" label, a leading
#     year/ordinal, and trailing ", Proceedings". What remains is the "name region".
# @param title [String]
# @param acronym [String] venue tag
# @return [String] the name region
def strip_framing(title, acronym)
  t = title.to_s.strip.sub(/\.\z/, "")
  t = t.sub(/\A[^,]{0,80},\s+(?=Proceedings\b)/i, "")   # drop a leading tagline ("Forging New Links, Proceedings of the ...")
  t = t.sub(/[,.\s-]+(held as part of|co-located with|part of)\b.*\z/i, "")   # satellite co-location tail
  t = t.sub(/,\s*(Proceedings(,?\s*Part\s+\S+)?|Revised Selected Papers|Companion Volume)\s*\z/i, "")

  # Cut at the acronym-year marker ("..., ICSE 2009, ...") — but only if what we KEEP
  # still has a type word. Some titles put the acronym first ("Proceedings KBSE'95, the
  # Tenth ... Conference"), and there the name is AFTER the marker, so we must not cut.
  a0 = acronym.to_s.split(/[@\s]/).first.to_s
  unless a0.empty?
    if (m = t.match(/[\s,(]#{Regexp.escape(a0)}\b[^,]*?['’]?\d{2,4}\b/))
      head = t[0...m.begin(0)]
      t = head if head =~ TYPE_RE
    end
  end
  t = t.strip.sub(/[\s,\-]+\z/, "")

  t = t.sub(/\A[A-Z][A-Za-z0-9@\/+'’.\-]*(\s+['’]?\d{2,4})?\s*:\s*/, "")        # "GECCO :" / "SOAP@PLDI 2021:" label
  t = t.sub(/\A(Joint\s+|Companion to the\s+|Addendum to the\s+)?Proceedings?\s+(of\s+the\s+|of\s+)?/i, "")
  t = t.sub(/\A(19|20)\d{2}\s+/, "")                                           # leading bare year
  t = strip_leading_ordinal(t).strip
  t.sub(/\Athe\s+/i, "")
end

# Split the region into comma segments. First normalise " - " dashes: a dash is an
#     inversion boundary only when an ordinal follows ("Topic - 7th Int Conf"); otherwise
#     it joins a subtitle and stays put. Also drop a leading co-brand before such a dash.
# @param region [String]
# @return [Array<String>] comma segments
def split_into_segments(region)
  t = region.sub(/\A[^,]*?\s[-–]\s+(\S.*?#{TYPE_RE.source}\s+(?:on|of|for|in)\b)/i) { $1 }
  t = t.gsub(/\s+[-–]\s+(?=(#{ORD_WORD}|\d{1,3}(st|nd|rd|th))\b)/i, ", ")
  t.split(/,\s*/).map {|s| s.strip.sub(/\Athe\s+/i, "") }.reject(&:empty?)
end

# Find the inverted-layout (LNCS) segment — "[ordinal] [quals] Type" when seg0 is a bare topic.
# @param segments [Array<String>]
# @return [Integer, nil] index of the "Nth ... Type" segment, or nil
def inverted_segment_index(segments)
  return nil if segments[0].to_s =~ TYPEON_RE          # seg0 is already a complete name -> not inverted
  j = segments[1..].to_a.index {|s| s =~ ORD_START_RE && s =~ TYPE_RE }
  j && j + 1
end

# Rebuild the name from an inverted layout (segments[index] is the "Nth ... Type" segment).
# @param segments [Array<String>]
# @param index [Integer] index of the "Nth ... Type" segment
# @return [String] the name
def build_inverted_name(segments, index)
  typ = strip_leading_ordinal(segments[index]).strip
  return typ if typ =~ TYPEON_RE                    # "International Conference on Quality ..." already complete
  bare = typ.sub(/\A(#{QUAL}|\s)+/i, "").strip      # strip "International/IEEE/..." decorators
  return typ unless bare =~ /\A#{TYPE_RE.source}\b/i # "SEI CSEE Conference" has its own topic -> keep as-is
  # Truly bare ("International Conference") — graft on the topic from the leading segments.
  topic = segments[0...index].reject {|s| s =~ /\A[A-Z][A-Za-z0-9]*(['’]\d{2})?\z/ }.join(", ")  # drop pure-acronym segments
  topic = segments[0...index].join(", ") if topic.empty?
  topic = topic.sub(/\A[A-Z][A-Za-z0-9]*(['’]\d{2})?\s*[:.\-]\s+/, "").strip                  # drop "OOER'95: " prefix
  "#{typ} on #{topic}"
end

# Build the name from a direct layout: take seg0, and extend it only across an
# "X, Y, and Z" list (never into place/date metadata).
# @param segments [Array<String>]
# @return [String] the name
def build_direct_name(segments)
  name = segments[0].to_s.strip
  return name if name =~ /\band\b/                  # the list, if any, already closes inside seg0
  extra = []
  segments[1..].to_a.each do |s|
    break if location_or_date?(s)
    extra << s.strip
    break if s =~ /\band\b/
  end
  (!extra.empty? && extra.last =~ /\band\b/) ? ([name] + extra).join(", ") : name
end

# Clean up an assembled name, and apply the satellite-prefix rule.
# @param name [String]
# @param kind [String]
# @param parent [String, nil] host venue acronym, or nil
# @return [String] the cleaned name
def tidy_name(name, kind, parent)
  name = name.gsub(/['’]\d{2}\b/, "").gsub(/\s(19|20)\d{2}(?=\s)/, " ")  # drop inline year: "SIGPLAN'95", "ACM 2003"
  name = name.sub(/[\s,]+(V\.?\s*\d+|Volume\s+\S+)\z/i, "")              # trailing "V.1" / "Volume 2"
  name = name.gsub(/\s*\([A-Z][A-Za-z0-9'’\/+&.\- ]*\)\s*\z/, "") if kind != "joint"  # trailing "(PLDI)"
  name = name.sub(/\s+Companion\z/i, "") if kind == "companion"         # companion name = parent name
  name = name.sub(/\s*[-–:;]+\s*\z/, "")                                # dangling trailing punctuation
  name = name.gsub(/\s{2,}/, " ").strip
  # Topic-only satellite ("Agent Modeling" under conf/aaai) -> "AAAI Workshop on Agent Modeling".
  if kind == "workshop" && parent && !name.empty? && name !~ NAME_TYPE_RE && !name.start_with?(parent)
    name = "#{parent} Workshop on #{name}"
  end
  name
end

# Return the host venue's acronym, used to name topic-only satellites. Only for conf/* (the
# journals/corr workshops are independent and have no useful parent).
# @param key [String] dblp key
# @return [String, nil] UPPER-cased venue acronym for conf/*, else nil
def parent_acronym(key)
  key.to_s.start_with?("conf/") ? key.split("/")[1].to_s.upcase : nil
end

# Recover the series name — the pipeline: strip_framing -> split_into_segments -> classify -> build -> tidy.
# @param key [String]
# @param title [String]
# @param acronym [String] venue tag
# @param kind [String]
# @return [String] the conf_name
def extract_conf_name(key, title, acronym, kind)
  segments = split_into_segments(strip_framing(title, acronym))
  if (index = inverted_segment_index(segments))
    name = build_inverted_name(segments, index)
  else
    # Skip a leading co-brand segment ("Software Evolution Week, IEEE Conference on ...").
    start = (segments[0].to_s !~ TYPEON_RE && (ti = segments.index {|s| s =~ TYPEON_RE }) && ti > 0) ? ti : 0
    name = build_direct_name(segments[start..])
    name = name.sub(/\s+and\s+(#{ORD_WORD}|\d{1,3}(st|nd|rd|th))\b.*\z/i, "")  # mega-title: drop 2nd conference
  end
  tidy_name(name, kind, parent_acronym(key))
end

# ============================================================================
# 6. ANALYZE — one proceedings record -> atoms (title-stage ordinal only).
# ============================================================================

# Reduce one proceedings record to its five atoms (the public entry point).
# @param key [String] dblp key
# @param booktitle [String]
# @param year [Integer]
# @param title [String]
# @return [Hash{String=>Object}] atoms — "acronym", "ordinal" (Integer|nil),
#   "ordinal_src" (title|none), "kind", "name"
def extract_atoms(key, booktitle, year, title)
  acronym = booktitle.to_s.strip
  if acronym.empty? && (m = title.to_s.match(/[(,]\s*([A-Z][A-Za-z0-9\/+@.'’-]*)\s+['’]?\d{2,4}\b/))
    acronym = m[1]                                       # empty booktitle -> recover tag from "..., SSBSE 2025"
  end
  kind = classify_volume(booktitle, title)
  ord  = first_ordinal(title.to_s)
  if kind == "joint"
    # A joint volume bundling >=2 separately-numbered events has no single ordinal; one
    # with a single numbered parent keeps it. (Co-located parent numbers don't count.)
    main = title.to_s.split(/co-located with/i, 2).first.to_s
    events = main.scan(/\b(?:\d{1,3}(?:st|nd|rd|th)|#{ORD_WORD})\b[^,;:]{0,40}?(?:Conference|Symposium|Workshop|Congress|Colloquium)/i).size
    ord = nil if events >= 2
  end
  { "acronym" => acronym, "ordinal" => ord, "ordinal_src" => (ord ? "title" : "none"),
    "kind" => kind, "name" => extract_conf_name(key, title, acronym, kind) }
end

# ============================================================================
# 7. ORDINAL INFERENCE — fill gaps deterministically across a venue series.
#    Group by (venue, full acronym). The event year comes from the KEY (the DB `year` is
#    the publication year and can differ). Fill a gap only by LOCAL evidence: interpolate
#    when bracketing anchors agree on offset; extrapolate only off a run of >=2 same-offset
#    anchors, so a renumbering / biennial gap is not crossed blindly.
# ============================================================================

# Parse the event year from the key ("conf/caise/94" -> 1994, "conf/models/2013jp" -> 2013).
# @param k [String] dblp key
# @return [Integer, nil]
def event_year_from_key(k)
  s = k.split("/").last.to_s
  (m = s.match(/\A(\d{4})/)) ? m[1].to_i : ((m = s.match(/\A(\d{2})(?!\d)/)) ? 1900 + m[1].to_i : nil)
end

# Fill nil ordinals in place. Mutates each record's :ord and :src (:src becomes "inferred").
# @param records [Array<Hash>] records with :key, :year, :acronym, :ord; gains :ey (event year)
# @return [void]
def infer_ordinals!(records)
  records.each {|r| ky = event_year_from_key(r[:key]); r[:ey] = (ky && (ky - r[:year]).abs <= 3) ? ky : r[:year] }
  records.group_by {|r| [r[:key].split("/")[0, 2].join("/"), r[:acronym].to_s.downcase] }.each_value do |grp|
    anchors = grp.select {|r| r[:ord] }.map {|r| [r[:ey], r[:ord]] }.sort_by {|a| a[0] }
    next if anchors.empty?
    grp.select {|r| r[:ord].nil? }.each do |r|
      y = r[:ey]
      prevs = anchors.select {|ay, _| ay < y }; nxts = anchors.select {|ay, _| ay > y }
      prev = prevs.last; nxt = nxts.first; off = nil
      if prev && nxt && (prev[0] - prev[1]) == (nxt[0] - nxt[1])
        off = prev[0] - prev[1]                                  # interpolation: neighbours agree
      elsif prev && !nxt
        po = prev[0] - prev[1]; run = 0
        prevs.reverse_each {|ay, ao| (ay - ao == po) ? run += 1 : break }
        off = po if run >= 2                                     # forward extrapolation, run>=2
      elsif nxt && !prev
        no = nxt[0] - nxt[1]; run = 0
        nxts.each {|ay, ao| (ay - ao == no) ? run += 1 : break }
        off = no if run >= 2                                     # backward extrapolation, run>=2
      end
      (r[:ord] = y - off; r[:src] = "inferred") if off
    end
  end
end

# ============================================================================
# 8. DRIVER — read the proceedings table, emit UPDATEs on stdout.
# ============================================================================

# Quote a value for SQL (single quotes doubled).
# @param s [Object]
# @return [String] a quoted SQL literal, e.g. "'O''Hara'"
def sql_quote(s)
  "'" + s.to_s.gsub("'", "''") + "'"
end

if __FILE__ == $0
  db = ARGV[0] or abort "usage: ruby dblp_confname.rb dblp.db | sqlite3 dblp.db"
  records = []
  IO.popen(["sqlite3", "-separator", "\t", db,
            "SELECT key, IFNULL(booktitle,''), IFNULL(year,0), IFNULL(title,'') FROM proceedings"]) do |io|
    io.each_line do |l|
      key, booktitle, yr, ti = l.chomp.split("\t", 4)
      next unless key
      a = extract_atoms(key, booktitle.to_s, yr.to_i, ti.to_s)
      records << { key: key, year: yr.to_i, booktitle: booktitle.to_s,
                acronym: a["acronym"], ord: a["ordinal"], src: a["ordinal_src"],
                kind: a["kind"], name: a["name"] }
    end
  end
  infer_ordinals!(records)                                          # fill ordinal gaps across series
  puts "BEGIN;"
  records.each do |r|
    canon = build_canonical_name(r[:kind], r[:ord], r[:name], r[:booktitle], r[:year])
    puts "UPDATE proceedings SET " \
         "kind=#{sql_quote(r[:kind])}, " \
         "ordinal=#{r[:ord] || 'NULL'}, " \
         "ordinal_src=#{sql_quote(r[:src])}, " \
         "conf_name=#{sql_quote(r[:name])}, " \
         "canonical=#{sql_quote(canon)} " \
         "WHERE key=#{sql_quote(r[:key])};"
  end
  puts "COMMIT;"
end
