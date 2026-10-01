import pytest

from citecheck.core import (
    CrossrefClient,
    ERROR,
    OK,
    WARNING,
    _is_retracted,
    check_reference,
)
from citecheck.parsers import Reference


def make_client(records, resolves=False):
    """A CrossrefClient whose transport is a dict: doi -> message | None.

    `resolves` controls the injected doi.org handle check for DOIs not in the
    Crossref records (True => "resolves but not in Crossref").
    """
    return CrossrefClient(
        _fetch=lambda doi: records.get(doi),
        _resolve=lambda doi: resolves,
    )


GOOD = {
    "DOI": "10.1371/journal.pone.0312345",
    "title": ["Sleep as a transdiagnostic node in BELL disorders"],
    "author": [{"family": "Kim", "given": "Gildong"}],
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


# --- retraction detection (real Crossref shapes) ----------------------------

def test_retraction_via_updated_by():
    """A retracted *article* carries `updated-by` (NOT `update-by`, which is not
    a Crossref field at all — see tests/test_retraction_real_shapes.py)."""
    msg = dict(GOOD, **{"updated-by": [{"type": "retraction", "DOI": "10.1/notice"}]})
    ref = Reference(raw="", doi="10.1371/journal.pone.0312345")
    res = check_reference(ref, make_client({ref.doi: msg}))
    assert res.status == ERROR
    finding = next(f for f in res.findings if "RETRACTED" in f.message)
    assert "10.1/notice" in finding.message  # notice DOI surfaced


def test_update_to_alone_is_not_a_retraction():
    """`update-to` says "this record updates X" — it does not make the record
    retracted. Elsevier deposits it symmetrically (on the retracted paper AND on
    the notice), so it identifies neither side; only `updated-by` is trusted."""
    msg = dict(GOOD, **{"update-to": [{"type": "retraction", "DOI": "10.1/orig"}]})
    ref = Reference(raw="", doi="10.1371/journal.pone.0312345")
    res = check_reference(ref, make_client({ref.doi: msg}))
    assert res.status == OK


def test_retraction_via_relation_key():
    msg = dict(GOOD, relation={"is-retracted-by": [{"id": "10.1/x"}]})
    ref = Reference(raw="", doi="10.1371/journal.pone.0312345")
    res = check_reference(ref, make_client({ref.doi: msg}))
    assert res.status == ERROR


def test_plain_article_not_retracted():
    ref = Reference(raw="", doi="10.1371/journal.pone.0312345")
    res = check_reference(ref, make_client({ref.doi: GOOD}))
    assert not any("RETRACTED" in f.message for f in res.findings)


# --- year comparison --------------------------------------------------------

def test_year_matches_any_date_field():
    """Online 2023 / print 2024: citing either year must not warn."""
    msg = dict(
        GOOD,
        **{
            "published-online": {"date-parts": [[2023, 12, 1]]},
            "published-print": {"date-parts": [[2024, 2, 1]]},
        },
    )
    del msg["issued"]
    for cited in (2023, 2024):
        ref = Reference(raw="", doi="10.1371/journal.pone.0312345", year=cited)
        res = check_reference(ref, make_client({ref.doi: msg}))
        assert not any("Year mismatch" in f.message for f in res.findings), cited


def test_year_mismatch_still_detected():
    ref = Reference(raw="", doi="10.1371/journal.pone.0312345", year=1990)
    res = check_reference(ref, make_client({ref.doi: GOOD}))
    assert res.status == WARNING
    assert any("Year mismatch" in f.message for f in res.findings)


# --- subtitle handling ------------------------------------------------------

def test_subtitle_not_a_false_title_mismatch():
    msg = dict(GOOD, title=["Deep learning"], subtitle=["a comprehensive review of methods"])
    ref = Reference(
        raw="",
        doi="10.1371/journal.pone.0312345",
        title="Deep learning: a comprehensive review of methods",
    )
    res = check_reference(ref, make_client({ref.doi: msg}))
    assert not any("Title mismatch" in f.message for f in res.findings)


# --- author diacritics ------------------------------------------------------

def test_author_diacritics_folded():
    msg = dict(GOOD, author=[{"family": "Müller"}])
    ref = Reference(raw="", doi="10.1371/journal.pone.0312345", author="Muller")
    res = check_reference(ref, make_client({ref.doi: msg}))
    assert not any("author mismatch" in f.message.lower() for f in res.findings)


def test_author_mismatch_detected():
    msg = dict(GOOD, author=[{"family": "Anderson"}])
    ref = Reference(raw="", doi="10.1371/journal.pone.0312345", author="Zhang")
    res = check_reference(ref, make_client({ref.doi: msg}))
    assert any("author mismatch" in f.message.lower() for f in res.findings)


def test_author_matches_any_listed_author():
    msg = dict(GOOD, author=[{"family": "Anderson"}, {"family": "Kim"}])
    ref = Reference(raw="", doi="10.1371/journal.pone.0312345", author="Kim")
    res = check_reference(ref, make_client({ref.doi: msg}))
    assert not any("author mismatch" in f.message.lower() for f in res.findings)


# --- resolvable-but-not-in-crossref -----------------------------------------

def test_unknown_but_resolvable_is_warning_not_error():
    ref = Reference(raw="", doi="10.5281/zenodo.123456")
    res = check_reference(ref, make_client({}, resolves=True))
    assert res.status == WARNING
    assert any("not in Crossref" in f.message for f in res.findings)


def test_unknown_and_unresolvable_is_error():
    ref = Reference(raw="", doi="10.9999/nope")
    res = check_reference(ref, make_client({}, resolves=False))
    assert res.status == ERROR
    assert any("does not resolve" in f.message for f in res.findings)


# --- malformed records must not crash the batch -----------------------------

import pytest


@pytest.mark.parametrize(
    "bad",
    [
        {"update-to": "x"},                       # str where list expected
        {"title": "Hello"},                       # str where list expected
        {"author": "nope"},
        {"author": [{}]},
        {"issued": {"date-parts": [[]]}},
        {"issued": {"date-parts": [[None]]}},
        {"issued": None},
        {"relation": "weird"},
        {"update-by": [None, "x", 3]},
    ],
)
def test_malformed_record_does_not_crash(bad):
    msg = dict(GOOD, **bad)
    ref = Reference(raw="", doi="10.1371/journal.pone.0312345", title="X", year=2024, author="Kim")
    res = check_reference(ref, make_client({ref.doi: msg}))
    assert res.status in (OK, WARNING, ERROR)  # returned a result, did not raise


# --- caching ----------------------------------------------------------------

def test_fetch_is_cached_per_doi():
    calls = []

    def counting_fetch(doi):
        calls.append(doi)
        return GOOD

    client = CrossrefClient(_fetch=counting_fetch, _resolve=lambda d: False)
    doi = "10.1371/journal.pone.0312345"
    for _ in range(3):
        check_reference(Reference(raw="", doi=doi), client)
    assert calls == [doi]  # fetched once, then served from cache


# --- user-agent / mailto ----------------------------------------------------

def test_default_user_agent_has_no_real_email():
    ua = CrossrefClient(mailto=None).user_agent
    assert "example.com" in ua  # placeholder only, no user address


def test_mailto_lands_in_user_agent():
    ua = CrossrefClient(mailto="me@lab.org").user_agent
    assert "me@lab.org" in ua


# --- retraction notice DOI fall-through -------------------------------------

def test_retraction_notice_falls_through_to_the_doi_bearing_entry():
    """Several `updated-by` entries; take the first retraction that has a DOI."""
    from citecheck.core import _retraction_notice
    msg = {
        "updated-by": [
            {"type": "retraction"},  # matches but no DOI
            {"type": "retraction", "DOI": "10.1/notice"},
        ]
    }
    assert _retraction_notice(msg) == "10.1/notice"


def test_retraction_notice_never_reports_the_papers_own_doi():
    """Regression: reading `update-to` as a fallback reported the retracted
    paper's OWN doi as its "retraction notice" — Elsevier deposits both."""
    from citecheck.core import _retraction_notice
    msg = {
        "updated-by": [{"type": "retraction"}],  # no DOI to offer
        "update-to": [{"type": "retraction", "DOI": "10.1/its-own-paper"}],
    }
    assert _retraction_notice(msg) is None


def test_retraction_notice_relation_narrowed():
    # is-retraction-of identifies the *notice*, not a retracted paper.
    from citecheck.core import _is_retracted
    assert _is_retracted({"relation": {"is-retraction-of": [{"id": "x"}]}}) is False
    assert _is_retracted({"relation": {"is-retracted-by": [{"id": "x"}]}}) is True


# --- _crossref_years shapes -------------------------------------------------

def test_crossref_years_shapes():
    from citecheck.core import _crossref_years
    assert _crossref_years({"issued": {"date-parts": [[2020, 1, 1]]}}) == {2020}
    assert _crossref_years({"issued": {"date-parts": [["2020"]]}}) == {2020}
    assert _crossref_years(
        {"published-print": {"date-parts": [[2024]]}, "issued": {"date-parts": [[2023]]}}
    ) == {2023, 2024}
    assert _crossref_years({"issued": {"date-parts": [[]]}}) == set()


# --- resolve caching --------------------------------------------------------

def test_resolve_is_cached_per_doi():
    calls = []

    def counting_resolve(doi):
        calls.append(doi)
        return True

    client = CrossrefClient(_fetch=lambda d: None, _resolve=counting_resolve)
    doi = "10.5281/zenodo.1"
    for _ in range(3):
        check_reference(Reference(raw="", doi=doi), client)
    assert calls == [doi]


# --- real network transport (monkeypatched, no sockets) ---------------------

def test_fetch_network_404_returns_none(monkeypatch):
    import urllib.error
    import urllib.request

    def fake_urlopen(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 404, "Not Found", {}, None)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    client = CrossrefClient()
    assert client.fetch("10.9999/nope") is None  # 404 => None, no retry


def test_fetch_network_retries_then_raises(monkeypatch):
    import urllib.error
    import urllib.request
    import time as _time

    attempts = []

    def fake_urlopen(req, timeout=None):
        attempts.append(1)
        raise urllib.error.URLError("boom")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(_time, "sleep", lambda s: None)
    client = CrossrefClient(retries=2)
    import pytest as _pytest
    with _pytest.raises(urllib.error.URLError):
        client.fetch("10.1/x")
    assert len(attempts) == 3  # initial + 2 retries


def test_resolve_network_status(monkeypatch):
    """doi.org handle API: responseCode 1 means registered, HTTP 404 means not."""
    import urllib.error
    import urllib.request

    class FakeResp:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b'{"responseCode": 1, "handle": "10.1/x", "values": []}'

    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=None: FakeResp())
    assert CrossrefClient().resolve("10.1/x") is True

    def http404(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 404, "NF", {}, None)

    monkeypatch.setattr(urllib.request, "urlopen", http404)
    assert CrossrefClient().resolve("10.9/none") is False


# --- text-mode title containment (hyphen/punctuation robustness) -------------

def test_text_title_check_tolerates_punctuation():
    from citecheck.parsers import Reference as R
    msg = dict(GOOD, title=["Neural networks: a review of deep-learning methods for COVID-19"])
    ref = R(
        raw="Kim H. Neural-networks — a review of deep learning methods for COVID-19. Nature. 2021. doi:10.1/x",
        doi="10.1/x",
        heuristic_fields=True,
    )
    res = check_reference(ref, make_client({"10.1/x": msg}))
    assert not any("wrong DOI" in f.message for f in res.findings)


def test_text_title_check_flags_unrelated_title():
    from citecheck.parsers import Reference as R
    msg = dict(GOOD, title=["Penguin foraging behaviour in Antarctic waters"])
    ref = R(
        raw="Kim H. A randomized trial of aspirin in stroke patients. Lancet. 2019. doi:10.1/x",
        doi="10.1/x",
        heuristic_fields=True,
    )
    res = check_reference(ref, make_client({"10.1/x": msg}))
    assert any("wrong DOI" in f.message for f in res.findings)


# --- journal / container mismatch -------------------------------------------

def test_journal_mismatch_warns():
    msg = dict(GOOD, **{"container-title": ["The Lancet"]})
    ref = Reference(raw="", doi="10.1371/journal.pone.0312345",
                    title=GOOD["title"][0], author="Kim", year=2024, journal="Nature")
    res = check_reference(ref, make_client({ref.doi: msg}))
    assert any("Journal mismatch" in f.message for f in res.findings)


def test_journal_abbreviation_not_flagged():
    msg = dict(GOOD, **{"container-title": ["The New England Journal of Medicine"]})
    ref = Reference(raw="", doi="10.1371/journal.pone.0312345",
                    title=GOOD["title"][0], author="Kim", year=2024, journal="N Engl J Med")
    res = check_reference(ref, make_client({ref.doi: msg}))
    assert not any("Journal mismatch" in f.message for f in res.findings)


def test_journal_leading_article_not_flagged():
    msg = dict(GOOD, **{"container-title": ["The Lancet"]})
    ref = Reference(raw="", doi="10.1371/journal.pone.0312345",
                    title=GOOD["title"][0], author="Kim", year=2024, journal="Lancet")
    res = check_reference(ref, make_client({ref.doi: msg}))
    assert not any("Journal mismatch" in f.message for f in res.findings)


def test_journal_matches_short_container_title():
    msg = dict(GOOD, **{"container-title": ["Journal of Clinical Oncology"],
                        "short-container-title": ["J Clin Oncol"]})
    ref = Reference(raw="", doi="10.1371/journal.pone.0312345",
                    title=GOOD["title"][0], author="Kim", year=2024, journal="J Clin Oncol")
    res = check_reference(ref, make_client({ref.doi: msg}))
    assert not any("Journal mismatch" in f.message for f in res.findings)


def test_journal_check_skipped_for_heuristic_refs():
    # Free-text refs never carry a parsed journal, and heuristic refs skip the
    # structured comparisons entirely — no journal false alarm.
    msg = dict(GOOD, **{"container-title": ["The Lancet"]})
    ref = Reference(raw="Nature paper", doi="10.1371/journal.pone.0312345",
                    journal="Nature", heuristic_fields=True)
    res = check_reference(ref, make_client({ref.doi: msg}))
    assert not any("Journal mismatch" in f.message for f in res.findings)


def test_no_doi_message_mentions_pmid():
    ref = Reference(raw="", pmid="16060722")
    res = check_reference(ref, make_client({}))
    assert res.status == WARNING
    assert "16060722" in res.findings[0].message


# --- journal matcher unit cases ---------------------------------------------

import pytest as _pytest_j


@_pytest_j.mark.parametrize("cited,cand,should_match", [
    ("Lancet", "The Lancet", True),
    ("The Lancet", "Lancet", True),
    ("PLoS ONE", "PLOS ONE", True),
    ("J Clin Oncol", "Journal of Clinical Oncology", True),
    ("N Engl J Med", "The New England Journal of Medicine", True),
    ("Nature", "The Lancet", False),
    ("Cancer", "Cancer Research", False),
    ("Circulation", "Circulation Research", False),
])
def test_journal_matcher_cases(cited, cand, should_match):
    from citecheck.core import _journal_matches
    assert _journal_matches(cited, [cand], 0.82) is should_match


@_pytest_j.mark.parametrize("cited,full", [
    ("Am J Med", "American Journal of Medicine"),
    ("Am J Cardiol", "American Journal of Cardiology"),
    ("Br J Cancer", "British Journal of Cancer"),
    ("Ann Intern Med", "Annals of Internal Medicine"),
    ("Eur Heart J", "European Heart Journal"),
    ("J Am Coll Cardiol", "Journal of the American College of Cardiology"),
])
def test_common_medical_abbreviations_not_flagged(cited, full):
    # These short-token ISO-4 abbreviations must NOT read as journal mismatches.
    from citecheck.core import _journal_matches
    assert _journal_matches(cited, [full], 0.82) is True


def test_single_token_abbrev_matches_short_container_title():
    from citecheck.core import _journal_matches
    # "Circ" only matches via short-container-title, which real records carry.
    assert _journal_matches("Circ", ["Circulation", "Circ"], 0.82) is True
    # But a distinct one-word journal is still a mismatch.
    assert _journal_matches("Cancer", ["Cancer Research"], 0.82) is False


def test_journal_matcher_empty_cited_returns_true():
    from citecheck.core import _journal_matches
    # Nothing comparable — must not emit a spurious mismatch.
    assert _journal_matches("", ["Nature"], 0.82) is True
    assert _journal_matches("   ", ["Nature"], 0.82) is True


def test_is_abbrev_single_word_guard():
    from citecheck.core import _is_abbrev_of
    # A single abbreviation word must not match everything.
    assert _is_abbrev_of("Med", "Medicine") is False


@_pytest_j.mark.parametrize("cited,full", [
    ("J Natl Cancer Inst", "Journal of the National Cancer Institute"),  # Natl (contraction)
    ("Dtsch Arztebl Int", "Deutsches Arzteblatt International"),          # Dtsch (contraction)
    ("Proc Natl Acad Sci", "Proceedings of the National Academy of Sciences"),
])
def test_contracted_iso4_abbreviations_not_flagged(cited, full):
    # Abbreviations that DROP interior letters ("Natl"->"National") must match,
    # not just prefix abbreviations.
    from citecheck.core import _journal_matches
    assert _journal_matches(cited, [full], 0.82) is True


def test_word_contraction_helper():
    from citecheck.core import _is_word_contraction
    assert _is_word_contraction("natl", "national") is True   # drops interior
    assert _is_word_contraction("engl", "england") is True    # prefix
    assert _is_word_contraction("dtsch", "deutsche") is True
    assert _is_word_contraction("xyz", "national") is False    # wrong first letter
    assert _is_word_contraction("cat", "dog") is False


def test_pnas_with_country_suffix_matches_via_short_container_title():
    # A citation carrying the country suffix ("U S A") matches PNAS's real
    # short-container-title even when the full container title omits it.
    from citecheck.core import _journal_matches
    cands = ["Proceedings of the National Academy of Sciences", "Proc Natl Acad Sci U S A"]
    assert _journal_matches("Proc Natl Acad Sci U S A", cands, 0.82) is True


# --- Crossref network transport (monkeypatched, no sockets) ------------------
#
# These pin the *response parsing*, which was until now the only layer of this
# project with no test at all — and it is precisely the layer that produced the
# headline bug. `update-by` vs `updated-by` was a single wrong key read out of a
# JSON envelope; every fixture in the suite was built around the same wrong key,
# so ~180 tests were green against a field the API never sends. The defence
# against a repeat is a test that asserts on a *verbatim live-shaped envelope*
# rather than on the code's own idea of one.
#
# PubMed's identical path was already covered (tests/test_pubmed.py); Crossref's
# was not, even though Crossref is the tool's primary source.


class _FakeResp:
    """Minimal stand-in for the urlopen context manager."""

    def __init__(self, body: bytes, status: int = 200):
        self._body = body
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self._body


def _fake_urlopen(monkeypatch, body, status=200):
    """Point urllib at a canned response and record the requests made."""
    import urllib.request

    seen = []

    def fake(req, timeout=None):
        seen.append(req)
        return _FakeResp(body.encode("utf-8") if isinstance(body, str) else body, status)

    monkeypatch.setattr(urllib.request, "urlopen", fake)
    return seen


def test_fetch_network_reads_the_message_envelope(monkeypatch):
    """`fetch` must return `data["message"]`, not the whole envelope.

    Nothing previously asserted this. Returning `data` instead would hand every
    downstream check a dict whose keys are `status`/`message-type`/`message` —
    so `updated-by`, `title` and `DOI` would all be missing, retraction
    detection would silently go dark, and the tool would report a clean pass.
    That is the `update-by` failure exactly.
    """
    body = (
        '{"status": "ok", "message-type": "work", "message": '
        '{"DOI": "10.1/x", "title": ["A study"], '
        '"updated-by": [{"type": "retraction", "DOI": "10.1/notice"}]}}'
    )
    _fake_urlopen(monkeypatch, body)

    msg = CrossrefClient().fetch("10.1/x")

    assert msg == {
        "DOI": "10.1/x",
        "title": ["A study"],
        "updated-by": [{"type": "retraction", "DOI": "10.1/notice"}],
    }
    # And the retraction signal survives the round trip end-to-end.
    assert _is_retracted(msg) is True


def test_fetch_network_404_is_not_in_crossref_not_an_error(monkeypatch):
    """A 404 means "not in the works index" and must return None, not raise."""
    import urllib.error
    import urllib.request

    def fake(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 404, "Not Found", {}, None)

    monkeypatch.setattr(urllib.request, "urlopen", fake)
    assert CrossrefClient().fetch("10.1/missing") is None


def test_fetch_network_quotes_the_doi_into_the_path(monkeypatch):
    """A DOI's '/' must be percent-encoded, or the URL path is wrong."""
    seen = _fake_urlopen(monkeypatch, '{"message": {"DOI": "10.1/x"}}')
    CrossrefClient().fetch("10.1000/abc/def")
    assert seen[0].full_url.endswith("10.1000%2Fabc%2Fdef")


def test_search_network_reads_message_items_and_drops_non_dicts(monkeypatch):
    """`search` must read `data["message"]["items"]` and skip junk entries."""
    body = (
        '{"status": "ok", "message": {"items": '
        '[{"DOI": "10.1/a"}, "junk", null, {"DOI": "10.1/b"}]}}'
    )
    _fake_urlopen(monkeypatch, body)

    items = CrossrefClient()._search_network("some query")

    assert items == [{"DOI": "10.1/a"}, {"DOI": "10.1/b"}]


def test_search_network_404_returns_empty_list(monkeypatch):
    import urllib.error
    import urllib.request

    def fake(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 404, "NF", {}, None)

    monkeypatch.setattr(urllib.request, "urlopen", fake)
    assert CrossrefClient()._search_network("q") == []


def test_search_network_requests_the_fields_retraction_detection_needs(monkeypatch):
    """`select` must keep `updated-by`/`relation`/`type`.

    Trimming the payload is an optimisation; dropping these three turns a
    suggested candidate's retraction invisible, and `--suggest-doi` would
    recommend citing a retracted paper (it once did).
    """
    seen = _fake_urlopen(monkeypatch, '{"message": {"items": []}}')
    CrossrefClient()._search_network("q")
    url = seen[0].full_url
    for required in ("updated-by", "relation", "type"):
        assert required in url


def test_resolve_network_reraises_non_404_http_errors(monkeypatch):
    """A doi.org 5xx/429 is transient and MUST NOT read as "does not resolve".

    If this `raise` ever became `return False`, a momentary doi.org outage would
    turn every not-in-Crossref DOI into a hard `doi-not-resolving` ERROR — the
    tool telling the author their correct DOI is a typo. The invariant was
    documented in a comment and protected by nothing.
    """
    import urllib.error
    import urllib.request

    def fake(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 503, "Service Unavailable", {}, None)

    monkeypatch.setattr(urllib.request, "urlopen", fake)
    with pytest.raises(urllib.error.HTTPError):
        CrossrefClient().resolve("10.1/x")


def test_resolve_network_404_means_not_registered(monkeypatch):
    import urllib.error
    import urllib.request

    def fake(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 404, "NF", {}, None)

    monkeypatch.setattr(urllib.request, "urlopen", fake)
    assert CrossrefClient().resolve("10.1/x") is False


def test_resolve_network_asks_the_handle_api_with_get(monkeypatch):
    """The resolver asks the handle server and never follows a publisher redirect.

    A HEAD to https://doi.org/<doi> follows the redirect to the landing page, so
    a landing page with a broken certificate made a registered DOI inconclusive,
    and a landing page that answers 404 to HEAD read as "does not resolve".
    """
    seen = _fake_urlopen(monkeypatch, '{"responseCode": 1, "handle": "10.1/x"}')
    assert CrossrefClient().resolve("10.1/x") is True
    assert "/api/handles/" in seen[0].full_url
    assert seen[0].full_url.startswith("https://doi.org/api/handles/")
    assert seen[0].get_method() == "GET"


def test_resolve_network_keeps_parentheses_in_the_handle_url(monkeypatch):
    seen = _fake_urlopen(monkeypatch, '{"responseCode": 1}')
    CrossrefClient().resolve("10.1016/s0140-6736(97)11096-0")
    assert seen[0].full_url.endswith("/api/handles/10.1016/s0140-6736(97)11096-0")


@pytest.mark.parametrize("code", [100, 200])
def test_resolve_network_handle_not_found_codes_mean_not_registered(monkeypatch, code):
    """responseCode 100 is "handle not found", 200 is "no values for the handle"."""
    _fake_urlopen(monkeypatch, '{"responseCode": %d, "handle": "10.1/x"}' % code)
    assert CrossrefClient().resolve("10.1/x") is False


@pytest.mark.parametrize(
    "body",
    [
        b"<html>Service temporarily unavailable</html>",  # not JSON
        b'{"responseCode": 2, "message": "Error"}',  # handle server error
        b'{"handle": "10.1/x"}',  # no responseCode at all
        b'["responseCode", 1]',  # JSON, wrong shape
    ],
)
def test_resolve_network_unexpected_answers_raise(monkeypatch, body):
    """Anything that is not a clear yes or no must be inconclusive, never False."""
    _fake_urlopen(monkeypatch, body)
    with pytest.raises(Exception):
        CrossrefClient().resolve("10.1/x")


def test_crossref_404_then_doi_org_503_is_lookup_failed_end_to_end(monkeypatch):
    """Through the real transports: Crossref says 404, doi.org is down."""
    import urllib.error
    import urllib.request

    def fake(req, timeout=None):
        if "api.crossref.org" in req.full_url:
            raise urllib.error.HTTPError(req.full_url, 404, "Not Found", {}, None)
        raise urllib.error.HTTPError(req.full_url, 503, "Service Unavailable", {}, None)

    monkeypatch.setattr(urllib.request, "urlopen", fake)
    res = check_reference(Reference(raw="", doi="10.5281/zenodo.0000001"), CrossrefClient())
    assert [f.code for f in res.findings] == ["lookup-failed"]
    assert res.status == WARNING


def test_crossref_404_then_handle_registered_is_doi_not_in_crossref(monkeypatch):
    import urllib.error
    import urllib.request

    def fake(req, timeout=None):
        if "api.crossref.org" in req.full_url:
            raise urllib.error.HTTPError(req.full_url, 404, "Not Found", {}, None)
        return _FakeResp(b'{"responseCode": 1, "handle": "10.5281/zenodo.0000001"}')

    monkeypatch.setattr(urllib.request, "urlopen", fake)
    res = check_reference(Reference(raw="", doi="10.5281/zenodo.0000001"), CrossrefClient())
    assert [f.code for f in res.findings] == ["doi-not-in-crossref"]


def test_transient_doi_org_failure_becomes_lookup_failed_not_a_hard_error():
    """End-to-end consequence of the invariant above: exit 3, never exit 1."""

    def boom(doi):
        raise TimeoutError("doi.org timed out")

    client = CrossrefClient(_fetch=lambda d: None, _resolve=boom)
    res = check_reference(Reference(raw="", doi="10.1/x"), client)

    codes = [f.code for f in res.findings]
    assert codes == ["lookup-failed"]
    assert "doi-not-resolving" not in codes


# --- long-title similarity must not collapse (difflib autojunk) -------------
#
# difflib's autojunk heuristic kicks in at 200 elements and treats common
# characters as junk, which is right for diffing source code and wrong for
# comparing prose. Clinical trial titles routinely pass 200 characters.

LONG_TRIAL_TITLE = (
    "Effect of a multifaceted quality improvement intervention on the incidence of "
    "ventilator-associated pneumonia in critically ill adults admitted to intensive "
    "care units: a stepped-wedge cluster-randomised controlled trial"
)


def test_long_title_similarity_is_not_deflated_by_autojunk():
    """The same title with the hyphens dropped must still read as the same title."""
    assert len(LONG_TRIAL_TITLE) > 200
    cited = LONG_TRIAL_TITLE.replace("-", " ")
    from citecheck.core import _similar

    assert _similar(cited, LONG_TRIAL_TITLE) > 0.95


def test_long_title_similarity_is_continuous_across_the_200_char_boundary():
    """The score must not jump when the Crossref title crosses 200 characters.

    Regression: truncating one pair at 199 vs 200 characters scored 0.930 vs
    0.856 — a cliff produced purely by difflib's internal threshold, not by any
    difference in the text.
    """
    from citecheck.core import _similar

    cited = (
        "Effect of a multifaceted quality intervention on the incidence of "
        "ventilator-associated pneumonia in critically ill adults admitted to "
        "intensive care units: a cluster-randomised controlled trial"
    )
    at_199 = _similar(cited[: int(199 * 0.87)], LONG_TRIAL_TITLE[:199])
    at_200 = _similar(cited[: int(200 * 0.87)], LONG_TRIAL_TITLE[:200])
    assert abs(at_199 - at_200) < 0.02


def test_long_title_does_not_produce_a_false_title_mismatch():
    """End-to-end: the visible symptom was a bogus warning on a correct citation."""
    record = {
        "DOI": "10.1/trial",
        "title": [LONG_TRIAL_TITLE],
        "author": [{"family": "Kim"}],
        "issued": {"date-parts": [[2021]]},
    }
    ref = Reference(
        raw="", doi="10.1/trial", title=LONG_TRIAL_TITLE.replace("-", " "),
        author="Kim", year=2021,
    )
    res = check_reference(ref, make_client({"10.1/trial": record}))
    assert [f.code for f in res.findings] == ["verified"]


def test_a_genuinely_different_long_title_still_mismatches():
    """The fix must not blunt the check it repairs."""
    record = {
        "DOI": "10.1/trial",
        "title": [LONG_TRIAL_TITLE],
        "author": [{"family": "Kim"}],
        "issued": {"date-parts": [[2021]]},
    }
    ref = Reference(
        raw="", doi="10.1/trial", author="Kim", year=2021,
        title="A completely unrelated paper about cardiology outcomes in outpatients",
    )
    res = check_reference(ref, make_client({"10.1/trial": record}))
    assert "title-mismatch" in [f.code for f in res.findings]
