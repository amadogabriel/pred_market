# paper/ — JFE-style preregistered working paper

This directory holds the LaTeX source for the formal write-up of the
pm-system research project. The class is `elsarticle` configured for
*Journal of Financial Economics* submission. The same source compiles
acceptably to any author-year finance-journal style with a one-line
change to `\bibliographystyle`.

## Files

| File | Contents |
|---|---|
| `main.tex` | The paper. Single-file source; sections are physically inline rather than `\input`-included for ease of distribution. |
| `references.bib` | BibTeX bibliography. Author-year entries for everything cited in `main.tex`. |
| `Makefile` | `make paper` builds `main.pdf` via `latexmk`; `make clean` removes intermediates. |

## Build

```sh
make paper        # latexmk -pdf main.tex
make view         # open the resulting PDF
make clean        # remove .aux, .bbl, etc.
make distclean    # also remove main.pdf
```

If `latexmk` is unavailable, use the fallback:

```sh
make manual
```

which runs the classical `pdflatex → bibtex → pdflatex → pdflatex` sequence
once. The TeX distribution must include the `elsarticle` class (it ships
with both TeX Live and MiKTeX by default).

## Repository placement and source of numbers

Every numerical claim in `main.tex` traces back to a runnable artifact in
the repository:

- Tables 1–2 are descriptive / preregistered and are written in by hand.
- The headline results table (Table 3) is reproduced by running
  `python ../scripts/research_report.py` against the project's state
  database at the analysis-time git SHA. The values in the .tex are
  hand-transcribed from that script's output; future revisions of the
  paper should run the script and re-paste rather than hand-edit numbers.

## Switching journal style

To target a different finance journal, change the class options on
the first line:

```latex
\documentclass[review,12pt,authoryear]{elsarticle}
```

- *Journal of Financial Economics* / *Review of Financial Studies*:
  `elsarticle` as above (current default).
- *Journal of Finance*: drop in the AFA submission template
  (`aer.cls`-derived); section numbering and abstract format match.
- Working-paper format (e.g., for SSRN): change `[review]` to `[preprint,
  authoryear]` and remove `\linenumbers`.

The bibliography style is set by `\bibliographystyle{elsarticle-harv}` near
the end of `main.tex`; standard finance alternatives include `aer.bst`
and `jf.bst`.

## Preregistration discipline

This is a preregistered working paper. The hypotheses and analysis plan in
`main.tex` Sections 4 and 5 mirror the file `../research/HYPOTHESES.md` and
`../research/STATISTICS.md` exactly, both committed to the public repository
before the final sample was assembled. Any post-registration deviation is
documented in `../research/CRITIQUE.md` § Deviations and acknowledged in
the paper.

## Reproducibility manifest

The paper's `\section{Preregistration manifest}` documents the artifacts
required for a third-party replication:

- The git SHA of the analysis-time commit
- The event-log day files for the sample window
- The markets snapshot at the analysis SHA
- The `config/fees.yaml` version covering the sample window
- The `scripts/research_report.py` invocation that produced Table 3
