# citecheck

**Verify your manuscript's citations against Crossref before a reviewer does.**

`citecheck` reads a `.bib` file or a plain reference list, looks every DOI up on
[Crossref](https://www.crossref.org/), and flags the three mistakes that quietly
slip into reference lists:

- 🔗 **Broken DOIs** — a DOI that doesn't resolve at all.
- 🔀 **Metadata mismatches** — the title, year, or first author you cited doesn't
  match the record Crossref holds (usually a copy-paste from the wrong entry).
- ⛔ **Retractions** — the paper you're citing has been retracted.

It is a single, dependency-free Python file set you can drop into CI to fail a
build when a citation is wrong.

> Built as a weekly automation tool. Pure standard library — no `pip install`
> of third-party packages required to run.

## Install

```bash
pip install git+https://github.com/hyeonjoong/citecheck.git
# or, from a clone:
pip install -e .
```

Or just run it without installing:

```bash
python -m citecheck references.bib
```

## Usage

```bash
# Check a BibTeX file
citecheck references.bib

# Check a plain-text reference list (one per line or blank-line separated)
citecheck refs.txt --format text

# Pipe from stdin
pbpaste | citecheck -

# Machine-readable output for CI
citecheck references.bib --json

# Join Crossref's faster "polite" pool with your email
citecheck references.bib --mailto you@example.com
```

### Example

```
$ citecheck examples/sample.bib

✗ smith2020
    DOI does not resolve on Crossref: 10.9999/does.not.exist
! jones2019
    Title mismatch (41% similar):
    cited:    A study of widgets
    crossref: An analysis of gadgets in industrial settings
✗ doe2018
    Reference appears to be RETRACTED according to Crossref.

checked 4 references: 1 ok, 1 warnings, 2 errors
```

`citecheck` exits with status **1** if any reference has an error (broken DOI or
retraction), and **0** otherwise — so it works as a CI gate:

```yaml
# .github/workflows/citations.yml
- run: pip install git+https://github.com/hyeonjoong/citecheck.git
- run: citecheck paper/references.bib
```

## What gets checked

| Check | Severity | Meaning |
|-------|----------|---------|
| DOI resolves on Crossref | error | The DOI is wrong or the record is gone |
| Retraction flag | error | Crossref marks the work as retracted |
| Title similarity < 80% | warning | Likely wrong DOI for this citation |
| Year mismatch | warning | Cited year ≠ Crossref publication year |
| First-author mismatch | warning | Cited surname ≠ Crossref first author |
| No DOI present | warning | Nothing to verify against |

Thresholds are conservative to avoid false alarms on formatting differences.

## Why

When you finalize a paper you often verify references by hand against Crossref,
PubMed, or DataCite. `citecheck` automates the Crossref half of that pass so a
swapped DOI or a retracted source can't make it into submission.

## Development

```bash
pip install -e ".[dev]"
pytest
```

Tests run fully offline — the Crossref client accepts an injected transport, so
no network calls happen in CI for the unit tests.

## License

MIT © hyeonjoong
