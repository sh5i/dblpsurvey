.PHONY: all install update clean distclean
.DELETE_ON_ERROR:

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

dblp2text: main.go go.mod
	go build -o $@ .

dblp.txt.gz: dblp.xml.gz dblp.dtd config.yaml dblp2text
	gunzip -c dblp.xml.gz \
	  | ./dblp2text --color --config=config.yaml --dtd=dblp.dtd \
	  | gzip -c > $@
