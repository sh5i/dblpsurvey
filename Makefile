.PHONY: all install update clean distclean
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

dblp.txt.gz: dblp.xml.gz dblp.dtd config.yaml $(EXTRACT_DEP)
	gunzip -c dblp.xml.gz \
	  | $(EXTRACT) --color --config=config.yaml --dtd=dblp.dtd \
	  | gzip -c > $@
