#!/usr/bin/env python3
# Filter the raw DBLP XML stream and emit one entry per line.
#
# Single pass, regex-based: no XML DOM is built and no external tools are needed.
# Two output formats (--format):
#   text (default) — one grep-friendly line per entry, for the dblp.txt.gz database.
#   sql            — INSERT statements for the SQLite database (see schema.sql).
# DBLP's regular, line-oriented formatting is assumed.  This is the readable Python
# reference; it must stay byte-for-byte in step with dblp_text.go (see `make test`).
import argparse
import os
import re
import sys

# Vendored PyYAML
_VENDORED_YAML = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "vendor", "pyyaml", "lib")
if not os.path.isdir(_VENDORED_YAML):
    sys.exit("dblp_text.py: vendored PyYAML missing -- run `git submodule update --init vendor/pyyaml`")
sys.path.insert(0, _VENDORED_YAML)
import yaml

# ANSI SGR escape codes for --color text output (applied per field in main()).
RESET     = "\x1b[0m"
BOLD      = "\x1b[1m"
UNDERLINE = "\x1b[4m"
GREEN     = "\x1b[32m"
CYAN      = "\x1b[36m"

# The five predefined XML entities plus the named character entities from the DBLP DTD (e.g. &auml; -> ä)
def load_dtd(path):
    ent = {'amp': '&', 'lt': '<', 'gt': '>', 'quot': '"', 'apos': "'"}
    entity_re = re.compile(r'<!ENTITY\s+(\w+)\s+"&#(x?[0-9A-Fa-f]+);"')
    try:
        f = open(path, encoding='utf-8')
    except OSError:
        sys.stderr.write(
            "dblp_text.py: DTD '%s' not found; named entities "
            "(e.g. &auml;) will be left unexpanded\n" % path)
        return ent
    with f:
        for line in f:
            m = entity_re.search(line)
            if not m:
                continue
            name, code = m.group(1), m.group(2)
            cp = int(code[1:], 16) if code.startswith('x') else int(code)
            ent[name] = chr(cp)
    return ent


ENT_RE = re.compile(r'&(#x[0-9A-Fa-f]+|#\d+|[A-Za-z]+);')
def unescape(s, ent):
    if '&' not in s:                         # most fields carry no entity
        return s

    def repl(m):
        e = m.group(1)
        if e.startswith('#x'):
            return chr(int(e[2:], 16))
        if e.startswith('#'):
            return chr(int(e[1:]))
        return ent.get(e, m.group(0))
    return ENT_RE.sub(repl, s)


TAG_RE = re.compile(r'<[^>]+>')
def strip_tags(s):
    return TAG_RE.sub('', s) if '<' in s else s   # most fields carry no inner markup


# Match the title-normalisation used by the SQLite `title_norm` column:
# keep ASCII alphanumerics only, lowercased.
NON_ALNUM_RE = re.compile(r'[^A-Za-z0-9]')
def norm_title(s):
    return NON_ALNUM_RE.sub('', s).lower()


def sql_quote(s):
    # An absent field is None here; coerce to '' so it serialises as an empty SQL string,
    # not the literal "None" that str(None) would produce.
    s = '' if s is None else str(s)
    return "'" + s.replace("'", "''") + "'"


# Return true if the venue is selected: listed explicitly, or "*" (pass everything).
def wanted(vset, venue):
    return '*' in vset or venue in vset


# Start tag of an article/inproceedings with a key; captures (from-tag, kind, venue).
# Venue membership is then a set lookup, not a huge regex alternation.
START_RE = re.compile(r'(<(?:article|inproceedings)\s.*key="(journals|conf)/([^/"]+)/.*)')
# A line up to and including the entry's closing tag.  In DBLP the closing tag of
# one entry and the opening tag of the next often share a line, so closing and
# (re)opening are both checked per line.
CLOSE_RE = re.compile(r'(.*</(?:article|inproceedings)>)')
# Proceedings volume records; captured into the `proceedings` table.  Usually conf/,
# but some workshop volumes are hosted under journals/corr (arXiv/EPTCS), so allow both
# (matching the entry rule and the Go extractor).
PROC_START_RE = re.compile(r'(<proceedings\s.*key="(journals|conf)/([^/"]+)/.*)')
PROC_CLOSE_RE = re.compile(r'(.*</proceedings>)')
YEAR_RE = re.compile(r'<year>(\d+)</year>')
KEY_RE = re.compile(r'key="([^"]*)"')
TYPE_RE = re.compile(r'<(article|inproceedings)\b')
VENUE_RE = re.compile(r'\A(?:journals|conf)/([^/]+)/')
AUTHOR_RE = re.compile(r'<author\b[^>]*>(.*?)</author>', re.S)
EE_RE = re.compile(r'<ee\b[^>]*>(.*?)</ee>', re.S)
FIELD_TAGS = ['journal', 'booktitle', 'series', 'volume', 'number', 'pages', 'title', 'publisher', 'isbn', 'url', 'crossref']
FIELD_RE = {t: re.compile(r'<%s\b[^>]*>(.*?)</%s>' % (t, t), re.S) for t in FIELD_TAGS}

REF_FIELDS = ['journal', 'booktitle', 'series', 'volume', 'number', 'pages']

def text_of(rec, tag, ent):    # first <tag>..</tag>, inner tags stripped, entities unescaped
    m = FIELD_RE[tag].search(rec)
    if not m:
        return None
    return unescape(strip_tags(m.group(1)), ent)


def first_group(rx, s, default=None):  # first capture group of the first match, or `default`
    m = rx.search(s)
    return m.group(1) if m else default


# Preferences: which journals/conferences and which year range survive.
# Returns None when the file is absent or isn't a YAML mapping, so the caller can report it as missing.
def load_config(path):
    try:
        with open(path, encoding='utf-8') as f:
            config = yaml.safe_load(f)
    except OSError:
        return None
    if not isinstance(config, dict):
        return None
    journals = set(config.get('journals') or [])
    confs = set(config.get('conferences') or [])
    journal_names = dict(config.get('journal_names') or {})
    year = config.get('year') or {}
    return journals, confs, journal_names, year.get('lower', 1900), year.get('upper', 2100)


def reference_of(fields):
    return ', '.join(fields[t] for t in REF_FIELDS if fields[t] is not None)


def doi_of(ees):
    for e in ees:
        if 'doi.org' in e:
            return e
    return ees[0] if ees else ''


def main():
    p = argparse.ArgumentParser(add_help=True)
    p.add_argument('--color', action='store_true', help='ANSI coloring (text format)')
    p.add_argument('--format', default='text', help='output format: text (default) or sql')
    p.add_argument('--config', default='config.yaml', help='preference YAML (default: config.yaml)')
    p.add_argument('--dtd', default='dblp.dtd', help='DTD file for entity definitions (default: dblp.dtd)')
    args = p.parse_args()

    cfg = load_config(args.config)
    if cfg is None:
        sys.exit("dblp_text.py: config '%s' not found "
                 "(e.g., try `cp config/sample-se.yaml config/default.yaml`)" % args.config)
    journals, confs, journal_names, lower, upper = cfg
    # A "*" entry passes every venue of that kind (no filtering); see sample-all.yaml.
    if not journals and not confs:
        sys.exit("dblp_text.py: config '%s' selects no venues; list ids under "
                 "journals:/conferences:, or use '- \"*\"' to pass everything" % args.config)

    ent = load_dtd(args.dtd)
    sql_out = args.format == 'sql'

    articles = []
    procs = []          # proceedings volume records (sql format only)
    rec = None          # list of line fragments for the record currently accumulating
    rectype = None      # 'entry' (article/inproceedings) or 'proc' (proceedings)

    for line in sys.stdin:
        if rec is not None:
            close_re = PROC_CLOSE_RE if rectype == 'proc' else CLOSE_RE
            m = close_re.search(line)
            if m:
                record = ''.join(rec) + m.group(1)
                rt = rectype
                rec = None
                rectype = None
                year = first_group(YEAR_RE, record)
                if year is None or lower <= int(year) <= upper:
                    key = first_group(KEY_RE, record, '')
                    if rt == 'proc':
                        procs.append({
                            'key': key,
                            'title': text_of(record, 'title', ent) or '',
                            'booktitle': text_of(record, 'booktitle', ent) or '',
                            'year': int(year or '0'),
                            'publisher': text_of(record, 'publisher', ent) or '',
                            'isbn': text_of(record, 'isbn', ent) or '',
                            'ee': ' '.join(unescape(e, ent) for e in EE_RE.findall(record)),
                            'url': text_of(record, 'url', ent) or '',
                        })
                    else:
                        fields = {t: text_of(record, t, ent) for t in REF_FIELDS}
                        articles.append({
                            'key': key,
                            'type': first_group(TYPE_RE, record, ''),
                            'venue': first_group(VENUE_RE, key, ''),
                            'year': int(year or '0'),
                            'authors': [unescape(strip_tags(a), ent) for a in AUTHOR_RE.findall(record)],
                            'title': re.sub(r'\.$', '', text_of(record, 'title', ent) or ''),
                            'fields': fields,
                            'ee': [unescape(e, ent) for e in EE_RE.findall(record)],
                            'crossref': text_of(record, 'crossref', ent) or '',
                        })
            else:
                rec.append(line)
        # Cheap substring guard before the regex: only record start tags carry a key
        # attribute, so most lines (authors, titles, …) are skipped here.  Venue
        # selection is a set lookup on the captured venue token.  Proceedings are only
        # collected for the sql format (the text database is papers only).
        if rec is None and 'key="' in line:
            m = START_RE.search(line)
            if m and wanted(journals if m.group(2) == 'journals' else confs, m.group(3)):
                rec = [m.group(1)]
                rectype = 'entry'
            elif sql_out:
                m = PROC_START_RE.search(line)
                if m and wanted(journals if m.group(2) == 'journals' else confs, m.group(3)):
                    rec = [m.group(1)]
                    rectype = 'proc'

    out = sys.stdout
    if sql_out:
        out.write('BEGIN;\n')
        # Journal full names from config (abbreviation -> full title); see schema.sql `journals`.
        for abbrev in sorted(journal_names):
            out.write("INSERT INTO journals(abbrev, full_name) VALUES(%s,%s);\n"
                      % (sql_quote(abbrev), sql_quote(journal_names[abbrev])))
        for a in articles:
            f = a['fields']
            cols = [
                sql_quote(a['key']),                  # key
                sql_quote(a['type']),                 # type
                sql_quote(a['venue']),                # venue
                str(a['year']),                       # year
                sql_quote(', '.join(a['authors'])),   # authors
                sql_quote(a['title']),                # title
                sql_quote(norm_title(a['title'])),    # title_norm
                sql_quote(f['journal']),              # journal
                sql_quote(f['booktitle']),            # booktitle
                sql_quote(f['volume']),               # volume
                sql_quote(f['number']),               # number
                sql_quote(f['pages']),                # pages
                sql_quote(doi_of(a['ee'])),           # doi
                sql_quote(' '.join(a['ee'])),         # ee
                sql_quote(a['crossref']),             # crossref
            ]
            out.write("INSERT INTO entries VALUES(%s);\n" % ','.join(cols))
        for pr in procs:
            cols = [
                sql_quote(pr['key']),        # key
                sql_quote(pr['title']),      # title
                sql_quote(pr['booktitle']),  # booktitle
                str(pr['year']),             # year
                sql_quote(pr['publisher']),  # publisher
                sql_quote(pr['isbn']),       # isbn
                sql_quote(pr['ee']),         # ee
                sql_quote(pr['url']),        # url
            ]
            out.write("INSERT INTO proceedings(key,title,booktitle,year,publisher,isbn,ee,url) "
                      "VALUES(%s);\n" % ','.join(cols))
        out.write('COMMIT;\n')
    else:
        articles.sort(key=lambda a: (a['year'], reference_of(a['fields'])))
        for a in articles:
            key = '(%s)' % a['key']
            authors = ', '.join(a['authors'])
            title = a['title']
            ref = reference_of(a['fields'])
            year = a['year']
            doi = doi_of(a['ee'])
            if args.color:
                key = GREEN + key + RESET
                authors = authors + RESET           # authors are unstyled, just terminated
                title = BOLD + title + RESET
                doi = CYAN + UNDERLINE + doi + RESET
            out.write('%s %s: "%s", %s, %s. %s\n' % (key, authors, title, ref, year, doi))


if __name__ == '__main__':
    main()
