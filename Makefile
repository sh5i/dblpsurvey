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

# Verify the Go and Ruby extractors agree and the emitted SQL builds a queryable DB.
# The assertions live in test/run.sh (built dblp2text is a prerequisite).
test: dblp2text
	@./test/run.sh

dblp.txt.gz: dblp.xml.gz dblp.dtd config.yaml $(EXTRACT_DEP)
	gunzip -c dblp.xml.gz \
	  | $(EXTRACT) --color --config=config.yaml --dtd=dblp.dtd \
	  | gzip -c > $@

# SQLite database for structured / agent queries (needs the sqlite3 CLI with FTS5).
# Final step derives clean conference names into proceedings.{kind,ordinal,conf_name,...}
# (Ruby post-pass, no LLM/network; the raw proceedings.title is left untouched).
dblp.db: dblp.xml.gz dblp.dtd config.yaml schema.sql dblp_confname.rb $(EXTRACT_DEP)
	rm -f $@
	sqlite3 $@ < schema.sql
	gunzip -c dblp.xml.gz | $(EXTRACT) --format=sql --config=config.yaml --dtd=dblp.dtd | sqlite3 $@
	sqlite3 $@ "INSERT INTO fts(key,title,authors) SELECT key,title,authors FROM entries;"
	ruby dblp_confname.rb $@ | sqlite3 $@
