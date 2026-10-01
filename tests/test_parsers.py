import pytest

from citecheck.parsers import find_doi, find_year, parse_bibtex, parse_references, parse_text

BIB = r"""
@article{kim2024sleep,
  title   = {Sleep as a transdiagnostic node in {BELL} disorders},
  author  = {Kim, Gildong J. and Lee, Sara and Park, Min},
  journal = {PLOS ONE},
  year    = {2024},
  doi     = {10.1371/journal.pone.0312345},
}

@book{strunk1999,
  title = {The Elements of Style},
  author = {Strunk, William and White, E. B.},
  year = {1999}
}
"""


def test_find_doi_strips_trailing_punctuation():
    assert find_doi("see 10.1371/journal.pone.0312345.") == "10.1371/journal.pone.0312345"
    assert find_doi("(doi: 10.1000/xyz123)") == "10.1000/xyz123"
    assert find_doi("no doi here") is None


def test_find_year():
    assert find_year("Published in 2024 somewhere") == 2024
    assert find_year("no year") is None


def test_parse_bibtex_extracts_fields():
    refs = parse_bibtex(BIB)
    assert len(refs) == 2
    first = refs[0]
    assert first.key == "kim2024sleep"
    assert first.doi == "10.1371/journal.pone.0312345"
    assert first.year == 2024
    assert first.author == "Kim"
    assert "transdiagnostic node" in first.title
    # Braces inside the title must be stripped.
    assert "{" not in first.title and "}" not in first.title


def test_parse_bibtex_author_without_doi():
    refs = parse_bibtex(BIB)
    book = refs[1]
    assert book.doi is None
    assert book.author == "Strunk"


def test_auto_detect_text():
    text = "Kim H, Lee S. A study. PLOS ONE. 2024. doi:10.1371/journal.pone.0312345"
    refs = parse_references(text, fmt="auto")
    assert len(refs) == 1
    assert refs[0].doi == "10.1371/journal.pone.0312345"
    assert refs[0].author == "Kim"
    assert refs[0].year == 2024


def test_parse_text_paragraphs():
    text = "Ref one 10.1000/a\n\nRef two 10.1000/b"
    refs = parse_text(text)
    assert [r.doi for r in refs] == ["10.1000/a", "10.1000/b"]


# --- DOI normalisation edge cases -------------------------------------------

def test_bibtex_doi_as_url():
    for val in (
        "https://doi.org/10.1371/journal.pone.0312345",
        "http://dx.doi.org/10.1371/journal.pone.0312345",
        "doi:10.1371/journal.pone.0312345",
        "DOI: 10.1371/journal.pone.0312345",
    ):
        refs = parse_bibtex("@article{k, doi={%s}}" % val)
        assert refs[0].doi == "10.1371/journal.pone.0312345", val


def test_doi_with_parentheses_preserved():
    # Elsevier/Lancet-style DOI containing balanced parentheses.
    assert find_doi("see 10.1016/S0140-6736(97)11096-0.") == "10.1016/s0140-6736(97)11096-0"


def test_doi_wrapping_paren_stripped():
    assert find_doi("(doi: 10.1000/xyz123)") == "10.1000/xyz123"


def test_long_registrant_code_doi():
    assert find_doi("10.1234567890/abc") == "10.1234567890/abc"


# --- @string / @comment must not swallow the next entry ---------------------

def test_string_macro_does_not_swallow_next_entry():
    bib = '@string{pone = "PLOS ONE"}\n\n@article{real2024, title={A real paper}, doi={10.1/x}, year={2024}}'
    refs = parse_bibtex(bib)
    assert [r.key for r in refs] == ["real2024"]
    assert refs[0].doi == "10.1/x"


def test_comment_entry_skipped():
    bib = "@comment{this is a note, ignore}\n@article{a, title={T}, doi={10.1/y}}"
    refs = parse_bibtex(bib)
    assert [r.key for r in refs] == ["a"]


# --- unterminated braces are bounded, do not consume following entries ------

def test_unterminated_entry_does_not_eat_next():
    bib = "@article{good, title={fine}, doi={10.1000/abc}}\n@article{bad, title={oops"
    refs = parse_bibtex(bib)
    keys = [r.key for r in refs]
    assert "good" in keys  # good must survive


def test_count_malformed_entries():
    from citecheck.parsers import count_malformed_entries
    good = "@article{a, title={ok}, doi={10.1000/x}}\n@book{b, title={ok}}"
    assert count_malformed_entries(good) == 0
    bad = "@article{a, doi={10.1000/x}}\n@article{b, title={oops"
    assert count_malformed_entries(bad) == 1


def test_nested_at_entry_in_field_value_not_dropped():
    # A field value that literally contains "@type{key," must not steal the entry.
    bib = "@article{real, title={On parsing @inproceedings{x, y} entries}, doi={10.1000/REAL}}"
    refs = parse_bibtex(bib)
    assert [r.key for r in refs] == ["real"]
    assert refs[0].doi == "10.1000/real"


# --- auto-detect must not misfire on emails ---------------------------------

def test_auto_detect_email_is_text_not_bibtex():
    text = "Smith J (smith@uni.edu). A study {n=5}. 2020. doi:10.1000/abc123"
    refs = parse_references(text, fmt="auto")
    assert len(refs) == 1
    assert refs[0].doi == "10.1000/abc123"


def test_auto_detect_real_bibtex():
    refs = parse_references("@article{k, title={T}, doi={10.1/z}}", fmt="auto")
    assert refs[0].key == "k"


# --- parse_text splitting ---------------------------------------------------

def test_text_multiline_single_ref_with_whitespace_blankline():
    # Blank line contains a space; both refs must stay intact (not shattered).
    text = "Smith J. A long title\nthat wraps. 10.1000/a\n \nJones K. Another. 10.1000/b"
    refs = parse_text(text)
    assert [r.doi for r in refs] == ["10.1000/a", "10.1000/b"]


def test_text_lines_without_blank_split_per_line():
    text = "Ref one 10.1000/a\nRef two 10.1000/b\nRef three 10.1000/c"
    refs = parse_text(text)
    assert [r.doi for r in refs] == ["10.1000/a", "10.1000/b", "10.1000/c"]


# --- first-author surname branches ------------------------------------------

def test_first_author_no_comma_form():
    from citecheck.parsers import _first_author_surname
    assert _first_author_surname("John von Neumann and Alan Turing") == "Neumann"


# --- normalize_doi_field (direct) -------------------------------------------

def test_normalize_doi_field_variants():
    from citecheck.parsers import normalize_doi_field
    assert normalize_doi_field("10.1/x") == "10.1/x"  # short registrant ok in field
    assert normalize_doi_field("https://doi.org/10.1371/journal.pone.1?utm=x") == "10.1371/journal.pone.1"
    assert normalize_doi_field("doi:10.1000/ABC") == "10.1000/abc"
    assert normalize_doi_field("") is None
    assert normalize_doi_field("not a doi at all") is None


def test_doi_query_string_stripped():
    from citecheck.parsers import normalize_doi_field
    assert normalize_doi_field("10.1371/journal.pone.0312345?utm_source=browser") == "10.1371/journal.pone.0312345"
    assert normalize_doi_field("10.1371/journal.pone.0312345#fragment") == "10.1371/journal.pone.0312345"


def test_unterminated_midfile_entry_is_skipped_and_later_entries_survive():
    from citecheck.parsers import count_malformed_entries, malformed_entry_keys
    bib = "@article{a, doi={10.1000/a}}\n@article{bad, title={oops\n@article{c, doi={10.1000/c}}"
    refs = parse_bibtex(bib)
    assert [r.key for r in refs] == ["a", "c"]  # c used to be swallowed by bad
    assert count_malformed_entries(bib) == 1
    assert malformed_entry_keys(bib) == ["bad"]


# --- RIS parsing (EndNote / Zotero / Mendeley) ------------------------------

RIS = """TY  - JOUR
AU  - Ioannidis, John P. A.
AU  - Smith, Jane
TI  - Why Most Published Research Findings Are False
JO  - PLoS Medicine
PY  - 2005
DO  - 10.1371/journal.pmed.0020124
AN  - 16060722
DB  - PubMed
ER  - 

TY  - JOUR
AU  - Kim, Gildong
T1  - A second paper
T2  - The Lancet
Y1  - 2021/03/15/
DO  - https://doi.org/10.1016/S0140-6736(97)11096-0
ER  - 
"""


def test_parse_ris_basic():
    from citecheck.parsers import parse_ris, detect_format
    assert detect_format(RIS) == "ris"
    refs = parse_ris(RIS)
    assert len(refs) == 2
    a, b = refs
    assert a.doi == "10.1371/journal.pmed.0020124"
    assert a.author == "Ioannidis"
    assert a.year == 2005
    assert a.journal == "PLoS Medicine"
    assert a.pmid == "16060722"  # AN trusted because DB is PubMed
    assert "Why Most" in a.title
    # Second: T1/T2 tags, DOI given as URL, year with slashes.
    assert b.doi == "10.1016/s0140-6736(97)11096-0"
    assert b.author == "Kim"
    assert b.year == 2021
    assert b.journal == "The Lancet"


def test_ris_an_not_trusted_without_pubmed_provider():
    from citecheck.parsers import parse_ris
    ris = "TY  - JOUR\nTI  - X\nAN  - 999\nDB  - Embase\nER  - \n"
    assert parse_ris(ris)[0].pmid is None


def test_ris_continuation_line():
    from citecheck.parsers import parse_ris
    ris = "TY  - JOUR\nTI  - A very long title that\n      wraps onto two lines\nER  - \n"
    assert parse_ris(ris)[0].title == "A very long title that wraps onto two lines"


def test_ris_missing_er_still_parsed():
    from citecheck.parsers import parse_ris
    ris = "TY  - JOUR\nTI  - No end tag\nDO  - 10.1/x\n"
    refs = parse_ris(ris)
    assert len(refs) == 1 and refs[0].doi == "10.1/x"


def test_ris_ignores_preamble():
    from citecheck.parsers import parse_ris
    ris = "Some junk\nProvider: Foo\nTY  - JOUR\nDO  - 10.1/y\nER  - \n"
    refs = parse_ris(ris)
    assert len(refs) == 1 and refs[0].doi == "10.1/y"


# --- CSL-JSON parsing (Zotero / Better BibTeX / pandoc) ---------------------

CSL = """[
  {"id": "ioannidis2005", "type": "article-journal",
   "DOI": "10.1371/journal.pmed.0020124",
   "title": "Why Most Published Research Findings Are False",
   "container-title": "PLoS Medicine",
   "author": [{"family": "Ioannidis", "given": "John P. A."}],
   "issued": {"date-parts": [[2005, 8, 30]]},
   "PMID": "16060722"},
  {"id": "book1", "type": "book", "title": "No DOI Here",
   "author": [{"literal": "William Strunk"}],
   "issued": {"date-parts": [[1999]]}}
]"""


def test_parse_csl_json_basic():
    from citecheck.parsers import parse_csl_json, detect_format
    assert detect_format(CSL) == "csljson"
    refs = parse_csl_json(CSL)
    assert len(refs) == 2
    a, b = refs
    assert a.doi == "10.1371/journal.pmed.0020124"
    assert a.author == "Ioannidis"
    assert a.year == 2005
    assert a.journal == "PLoS Medicine"
    assert a.pmid == "16060722"
    assert a.key == "ioannidis2005"
    assert b.doi is None
    assert b.author == "Strunk"  # from literal name
    assert b.year == 1999


def test_csl_single_object_accepted():
    from citecheck.parsers import parse_csl_json, detect_format
    one = '{"DOI": "10.1/z", "title": "T", "type": "article-journal"}'
    assert detect_format(one) == "csljson"
    refs = parse_csl_json(one)
    assert len(refs) == 1 and refs[0].doi == "10.1/z"


def test_csl_container_title_as_list():
    from citecheck.parsers import parse_csl_json
    one = '[{"DOI":"10.1/z","title":["T"],"container-title":["Nature"]}]'
    r = parse_csl_json(one)[0]
    assert r.title == "T" and r.journal == "Nature"


def test_non_citation_json_not_detected_as_csl():
    from citecheck.parsers import detect_format
    # A JSON config that is not a reference list must fall through, not crash.
    assert detect_format('{"host": "localhost", "port": 8080}') != "csljson"


def test_malformed_json_falls_back_to_text():
    from citecheck.parsers import detect_format, parse_references
    bad = '[{"DOI": "10.1/x", title: broken}]'  # invalid JSON
    assert detect_format(bad) != "csljson"
    # Must not raise.
    parse_references(bad)


# --- PMID extraction --------------------------------------------------------

def test_find_pmid_labelled_only():
    from citecheck.parsers import find_pmid
    assert find_pmid("PMID: 16060722") == "16060722"
    assert find_pmid("pmid16060722") == "16060722"
    assert find_pmid("https://pubmed.ncbi.nlm.nih.gov/16060722/") == "16060722"
    assert find_pmid("PMID: 016060722") == "16060722"  # leading zeros stripped
    assert find_pmid("enrolled 2000 patients, page 1234") is None  # not labelled
    assert find_pmid("no id here") is None


def test_bibtex_pmid_field():
    from citecheck.parsers import parse_bibtex
    refs = parse_bibtex("@article{k, title={T}, doi={10.1/x}, pmid={16060722}}")
    assert refs[0].pmid == "16060722"


def test_bibtex_pmid_from_note():
    from citecheck.parsers import parse_bibtex
    refs = parse_bibtex("@article{k, title={T}, doi={10.1/x}, note={PMID: 16060722}}")
    assert refs[0].pmid == "16060722"


def test_bibtex_journal_extracted():
    from citecheck.parsers import parse_bibtex
    refs = parse_bibtex("@article{k, title={T}, journal={The Lancet}, doi={10.1/x}}")
    assert refs[0].journal == "The Lancet"


# --- hardening round 1 regressions ------------------------------------------

def test_biblatex_date_and_journaltitle_read():
    # Better BibTeX / biblatex exports use `date` and `journaltitle`.
    from citecheck.parsers import parse_bibtex
    r = parse_bibtex("@article{k, title={T}, journaltitle={The Lancet}, "
                     "date={2020-03-14}, doi={10.1/x}}")[0]
    assert r.year == 2020
    assert r.journal == "The Lancet"


def test_year_field_preferred_over_date():
    from citecheck.parsers import parse_bibtex
    r = parse_bibtex("@article{k, year={1999}, date={2020-03-14}, doi={10.1/x}}")[0]
    assert r.year == 1999


def test_find_pmid_rejects_zero():
    from citecheck.parsers import find_pmid
    assert find_pmid("PMID: 0") is None
    assert find_pmid("PMID: 0000") is None


def test_ris_false_trigger_on_prose_is_text():
    # A lone "TY  - ..." line in plain prose must NOT route the file to RIS.
    from citecheck.parsers import detect_format
    prose = "TY  - see the figure caption\nSmith J. A study. Lancet. 2020. 10.1/x"
    assert detect_format(prose) == "text"


def test_ris_still_detected_without_er_when_enough_tags():
    from citecheck.parsers import detect_format
    ris = "TY  - JOUR\nTI  - No end tag here\nDO  - 10.1/x\n"
    assert detect_format(ris) == "ris"


def test_deeply_nested_json_does_not_crash():
    from citecheck.parsers import detect_format, parse_references
    s = "[" * 60000 + "]" * 60000
    # Must degrade gracefully, never raise RecursionError.
    assert detect_format(s) == "text"
    parse_references(s)


def test_csl_pmid_zero_and_bool_rejected():
    from citecheck.parsers import parse_csl_json
    assert parse_csl_json('[{"DOI":"10.1/x","PMID":0}]')[0].pmid is None
    assert parse_csl_json('[{"DOI":"10.1/x","PMID":true}]')[0].pmid is None


# --- previously-untested branches (from security/test review) ---------------

def test_csl_year_from_raw_and_alternate_keys():
    from citecheck.parsers import parse_csl_json
    assert parse_csl_json('[{"DOI":"10.1/x","issued":{"raw":"published 2005"}}]')[0].year == 2005
    assert parse_csl_json('[{"DOI":"10.1/x","published-print":{"date-parts":[[2011,2]]}}]')[0].year == 2011
    assert parse_csl_json('[{"DOI":"10.1/x","issued":{"literal":"2018"}}]')[0].year == 2018


def test_csl_pmid_from_note_and_integer():
    from citecheck.parsers import parse_csl_json
    assert parse_csl_json('[{"DOI":"10.1/x","note":"PMID: 555"}]')[0].pmid == "555"
    assert parse_csl_json('[{"DOI":"10.1/x","PMID":16060722}]')[0].pmid == "16060722"


def test_csl_non_list_top_level_is_empty():
    from citecheck.parsers import parse_csl_json
    assert parse_csl_json("123") == []
    assert parse_csl_json('"just a string"') == []


def test_csl_skips_non_dict_items():
    from citecheck.parsers import parse_csl_json
    refs = parse_csl_json('[{"DOI":"10.1/x"}, 42, null, "str"]')
    assert len(refs) == 1 and refs[0].doi == "10.1/x"


def test_ris_pmid_from_free_text_note_without_trusted_an():
    from citecheck.parsers import parse_ris
    ris = "TY  - JOUR\nTI  - X\nN1  - See PMID: 16060722 for details\nER  - \n"
    assert parse_ris(ris)[0].pmid == "16060722"


def test_pmid_regex_no_redos():
    import time
    from citecheck.parsers import find_pmid
    start = time.time()
    find_pmid("pmid" + " " * 100000)  # no trailing digit
    assert time.time() - start < 1.0  # linear, not O(N^2)


def test_guess_text_author_is_linear():
    import time
    from citecheck.parsers import _guess_text_author
    start = time.time()
    _guess_text_author(" " * 300000 + "X")  # pathological leading whitespace
    assert time.time() - start < 1.0
    # Behaviour preserved across the usual forms.
    assert _guess_text_author("Kim H, Lee S.") == "Kim"
    assert _guess_text_author("[12] Smith J") == "Smith"
    assert _guess_text_author("1. Jones K") == "Jones"


# --- DOIs with invisible / non-ASCII characters glued on ---------------------
#
# `_DOI_RE` stops only at whitespace, so anything abutting the DOI is captured
# with it. The result is the worst error this tool can make: it tells the author
# their CORRECT DOI is a typo, and prints a string that looks identical to the
# right one on screen. Every case below came out of an ordinary authoring tool.

@pytest.mark.parametrize(
    "raw, expected",
    [
        # Invisible formatting characters (Unicode category Cf).
        ("doi:10.1000/x​", "10.1000/x"),        # zero-width space (web copy)
        ("doi:10.1000/x­", "10.1000/x"),        # soft hyphen (Word)
        ("doi:10.1000/x﻿", "10.1000/x"),        # inline BOM
        ("doi:10.1000/x⁠", "10.1000/x"),        # word joiner
        ("doi:10.10​00/x", "10.1000/x"),        # ...even mid-DOI
        # CJK / full-width punctuation from a Korean or Japanese manuscript.
        ("doi:10.5665/sleep.1872。", "10.5665/sleep.1872"),      # 。
        ("doi:10.1000/x（2021）", "10.1000/x"),              # （2021）
        ("doi:10.1000/x，", "10.1000/x"),                        # ，
        ("doi:10.1000/x、", "10.1000/x"),                        # 、
        # Adjacent Hangul with no separating space.
        ("doi:10.1000/x입니다", "10.1000/x"),
    ],
)
def test_doi_survives_invisible_and_cjk_characters(raw, expected):
    assert find_doi(raw) == expected


@pytest.mark.parametrize(
    "raw, expected",
    [
        # The ASCII behaviour these fixes must not disturb.
        ("10.1016/S0140-6736(97)11096-0", "10.1016/s0140-6736(97)11096-0"),
        ("(doi: 10.1000/x)", "10.1000/x"),
        ("10.5555/foo[bar]", "10.5555/foo[bar]"),
        ("10.5555/foo)", "10.5555/foo"),
        ("10.1000/x-y_z.w", "10.1000/x-y_z.w"),
        ("doi:10.1000/x, 2021", "10.1000/x"),
        ("https://doi.org/10.1000/x?utm_source=z", "10.1000/x"),
    ],
)
def test_ascii_doi_cleanup_is_unchanged(raw, expected):
    assert find_doi(raw) == expected


def test_invisible_character_doi_does_not_become_a_false_typo_error(tmp_path, capsys):
    """End-to-end: the symptom was `doi-not-resolving` on a correct DOI."""
    import json as _json

    from citecheck.cli import run
    from citecheck.core import CrossrefClient

    real = "10.1371/journal.pone.0312345"
    path = tmp_path / "zwsp.bib"
    # A zero-width space between the DOI and the closing brace, as a web copy
    # or a Word autocorrect leaves it.
    path.write_text(
        "@article{k, title={Sleep}, doi={%s​}}" % real, encoding="utf-8"
    )
    record = {"DOI": real, "title": ["Sleep"], "author": [{"family": "Kim"}],
              "issued": {"date-parts": [[2024]]}}
    code = run([str(path), "--json", "--delay", "0"],
               client=CrossrefClient(_fetch=lambda d: {real: record}.get(d),
                                     _resolve=lambda d: False))
    payload = _json.loads(capsys.readouterr().out)
    assert payload[0]["doi"] == real
    codes = [f["code"] for f in payload[0]["findings"]]
    assert "doi-not-resolving" not in codes
    assert code == 0


# --- CSV: the delimiter must never be glued onto a DOI ----------------------
#
# parse_csv scans the whole row for a DOI when no DOI column holds one — the
# documented "the DOI is often parked in a URL or Notes column" path. It used to
# build that scan text with `delim.join(...)`, and `_DOI_RE` stops only at
# whitespace, so the next column came along for the ride and the author's
# correct DOI was reported as a typo.

@pytest.mark.parametrize("delim", [",", ";", "|", "\t"])
def test_csv_doi_in_a_notes_column_is_not_glued_to_the_next_column(delim):
    text = (
        delim.join(["Title", "DOI", "Notes", "Year"]) + "\n"
        + delim.join(["Sleep and CBT", "", "see doi 10.1016/j.sleep.2021.01.001", "2021"])
        + "\n"
    )
    (ref,) = parse_references(text, fmt="csv")
    assert ref.doi == "10.1016/j.sleep.2021.01.001"


@pytest.mark.parametrize("delim", [",", ";", "|"])
def test_csv_doi_in_a_url_column_is_not_glued_to_the_next_column(delim):
    text = (
        delim.join(["Title", "DOI", "URL", "Year"]) + "\n"
        + delim.join(["Insomnia trial", "", "https://doi.org/10.5664/jcsm.8000", "2020"])
        + "\n"
    )
    (ref,) = parse_references(text, fmt="csv")
    assert ref.doi == "10.5664/jcsm.8000"


def test_csv_pmid_in_a_notes_column_is_not_glued_to_the_next_column():
    text = (
        "Title,PMID,Notes,Year\n"
        "Sleep and CBT,,PMID 23842505,2021\n"
    )
    (ref,) = parse_references(text, fmt="csv")
    assert ref.pmid == "23842505"


def test_csv_row_scan_still_finds_a_doi_when_a_real_doi_column_is_empty():
    """The feature the fix protects: a proper DOI column still wins outright."""
    text = (
        "Title,DOI,Notes\n"
        "A,10.1000/from-column,see doi 10.9999/from-notes\n"
        "B,,see doi 10.9999/from-notes\n"
    )
    a, b = parse_references(text, fmt="csv")
    assert a.doi == "10.1000/from-column"
    assert b.doi == "10.9999/from-notes"


# --- 0.2.0: one unterminated entry must not drop every later entry -----------

def _good(i):
    return "@article{good%d,\n  title = {Paper %d},\n  doi = {10.1000/p.%d}\n}\n" % (i, i, i)


BAD = "@article{bad,\n  title = {Effects of {IL-6 on sleep},\n  doi = {10.1000/bad}\n}\n"


def test_entries_after_an_unterminated_one_are_recovered():
    from citecheck.parsers import malformed_entry_keys

    bib = _good(0) + BAD + "".join(_good(i) for i in range(1, 50))
    refs = parse_bibtex(bib)
    assert len(refs) == 50
    assert [r.key for r in refs][:2] == ["good0", "good1"]
    assert malformed_entry_keys(bib) == ["bad"]


def test_unterminated_first_entry_does_not_hide_the_rest():
    bib = BAD + "".join(_good(i) for i in range(1999))
    refs = parse_bibtex(bib)
    assert len(refs) == 1999


def test_many_unterminated_entries_parse_in_linear_time():
    import time

    bib = "".join(BAD.replace("{bad,", "{bad%d," % i) + _good(i) for i in range(5000))
    t0 = time.perf_counter()
    refs = parse_bibtex(bib)
    elapsed = time.perf_counter() - t0
    assert len(refs) == 5000
    assert elapsed < 2.0, f"5,000 bad entries took {elapsed:.2f}s"


def test_many_unterminated_paren_entries_parse_in_linear_time():
    import time

    from citecheck.parsers import malformed_entry_keys

    bad_paren = "@article(badp%d,\n  title = {A paren entry that never closes}\n"
    bib = "".join(bad_paren % i + _good(i) for i in range(3000))
    t0 = time.perf_counter()
    refs = parse_bibtex(bib)
    keys = malformed_entry_keys(bib)
    elapsed = time.perf_counter() - t0
    assert len(refs) == 3000
    assert len(keys) == 3000 and keys[0] == "badp0"
    assert elapsed < 2.0, f"3,000 bad paren entries took {elapsed:.2f}s"


def test_cr_only_line_endings_still_resync_after_a_bad_entry():
    bib = (_good(0) + BAD + _good(1)).replace("\n", "\r")
    refs = parse_bibtex(bib)
    assert [r.key for r in refs] == ["good0", "good1"]


def test_entry_header_regex_is_linear_on_long_whitespace():
    """`@a{` followed by a long whitespace run used to backtrack quadratically."""
    import time

    from citecheck.parsers import detect_format

    text = "@a{" + " " * 100_000 + "x\n"
    t0 = time.perf_counter()
    detect_format(text)
    parse_references(text)
    assert time.perf_counter() - t0 < 1.0


def test_a_field_line_starting_with_at_does_not_cut_a_paren_entry_short():
    """Only a header with its key and comma ends a parenthesised entry early;
    an abstract line such as "@WHO (World Health Organization) ..." does not."""
    from citecheck.parsers import malformed_entry_keys

    bib = (
        "@article(ok2020,\n"
        "  title = {Why Most Published Research Findings Are False},\n"
        "  author = {Ioannidis, John P. A.},\n"
        "  year = {2005},\n"
        "  abstract = {Data were posted by the account\n"
        "@WHO (World Health Organization) during the outbreak.},\n"
        "  doi = {10.1371/journal.pmed.0020124}\n"
        ")\n"
    )
    refs = parse_bibtex(bib)
    assert [r.key for r in refs] == ["ok2020"]
    assert refs[0].doi == "10.1371/journal.pmed.0020124"
    assert malformed_entry_keys(bib) == []


def test_many_unterminated_paren_string_definitions_cost_linear_time():
    """An @string line still bounds the scan of an unterminated @string( before it."""
    import time

    bib = '@string(x = "J"\n' * 3000 + "".join(_good(i) for i in range(10))
    t0 = time.perf_counter()
    refs = parse_bibtex(bib)
    assert len(refs) == 10
    assert time.perf_counter() - t0 < 2.0


def test_a_header_with_whitespace_around_the_key_still_matches():
    refs = parse_bibtex("@article{  spaced  ,\n  doi = {10.1000/s}\n}\n@article{,doi={10.1000/e}}")
    assert [r.key for r in refs] == ["spaced", None]
    assert [r.doi for r in refs] == ["10.1000/s", "10.1000/e"]


# --- 0.2.0: @string macros are expanded, undefined ones are not compared -----

def test_string_macro_is_expanded_in_a_field():
    bib = (
        '@string{plosmed = "PLoS Medicine"}\n'
        "@article{k, title = {T}, journal = plosmed, doi = {10.1000/k}}\n"
    )
    assert parse_bibtex(bib)[0].journal == "PLoS Medicine"


def test_string_macro_names_are_case_insensitive_and_paren_form_works():
    bib = (
        "@STRING(JSR = {Journal of Sleep Research})\n"
        "@article{k, journal = jsr, doi = {10.1000/k}}\n"
    )
    assert parse_bibtex(bib)[0].journal == "Journal of Sleep Research"


def test_string_concatenation_in_definitions_and_fields():
    bib = (
        '@string{j = "Journal of" # { Sleep}}\n'
        '@string{jsr = j # " Research"}\n'
        '@article{k, journal = jsr # " (Online)", title = "A" # { B}, doi = {10.1000/k}}\n'
    )
    ref = parse_bibtex(bib)[0]
    assert ref.journal == "Journal of Sleep Research (Online)"
    assert ref.title == "A B"


def test_undefined_macro_is_not_compared():
    """`journal = jsr` with no @string for it must not reach the journal check
    as the literal text 'jsr' (a guaranteed false journal-mismatch)."""
    bib = "@article{k, title = {T}, journal = jsr, year = 2005, doi = {10.1000/k}}\n"
    ref = parse_bibtex(bib)[0]
    assert ref.journal is None
    assert ref.year == 2005  # bare numbers are still read
    assert ref.doi == "10.1000/k"


def test_string_using_an_undefined_macro_is_left_out():
    bib = (
        "@string{a = undefinedmacro # { x}}\n"
        "@article{k, journal = a, doi = {10.1000/k}}\n"
    )
    assert parse_bibtex(bib)[0].journal is None


def _doubling_chain(levels):
    lines = ['@string{a0 = "xxxxxxxxxx"}']
    lines += ["@string{a%d = a%d # a%d}" % (i, i - 1, i - 1) for i in range(1, levels + 1)]
    return "\n".join(lines) + (
        "\n@article{k, title = a%d, journal = a3, doi = {10.1000/k}}\n" % levels
    )


def test_doubling_macro_chain_is_capped_not_expanded():
    """`@string{aN = a(N-1) # a(N-1)}` doubles at every line: 40 lines under 1 KB
    would expand to about 10 TB. The expansion stops at _MAX_MACRO_CHARS and the
    field is left out, like an undefined macro."""
    import time

    from citecheck.parsers import _MAX_MACRO_CHARS, _string_macros

    # 22 levels first: without the cap this is a 42 MB title, which fails the
    # assertions below without exhausting memory, so the 40-level run is only
    # reached when the cap works.
    short = _doubling_chain(22)
    assert max(len(v) for v in _string_macros(short).values()) <= _MAX_MACRO_CHARS
    assert parse_bibtex(short)[0].title is None

    bib = _doubling_chain(40)
    assert len(bib) < 1100
    t0 = time.perf_counter()
    macros = _string_macros(bib)
    (ref,) = parse_bibtex(bib)
    assert time.perf_counter() - t0 < 1.0
    assert max(len(v) for v in macros.values()) <= _MAX_MACRO_CHARS
    assert ref.title is None
    assert ref.journal == "x" * 80  # a short macro of the same chain still expands
    assert ref.doi == "10.1000/k"


def test_repeating_one_macro_in_a_field_is_capped_too():
    from citecheck.parsers import _MAX_MACRO_CHARS

    big = "y" * (_MAX_MACRO_CHARS // 2 + 1)
    bib = '@string{big = "%s"}\n@article{k, title = big # big # big, doi = {10.1000/k}}\n' % big
    assert parse_bibtex(bib)[0].title is None
    ok = '@string{big = "%s"}\n@article{k, title = big, doi = {10.1000/k}}\n' % big
    assert parse_bibtex(ok)[0].title == big


def test_bare_unbraced_prose_values_keep_their_old_reading():
    """Not valid BibTeX, but seen in hand-written files: keep the raw text."""
    bib = "@article{k, title = A study of sleep, doi = 10.1371/journal.pmed.0020124}\n"
    ref = parse_bibtex(bib)[0]
    assert ref.title == "A study of sleep"
    assert ref.doi == "10.1371/journal.pmed.0020124"


def test_unterminated_string_definitions_cost_linear_time():
    import time

    bib = "@string{x = {J}\n" * 2000 + "".join(_good(i) for i in range(10))
    t0 = time.perf_counter()
    refs = parse_bibtex(bib)
    assert len(refs) == 10
    assert time.perf_counter() - t0 < 2.0


def test_macro_journal_does_not_raise_a_journal_mismatch():
    from citecheck.core import CrossrefClient, check_reference

    bib = (
        '@string{plosmed = "PLoS Medicine"}\n'
        "@article{ioannidis2005, title = {Why Most Published Research Findings Are False},"
        " author = {Ioannidis, John P. A.}, journal = plosmed, year = {2005},"
        " doi = {10.1371/journal.pmed.0020124}}\n"
        "@article{other, title = {Why Most Published Research Findings Are False},"
        " author = {Ioannidis, John P. A.}, journal = undefinedjournal, year = {2005},"
        " doi = {10.1371/journal.pmed.0020124}}\n"
    )
    record = {
        "DOI": "10.1371/journal.pmed.0020124",
        "title": ["Why Most Published Research Findings Are False"],
        "author": [{"family": "Ioannidis", "given": "John P. A."}],
        "container-title": ["PLoS Medicine"],
        "issued": {"date-parts": [[2005, 8, 30]]},
    }
    client = CrossrefClient(_fetch=lambda d: record, _resolve=lambda d: True)
    for ref in parse_bibtex(bib):
        codes = [f.code for f in check_reference(ref, client).findings]
        assert codes == ["verified"], (ref.key, codes)


def test_bare_url_and_prefixed_doi_values_are_not_taken_for_macros():
    """`doi = doi:10.1000/x` and a bare URL are not macro names; keep the text."""
    bib = (
        "@article{k, title = {T}, doi = doi:10.1000/xyz,\n"
        "  url = https://pubmed.ncbi.nlm.nih.gov/12345678/}\n"
        "@article{u, title = {T}, doi = https://doi.org/10.1000/abc}\n"
    )
    k, u = parse_bibtex(bib)
    assert k.doi == "10.1000/xyz"
    assert k.pmid == "12345678"
    assert u.doi == "10.1000/abc"
