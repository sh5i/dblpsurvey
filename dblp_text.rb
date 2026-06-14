#!/usr/bin/env ruby
# Convert the DBLP XML stream to one grep-friendly line per entry.
#
# Regex-based extraction: no XML DOM is built, so this is much faster and far
# lighter than a full-document parse.  The input is assumed to be the output of
# `xmllint --noent` (the Makefile pipeline), i.e. character/HTML entities are
# already expanded; only the predefined XML entities (& < > " ') and numeric
# references remain to unescape.  DBLP's regular, line-oriented formatting is
# assumed (the same assumption dblp_filter.rb already relies on).
require 'optparse'

Encoding.default_external = Encoding::UTF_8
$stdout.set_encoding('UTF-8')

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

ENT = { 'amp' => '&', 'lt' => '<', 'gt' => '>', 'quot' => '"', 'apos' => "'" }
def unescape(s)
  s.gsub(/&(#x[0-9A-Fa-f]+|#\d+|[A-Za-z]+);/) do
    e = $1
    if    e.start_with?('#x') then [e[2..].to_i(16)].pack('U')
    elsif e.start_with?('#')  then [e[1..].to_i].pack('U')
    else  ENT[e] || "&#{e};" end
  end
end

# Precompiled per-field matchers (interpolated regexps must not be rebuilt in the loop).
FIELD_RE = %w[journal booktitle series volume number pages title]
             .each_with_object({}) { |t, h| h[t] = %r{<#{t}\b[^>]*>(.*?)</#{t}>}m }
AUTHOR_RE = %r{<author\b[^>]*>(.*?)</author>}m
EE_RE     = %r{<ee\b[^>]*>(.*?)</ee>}m
KEY_RE    = /key="([^"]*)"/
YEAR_RE   = %r{<year>(\d+)</year>}
START_RE  = /<(?:article|inproceedings)[\s>]/
CLOSE_RE  = %r{</(?:article|inproceedings)>}

def text_of(rec, tag)              # first <tag>..</tag>, inner tags stripped, entities unescaped
  return nil unless rec =~ FIELD_RE[tag]
  unescape($1.gsub(/<[^>]+>/, ''))
end

articles = []
rec = nil
ARGF.each_line do |line|
  if rec.nil?
    next unless line =~ START_RE
    rec = line.dup
  else
    rec << line
  end
  next unless line =~ CLOSE_RE

  reference = %w[journal booktitle series volume number pages]
                .map { |k| text_of(rec, k) }.compact.join(', ')
  articles << {
    :key       => (rec[KEY_RE, 1] || ''),
    :authors   => rec.scan(AUTHOR_RE).map { |m| unescape(m[0].gsub(/<[^>]+>/, '')) },
    :title     => (text_of(rec, 'title') || '').sub(/\.$/, ''),
    :reference => reference,
    :year      => (rec[YEAR_RE, 1] || '0').to_i,
    :ee        => rec.scan(EE_RE).map { |m| unescape(m[0]) },
  }
  rec = nil
end

articles.sort_by! { |a| [a[:year], a[:reference]] }
articles.each do |a|
  key = "(#{a[:key]})"
  authors = a[:authors].join(', ')
  title = a[:title]
  ref = a[:reference]
  year = a[:year]
  doi = a[:ee].find { |e| /doi\.org/ =~ e } || a[:ee][0] || ''
  if $color
    key = "#{KEY}#{key}#{CLEAR}"
    authors = "#{AUTHORS}#{authors}#{CLEAR}"
    title = "#{TITLE}#{title}#{CLEAR}"
    doi = "#{DOI}#{doi}#{CLEAR}"
  end
  puts %Q[#{key} #{authors}: "#{title}", #{ref}, #{year}. #{doi}]
end
