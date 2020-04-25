# dblpsurvey
A quick & fast survey tool with a grep-friendly text file generated from [dblp](https://dblp.org/) database

## Usage
```
$ dblpsurvey [opts] [keyword...]
```
Options:
- `-k`: Remove DBLP keys from the output
- `-d`: Remove DOI URLs from the output
- `keyword`: Used as an initial keywords when specified

When running `dblpsurvey`, you can select your favorite lines if you have installed incremental search tools such as `peco`.
The results are pasted to the clipboard with `pbcopy`.

## Prerequisites
- Basic commands: `make`, `curl`, `gzip`, `gunzip`, `realpath`
- For the main scripts: `ruby`, `perl`
- For expanding entities: `xmllint`
- For search: `peco`, `fzf`, or `grep`
- For pasting clipboard: `pbcopy`, `xsel`, or `putclip` (optional)

## Installation
```
$ git clone https://github.com/sh5i/dblpsurvey.git
$ cd dblpsurvey
$ cp config.yaml.sample config.yaml
# (Edit config.yaml as you like)
$ make
$ sudo make install   # this just does: ln -s $(realpath ./dblpsurvey) /usr/local/bin/
```
The `make` first downloads the DBLP XML database file from https://dblp.org/ and generates a smaller XML based on the preference specified by `config.yaml`.
Then, the extracted XML will be converted to a simple text, each line represents a DBLP entry (`<article>` or `<inproceedings>`).
Such a text file is suitable for the grep-based search.

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
