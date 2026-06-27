.PHONY: all update clean distclean test
.DELETE_ON_ERROR:

# Recipes are pipelines (gunzip | extractor | sqlite3); without pipefail a crashing
# extractor is masked by sqlite3's exit 0, leaving a silently half-built DB.  bash is a
# stated prerequisite.
SHELL := bash
.SHELLFLAGS := -o pipefail -c

# Extractor: go (fast, default) or python (readable reference, easy to hack on).
# Both share the same I/O: stdin XML -> stdout text, flags --color/--config/--dtd.
EXTRACTOR ?= go
ifeq ($(EXTRACTOR),go)
EXTRACT     = ./build/dblp2text
EXTRACT_DEP = build/dblp2text
else
EXTRACT     = python3 src/dblp_text.py
EXTRACT_DEP = src/dblp_text.py vendor/pyyaml/lib/yaml/__init__.py
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

data/dblp.dtd: | data
	curl --fail -L https://dblp.org/xml/dblp.dtd -o $@
data/dblp.xml.gz: | data
	curl --fail -L https://dblp.org/xml/dblp.xml.gz -o $@

update:
	-mv data/dblp.xml.gz data/dblp.xml.gz.0
	$(MAKE)
	-rm data/dblp.xml.gz.0

clean:
	rm -f data/*.txt.gz data/*.db build/dblp2text

distclean: clean
	rm -f data/dblp.xml.gz data/dblp.xml.gz.0 data/dblp.dtd

# -mod=readonly: the repo-root vendor/ holds git submodules (not Go modules), which would
# otherwise put `go build` into vendor mode; this keeps it on the pinned go.mod/go.sum.
build/dblp2text: src/dblp_text.go go.mod go.sum | build
	go build -mod=readonly -o $@ ./src

# Verify the Go and Python extractors agree and the emitted SQL builds a queryable DB
# (test/test_extract.sh), then the dblpdb domain layer (test/test_dblpdb.py), the dblplint
# .bib-checker (test/test_dblplint.py) and the bibgraft .bib editor (test/test_bibgraft.py).
test: build/dblp2text vendor/bibtexparser/bibtexparser/splitter.py vendor/pyyaml/lib/yaml/__init__.py
	@./test/test_extract.sh
	@python3 test/test_dblpdb.py
	@python3 test/test_dblplint.py
	@python3 test/test_bibgraft.py

# bibgraft/bibspan depend on the vendored bibtexparser submodule; fetch it if absent.
vendor/bibtexparser/bibtexparser/splitter.py:
	git submodule update --init vendor/bibtexparser

# The Python extractor reads its config with the vendored PyYAML submodule; fetch if absent.
vendor/pyyaml/lib/yaml/__init__.py:
	git submodule update --init vendor/pyyaml

# One text DB per profile (data/NAME.txt.gz from config/NAME.yaml).
data/%.txt.gz: data/dblp.xml.gz data/dblp.dtd config/%.yaml $(EXTRACT_DEP) | data
	gunzip -c data/dblp.xml.gz \
	  | $(EXTRACT) --color --config=config/$*.yaml --dtd=data/dblp.dtd \
	  | gzip -c > $@

# One SQLite database per profile (data/NAME.db; needs the sqlite3 CLI with FTS5).  Final step
# derives clean conference names into proceedings.{kind,ordinal,conf_name,...} (Python post-pass,
# no LLM/network; the raw proceedings.title is left untouched).
data/%.db: data/dblp.xml.gz data/dblp.dtd config/%.yaml src/schema.sql src/dblp_confname.py $(EXTRACT_DEP) | data
	rm -f $@
	sqlite3 $@ < src/schema.sql
	gunzip -c data/dblp.xml.gz | $(EXTRACT) --format=sql --config=config/$*.yaml --dtd=data/dblp.dtd | sqlite3 $@
	sqlite3 $@ "INSERT INTO fts(key,title,authors) SELECT key,title,authors FROM entries;"
	python3 src/dblp_confname.py $@ | sqlite3 $@
