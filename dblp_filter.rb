#!/usr/bin/env ruby
require 'yaml'
require 'optparse'

$conf = 'config.yaml'
ARGV.options do |q|
  q.on('--config=s', "set config yaml (default: config.yaml)") {|a| $conf = a }
  q.on('--help', 'print this message') { puts q ; exit 0 }
  q.parse!
end

config = YAML.load_file($conf)

# year-range
lower = config['year']['lower'] rescue 1900
upper = config['year']['upper'] rescue 2100
years = (lower .. upper)

puts '<?xml version="1.0" encoding="UTF-8"?>'
puts '<!DOCTYPE dblp SYSTEM "dblp.dtd">'
puts '<dblp>'

article = nil
ARGF.each do |line|
  if article
    if %r[(.*</(?:article|inproceedings)>)] =~ line
      puts [ *article, $1 ].join('')
      article = nil
    else
      article << line
    end
    if %r[<year>(\d+)</year>] =~ line
      article = nil unless years.cover?($1.to_i)
    end
  end

  unless article
    if %r[(<(?:article|inproceedings)\s.*key="(?:
            journals/#{Regexp.union(config['journals'])} |
            conf/#{Regexp.union(config['conferences'])} ).*)]ox =~ line
      article = [ $1 ]
    end
  end
end

puts '</dblp>'
