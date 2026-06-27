#!/usr/bin/env bash
# Build the demo profile data/demo.{txt.gz,db} -- the 14-paper subset behind the README GIFs --
# by keeping only demo.keys from a full profile's databases (default sample-se, reproducible from
# config/sample-se.yaml + the dblp dump).  usage:  build-demo.sh [SRC_PROFILE]
set -e
cd "$(dirname "$0")"                 # work from docs/ regardless of the caller's cwd
src=${1:-sample-se}
srctxt=../data/$src.txt.gz
srcdb=../data/$src.db
for f in "$srctxt" "$srcdb"; do
  [ -e "$f" ] || { echo "build-demo.sh: missing $f -- build it first:  make PROFILE=$src" >&2; exit 1; }
done
n=$(grep -c . demo.keys)

# text DB: keep each demo.keys line in order; -F on the literal "(key)" ignores the ANSI colors
tmp=$(mktemp); trap 'rm -f "$tmp"' EXIT
gunzip -c "$srctxt" > "$tmp"
while IFS= read -r k; do [ -n "$k" ] && grep -F "($k)" "$tmp"; done < demo.keys | gzip > ../data/demo.txt.gz
got=$(gunzip -c ../data/demo.txt.gz | grep -c .)
[ "$got" = "$n" ] || { echo "build-demo.sh: text DB has $got/$n keys -- some missing from '$src'?" >&2; exit 1; }

# SQLite DB: the entries + the journals/proceedings they reference + the FTS index
keys=$(grep . demo.keys | sed "s/.*/'&'/" | paste -sd, -)
rm -f ../data/demo.db
sqlite3 ../data/demo.db < ../src/schema.sql
sqlite3 ../data/demo.db "ATTACH '$srcdb' AS d;
  INSERT INTO entries     SELECT * FROM d.entries     WHERE key IN ($keys);
  INSERT INTO journals    SELECT * FROM d.journals    WHERE abbrev IN (SELECT DISTINCT journal FROM entries WHERE journal<>'');
  INSERT INTO proceedings SELECT * FROM d.proceedings WHERE key   IN (SELECT DISTINCT crossref FROM entries WHERE crossref<>'');
  INSERT INTO fts(key,title,authors) SELECT key,title,authors FROM entries;"
got=$(sqlite3 ../data/demo.db "SELECT count(*) FROM entries;")
[ "$got" = "$n" ] || { echo "build-demo.sh: db has $got/$n entries -- some missing from '$src'?" >&2; exit 1; }

echo "build-demo.sh: built data/demo.{txt.gz,db} from '$src' ($n papers)"
