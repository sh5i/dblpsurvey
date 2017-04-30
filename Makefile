dblp.dtd:
	curl http://dblp.uni-trier.de/xml/dblp.dtd -o $@
dblp.xml.gz:
	curl http://dblp.uni-trier.de/xml/dblp.xml.gz -o $@

filtered.xml: dblp.xml.gz venues.yaml
	gunzip -c dblp.xml.gz | ruby dblp_filter.rb > $@

noent.xml: filtered.xml dblp.dtd
	xmllint --noent --loaddtd filtered.xml --output $@

result.txt: noent.xml
	ruby dblp_text.rb $< > $@
