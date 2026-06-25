from citecheck.core import CrossrefClient, ERROR, OK, WARNING, check_reference
from citecheck.parsers import Reference


def make_client(records):
    """A CrossrefClient whose transport is a dict: doi -> message | None."""
    return CrossrefClient(_fetch=lambda doi: records.get(doi))


GOOD = {
    "DOI": "10.1371/journal.pone.0312345",
    "title": ["Sleep as a transdiagnostic node in BELL disorders"],
    "author": [{"family": "Kim", "given": "Hyeon"}],
    "issued": {"date-parts": [[2024, 5, 1]]},
    "type": "journal-article",
}


def test_verified_reference_is_ok():
    ref = Reference(
        raw="",
        doi="10.1371/journal.pone.0312345",
        title="Sleep as a transdiagnostic node in BELL disorders",
        author="Kim",
        year=2024,
    )
    res = check_reference(ref, make_client({ref.doi: GOOD}))
    assert res.status == OK


def test_missing_doi_warns():
    res = check_reference(Reference(raw="something"), make_client({}))
    assert res.status == WARNING
    assert "No DOI" in res.findings[0].message


def test_unresolvable_doi_errors():
    ref = Reference(raw="", doi="10.9999/nope")
    res = check_reference(ref, make_client({}))  # fetch returns None
    assert res.status == ERROR
    assert "does not resolve" in res.findings[0].message


def test_title_mismatch_warns():
    ref = Reference(
        raw="",
        doi="10.1371/journal.pone.0312345",
        title="A completely unrelated paper about penguins",
    )
    res = check_reference(ref, make_client({ref.doi: GOOD}))
    assert res.status == WARNING
    assert any("Title mismatch" in f.message for f in res.findings)


def test_year_mismatch_warns():
    ref = Reference(raw="", doi="10.1371/journal.pone.0312345", year=2019)
    res = check_reference(ref, make_client({ref.doi: GOOD}))
    assert any("Year mismatch" in f.message for f in res.findings)


def test_retraction_errors():
    retracted = dict(GOOD, type="retraction")
    ref = Reference(raw="", doi="10.1371/journal.pone.0312345")
    res = check_reference(ref, make_client({ref.doi: retracted}))
    assert res.status == ERROR
    assert any("RETRACTED" in f.message for f in res.findings)


def test_lookup_failure_is_warning_not_error():
    def boom(doi):
        raise TimeoutError("network down")

    ref = Reference(raw="", doi="10.1371/journal.pone.0312345")
    res = check_reference(ref, CrossrefClient(_fetch=boom))
    assert res.status == WARNING
    assert "Lookup failed" in res.findings[0].message
