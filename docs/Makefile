# Regenerate the README demo GIFs (maintainer-only; needs vhs + ttyd + ffmpeg + sqlite3).
# build-demo.sh assembles data/demo.* from demo.keys + the sample-se profile (build it once with
# `make PROFILE=sample-se`), so no binary fixture is committed.  Run:  make -C docs
DEMO_SRC ?= sample-se

.PHONY: gif demo
gif: dblpsurvey.gif bibtex.gif dblplint.gif

# assemble data/demo.{txt.gz,db} -- the 14-paper subset -- from demo.keys + DEMO_SRC
demo:
	bash build-demo.sh $(DEMO_SRC)

# post-render shrink (~3x, no visible loss): the plain-text recording uses ~7 real colors, so an
# 8-color palette at 10 fps sheds vhs's anti-aliasing bulk.  $@ is the gif being built.
optimize = ffmpeg -y -loglevel error -i $@ -vf "fps=10,split[a][b];[a]palettegen=max_colors=8[p];[b][p]paletteuse=dither=none" .opt.gif && mv .opt.gif $@

dblpsurvey.gif: dblpsurvey.tape | demo
	vhs dblpsurvey.tape
	$(optimize)

bibtex.gif: bibtex.tape seed.bib | demo
	vhs bibtex.tape
	rm -f refs.bib
	$(optimize)

dblplint.gif: dblplint.tape sample.bib | demo
	vhs dblplint.tape
	rm -f refs.bib
	$(optimize)
