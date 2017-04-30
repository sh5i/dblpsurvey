dblp.dtd:
	curl http://dblp.uni-trier.de/xml/dblp.dtd -o $@
dblp.xml.gz:
	curl http://dblp.uni-trier.de/xml/dblp.xml.gz -o $@

dblp_filtered.xml.gz: dblp.xml.gz dblp.dtd config.yaml
	gunzip -c dblp.xml.gz | \
	    ruby dblp_filter.rb --config=config.yaml | \
	    xmllint --noent --loaddtd - | \
	    gzip -c > $@

result.txt: dblp_filtered.xml.gz
	gunzip -c $< | ruby dblp_text.rb > $@
