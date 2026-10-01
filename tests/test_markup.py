"""JATS/HTML markup and Unicode hyphens in Crossref titles.

Wiley and other publishers deposit titles with <scp>, <sub>, <i> tags, entity
escapes and U+2010 hyphens. A correct plain-text citation used to score below
the 0.80 title threshold against them. The titles below are copied exactly
from live Crossref records (fetched 2026-10-01).
"""

import pytest

from citecheck.core import OK, CrossrefClient, _norm, _plain, check_reference
from citecheck.parsers import Reference

LIVE = [
    (
        "10.1111/resp.13144",
        "Treating <scp>OSA</scp>: <scp>C</scp>urrent and emerging therapies beyond "
        "<scp>CPAP</scp>",
        "Treating OSA: Current and emerging therapies beyond CPAP",
    ),
    (
        "10.1111/resp.70060",
        "Fixed <scp>CPAP</scp> at <scp>10 cmH<sub>2</sub>O</scp> for <scp>OSA</scp>: "
        "A One‐Size‐Fits‐All <scp>Approach</scp>?",
        "Fixed CPAP at 10 cmH2O for OSA: A One-Size-Fits-All Approach?",
    ),
    (
        "10.1111/jsr.14260",
        "Association between obstructive sleep apnea (<scp>OSA</scp>) and "
        "<scp>COVID</scp>‐19 severity",
        "Association between obstructive sleep apnea (OSA) and COVID-19 severity",
    ),
    (
        "10.1111/resp.13183",
        "Does remote monitoring change <scp>OSA</scp> management and <scp>CPAP</scp> "
        "adherence?",
        "Does remote monitoring change OSA management and CPAP adherence?",
    ),
    (
        "10.1111/jsr.13709",
        "Pre‐pandemic sleep reactivity prospectively predicts distress during the\n"
        "                    <scp>COVID</scp>\n                    ‐19 pandemic: "
        "The protective effect of insomnia treatment",
        "Pre-pandemic sleep reactivity prospectively predicts distress during the "
        "COVID-19 pandemic: The protective effect of insomnia treatment",
    ),
]


def client_for(record):
    return CrossrefClient(_fetch=lambda d: record, _resolve=lambda d: False)


@pytest.mark.parametrize("doi, crossref_title, cited", LIVE, ids=[d for d, _, _ in LIVE])
def test_plain_citation_of_a_marked_up_title_is_verified(doi, crossref_title, cited):
    record = {"DOI": doi, "title": [crossref_title], "issued": {"date-parts": [[2020]]}}
    res = check_reference(Reference(raw="", doi=doi, title=cited), client_for(record))
    assert res.status == OK, [f.message for f in res.findings]
    verified = res.findings[0].message
    assert "<scp>" not in verified and "<sub>" not in verified
    assert "\n" not in verified


def test_markup_is_stripped_and_entities_unescaped():
    assert _plain("<scp>COVID</scp>&#8208;19 &amp; <i>in vivo</i>") == "COVID‐19 & in vivo"


def test_text_that_merely_contains_angle_brackets_is_kept():
    assert _plain("Outcomes in adults aged <65 vs >65 years") == (
        "Outcomes in adults aged <65 vs >65 years"
    )
    assert _plain("a < b and c > d") == "a < b and c > d"


@pytest.mark.parametrize("dash", ["\u2010", "\u2011", "\u2012", "\u2013", "\u2212"])
def test_unicode_hyphens_fold_to_ascii(dash):
    assert _norm(f"COVID{dash}19") == "covid-19"


def test_container_entities_are_unescaped_before_comparison():
    record = {
        "DOI": "10.1000/mp.1",
        "title": ["A synthetic title"],
        "container-title": ["Multiculture &amp; Peace"],
        "issued": {"date-parts": [[2020]]},
    }
    ref = Reference(raw="", doi="10.1000/mp.1", journal="Multiculture & Peace")
    res = check_reference(ref, client_for(record))
    assert res.status == OK, [f.message for f in res.findings]


def test_a_truly_different_title_still_warns_through_the_markup():
    doi, crossref_title, _ = LIVE[0]
    record = {"DOI": doi, "title": [crossref_title]}
    ref = Reference(raw="", doi=doi, title="A completely unrelated paper about penguins")
    res = check_reference(ref, client_for(record))
    assert any(f.code == "title-mismatch" for f in res.findings)
    shown = next(f.message for f in res.findings if f.code == "title-mismatch")
    assert "<scp>" not in shown


def test_retracted_prefix_inside_markup_is_still_seen():
    record = {
        "DOI": "10.1000/r.1",
        "title": ["<scp>RETRACTED</scp>: A synthetic trial of something"],
        "issued": {"date-parts": [[2020]]},
    }
    ref = Reference(raw="", doi="10.1000/r.1", title="A synthetic trial of something")
    res = check_reference(ref, client_for(record))
    assert [f.code for f in res.findings] == ["retracted"]


def test_free_text_citation_of_a_title_of_short_words_is_not_flagged():
    """A title such as "Go or no go?" has no word of three letters or more to
    look for, so the free-text title check has nothing to judge and must stay
    quiet."""
    from citecheck.parsers import parse_text

    record = {"DOI": "10.1000/s.1", "title": ["Go or no go?"], "issued": {"date-parts": [[2020]]}}
    ref = parse_text("Lee K. Go or no go? Critical Care Synthesis. 2020. doi:10.1000/s.1")[0]
    res = check_reference(ref, client_for(record))
    assert [f.code for f in res.findings] == ["verified"]
