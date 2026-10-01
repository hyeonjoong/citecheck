"""The 0.1.0 test suite, kept as a compatibility contract.

These are the 0.1.0 tests that still describe correct behaviour, rewritten with
synthetic names, titles and DOIs. The old test_retraction_errors is not here: it
built its fixture with type="retraction", which is not a Crossref work type.
Retraction detection is covered with real record shapes in
test_retraction_real_shapes.py.

0.1.0 documented that tests run fully offline through an injected transport.
A caller that injects only ``_fetch`` must therefore never reach doi.org.
"""

import re
from pathlib import Path

import citecheck
from citecheck import core
from citecheck.core import ERROR, OK, WARNING, CrossrefClient, PubMedClient, check_reference
from citecheck.parsers import (
    Reference,
    find_doi,
    find_year,
    parse_bibtex,
    parse_references,
    parse_text,
)

DOI = "10.1000/example.0001"


def make_client(records):
    """A CrossrefClient whose only transport is a dict: doi -> message | None."""
    return CrossrefClient(_fetch=lambda doi: records.get(doi))


GOOD = {
    "DOI": DOI,
    "title": ["A synthetic study of sleep and hearing in adults"],
    "author": [{"family": "Hong", "given": "Gildong"}],
    "issued": {"date-parts": [[2024, 5, 1]]},
    "type": "journal-article",
}


def test_verified_reference_is_ok():
    ref = Reference(
        raw="",
        doi=DOI,
        title="A synthetic study of sleep and hearing in adults",
        author="Hong",
        year=2024,
    )
    res = check_reference(ref, make_client({DOI: GOOD}))
    assert res.status == OK


def test_missing_doi_warns():
    res = check_reference(Reference(raw="something"), make_client({}))
    assert res.status == WARNING
    assert "No DOI" in res.findings[0].message


def test_unresolvable_doi_errors_with_only_fetch_injected(monkeypatch):
    """Only _fetch is injected, so the doi.org check must not go to the network."""

    def no_network(*args, **kwargs):
        raise AssertionError("an injected-transport client reached the network")

    monkeypatch.setattr(core.CrossrefClient, "_resolve_network", no_network)
    ref = Reference(raw="", doi="10.9999/nope")
    res = check_reference(ref, make_client({}))  # fetch returns None
    assert res.status == ERROR
    assert "does not resolve" in res.findings[0].message
    assert res.findings[0].code == "doi-not-resolving"


def test_title_mismatch_warns():
    ref = Reference(raw="", doi=DOI, title="A completely unrelated paper about penguins")
    res = check_reference(ref, make_client({DOI: GOOD}))
    assert res.status == WARNING
    assert any("Title mismatch" in f.message for f in res.findings)


def test_year_mismatch_warns():
    ref = Reference(raw="", doi=DOI, year=2019)
    res = check_reference(ref, make_client({DOI: GOOD}))
    assert any("Year mismatch" in f.message for f in res.findings)


def test_lookup_failure_is_warning_not_error():
    def boom(doi):
        raise TimeoutError("network down")

    res = check_reference(Reference(raw="", doi=DOI), CrossrefClient(_fetch=boom))
    assert res.status == WARNING
    assert "Lookup failed" in res.findings[0].message


# --- parsers (0.1.0 tests/test_parsers.py) -----------------------------------

BIB = r"""
@article{hong2024sleep,
  title   = {A synthetic study of sleep and hearing in {ADULT} cohorts},
  author  = {Hong, Gildong J. and Lee, Sara and Park, Min},
  journal = {PLOS ONE},
  year    = {2024},
  doi     = {10.1000/example.0001},
}

@book{strunk1999,
  title = {The Elements of Style},
  author = {Strunk, William and White, E. B.},
  year = {1999}
}
"""


def test_find_doi_strips_trailing_punctuation():
    assert find_doi("see 10.1000/example.0001.") == "10.1000/example.0001"
    assert find_doi("(doi: 10.1000/xyz123)") == "10.1000/xyz123"
    assert find_doi("no doi here") is None


def test_find_year():
    assert find_year("Published in 2024 somewhere") == 2024
    assert find_year("no year") is None


def test_parse_bibtex_extracts_fields():
    refs = parse_bibtex(BIB)
    assert len(refs) == 2
    first = refs[0]
    assert first.key == "hong2024sleep"
    assert first.doi == "10.1000/example.0001"
    assert first.year == 2024
    assert first.author == "Hong"
    assert "sleep and hearing" in first.title
    # Braces inside the title must be stripped.
    assert "{" not in first.title and "}" not in first.title


def test_parse_bibtex_author_without_doi():
    book = parse_bibtex(BIB)[1]
    assert book.doi is None
    assert book.author == "Strunk"


def test_auto_detect_text():
    text = "Hong G, Lee S. A study. PLOS ONE. 2024. doi:10.1000/example.0001"
    refs = parse_references(text, fmt="auto")
    assert len(refs) == 1
    assert refs[0].doi == "10.1000/example.0001"
    assert refs[0].author == "Hong"
    assert refs[0].year == 2024


def test_parse_text_paragraphs():
    text = "Ref one 10.1000/a\n\nRef two 10.1000/b"
    refs = parse_text(text)
    assert [r.doi for r in refs] == ["10.1000/a", "10.1000/b"]


# --- version and User-Agent --------------------------------------------------

def test_user_agent_uses_package_version():
    expected = f"citecheck/{citecheck.__version__} "
    assert CrossrefClient().user_agent.startswith(expected)
    assert CrossrefClient(mailto="you@example.com").user_agent.startswith(expected)
    assert PubMedClient().user_agent.startswith(expected)
    assert PubMedClient(mailto="you@example.com").user_agent.startswith(expected)
    assert core.DEFAULT_UA.startswith(expected)


def test_version_matches_pyproject():
    pyproject = (Path(__file__).resolve().parent.parent / "pyproject.toml").read_text(
        encoding="utf-8"
    )
    m = re.search(r'^version\s*=\s*"([^"]+)"', pyproject, re.MULTILINE)
    assert m and m.group(1) == citecheck.__version__ == "0.2.0"
