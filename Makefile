.PHONY: all install update clean distclean test
.DELETE_ON_ERROR:

# Extractor: ruby (readable reference, easy to hack on) or go (fast, "compiled").
# Both share the same I/O: stdin XML -> stdout text, flags --color/--config/--dtd.
EXTRACTOR ?= ruby
ifeq ($(EXTRACTOR),go)
EXTRACT     = ./dblp2text
EXTRACT_DEP = dblp2text
else
EXTRACT     = ruby dblp_text.rb
EXTRACT_DEP = dblp_text.rb
endif

all: dblp.txt.gz

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
	rm -f dblp_filtered.xml.gz dblp.txt.gz dblp2text

distclean: clean
	rm -f dblp.xml.gz dblp.xml.gz.0 dblp.dtd

dblp2text: dblp_text.go go.mod
	go build -o $@ .

# Verify the Ruby and Go extractors produce identical output (multiset, since the
# tie-order of equal-key entries differs between Ruby's and Go's sort).  This is the
# contract that lets dblp_text.rb stay the editable reference and Go a fast twin.
test: dblp2text
	@ruby dblp_text.rb --color --config=test/config.yaml --dtd=test/test.dtd < test/fixture.xml | sort > /tmp/dblp_test.ruby
	@./dblp2text       --color --config=test/config.yaml --dtd=test/test.dtd < test/fixture.xml | sort > /tmp/dblp_test.go
	@if cmp -s /tmp/dblp_test.ruby /tmp/dblp_test.go; then \
	  echo "PASS: ruby == go ($$(wc -l < /tmp/dblp_test.ruby | tr -d ' ') entries)"; \
	else \
	  echo "FAIL: ruby and go differ:"; diff /tmp/dblp_test.ruby /tmp/dblp_test.go; \
	  rm -f /tmp/dblp_test.ruby /tmp/dblp_test.go; exit 1; \
	fi
	@rm -f /tmp/dblp_test.ruby /tmp/dblp_test.go

dblp.txt.gz: dblp.xml.gz dblp.dtd config.yaml $(EXTRACT_DEP)
	gunzip -c dblp.xml.gz \
	  | $(EXTRACT) --color --config=config.yaml --dtd=dblp.dtd \
	  | gzip -c > $@
