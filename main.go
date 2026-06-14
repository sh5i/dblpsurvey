// Single-pass DBLP filter+extractor — Go port of dblp_text.rb (Phase 1: single
// threaded, stdlib only).  Reads raw DBLP XML on stdin, writes one line per entry.
package main

import (
	"bufio"
	"flag"
	"os"
	"regexp"
	"sort"
	"strconv"
	"strings"
)

const (
	cKey   = "\x1b[32m"
	cTitle = "\x1b[1m"
	cDoi   = "\x1b[36m\x1b[4m"
	cClear = "\x1b[0m"
)

var (
	startRe  = regexp.MustCompile(`(<(?:article|inproceedings)\s.*key="(journals|conf)/([^/"]+)/.*)`)
	closeRe  = regexp.MustCompile(`(.*</(?:article|inproceedings)>)`)
	yearRe   = regexp.MustCompile(`<year>(\d+)</year>`)
	keyRe    = regexp.MustCompile(`key="([^"]*)"`)
	authorRe = regexp.MustCompile(`(?s)<author\b[^>]*>(.*?)</author>`)
	eeRe     = regexp.MustCompile(`(?s)<ee\b[^>]*>(.*?)</ee>`)
	entRe    = regexp.MustCompile(`&(#x[0-9A-Fa-f]+|#\d+|[A-Za-z]+);`)
	tagRe    = regexp.MustCompile(`<[^>]+>`)
)

var refTags = []string{"journal", "booktitle", "series", "volume", "number", "pages"}
var fieldRe = map[string]*regexp.Regexp{}

func init() {
	for _, t := range append(append([]string{}, refTags...), "title") {
		fieldRe[t] = regexp.MustCompile(`(?s)<` + t + `\b[^>]*>(.*?)</` + t + `>`)
	}
}

func loadConfig(path string) (journals, confs map[string]bool, lower, upper int) {
	journals, confs = map[string]bool{}, map[string]bool{}
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
		case strings.HasPrefix(t, "lower:"):
			lower, _ = strconv.Atoi(strings.TrimSpace(t[len("lower:"):]))
		case strings.HasPrefix(t, "upper:"):
			upper, _ = strconv.Atoi(strings.TrimSpace(t[len("upper:"):]))
		}
	}
	return
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
	color := flag.Bool("color", false, "ANSI coloring")
	config := flag.String("config", "config.yaml", "preference YAML")
	dtd := flag.String("dtd", "dblp.dtd", "DTD for entity definitions")
	flag.Parse()

	journals, confs, lower, upper := loadConfig(*config)
	ent := loadDTD(*dtd)

	unescape := func(s string) string {
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
				if v, ok := ent[e]; ok {
					return v
				}
				return m
			}
		})
	}
	stripTags := func(s string) string {
		if !strings.Contains(s, "<") {
			return s
		}
		return tagRe.ReplaceAllString(s, "")
	}
	textOf := func(rec string, re *regexp.Regexp) (string, bool) {
		m := re.FindStringSubmatch(rec)
		if m == nil {
			return "", false
		}
		return unescape(stripTags(m[1])), true
	}

	type entry struct {
		year int
		ref  string
		line string
	}
	var entries []entry

	emit := func(record string) {
		var year int
		hasYear := false
		if m := yearRe.FindStringSubmatch(record); m != nil {
			hasYear = true
			year, _ = strconv.Atoi(m[1])
		}
		if hasYear && (year < lower || year > upper) {
			return
		}
		key := ""
		if m := keyRe.FindStringSubmatch(record); m != nil {
			key = m[1]
		}
		var authors []string
		for _, m := range authorRe.FindAllStringSubmatch(record, -1) {
			authors = append(authors, unescape(stripTags(m[1])))
		}
		title := ""
		if v, ok := textOf(record, fieldRe["title"]); ok {
			title = strings.TrimSuffix(v, ".")
		}
		var refs []string
		for _, t := range refTags {
			if v, ok := textOf(record, fieldRe[t]); ok {
				refs = append(refs, v)
			}
		}
		ref := strings.Join(refs, ", ")
		var ees []string
		for _, m := range eeRe.FindAllStringSubmatch(record, -1) {
			ees = append(ees, unescape(m[1]))
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
		yr := strconv.Itoa(year)
		var line string
		if *color {
			line = cKey + "(" + key + ")" + cClear + " " + auth + cClear + ": \"" +
				cTitle + title + cClear + "\", " + ref + ", " + yr + ". " + cDoi + doi + cClear
		} else {
			line = "(" + key + ") " + auth + ": \"" + title + "\", " + ref + ", " + yr + ". " + doi
		}
		entries = append(entries, entry{year, ref, line})
	}

	sc := bufio.NewScanner(os.Stdin)
	sc.Buffer(make([]byte, 1<<20), 64<<20)
	var buf strings.Builder
	active := false
	for sc.Scan() {
		line := sc.Text()
		if active {
			if m := closeRe.FindStringSubmatch(line); m != nil {
				buf.WriteString(m[1])
				emit(buf.String())
				buf.Reset()
				active = false
			} else {
				buf.WriteString(line)
				buf.WriteByte('\n')
			}
		}
		if !active && strings.Contains(line, `key="`) {
			if m := startRe.FindStringSubmatch(line); m != nil {
				ok := (m[2] == "journals" && journals[m[3]]) || (m[2] == "conf" && confs[m[3]])
				if ok {
					buf.WriteString(m[1])
					active = true
				}
			}
		}
	}

	sort.SliceStable(entries, func(i, j int) bool {
		if entries[i].year != entries[j].year {
			return entries[i].year < entries[j].year
		}
		return entries[i].ref < entries[j].ref
	})
	w := bufio.NewWriterSize(os.Stdout, 1<<20)
	defer w.Flush()
	for _, e := range entries {
		w.WriteString(e.line)
		w.WriteByte('\n')
	}
}
