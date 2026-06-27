# README dblplint GIF: show a rough .bib entry, the proposals, then apply them all interactively.

Output dblplint.gif

Set Shell bash
Set FontSize 18
Set Width 950
Set Height 560
Set Padding 20
Set Framerate 24
Set TypingSpeed 90ms

# hidden setup: clean prompt, bin/ on PATH, demo profile, work on a copy of the sample .bib
Hide
Type "export PS1='$ '"
Enter
Type "export PATH=$PWD/../bin:$PATH"
Enter
Type "export DBLPSURVEY_PROFILE=demo"
Enter
Type "cp sample.bib refs.bib"
Enter
Type "clear"
Enter
Show

Type "cat refs.bib"
Enter
Sleep 2.5s

Type "dblplint refs.bib"
Enter
Sleep 3s

# apply interactively: Tab marks each of the 5 proposals, Enter accepts them
Type "dblplint refs.bib --apply"
Enter
Sleep 2s
Tab@350ms 5
Sleep 1s
Enter
Sleep 1.5s

Type "cat refs.bib"
Enter
Sleep 3.5s
