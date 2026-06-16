#!/usr/bin/env ruby
# Filter the raw DBLP XML stream and emit one entry per line.
#
# Single pass, regex-based: no XML DOM is built and no external tools are needed.
# Two output formats (--format):
#   text (default) — one grep-friendly line per entry, for the dblp.txt.gz database.
#   sql            — INSERT statements for the SQLite database (see schema.sql).
# DBLP's regular, line-oriented formatting is assumed.
require 'optparse'
require 'yaml'
require 'set'

Encoding.default_external = Encoding::UTF_8
$stdout.set_encoding('UTF-8')

KEY     = "\e[32m"      # green
AUTHORS = ""
TITLE   = "\e[1m"       # bold
DOI     = "\e[36m\e[4m" # cyan underscore
CLEAR   = "\e[0m"

$color = false
$format = 'text'
$config = 'config.yaml'
$dtd = 'dblp.dtd'
ARGV.options do |q|
  q.on('--color', 'ANSI coloring (text format)') { $color = true }
  q.on('--format=s', 'output format: text (default) or sql') { |a| $format = a }
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
  return s unless s.include?('&')          # most fields carry no entity
  s.gsub(/&(#x[0-9A-Fa-f]+|#\d+|[A-Za-z]+);/) do
    e = $1
    if    e.start_with?('#x') then [e[2..].to_i(16)].pack('U')
    elsif e.start_with?('#')  then [e[1..].to_i].pack('U')
    else  ENT[e] || "&#{e};" end
  end
end

def strip_tags(s)
  s.include?('<') ? s.gsub(/<[^>]+>/, '') : s   # most fields carry no inner markup
end

# Match the title-normalisation used by the SQLite `title_norm` column: keep ASCII
# alphanumerics only, lowercased.  Deliberately ASCII-only (no Unicode case folding)
# so Ruby, Go and the query side normalise identically (see README).
def norm_title(s)
  s.gsub(/[^A-Za-z0-9]/, '').downcase
end

def sql_quote(s)
  "'" + s.to_s.gsub("'", "''") + "'"
end

# Precompiled matchers (interpolated regexps must not be rebuilt in the loop).
JOURNALS = config['journals'].to_set
CONFS    = config['conferences'].to_set
# Start tag of an article/inproceedings with a key; captures (from-tag, kind, venue).
# Venue membership is then a hash lookup, not a huge regex alternation.
START_RE  = %r{(<(?:article|inproceedings)\s.*key="(journals|conf)/([^/"]+)/.*)}
# Proceedings volume records; captured into the `proceedings` table.  Usually conf/,
# but some workshop volumes are hosted under journals/corr (arXiv/EPTCS), so allow both
# (matching the entry rule and the Go extractor).
PROC_START_RE = %r{(<proceedings\s.*key="(journals|conf)/([^/"]+)/.*)}
# A line up to and including the entry's closing tag.  In DBLP the closing tag of
# one entry and the opening tag of the next often share a line, so closing and
# (re)opening are both checked per line.
CLOSE_RE  = %r{(.*</(?:article|inproceedings)>)}
PROC_CLOSE_RE = %r{(.*</proceedings>)}
YEAR_RE   = %r{<year>(\d+)</year>}
KEY_RE    = /key="([^"]*)"/
TYPE_RE   = %r{<(article|inproceedings)\b}
VENUE_RE  = %r{\A(?:journals|conf)/([^/]+)/}
AUTHOR_RE = %r{<author\b[^>]*>(.*?)</author>}m
EE_RE     = %r{<ee\b[^>]*>(.*?)</ee>}m
FIELD_RE  = %w[journal booktitle series volume number pages title publisher isbn url crossref]
              .each_with_object({}) { |t, h| h[t] = %r{<#{t}\b[^>]*>(.*?)</#{t}>}m }

def text_of(rec, tag)              # first <tag>..</tag>, inner tags stripped, entities unescaped
  return nil unless rec =~ FIELD_RE[tag]
  unescape(strip_tags($1))
end

REF_FIELDS = %w[journal booktitle series volume number pages]

articles = []
procs = []        # proceedings volume records (sql format only)
rec = nil         # array of line fragments for the record currently being accumulated
rectype = nil     # :entry (article/inproceedings) or :proc (proceedings)
ARGF.each_line do |line|
  if rec
    if line =~ (rectype == :proc ? PROC_CLOSE_RE : CLOSE_RE)
      record = [*rec, $1].join('')
      rt = rectype
      rec = nil
      rectype = nil
      year = record[YEAR_RE, 1]
      if year.nil? || years.cover?(year.to_i)
        if rt == :proc
          procs << {
            :key       => (record[KEY_RE, 1] || ''),
            :title     => (text_of(record, 'title') || ''),
            :booktitle => (text_of(record, 'booktitle') || ''),
            :year      => (year || '0').to_i,
            :publisher => (text_of(record, 'publisher') || ''),
            :isbn      => (text_of(record, 'isbn') || ''),
            :ee        => record.scan(EE_RE).map { |m| unescape(m[0]) }.join(' '),
            :url       => (text_of(record, 'url') || ''),
          }
        else
          fields = {}
          REF_FIELDS.each { |t| fields[t] = text_of(record, t) }
          articles << {
            :key      => (record[KEY_RE, 1] || ''),
            :type     => (record[TYPE_RE, 1] || ''),
            :venue    => ((record[KEY_RE, 1] || '')[VENUE_RE, 1] || ''),
            :year     => (year || '0').to_i,
            :authors  => record.scan(AUTHOR_RE).map { |m| unescape(strip_tags(m[0])) },
            :title    => (text_of(record, 'title') || '').sub(/\.$/, ''),
            :fields   => fields,
            :ee       => record.scan(EE_RE).map { |m| unescape(m[0]) },
            :crossref => (text_of(record, 'crossref') || ''),
          }
        end
      end
    else
      rec << line
    end
  end
  # Cheap substring guard before the regex: only record start tags carry a key
  # attribute, so most lines (authors, titles, …) are skipped here.  Venue
  # selection is a hash lookup on the captured venue token.  Proceedings are only
  # collected for the sql format (the text database is papers only).
  if rec.nil? && line.include?('key="')
    if line =~ START_RE && ($2 == 'journals' ? JOURNALS : CONFS).include?($3)
      rec = [$1]
      rectype = :entry
    elsif $format == 'sql' && line =~ PROC_START_RE && ($2 == 'journals' ? JOURNALS : CONFS).include?($3)
      rec = [$1]
      rectype = :proc
    end
  end
end

def reference_of(a)
  REF_FIELDS.map { |t| a[:fields][t] }.compact.join(', ')
end

def doi_of(a)
  a[:ee].find { |e| /doi\.org/ =~ e } || a[:ee][0] || ''
end

if $format == 'sql'
  puts 'BEGIN;'
  # Journal full names from config (abbreviation -> full title); see schema.sql `journals`.
  (config['journal_names'] || {}).sort.each do |abbrev, full|
    puts "INSERT INTO journals(abbrev, full_name) VALUES(#{sql_quote(abbrev)},#{sql_quote(full)});"
  end
  articles.each do |a|
    f = a[:fields]
    cols = [
      sql_quote(a[:key]),                # key
      sql_quote(a[:type]),               # type
      sql_quote(a[:venue]),              # venue
      a[:year].to_s,                     # year
      sql_quote(a[:authors].join(', ')), # authors
      sql_quote(a[:title]),              # title
      sql_quote(norm_title(a[:title])),  # title_norm
      sql_quote(f['journal']),           # journal
      sql_quote(f['booktitle']),         # booktitle
      sql_quote(f['volume']),            # volume
      sql_quote(f['number']),            # number
      sql_quote(f['pages']),             # pages
      sql_quote(doi_of(a)),              # doi
      sql_quote(a[:ee].join(' ')),       # ee
      sql_quote(a[:crossref]),           # crossref
    ]
    puts "INSERT INTO entries VALUES(#{cols.join(',')});"
  end
  procs.each do |p|
    cols = [
      sql_quote(p[:key]),       # key
      sql_quote(p[:title]),     # title
      sql_quote(p[:booktitle]), # booktitle
      p[:year].to_s,            # year
      sql_quote(p[:publisher]), # publisher
      sql_quote(p[:isbn]),      # isbn
      sql_quote(p[:ee]),        # ee
      sql_quote(p[:url]),       # url
    ]
    puts "INSERT INTO proceedings(key,title,booktitle,year,publisher,isbn,ee,url) VALUES(#{cols.join(',')});"
  end
  puts 'COMMIT;'
else
  articles.sort_by! { |a| [a[:year], reference_of(a)] }
  articles.each do |a|
    key = "(#{a[:key]})"
    authors = a[:authors].join(', ')
    title = a[:title]
    ref = reference_of(a)
    year = a[:year]
    doi = doi_of(a)
    if $color
      key = "#{KEY}#{key}#{CLEAR}"
      authors = "#{AUTHORS}#{authors}#{CLEAR}"
      title = "#{TITLE}#{title}#{CLEAR}"
      doi = "#{DOI}#{doi}#{CLEAR}"
    end
    puts %Q[#{key} #{authors}: "#{title}", #{ref}, #{year}. #{doi}]
  end
end
