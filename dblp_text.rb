#!/usr/bin/env ruby

def extract_articles(io)
  begin
    require 'nokogiri'
    return extract_articles_via_nokogiri(io)
  rescue LoadError => e
    warn 'Consider installing nokogiri for more efficient extraction: `gem install nokogiri`'
    require 'rexml/document'
    return extract_articles_via_rexml(io)
  end
end

def extract_articles_via_nokogiri(io)
  result = []
  Nokogiri::XML(io).xpath('/*/article | /*/inproceedings').each do |a|
    reference = %w[journal booktitle series volume number pages]
      .map {|k| a.xpath(k).first }.compact.map {|e| e.text }.join(', ')
    result << {
      :key => a.attribute('key').to_s,
      :authors => a.xpath('author').map {|e| e.text },
      :title => a.xpath('title').first.text.sub(/\.$/, ''),
      :reference => reference,
      :year => a.xpath('year').first.text.to_i,
      :ee => a.xpath('ee').map {|e| e.text },
    }
  end
  result
end

def extract_articles_via_rexml(io)
  doc = REXML::Document.new(io)
  result = []
  REXML::XPath.each(doc, '/*/article | /*/inproceedings') do |a|
    reference = %w[journal booktitle series volume number pages]
      .map {|k| a.elements[k] }.compact.map {|e| e.text }.join(', ')
    result << {
      :key => a.attributes['key'],
      :authors => a.get_elements('author').map {|e| e.text },
      :title => REXML::XPath.match(a.elements['title'], './/text()')
                  .map {|t| t.value }.join('').sub(/\.$/, ''),
      :reference => reference,
      :year => a.elements['year'].text.to_i,
      :ee => a.get_elements('ee').map {|e| e.text },
    }
  end
  result
end

articles = extract_articles(ARGF)
articles.sort_by! {|a| [a[:year], a[:reference]] }
articles.each do |a|
  doi = a[:ee].find {|e| /doi\.org/ =~ e } || a[:ee][0] || ''
  puts "(#{a[:key]}) #{a[:authors].join(', ')}: #{a[:title]}, #{a[:reference]}, #{a[:year]}. #{doi}".strip
end
