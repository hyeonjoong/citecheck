"""Retraction detection against REAL Crossref payload shapes.

Why this file exists
--------------------
Every retraction test in this suite used to build its fixture around a field
called ``update-by``. **Crossref has no such field.** The real one is
``updated-by``. The tests were green, the code was wrong, and the tool reported
``✓ Verified`` for Wakefield 1998 — the most famous retraction in medicine,
whose own Crossref title literally begins "RETRACTED:". Three prior hardening
rounds missed it, because fixture and code shared the same invented field name.

So the fixtures below are trimmed copies of live ``api.crossref.org`` responses
(fetched 2026-07-16), keeping the retraction-relevant keys verbatim. If someone
renames a field again, these fail. Comparing our fixtures against the real API is
the only thing that makes a green suite mean anything here.

They stay offline: the payloads are inlined, not fetched.
"""

import pytest

from citecheck.core import (
    ERROR,
    OK,
    WARNING,
    CrossrefClient,
    _classify_updates,
    _crossref_title_candidates,
    _is_retracted,
    _retraction_notice,
    check_reference,
)
from citecheck.parsers import Reference

# api.crossref.org/works/10.1016/S0140-6736(97)11096-0 — Wakefield 1998.
# Retracted 2010. Note `relation: {}` and NO `update-to`: `updated-by` is the
# only Crossmark signal, alongside the title prefix.
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
    "created": {"date-parts": [[2002, 8, 25]]},  # deposit date, NOT publication
    "relation": {},
    "updated-by": [
        {
            "DOI": "10.1016/s0140-6736(04)15715-2",
            "type": "correction",
            "label": "Correction",
            "source": "retraction-watch",
        },
        {
            "DOI": "10.1016/s0140-6736(10)60175-4",
            "type": "retraction",
            "label": "Retraction",
            "source": "retraction-watch",
        },
    ],
}

# api.crossref.org/works/10.1016/S0140-6736(10)60175-4 — the retraction NOTICE
# for Wakefield. Carries `update-to`; `updated-by` is null. Citing a retraction
# notice is legitimate, so this must NOT be reported as retracted.
WAKEFIELD_NOTICE = {
    "DOI": "10.1016/s0140-6736(10)60175-4",
    "type": "journal-article",
    "title": [
        "Retraction—Ileal-lymphoid-nodular hyperplasia, non-specific colitis, "
        "and pervasive developmental disorder in children"
    ],
    "container-title": ["The Lancet"],
    "issued": {"date-parts": [[2010, 2]]},
    "updated-by": None,
    "update-to": [
        {
            "DOI": "10.1016/s0140-6736(97)11096-0",
            "type": "retraction",
            "label": "Retraction",
            "source": "retraction-watch",
        }
    ],
}

# api.crossref.org/works/10.1056/NEJMoa2007621 — Mehra, NEJM, retracted 2020.
# Crossmark records only an expression_of_concern; the retraction shows up ONLY
# in the title prefix. Without the title signal this reads as a mere warning.
MEHRA_NEJM = {
    "DOI": "10.1056/nejmoa2007621",
    "type": "journal-article",
    "title": ["RETRACTED: Cardiovascular Disease, Drug Therapy, and Mortality in Covid-19"],
    "author": [{"family": "Mehra", "given": "Mandeep R."},
               {"family": "Desai", "given": "Sapan S."}],
    "container-title": ["New England Journal of Medicine"],
    "issued": {"date-parts": [[2020, 6, 18]]},
    "relation": {},
    "update-to": None,
    "updated-by": [
        {
            "DOI": "10.1056/nejme2020822",
            "type": "expression_of_concern",
            "label": "Expression of concern",
            "source": "retraction-watch",
        }
    ],
}

# api.crossref.org/works/10.1016/S0140-6736(20)31180-6 — Mehra, Lancet
# (Surgisphere HCQ). The messy one: Elsevier deposits `update-to` retractions on
# the RETRACTED PAPER ITSELF, pointing at its own notices. So `update-to` marks
# neither side reliably — which is exactly why the code trusts `updated-by` only.
MEHRA_LANCET = {
    "DOI": "10.1016/s0140-6736(20)31180-6",
    "type": "journal-article",
    "title": [
        "RETRACTED: Hydroxychloroquine or chloroquine with or without a macrolide "
        "for treatment of COVID-19: a multinational registry analysis"
    ],
    "author": [{"family": "Mehra", "given": "Mandeep R"}],
    "container-title": ["The Lancet"],
    "issued": {"date-parts": [[2020, 5]]},
    "relation": {"has-review": [{"id-type": "doi", "id": "10.3410/f.737996380.793574823"}]},
    "updated-by": [
        {"DOI": "10.1016/s0140-6736(20)31290-3", "type": "expression_of_concern",
         "source": "retraction-watch"},
        {"DOI": "10.1016/s0140-6736(20)31249-6", "type": "correction",
         "source": "retraction-watch"},
        {"DOI": "10.1016/s0140-6736(20)31324-6", "type": "retraction",
         "source": "retraction-watch"},
        {"DOI": "10.1016/s0140-6736(20)31174-0", "type": "retraction", "source": "publisher"},
        {"DOI": "10.1016/s0140-6736(20)31528-2", "type": "erratum", "source": "publisher"},
    ],
    "update-to": [
        {"DOI": "10.1016/s0140-6736(20)31174-0", "type": "retraction", "source": "publisher"},
        {"DOI": "10.1016/s0140-6736(20)31290-3", "type": "retraction", "source": "publisher"},
    ],
}


def client(record):
    return CrossrefClient(_fetch=lambda d: record, _resolve=lambda d: True)


def check(record, **kw):
    ref = Reference(raw="", doi=record["DOI"], **kw)
    return check_reference(ref, client(record))


# --- the headline regression ------------------------------------------------


@pytest.mark.parametrize(
    "name, record",
    [("wakefield", WAKEFIELD), ("mehra_nejm", MEHRA_NEJM), ("mehra_lancet", MEHRA_LANCET)],
)
def test_real_retracted_papers_are_reported_as_retracted(name, record):
    """The bug this whole file exists for: these three all read `✓ Verified`."""
    assert _is_retracted(record) is True
    res = check(record)
    assert res.status == ERROR
    assert any("RETRACTED" in f.message for f in res.findings)


def test_the_field_the_code_reads_is_the_field_crossref_sends():
    """Guard the exact typo that caused it. `update-by` is not a Crossref field."""
    for record in (WAKEFIELD, MEHRA_NEJM, MEHRA_LANCET):
        assert "updated-by" in record
        assert "update-by" not in record


def test_wakefield_is_not_verified_clean():
    res = check(WAKEFIELD, title="Ileal-lymphoid-nodular hyperplasia, non-specific "
                                 "colitis, and pervasive developmental disorder in children",
                author="Wakefield", year=1998, journal="The Lancet")
    assert res.status == ERROR
    assert not any(f.severity == OK for f in res.findings)


# --- the notice must stay citable -------------------------------------------


def test_a_retraction_notice_is_not_itself_retracted():
    assert _is_retracted(WAKEFIELD_NOTICE) is False
    assert check(WAKEFIELD_NOTICE).status == OK


def test_null_updated_by_is_handled():
    """The live notice payload really does send `"updated-by": null`."""
    assert _classify_updates(WAKEFIELD_NOTICE) == []


# --- the notice DOI we surface ----------------------------------------------


def test_notice_doi_is_the_retraction_not_the_paper_itself():
    assert _retraction_notice(WAKEFIELD) == "10.1016/s0140-6736(10)60175-4"
    # The Lancet paper deposits its own DOI under `update-to`; never report that.
    notice = _retraction_notice(MEHRA_LANCET)
    assert notice != MEHRA_LANCET["DOI"]
    assert notice == "10.1016/s0140-6736(20)31324-6"


def test_no_crossmark_retraction_means_no_notice_doi():
    """MEHRA_NEJM is known retracted only via its title — we have no notice DOI
    to offer, and must not invent one from the expression-of-concern entry."""
    assert _retraction_notice(MEHRA_NEJM) is None


# --- title handling ---------------------------------------------------------


def test_retracted_prefix_is_stripped_before_title_comparison():
    """Regression: the "RETRACTED: " prefix made every correctly-cited retracted
    paper ALSO report a bogus title mismatch."""
    assert _crossref_title_candidates(MEHRA_LANCET)[0].startswith("Hydroxychloroquine or chloroquine")
    res = check(
        MEHRA_LANCET,
        title="Hydroxychloroquine or chloroquine with or without a macrolide for "
              "treatment of COVID-19: a multinational registry analysis",
        author="Mehra",
        year=2020,
    )
    assert not any("Title mismatch" in f.message for f in res.findings)
    assert res.status == ERROR  # still retracted, of course


def test_title_prefix_alone_is_enough_to_catch_a_retraction():
    """Many publishers mark ONLY the title; Crossmark can be entirely absent."""
    bare = {
        "DOI": "10.1/x",
        "title": ["RETRACTED: A fabricated trial"],
        "issued": {"date-parts": [[2020]]},
    }
    assert _is_retracted(bare) is True


@pytest.mark.parametrize(
    "title",
    [
        "RETRACTED: A study",
        "Retracted: A study",
        "RETRACTED ARTICLE: A study",
        "WITHDRAWN: A study",
        "Withdrawn: A study",
        "REMOVED: A study",
        "Temporary removal: A study",
        "RETRACTED - A study",
        "RETRACTED — A study",
    ],
)
def test_publisher_retraction_markers_are_recognised(title):
    assert _is_retracted({"DOI": "10.1/x", "title": [title]}) is True


@pytest.mark.parametrize(
    "title",
    [
        "Retraction of the fentanyl patch: a dosing study",  # topic, not a marker
        "A study of retracted papers in oncology",
        "Withdrawal symptoms after opioid cessation",  # 'Withdrawal' as a topic!
        "Retracted claims in the literature: a review",
        "Removal of the spleen: outcomes",
    ],
)
def test_papers_merely_about_retraction_are_not_flagged(title):
    """"Withdrawal symptoms after opioid cessation" is a real clinical title —
    the marker only counts as a prefix followed by a separator."""
    assert _is_retracted({"DOI": "10.1/x", "title": [title]}) is False


# --- the softer flags on a real record --------------------------------------


def test_lancet_paper_reports_retraction_only_not_its_four_other_notices():
    """MEHRA_LANCET has an EoC, a correction, an erratum AND two retractions.
    A reader needs one clear verdict, not five stacked findings."""
    res = check(MEHRA_LANCET)
    messages = [f.message for f in res.findings]
    assert sum("RETRACTED" in m for m in messages) == 1
    assert not any("correction" in m or "erratum" in m or "EXPRESSION" in m for m in messages)


def test_nejm_expression_of_concern_is_classified_when_not_retracted():
    """Strip the title marker and the EoC becomes the operative finding."""
    no_marker = dict(MEHRA_NEJM, title=["Cardiovascular Disease, Drug Therapy, and Mortality"])
    assert _is_retracted(no_marker) is False
    kinds = _classify_updates(no_marker)
    assert [(label, sev, code) for label, sev, _doi, code in kinds] == [
        ("has an EXPRESSION OF CONCERN against it", ERROR, "expression-of-concern")
    ]


# --- the `created` deposit-date trap ----------------------------------------


def test_wakefields_deposit_year_is_not_accepted_as_a_publication_year():
    """`created` is 2002 for this 1998 paper. Citing it as 2002 used to pass."""
    from citecheck.core import _crossref_years

    years = _crossref_years(WAKEFIELD)
    assert 1998 in years
    assert 2002 not in years


# --- the notice DOI must never be the record's own DOI -----------------------

# api.crossref.org/works/10.1007/s00132-015-3148-2 — a Springer record whose
# `updated-by` entries BOTH name the record's own DOI rather than the notice's.
# Sampling 400 live records with a retraction in `updated-by`, 185 of 186
# publisher-deposited entries self-referenced like this. It is the norm, not an
# oddity, so emitting the DOI unchecked printed "(retraction notice: <the paper
# itself>)" on nearly every publisher-deposited retraction.
SELF_REFERENCING = {
    "DOI": "10.1007/s00132-015-3148-2",
    "type": "journal-article",
    "title": ["Vergleich zweier Verfahren"],
    "issued": {"date-parts": [[2015]]},
    "updated-by": [
        {"DOI": "10.1007/s00132-015-3148-2", "type": "retraction",
         "source": "retraction-watch"},
        {"DOI": "10.1007/s00132-015-3148-2", "type": "retraction", "source": "publisher"},
    ],
}


def test_a_self_referencing_notice_doi_is_not_reported():
    assert _retraction_notice(SELF_REFERENCING) is None
    res = check(SELF_REFERENCING)
    assert res.status == ERROR
    msg = next(f.message for f in res.findings if "RETRACTED" in f.message)
    assert "retraction notice" not in msg  # better silent than circular
    assert SELF_REFERENCING["DOI"] not in msg


def test_self_reference_detection_is_case_insensitive():
    """Crossref lowercases DOIs inconsistently between fields."""
    msg = dict(SELF_REFERENCING, DOI="10.1007/S00132-015-3148-2")
    assert _retraction_notice(msg) is None


def test_a_real_notice_doi_wins_over_a_self_reference():
    """Skip the self-references, but still surface a genuine notice if present."""
    msg = dict(
        SELF_REFERENCING,
        **{
            "updated-by": [
                {"DOI": "10.1007/s00132-015-3148-2", "type": "retraction"},  # self
                {"DOI": "10.1007/s00132-099-9999-9", "type": "retraction"},  # real
            ]
        },
    )
    assert _retraction_notice(msg) == "10.1007/s00132-099-9999-9"


def test_softer_update_notices_also_skip_self_references():
    msg = {
        "DOI": "10.1/x",
        "title": ["A paper"],
        "updated-by": [{"DOI": "10.1/x", "type": "correction"}],
    }
    res = check(msg)
    assert res.status == WARNING
    assert "notice:" not in next(f.message for f in res.findings)
