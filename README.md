# dblpfilter
Generates a line-based single text file from dblp XML database

# How to use
```
$ cp config.yaml.sample config.yaml
# Edit config.yaml as you like
$ make
$ grep 'your favorite keyword' result.txt
```
It first downloads the DBLP XML database file from http://dblp.uni-trier.de/xml/
and generates a smaller XML based on the preference specified by `config.yaml`.
Then, the extracted XML will be converted to a simple text, each line represents
a DBLP entry (`<article>` or `<inproceedings>`).
Such text file is suitable for the grep-based search.

If you are using an environment that `pbcopy` and `peco` are available, you can enjoy your survey using `survey`.
```
$ ln -s path/to/dblpfilter/survey ~/bin/
$ survey queries
```
Then, the selected lines using `peco` are in your clipboard.


# Example of config.yaml
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
