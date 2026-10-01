# Hardening log

Adversarial, multi-round hardening of `citecheck`. Each round runs an
independent panel of reviewers (correctness, edge cases, real-world usefulness,
docs honesty, tests/security), then every material finding is fixed with a
regression test added, and the suite is re-run to green.

> ## Correction (2026-07-16, Rounds 4-5)
>
> Rounds 1-3 below contain false claims, left in place as a record of how the
> failure happened. They repeatedly describe `citecheck` detecting retractions
> via a Crossref Crossmark field called `update-by`. *Crossref has no such
> field* - the real one is `updated-by`. Reading a key that never exists
> silently disabled retraction detection entirely, and the tool reported
> `Verified` for Wakefield 1998 (whose own Crossref title begins
> "RETRACTED:") through all three rounds.
>
> Round 3's "Verified holdings" section specifically certifies *"retraction
> narrowing (Crossref `update-by`/`is-retracted-by`...)"* as independently
> verified. It cannot have been: no live Crossref payload has that key. The
> fixtures were built around the same invented name as the code, so ~180 tests
> were green against a field the API never sends, and the docs were re-verified
> "command-by-command" *against* the bug - with `update-by` dead, no correction
> was detected, so the sample output matched the README and both were
> consistently wrong.
>
> Treat any "Verified holdings" claim in Rounds 1-3 as unproven. They certify
> what the fixtures were rigged to certify. Round 4 rebuilt the retraction
> fixtures from live `api.crossref.org` payloads
> (`tests/test_retraction_real_shapes.py`); that file, not this log, is the
> trustworthy record.
>
> The lesson generalises beyond one typo: a test whose fixture is written from
> the same assumption as the code proves only that the assumption is
> self-consistent. Rounds 4-5 therefore verify fixtures against the live API,
> and use mutation testing to check that tests actually fail when the code
> breaks.

---

## 2026-07-16 - Round 1

Panel: 5 independent reviewers (correctness, edge-cases/robustness, 
clinical-researcher usefulness, docs honesty, test-quality/security).
Baseline: 13 tests passing. After round: 62 tests passing, offline.

### Correctness / parsing fixes

- DOI given as a URL or `doi:`-prefixed in a BibTeX `doi` field produced a
  false "DOI does not resolve" *error* (the raw URL was percent-encoded whole
  into the Crossref path). Reference managers export this form by default. Fixed:
  `parse_bibtex` now routes the field through `normalize_doi_field`, which strips
  `https://doi.org/`, `dx.doi.org`, and `doi:` prefixes and extracts the DOI
  core. (`parsers.py`)
- DOI regex dropped `)`, truncating Elsevier/Wiley DOIs such as
  `10.1016/S0140-6736(97)11096-0`. Fixed: the regex now allows parentheses, and
  `_clean_doi` trims trailing punctuation with bracket balancing (a wrapping
  `)` from "(doi: ...)" is removed, an internal balanced `)` is kept). (`parsers.py`)
- `@string`/`@comment` macros swallowed the following real entry because the
  cite-key regex `[^,]*` ran across brace boundaries to the next comma. Fixed:
  the key is bounded (`[^,{}\s]*`), non-reference entry types are skipped, and
  the brace-matcher's forward scan is bounded by the next entry header. (`parsers.py`)
- Retraction detection missed genuinely retracted *articles*. It only checked
  the retraction *notice* shape (`type == "retraction"`, `update-to`). A retracted
  article carries Crossmark `update-by`. Fixed: `_is_retracted` now checks
  `update-by`, `update-to`, and `is-retracted-by` relation keys, and the finding
  <!-- FALSE (see Correction above): `update-by` is not a Crossref field. This
  "fix" fixed nothing; retraction detection stayed dead until Round 4. -->
  surfaces the retraction-notice DOI. Docs now state the coverage caveat honestly.
  (`core.py`, `README.md`)
- Year comparison preferred `published-print`, producing false "year
  mismatch" on online-ahead-of-print papers. Fixed: `_crossref_years` collects
  every date field (print/online/issued/created); a citation matches if it
  equals *any* of them. (`core.py`)
- Subtitle dropped -> false title mismatch on trial titles. Fixed: title
  similarity is computed against both the main title and `title + subtitle`,
  taking the best. (`core.py`)
- `parse_text` guard/split disagreed (`"\n\n" in text` vs. the `\n\s*\n`
  split), shattering multi-line references when a blank line contained
  whitespace. Fixed: guard and split now use the same regex. (`parsers.py`)
- Diacritics not folded -> "Müller" vs "Muller" false author mismatch. Fixed:
  `_norm` now NFKD-folds and strips combining marks; author check also matches
  against *any* listed author. (`core.py`)
- Auto-detect misfired on plain text containing `@` + `{` (e.g. an email
  address), routing to the BibTeX parser and losing the reference. Fixed:
  detection now requires a real `@type{` header. (`parsers.py`)

### Robustness / crash fixes

- Non-UTF-8 input crashed with an uncaught `UnicodeDecodeError` (the guard
  only caught `OSError`) - common for cp949/latin-1 exports. Fixed: input is read
  as bytes and decoded with a utf-8 -> utf-8-sig -> cp949 -> latin-1 fallback chain,
  with a stderr note on non-UTF-8. (`cli.py`)
- A single malformed Crossref record aborted the whole batch (comparison
  helpers ran outside any guard; e.g. `update-to` as a string). Fixed: the
  comparison block is wrapped (bad record -> warning, not a crash), and every
  helper type-guards its inputs (`_as_list`, dict/int checks). (`core.py`)
- O(n²) brace scanning on unbalanced BibTeX let a truncated file burn CPU
  (21 s for 8k bad entries). Fixed: the forward scan is bounded by the next entry
  header, so malformed entries cost O(entry), not O(file). (`parsers.py`)
- Negative `--delay` crashed (`time.sleep(-5)`). Fixed: `--delay` validates
  `>= 0`. (`cli.py`)
- `title`/`author` as a bare string (not a list) returned the first
  *character*. Fixed by `_as_list` coercion + string filtering. (`core.py`)

### Usefulness improvements

- "Not in Crossref" is no longer a hard error when the DOI still resolves. On
  a Crossref 404, citecheck now does a `doi.org` handle check: a resolvable DOI
  (dataset/preprint/DataCite) becomes a *warning*, only a truly dead handle is an
  *error*. (`core.py`)
- Duplicate-DOI detection across the manuscript (same DOI under two keys).
  (`cli.py`)
- Per-DOI caching / memoisation so a repeated DOI (or a re-run) hits the
  network once. (`core.py`)
- `--strict` promotes warnings to a non-zero exit for a stricter submission
  gate. (`cli.py`)
- Clearer summary: "N verified against Crossref, M could not be verified",
  separating genuine verification from unverifiable references. (`cli.py`)

### Security / PII

- ANSI/terminal-escape injection: crafted BibTeX titles or poisoned Crossref
  records could emit escape sequences to the terminal (e.g. fake a "all
  verified"). Fixed: all externally sourced text is stripped of C0/C1 control
  chars before printing. (`cli.py`)
- Confirmed clean: URL quoting is SSRF/CRLF-safe (`quote(doi, safe="")`); no
  `eval`/`exec`/`pickle`/shell; the default User-Agent carries only a placeholder
  email (a real `--mailto` is sent only when explicitly provided). Added
  regression tests pinning the UA behaviour.

### Tests

- Added `tests/conftest.py` - an autouse fixture that blocks real sockets, making
  the offline-test guarantee an enforced invariant (accidental live calls now
  fail loudly).
- Added `tests/test_cli.py` (exit-code contract 0/1/2, `--json` shape, non-UTF-8
  handling, ANSI stripping, `--strict`, duplicate-DOI, malformed-entry reporting).
- Extended `tests/test_core.py` and `tests/test_parsers.py`: retraction shapes
  (`update-by`/`update-to`/relation - FALSE, see Correction above), year-any-date, subtitle, diacritics,
  malformed-record parametrised no-crash, caching, DOI-URL/paren normalisation,
  `@string`/`@comment`, auto-detect, text-splitting.

### Docs honesty

- README's fabricated example (keys `smith2020/jones2019/doe2018` that were
  never in the sample, invented findings and totals) replaced with the tool's
  real output on the shipped sample.
- Added an explicit "requires internet" disclosure and offline behaviour.
- Usage now leads with `python3 -m citecheck` (the bare `citecheck` command is
  only on `PATH` after an install); removed the author's private, nonexistent
  path from `사용법.md` and `실행.command` in favour of a placeholder.
- Added an honest retraction-detection caveat (Crossref coverage is
  incomplete).
- Rewrote `examples/sample.bib` with distinct real DOIs so each demo finding
  (verified / broken / year-mismatch / no-DOI) is reproducible.

---

## 2026-07-16 - Round 2

Panel: 4 independent reviewers re-reviewed the hardened code (correctness, 
edge-cases, docs+tests, clinical usefulness). Baseline: 62 tests. After
round: 83 tests passing, offline. Round 2 caught two regressions Round 1
introduced, plus real gaps.

### Regressions introduced in Round 1 (now fixed)

- HIGH - BibTeX splitter dropped a valid entry whose field value contained a
  `@type{key,` pattern (a title discussing BibTeX, or a URL/note field). Round
  1's "bound the scan by the next entry header" fix mis-bounded such entries and
  fabricated a phantom entry from the inner text. Replaced with a correct
  single-pass, brace-depth scanner (`_scan_entries`): a `@...{` inside a field
  value is consumed as part of the enclosing body via depth, never mistaken for
  a top-level entry. Still O(n) (verified: 20k unterminated entries in 0.03 s),
  so the Round-1 DoS fix is preserved. (`parsers.py`)
- Docs contradiction - offline exit code. Round 1's docs claimed an offline
  run "does not exit 0 / never falsely passes," but the default (non-strict)
  path returned 0 on all-warnings. Fixed for real: a lookup failure now yields a
  distinct exit code 3 (inconclusive), so a network outage can't read as a
  clean CI pass; docs (README + 사용법) corrected to match. (`cli.py`)

### New correctness fixes

- URL query string / fragment not stripped from a DOI (e.g. a browser-copied
  `...0312345?utm_source=...`) produced a false "does not resolve" error. `_clean_doi`
  now cuts at `?`/`#`. (`parsers.py`)
- `normalize_doi_field` fallback returned junk (`_clean_doi(value)`) for a
  field with no DOI core, so "not a doi" became a DOI. Now returns None. (`parsers.py`)
- <!-- The premise is FALSE (see Correction above); the real bug was the
  opposite - `update-to` made us report the paper's OWN doi as its notice. -->
- Retraction-notice DOI missed when `update-by` matched on type but carried
  no DOI while `update-to` had it - `_retraction_notice` now falls through to the
  DOI-bearing field. (`core.py`)
- Retraction relation over-matched `is-retraction-of` (which identifies the
  *notice*, not a retracted paper); narrowed to `is-retracted-by`. (`core.py`)

### New robustness fixes

- CRASH - `--delay nan` / `--delay inf` passed the `>= 0` check (`nan < 0`
  and `inf < 0` are both False) and then crashed in `time.sleep`. `_nonneg_float`
  now rejects non-finite values with a clean usage error (exit 2). (`cli.py`)
- UTF-8 BOM decoded to a phantom `﻿` reference (the `utf-8-sig` branch
  was dead code behind `utf-8`). BOMs (UTF-8 and UTF-16 LE/BE) are now detected
  first and stripped; added a `--encoding` override so a user whose Western
  file was greedily mis-decoded as cp949 can force the right codec. (`cli.py`)
- Non-UTF-8 Crossref *response* body (`resp.read().decode("utf-8")`) could
  raise `UnicodeDecodeError` outside the retry loop's caught set; added it. (`core.py`)

### Usefulness fixes

- Text-mode false positives suppressed. Free-text parsing guesses author from
  the first capitalised word ("Effect of aspirin..." -> "Effect") and year from the
  first year-like token ("2000 patients ... 2019" -> 2000), producing false
  author/year mismatches on otherwise-correct citations. References parsed from
  free text are now marked `heuristic_fields=True`, and the verifier skips the
  author/year comparisons for them (DOI, title, retraction checks still run).
  (`parsers.py`, `core.py`)
- Clearer error wording: "DOI does not resolve on Crossref" -> "does not
  resolve anywhere (Crossref or doi.org) - check for a typo" (it fires only after
  the doi.org handle check also fails). (`core.py`)

### Tests added (62 -> 83)

- Nested-`@`-in-field regression; `count_malformed_entries`; `normalize_doi_field`
  direct + query-string cases; retraction-notice fall-through and relation
  narrowing; `_crossref_years` shapes (int/str/multi/empty); fetch and resolve
  caching; monkeypatched real-transport tests (`_fetch_network` 404-no-retry and
  retry-then-raise; `_resolve_network` 200->True / 404->False); UTF-8-BOM/UTF-16/
  `--encoding` decoding; `--delay` non-finite rejection; exit-code-3 offline;
  `--strict` in JSON mode; text-mode heuristic suppression.

### Verified holding (no change needed)

Reviewers independently confirmed: DOI-quoting is SSRF/CRLF-safe; no
`eval`/`exec`/`pickle`/shell; the socket guard blocks all real network in tests;
the O(n) splitter and Counter-based duplicate detection scale linearly (5k refs
in ~0.04 s); `core.py` is defensive against every malformed Crossref shape tried;
the `--strict` exit matrix is correct in text and JSON; README's demo output now
matches the tool line-for-line.

### Known limitations (documented, not bugs)

- Retraction detection relies on Crossref's incomplete coverage (README caveat).
- A mid-file unterminated BibTeX entry may swallow following entries; the CLI
  warns that entries were skipped.
- Legacy Wiley SICI DOIs containing `<`/`>` are not matched (excluded to avoid
  capturing XML markup); modern DOIs are unaffected.

---

## 2026-07-16 - Round 3

Panel: 2 fresh reviewers (correctness+robustness combined, docs+tests+
usefulness). Baseline: 83 tests. After round: 91 tests passing, offline.
The correctness/robustness reviewer found no bug on any valid input and
validated every Round-2 fix; the usefulness reviewer surfaced two real issues.

### Fixes

- HIGH - transient doi.org failure was reported as a false hard error. When
  Crossref returned 404, `check_reference` called `resolve()`, whose
  `_resolve_network` collapsed *every* exception (including a transient timeout /
  5xx / 429) to `False` -> "DOI does not resolve anywhere" ERROR -> failed the CI
  gate for a legitimate reference. Now `_resolve_network` returns `False` only on
  a real doi.org 404 and re-raises transient errors; `check_reference` treats
  a resolve failure as an inconclusive "Lookup failed" warning (exit 3), never
  a hard error. (`core.py`)
- MEDIUM - plain-text mode verified less than the docs implied. `parse_text`
  never extracts a title, and author/year are suppressed as unreliable, so a
  swapped-but-valid DOI (pointing to a *different* real paper) passed clean. Added
  a conservative title-containment check: for a free-text reference, warn if the
  cited line's words don't overlap the Crossref title (a strong swapped-DOI
  signal). Skipped for bare-DOI lines (no prose) and only fires well below half
  overlap, so it does not manufacture false positives. Verified live: a correct
  citation stays ``; a DOI pointing to an unrelated paper is flagged. Docs
  (README + 사용법) now state exactly what text mode checks. (`core.py`, docs)

### Tests added (83 -> 91)

- Text-mode: swapped-DOI caught via title, correct-title stays clean, bare-DOI
  not flagged, transient-resolve-failure -> inconclusive (not error).
- CLI end-to-end: unknown `--encoding` -> exit 2, resolvable-not-in-Crossref
  warning, retraction -> exit 1, summary "verified / could not be verified"
  counts, unterminated-mid-file entry is counted and prior entries survive.

### Verified holding

Both reviewers confirmed: `_scan_entries` is correct and O(n) (40k entries in
0.07 s) with no valid-input misparse; DOI query/fragment stripping preserves
balanced parens; BOM/UTF-16 decoding and invalid-codec->exit-2 work; the exit-code
matrix is correct including "error dominates over inconclusive"; retraction
narrowing (`is-retracted-by` yes, `is-retraction-of` no) holds; all malformed
Crossref shapes degrade to warnings without crashing; the socket guard blocks all
real network in tests. Only LOW/informational note: an unbalanced brace inside a
quoted BibTeX value (which real BibTeX also rejects) truncates that one entry.

---

## 2026-07-16 - Round 4 (verification)

Panel: 1 focused regression reviewer over the Round-3 changes (resolve
tri-state, exception-cache handling, title-containment heuristic). Baseline:
91 tests. After round: 93 tests passing, offline.

Result: clean - no regression. The reviewer confirmed the resolve tri-state
and exit-code mapping (transient -> inconclusive/exit 3, 404 -> error, resolvable ->
warning), that raised transport exceptions are never cached (re-raised each
call), that the title-containment heuristic stays clean on legitimate citations
(verbatim, subtitle, stopword-heavy, accented, punctuated) while catching the
swapped-DOI case, and that BibTeX refs never reach the text-mode check (no
double-warn).

One small polish applied: `_alpha_tokens` now extracts alphabetic sub-words
(`[a-z]+`) instead of dropping any token containing punctuation/digits, so a
hyphenated/technical title ("COVID-19", "neural-networks") no longer skews the
overlap - reducing the reviewer's noted low-severity over-warn edge. Added two
regression tests (punctuation-tolerant match stays clean; unrelated title still
flagged).

Two rounds (R3 correctness, R4) now pass clean, so hardening stops here.

---

## 2026-07-16 - Deep improvement + 3-round hardening (new capabilities)

A second, larger pass: real new capability plus three fresh adversarial panels
(5 + 3 + 3 independent reviewers), each recomputing statistics from first
principles and hammering edge cases, then every material finding fixed with a
regression test. Baseline: 93 tests. After: 179 tests passing, fully
offline.

### New capabilities (genuine, tested)

- RIS input (EndNote / Zotero / Mendeley export). `parse_ris` handles the
  `TY...ER` record model, continuation lines, `TI/T1/BT` titles, `AU/A1`
  authors, `PY/Y1/DA` dates, `JF/JO/JA/T2` journals, `DO`/whole-record DOI
  recovery, and `AN` -> PMID only when the provider tags say PubMed/MEDLINE.
- CSL-JSON input (Zotero / Better BibTeX / pandoc). `parse_csl_json` reads
  `title`/`author` (family or literal) / `issued` (date-parts, raw, literal) /
  `container-title` / `DOI` / `PMID`, tolerating str-or-list fields and
  non-citation JSON.
- Journal / container mismatch check - a second independent swapped-DOI
  signal. `_journal_matches` folds diacritics and leading articles ("Lancet" =
  "The Lancet") and understands ISO-4 abbreviations, including *contracted*
  ones that drop interior letters ("Am J Med", "N Engl J Med", "Proc Natl Acad
  Sci", "J Natl Cancer Inst"). Deliberately permissive: only a clear mismatch
  ("Nature" cited, Crossref says "Lancet") warns.
- PMID awareness - parsed from every format (labelled only, so a page
  number is never mistaken for a PMID); surfaced in the no-DOI message and JSON;
  duplicate-PMID detection (suppressed when already a duplicate DOI).
- `--pubmed` cross-check (opt-in) - a stdlib `PubMedClient` (esummary,
  injectable transport like `CrossrefClient`) that catches retractions Crossref
  misses (PubMed "Retracted Publication" pubtype, distinct from the
  "Retraction of Publication" notice) and reports a PMIDvsDOI mismatch when
  the cited PMID and DOI resolve to different papers. Runs before the no-DOI
  early return; a PubMed lookup failure reads as inconclusive (exit 3), never a
  false clean pass.
- CSV and Markdown reports (`--report csv|markdown`, alongside `text`/`json`)
  for sharing with co-authors. CSV cells beginning `= + - @ \t \r` are
  quote-prefixed to defuse spreadsheet formula injection.
- biblatex fields - `date=` (year) and `journaltitle=` are now read, so
  Better BibTeX / biblatex exports get full year + journal verification.

### Hardening fixes (each with a regression test)

Round 1 (5 reviewers):
- ReDoS (HIGH) in `_PMID_RE`: `\s*:?\s*` had ambiguous adjacent whitespace
  quantifiers -> O(N²) on `pmid<many spaces>` reachable from any untrusted file
  (≈30 s at 80k spaces). Collapsed to a single `[\s:]*` class -> 0.002 s.
- `RecursionError` crash on pathologically deep JSON: `json.loads` raises
  `RecursionError` (a `RuntimeError`, not `ValueError`); added it to the guards
  in `looks_like_csl_json`/`parse_csl_json` so a crafted `.json` degrades to
  "not CSL-JSON" instead of a traceback.
- JSON report leaked C1 controls: `_to_json` didn't sanitize; a poisoned
  title/journal/DOI could smuggle a raw `0x9b` (CSI) into JSON stdout. Now
  `_sanitize`s label, doi, journal, and every message - consistent with the
  text/csv/markdown paths.
- Journal false-positive on short-token abbreviations: `_is_abbrev_of` used
  `_alpha_tokens` (drops <3-char words) so "Am J Med" reduced to `["med"]` and
  falsely mismatched "American Journal of Medicine". Switched to `_journal_words`
  (keeps short words).
- RIS false-trigger: a lone `TY  - ...` line in plain prose routed a whole
  file into a single RIS record (silent data loss). Detection now requires a
  `TY` header AND (an `ER` terminator OR ≥3 tag lines).
- Invalid PMID `0` accepted; `_clean_pmid` now rejects zero/empty and strips
  leading zeros everywhere (BibTeX, RIS `AN`, CSL, free text).

Round 2 (3 reviewers):
- `_pubmed_crosscheck` crash on a non-dict record (contract violation from a
  transport): added the symmetric `isinstance(record, dict)` guard the Crossref
  path already had.
- JSON `doi` field still unsanitized (a DOI can carry `0x9b`); sanitized it.
- Doc honesty: exit-3 wording corrected (a lookup failure means verification is
  *incomplete*, not that *nothing* was verified - `--pubmed` makes the mixed
  case common); PMIDvsDOI wording tightened; added CLI-level exit-code tests
  (exit 3 on PubMed failure; notice-not-flagged -> exit 0) and a status assertion.

Round 3 (3 reviewers):
- Contracted ISO-4 abbreviations ("Proc Natl Acad Sci", "J Natl
  Cancer Inst", "Dtsch Arztebl Int") produced false journal-mismatch warnings
  when Crossref lacked `short-container-title`, because `_is_abbrev_of` required
  a *prefix*. Introduced `_is_word_contraction` (first-letter-anchored
  subsequence), which subsumes the prefix rule -> no regressions, fixes PNAS/JNCI.
- Latent quadratic in `_guess_text_author` (two `\s*` groups straddling a
  leading whitespace run) - unreachable today (`parse_text` strips first), but
  removed the redundant leading `\s*` (via `.lstrip()`) as defense in depth and
  added a linearity regression test.

### Verified holdings

Across the panels: RIS/CSL/BibTeX/text parsing, DOI normalization (parens, URL,
query strings), retraction narrowing (Crossref `update-by`/`is-retracted-by` -
FALSE, never verifiable: see the Correction at the top of this file;
PubMed "Retracted Publication" vs the notice), the journal matcher on ~15 real
medical abbreviations (match) and genuinely-different journals (no match), all
four report formats' sanitization + CSV formula-injection guard, the offline
socket guard now covering `PubMedClient`, no `--mailto`/PII leak into any report,
and every regex linear at N≥200k. Docs (README / 사용법.md / 실행.command) were
re-verified command-by-command against the code with the example files
(`sample.bib`/`.ris`/`.json`) parsing correctly.


---

## 2026-07-16 - Rounds 4-5 (features + the retraction bug)

Panel: 6 independent reviewers across two rounds (correctness ×2, edge
cases, clinical-researcher usefulness, docs honesty, test-quality/security).
Reviewers were given live network access and told to check assumptions against
`api.crossref.org` rather than against this repo's fixtures - which is how the
headline bug was finally caught.
Baseline: 179 tests. After: 500 tests, offline, plus live end-to-end
verification against real Crossref records.

### The headline bug: retraction detection never worked

`citecheck` reported `Verified` for Wakefield 1998
(`10.1016/S0140-6736(97)11096-0`) - the most famous retraction in medicine -
and for Mehra's retracted NEJM and Lancet COVID papers. The cause was a
one-word typo with total consequences: the code read `update-by`; the Crossref
field is `updated-by`. Every retraction fixture in the suite was built around
the same invented name, so the tests were green and three hardening rounds
certified it as working (see the Correction at the top of this file).

Fixed, and verified live - all three now report `ERROR` with the correct notice
DOI, and the retraction *notice* correctly stays citable:

- `_is_retracted` now reads the real `updated-by`, and additionally detects the
  publisher's `RETRACTED:` title prefix (Elsevier/Springer/Wiley/NEJM all
  use it; it is often the *only* signal - Mehra's NEJM paper has no Crossmark
  retraction at all, only an expression of concern plus the title marker).
- `update-to` is now deliberately not consulted. It is not the clean
  "this is the notice" marker Rounds 1-3 assumed: Elsevier deposits it
  *symmetrically*, so the retracted Lancet paper carries `update-to` retractions
  pointing at its own notices. `updated-by` classifies every real record we
  checked correctly on its own.
- The `RETRACTED:` prefix is stripped before title comparison, so a correctly
  cited retracted paper no longer also reports a bogus title mismatch.
- `tests/test_retraction_real_shapes.py` rebuilds the fixtures from live
  payloads, including the awkward ones (a `"updated-by": null` notice; the
  Lancet record with 7 `updated-by` entries and symmetric `update-to`).

### `--suggest-doi` recommended retracted papers (self-inflicted, round 5)

Round 4's title-strip made a new bug worse than the one it fixed. `_suggest_doi`
never checked retraction status, and `_search_network`'s `select` didn't even
request `updated-by` - so for a DOI-less reference to Wakefield 1998 the tool
emitted *"Crossref has a 100%-confident match - consider citing DOI
10.1016/s0140-6736(97)11096-0"*, at exit 0, with the `RETRACTED:` cue stripped
out of the displayed title. Fixed: the search now requests the retraction
fields, and a retracted match is an error that names the DOI and explicitly
does not recommend it. This turns the flaw into a capability - citecheck can now
catch a retracted source that was cited without any DOI at all.

### Other correctness fixes

- `_retraction_notice` reported the paper's own DOI as its retraction notice.
  Sampling 400 live records, 185 of 186 publisher-deposited `updated-by`
  entries self-reference. Self-references are now skipped; if no entry names a
  different document we say nothing rather than point in a circle.
- `created` removed from `_crossref_years`. It is Crossref's *deposit*
  timestamp: Wakefield 1998 has `created: 2002`, so citing it as "2002" passed.
- `--suggest-doi` could confidently offer a wrong DOI. `title={Editorial}`
  scored 1.00 against thousands of works titled "Editorial". Added two more hard
  rejects (author-surname disagreement; a title under 4 substantial words).
- `OverflowError` crashed the run on standards-valid JSON. `1e400` decodes
  to `float('inf')`; `int(inf)` raises `OverflowError`, which is neither
  `TypeError` nor `ValueError` - and the reachable paths sit *outside* the
  "one weird record must not abort the batch" guards.
- A prose reference list was misrouted to CSV, silently losing references.
  A line containing ", PubMed," normalised to a known PMID column alias. The
  first reference was eaten as a header row and never checked; survivors lost
  author/year/journal *and* the free-text swapped-DOI guard - checked *less* than
  before, at exit 0. `looks_like_csv` now also requires a header that reads as
  column names (a header never *contains* a DOI) and a rectangular shape.
- A text list containing an RIS block discarded the whole list (`_ris_records`
  drops everything before the first `TY`). RIS detection now requires the file
  to *begin* as RIS.
- `@article(key, ...)` entries vanished - valid BibTeX, dropped silently and
  reported as 0 malformed. Now parsed, with parens counted only outside braces so
  `title={Aspirin (low dose)}` doesn't truncate the entry.
- `--cache-ttl 1e308` overflowed to `inf`, making entries immortal and
  defeating the expiry invariant the cache is documented on. Now bounded.
- `socket.timeout` added to the retry tuple - it only aliases `TimeoutError`
  from Python 3.10, and `pyproject.toml` declares 3.9.
- Cache hits were miscounted: "served from cache" was inferred from "made no
  network call", crediting the cache for DOI-less references and claiming hits
  against a cache file that did not exist.
- `--ignore lookup-failed` faked a clean pass. It reported "1 ok, 0 warnings,
  0 errors" and exit 0 on an offline run while the next line said "0 of 1
  compared against a Crossref record". `lookup-failed` is now the one
  non-ignorable code: every other code is a judgement the user is entitled to
  make; that one is a fact about whether the run happened. The exit-3 guard also
  now keys on the code rather than `message.startswith("Lookup failed")` -
  rewording any of four f-strings would silently have turned every offline run
  into exit 0.
- All-caps journal initialisms produced false "Journal mismatch" warnings.
  `_is_abbrev_of` structurally requires >= 2 abbreviation words, so "NEJM",
  "JAMA", "BMJ", "PNAS", "JCO" - the way clinicians actually write these - could
  never match. Found by running a realistic Covidence-style CSV export rather
  than a fixture. Added `_is_initialism_of`, deliberately narrow: the token must
  be all-caps *as written* (the case is the signal), and its letters must equal
  the initials of the candidate's significant words exactly, so "Cancer" still
  correctly mismatches "Cancer Research".

### New capabilities (this is not only a bugfix round)

- Expression of concern / withdrawal / removal (errors) and correction /
  erratum / corrigendum / addendum / clarification / new version (warnings),
  from the same `updated-by` data. The kind list and its severities were taken
  from the live `update-type` facet - what Crossref actually emits, by
  volume - not from the schema docs.
- Preprint -> published version: cited the medRxiv preprint but the
  peer-reviewed paper is out? Its DOI is named. (Results change in review.)
- `--suggest-doi`: find the DOI for a reference that cites none, via
  Crossref's bibliographic index. Deliberately conservative; activates the
  `_search` transport hook that had been dead code since the tool was written.
- `--cache` / `--cache-ttl`: expiring on-disk lookup cache, so re-running
  through a round of revisions is instant. Expiry is a safety property, not a
  convenience one: an immortal cache would eventually report a stale clean pass.
- CSV/TSV input reached the CLI. `parse_csv` existed and auto-detect used it,
  but `--format`'s choices omitted `csv`, so it could not be forced and was
  undocumented - a whole input format (the systematic-reviewer's Excel/Covidence
  table) was half-wired for three rounds.
- Finding codes + `--ignore` + `--list-checks`, and severity-ordered output.
  `--strict` was documented as a submission gate but was unusable as one: every
  real manuscript cites DOI-less books and guidelines, so it failed forever. Codes
  are also emitted in the JSON report and a CSV `codes` column, so CI branches on
  a stable identifier instead of regex-matching English prose.
- Honest `--pubmed` coverage. On a PMID-less file it was a silent no-op with
  byte-identical output; it now says so, and reports partial coverage.
- Honest summary line. "N verified, M could not be verified" excluded hard
  errors from *both* buckets, so the numbers didn't sum to the total.

### Test-quality work

The suite went from 179 to 500 tests, but the point is that they now *bite*:

- Fixtures are checked against the live API, not against the code's
  assumptions - the discipline whose absence caused the headline bug.
- Mutation testing drove the new tests: 13 mutants that survived the old
  suite (dropped `_sanitize` in the JSON/Markdown/CSV paths, the paren/brace
  guard, the `@comment` filter, the update aliases, the notice self-reference
  filter, the suggestion retraction check) are all killed now.
- Two of my own new tests were found to be tautological and rewritten: one
  asserted no raw `\x1b` in stdout, which `json.dumps` escapes to `\u001b`
  either way (a consumer doing `jq -r` decodes it back to a live escape - so the
  assertion had to be on the *parsed value*); another grepped a function's
  source for "updated-by" and passed on the strength of the comment above the
  select. Fixtures that didn't actually exercise their target (`@string` never
  matches the entry regex; *balanced* inner parens cancel out) were replaced.
- The docs are now tested: the README's example block is checked against the
  real `sample.bib` (cite keys, totals, and that the buckets sum), every
  `examples/` path named by any doc must exist, and `update-by` may never
  reappear in the docs.
- `examples/sample.bib` claimed to show a `` that it hadn't shown since PLoS
  Medicine corrected Ioannidis 2005 in 2022 - the double-click demo never
  displayed a verified reference. Added a genuinely clean entry (PRISMA 2009) and
  kept Ioannidis as a live demo of the *new* correction check.

### Verified against live Crossref (not fixtures)

Wakefield 1998, Mehra NEJM 2020 and Mehra Lancet 2020 -> `ERROR` with correct
notice DOIs; the Wakefield retraction notice -> `OK` (still citable); Ioannidis
2005 -> correction warning; PRISMA 2009 -> clean ``; a Springer record whose
`updated-by` self-references -> retraction reported with no circular notice;
`--suggest-doi` on a DOI-less Wakefield citation -> `ERROR`, not a
recommendation. Reviewers independently confirmed the PubMed esummary shapes,
the `_UPDATE_KINDS` volumes, that the `--mailto` address reaches no report,
cache file or error message, that the cache file is written `0600` via an atomic
replace, and that the offline test guard covers `CrossrefClient.search`.

### Known limitations (deliberate, not oversights)

- Crossref/PubMed retraction coverage is genuinely incomplete; a clean result is
  not a guarantee. Retraction Watch's CC0 dataset would close most of the gap but
  costs a ~30 MB data file and a refresh policy, against the tool's
  pure-stdlib/no-data-files design. Not taken.
- Two records Crossref-wide carry the update types `retration`/`retracion`
  (typos), which the `"retract"` substring match misses. Not worth fuzzy-matching.
- 200 references still means 200 serial HTTPS round-trips. `--cache` makes the
  *re-run* instant; the first run is unchanged.

## 2026-07-31 - Excel/Word input, reference-list profile, and one review round

### Added (new capability, with tests)

- `--profile` - descriptive statistics for the reference list as a whole,
  computed from records already fetched (no extra lookup): DOI/PMID coverage,
  publication-year median/IQR/range, median reference age, Price index
  (share published within the last 5 years - the standard answer to "your
  references are out of date"), journal spread, Crossref document types,
  per-reference integrity-flag counts, and `--self-cite Kim,Park` for the
  self-citation share. `--as-of YEAR` pins the age reference year so a run is
  reproducible. Appears in the text report, as a `## Reference profile` section
  in Markdown, as `{"references": ..., "profile": ...}` in JSON (the plain array is
  unchanged without the flag), and on stderr for `--report csv` so a
  redirected table stays clean. Built *before* `--ignore`: hiding a report line
  must not edit what the reference list is made of.
- `.xlsx` and `.docx` input (stdlib `zipfile` + `xml.etree`, no new deps).
  A workbook is converted to a table and parsed by column; a Word file to one
  line per paragraph, footnotes and endnotes included. `--sheet NAME|N` picks a
  worksheet. Untrusted-input caps: expanded size, member count, member size,
  row/column counts, and any part declaring a DOCTYPE is refused (`xml.etree`
  *does* expand internal-subset entities - billion laughs).

### Found by the review round and fixed

*Four independent reviewers (correctness / edge cases / docs honesty / tests
+ security), one round, all findings verified before fixing.*

- PII leak (the serious one). A converted worksheet went through format
  *auto-detection*; a real extraction sheet (banner row, or no DOI/PMID/Title
  column) fails the CSV detector and fell through to the free-text parser, which
  marks rows `heuristic_fields=True` - and `--suggest-doi` then sends the row's
  raw text to `api.crossref.org`. Verified emitting
  `query.bibliographic=S-001,4429981,"subject 88213 relapsed, PHQ9=19",...`. An
  `.xlsx` is now always parsed as a table, and a workbook with no reference
  table is a usage error rather than a run over junk rows.
- Data loss, silent, at exit 0. A banner row above the header destroyed the
  whole column mapping; a cover/patient tab could out-score the real table
  (sheets are now scored on the references they *yield*); a missing
  `sharedStrings.xml` blanked every cell; `MAX_ROWS` truncated a sheet silently;
  Word footnotes/endnotes were never read; a Shift+Enter-separated reference
  list collapsed into one reference, dropping every DOI but the first; a
  `.doc`/`.xls`/PDF decoded as mojibake and was "checked".
- Wrong data, reported as the author's mistake. Duplicate/out-of-order cell
  refs shifted columns; a negative shared-string index returned another cell's
  text; Excel phonetic hints (`rPh`) were appended to CJK titles; Word text-box
  content was emitted twice and welded together into a non-existent DOI reported
  as broken.
- Statistics. Crossref years are now bounded (a mis-deposited `99999999`
  moved the median to 50,001,009) and booleans rejected; journal grouping folds
  punctuation and keys off the *sanitised* name; a non-string `type` no longer
  leaks a Python repr; rounding is half-up so `0.125` prints as `0.13` beside
  its own `1/8`; the profile's duplicate per-status block (which contradicted
  `references[].status` under `--ignore` in the same JSON) is gone.
- Injection. `profile_lines`/`profile_markdown` did not escape `|` or fold
  newlines (`\n` is not a C0 control char), so an ordinary `journal={A | B}`
  field forged a Markdown table row; `OfficeError` messages echoed ZIP member
  names with ANSI escapes intact. Both fixed, both pinned by tests.
- Silent no-ops. `--self-cite ",,"` disabled the profile without a word;
  `--as-of` without `--profile` did nothing silently; `--encoding` was ignored
  for Office input without saying so.
- Docs. The README profile block is now real output (it was hand-aligned and
  omitted a line the code always prints); the Korean 사용법 claimed the
  year-source split appears on screen (it is in the JSON); the journal-grouping
  claim was softened to what the code does (punctuation yes, abbreviations no);
  `--help` now names `.xlsx`/`.docx`; the whole-document `.docx` behaviour, the
  offline "years are all as-cited" caveat, and paragraph-vs-reference counting
  are stated rather than implied.

### Test quality

Reviewer mutation testing killed every mutation of the new boundaries (Price
index `<=5`, older-than-10 `>10`, q1/q3, DOCTYPE guard, sparse-cell padding,
sheet choice, profile-before-ignore). Surviving mutants are now covered:
`--as-of`'s current-year default, `MAX_MEMBER_BYTES`, dedup of `--self-cite`
names (the old assertion was a substring of the buggy output), tab-order
tie-breaking, and sanitisation of Crossref `type`/self-cite names into JSON and
Markdown. Two weak assertions were tightened (`code in (0, 1)`; a CSV width
check that split on commas instead of parsing CSV). Suite: 669 tests, offline.

## 2026-10-01 - 0.2.0: standalone release and a verified review

The standalone repository (github.com/hyeonjoong/citecheck) had stayed at the
0.1.0 seed. This round ports the hardened code above into it and fixes what an
independent review found in both copies. Every finding below was reproduced
before it was fixed and has a regression test. Baseline: 13 tests in the
standalone. After: 888 tests, offline, on Python 3.14 and the stock macOS
Python 3.9.

### Fixed

- One BibTeX entry with an unbalanced brace dropped every entry after it, and
  the run still exited 0. The scanner now resumes at the next line that opens
  an entry, names the skipped keys on stderr, and exits 3 when nothing else is
  an error. Brace entries are matched through one precomputed table and a
  parenthesised entry is never searched past the next entry line, so many bad
  entries stay linear (5,000 in well under 2 s). An entry line needs its key
  and comma (or is an @string, @comment or @preamble), so a field line such as
  "@WHO (World Health Organization) ..." does not end a parenthesised entry.
- `@string` macros were never expanded, so `journal = jsr` was compared as the
  literal "jsr". Macros and `#` concatenation are expanded; an undefined macro
  is left out of the comparison. A bare `doi = doi:10...` or URL is still text.
  An expansion longer than 10,000 characters is treated as undefined: macros
  that double each other would otherwise turn a 617-byte file into an 84 MB
  title (the BibTeX form of an XML entity bomb).
- The doi.org check sent a HEAD to https://doi.org/<doi>, which follows the
  redirect to the publisher. A landing page with a bad certificate made a
  registered DOI inconclusive, and one that answers 404 to HEAD would have read
  as a broken DOI. It now asks the handle API (`/api/handles/<doi>`): response
  code 1 is registered, HTTP 404 or code 100/200 is not, anything else is
  `lookup-failed`.
- A client given only `_fetch` reached doi.org over the network. It is offline
  again, as 0.1.0 documented.
- Korean column headers folded to "" and were ignored, so a Korean table was
  checked on its DOI alone. Hangul is kept in headers and Korean aliases are
  recognised; a table that maps only DOI/PMID names its unused columns. A
  header is looked up by its full key, then its Latin part, then its Hangul
  part, so bilingual headers such as "Title(제목)", "DOI(링크)" and "DOI 번호"
  still map, in CSV files and in .xlsx sheets.
- Hangul titles produced no word tokens, so a correct Korean free-text
  citation of a Korean record was flagged `text-title-missing`. Words of any
  script count now.
- Fields cited in one script against a Crossref record in another (Korean
  against English, or the reverse) are skipped and named in the Verified line
  instead of raising false title, author and journal mismatches.
- Hangul names were compared by fuzzy similarity on decomposed jamo, where a
  one-letter difference still passed. They are compared syllable by syllable,
  and a KCI record that splits a name into family and given matches the full
  name.
- Author cells: "et al.", Korean 외/등/외 2인 markers and semicolon-separated
  lists were read into the surname; LaTeX accents were left as `\"O`; a
  brace-protected corporate author was cut to its last word. All fixed, and
  organisation authors are compared by name.
- JATS/HTML markup, entities and Unicode hyphens in Crossref titles and
  journal names gave false title mismatches and leaked tags into the report.
- A CR-only file (old Excel for Mac CSV) was read as one line.
- `citecheck` with no input on a terminal waited silently; it now prints a
  usage hint and exits 2.
- Docs: a local path, an invented `--suggest-doi` example and a wrong PMID in
  `examples/sample.csv` were removed or corrected; the README suggest block is
  now replayed by a test, and a hygiene test keeps local paths out of the docs.

### Known limitations

- A swapped DOI between a Korean paper and an English-only record is caught
  only by the year, because the other fields cannot be compared across
  scripts. The Verified line says which fields were skipped.
- After an unterminated BibTeX entry, scanning resumes at the next entry that
  starts its own line; an entry that shares a line with the broken one is not
  recovered.
