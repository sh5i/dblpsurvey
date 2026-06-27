# README -i GIF: pick a paper and append it to refs.bib as BibTeX in the file's own
# conventions -- the seed uses "..." delimiters, so the inserted entry does too.

Output bibtex.gif

Set Shell bash
Set FontSize 18
Set Width 950
Set Height 540
Set Padding 20
Set Framerate 24
Set TypingSpeed 90ms

# hidden setup: clean prompt, bin/ on PATH, demo profile, work on a copy of the seed .bib
Hide
Type "export PS1='$ '"
Enter
Type "export PATH=$PWD/../bin:$PATH"
Enter
Type "export DBLPSURVEY_PROFILE=demo"
Enter
Type "cp seed.bib refs.bib"
Enter
Type "clear"
Enter
Show

Type "dblpsurvey -i refs.bib smell refactoring"
Enter
Sleep 2s

# before "refactoring"'s 'r' (11 chars back), insert 'p' -> "prefactoring", then pick
Left 11
Sleep 1s
Type "p"
Sleep 2s
Enter
Sleep 1.5s

Type "cat refs.bib"
Enter
Sleep 3.5s
