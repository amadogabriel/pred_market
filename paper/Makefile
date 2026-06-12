## Build the JFE-style preregistered paper.
##
## Requires a TeX distribution with elsarticle.cls (TeX Live / MiKTeX include
## it by default). Uses latexmk for dependency tracking.

.PHONY: all paper clean distclean view

all: paper

paper: main.pdf

main.pdf: main.tex references.bib
	latexmk -pdf -interaction=nonstopmode -halt-on-error main.tex

# Fallback build for systems without latexmk.
manual: main.tex references.bib
	pdflatex -interaction=nonstopmode main.tex
	bibtex main
	pdflatex -interaction=nonstopmode main.tex
	pdflatex -interaction=nonstopmode main.tex

view: main.pdf
ifeq ($(OS),Windows_NT)
	start main.pdf
else
	xdg-open main.pdf 2>/dev/null || open main.pdf
endif

clean:
	-latexmk -c main.tex
	-rm -f main.aux main.bbl main.blg main.log main.out main.synctex.gz \
		main.fls main.fdb_latexmk main.spl main.toc

distclean: clean
	-rm -f main.pdf
