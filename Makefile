.PHONY: all install update clean distclean test
.DELETE_ON_ERROR:

# Extractor: go (fast, default) or ruby (readable reference, easy to hack on).
# Both share the same I/O: stdin XML -> stdout text, flags --color/--config/--dtd.
EXTRACTOR ?= go
ifeq ($(EXTRACTOR),go)
EXTRACT     = ./build/dblp2text
EXTRACT_DEP = build/dblp2text
else
EXTRACT     = ruby dblp_text.rb
EXTRACT_DEP = dblp_text.rb
endif

# build/ holds compiled output (the Go extractor); data/ holds the dblp dataset (the XML
# download plus the derived text/SQLite databases).  Both are git-ignored.  `| build` /
# `| data` are order-only prerequisites that just ensure the directory exists.
all: data/dblp.txt.gz data/dblp.db

build data:
	mkdir -p $@

install:
	ln -s $(realpath ./dblpsurvey) /usr/local/bin/

data/dblp.dtd: | data
	curl --fail -L https://dblp.org/xml/dblp.dtd -o $@
data/dblp.xml.gz: | data
	curl --fail -L https://dblp.org/xml/dblp.xml.gz -o $@

update:
	-mv data/dblp.xml.gz data/dblp.xml.gz.0
	$(MAKE)
	-rm data/dblp.xml.gz.0

clean:
	rm -f data/dblp_filtered.xml.gz data/dblp.txt.gz data/dblp.db build/dblp2text

distclean: clean
	rm -f data/dblp.xml.gz data/dblp.xml.gz.0 data/dblp.dtd

build/dblp2text: dblp_text.go go.mod | build
	go build -o $@ .

# Verify the Go and Ruby extractors agree and the emitted SQL builds a queryable DB
# (test/run.sh), then the dblpbib .bib-checker (test/test_dblpbib.py) and the bibgraft
# .bib editor (test/test_bibgraft.py) against fixtures.
test: build/dblp2text vendor/bibtexparser/bibtexparser/splitter.py
	@./test/run.sh
	@python3 test/test_dblpbib.py
	@python3 test/test_bibgraft.py

# bibgraft/bibspan depend on the vendored bibtexparser submodule; fetch it if absent.
vendor/bibtexparser/bibtexparser/splitter.py:
	git submodule update --init vendor/bibtexparser

data/dblp.txt.gz: data/dblp.xml.gz data/dblp.dtd config.yaml $(EXTRACT_DEP) | data
	gunzip -c data/dblp.xml.gz \
	  | $(EXTRACT) --color --config=config.yaml --dtd=data/dblp.dtd \
	  | gzip -c > $@

# SQLite database for structured / agent queries (needs the sqlite3 CLI with FTS5).
# Final step derives clean conference names into proceedings.{kind,ordinal,conf_name,...}
# (Ruby post-pass, no LLM/network; the raw proceedings.title is left untouched).
data/dblp.db: data/dblp.xml.gz data/dblp.dtd config.yaml schema.sql dblp_confname.rb $(EXTRACT_DEP) | data
	rm -f $@
	sqlite3 $@ < schema.sql
	gunzip -c data/dblp.xml.gz | $(EXTRACT) --format=sql --config=config.yaml --dtd=data/dblp.dtd | sqlite3 $@
	sqlite3 $@ "INSERT INTO fts(key,title,authors) SELECT key,title,authors FROM entries;"
	ruby dblp_confname.rb $@ | sqlite3 $@
