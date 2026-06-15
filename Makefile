.PHONY: all install update clean distclean test
.DELETE_ON_ERROR:

# Extractor: go (fast, default) or ruby (readable reference, easy to hack on).
# Both share the same I/O: stdin XML -> stdout text, flags --color/--config/--dtd.
EXTRACTOR ?= go
ifeq ($(EXTRACTOR),go)
EXTRACT     = ./dblp2text
EXTRACT_DEP = dblp2text
else
EXTRACT     = ruby dblp_text.rb
EXTRACT_DEP = dblp_text.rb
endif

all: dblp.txt.gz dblp.db

install:
	ln -s $(realpath ./dblpsurvey) /usr/local/bin/

dblp.dtd:
	curl --fail -L https://dblp.org/xml/dblp.dtd -o $@
dblp.xml.gz:
	curl --fail -L https://dblp.org/xml/dblp.xml.gz -o $@

update:
	-mv dblp.xml.gz dblp.xml.gz.0
	$(MAKE)
	-rm dblp.xml.gz.0

clean:
	rm -f dblp_filtered.xml.gz dblp.txt.gz dblp.db dblp2text

distclean: clean
	rm -f dblp.xml.gz dblp.xml.gz.0 dblp.dtd

dblp2text: dblp_text.go go.mod
	go build -o $@ .

# Contract that lets dblp_text.rb stay the editable reference and Go a fast twin:
# both extractors must agree (text and sql), and the sql output must build a queryable
# DB.  Multiset comparison (sort) since tie-order of equal-key entries differs.
test: dblp2text
	@ruby dblp_text.rb --color --config=test/config.yaml --dtd=test/test.dtd < test/fixture.xml | sort > /tmp/dblp_test.rb.txt
	@./dblp2text       --color --config=test/config.yaml --dtd=test/test.dtd < test/fixture.xml | sort > /tmp/dblp_test.go.txt
	@cmp -s /tmp/dblp_test.rb.txt /tmp/dblp_test.go.txt || { echo "FAIL (text): ruby != go"; diff /tmp/dblp_test.rb.txt /tmp/dblp_test.go.txt; exit 1; }
	@ruby dblp_text.rb --format=sql --config=test/config.yaml --dtd=test/test.dtd < test/fixture.xml | sort > /tmp/dblp_test.rb.sql
	@./dblp2text       --format=sql --config=test/config.yaml --dtd=test/test.dtd < test/fixture.xml | sort > /tmp/dblp_test.go.sql
	@cmp -s /tmp/dblp_test.rb.sql /tmp/dblp_test.go.sql || { echo "FAIL (sql): ruby != go"; diff /tmp/dblp_test.rb.sql /tmp/dblp_test.go.sql; exit 1; }
	@rm -f /tmp/dblp_test.db
	@sqlite3 /tmp/dblp_test.db < schema.sql
	@./dblp2text --format=sql --config=test/config.yaml --dtd=test/test.dtd < test/fixture.xml | sqlite3 /tmp/dblp_test.db
	@sqlite3 /tmp/dblp_test.db "INSERT INTO fts(key,title,authors) SELECT key,title,authors FROM entries;"
	@test "$$(sqlite3 /tmp/dblp_test.db 'SELECT count(*) FROM entries')" = 3 || { echo "FAIL (db): row count"; exit 1; }
	@test "$$(sqlite3 /tmp/dblp_test.db "SELECT key FROM entries WHERE title_norm='onh2orefactoring'")" = journals/tse/MuellerA14 || { echo "FAIL (db): title_norm"; exit 1; }
	@test "$$(sqlite3 /tmp/dblp_test.db "SELECT key FROM fts WHERE fts MATCH 'refactoring'")" = journals/tse/MuellerA14 || { echo "FAIL (db): fts"; exit 1; }
	@test "$$(sqlite3 /tmp/dblp_test.db "SELECT key FROM entries WHERE ee LIKE '%10.1/late%'")" = journals/tse/MuellerA14 || { echo "FAIL (db): ee like"; exit 1; }
	@test "$$(sqlite3 /tmp/dblp_test.db 'SELECT count(*) FROM proceedings')" = 2 || { echo "FAIL (db): proceedings count"; exit 1; }
	@test "$$(sqlite3 /tmp/dblp_test.db "SELECT count(*) FROM proceedings WHERE key LIKE 'journals/%'")" = 1 || { echo "FAIL (db): journals-keyed proceedings"; exit 1; }
	@test "$$(sqlite3 /tmp/dblp_test.db "SELECT substr(p.title,1,21) FROM entries e JOIN proceedings p ON p.key=e.crossref WHERE e.key='conf/icse/SmithB01'")" = "Proceedings of the 23" || { echo "FAIL (db): crossref join"; exit 1; }
	@rm -f /tmp/dblp_test.rb.txt /tmp/dblp_test.go.txt /tmp/dblp_test.rb.sql /tmp/dblp_test.go.sql /tmp/dblp_test.db
	@echo "PASS: text + sql agree; db builds and queries (title_norm, fts, ee, proceedings join)"

dblp.txt.gz: dblp.xml.gz dblp.dtd config.yaml $(EXTRACT_DEP)
	gunzip -c dblp.xml.gz \
	  | $(EXTRACT) --color --config=config.yaml --dtd=dblp.dtd \
	  | gzip -c > $@

# SQLite database for structured / agent queries (needs the sqlite3 CLI with FTS5).
dblp.db: dblp.xml.gz dblp.dtd config.yaml schema.sql $(EXTRACT_DEP)
	rm -f $@
	sqlite3 $@ < schema.sql
	gunzip -c dblp.xml.gz | $(EXTRACT) --format=sql --config=config.yaml --dtd=dblp.dtd | sqlite3 $@
	sqlite3 $@ "INSERT INTO fts(key,title,authors) SELECT key,title,authors FROM entries;"
