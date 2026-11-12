# Building agents with keelchat: a plain guide

[building-agents.pdf](building-agents.pdf) is a beginner's guide: how keelchat works (with diagrams), setting up, your
first bot, six recipes, testing, extending and going live.

- The source is [building-agents.tex](building-agents.tex). Its code listings are read from [snippets/](snippets), so
  the PDF shows the files that `tests/test_guide.py` checks: every YAML example builds, and the two broken ones fail
  with the error the guide quotes.
- Rebuild the PDF after changing either: `cd docs/guide && latexmk -g -xelatex building-agents.tex` (needs a TeX
  distribution with the Inter and Fira Mono fonts), then commit the PDF.
