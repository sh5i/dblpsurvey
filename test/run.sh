#!/usr/bin/env bash
# Test harness for dblpsurvey. The contract that lets dblp_text.rb stay the editable
# reference and dblp_text.go a fast twin: both extractors must agree (text and sql),
# and the emitted sql must build a queryable dblp.db. Multiset comparison (sort) since
# the tie-order of equal-key entries differs between the two.
#
# Run via `make test` (which builds ./dblp2text first), or directly: ./test/run.sh
set -uo pipefail
cd "$(dirname "$0")/.."   # repo root

TMP=$(mktemp -d "${TMPDIR:-/tmp}/dblpsurvey.XXXXXX") || exit 1
# Scratch files (db, diffs) live here. Remove them on success; on failure keep them and
# print the path, so the failing db/output can be inspected.
trap 'rc=$?; if [ "$rc" -eq 0 ]; then rm -rf "$TMP"; else echo "test artifacts kept in $TMP" >&2; fi' EXIT

DTD=test/test.dtd
FIX=test/fixture.xml
CFG=test/config.yaml
DB=$TMP/test.db
RB="ruby dblp_text.rb"
GO=./dblp2text

fail() { echo "FAIL: $*" >&2; exit 1; }
q()    { sqlite3 "$DB" "$1"; }
eq()   { [ "$2" = "$3" ] || fail "$1 (got '$2', want '$3')"; }   # eq <what> <actual> <expected>

# 1. extractors agree on the text output
$RB --color --config="$CFG" --dtd="$DTD" < "$FIX" | sort > "$TMP/rb.txt"
$GO --color --config="$CFG" --dtd="$DTD" < "$FIX" | sort > "$TMP/go.txt"
cmp -s "$TMP/rb.txt" "$TMP/go.txt" || { diff "$TMP/rb.txt" "$TMP/go.txt"; fail "text: ruby != go"; }

# 2. extractors agree on the sql output
$RB --format=sql --config="$CFG" --dtd="$DTD" < "$FIX" | sort > "$TMP/rb.sql"
$GO --format=sql --config="$CFG" --dtd="$DTD" < "$FIX" | sort > "$TMP/go.sql"
cmp -s "$TMP/rb.sql" "$TMP/go.sql" || { diff "$TMP/rb.sql" "$TMP/go.sql"; fail "sql: ruby != go"; }

# 3. the sql builds a queryable db
sqlite3 "$DB" < schema.sql
$GO --format=sql --config="$CFG" --dtd="$DTD" < "$FIX" | sqlite3 "$DB"
q "INSERT INTO fts(key,title,authors) SELECT key,title,authors FROM entries;"
ruby dblp_confname.rb "$DB" | sqlite3 "$DB"

eq "row count"                  "$(q 'SELECT count(*) FROM entries')" 3
eq "title_norm"                 "$(q "SELECT key FROM entries WHERE title_norm='onh2orefactoring'")" journals/tse/MuellerA14
eq "fts"                        "$(q "SELECT key FROM fts WHERE fts MATCH 'refactoring'")" journals/tse/MuellerA14
eq "ee like"                    "$(q "SELECT key FROM entries WHERE ee LIKE '%10.1/late%'")" journals/tse/MuellerA14
eq "proceedings count"          "$(q 'SELECT count(*) FROM proceedings')" 2
eq "journals-keyed proceedings" "$(q "SELECT count(*) FROM proceedings WHERE key LIKE 'journals/%'")" 1
eq "crossref join"              "$(q "SELECT substr(p.title,1,21) FROM entries e JOIN proceedings p ON p.key=e.crossref WHERE e.key='conf/icse/SmithB01'")" "Proceedings of the 23"
eq "conf_name"                  "$(q "SELECT conf_name FROM proceedings WHERE key='conf/icse/2001'")" "International Conference on Software Engineering"
eq "conf ordinal"               "$(q "SELECT ordinal FROM proceedings WHERE key='conf/icse/2001'")" 23
eq "workshop kind/ordinal"      "$(q "SELECT kind||'/'||ordinal FROM proceedings WHERE key='journals/corr/absWS25'")" "workshop/5"
eq "journal join"               "$(q "SELECT j.full_name FROM entries e JOIN journals j ON j.abbrev=e.journal WHERE e.key='journals/tse/MuellerA14'")" "IEEE Transactions on Software Engineering"

# 4. pass-through ("*"): exercise the shipped config-all.yaml; both extractors agree, and
#    the otherwise-filtered venue (journals/xx, absent from test/config.yaml) now survives.
$RB --format=sql --config=config-all.yaml --dtd="$DTD" < "$FIX" | sort > "$TMP/rb.all"
$GO --format=sql --config=config-all.yaml --dtd="$DTD" < "$FIX" | sort > "$TMP/go.all"
cmp -s "$TMP/rb.all" "$TMP/go.all" || { diff "$TMP/rb.all" "$TMP/go.all"; fail "wildcard: ruby != go"; }
grep -q "journals/xx/Unwanted10" "$TMP/rb.all" || fail "wildcard: off-list venue not passed through"

# 5. fail-fast: a config selecting no venues (here: empty /dev/null) must error, not emit nothing.
if $RB --format=sql --config=/dev/null --dtd="$DTD" < "$FIX" >/dev/null 2>&1; then fail "fail-fast: ruby accepted an empty config"; fi
if $GO --format=sql --config=/dev/null --dtd="$DTD" < "$FIX" >/dev/null 2>&1; then fail "fail-fast: go accepted an empty config"; fi

echo "PASS: text + sql agree; db builds and queries (title_norm, fts, ee, proceedings join, conf_name, journal join); wildcard + fail-fast"
