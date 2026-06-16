// dblp_text.go — fast Go counterpart of dblp_text.rb (identical I/O: raw DBLP XML
// on stdin, flags --color/--format/--config/--dtd).  --format text (default) emits
// one line per entry; --format sql emits INSERTs for the SQLite database.
// A byte-scanning producer finds entry boundaries (no regexp) and a pool of worker
// goroutines does the per-entry extraction.  Kept in sync with dblp_text.rb; run
// `make test` to verify the two agree.
package main

import (
	"bufio"
	"flag"
	"os"
	"regexp"
	"runtime"
	"sort"
	"strconv"
	"strings"
	"sync"
)

const (
	cKey   = "\x1b[32m"
	cTitle = "\x1b[1m"
	cDoi   = "\x1b[36m\x1b[4m"
	cClear = "\x1b[0m"
)

var (
	yearRe   = regexp.MustCompile(`<year>(\d+)</year>`)
	keyRe    = regexp.MustCompile(`key="([^"]*)"`)
	authorRe = regexp.MustCompile(`(?s)<author\b[^>]*>(.*?)</author>`)
	eeRe     = regexp.MustCompile(`(?s)<ee\b[^>]*>(.*?)</ee>`)
	entRe    = regexp.MustCompile(`&(#x[0-9A-Fa-f]+|#\d+|[A-Za-z]+);`)
	tagRe    = regexp.MustCompile(`<[^>]+>`)
)

var refTags = []string{"journal", "booktitle", "series", "volume", "number", "pages"}
var allTags = []string{"journal", "booktitle", "series", "volume", "number", "pages",
	"title", "publisher", "isbn", "url", "crossref"}
var fieldRe = map[string]*regexp.Regexp{}

func init() {
	for _, t := range allTags {
		fieldRe[t] = regexp.MustCompile(`(?s)<` + t + `\b[^>]*>(.*?)</` + t + `>`)
	}
}

// read-only after main() sets them, so safe for concurrent worker use
var (
	entMap         map[string]string
	lowerY, upperY int
	colorOn        bool
	sqlOut         bool
)

func unescape(s string) string {
	if !strings.Contains(s, "&") {
		return s
	}
	return entRe.ReplaceAllStringFunc(s, func(m string) string {
		e := m[1 : len(m)-1]
		switch {
		case strings.HasPrefix(e, "#x"):
			cp, _ := strconv.ParseInt(e[2:], 16, 32)
			return string(rune(cp))
		case e[0] == '#':
			cp, _ := strconv.ParseInt(e[1:], 10, 32)
			return string(rune(cp))
		default:
			if v, ok := entMap[e]; ok {
				return v
			}
			return m
		}
	})
}

func stripTags(s string) string {
	if !strings.Contains(s, "<") {
		return s
	}
	return tagRe.ReplaceAllString(s, "")
}

func textOf(rec string, re *regexp.Regexp) (string, bool) {
	m := re.FindStringSubmatch(rec)
	if m == nil {
		return "", false
	}
	return unescape(stripTags(m[1])), true
}

func sqlQuote(s string) string { return "'" + strings.ReplaceAll(s, "'", "''") + "'" }

// normTitle mirrors dblp_text.rb's title_norm: keep ASCII alphanumerics only,
// lowercased.  ASCII-only on purpose so Ruby/Go/query side normalise identically.
func normTitle(s string) string {
	var b strings.Builder
	for i := 0; i < len(s); i++ {
		switch c := s[i]; {
		case c >= 'A' && c <= 'Z':
			b.WriteByte(c + 32)
		case (c >= 'a' && c <= 'z') || (c >= '0' && c <= '9'):
			b.WriteByte(c)
		}
	}
	return b.String()
}

func typeOf(record string) string {
	if strings.HasPrefix(record, "<inproceedings") {
		return "inproceedings"
	}
	if strings.HasPrefix(record, "<proceedings") {
		return "proceedings"
	}
	return "article"
}

func venueOf(key string) string {
	rest := ""
	if r, ok := strings.CutPrefix(key, "journals/"); ok {
		rest = r
	} else if r, ok := strings.CutPrefix(key, "conf/"); ok {
		rest = r
	} else {
		return ""
	}
	if i := strings.IndexByte(rest, '/'); i >= 0 {
		return rest[:i]
	}
	return rest
}

type entry struct {
	year int
	ref  string
	out  string
}

// closeCapture returns the line up to and including the rightmost record close tag
// (</article>, </inproceedings> or </proceedings>).  A record contains only its own
// close tag, so searching all three is safe; "</proceedings>" is not a substring of
// "</inproceedings>".
func closeCapture(line string) (string, bool) {
	end := -1
	for _, tag := range []string{"</article>", "</inproceedings>", "</proceedings>"} {
		if i := strings.LastIndex(line, tag); i >= 0 {
			if e := i + len(tag); e > end {
				end = e
			}
		}
	}
	if end < 0 {
		return "", false
	}
	return line[:end], true
}

// startMatch finds the leftmost <article / <inproceedings start tag whose key is
// in a wanted venue, and returns the slice from that tag to end of line (mirrors
// `(<(?:article|inproceedings)\s.*key="(journals|conf)/([^/"]+)/.*)` + set lookup).
func startMatch(line string, journals, confs map[string]bool) (string, bool) {
	start := -1
	if i := strings.Index(line, "<article "); i >= 0 {
		start = i
	}
	if i := strings.Index(line, "<inproceedings "); i >= 0 && (start < 0 || i < start) {
		start = i
	}
	if sqlOut { // proceedings volumes go to the sql output only
		if i := strings.Index(line, "<proceedings "); i >= 0 && (start < 0 || i < start) {
			start = i
		}
	}
	if start < 0 {
		return "", false
	}
	ki := strings.Index(line[start:], `key="`)
	if ki < 0 {
		return "", false
	}
	v := line[start+ki+len(`key="`):]
	ok := false
	if rest, found := strings.CutPrefix(v, "journals/"); found {
		if e := strings.IndexAny(rest, `/"`); e >= 0 && rest[e] == '/' {
			ok = journals[rest[:e]]
		}
	} else if rest, found := strings.CutPrefix(v, "conf/"); found {
		if e := strings.IndexAny(rest, `/"`); e >= 0 && rest[e] == '/' {
			ok = confs[rest[:e]]
		}
	}
	if !ok {
		return "", false
	}
	return line[start:], true
}

func extract(record string) (entry, bool) {
	var year int
	hasYear := false
	if m := yearRe.FindStringSubmatch(record); m != nil {
		hasYear = true
		year, _ = strconv.Atoi(m[1])
	}
	if hasYear && (year < lowerY || year > upperY) {
		return entry{}, false
	}
	key := ""
	if m := keyRe.FindStringSubmatch(record); m != nil {
		key = m[1]
	}
	typ := typeOf(record)
	field := func(t string) string { v, _ := textOf(record, fieldRe[t]); return v }
	var ees []string
	for _, m := range eeRe.FindAllStringSubmatch(record, -1) {
		ees = append(ees, unescape(m[1]))
	}

	if typ == "proceedings" {
		out := "INSERT INTO proceedings(key,title,booktitle,year,publisher,isbn,ee,url) VALUES(" +
			sqlQuote(key) + "," + // key
			sqlQuote(field("title")) + "," + // title
			sqlQuote(field("booktitle")) + "," + // booktitle
			strconv.Itoa(year) + "," + // year
			sqlQuote(field("publisher")) + "," + // publisher
			sqlQuote(field("isbn")) + "," + // isbn
			sqlQuote(strings.Join(ees, " ")) + "," + // ee
			sqlQuote(field("url")) + ");" // url
		return entry{year, "", out}, true
	}

	var authors []string
	for _, m := range authorRe.FindAllStringSubmatch(record, -1) {
		authors = append(authors, unescape(stripTags(m[1])))
	}
	title := ""
	if v, ok := textOf(record, fieldRe["title"]); ok {
		title = strings.TrimSuffix(v, ".")
	}
	doi := ""
	for _, e := range ees {
		if strings.Contains(e, "doi.org") {
			doi = e
			break
		}
	}
	if doi == "" && len(ees) > 0 {
		doi = ees[0]
	}
	auth := strings.Join(authors, ", ")

	if sqlOut {
		out := "INSERT INTO entries VALUES(" +
			sqlQuote(key) + "," + // key
			sqlQuote(typ) + "," + // type
			sqlQuote(venueOf(key)) + "," + // venue
			strconv.Itoa(year) + "," + // year
			sqlQuote(auth) + "," + // authors
			sqlQuote(title) + "," + // title
			sqlQuote(normTitle(title)) + "," + // title_norm
			sqlQuote(field("journal")) + "," + // journal
			sqlQuote(field("booktitle")) + "," + // booktitle
			sqlQuote(field("volume")) + "," + // volume
			sqlQuote(field("number")) + "," + // number
			sqlQuote(field("pages")) + "," + // pages
			sqlQuote(doi) + "," + // doi
			sqlQuote(strings.Join(ees, " ")) + "," + // ee
			sqlQuote(field("crossref")) + ");" // crossref
		return entry{year, "", out}, true
	}

	var refs []string
	for _, t := range refTags {
		if v, ok := textOf(record, fieldRe[t]); ok {
			refs = append(refs, v)
		}
	}
	ref := strings.Join(refs, ", ")
	yr := strconv.Itoa(year)
	var out string
	if colorOn {
		out = cKey + "(" + key + ")" + cClear + " " + auth + cClear + ": \"" +
			cTitle + title + cClear + "\", " + ref + ", " + yr + ". " + cDoi + doi + cClear
	} else {
		out = "(" + key + ") " + auth + ": \"" + title + "\", " + ref + ", " + yr + ". " + doi
	}
	return entry{year, ref, out}, true
}

func loadConfig(path string) (journals, confs map[string]bool, journalNames map[string]string, lower, upper int) {
	journals, confs = map[string]bool{}, map[string]bool{}
	journalNames = map[string]string{}
	lower, upper = 1900, 2100
	f, err := os.Open(path)
	if err != nil {
		return
	}
	defer f.Close()
	section := ""
	sc := bufio.NewScanner(f)
	for sc.Scan() {
		line := sc.Text()
		if i := strings.IndexByte(line, '#'); i >= 0 {
			line = line[:i]
		}
		if strings.TrimSpace(line) == "" {
			continue
		}
		if !strings.HasPrefix(line, " ") && strings.HasSuffix(strings.TrimRight(line, " \t"), ":") {
			section = strings.TrimSuffix(strings.TrimRight(line, " \t"), ":")
			continue
		}
		t := strings.TrimSpace(line)
		switch {
		case strings.HasPrefix(t, "- "):
			v := strings.TrimSpace(t[2:])
			if section == "journals" {
				journals[v] = true
			} else if section == "conferences" {
				confs[v] = true
			}
		case section == "journal_names" && strings.HasPrefix(t, `"`):
			// a quoted YAML mapping line:  "abbrev": "full title"
			if k, v, ok := parseQuotedPair(t); ok {
				journalNames[k] = v
			}
		case strings.HasPrefix(t, "lower:"):
			lower, _ = strconv.Atoi(strings.TrimSpace(t[len("lower:"):]))
		case strings.HasPrefix(t, "upper:"):
			upper, _ = strconv.Atoi(strings.TrimSpace(t[len("upper:"):]))
		}
	}
	return
}

// parseQuotedPair reads a `"key": "value"` line (both double-quoted, no escaped quotes —
// DBLP journal names contain none) into its key and value.
func parseQuotedPair(t string) (key, val string, ok bool) {
	rest := t[1:] // drop opening quote
	ke := strings.IndexByte(rest, '"')
	if ke < 0 {
		return
	}
	key = rest[:ke]
	rest = rest[ke+1:]
	vs := strings.IndexByte(rest, '"') // opening quote of value
	if vs < 0 {
		return
	}
	rest = rest[vs+1:]
	ve := strings.IndexByte(rest, '"')
	if ve < 0 {
		return
	}
	return key, rest[:ve], true
}

func loadDTD(path string) map[string]string {
	ent := map[string]string{"amp": "&", "lt": "<", "gt": ">", "quot": "\"", "apos": "'"}
	f, err := os.Open(path)
	if err != nil {
		os.Stderr.WriteString("dblp(go): DTD '" + path + "' not found; named entities left unexpanded\n")
		return ent
	}
	defer f.Close()
	re := regexp.MustCompile(`<!ENTITY\s+(\w+)\s+"&#(x?[0-9A-Fa-f]+);"`)
	sc := bufio.NewScanner(f)
	for sc.Scan() {
		m := re.FindStringSubmatch(sc.Text())
		if m == nil {
			continue
		}
		code := m[2]
		var cp int64
		if code[0] == 'x' {
			cp, _ = strconv.ParseInt(code[1:], 16, 32)
		} else {
			cp, _ = strconv.ParseInt(code, 10, 32)
		}
		ent[m[1]] = string(rune(cp))
	}
	return ent
}

func main() {
	color := flag.Bool("color", false, "ANSI coloring (text format)")
	format := flag.String("format", "text", "output format: text or sql")
	config := flag.String("config", "config.yaml", "preference YAML")
	dtd := flag.String("dtd", "dblp.dtd", "DTD for entity definitions")
	flag.Parse()

	journals, confs, journalNames, lower, upper := loadConfig(*config)
	entMap = loadDTD(*dtd)
	lowerY, upperY = lower, upper
	colorOn = *color
	sqlOut = *format == "sql"

	const batchSize = 256
	records := make(chan []string, 2*runtime.NumCPU())
	nw := runtime.NumCPU()
	results := make([][]entry, nw)
	var wg sync.WaitGroup
	for w := 0; w < nw; w++ {
		wg.Add(1)
		go func(id int) {
			defer wg.Done()
			var local []entry
			for batch := range records {
				for _, rec := range batch {
					if e, ok := extract(rec); ok {
						local = append(local, e)
					}
				}
			}
			results[id] = local
		}(w)
	}

	// producer: scan boundaries, emit raw record strings in batches
	sc := bufio.NewScanner(os.Stdin)
	sc.Buffer(make([]byte, 1<<20), 64<<20)
	var recLines []string
	active := false
	batch := make([]string, 0, batchSize)
	for sc.Scan() {
		line := sc.Text()
		if active {
			if capt, ok := closeCapture(line); ok {
				recLines = append(recLines, capt)
				batch = append(batch, strings.Join(recLines, ""))
				recLines = recLines[:0]
				active = false
				if len(batch) >= batchSize {
					records <- batch
					batch = make([]string, 0, batchSize)
				}
			} else {
				recLines = append(recLines, line, "\n")
			}
		}
		if !active && strings.Contains(line, `key="`) {
			if capt, ok := startMatch(line, journals, confs); ok {
				recLines = append(recLines, capt)
				active = true
			}
		}
	}
	if len(batch) > 0 {
		records <- batch
	}
	close(records)
	wg.Wait()

	var entries []entry
	for _, r := range results {
		entries = append(entries, r...)
	}
	w := bufio.NewWriterSize(os.Stdout, 1<<20)
	defer w.Flush()
	if sqlOut {
		w.WriteString("BEGIN;\n")
		// Journal full names from config (abbreviation -> full title); see schema.sql `journals`.
		names := make([]string, 0, len(journalNames))
		for k := range journalNames {
			names = append(names, k)
		}
		sort.Strings(names)
		for _, k := range names {
			w.WriteString("INSERT INTO journals(abbrev, full_name) VALUES(" +
				sqlQuote(k) + "," + sqlQuote(journalNames[k]) + ");\n")
		}
		for _, e := range entries {
			w.WriteString(e.out)
			w.WriteByte('\n')
		}
		w.WriteString("COMMIT;\n")
		return
	}
	sort.SliceStable(entries, func(i, j int) bool {
		if entries[i].year != entries[j].year {
			return entries[i].year < entries[j].year
		}
		return entries[i].ref < entries[j].ref
	})
	for _, e := range entries {
		w.WriteString(e.out)
		w.WriteByte('\n')
	}
}
