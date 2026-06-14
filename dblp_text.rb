#!/usr/bin/env ruby
# Filter the raw DBLP XML stream and emit one grep-friendly line per entry.
#
# Single pass, regex-based: no XML DOM is built and no external tools are needed.
# This merges the former dblp_filter.rb (venue/year selection) and dblp_text.rb
# (entity expansion + extraction) so the raw dump is scanned only once.  DBLP's
# regular, line-oriented formatting is assumed.
require 'optparse'
require 'yaml'

Encoding.default_external = Encoding::UTF_8
$stdout.set_encoding('UTF-8')

KEY     = "\e[32m"      # green
AUTHORS = ""
TITLE   = "\e[1m"       # bold
DOI     = "\e[36m\e[4m" # cyan underscore
CLEAR   = "\e[0m"

$color = false
$config = 'config.yaml'
$dtd = 'dblp.dtd'
ARGV.options do |q|
  q.on('--color', 'ANSI coloring') { $color = true }
  q.on('--config=s', 'preference YAML (default: config.yaml)') { |a| $config = a }
  q.on('--dtd=s', 'DTD file for entity definitions (default: dblp.dtd)') { |a| $dtd = a }
  q.parse!
end

# Preferences: which journals/conferences and which year range survive.
config = YAML.load_file($config)
lower = config['year']['lower'] rescue 1900
upper = config['year']['upper'] rescue 2100
years = (lower..upper)

# The five predefined XML entities plus the named character entities from the DBLP
# DTD (e.g. &auml; -> ä), which are all numeric character references there.  Loading
# them here lets us expand entities ourselves, replacing the `xmllint --noent` stage.
ENT = { 'amp' => '&', 'lt' => '<', 'gt' => '>', 'quot' => '"', 'apos' => "'" }
if File.exist?($dtd)
  File.foreach($dtd) do |l|
    next unless l =~ /<!ENTITY\s+(\w+)\s+"&#(x?[0-9A-Fa-f]+);"/
    name, code = $1, $2
    ENT[name] = [code.start_with?('x') ? code[1..].to_i(16) : code.to_i].pack('U')
  end
else
  warn "dblp_text.rb: DTD '#{$dtd}' not found; named entities (e.g. &auml;) will be left unexpanded"
end

def unescape(s)
  s.gsub(/&(#x[0-9A-Fa-f]+|#\d+|[A-Za-z]+);/) do
    e = $1
    if    e.start_with?('#x') then [e[2..].to_i(16)].pack('U')
    elsif e.start_with?('#')  then [e[1..].to_i].pack('U')
    else  ENT[e] || "&#{e};" end
  end
end

# Precompiled matchers (interpolated regexps must not be rebuilt in the loop).
JRX = Regexp.union(config['journals'])
CRX = Regexp.union(config['conferences'])
# Start tag of a wanted entry (one of our venues), captured to end of line.
START_RE  = %r{(<(?:article|inproceedings)\s.*key="(?:journals/#{JRX}/|conf/#{CRX}/).*)}
# A line up to and including the entry's closing tag.  In DBLP the closing tag of
# one entry and the opening tag of the next often share a line, so closing and
# (re)opening are both checked per line.
CLOSE_RE  = %r{(.*</(?:article|inproceedings)>)}
YEAR_RE   = %r{<year>(\d+)</year>}
KEY_RE    = /key="([^"]*)"/
AUTHOR_RE = %r{<author\b[^>]*>(.*?)</author>}m
EE_RE     = %r{<ee\b[^>]*>(.*?)</ee>}m
FIELD_RE  = %w[journal booktitle series volume number pages title]
              .each_with_object({}) { |t, h| h[t] = %r{<#{t}\b[^>]*>(.*?)</#{t}>}m }

def text_of(rec, tag)              # first <tag>..</tag>, inner tags stripped, entities unescaped
  return nil unless rec =~ FIELD_RE[tag]
  unescape($1.gsub(/<[^>]+>/, ''))
end

articles = []
rec = nil   # array of line fragments for the entry currently being accumulated
ARGF.each_line do |line|
  if rec
    if line =~ CLOSE_RE
      record = [*rec, $1].join('')
      rec = nil
      year = record[YEAR_RE, 1]
      if year.nil? || years.cover?(year.to_i)
        reference = %w[journal booktitle series volume number pages]
                      .map { |k| text_of(record, k) }.compact.join(', ')
        articles << {
          :key       => (record[KEY_RE, 1] || ''),
          :authors   => record.scan(AUTHOR_RE).map { |m| unescape(m[0].gsub(/<[^>]+>/, '')) },
          :title     => (text_of(record, 'title') || '').sub(/\.$/, ''),
          :reference => reference,
          :year      => (year || '0').to_i,
          :ee        => record.scan(EE_RE).map { |m| unescape(m[0]) },
        }
      end
    else
      rec << line
    end
  end
  rec = [$1] if rec.nil? && line =~ START_RE
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
