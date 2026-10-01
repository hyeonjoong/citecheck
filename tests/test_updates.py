"""Crossmark update flags (expression of concern / withdrawal / correction) and
preprint-superseded-by-published detection.

These are the checks a clinical/pharma author most needs and that a plain
"does the DOI resolve" pass cannot give: a paper can resolve perfectly, have
flawless metadata, and still be under an expression of concern.
"""

import pytest

from citecheck.core import (
    ERROR,
    OK,
    WARNING,
    _classify_updates,
    _normalize_update_type,
    _published_version_doi,
    _relation_ids,
    check_reference,
)
from citecheck.parsers import Reference

BASE = {
    "DOI": "10.1/x",
    "title": ["A trial of something"],
    "author": [{"family": "Kim"}],
    "issued": {"date-parts": [[2024]]},
    "type": "journal-article",
}


def client(records, resolves=False):
    from citecheck.core import CrossrefClient

    return CrossrefClient(_fetch=lambda d: records.get(d), _resolve=lambda d: resolves)


def check(message, ref=None):
    ref = ref or Reference(raw="", doi="10.1/x")
    return check_reference(ref, client({"10.1/x": message}))


# --- severity contract ------------------------------------------------------


@pytest.mark.parametrize(
    "update_type, severity, needle",
    [
        ("expression_of_concern", ERROR, "EXPRESSION OF CONCERN"),
        ("withdrawal", ERROR, "WITHDRAWN"),
        ("removal", ERROR, "REMOVED"),
        ("correction", WARNING, "correction"),
        ("erratum", WARNING, "erratum"),
        ("addendum", WARNING, "addendum"),
        ("clarification", WARNING, "clarification"),
        ("new_edition", WARNING, "newer edition"),
    ],
)
def test_update_kinds_map_to_expected_severity(update_type, severity, needle):
    res = check(dict(BASE, **{"updated-by": [{"type": update_type, "DOI": "10.1/notice"}]}))
    assert res.status == severity
    assert any(needle in f.message for f in res.findings)
    # The notice DOI is surfaced so the author can go read it.
    assert any("10.1/notice" in f.message for f in res.findings)


def test_update_without_notice_doi_still_reported():
    res = check(dict(BASE, **{"updated-by": [{"type": "expression_of_concern"}]}))
    assert res.status == ERROR
    assert not any("notice:" in f.message for f in res.findings)


@pytest.mark.parametrize(
    "written",
    ["expression_of_concern", "expression-of-concern", "Expression Of Concern", "  EXPRESSION  OF  CONCERN  "],
)
def test_update_type_spelling_variants_all_normalize(written):
    assert _normalize_update_type(written) == "expression_of_concern"
    res = check(dict(BASE, **{"updated-by": [{"type": written}]}))
    assert res.status == ERROR


def test_clean_record_reports_no_updates():
    assert _classify_updates(BASE) == []
    assert check(BASE).status == OK


# --- narrowing: what must NOT be flagged ------------------------------------


def test_update_to_is_not_flagged():
    """A record carrying `update-to` IS the notice, not a corrected article.

    Reporting "this reference has a correction issued" about an erratum notice
    would be exactly backwards, so only `update-by` is consulted.
    """
    res = check(dict(BASE, **{"update-to": [{"type": "correction", "DOI": "10.1/orig"}]}))
    assert res.status == OK


def test_retraction_suppresses_softer_update_flags():
    """A retracted paper must not also nag about its correction — one verdict."""
    res = check(
        dict(
            BASE,
            **{
                "updated-by": [
                    {"type": "retraction", "DOI": "10.1/retraction"},
                    {"type": "correction", "DOI": "10.1/corr"},
                ]
            },
        )
    )
    assert res.status == ERROR
    messages = [f.message for f in res.findings]
    assert any("RETRACTED" in m for m in messages)
    assert not any("correction" in m for m in messages)


def test_partial_retraction_is_handled_by_the_retraction_path_only():
    """`_is_retracted` substring-matches "retract", so partial_retraction is its
    business — the update classifier must not double-report it."""
    msg = dict(BASE, **{"updated-by": [{"type": "partial_retraction"}]})
    assert _classify_updates(msg) == []
    res = check(msg)
    assert res.status == ERROR
    assert sum("RETRACT" in f.message.upper() for f in res.findings) == 1


def test_unknown_update_type_is_ignored():
    """Crossref adds update types over time; an unrecognised one must not warn."""
    assert _classify_updates(dict(BASE, **{"updated-by": [{"type": "some_future_type"}]})) == []


def test_duplicate_update_kinds_reported_once():
    msg = dict(
        BASE,
        **{"updated-by": [{"type": "correction", "DOI": "10.1/a"}, {"type": "correction", "DOI": "10.1/b"}]},
    )
    assert len(_classify_updates(msg)) == 1


# --- malformed records must never raise -------------------------------------


@pytest.mark.parametrize(
    "update_by",
    [
        "a string where a list belongs",
        ["a bare string, not a dict"],
        [None],
        [{"type": None}],
        [{"type": ["a", "list"]}],
        [{}],
        [{"type": "correction", "DOI": None}],
        {"type": "correction"},  # a bare dict, not a list
    ],
)
def test_malformed_update_by_never_raises(update_by):
    res = check(dict(BASE, **{"updated-by": update_by}))
    assert res.status in (OK, WARNING, ERROR)  # a verdict, not a crash


# --- preprint superseded by a published version -----------------------------


PREPRINT = dict(
    BASE,
    type="posted-content",
    subtype="preprint",
    relation={"is-preprint-of": [{"id": "10.1056/published", "id-type": "doi"}]},
)


def test_preprint_with_published_version_warns_and_names_it():
    res = check(PREPRINT)
    assert res.status == WARNING
    msg = next(f.message for f in res.findings if "preprint" in f.message)
    assert "10.1056/published" in msg


def test_preprint_without_a_published_version_does_not_warn():
    """Citing a preprint is legitimate; only a *superseded* one is actionable."""
    res = check(dict(BASE, type="posted-content", subtype="preprint"))
    assert res.status == OK


def test_preprint_relation_underscore_spelling_tolerated():
    msg = dict(BASE, relation={"is_preprint_of": [{"id": "10.1056/published"}]})
    assert _published_version_doi(msg) == "10.1056/published"


def test_published_version_doi_normalized_from_url_form():
    msg = dict(BASE, relation={"is-preprint-of": [{"id": "https://doi.org/10.1056/Published"}]})
    assert _published_version_doi(msg) == "10.1056/published"


def test_non_doi_relation_ids_ignored():
    """A relation entry pointing at a PMID/URL is not a DOI we can suggest."""
    msg = dict(BASE, relation={"is-preprint-of": [{"id": "12345", "id-type": "pmid"}]})
    assert _published_version_doi(msg) is None


def test_has_preprint_relation_is_not_a_preprint_warning():
    """The published version points back with `has-preprint`. That record is the
    one the author *should* cite, so it must stay clean."""
    msg = dict(BASE, relation={"has-preprint": [{"id": "10.1101/preprint"}]})
    assert _published_version_doi(msg) is None
    assert check(msg).status == OK


@pytest.mark.parametrize(
    "relation",
    ["not a dict", [], {"is-preprint-of": "a string"}, {"is-preprint-of": [None]}, {None: [{"id": "x"}]}],
)
def test_malformed_relation_never_raises(relation):
    assert _relation_ids(dict(BASE, relation=relation), "is-preprint-of") == []


def test_retraction_wins_over_preprint_notice():
    """A retracted preprint is an error, and the preprint hint still helps."""
    msg = dict(PREPRINT, **{"updated-by": [{"type": "retraction"}]})
    res = check(msg)
    assert res.status == ERROR
