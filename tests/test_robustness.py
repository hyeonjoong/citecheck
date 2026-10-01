"""Regression tests for defects found by adversarial review.

Each test here corresponds to a real, reproduced failure. The common thread is
that almost all of them failed *silently* — wrong format detected, references
dropped, exit 0 — which is the worst way for a verification tool to fail.
"""

import json

import pytest

from citecheck.cli import run
from citecheck.core import CrossrefClient, _crossref_years, _score_candidate, check_reference
from citecheck.parsers import (
    Reference,
    count_malformed_entries,
    detect_format,
    parse_references,
)

GOOD = {
    "DOI": "10.1234/a",
    "title": ["A trial of things in patients"],
    "author": [{"family": "Kim"}],
    "issued": {"date-parts": [[2020]]},
}


def write(tmp_path, name, data):
    p = tmp_path / name
    p.write_text(data, encoding="utf-8") if isinstance(data, str) else p.write_bytes(data)
    return str(p)


# --- OverflowError: `1e400` is valid JSON and decodes to inf -----------------


def test_infinite_year_in_csl_json_does_not_abort_the_run(tmp_path, capsys):
    """`json.loads("1e400")` -> float('inf'); `int(inf)` raises OverflowError,
    which is neither TypeError nor ValueError. This used to be a raw traceback
    on a standards-conformant JSON file."""
    path = write(
        tmp_path,
        "r.json",
        '[{"DOI":"10.1234/a","title":"A trial","issued":{"date-parts":[[1e400]]}}]',
    )
    code = run([path, "--delay", "0", "--no-color"], client=CrossrefClient(
        _fetch=lambda d: GOOD, _resolve=lambda d: True))
    assert code in (0, 1, 3)  # a verdict, not a crash
    refs = parse_references(open(path).read())
    assert refs[0].year is None  # unusable year is dropped, not fatal


def test_infinite_year_in_a_crossref_record_does_not_abort(tmp_path):
    """Reachable outside the "one weird record can't abort the batch" guards:
    a free-text reference returns from _compare early, leaving findings empty,
    so the OK-summary branch calls _crossref_years unguarded."""
    msg = dict(GOOD, issued={"date-parts": [[1e400]]})
    ref = parse_references("Kim H. A trial of things in patients. J Med. doi:10.1234/a")[0]
    res = check_reference(ref, CrossrefClient(_fetch=lambda d: msg, _resolve=lambda d: True))
    assert res.status in ("ok", "warning", "error")


def test_infinite_year_in_a_search_candidate_does_not_abort():
    """Only client.search() was wrapped; the scoring of its results was not."""
    c = CrossrefClient(
        _fetch=lambda d: None,
        _resolve=lambda d: False,
        _search=lambda q: [{"DOI": "10.1/z", "title": ["Cancer risk in adult patients"],
                            "issued": {"date-parts": [[1e400]]}}],
    )
    res = check_reference(
        Reference(raw="", title="Cancer risk in adult patients", year=2020),
        c,
        suggest_missing=True,
    )
    assert res.status == "warning"


@pytest.mark.parametrize("value", [1e400, float("inf"), float("-inf"), float("nan")])
def test_crossref_years_survives_non_finite_dates(value):
    assert _crossref_years({"issued": {"date-parts": [[value]]}}) == set()


# --- plain text misrouted to CSV (silent data loss) -------------------------

PROSE = (
    "Steen RG, Casadevall A. Retraction, PubMed, and the biomedical literature. "
    "PLoS One. 2013. doi:10.1371/journal.pone.0068397\n"
    "Park J, Choi Y. Another study of things. Nature. 2021. doi:10.1038/aaa123\n"
)


def test_prose_reference_list_is_not_misrouted_to_csv():
    """" PubMed" as a comma-cell normalises to a known PMID column alias, which
    used to satisfy looks_like_csv. The first reference was then eaten as a
    header row and never checked, at exit 0."""
    assert detect_format(PROSE) == "text"
    refs = parse_references(PROSE)
    assert len(refs) == 2
    assert refs[0].doi == "10.1371/journal.pone.0068397"
    assert refs[1].doi == "10.1038/aaa123"


def test_misrouting_would_also_have_disabled_the_swapped_doi_guard():
    """The CSV path marks fields structured, which skips the free-text
    "does the line mention the title" check — so the survivor was checked LESS."""
    assert all(r.heuristic_fields for r in parse_references(PROSE))


@pytest.mark.parametrize(
    "line",
    [
        "Kim H. A study of title, DOI, and other things. J Med. 2020;1:2-3.",
        "Lee S, Park J. Titles, abstracts, and PubMed indexing. Nature. 2021.",
        "Choi Y. DOI, PMID, title: a review of identifiers. Cell. 2022.",
    ],
)
def test_comma_rich_prose_first_lines_stay_text(line):
    assert detect_format(line + "\nSecond ref. J Med. 2020. doi:10.1/b\n") == "text"


def test_a_header_containing_an_actual_doi_is_not_a_header():
    """A real header names a DOI column; it never holds a DOI."""
    assert detect_format("doi,title\n10.1/a,A paper\n") == "csv"
    assert detect_format("10.1371/journal.pone.0068397,title,and more prose here\n") != "csv"


def test_real_csv_tables_still_detected():
    assert detect_format("Study ID,Article DOI,Title\nS1,10.1/a,A paper\n") == "csv"
    assert detect_format("id\tdoi\ttitle\nS1\t10.1/a\tA paper\n") == "csv"
    assert detect_format("doi;title\n10.1/a;A paper\n") == "csv"


def test_header_only_csv_still_detected():
    """No data rows can't contradict the header's shape."""
    assert detect_format("Study ID,Article DOI,Title\n") == "csv"


def test_ragged_csv_still_detected():
    """Real exports have trailing-empty/short rows; a majority must agree, not all."""
    table = "id,doi,title\nS1,10.1/a,A paper\nS2,10.1/b,B paper\nS3,10.1/c\n"
    assert detect_format(table) == "csv"


def test_csv_can_still_be_forced_when_detection_declines():
    """The escape hatch: --format csv overrides the (conservative) sniffer."""
    refs = parse_references("doi,title\n10.1/a,A paper\n", fmt="csv")
    assert refs[0].doi == "10.1/a"


# --- an RIS block inside a text list swallowed the whole list ---------------

MIXED_RIS = (
    "Kim H. Study one. J Med. 2020. doi:10.1234/a\n"
    "Park J. Study two. Nature. 2021. doi:10.1234/b\n"
    "Lee S. Study three. Cell. 2022. doi:10.1234/c\n"
    "TY  - JOUR\n"
    "AU  - Someone\n"
    "ER  - \n"
)


def test_text_list_containing_an_ris_block_is_not_parsed_as_ris():
    """_ris_records discards everything before the first TY, so this used to
    check 1 reference instead of 3 — silently, at exit 0."""
    assert detect_format(MIXED_RIS) == "text"
    refs = parse_references(MIXED_RIS)
    assert {"10.1234/a", "10.1234/b", "10.1234/c"} <= {r.doi for r in refs if r.doi}


def test_real_ris_still_detected():
    ris = "TY  - JOUR\nAU  - Kim, H\nTI  - A paper\nDO  - 10.1/a\nER  - \n"
    assert detect_format(ris) == "ris"
    assert parse_references(ris)[0].doi == "10.1/a"


def test_ris_with_leading_blank_lines_still_detected():
    ris = "\n\n  \nTY  - JOUR\nAU  - Kim, H\nTI  - A paper\nDO  - 10.1/a\nER  - \n"
    assert detect_format(ris) == "ris"


# --- parenthesis-delimited BibTeX entries were dropped silently -------------


def test_paren_delimited_entries_are_parsed():
    """`@article(key, ...)` is valid BibTeX (bibtex/biblatex/JabRef accept it).
    It used to vanish AND report 0 malformed, so there was no signal at all."""
    text = (
        "@article{a, title={First}, doi={10.1234/a}}\n"
        "@article(b, title={Second}, doi={10.1234/b})\n"
        "@article{c, title={Third}, doi={10.1234/c}}\n"
    )
    refs = parse_references(text)
    assert [r.key for r in refs] == ["a", "b", "c"]
    assert refs[1].doi == "10.1234/b"


def test_parens_inside_a_field_do_not_close_a_paren_entry():
    """"Aspirin (low dose)" must not truncate the entry at the inner ')'."""
    refs = parse_references("@article(b, title={Aspirin (low dose) in stroke}, doi={10.1234/b})")
    assert refs[0].title == "Aspirin (low dose) in stroke"
    assert refs[0].doi == "10.1234/b"


def test_unterminated_paren_entry_is_counted_as_malformed():
    assert count_malformed_entries("@article(b, title={oops}") == 1


def test_doi_with_parens_still_survives_a_brace_entry():
    """Elsevier DOIs contain balanced parens — the classic regression."""
    refs = parse_references("@article{k, doi={10.1016/S0140-6736(97)11096-0}}")
    assert refs[0].doi == "10.1016/s0140-6736(97)11096-0"


# --- --suggest-doi must not confidently offer a wrong DOI -------------------

EDITORIAL = {
    "DOI": "10.1097/ana.0000000000000611",
    "title": ["Editorial"],
    "author": [{"family": "Smith", "given": "Martin"}],
    "issued": {"date-parts": [[2019]]},
}


def test_a_generic_one_word_title_is_never_matched():
    """`title={Editorial}, author={Smith}, year=2019` scored 1.00 and emitted a
    "100%-confident" DOI — one of thousands of works titled "Editorial"."""
    ref = Reference(raw="", title="Editorial", author="Smith", year=2019)
    assert _score_candidate(ref, EDITORIAL) == 0.0


@pytest.mark.parametrize("title", ["Editorial", "Introduction", "Correction", "Reply", "Obituary Notice"])
def test_short_generic_titles_are_all_rejected(title):
    ref = Reference(raw="", title=title, year=2019)
    assert _score_candidate(ref, dict(EDITORIAL, title=[title])) == 0.0


def test_a_substantial_title_is_still_matched():
    """The guard must not break the feature it protects."""
    rec = dict(EDITORIAL, title=["Sleep as a transdiagnostic node in BELL disorders"])
    ref = Reference(raw="", title="Sleep as a transdiagnostic node in BELL disorders",
                    author="Smith", year=2019)
    assert _score_candidate(ref, rec) >= 0.92


def test_author_disagreement_rejects_an_identical_title():
    rec = dict(EDITORIAL, title=["Sleep as a transdiagnostic node in BELL disorders"])
    ref = Reference(raw="", title="Sleep as a transdiagnostic node in BELL disorders",
                    author="Ioannidis", year=2019)
    assert _score_candidate(ref, rec) == 0.0


def test_author_unknown_on_either_side_is_not_a_reject():
    """"Unknown" must not mean "reject", or every author-less ref loses its hint."""
    rec = dict(EDITORIAL, title=["Sleep as a transdiagnostic node in BELL disorders"])
    ref = Reference(raw="", title="Sleep as a transdiagnostic node in BELL disorders", year=2019)
    assert _score_candidate(ref, rec) > 0
    no_authors = {k: v for k, v in rec.items() if k != "author"}
    ref2 = Reference(raw="", title="Sleep as a transdiagnostic node in BELL disorders",
                     author="Anybody", year=2019)
    assert _score_candidate(ref2, no_authors) > 0


def test_generic_candidate_title_rejected_for_free_text_refs_too():
    ref = Reference(raw="Smith J. Editorial. Anesthesiology. 2019.", heuristic_fields=True)
    assert _score_candidate(ref, EDITORIAL) == 0.0


# --- --cache-ttl overflow made entries immortal -----------------------------


def test_absurd_cache_ttl_is_rejected(tmp_path):
    """1e308 * 86400 == inf, so `age > ttl` was never true and nothing expired —
    defeating the expiry invariant the cache is documented on."""
    with pytest.raises(SystemExit):
        run(["x.bib", "--cache-ttl", "1e308"])


def test_reasonable_cache_ttls_are_accepted():
    from citecheck.cli import build_parser

    for value in ["0", "0.5", "7", "3650"]:
        assert build_parser().parse_args(["x.bib", "--cache-ttl", value]).cache_ttl >= 0


# --- --pubmed must not silently do nothing ----------------------------------


def test_pubmed_on_a_file_with_no_pmids_says_so(tmp_path, capsys):
    """The flag implies PubMed-grade retraction coverage. On a PMID-less file it
    was a silent no-op with byte-identical output."""
    path = write(tmp_path, "r.bib", "@article{k, doi={10.1234/a}}")
    run([path, "--pubmed", "--delay", "0", "--no-color"],
        client=CrossrefClient(_fetch=lambda d: GOOD, _resolve=lambda d: True))
    assert "--pubmed had no effect" in capsys.readouterr().err


def test_pubmed_partial_coverage_is_reported(tmp_path, capsys):
    bib = "@article{a, doi={10.1234/a}, pmid={123}}\n@article{b, doi={10.1234/a}}"
    path = write(tmp_path, "r.bib", bib)
    from citecheck.core import PubMedClient

    run([path, "--pubmed", "--delay", "0", "--no-color"],
        client=CrossrefClient(_fetch=lambda d: GOOD, _resolve=lambda d: True),
        pubmed=PubMedClient(_fetch=lambda p: {"pubtype": ["Journal Article"]}))
    assert "cross-checked 1 of 2" in capsys.readouterr().err


def test_full_pubmed_coverage_is_quiet(tmp_path, capsys):
    path = write(tmp_path, "r.bib", "@article{a, doi={10.1234/a}, pmid={123}}")
    from citecheck.core import PubMedClient

    run([path, "--pubmed", "--delay", "0", "--no-color"],
        client=CrossrefClient(_fetch=lambda d: GOOD, _resolve=lambda d: True),
        pubmed=PubMedClient(_fetch=lambda p: {"pubtype": ["Journal Article"]}))
    err = capsys.readouterr().err
    assert "had no effect" not in err and "cross-checked" not in err


# --- the cache must not take credit for lookups that never happened ---------


def test_no_doi_references_are_not_reported_as_cache_hits(tmp_path, capsys):
    """A DOI-less reference makes no lookup at all. Inferring "cache hit" from
    "made no network call" credited the cache for those — and claimed hits
    against a cache file that did not even exist."""
    path = write(tmp_path, "r.txt", "Kim H. A study with no doi. J Med. 2020.\n"
                                    "Park J. Another with no doi. Nature. 2021.\n")
    cache_path = str(tmp_path / "cache.json")
    import citecheck.cli as cli

    real = cli.CrossrefClient
    cli.CrossrefClient = lambda mailto=None, cache=None: CrossrefClient(
        cache=cache, _fetch=lambda d: None, _resolve=lambda d: False
    )
    try:
        run([path, "--cache", cache_path, "--delay", "0", "--no-color"])
    finally:
        cli.CrossrefClient = real
    out = capsys.readouterr().out
    assert "served from the cache" not in out
    assert not (tmp_path / "cache.json").exists()  # nothing to cache, nothing written


def test_cache_hits_are_counted_per_lookup_not_per_reference(tmp_path):
    """A single reference can cost two lookups (fetch + resolve); the count
    reports lookups, which is what the cache actually served."""
    from citecheck.core import DiskCache

    c = DiskCache(tmp_path / "c.json", _now=lambda: 1000.0)
    client = CrossrefClient(cache=c, _fetch=lambda d: None, _resolve=lambda d: True)
    client.fetch("10.1/x")
    client.resolve("10.1/x")
    assert c.hits == 0  # first run: everything was a miss
    c.save()

    c2 = DiskCache(tmp_path / "c.json", _now=lambda: 1000.0)
    client2 = CrossrefClient(cache=c2, _fetch=lambda d: None, _resolve=lambda d: True)
    client2.fetch("10.1/x")
    client2.resolve("10.1/x")
    assert c2.hits == 2


# --- all-caps journal initialisms must not warn -----------------------------
#
# "NEJM" is how clinical authors write the New England Journal of Medicine, and
# it produced a false "Journal mismatch" on every reference that used it:
# _is_abbrev_of requires >= 2 abbreviation words, so a single token could never
# match. Surfaced on a realistic Covidence-style CSV export.

from citecheck.core import _is_initialism_of, _journal_matches  # noqa: E402


@pytest.mark.parametrize(
    "cited, full",
    [
        ("NEJM", "New England Journal of Medicine"),
        ("JAMA", "Journal of the American Medical Association"),
        ("BMJ", "British Medical Journal"),
        ("PNAS", "Proceedings of the National Academy of Sciences"),
        ("JCO", "Journal of Clinical Oncology"),
        ("AJRCCM", "American Journal of Respiratory and Critical Care Medicine"),
        ("JACC", "Journal of the American College of Cardiology"),
        ("N.E.J.M.", "New England Journal of Medicine"),
    ],
)
def test_real_medical_initialisms_match(cited, full):
    assert _is_initialism_of(cited, full) is True
    assert _journal_matches(cited, [full], 0.82) is True


@pytest.mark.parametrize(
    "cited, full",
    [
        ("NEJM", "The Lancet"),
        ("JAMA", "Journal of Clinical Oncology"),
        ("BMJ", "Boston Medical Journal of Cardiology"),  # right letters, wrong words
        ("JCO", "Journal of Cardiology"),  # too few words to spell JCO
    ],
)
def test_initialism_does_not_match_a_different_journal(cited, full):
    assert _is_initialism_of(cited, full) is False


@pytest.mark.parametrize(
    "cited, full",
    [
        # Case is the signal that an initialism was meant. A normal word must not
        # be reinterpreted as one, or "Cancer" would match anything C-something.
        ("Cancer", "Cancer Nursing"),
        ("Nejm", "New England Journal of Medicine"),
        ("nejm", "New England Journal of Medicine"),
        # A single letter is not evidence of anything.
        ("J", "Journal of Medicine"),
        # Non-alphabetic tokens are not initialisms.
        ("NEJM2", "New England Journal of Medicine"),
        ("", "New England Journal of Medicine"),
        # A one-word journal has no initialism.
        ("LA", "Lancet"),
    ],
)
def test_initialism_rule_stays_narrow(cited, full):
    assert _is_initialism_of(cited, full) is False


def test_cancer_still_mismatches_cancer_research():
    """The documented reason single-token expansion is not attempted."""
    assert _journal_matches("Cancer", ["Cancer Research"], 0.82) is False


def test_a_genuinely_wrong_journal_still_warns(tmp_path, capsys):
    """The whole point of the journal check is a second swapped-DOI signal —
    loosening it must not disarm it."""
    record = dict(GOOD, **{"container-title": ["The Lancet"]})
    path = write(tmp_path, "r.bib", "@article{k, doi={10.1234/a}, journal={NEJM}}")
    run([path, "--json", "--delay", "0"],
        client=CrossrefClient(_fetch=lambda d: record, _resolve=lambda d: True))
    payload = json.loads(capsys.readouterr().out)
    assert any(f["code"] == "journal-mismatch" for f in payload[0]["findings"])
