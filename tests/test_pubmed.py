"""PubMed cross-check tests. All use an injected transport — no network."""

import pytest

from citecheck.core import (
    CrossrefClient,
    PubMedClient,
    ERROR,
    OK,
    WARNING,
    check_reference,
    _pubmed_doi,
    _pubmed_is_retracted,
    _pubmed_record,
)
from citecheck.parsers import Reference

GOOD = {
    "DOI": "10.1371/journal.pone.0312345",
    "title": ["A study"],
    "author": [{"family": "Kim"}],
    "issued": {"date-parts": [[2024]]},
    "type": "journal-article",
}


def cr_client(records=None, resolves=False):
    records = records or {"10.1371/journal.pone.0312345": GOOD}
    return CrossrefClient(_fetch=lambda d: records.get(d), _resolve=lambda d: resolves)


def pm_client(records):
    return PubMedClient(_fetch=lambda p: records.get(p))


# --- record extraction ------------------------------------------------------

def test_pubmed_record_extraction():
    env = {"result": {"uids": ["123"], "123": {"uid": "123", "pubtype": ["Journal Article"]}}}
    assert _pubmed_record(env, "123")["uid"] == "123"
    assert _pubmed_record({"result": {"0": {"error": "bad id"}}}, "0") is None
    assert _pubmed_record({"result": {}}, "123") is None
    assert _pubmed_record("nonsense", "123") is None
    assert _pubmed_record({"no": "result"}, "123") is None


def test_pubmed_doi_extraction():
    rec = {"articleids": [{"idtype": "pubmed", "value": "123"},
                          {"idtype": "doi", "value": "10.1/ABC"}]}
    assert _pubmed_doi(rec) == "10.1/abc"  # normalised/lowercased
    assert _pubmed_doi({"articleids": []}) is None
    assert _pubmed_doi({"articleids": "weird"}) is None  # defensive


def test_pubmed_retraction_detection():
    assert _pubmed_is_retracted({"pubtype": ["Journal Article", "Retracted Publication"]}) is True
    # The retraction *notice* is not a retracted source.
    assert _pubmed_is_retracted({"pubtype": ["Retraction of Publication"]}) is False
    assert _pubmed_is_retracted({"pubtype": ["Journal Article"]}) is False
    assert _pubmed_is_retracted({"pubtype": "weird"}) is False  # defensive


# --- cross-check integration ------------------------------------------------

def test_pubmed_retraction_flags_error_even_if_crossref_clean():
    rec = {"pubtype": ["Retracted Publication"],
           "articleids": [{"idtype": "doi", "value": "10.1371/journal.pone.0312345"}]}
    ref = Reference(raw="", doi="10.1371/journal.pone.0312345", pmid="999")
    res = check_reference(ref, cr_client(), pubmed=pm_client({"999": rec}))
    assert res.status == ERROR
    assert any("RETRACTED according to PubMed" in f.message for f in res.findings)


def test_pmid_doi_mismatch_warns():
    rec = {"pubtype": ["Journal Article"],
           "articleids": [{"idtype": "doi", "value": "10.1/other"}]}
    ref = Reference(raw="", doi="10.1371/journal.pone.0312345", pmid="888")
    res = check_reference(ref, cr_client(), pubmed=pm_client({"888": rec}))
    assert any("PMID/DOI mismatch" in f.message for f in res.findings)
    # A mismatch is a warning, NOT escalated to an error.
    assert res.status == WARNING


def test_pmid_doi_match_no_warning():
    rec = {"pubtype": ["Journal Article"],
           "articleids": [{"idtype": "doi", "value": "10.1371/journal.pone.0312345"}]}
    ref = Reference(raw="", doi="10.1371/journal.pone.0312345", pmid="777")
    res = check_reference(ref, cr_client(), pubmed=pm_client({"777": rec}))
    assert not any("PMID/DOI mismatch" in f.message for f in res.findings)


def test_pmid_only_ref_gets_doi_suggestion():
    rec = {"pubtype": ["Journal Article"],
           "articleids": [{"idtype": "doi", "value": "10.1/found"}]}
    ref = Reference(raw="", pmid="666")  # no DOI
    res = check_reference(ref, cr_client(), pubmed=pm_client({"666": rec}))
    assert any("maps to DOI 10.1/found" in f.message for f in res.findings)


def test_pubmed_lookup_failure_is_inconclusive_warning():
    def boom(p):
        raise TimeoutError("pubmed down")

    ref = Reference(raw="", doi="10.1371/journal.pone.0312345", pmid="555")
    res = check_reference(ref, cr_client(), pubmed=PubMedClient(_fetch=boom))
    assert any(f.message.startswith("Lookup failed") for f in res.findings)


def test_pmid_not_in_pubmed_warns():
    ref = Reference(raw="", doi="10.1371/journal.pone.0312345", pmid="444")
    res = check_reference(ref, cr_client(), pubmed=pm_client({}))  # not found
    assert any("not found in PubMed" in f.message for f in res.findings)


def test_no_pubmed_client_skips_crosscheck():
    ref = Reference(raw="", doi="10.1371/journal.pone.0312345", pmid="999")
    res = check_reference(ref, cr_client())  # no pubmed
    assert res.status == OK


def test_ref_without_pmid_never_calls_pubmed():
    calls = []

    def track(p):
        calls.append(p)
        return None

    ref = Reference(raw="", doi="10.1371/journal.pone.0312345")  # no PMID
    check_reference(ref, cr_client(), pubmed=PubMedClient(_fetch=track))
    assert calls == []


def test_pubmed_fetch_cached_per_pmid():
    calls = []

    def track(p):
        calls.append(p)
        return {"pubtype": ["Journal Article"]}

    client = PubMedClient(_fetch=track)
    for _ in range(3):
        client.fetch("123")
    assert calls == ["123"]


# --- network transport (monkeypatched, no sockets) --------------------------

def test_pubmed_network_parses_esummary(monkeypatch):
    import io
    import urllib.request

    body = (
        '{"result": {"uids": ["16060722"], "16060722": '
        '{"uid": "16060722", "pubtype": ["Journal Article"], '
        '"articleids": [{"idtype": "doi", "value": "10.1/x"}]}}}'
    )

    class FakeResp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return body.encode("utf-8")

    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=None: FakeResp())
    rec = PubMedClient().fetch("16060722")
    assert rec is not None and _pubmed_doi(rec) == "10.1/x"


def test_pubmed_network_404_returns_none(monkeypatch):
    import urllib.error
    import urllib.request

    def fake(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 404, "NF", {}, None)

    monkeypatch.setattr(urllib.request, "urlopen", fake)
    assert PubMedClient().fetch("1") is None


def test_crosscheck_tolerates_non_dict_record():
    # Defense in depth: a transport that violates the dict|None contract must not
    # crash the run (mirrors the Crossref shape guard).
    ref = Reference(raw="", doi="10.1371/journal.pone.0312345", pmid="123")
    res = check_reference(ref, cr_client(), pubmed=PubMedClient(_fetch=lambda p: "oops-a-string"))
    assert res.status in (OK, WARNING, ERROR)  # returned, did not raise
    assert any("could not cross-check" in f.message for f in res.findings)
