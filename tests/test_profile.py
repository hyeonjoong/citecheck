"""Tests for the reference-list profile (--profile).

The statistics are recomputed by hand here (medians, quartiles, Price index)
rather than compared against whatever the code happens to produce — a profile
that quietly reports the wrong median age would be worse than no profile.
"""

import csv
import io
import json
import statistics

import pytest

from citecheck.cli import run
from citecheck.core import CheckResult, WARNING
from citecheck.parsers import Reference
from citecheck.profile import (
    OLD_REFERENCE_YEARS,
    PRICE_INDEX_YEARS,
    build_profile,
    profile_lines,
    profile_markdown,
)


def make_result(
    doi=None, year=None, cr_year=None, journal=None, cr_journal=None,
    authors=(), cited_author=None, cr_type=None, pmid=None, findings=(),
):
    """A CheckResult as the checker would have produced it."""
    ref = Reference(
        raw="raw", doi=doi, year=year, journal=journal, author=cited_author, pmid=pmid
    )
    crossref = None
    if cr_year is not None or cr_journal or authors or cr_type:
        crossref = {}
        if cr_year is not None:
            crossref["issued"] = {"date-parts": [[cr_year]]}
        if cr_journal:
            crossref["container-title"] = [cr_journal]
        if authors:
            crossref["author"] = [{"family": a} for a in authors]
        if cr_type:
            crossref["type"] = cr_type
    result = CheckResult(reference=ref, crossref=crossref)
    for severity, code in findings:
        result.add(severity, f"message for {code}", code)
    return result


def test_year_statistics_match_a_hand_computation():
    years = [2001, 2010, 2015, 2019, 2020, 2021, 2024, 2025]
    results = [make_result(doi=f"10.1/{y}", cr_year=y) for y in years]

    profile = build_profile(results, as_of_year=2026)
    block = profile["years"]

    assert block["n"] == len(years)
    assert block["min"] == 2001 and block["max"] == 2025
    assert block["median"] == statistics.median(years)  # (2019+2020)/2 = 2019.5
    assert block["median"] == 2019.5
    q1, _q2, q3 = statistics.quantiles(sorted(years), n=4, method="inclusive")
    assert block["q1"] == round(q1, 1) and block["q3"] == round(q3, 1)

    ages = [2026 - y for y in years]  # [25, 16, 11, 7, 6, 5, 2, 1]
    assert block["median_age"] == statistics.median(ages)  # (7+6)/2 = 6.5
    assert block["median_age"] == 6.5
    # Price index: age <= 5 -> 2021, 2024, 2025 = 3 of 8
    assert block["price_index"] == pytest.approx(3 / 8)
    assert block["within_5y"] == 3
    # older than 10 years: ages 25, 16, 11 -> 3 of 8
    assert block["older_than_10y"] == 3
    assert block["older_than_10y_pct"] == pytest.approx(37.5)


def test_price_index_boundary_is_inclusive_of_exactly_five_years():
    """age == PRICE_INDEX_YEARS counts as recent; age == +1 does not."""
    as_of = 2026
    recent = make_result(doi="10.1/a", cr_year=as_of - PRICE_INDEX_YEARS)
    older = make_result(doi="10.1/b", cr_year=as_of - PRICE_INDEX_YEARS - 1)
    profile = build_profile([recent, older], as_of_year=as_of)
    assert profile["years"]["within_5y"] == 1
    assert profile["years"]["price_index"] == pytest.approx(0.5)


def test_old_bucket_boundary_is_strictly_greater_than_ten():
    as_of = 2026
    exactly_ten = make_result(doi="10.1/a", cr_year=as_of - OLD_REFERENCE_YEARS)
    eleven = make_result(doi="10.1/b", cr_year=as_of - OLD_REFERENCE_YEARS - 1)
    profile = build_profile([exactly_ten, eleven], as_of_year=as_of)
    assert profile["years"]["older_than_10y"] == 1


def test_crossref_year_wins_over_the_cited_year_and_sources_are_reported():
    """The whole point of the tool is that the citation may be wrong."""
    results = [
        make_result(doi="10.1/a", year=1998, cr_year=2005),  # Crossref wins
        make_result(year=2011),  # no record -> as cited
        make_result(),  # no year at all
    ]
    profile = build_profile(results, as_of_year=2026)
    block = profile["years"]
    assert block["n"] == 2
    assert sorted([block["min"], block["max"]]) == [2005, 2011]
    assert block["source"] == {"crossref": 1, "cited": 1}
    assert block["unknown"] == 1


def test_empty_year_block_does_not_invent_statistics():
    profile = build_profile([make_result(doi="10.1/a")], as_of_year=2026)
    block = profile["years"]
    assert block["n"] == 0
    assert "median" not in block and "price_index" not in block
    assert "no reference carries a usable year" in "\n".join(profile_lines(profile))


def test_single_reference_reports_no_spread_rather_than_crashing():
    profile = build_profile([make_result(doi="10.1/a", cr_year=2020)], as_of_year=2026)
    block = profile["years"]
    assert block["median"] == block["q1"] == block["q3"] == 2020
    assert block["median_age"] == 6


def test_journals_group_on_the_normalised_name_and_prefer_crossref():
    results = [
        make_result(doi="10.1/a", journal="Lancet", cr_journal="The Lancet"),
        make_result(doi="10.1/b", journal="the lancet", cr_journal="The Lancet"),
        make_result(journal="The Lancet"),  # no record: the cited name is used
        make_result(doi="10.1/c", cr_journal="BMJ"),
    ]
    profile = build_profile(results, as_of_year=2026)
    assert profile["journals"]["distinct"] == 2
    assert profile["journals"]["with_journal"] == 4
    assert profile["journals"]["top"][0] == ["The Lancet", 3]


def test_types_and_integrity_counts_are_per_reference():
    results = [
        make_result(doi="10.1/a", cr_type="journal-article",
                    findings=[(WARNING, "correction"), (WARNING, "correction")]),
        make_result(doi="10.1/b", cr_type="posted-content",
                    findings=[(WARNING, "preprint-published")]),
        make_result(doi="10.1/c", cr_type="journal-article"),
    ]
    profile = build_profile(results, as_of_year=2026)
    assert profile["types"] == [["journal-article", 2], ["posted-content", 1]]
    # Two `correction` findings on one reference is one corrected reference.
    assert profile["integrity"] == {"correction": 1, "preprint-published": 1}


def test_coverage_counts_and_percentages():
    results = [
        make_result(doi="10.1/a", cr_year=2020),
        make_result(doi="10.1/b", pmid="123"),
        make_result(),
        make_result(),
    ]
    profile = build_profile(results, as_of_year=2026)
    cov = profile["coverage"]
    assert cov["with_doi"] == 2 and cov["with_doi_pct"] == 50.0
    assert cov["with_pmid"] == 1
    assert cov["compared_to_crossref"] == 1 and cov["compared_to_crossref_pct"] == 25.0


def test_empty_result_list_does_not_divide_by_zero():
    profile = build_profile([], as_of_year=2026)
    assert profile["references"] == 0
    assert profile["coverage"]["with_doi_pct"] is None
    assert profile["years"]["n"] == 0
    # And it still renders.
    assert profile_lines(profile) and profile_markdown(profile)


def test_self_citation_counts_only_references_with_a_known_author():
    results = [
        make_result(doi="10.1/a", authors=["Kim", "Lee"]),
        make_result(doi="10.1/b", authors=["Park"]),
        make_result(cited_author="Kim"),  # no record — the cited author is used
        make_result(doi="10.1/d"),  # no author anywhere: not judged
    ]
    profile = build_profile(results, as_of_year=2026, self_cite=("Kim",))
    assert profile["self_citation"] == {
        "authors": ["Kim"], "matched": 2, "judged": 3, "pct": pytest.approx(66.7)
    }


def test_self_citation_tolerates_diacritics_but_not_a_different_name():
    results = [
        make_result(doi="10.1/a", authors=["Müller"]),
        make_result(doi="10.1/b", authors=["Mahler"]),
    ]
    profile = build_profile(results, as_of_year=2026, self_cite=("Muller",))
    assert profile["self_citation"]["matched"] == 1


def test_self_citation_absent_when_not_requested():
    profile = build_profile([make_result(doi="10.1/a")], as_of_year=2026)
    assert "self_citation" not in profile


def test_profile_never_mutates_the_results_it_reads():
    results = [make_result(doi="10.1/a", cr_year=2020, findings=[(WARNING, "correction")])]
    before = [(r.status, len(r.findings), r.crossref) for r in results]
    build_profile(results, as_of_year=2026, self_cite=("Kim",))
    assert [(r.status, len(r.findings), r.crossref) for r in results] == before


def test_control_characters_in_a_journal_name_are_stripped():
    results = [make_result(doi="10.1/a", cr_journal="Lan\x1b[31mcet")]
    profile = build_profile(results, as_of_year=2026)
    assert "\x1b" not in profile["journals"]["top"][0][0]
    assert "\x1b" not in "\n".join(profile_lines(profile))
    assert "\x1b" not in profile_markdown(profile)


# --- CLI wiring --------------------------------------------------------------


class FakeClient:
    """A Crossref client whose records are supplied by the test."""

    remote_calls = 0

    def __init__(self, records):
        self.records = records

    def fetch(self, doi):
        return self.records.get(doi.lower())

    def resolve(self, doi):
        return False


RECORDS = {
    "10.1/a": {
        "DOI": "10.1/a",
        "title": ["Aspirin in acute coronary syndrome: a randomised trial"],
        "issued": {"date-parts": [[2024]]},
        "container-title": ["The Lancet"],
        "author": [{"family": "Kim"}],
        "type": "journal-article",
    },
    "10.1/b": {
        "DOI": "10.1/b",
        "title": ["Statins and muscle symptoms in older adults"],
        "issued": {"date-parts": [[2004]]},
        "container-title": ["BMJ"],
        "author": [{"family": "Park"}],
        "type": "journal-article",
    },
}

BIB = """
@article{a, title={Aspirin in acute coronary syndrome: a randomised trial},
  author={Kim, H}, year={2024}, journal={Lancet}, doi={10.1/a}}
@article{b, title={Statins and muscle symptoms in older adults},
  author={Park, J}, year={2004}, journal={BMJ}, doi={10.1/b}}
"""


def write_bib(tmp_path):
    path = tmp_path / "refs.bib"
    path.write_text(BIB, encoding="utf-8")
    return str(path)


def test_cli_profile_text_report(tmp_path, capsys):
    code = run(
        [write_bib(tmp_path), "--profile", "--as-of", "2026", "--delay", "0"],
        client=FakeClient(RECORDS),
    )
    out = capsys.readouterr().out
    assert code == 0
    assert "Reference profile (ages measured against 2026)" in out
    assert "Price index           0.50" in out
    assert "median 2014" in out  # (2004 + 2024) / 2
    assert "The Lancet (1)" in out and "BMJ (1)" in out


def test_cli_profile_json_wraps_only_when_asked(tmp_path, capsys):
    path = write_bib(tmp_path)
    run([path, "--json", "--delay", "0"], client=FakeClient(RECORDS))
    plain = json.loads(capsys.readouterr().out)
    assert isinstance(plain, list), "the historical JSON shape must not change"

    run([path, "--json", "--profile", "--as-of", "2026", "--delay", "0"],
        client=FakeClient(RECORDS))
    wrapped = json.loads(capsys.readouterr().out)
    assert set(wrapped) == {"references", "profile"}
    assert wrapped["references"] == plain
    assert wrapped["profile"]["years"]["median"] == 2014
    assert wrapped["profile"]["as_of_year"] == 2026


def test_cli_profile_csv_keeps_stdout_a_clean_table(tmp_path, capsys):
    run([write_bib(tmp_path), "--report", "csv", "--profile", "--as-of", "2026",
         "--delay", "0"], client=FakeClient(RECORDS))
    captured = capsys.readouterr()
    assert "Reference profile" not in captured.out
    assert "Reference profile" in captured.err
    # Every stdout line is still a well-formed CSV row of the same width
    # (parsed as CSV — splitting on "," would miscount any quoted cell).
    widths = {len(row) for row in csv.reader(io.StringIO(captured.out)) if row}
    assert widths == {7}


def test_cli_profile_markdown_appends_a_section(tmp_path, capsys):
    run([write_bib(tmp_path), "--report", "markdown", "--profile", "--as-of", "2026",
         "--delay", "0"], client=FakeClient(RECORDS))
    out = capsys.readouterr().out
    assert "## Reference profile" in out
    assert "| Price index (share <= 5 years old) | 0.50 (1/2) |" in out


def test_cli_self_cite_implies_profile(tmp_path, capsys):
    run([write_bib(tmp_path), "--self-cite", "Kim", "--as-of", "2026", "--delay", "0"],
        client=FakeClient(RECORDS))
    out = capsys.readouterr().out
    assert "Reference profile" in out
    assert "self-citations        1 of 2 (50%) match Kim" in out


def test_cli_self_cite_accepts_several_names(tmp_path, capsys):
    run([write_bib(tmp_path), "--self-cite", "Kim, Park;Kim", "--as-of", "2026",
         "--delay", "0"], client=FakeClient(RECORDS))
    out = capsys.readouterr().out
    # Deduplicated and order-preserving: asserting the exact rendered list, not a
    # prefix of it — "Kim, Park, Kim" contains "Kim, Park".
    assert "self-citations        2 of 2 (100%) match Kim, Park\n" in out


def test_cli_profile_ignores_do_not_edit_the_statistics(tmp_path, capsys):
    """--ignore hides report lines; it must not rewrite what the list is made of."""
    records = dict(RECORDS)
    records["10.1/b"] = {
        **RECORDS["10.1/b"],
        "updated-by": [{"type": "correction", "DOI": "10.1/notice"}],
    }
    run([write_bib(tmp_path), "--profile", "--as-of", "2026", "--ignore", "correction",
         "--delay", "0"], client=FakeClient(records))
    out = capsys.readouterr().out
    assert "correction 1" in out  # still counted in the profile
    assert "hidden by --ignore" in out  # but hidden from the per-reference report


def test_cli_as_of_rejects_a_nonsense_year(tmp_path, capsys):
    with pytest.raises(SystemExit) as exc:
        run([write_bib(tmp_path), "--profile", "--as-of", "20255"],
            client=FakeClient(RECORDS))
    assert exc.value.code == 2
    assert "between 1600 and 2200" in capsys.readouterr().err


def test_cli_without_profile_prints_nothing_extra(tmp_path, capsys):
    run([write_bib(tmp_path), "--delay", "0"], client=FakeClient(RECORDS))
    assert "Reference profile" not in capsys.readouterr().out


# --- regression tests for the 2026-07-31 review round -------------------------


def test_implausible_crossref_years_are_ignored_rather_than_averaged_in():
    """A mis-deposited `date-parts: [[99999999]]` dragged the median to 50 million."""
    results = [
        make_result(doi="10.1/a", cr_year=2020),
        make_result(doi="10.1/b", cr_year=99999999, year=2018),
        make_result(doi="10.1/c", cr_year=0),
    ]
    profile = build_profile(results, as_of_year=2026)
    block = profile["years"]
    assert block["n"] == 2  # the bogus record falls back to its cited year
    assert [block["min"], block["max"]] == [2018, 2020]
    assert block["source"] == {"crossref": 1, "cited": 1}
    assert block["unknown"] == 1


def test_a_boolean_year_is_not_treated_as_an_integer():
    """`isinstance(True, int)` is True in Python — a record with date-parts
    [[true]] used to emit `"min": true` into the JSON."""
    results = [make_result(doi="10.1/a", cr_year=True)]
    assert build_profile(results, as_of_year=2026)["years"]["n"] == 0


def test_rounding_is_half_up_so_the_printed_numbers_agree():
    results = [make_result(doi=f"10.1/{i}", cr_year=2026 - (0 if i == 0 else 20))
               for i in range(8)]
    profile = build_profile(results, as_of_year=2026)
    # 1/8 = 0.125 -> 0.13, not Python's round-half-even 0.12
    assert profile["years"]["price_index"] == 0.125
    assert "Price index           0.13 (1/8" in "\n".join(profile_lines(profile))
    assert profile["coverage"]["with_doi_pct"] == 100.0


def test_journal_grouping_folds_trailing_punctuation():
    """A trailing period is standard Vancouver style; it used to make a second
    journal and inflate `distinct`."""
    results = [
        make_result(journal="Lancet"),
        make_result(journal="Lancet."),
        make_result(journal="The Lancet,"),
        make_result(journal="N. Engl. J. Med."),
        make_result(journal="N Engl J Med"),
    ]
    profile = build_profile(results, as_of_year=2026)
    assert profile["journals"]["distinct"] == 2
    assert profile["journals"]["top"][0] == ["Lancet", 3]


def test_a_control_character_does_not_split_a_journal_into_two_groups():
    results = [make_result(cr_journal="Lancet"), make_result(cr_journal="Lan\x01cet")]
    profile = build_profile(results, as_of_year=2026)
    assert profile["journals"]["distinct"] == 1
    assert profile["journals"]["top"] == [["Lancet", 2]]


def test_newlines_and_pipes_cannot_forge_a_row_in_any_report():
    results = [
        make_result(cr_journal="Lancet | 999 | Integrity flags | none\nEVIL"),
        make_result(doi="10.1/b", cr_type="journal-article\n| forged |"),
    ]
    profile = build_profile(results, as_of_year=2026, self_cite=("Kim|Park",))
    text = "\n".join(profile_lines(profile))
    assert "EVIL" in text and "\nEVIL" not in text  # folded onto its own row's line
    markdown = profile_markdown(profile)
    for line in markdown.splitlines():
        if line.startswith("|"):
            assert line.count("|") - line.count("\\|") == 3, line


def test_a_non_string_crossref_type_does_not_leak_a_python_repr():
    results = [make_result(doi="10.1/a", cr_type=["journal-article"])]
    profile = build_profile(results, as_of_year=2026)
    assert profile["types"] == [["unknown", 1]]


def test_profile_does_not_duplicate_the_per_reference_status():
    """It is built before --ignore while references[].status is built after, so
    a status block here would contradict its sibling inside one JSON document."""
    profile = build_profile([make_result(doi="10.1/a")], as_of_year=2026)
    assert "status" not in profile


def test_pmid_coverage_is_shown_when_any_reference_carries_one():
    with_pmid = build_profile(
        [make_result(doi="10.1/a", pmid="123"), make_result(doi="10.1/b")], as_of_year=2026
    )
    assert "with a PMID           1 (50%)" in "\n".join(profile_lines(with_pmid))
    without = build_profile([make_result(doi="10.1/a")], as_of_year=2026)
    assert "with a PMID" not in "\n".join(profile_lines(without))


def test_cli_as_of_defaults_to_the_current_year(tmp_path, capsys, monkeypatch):
    import time as time_module

    import citecheck.cli as cli_module

    monkeypatch.setattr(
        cli_module.time, "localtime", lambda *a: time_module.struct_time(
            (2031, 1, 1, 0, 0, 0, 0, 1, 0)
        )
    )
    run([write_bib(tmp_path), "--profile", "--delay", "0"], client=FakeClient(RECORDS))
    out = capsys.readouterr().out
    assert "ages measured against 2031" in out
    assert "median age            17 years" in out  # 2031 - median(2004, 2024)


def test_cli_as_of_without_profile_says_it_did_nothing(tmp_path, capsys):
    run([write_bib(tmp_path), "--as-of", "2026", "--delay", "0"], client=FakeClient(RECORDS))
    captured = capsys.readouterr()
    assert "--as-of has no effect without --profile" in captured.err
    assert "Reference profile" not in captured.out


def test_cli_self_cite_with_no_surname_is_a_usage_error(tmp_path, capsys):
    code = run([write_bib(tmp_path), "--self-cite", " , ,", "--delay", "0"],
               client=FakeClient(RECORDS))
    assert code == 2
    assert "lists no surname" in capsys.readouterr().err


def test_cli_json_profile_carries_no_control_characters(tmp_path, capsys):
    records = {
        "10.1/a": {**RECORDS["10.1/a"], "container-title": ["Lan\x1b[31mcet"],
                   "type": "journal-\x07article"},
        "10.1/b": RECORDS["10.1/b"],
    }
    run([write_bib(tmp_path), "--json", "--profile", "--as-of", "2026", "--delay", "0"],
        client=FakeClient(records))
    raw = capsys.readouterr().out
    assert "\x1b" not in raw and "\x07" not in raw
    payload = json.loads(raw)
    names = [name for name, _n in payload["profile"]["journals"]["top"]]
    assert sorted(names) == ["BMJ", "Lan[31mcet"]
    assert [t for t, _n in payload["profile"]["types"]] == ["journal-article"]
