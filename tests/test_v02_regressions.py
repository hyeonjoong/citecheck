"""Regression tests for the defects fixed in 0.2.0.

Each test reproduces a bug that 0.1.0 shipped with. Most of them use only the
API that 0.1.0 already had (parse_references, parse_text, check_reference, an
injected CrossrefClient transport, cli.run) and fail on 0.1.0. The C4 test also
uses the newer `_resolve` hook and finding codes, so on 0.1.0 it stops with a
TypeError instead of showing the bug. All of them are offline: Crossref is
either injected or refused.
"""

import json
import time
import urllib.error
import urllib.request

import pytest

from citecheck import cli
from citecheck.core import ERROR, OK, WARNING, CrossrefClient, check_reference
from citecheck.parsers import Reference, parse_references, parse_text


def _offline(monkeypatch):
    """Make every HTTP request fail the way it does without a network."""

    def refuse(*args, **kwargs):
        raise urllib.error.URLError("offline (test)")

    monkeypatch.setattr(urllib.request, "urlopen", refuse)
    monkeypatch.setattr(time, "sleep", lambda seconds: None)


# --- C1: retracted papers must not be reported as verified -------------------

# Trimmed from the live Crossref record of 10.1016/S0140-6736(97)11096-0.
WAKEFIELD = {
    "DOI": "10.1016/s0140-6736(97)11096-0",
    "type": "journal-article",
    "title": [
        "RETRACTED: Ileal-lymphoid-nodular hyperplasia, non-specific colitis, "
        "and pervasive developmental disorder in children"
    ],
    "author": [{"family": "Wakefield", "given": "AJ"}],
    "container-title": ["The Lancet"],
    "issued": {"date-parts": [[1998, 2]]},
    "relation": {},
    "updated-by": [
        {"DOI": "10.1016/s0140-6736(10)60175-4", "type": "retraction",
         "label": "Retraction", "source": "retraction-watch"},
    ],
}

# The retraction notice for it: carries `update-to` and the relation
# `is-retraction-of`. Citing a notice is legitimate.
WAKEFIELD_NOTICE = {
    "DOI": "10.1016/s0140-6736(10)60175-4",
    "type": "journal-article",
    "title": [
        "Retraction - Ileal-lymphoid-nodular hyperplasia, non-specific colitis, "
        "and pervasive developmental disorder in children"
    ],
    "container-title": ["The Lancet"],
    "issued": {"date-parts": [[2010, 2]]},
    "relation": {"is-retraction-of": [{"id-type": "doi", "id": "10.1016/s0140-6736(97)11096-0"}]},
    "update-to": [
        {"DOI": "10.1016/s0140-6736(97)11096-0", "type": "retraction",
         "label": "Retraction", "source": "retraction-watch"},
    ],
}


def _client(records):
    return CrossrefClient(_fetch=lambda doi: records.get(doi))


def test_retracted_paper_is_an_error_naming_its_notice():
    ref = Reference(
        raw="",
        doi="10.1016/s0140-6736(97)11096-0",
        title="Ileal-lymphoid-nodular hyperplasia, non-specific colitis, and "
        "pervasive developmental disorder in children",
        author="Wakefield",
        year=1998,
    )
    res = check_reference(ref, _client({ref.doi: WAKEFIELD}))
    assert res.status == ERROR
    assert any("RETRACTED" in f.message and "10.1016/s0140-6736(10)60175-4" in f.message
               for f in res.findings)


def test_retraction_notice_itself_is_not_flagged_as_retracted():
    ref = Reference(raw="", doi="10.1016/s0140-6736(10)60175-4")
    res = check_reference(ref, _client({ref.doi: WAKEFIELD_NOTICE}))
    assert not any("RETRACTED" in f.message for f in res.findings)
    assert res.status == OK


def test_title_prefix_alone_marks_a_retraction():
    record = {
        "DOI": "10.1056/nejmoa2007621",
        "type": "journal-article",
        "title": ["RETRACTED: Cardiovascular Disease, Drug Therapy, and Mortality in Covid-19"],
        "author": [{"family": "Mehra", "given": "Mandeep R."}],
        "issued": {"date-parts": [[2020, 6, 18]]},
        "relation": {},
    }
    ref = Reference(raw="", doi="10.1056/nejmoa2007621")
    res = check_reference(ref, _client({ref.doi: record}))
    assert res.status == ERROR


# --- C3: an offline run must not exit 0 --------------------------------------

def test_offline_run_is_inconclusive_not_clean(tmp_path, monkeypatch, capsys):
    _offline(monkeypatch)
    bib = tmp_path / "refs.bib"
    bib.write_text(
        "@article{a,\n  title = {T},\n  doi = {10.1371/journal.pmed.0020124}\n}\n",
        encoding="utf-8",
    )
    code = cli.run([str(bib), "--no-color", "--delay", "0"])
    assert code == 3


# --- C4: a DataCite DOI that resolves at doi.org is not a hard error ---------

def test_doi_registered_outside_crossref_is_a_warning():
    client = CrossrefClient(_fetch=lambda doi: None, _resolve=lambda doi: True)
    res = check_reference(Reference(raw="", doi="10.5281/zenodo.21519918"), client)
    assert res.status == WARNING
    assert res.findings[0].code == "doi-not-in-crossref"


# --- C5: @string/@comment and parenthesised entries --------------------------

MACROS_BIB = """\
@string{plosmed = "PLoS Medicine"}

@article{ioannidis2005,
  title   = {Why Most Published Research Findings Are False},
  author  = {Ioannidis, John P. A.},
  journal = plosmed,
  year    = {2005},
  doi     = {10.1371/journal.pmed.0020124}
}

@comment{jabref-meta: databaseType:bibtex;}

@article{wakefield1998,
  title   = {Ileal-lymphoid-nodular hyperplasia, non-specific colitis, and
             pervasive developmental disorder in children},
  author  = {Wakefield, A. J.},
  journal = {The Lancet},
  year    = {1998},
  doi     = {10.1016/S0140-6736(97)11096-0}
}

@article(prisma2009,
  title   = {Preferred Reporting Items for Systematic Reviews and Meta-Analyses:
             The PRISMA Statement},
  author  = {Moher, David},
  year    = {2009},
  doi     = {10.1371/journal.pmed.1000097}
)
"""


def test_macro_and_comment_entries_do_not_swallow_real_references():
    refs = parse_references(MACROS_BIB)
    assert [r.key for r in refs] == ["ioannidis2005", "wakefield1998", "prisma2009"]
    assert [r.doi for r in refs] == [
        "10.1371/journal.pmed.0020124",
        "10.1016/s0140-6736(97)11096-0",
        "10.1371/journal.pmed.1000097",
    ]


# --- C6: text-mode DOI extraction and splitting ------------------------------

def test_text_mode_keeps_parenthesised_lancet_dois_whole():
    text = (
        "Wakefield AJ, Murch SH, Anthony A, et al. Ileal-lymphoid-nodular hyperplasia, "
        "non-specific colitis, and pervasive developmental disorder in children. "
        "Lancet. 1998;351(9103):637-41. doi:10.1016/S0140-6736(97)11096-0\n"
        "Mehra MR, Desai SS, Ruschitzka F, Patel AN. Hydroxychloroquine or chloroquine "
        "with or without a macrolide for treatment of COVID-19. Lancet. 2020. "
        "https://doi.org/10.1016/S0140-6736(20)31180-6\n"
    )
    refs = parse_text(text)
    assert [r.doi for r in refs] == [
        "10.1016/s0140-6736(97)11096-0",
        "10.1016/s0140-6736(20)31180-6",
    ]


def test_doi_field_with_a_url_query_string_is_normalised():
    bib = (
        "@article{q,\n  title = {T},\n"
        "  doi = {https://doi.org/10.1136/bmj.b2535?utm_source=share}\n}\n"
    )
    assert parse_references(bib)[0].doi == "10.1136/bmj.b2535"


def test_whitespace_only_blank_line_separates_two_references():
    text = (
        "Smith J. A first synthetic paper title.\n"
        "PLoS ONE. 2019. doi:10.1000/alpha.1\n"
        " \n"
        "Lee K. A second synthetic paper title.\n"
        "PLoS ONE. 2020. doi:10.1000/beta.2\n"
    )
    refs = parse_text(text)
    assert [r.doi for r in refs] == ["10.1000/alpha.1", "10.1000/beta.2"]


def test_email_address_and_brace_do_not_route_text_to_bibtex():
    text = (
        "Smith J. A synthetic paper {draft}. Contact: smith@example.org. "
        "doi:10.1000/alpha.1\n"
        "Lee K. Another synthetic paper. doi:10.1000/beta.2\n"
    )
    refs = parse_references(text)
    assert [r.doi for r in refs] == ["10.1000/alpha.1", "10.1000/beta.2"]


# --- C7: Korean encodings ----------------------------------------------------

def test_cp949_bib_file_on_disk_is_decoded_not_a_crash(tmp_path, capsys):
    bib = (
        "@article{kim2020,\r\n"
        "  title  = {수면 무호흡 환자의 인지 기능 변화},\r\n"
        "  author = {홍길동 and 김철수},\r\n"
        "  year   = {2020}\r\n"
        "}\r\n"
    )
    path = tmp_path / "korean.bib"
    path.write_bytes(bib.encode("cp949"))
    code = cli.run([str(path), "--no-color", "--delay", "0"])
    err = capsys.readouterr().err
    assert code == 0  # one no-doi warning; not a traceback, not exit 1
    assert "decoded as cp949" in err


def test_utf8_bom_csv_has_no_phantom_header_reference(tmp_path, monkeypatch, capsys):
    _offline(monkeypatch)
    path = tmp_path / "refs.csv"
    path.write_bytes(
        b"\xef\xbb\xbf"  # UTF-8 BOM, written as bytes so no editor can drop it
        + "Title,DOI\r\nA synthetic paper title,10.1000/alpha.1\r\n".encode("utf-8")
    )
    cli.run([str(path), "--json", "--delay", "0"])
    payload = json.loads(capsys.readouterr().out)
    assert len(payload) == 1
    assert payload[0]["doi"] == "10.1000/alpha.1"


# --- C10: false mismatches on correct citations ------------------------------

def test_online_first_year_is_accepted():
    record = {
        "DOI": "10.1111/jsr.13709",
        "title": ["A synthetic online-first title"],
        "author": [{"family": "Smith", "given": "J"}],
        "published-online": {"date-parts": [[2022, 8, 29]]},
        "published-print": {"date-parts": [[2023, 2]]},
        "issued": {"date-parts": [[2022, 8, 29]]},
    }
    ref = Reference(raw="", doi="10.1111/jsr.13709", year=2022, author="Smith")
    res = check_reference(ref, _client({ref.doi: record}))
    assert res.status == OK


def test_diacritics_are_folded_in_author_names():
    record = {
        "DOI": "10.1000/umlaut",
        "title": ["A synthetic title"],
        "author": [{"family": "Müller", "given": "A"}],
        "issued": {"date-parts": [[2020]]},
    }
    ref = Reference(raw="", doi="10.1000/umlaut", author="Muller", year=2020)
    res = check_reference(ref, _client({ref.doi: record}))
    assert res.status == OK


# --- B12: CLI argument and terminal handling ---------------------------------

def test_negative_delay_is_a_usage_error(tmp_path):
    bib = tmp_path / "refs.bib"
    bib.write_text(
        "@book{a, title = {One}, year = {1999}}\n@book{b, title = {Two}, year = {2000}}\n",
        encoding="utf-8",
    )
    with pytest.raises(SystemExit) as exc:
        cli.run([str(bib), "--delay", "-1"])
    assert exc.value.code == 2
