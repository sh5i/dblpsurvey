# dblpsurvey
A quick & fast survey tool with a grep-friendly text file generated from [dblp](https://dblp.org/) database

![](https://i.gyazo.com/3c1e31b89302d81cd1fbdfdf18b3fb89.gif)

## Usage
```
$ dblpsurvey [-k] [-d] [keyword...]
```
Options:
- `-k`: Remove DBLP keys from the output
- `-d`: Remove DOI URLs from the output
- `keyword`: Used as initial keywords when specified

When running `dblpsurvey`, you can select your favorite lines if you have installed incremental search tools such as `peco`.
The results are pasted to the clipboard with `pbcopy`.

## Prerequisites
- Basic commands: `bash`, `curl`, `gzip`, `gunzip`, `realpath`, `perl`, and `make`
- for building the text database, either:
   - [Go](https://go.dev/) — the default, fast extractor (`make`), or
   - [`ruby`](https://www.ruby-lang.org/) — the readable reference extractor (`make EXTRACTOR=ruby`)
- for search: [`fzf`](https://github.com/junegunn/fzf), [`peco`](https://github.com/peco/peco), or `grep`
- (optional) for pasting to the clipboard: `pbcopy`, `xsel`, or `putclip`

## Installation
```
$ git clone https://github.com/sh5i/dblpsurvey.git
$ cd dblpsurvey
$ cp config.yaml.sample config.yaml
# (Edit config.yaml as you like)
$ make
$ sudo make install   # this just does: ln -s $(realpath ./dblpsurvey) /usr/local/bin/
```
The `make` first downloads the DBLP XML database file from https://dblp.org/, then filters it by the preference in `config.yaml` and converts the selected entries to a simple text in a single pass, each line representing a DBLP entry (`<article>` or `<inproceedings>`).
Such a text file is suitable for the grep-based search.

By default `make` builds and uses a fast Go extractor (`dblp_text.go`).
To avoid installing Go, use the equivalent Ruby extractor instead: `make EXTRACTOR=ruby` (`dblp_text.rb`).
`make test` checks that the two extractors produce identical output.

## Example of config.yaml
```
journals:
  # Enumerate your favorite journals in the DBLP world.
  # Only the <article>s of ID "journals/(journal ID)/*" survive.
  - tse
  - tosem

conferences:
  # Enumerate your favorite conferences in the DBLP world.
  # Only the <inproceedings>s of ID "conf/(conference ID)/*" survive.
  - icse
  - sigsoft
  - kbse

year:
  # Only the entries whose publishing year is in [lower, upper] survive.
  lower: 2005
  upper: 2100
```
