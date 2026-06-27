# The build lives in GNUmakefile and needs GNU make.  GNU make reads GNUmakefile first and never
# sees this file; BSD make reads this one and stops with the message below.
.error This project needs GNU make.  Run 'gmake' (on the *BSDs: pkg install gmake; on macOS, 'make' is already GNU make).
