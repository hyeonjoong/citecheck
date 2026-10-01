# Changelog

## 0.2.0 - 2026-10-01

This release brings the standalone repository up to the hardened version that
had been developed separately, and fixes the defects found in a review of both.

### Added

- Input formats: RIS (EndNote, Zotero, Mendeley), CSL-JSON (Zotero, Better
  BibTeX, pandoc), CSV/TSV reference tables with columns matched by name
  (English and Korean column names), Excel `.xlsx` and Word `.docx`.
- `--pubmed`: cross-check PMIDs against PubMed for retractions Crossref misses
  and for PMID/DOI mismatches.
- `--suggest-doi`: for a reference with no DOI, search Crossref and name the DOI
  of a confident match (never a retracted one).
- `--cache` and `--cache-ttl`: keep lookups on disk; entries expire (default 7
  days) so a new retraction is not hidden.
- `--ignore` and `--list-checks`: every finding has a stable code, and codes can
  be silenced in the report. `lookup-failed` cannot be ignored.
- `--strict`: warnings fail the run too.
- `--profile`, `--as-of` and `--self-cite`: descriptive statistics for the
  reference list (DOI coverage, year median and IQR, Price index, journals).
- `--report csv|markdown` next to text and JSON, and `--encoding` to force the
  input encoding.
- Findings for expressions of concern, withdrawals, removals, corrections and
  other Crossmark updates, preprints that have a published version, and
  duplicate DOIs or PMIDs.
- `CHANGELOG.md`, `examples/` files for BibTeX, RIS, CSL-JSON and CSV, and a
  CI job that builds the wheel and runs it from a clean environment.

### Fixed

- Retracted papers were reported as Verified, and retraction notices were
  flagged as retracted. Retractions are now read from Crossref `updated-by`,
  the `is-retracted-by` relation and the publisher's "RETRACTED:" title prefix.
- A run with no network, a rate limit or a Crossref outage exited 0. It now
  exits 3 (inconclusive).
- A DOI registered outside Crossref (DataCite, Zenodo, figshare) was a hard
  error. It is now a warning. Registration is checked with the doi.org handle
  API, which never follows the redirect to the publisher, so a publisher site
  with a broken certificate or a 404 cannot turn a registered DOI into an error
  or an inconclusive run.
- BibTeX: entries after `@string`, `@comment` or `@preamble` were merged into
  junk references, `@article(...)` entries were dropped, and one entry with an
  unbalanced brace dropped every entry after it without a word. Each entry is
  now read on its own, a broken entry is skipped and named on stderr, and the
  entries after it are still checked. `@string` macros are expanded; an
  expansion longer than 10,000 characters is treated as undefined, so a file
  of macros that double each other cannot exhaust memory.
- Text input cut Elsevier and Lancet DOIs at ")", kept URL query strings on
  DOIs, split one reference into several on a blank line holding spaces, and
  routed a list with an e-mail address and a "{" to the BibTeX parser.
- A CP949 file (Korean Excel export) crashed with a traceback and exit 1; a
  UTF-8 file with a BOM produced a phantom header reference. Both are decoded,
  and CR-only line endings (old Excel for Mac CSV) are read as rows.
- Korean column headers were ignored, so a Korean table was checked on its DOI
  alone and a swapped DOI passed. Korean headers are recognised, bilingual ones
  such as "Title(제목)" or "DOI 번호" too, and a table that maps only DOI/PMID
  lists its unused columns.
- Korean citations of English-only Crossref records (or the reverse) raised
  false title and author mismatches. Fields that cannot be compared across
  scripts are now skipped and named in the Verified line. Hangul words count
  when a free-text citation is matched against a Korean title, and Hangul names
  are compared syllable by syllable, including KCI records that split a name
  into family and given.
- False mismatches on correct citations: online-first years, diacritics,
  subtitles, long titles, LaTeX accents in `.bib` names, brace-protected
  corporate authors, "et al." and Korean 외/등 markers in author cells,
  semicolon-separated author lists ("Smith J; Lee K"), `@string` journal
  macros, and JATS/HTML markup (`<scp>`, `<sub>`, `&amp;`) and Unicode hyphens
  in Crossref titles.
- Terminal escape sequences in a cite key or title are no longer passed to the
  terminal.
- `citecheck` with no file on an interactive terminal waited silently for
  input; it now prints a usage hint and exits 2. `--delay -1` crashed; it is a
  usage error.
- Documentation: removed a local path and an invented `--suggest-doi` example,
  fixed the sample CSV's CONSORT 2010 PMID (20332509 is the BMJ copy the row
  names), and the install notes now cover the stock macOS python3.

### Behaviour changes

- New exit code 3 when a lookup failed or a BibTeX entry could not be parsed.
  Both used to exit 0. The full table: 0 clean, 1 an error (or a warning under
  `--strict`), 2 usage problem, 3 inconclusive.
- A DOI that is registered at doi.org but missing from Crossref is a warning
  (`doi-not-in-crossref`), not an error.
- Reports list errors first, then warnings, then verified references. This
  applies to the JSON report too, which used to follow input order.
- JSON items gain `code` (per finding), `pmid` and `journal` keys. Existing keys
  are unchanged.
- Text output indents finding lines by 4 spaces instead of 8.
- Text output now ends with a coverage line after the "checked N references"
  summary, for example "(3 of 5 compared against a Crossref record; 2 could not
  be ...)". A script that read the last line of 0.1.0 output should read the
  line before it.
- In plain-text input (`--format text`, and `.docx`), the author and year
  guessed from the line are no longer compared, because guesses from free text
  raised false alarms. The DOI and retraction checks still run, and a citation
  that does not mention its Crossref title is flagged (`text-title-missing`).
  Use a structured format for the full comparison.
- `Reference` gained `journal` and `pmid` fields, inserted before `key`.
  Keyword construction is unaffected; positional construction past `year`
  shifts.
- `CrossrefClient` gained a `cache` parameter before `_fetch` in the positional
  order. A client given only `_fetch` makes no network call at all: a DOI its
  transport does not know reads as not registered, as in 0.1.0.
- The User-Agent carries the package version (`citecheck/0.2.0`).
- License metadata uses the SPDX form (`license = "MIT"`), which needs
  setuptools 77 or newer to build.

## 0.1.0 - 2026-06-25

- First release: check BibTeX or plain-text reference lists against Crossref
  for DOIs that do not resolve, title, year and first-author mismatches, and
  retractions. Text or JSON output; exit 1 on an error.
