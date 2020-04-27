all: dblp.txt.gz

install:
	ln -s $(realpath ./dblpsurvey) /usr/local/bin/

dblp.dtd:
	curl https://dblp.org/xml/dblp.dtd -o $@
dblp.xml.gz:
	curl https://dblp.org/xml/dblp.xml.gz -o $@

update:
	-rm dblp.xml.gz
	$(MAKE)

dblp_filtered.xml.gz: dblp.xml.gz dblp.dtd config.yaml
	gunzip -c dblp.xml.gz \
	  | ruby dblp_filter.rb --config=config.yaml \
	  | xmllint --noent --loaddtd - \
	  | gzip -c > $@

dblp.txt.gz: dblp_filtered.xml.gz
	gunzip -c $< \
	  | ruby dblp_text.rb --color \
	  | gzip -c > $@
