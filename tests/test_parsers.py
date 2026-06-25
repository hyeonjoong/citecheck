from citecheck.parsers import find_doi, find_year, parse_bibtex, parse_references, parse_text

BIB = r"""
@article{kim2024sleep,
  title   = {Sleep as a transdiagnostic node in {BELL} disorders},
  author  = {Kim, Hyeon J. and Lee, Sara and Park, Min},
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
