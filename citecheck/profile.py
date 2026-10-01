"""Reference-list profile: the descriptive statistics a reviewer will ask about.

Checking that each citation is *correct* is only half of what a clinical or
pharma author needs before submission. The other half is what the reference list
looks like as a whole, because these are standard desk-review and peer-review
questions:

* "The references are out of date." — journals and reviewers say this constantly,
  and the accepted quantitative answer is **Price's index**: the share of
  references published within the last 5 years (Price, *Science* 1965/1970).
  Reporting it turns an argument into a number.
* "How many of these are preprints?" — a preprint-heavy reference list is a
  known reviewer objection in clinical work, and Crossref types tell us exactly.
* "Are the references concentrated in a couple of journals?" / "How much
  self-citation is there?" — increasingly asked by editors and integrity checks.
* "How much of this list could actually be verified?" — the honest denominator
  for every other number here.

Everything is computed from records the run has *already* fetched, so a profile
costs no extra network call.

Definitions, stated so a reader can recompute them by hand:

* **Publication year** — Crossref's earliest publication year for the reference
  when a record was retrieved; otherwise the year as cited. Which of the two was
  used is reported per bucket, and references with no year at all are excluded
  from the year statistics and counted separately.
* **Age** — ``as_of_year - publication_year`` (0 for something published this
  year). Online-ahead-of-print references can give a negative age; those are kept
  as-is and counted in the "within 5 years" bucket.
* **Price index** — (number of references with age <= 5) / (number of references
  with a known year). Reported as a proportion, denominator shown.
* **Quartiles** — linear interpolation between order statistics
  (``statistics.quantiles(..., method="inclusive")``; the same convention as R's
  type 7 and numpy's default).
* **Self-citation** — a reference whose author list (Crossref's when available,
  otherwise the cited first author) contains a surname the user named with
  ``--self-cite``, compared diacritic-folded and case-insensitively at >= 0.90
  similarity. The denominator is references for which any author is known.
"""

from __future__ import annotations

import math
import re
import statistics
from collections import Counter
from typing import Optional

from .core import (
    _crossref_all_author_families,
    _crossref_container_candidates,
    _crossref_years,
    _journal_key,
    _similar,
    sanitize_text,
)

# Findings that say something happened to the *literature*, as opposed to
# something being wrong with how the author wrote the citation. These are the
# ones worth counting in a profile.
INTEGRITY_CODES = (
    "retracted",
    "expression-of-concern",
    "withdrawal",
    "removal",
    "correction",
    "addendum",
    "clarification",
    "new-edition",
    "preprint-published",
)

# Price's index horizon, in years. 5 is the standard definition; it is a module
# constant so the docs and the code cannot drift apart.
PRICE_INDEX_YEARS = 5
# The "is this list old?" bucket a desk editor tends to use.
OLD_REFERENCE_YEARS = 10
SELF_CITE_THRESHOLD = 0.90
TOP_JOURNALS = 5


# Plausible publication years. Crossref records are not always sane — a poisoned
# or mis-deposited record carrying `date-parts: [[99999999]]` dragged the median
# to 50,001,009 and the median age to -49,998,983 with a straight face. `--as-of`
# is bounded for exactly this reason (see cli.MIN/MAX_PROFILE_YEAR); the year
# read *out of a record* needs the same guard.
MIN_YEAR = 1400
MAX_YEAR = 2200

# Punctuation that varies freely between reference styles and must not split a
# journal into two: "Lancet", "Lancet.", "N Engl J Med" and "N. Engl. J. Med."
# are one journal for counting purposes.
_JOURNAL_PUNCT_RE = re.compile(r"[^\w\s]+", re.UNICODE)


def _round_half_up(value: float, digits: int) -> float:
    """Round half away from zero, so 0.125 -> 0.13 as a reader expects.

    Python's built-in ``round`` is round-half-to-even, which printed
    ``Price index 0.12 (1/8 …)`` — a number that disagrees with the fraction
    printed next to it.
    """
    factor = 10 ** digits
    scaled = value * factor
    rounded = math.floor(scaled + 0.5) if scaled >= 0 else math.ceil(scaled - 0.5)
    return rounded / factor


def _clean_display(text: str) -> str:
    """Externally-sourced text made safe for every report format.

    Strips control characters *and* collapses all whitespace. The whitespace part
    is not cosmetic: ``\n`` and ``\t`` are not control characters by
    ``CONTROL_CHARS_RE``, and a Crossref ``container-title`` containing a newline
    forged an extra row in the Markdown profile table and an extra line in the
    text profile.
    """
    return " ".join(sanitize_text(str(text)).split())


def _md(text: str) -> str:
    """A value safe to put inside a Markdown table cell (mirrors cli._md_cell)."""
    return _clean_display(text).replace("|", "\\|")


def _pct(n: int, total: int) -> Optional[float]:
    """Percentage, or None when the denominator is 0 (never 0% by accident)."""
    if total <= 0:
        return None
    return _round_half_up(100.0 * n / total, 1)


def _plausible(year) -> bool:
    """Is *year* a publication year rather than a deposit accident?

    `isinstance(year, int)` also rejects ``True`` (a bool IS an int in Python, so
    a record with ``date-parts: [[true]]`` emitted ``"min": true`` into the JSON).
    """
    return isinstance(year, int) and not isinstance(year, bool) and MIN_YEAR <= year <= MAX_YEAR


def _reference_year(result) -> tuple[Optional[int], str]:
    """(year, source) for one result — Crossref's if we have it, else as cited."""
    if isinstance(result.crossref, dict):
        years = [y for y in _crossref_years(result.crossref) if _plausible(y)]
        if years:
            return min(years), "crossref"
    if _plausible(result.reference.year):
        return result.reference.year, "cited"
    return None, "unknown"


def _reference_journal(result) -> Optional[str]:
    """The journal name to group on — Crossref's canonical one when available.

    Preferring Crossref matters for the *statistics*: the same journal written
    "N Engl J Med", "NEJM" and "New England Journal of Medicine" across a hand-
    kept reference table would otherwise count as three journals.
    """
    if isinstance(result.crossref, dict):
        cands = _crossref_container_candidates(result.crossref)
        if cands:
            return cands[0]
    return result.reference.journal or None


def _group_key(name: str) -> str:
    """Grouping key for a journal name: article- and punctuation-insensitive.

    ``core._journal_key`` folds diacritics/case and a leading article, which is
    not enough for counting: a trailing period is standard Vancouver style, so
    "Lancet." and "Lancet" arrived as two distinct journals and inflated the
    `distinct` count on exactly the hand-kept tables this tool targets.

    It does NOT resolve abbreviations — "N Engl J Med" and "New England Journal
    of Medicine" still count separately when neither has a Crossref record to
    canonicalise it. That is stated in the docs rather than papered over.
    """
    return " ".join(_JOURNAL_PUNCT_RE.sub(" ", _journal_key(name)).split())


def _author_families(result) -> list[str]:
    if isinstance(result.crossref, dict):
        families = _crossref_all_author_families(result.crossref)
        if families:
            return families
    return [result.reference.author] if result.reference.author else []


def _five_number(values: list[int]) -> dict:
    """min / Q1 / median / Q3 / max, with the small-n cases handled explicitly."""
    ordered = sorted(values)
    n = len(ordered)
    summary = {
        "min": ordered[0],
        "max": ordered[-1],
        "median": statistics.median(ordered),
        "mean": _round_half_up(statistics.fmean(ordered), 1),
    }
    if n >= 2:
        q1, _q2, q3 = statistics.quantiles(ordered, n=4, method="inclusive")
        summary["q1"], summary["q3"] = _round_half_up(q1, 1), _round_half_up(q3, 1)
    else:
        # A single reference has no spread; saying so beats inventing quartiles.
        summary["q1"] = summary["q3"] = float(ordered[0])
    return summary


def build_profile(results, as_of_year: int, self_cite: tuple = ()) -> dict:
    """Descriptive statistics for a checked reference list.

    *results* are :class:`~citecheck.core.CheckResult` objects (already checked);
    *as_of_year* is the year ages are measured against; *self_cite* is a tuple of
    surnames to count self-citations for.

    Pure and side-effect free — it never touches the network and never mutates a
    result — so a caller can build a profile at any point after checking.
    """
    total = len(results)
    # Deliberately no per-status counts here. The profile is built *before*
    # --ignore is applied while `references[].status` in the JSON report is built
    # after, so the two would contradict each other inside one document; the
    # report already prints the status totals anyway.
    profile: dict = {"references": total, "as_of_year": as_of_year}

    with_doi = sum(1 for r in results if r.reference.doi)
    with_pmid = sum(1 for r in results if r.reference.pmid)
    compared = sum(1 for r in results if isinstance(r.crossref, dict))
    profile["coverage"] = {
        "with_doi": with_doi,
        "with_doi_pct": _pct(with_doi, total),
        "with_pmid": with_pmid,
        "compared_to_crossref": compared,
        "compared_to_crossref_pct": _pct(compared, total),
    }

    # --- years / ages -------------------------------------------------------
    years: list[int] = []
    sources = Counter()
    for r in results:
        year, source = _reference_year(r)
        sources[source] += 1
        if year is not None:
            years.append(year)
    year_block: dict = {
        "n": len(years),
        "unknown": sources["unknown"],
        "source": {"crossref": sources["crossref"], "cited": sources["cited"]},
    }
    if years:
        year_block.update(_five_number(years))
        ages = [as_of_year - y for y in years]
        recent = sum(1 for a in ages if a <= PRICE_INDEX_YEARS)
        old = sum(1 for a in ages if a > OLD_REFERENCE_YEARS)
        year_block["median_age"] = statistics.median(ages)
        year_block["price_index"] = _round_half_up(recent / len(ages), 3)
        year_block["within_5y"] = recent
        year_block["older_than_10y"] = old
        year_block["older_than_10y_pct"] = _pct(old, len(ages))
    profile["years"] = year_block

    # --- journals -----------------------------------------------------------
    # Group on the normalised key (so "The Lancet" and "Lancet" are one journal)
    # but display the spelling seen most often, which is what the reader expects.
    grouped: dict[str, Counter] = {}
    for r in results:
        name = _reference_journal(r)
        if not name:
            continue
        # Cleaned at build time, not at render time: the profile dict is also
        # emitted verbatim as JSON, and a journal name (or a Crossref `type`) is
        # externally supplied text like any other. The *key* is derived from the
        # cleaned name too — keying on the raw one put "Lancet" and
        # "Lan\x01cet" in two groups whose displayed names were identical.
        display = _clean_display(name)
        if not display:
            continue
        grouped.setdefault(_group_key(display), Counter())[display] += 1
    ranked = sorted(
        (
            (counts.most_common(1)[0][0], sum(counts.values()))
            for counts in grouped.values()
        ),
        key=lambda item: (-item[1], item[0].lower()),
    )
    with_journal = sum(n for _name, n in ranked)
    profile["journals"] = {
        "with_journal": with_journal,
        "distinct": len(ranked),
        "top": [[name, n] for name, n in ranked[:TOP_JOURNALS]],
    }

    # --- document types (Crossref's own classification) ---------------------
    # `type` must be a *string* to be reported: a poisoned record holding a list
    # rendered as "['journal-article'] 1" in the report.
    types = Counter(
        _clean_display(r.crossref.get("type")) or "unknown"
        if isinstance(r.crossref.get("type"), str)
        else "unknown"
        for r in results
        if isinstance(r.crossref, dict)
    )
    profile["types"] = [[t, n] for t, n in sorted(types.items(), key=lambda kv: (-kv[1], kv[0]))]

    # --- integrity flags ----------------------------------------------------
    # Counted per *reference*, not per finding: a paper carrying both an erratum
    # and a corrigendum is one corrected reference.
    integrity = {}
    for code in INTEGRITY_CODES:
        n = sum(1 for r in results if any(f.code == code for f in r.findings))
        if n:
            integrity[code] = n
    profile["integrity"] = integrity

    # --- self-citation ------------------------------------------------------
    if self_cite:
        matched = 0
        judged = 0
        for r in results:
            families = _author_families(r)
            if not families:
                continue
            judged += 1
            if any(
                _similar(name, fam) >= SELF_CITE_THRESHOLD
                for fam in families
                for name in self_cite
            ):
                matched += 1
        profile["self_citation"] = {
            "authors": [_clean_display(name) for name in self_cite],
            "matched": matched,
            "judged": judged,
            "pct": _pct(matched, judged),
        }
    return profile


# Width of the label column in the text profile.
_LABEL_WIDTH = 22


def _fmt_pct(value: Optional[float]) -> str:
    return "—" if value is None else f"{value:g}%"


def _row(label: str, value: str) -> str:
    """One aligned profile line. Centralised so a longer label ("older than 10
    years") cannot silently knock its own value one column out of the block."""
    return f"  {label:<{_LABEL_WIDTH}}{value}"


def profile_lines(profile: dict) -> list[str]:
    """The profile as plain lines (shared by the text report and the stderr copy)."""
    total = profile["references"]
    cov = profile["coverage"]
    years = profile["years"]
    journals = profile["journals"]
    lines = [
        f"Reference profile (ages measured against {profile['as_of_year']})",
        _row("references", str(total)),
        _row("with a DOI", f"{cov['with_doi']} ({_fmt_pct(cov['with_doi_pct'])})"),
        _row(
            "compared to Crossref",
            f"{cov['compared_to_crossref']} ({_fmt_pct(cov['compared_to_crossref_pct'])})",
        ),
    ]
    if cov["with_pmid"]:
        lines.append(
            _row(
                "with a PMID",
                f"{cov['with_pmid']} ({_fmt_pct(_pct(cov['with_pmid'], total))}) "
                f"— the denominator for --pubmed",
            )
        )
    if years["n"]:
        lines.append(
            _row(
                "publication year",
                f"median {years['median']:g} (IQR {years['q1']:g}–{years['q3']:g}, "
                f"range {years['min']}–{years['max']})",
            )
        )
        lines.append(
            _row(
                "median age",
                f"{years['median_age']:g} years (n={years['n']}; {years['unknown']} "
                f"reference{'' if years['unknown'] == 1 else 's'} with no year excluded)",
            )
        )
        lines.append(
            _row(
                "Price index",
                f"{_round_half_up(years['price_index'], 2):.2f} "
                f"({years['within_5y']}/{years['n']} published within "
                f"{PRICE_INDEX_YEARS} years)",
            )
        )
        lines.append(
            _row(
                f"older than {OLD_REFERENCE_YEARS} years",
                f"{years['older_than_10y']} ({_fmt_pct(years['older_than_10y_pct'])})",
            )
        )
    else:
        lines.append(_row("publication year", "— (no reference carries a usable year)"))
    if journals["distinct"]:
        top = ", ".join(f"{name} ({n})" for name, n in journals["top"])
        lines.append(
            _row(
                "journals",
                f"{journals['distinct']} distinct across {journals['with_journal']} "
                f"references; top: {top}",
            )
        )
    if profile["types"]:
        shown = ", ".join(f"{t} {n}" for t, n in profile["types"][:TOP_JOURNALS])
        lines.append(_row("Crossref types", shown))
    if profile["integrity"]:
        shown = ", ".join(f"{code} {n}" for code, n in sorted(profile["integrity"].items()))
        lines.append(_row("integrity flags", shown))
    else:
        lines.append(_row("integrity flags", "none found"))
    self_cite = profile.get("self_citation")
    if self_cite:
        lines.append(
            _row(
                "self-citations",
                f"{self_cite['matched']} of {self_cite['judged']} "
                f"({_fmt_pct(self_cite['pct'])}) match {', '.join(self_cite['authors'])}",
            )
        )
    lines.append(
        "  (years are Crossref's where a record was retrieved, otherwise as cited; "
        "the Price index is the share published within the last "
        f"{PRICE_INDEX_YEARS} years)"
    )
    return lines


def profile_markdown(profile: dict) -> str:
    """The profile as a Markdown section, for the shareable report."""
    lines = ["", "## Reference profile", ""]
    lines.append(f"Ages measured against **{profile['as_of_year']}**.")
    lines.append("")
    lines.append("| Metric | Value |")
    lines.append("| ------ | ----- |")
    cov = profile["coverage"]
    years = profile["years"]
    rows = [
        ("References", str(profile["references"])),
        ("With a DOI", f"{cov['with_doi']} ({_fmt_pct(cov['with_doi_pct'])})"),
        (
            "Compared to a Crossref record",
            f"{cov['compared_to_crossref']} ({_fmt_pct(cov['compared_to_crossref_pct'])})",
        ),
    ]
    if years["n"]:
        rows += [
            (
                "Publication year (median, IQR)",
                f"{years['median']:g} ({years['q1']:g}–{years['q3']:g}); "
                f"range {years['min']}–{years['max']}",
            ),
            ("Median age (years)", f"{years['median_age']:g} (n={years['n']})"),
            (
                f"Price index (share <= {PRICE_INDEX_YEARS} years old)",
                f"{_round_half_up(years['price_index'], 2):.2f} "
                f"({years['within_5y']}/{years['n']})",
            ),
            (
                f"Older than {OLD_REFERENCE_YEARS} years",
                f"{years['older_than_10y']} ({_fmt_pct(years['older_than_10y_pct'])})",
            ),
        ]
    else:
        rows.append(("Publication year", "— (no reference carries a usable year)"))
    journals = profile["journals"]
    if journals["distinct"]:
        top = ", ".join(f"{_md(name)} ({n})" for name, n in journals["top"])
        rows.append(("Distinct journals", f"{journals['distinct']} — top: {top}"))
    if profile["types"]:
        rows.append(
            (
                "Crossref types",
                ", ".join(f"{_md(t)} {n}" for t, n in profile["types"][:TOP_JOURNALS]),
            )
        )
    rows.append(
        (
            "Integrity flags",
            ", ".join(f"{_md(c)} {n}" for c, n in sorted(profile["integrity"].items()))
            or "none found",
        )
    )
    self_cite = profile.get("self_citation")
    if self_cite:
        rows.append(
            (
                f"Self-citations ({', '.join(_md(a) for a in self_cite['authors'])})",
                f"{self_cite['matched']} of {self_cite['judged']} "
                f"({_fmt_pct(self_cite['pct'])})",
            )
        )
    for name, value in rows:
        # `name` is a literal from this module; `value` may embed externally
        # sourced text, which each row above has already passed through `_md`.
        lines.append(f"| {name} | {value} |")
    return "\n".join(lines)
