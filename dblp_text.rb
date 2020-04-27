#!/usr/bin/env ruby
require 'optparse'

KEY     = "\e[32m"      # green
AUTHORS = ""
TITLE   = "\e[1m"       # bold
DOI     = "\e[36m\e[4m" # cyan underscore
CLEAR   = "\e[0m"

$color = false
ARGV.options do |q|
  q.on('--color', 'ANSI coloring') { $color = true }
  q.parse!
end

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
  key = "(#{a[:key]})"
  authors = a[:authors].join(', ')
  title = a[:title]
  ref = a[:reference]
  year = a[:year]
  doi = a[:ee].find {|e| /doi\.org/ =~ e } || a[:ee][0] || ''
  if $color
    key = "#{KEY}#{key}#{CLEAR}"
    authors = "#{AUTHORS}#{authors}#{CLEAR}"
    title = "#{TITLE}#{title}#{CLEAR}"
    doi = "#{DOI}#{doi}#{CLEAR}"
  end
  puts %Q[#{key} #{authors}: "#{title}", #{ref}, #{year}. #{doi}]
end
