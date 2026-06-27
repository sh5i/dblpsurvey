# README hero GIF: search "smell refactoring", refine to "prefactoring", then pick.

Output dblpsurvey.gif

Set Shell bash
Set FontSize 18
Set Width 950
Set Height 480
Set Padding 20
Set Framerate 24
Set TypingSpeed 90ms

# hidden setup: clean prompt, bin/ on PATH, demo profile (so commands need no -p)
Hide
Type "export PS1='$ '"
Enter
Type "export PATH=$PWD/../bin:$PATH"
Enter
Type "export DBLPSURVEY_PROFILE=demo"
Enter
Type "clear"
Enter
Show

Type "dblpsurvey smell refactoring"
Enter
Sleep 2s

# before "refactoring"'s 'r' (11 chars back), insert 'p' -> "prefactoring"
Left 11
Sleep 1s
Type "p"
Sleep 2s
Enter
Sleep 3s
