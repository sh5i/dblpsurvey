.PHONY: all install update clean distclean test
.DELETE_ON_ERROR:

# Extractor: go (fast, default) or ruby (readable reference, easy to hack on).
# Both share the same I/O: stdin XML -> stdout text, flags --color/--config/--dtd.
EXTRACTOR ?= go
ifeq ($(EXTRACTOR),go)
EXTRACT     = ./build/dblp2text
EXTRACT_DEP = build/dblp2text
else
EXTRACT     = ruby src/dblp_text.rb
EXTRACT_DEP = src/dblp_text.rb
endif

# A "profile" NAME pairs config/NAME.yaml with its own databases data/NAME.{txt.gz,db}; the
# raw XML download (data/dblp.xml.gz, data/dblp.dtd) is shared across profiles.  `make` builds
# the default profile; `make PROFILE=ml` (or `make data/ml.db`) builds another.
PROFILE ?= default

# build/ holds compiled output (the Go extractor); data/ holds the dblp dataset (the shared
# XML download plus per-profile text/SQLite DBs).  Both are git-ignored.  `| build` / `| data`
# are order-only prerequisites that just ensure the directory exists.
all: data/$(PROFILE).txt.gz data/$(PROFILE).db

build data:
	mkdir -p $@

install:
	ln -s $(realpath ./bin/dblpsurvey) /usr/local/bin/

data/dblp.dtd: | data
	curl --fail -L https://dblp.org/xml/dblp.dtd -o $@
data/dblp.xml.gz: | data
	curl --fail -L https://dblp.org/xml/dblp.xml.gz -o $@

update:
	-mv data/dblp.xml.gz data/dblp.xml.gz.0
	$(MAKE)
	-rm data/dblp.xml.gz.0

clean:
	rm -f data/dblp_filtered.xml.gz data/*.txt.gz data/*.db build/dblp2text

distclean: clean
	rm -f data/dblp.xml.gz data/dblp.xml.gz.0 data/dblp.dtd

build/dblp2text: src/dblp_text.go go.mod | build
	go build -o $@ ./src

# Verify the Go and Ruby extractors agree and the emitted SQL builds a queryable DB
# (test/test_extract.sh), then the dblplint .bib-checker (test/test_dblplint.py) and the bibgraft
# .bib editor (test/test_bibgraft.py) against fixtures.
test: build/dblp2text vendor/bibtexparser/bibtexparser/splitter.py
	@./test/test_extract.sh
	@python3 test/test_dblplint.py
	@python3 test/test_bibgraft.py

# bibgraft/bibspan depend on the vendored bibtexparser submodule; fetch it if absent.
vendor/bibtexparser/bibtexparser/splitter.py:
	git submodule update --init vendor/bibtexparser

# One text DB per profile (data/NAME.txt.gz from config/NAME.yaml).
data/%.txt.gz: data/dblp.xml.gz data/dblp.dtd config/%.yaml $(EXTRACT_DEP) | data
	gunzip -c data/dblp.xml.gz \
	  | $(EXTRACT) --color --config=config/$*.yaml --dtd=data/dblp.dtd \
	  | gzip -c > $@

# One SQLite database per profile (data/NAME.db; needs the sqlite3 CLI with FTS5).  Final step
# derives clean conference names into proceedings.{kind,ordinal,conf_name,...} (Ruby post-pass,
# no LLM/network; the raw proceedings.title is left untouched).
data/%.db: data/dblp.xml.gz data/dblp.dtd config/%.yaml src/schema.sql src/dblp_confname.rb $(EXTRACT_DEP) | data
	rm -f $@
	sqlite3 $@ < src/schema.sql
	gunzip -c data/dblp.xml.gz | $(EXTRACT) --format=sql --config=config/$*.yaml --dtd=data/dblp.dtd | sqlite3 $@
	sqlite3 $@ "INSERT INTO fts(key,title,authors) SELECT key,title,authors FROM entries;"
	ruby src/dblp_confname.rb $@ | sqlite3 $@
