# citecheck

Verify your manuscript's citations against Crossref before a reviewer does.

`citecheck` reads a BibTeX (`.bib`), RIS (EndNote/Zotero/Mendeley),
CSL-JSON (Zotero/Better BibTeX/pandoc), CSV/TSV reference table
(Excel/Sheets/Covidence), Excel workbook (`.xlsx`), Word document
(`.docx`), or plain reference list, looks every DOI up on
[Crossref](https://www.crossref.org/), and flags the mistakes that quietly slip
into reference lists:

- Broken DOIs - a DOI that doesn't resolve at all. (A DOI that resolves
  but simply isn't in Crossref - e.g. a dataset/preprint/DataCite DOI - is
  reported as a *warning*, not a hard error.)
- Metadata mismatches - the title, year, first author, or journal you
  cited doesn't match the record Crossref holds (usually a copy-paste from the
  wrong entry). The journal check understands ISO-4 abbreviations *and* all-caps
  initialisms, so "N Engl J Med", "NEJM" and "The New England Journal of
  Medicine" are all treated as equal - likewise JAMA, BMJ, PNAS, JCO.
- Retractions - the paper you're citing has been marked retracted *in
  Crossref*, and (with `--pubmed`) in PubMed too, which catches many
  retractions Crossref misses. See the caveat under [Retraction detection](#retraction-detection).
- Expressions of concern, withdrawals, corrections - Crossref's Crossmark
  data also records that a paper is under an expression of concern, has been
  withdrawn, or has had a correction/erratum issued. Citing a trial under
  an expression of concern without knowing is exactly the slip this catches.
- Preprints with a published version - you cited the medRxiv/bioRxiv
  preprint, but the peer-reviewed paper is out; citecheck names its DOI. (The
  numbers frequently change in review, so this matters.)
- PMIDvsDOI mismatch (with `--pubmed`) - the PMID and DOI you cited point
  to *different* papers (a classic copy-paste-from-two-records slip).
- Duplicate DOIs / PMIDs - the same paper cited under two different keys.
- A profile of the reference list itself (with `--profile`) - DOI
  coverage, the publication-year median/IQR, median reference age, the Price
  index (share published within the last 5 years), journal spread, Crossref
  document types, integrity-flag counts, and optional self-citation share.
  These are the numbers a desk editor or reviewer asks for ("your references are
  out of date", "how many of these are preprints?"), and they cost no extra
  lookup - they are computed from the records already fetched.
- Missing DOIs, found (with `--suggest-doi`) - for a reference that cites
  no DOI, citecheck searches Crossref by title/author/year and reports the DOI of
  a confident match, so you can add it instead of hunting for it by hand. And if
  that match turns out to be retracted, it says so and refuses to recommend
  it - the only way to catch a retracted source you never gave a DOI for.

Output as coloured text, JSON, CSV, or a Markdown table you can paste
into a PR or lab notebook.

It is a small, dependency-free Python package you can drop into CI to fail a
build when a citation is wrong.

> Built as a weekly automation tool. Pure standard library - no `pip install`
> of third-party packages required to run.
>
> Requires internet access to reach Crossref (`api.crossref.org`) and the
> DOI resolver (`doi.org`). Offline, every lookup is reported as a warning and
> the run exits 3 (inconclusive) - never 0 - so a network outage can't be
> mistaken for a clean pass in CI.

## 목적 / Why this exists

한글: 논문을 제출하기 직전, 참고문헌의 DOI를 손으로 하나씩 Crossref, PubMed에 대조하는 일은 지루하고 실수가 잦습니다. 잘못 붙여넣은 DOI, 다른 논문의 메타데이터, 심지어 이미 철회된 논문이 그대로 제출되면 리뷰어 지적이나 게재 후 정정 사유가 됩니다. `citecheck`는 그 Crossref 대조 과정을 자동화해, 임상, 약리 연구자가 원고를 제출하기 전에 인용 오류를 빠르게 잡도록 돕습니다.

English: Right before submission, manually checking every reference DOI against Crossref/PubMed is tedious and error-prone. A mistyped DOI, metadata from the wrong paper, or a retracted source can slip into a manuscript and trigger reviewer complaints or post-publication corrections. `citecheck` automates the Crossref half of that verification pass so a researcher can catch citation errors before submitting - and can even wire it into CI as a submission gate.

## Install

```bash
python3 -m pip install git+https://github.com/hyeonjoong/citecheck.git
# or, from a clone:
git clone https://github.com/hyeonjoong/citecheck.git
cd citecheck
python3 -m pip install .
```

The stock macOS `python3` (3.9 with pip 21) cannot do an editable install
(`pip install -e .`) of this package; it stops with "editable mode currently
requires a setuptools-based build". Either run `python3 -m pip install --upgrade pip`
first, or use the non-editable `python3 -m pip install .` shown above.

Or just run it without installing (this always works from a clone; the bare
`citecheck` command only works if your Python scripts dir is on `PATH`):

```bash
python3 -m citecheck references.bib
```

## Usage

```bash
# Check a BibTeX file
python3 -m citecheck references.bib

# Check an EndNote/Zotero RIS export, or a Zotero/pandoc CSL-JSON export
python3 -m citecheck references.ris
python3 -m citecheck references.json

# Check a reference table kept in Excel/Sheets/Covidence (CSV or TSV)
python3 -m citecheck included_studies.csv
# (input format is auto-detected; force it with
#  --format bibtex|ris|csljson|csv|text. .xlsx/.docx are detected from the
#  file's own bytes and converted before that.)

# Check a plain-text reference list (one per line or blank-line separated)
python3 -m citecheck refs.txt --format text

# Pipe from stdin
pbpaste | python3 -m citecheck -

# Machine-readable output for CI
python3 -m citecheck references.bib --json

# A shareable report for co-authors (CSV opens in Excel; Markdown pastes into a PR)
python3 -m citecheck references.bib --report csv > citations.csv
python3 -m citecheck references.bib --report markdown

# Also cross-check every PMID against PubMed (fuller retraction coverage +
# PMID/DOI consistency). Needs internet access to eutils.ncbi.nlm.nih.gov.
python3 -m citecheck references.bib --pubmed

# Check the reference table straight out of Excel, or the reference list
# straight out of the Word manuscript — no export step
python3 -m citecheck included_studies.xlsx
python3 -m citecheck manuscript.docx

# Profile the reference list as a whole (median age, Price index, journals...)
python3 -m citecheck references.bib --profile
python3 -m citecheck references.bib --profile --as-of 2026   # reproducible ages
python3 -m citecheck references.bib --self-cite Kim,Park     # implies --profile

# For references that cite no DOI, search Crossref and report the DOI to add
python3 -m citecheck references.bib --suggest-doi

# Cache lookups so re-running through a round of revisions is instant
python3 -m citecheck references.bib --cache          # ~/.cache/citecheck/lookups.json
python3 -m citecheck references.bib --cache my.json --cache-ttl 1

# Fail the run on warnings too (not just errors)
python3 -m citecheck references.bib --strict

# ...but ignore the checks you've decided don't matter (see --list-checks)
python3 -m citecheck references.bib --strict --ignore no-doi,correction
python3 -m citecheck --list-checks

# Join Crossref's faster "polite" pool (your email is sent in the request header)
python3 -m citecheck references.bib --mailto you@example.com
```

### Example

```
$ python3 -m citecheck examples/sample.bib --verbose --no-color

✗ broken_doi
    DOI does not resolve anywhere (Crossref or doi.org) — check for a typo: 10.9999/does.not.exist
! ioannidis2005
    Crossref says this reference has a correction issued. (notice: 10.1371/journal.pmed.1004085)
! wrong_year
    Year mismatch: cited 2015, Crossref says 2010.
! no_doi
    No DOI found — cannot verify against Crossref.
✓ prisma2009
    Verified: Moher (2009) — Preferred Reporting Items for Systematic Reviews and Meta-Analyses: The PRISMA Statement

checked 5 references: 1 ok, 3 warnings, 1 errors
  (3 of 5 compared against a Crossref record; 2 could not be — no DOI, broken DOI, or not in Crossref)
```

Errors sort first, so the reference that needs attention is at the top. Run
without `--verbose` to hide the verified `` lines and see only problems.

### Exit codes

| Code | Meaning |
|------|---------|
| `0` | All references checked; no errors (and no warnings under `--strict`) |
| `1` | At least one error (broken DOI / retraction), or a warning under `--strict` |
| `2` | Usage problem: file unreadable, no references found, or no input given on a terminal |
| `3` | Inconclusive: at least one lookup failed (e.g. offline), or a BibTeX entry could not be parsed and was skipped, so verification is incomplete (some references may still have been verified) |

So it works as a CI gate:

```yaml
# .github/workflows/citations.yml
- run: pip install git+https://github.com/hyeonjoong/citecheck.git
- run: python3 -m citecheck paper/references.bib
```

## What gets checked

| Check | Severity | Meaning |
|-------|----------|---------|
| DOI does not resolve on Crossref *and* not at doi.org | error | The DOI is wrong or the record is gone |
| Retraction flag in Crossref | error | Crossref marks the work as retracted (see caveat below) |
| Retraction flag in PubMed (`--pubmed`) | error | PubMed marks the PMID as a "Retracted Publication" |
| Expression of concern / withdrawal / removal in Crossref | error | The literature has flagged the work's integrity - decide consciously before citing it |
| Correction / erratum / corrigendum / addendum / clarification / new edition-or-version in Crossref | warning | The paper stands, but a reported value may have changed - check you're citing the corrected one |
| Preprint with a published version | warning | You cited the preprint; the peer-reviewed DOI is named |
| PMIDvsDOI mismatch (`--pubmed`) | warning | The cited DOI differs from the DOI PubMed has registered for the cited PMID |
| PMID present but no DOI cited (`--pubmed`) | warning | PubMed knows this PMID's DOI - it's named so you can add it (works without `--suggest-doi`) |
| PMID not found in PubMed (`--pubmed`) | warning | The cited PMID doesn't exist - likely a typo |
| DOI resolves but is not in Crossref | warning | Likely a dataset/preprint/DataCite DOI; metadata not checked |
| Title similarity < 80% (vs. title, or title+subtitle) | warning | Likely wrong DOI for this citation |
| Year mismatch (vs. *any* of Crossref's dates) | warning | Cited year ≠ any Crossref publication date |
| First-author mismatch (< 85% vs. any listed author) | warning | Cited surname ≠ Crossref authors (diacritics folded) |
| Journal mismatch (vs. container title, ISO-4 abbreviations tolerated) | warning | Cited journal ≠ Crossref container (a second swapped-DOI signal) |
| Duplicate DOI | warning | Same DOI cited under two references |
| Duplicate PMID | warning | Same PubMed ID cited under two references |
| No DOI present | warning | Nothing to verify against (the PMID, if any, is surfaced) |
| Crossref match for a DOI-less reference (`--suggest-doi`) | warning | Crossref holds this paper - the DOI to add is named |

Thresholds are conservative to avoid false alarms on formatting differences.
Use `--strict` to make warnings fail the run as well.

Some details of the comparison:

- A DOI that Crossref does not know is looked up in the doi.org handle registry
  (`https://doi.org/api/handles/<doi>`). That answer comes from the DOI system
  itself, without following the redirect to the publisher's site, so a
  publisher page that is down or has a broken certificate cannot turn a
  registered DOI into an error. If the handle server itself cannot answer, the
  reference reports `lookup-failed` (exit 3), never a broken DOI.
- Crossref titles are compared as plain text. JATS/HTML tags that publishers
  deposit (`<scp>OSA</scp>`, `cmH<sub>2</sub>O`) and entities (`&amp;`) are
  removed, and Unicode hyphens (U+2010 and friends) count as `-`.
- A title is also compared with the record's `original-title` when it has one,
  and the year with any of the online, print and issued dates.
- Author names: diacritics are folded, LaTeX accents in a `.bib` are decoded
  (`{\"O}zt{\"u}rk` is Öztürk), a brace-protected corporate author such as
  `{The Editors of The Lancet}` is kept whole and also compared with
  organisation authors, a trailing "et al." (or Korean 외, 등, 외 2인) is
  dropped before the comparison, and in an author list separated by semicolons
  ("Smith J; Lee K") only the first name is read.

### Finding codes, `--ignore`, and using `--strict` as a real gate

Every finding carries a stable code (`retracted`, `no-doi`, `title-mismatch`,
...). `python3 -m citecheck --list-checks` prints them all with their meanings.
The code appears in the JSON report and in a `codes` column in the CSV report, so
CI can branch on it instead of regex-matching English prose:

```json
{ "severity": "error", "code": "retracted", "message": "Reference appears to be RETRACTED …" }
```

Codes make `--strict` usable as a submission gate. On its own, `--strict` fails
forever on any real manuscript - every one of them cites books, guidelines and
package inserts that simply have no DOI. So name the checks you've decided don't
matter:

```bash
# Fail on anything meaningful, but don't fail on DOI-less guidelines or on
# papers that merely had an erratum issued.
python3 -m citecheck refs.bib --strict --ignore no-doi,correction
```

`--ignore` only silences the *report* - it never skips a lookup, so it cannot
change what else is found. An unknown code is a usage error (exit 2), not a
silent no-op: a typo'd `--ignore retracton` must never quietly fail to suppress
what you asked for, nor quietly suppress a retraction.

`lookup-failed` cannot be ignored. Every other code is a judgement call
you're entitled to make. That one isn't a judgement about a citation - it means
the check *didn't happen*, and hiding it would let an offline run report a clean
pass, which is the one thing this tool promises never to do.

Output is ordered errors first, then warnings, then verified - on a
200-reference manuscript the one retraction shouldn't be buried among sixty
"No DOI found" lines.

### Input formats

| Format | Auto-detected by | Fields used |
|--------|------------------|-------------|
| BibTeX | a real `@type{key,` entry header | title, author, year, journal, doi, pmid |
| RIS | a `TY  - ` record header | `TI`/`T1`, `AU`/`A1`, `PY`/`Y1`, `JO`/`JF`/`T2`, `DO`, `AN` (PMID) |
| CSL-JSON | a JSON array/object of citation items | `title`, `author`, `issued`, `container-title`, `DOI`, `PMID` |
| CSV / TSV | a header row naming a DOI/PMID/title column | columns matched by name, not position - `DOI`, `Article DOI`, `PMID`, `Title`, `Authors`, `Year`, `Journal`/`Source`, `Study ID` and many aliases |
| Excel `.xlsx` | a ZIP container holding `xl/workbook.xml` | converted to a CSV table (same column matching); the sheet that best looks like a reference table is used, in workbook tab order |
| Word `.docx` | a ZIP container holding `word/document.xml` | converted to text, one reference per paragraph |
| plain text | anything else | doi + (guessed) author/year/pmid, one ref per line/paragraph |

A CSV/TSV table is how a systematic review's included-studies list usually
lives (Excel, Sheets, Covidence, Rayyan, EndNote's tab-delimited export).
citecheck sniffs the delimiter (`,` `\t` `;` `|`), matches columns by name in any
order or capitalisation, and - if the DOI is parked in a "Notes" or "URL" column
instead of a DOI column - still finds it by scanning the row.

Excel input (`.xlsx`). Converted to a CSV table and parsed as a table -
the same column-alias matching described above, never as free text. Rows above
the header (a banner such as "Included studies (n=42)") are dropped, and when a
workbook has several tabs the one that yields the most identifiable references
wins, so a workbook that opens on a cover/PRISMA-counts tab still reads
correctly. citecheck prints which worksheet it read; `--sheet NAME|N` overrides
the choice (`--sheet "Included studies"`, `--sheet 2`). If no sheet holds a
recognisable reference table you get a usage error naming the problem, not a
silent run over junk rows. That a worksheet is always parsed by column is also a
*privacy* property: the free-text path sends a reference's raw line to Crossref
when `--suggest-doi` is on, and a clinical extraction sheet's row includes its
Study-ID, MRN and notes columns.

Word input (`.docx`). Converted to one line per paragraph - including
footnotes and endnotes, where many manuscripts keep their citations - and
checked as plain text, with the reduced checking described in the next
paragraph (a Word file carries no structured fields). Two things to know:
citecheck checks the whole document, so every non-reference paragraph is
reported as "No DOI found" (paste just the reference list into a file, or add
`--ignore no-doi`, before using `--strict` as a gate); and a DOI that appears
*only* as a hyperlink target with different display text ("[Link]") is not
recovered, because the URL lives in the relationships part.

Both formats are treated as untrusted input: the archive's expanded size, member
count and row count are capped, and a part declaring a DOCTYPE is refused
(entity-expansion defence). `--encoding` does not apply to them (OOXML is always
UTF-8) and citecheck says so if you pass it. A binary file that is *not* an
Office ZIP - a legacy `.doc`/`.xls`, a PDF, an image - is named and refused
(exit 2) instead of being decoded as mojibake and "checked".

Structured vs. plain-text input. With structured BibTeX/RIS/CSL-JSON fields,
all of the above checks run. In plain-text/`--format text` mode the author and
year are only *guessed* from the line, so those comparisons (and the journal
comparison) are skipped to avoid false alarms; citecheck still verifies the DOI
resolves, checks for retraction, and - because a real citation quotes the
paper's title - warns if the cited line doesn't mention the Crossref title (a
swapped DOI). For full title/year/author/journal verification, use a structured
file.

#### BibTeX details

`@string` macros are expanded (`journal = jsr` with
`@string{jsr = {Journal of Sleep Research}}`), including `#` concatenation; a
macro the file never defines is left out of the comparison rather than compared
as the literal text. `@comment`, `@preamble` and parenthesised `@article(...)`
entries are handled. An entry whose braces never balance is skipped on its own,
the entries after it are still checked, and the run names it on stderr and exits
3 unless something else is an error:

```
citecheck: warning: 1 malformed BibTeX entry could not be parsed and was not checked (unbalanced braces or parentheses): bad.
```

#### Korean input

Reference tables exported from Korean Excel (CP949, or UTF-8
with a BOM, CRLF or bare-CR line endings) are read as tables; a CP949 file prints
`note: input was not UTF-8; decoded as cp949`. Korean column names are
recognised: 제목, 논문제목, 논문명, 표제 (title); 저자, 저자명, 제1저자, 주저자,
제1저자명 (author); 연도, 년도, 발행연도, 출판연도, 게재연도, 발행년도, 출판년도
(year); 학술지, 학술지명, 저널, 저널명, 게재지 (journal); 번호, 연번, 연구ID,
연구번호 (key). Bilingual headers such as `Title(제목)`, `DOI(링크)` or `DOI 번호`
are matched by their English part. When a table maps only its DOI or PMID
column, citecheck says so and lists the `Columns not used:`, so a skipped
comparison is visible. Hangul names are compared syllable by syllable, and a
KCI record that splits a name into family
and given (홍 + 길동) matches the full name. A field cited in Korean whose Crossref
record is English only (or the reverse) cannot be compared letter by letter, so
it is skipped and the verified line (shown with `--verbose`) ends with a note
such as `(title/author/journal not compared: cited in Korean, Crossref record in English)`.
The year, DOI and retraction checks still run.

### Report formats

`--report text` (default), `--report json` (`--json` is a shorthand),
`--report csv`, and `--report markdown`. CSV cells beginning with `=`, `+`, `-`,
`@` or a tab are prefixed with `'` so a malicious title can't run as a
spreadsheet formula when a co-author opens the file. Terminal control characters
are stripped from every report format - including JSON, where a raw escape would
otherwise reach a terminal via `jq -r`.

### Profiling the reference list (`--profile`)

`--profile` adds a block of descriptive statistics about the reference list as a
whole. Every number is computed from records already fetched, so it costs no
extra network call.

```
Reference profile (ages measured against 2026)
  references            42
  with a DOI            40 (95.2%)
  compared to Crossref  40 (95.2%)
  publication year      median 2018 (IQR 2013.3–2021.8, range 1998–2025)
  median age            8 years (n=38; 4 references with no year excluded)
  Price index           0.34 (13/38 published within 5 years)
  older than 10 years   14 (36.8%)
  journals              29 distinct across 42 references; top: The Lancet (5), New England Journal of Medicine (4), BMJ (3), JAMA (3), Circulation (2)
  Crossref types        journal-article 38, posted-content 2
  integrity flags       correction 3
  self-citations        3 of 40 (7.5%) match Kim
  (years are Crossref's where a record was retrieved, otherwise as cited; the Price index is the share published within the last 5 years)
```

(The `self-citations` line appears only with `--self-cite`; the `with a PMID`
line only when some reference carries one.)

How each number is defined (so you can recompute it by hand, and so a reviewer
can trust it):

| Metric | Definition |
|--------|------------|
| publication year | Crossref's earliest publication year where a record was retrieved, otherwise the year as cited. Which source was used for how many references is reported in the JSON output; references with no year at all are excluded and counted. |
| quartiles | linear interpolation between order statistics (`statistics.quantiles(..., method="inclusive")` - the same convention as R type 7 and numpy's default). |
| age | `as-of year - publication year`; `--as-of YEAR` pins it (default: the current year) so a run is reproducible. |
| Price index | share of references with age ≤ 5 (Price, 1970). Denominator = references with a known year, printed alongside. |
| older than 10 years | age > 10, strictly. |
| journals | grouped on the normalised name - diacritics, case, a leading article and punctuation are folded, so "Lancet", "The Lancet", "Lancet." and "lancet" are one journal - and Crossref's own container name is preferred over the cited one. Abbreviations are *not* resolved: if neither has a Crossref record to canonicalise it, "N Engl J Med" and "New England Journal of Medicine" still count as two. |
| integrity flags | counted per reference, not per finding - a paper with both an erratum and a corrigendum is one corrected reference. |
| self-citations | `--self-cite Kim,Park`: references whose author list (Crossref's, else the cited first author) contains that surname at ≥ 0.90 similarity, diacritic-folded. Denominator = references with any known author. |

The profile is built before `--ignore` is applied: `--ignore` hides report
lines, it does not change what the reference list is made of. (For the same
reason it carries no per-status counts - those would contradict the
`references[].status` values in the same JSON document, which *are* post-ignore.)

Two honest caveats. Offline (or behind a proxy), no Crossref record is
retrieved, so the years are entirely "as cited" and `compared to Crossref` reads
`0 (0%)` - read that line first. And on free-text or `.docx` input, `references`
counts *paragraphs*, not verified reference entries.

Where it appears: appended to the text report; as a `## Reference profile`
section in `--report markdown`; and in `--report json` the payload becomes
`{"references": [...], "profile": {...}}`. Without `--profile` the JSON
report stays exactly what it always was - a plain array - so existing CI scripts
are unaffected. With `--report csv` the profile goes to stderr, leaving
`> out.csv` a clean table for Excel.

### Retraction & integrity-flag detection

citecheck reports a retraction on any of three independent signals:

1. Crossref's Crossmark `updated-by` field - the field an *article* carries
   to name the notices issued against it - contains a retraction.
2. An `is-retracted-by` relation.
3. The publisher prefixed the record's title with `RETRACTED:` (or
   `WITHDRAWN:`). Elsevier, Springer, Wiley and NEJM all do this, and it is
   frequently present when Crossmark data is thin - so it is load-bearing, not a
   nicety. The marker is stripped before titles are compared, so a correctly
   cited retracted paper doesn't also report a bogus title mismatch.

citecheck deliberately does not read `update-to`. In principle that field
marks the retraction notice, but Elsevier deposits it symmetrically - the
retracted Lancet paper `10.1016/S0140-6736(20)31180-6` carries `update-to`
retractions pointing at its own notices - so it identifies neither side.

From the same `updated-by` data citecheck also reports expressions of
concern, withdrawals and removals (errors), and corrections /
errata / corrigenda / addenda / clarifications / new versions (warnings). A
retracted paper reports only its retraction; the softer flags are suppressed
rather than stacked on top.

The retraction notice's DOI is shown when Crossref actually names one. Most
publisher-deposited `updated-by` entries name the article's *own* DOI rather
than the notice's, and those are skipped - an honest silence beats a pointer
that leads back to the paper you started from.

This is not exhaustive: Crossref's retraction
coverage is incomplete (many older or smaller-publisher retractions are never
marked), so a clean result is *not* a guarantee that a paper hasn't been
retracted. Treat a retraction finding as a strong signal, but don't treat its
absence as proof.

For fuller coverage, pass `--pubmed`: for every reference that carries a
PMID, citecheck queries PubMed's E-utilities and flags a `Retracted Publication`
publication type (which frequently catches retractions Crossref hasn't marked),
and reports a PMIDvsDOI mismatch when the cited PMID and DOI resolve to
different papers. PubMed's coverage is also imperfect, so - as with Crossref - a
clean result is not a guarantee. `--pubmed` needs internet access to
`eutils.ncbi.nlm.nih.gov`; a failed PubMed lookup is reported and makes the run
exit 3 (inconclusive), never a false clean pass.

### Finding missing DOIs (`--suggest-doi`)

A reference with no DOI normally just warns "cannot verify". With
`--suggest-doi`, citecheck instead searches Crossref's bibliographic index for
the reference and, when it finds a near-certain match, reports the DOI.
`examples/suggest.bib` holds one real paper with its DOI left out:

```
$ python3 -m citecheck examples/suggest.bib --suggest-doi --verbose --no-color

! ioannidis2005
    No DOI found — cannot verify against Crossref.
    Crossref has a 100%-confident match for this reference — consider citing DOI 10.1371/journal.pmed.0020124
          match: Ioannidis (2005) — Why Most Published Research Findings Are False

checked 1 references: 0 ok, 1 warnings, 0 errors
  (0 of 1 compared against a Crossref record; 1 could not be — no DOI, broken DOI, or not in Crossref)
```

It is deliberately conservative. A suggestion needs ≥92% title similarity (or,
for a free-text reference, ≥80% of the matched title's words present in the cited
line), plus three hard rejects, any one of which drops the match silently:

| Reject | Why |
|--------|-----|
| The years disagree (when both state one) | Crossref happily returns a same-titled paper from another year - a later edition, a conference/journal pair |
| The first-author surnames disagree (when both state one) | Title similarity alone can't tell two papers with a generic title apart; the surname can |
| The cited title has fewer than 4 substantial words | A perfect score on `title = {Editorial}` discriminates typos, not papers - there are thousands of works titled "Editorial" |

A field that neither side states is never a reject, only a stated *disagreement*
is. A confidently wrong DOI is worse than none, so a merely-plausible match is
dropped silently rather than guessed at. Always eyeball the match line before
pasting the DOI in. The flag is off by default because it costs one extra
Crossref call per DOI-less reference.

If the matched paper is retracted, citecheck says so and does not offer it for
citation - it reports an *error* naming the DOI so you can go check. This is
the one case where a DOI-less reference gets you more than a shrug: citecheck can
tell you the paper you cited has been retracted even though you never gave it a
DOI to look up.

### Caching (`--cache`)

Re-checking a 150-reference manuscript through a round of revisions means 150
network round-trips every time. `--cache` stores Crossref/PubMed lookups on disk
so the second run is instant and Crossref is spared the load:

```bash
python3 -m citecheck refs.bib --cache               # ~/.cache/citecheck/lookups.json
python3 -m citecheck refs.bib --cache ./ci-cache.json --cache-ttl 1
```

(The bare `--cache` path honours `XDG_CACHE_HOME` if you set it.)

Entries expire after `--cache-ttl` days (default 7) - *by design*: the point
of the tool is to catch a newly retracted reference, and an indefinite cache
would eventually report a stale clean pass. A cache is only ever an optimisation:
a missing, corrupt, or unwritable cache file degrades to "no cache" and never
changes a verdict or fails a run.

## Why

When you finalize a paper you often verify references by hand against Crossref,
PubMed, or DataCite. `citecheck` automates the Crossref half of that pass so a
swapped DOI or a retracted source can't make it into submission.

## Development

```bash
python3 -m pip install --upgrade pip
python3 -m pip install -e ".[dev]"
python3 -m pytest
```

Tests run fully offline - the Crossref client accepts an injected transport, so
no network calls happen in CI for the unit tests. A client given only `_fetch`
never contacts doi.org either. Changes per release are listed in
[CHANGELOG.md](CHANGELOG.md).

## License

MIT © hyeonjoong
