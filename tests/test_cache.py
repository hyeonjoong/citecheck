"""The on-disk lookup cache.

Two invariants dominate: (1) a cache may never change a *verdict*, only the time
taken to reach it, and (2) it must expire, because the tool exists to catch a
*newly* retracted reference and a stale clean pass is the one unacceptable
failure. Everything else here is "a broken cache degrades to no cache".
"""

import json

import pytest

from citecheck.core import CrossrefClient, DiskCache, PubMedClient, _MISS

RECORD = {"DOI": "10.1/x", "title": ["A paper"], "author": [{"family": "Kim"}]}


class Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


def cache(tmp_path, clock=None, ttl=7 * 24 * 3600):
    return DiskCache(tmp_path / "c.json", ttl_seconds=ttl, _now=clock or Clock())


# --- basic behaviour --------------------------------------------------------


def test_miss_is_distinct_from_a_cached_none(tmp_path):
    """`None` is a real answer ("not in Crossref"), so it must be cacheable and
    must not be confused with "not cached" — else every unknown DOI re-fetches."""
    c = cache(tmp_path)
    assert c.get("k") is _MISS
    c.set("k", None)
    assert c.get("k") is None
    assert c.get("k") is not _MISS


def test_roundtrip_through_disk(tmp_path):
    c = cache(tmp_path)
    c.set("crossref:fetch:10.1/x", RECORD)
    assert c.save() is True
    assert DiskCache(tmp_path / "c.json", _now=Clock()).get("crossref:fetch:10.1/x") == RECORD


def test_save_is_a_noop_when_nothing_changed(tmp_path):
    c = cache(tmp_path)
    assert c.save() is True
    assert not (tmp_path / "c.json").exists()  # no file written for an empty run


def test_save_creates_missing_parent_directories(tmp_path):
    c = DiskCache(tmp_path / "deep" / "nested" / "c.json", _now=Clock())
    c.set("k", 1)
    assert c.save() is True
    assert (tmp_path / "deep" / "nested" / "c.json").exists()


def test_save_leaves_no_temp_files_behind(tmp_path):
    c = cache(tmp_path)
    c.set("k", RECORD)
    c.save()
    assert [p.name for p in tmp_path.iterdir()] == ["c.json"]


# --- expiry -----------------------------------------------------------------


def test_entry_expires_after_the_ttl(tmp_path):
    clock = Clock()
    c = cache(tmp_path, clock, ttl=100)
    c.set("k", RECORD)
    clock.t += 99
    assert c.get("k") == RECORD
    clock.t += 2  # now 101s old
    assert c.get("k") is _MISS


def test_a_newly_retracted_paper_is_not_hidden_by_an_expired_cache(tmp_path):
    """The scenario the TTL exists for, end to end."""
    clock = Clock()
    path = tmp_path / "c.json"
    clean = dict(RECORD)
    retracted = dict(RECORD, **{"updated-by": [{"type": "retraction"}]})

    c1 = DiskCache(path, ttl_seconds=100, _now=clock)
    CrossrefClient(cache=c1, _fetch=lambda d: clean).fetch("10.1/x")
    c1.save()

    clock.t += 1000  # the manuscript sits for a while; the paper is retracted
    c2 = DiskCache(path, ttl_seconds=100, _now=clock)
    fresh = CrossrefClient(cache=c2, _fetch=lambda d: retracted).fetch("10.1/x")
    assert fresh == retracted  # re-fetched, not served stale


def test_a_backwards_clock_expires_rather_than_trusting_forever(tmp_path):
    clock = Clock()
    c = cache(tmp_path, clock, ttl=100)
    c.set("k", RECORD)
    clock.t -= 500  # DST change / NTP correction / doctored file
    assert c.get("k") is _MISS


def test_zero_ttl_disables_reuse(tmp_path):
    c = cache(tmp_path, ttl=0)
    c.set("k", RECORD)
    assert c.get("k") == RECORD  # same instant is still within a 0s TTL
    c._now = Clock(c._now() + 1)
    assert c.get("k") is _MISS


# --- corrupt / hostile cache files degrade to "no cache" --------------------


@pytest.mark.parametrize(
    "content",
    [
        "",
        "not json at all",
        "[]",
        "null",
        '{"entries": "a string"}',
        '{"entries": {"k": "not a dict"}}',
        '{"entries": {"k": {}}}',  # no value key
        '{"entries": {"k": {"value": 1}}}',  # no timestamp
        '{"entries": {"k": {"value": 1, "stored_at": "yesterday"}}}',
        '{"entries": {"k": {"value": 1, "stored_at": null}}}',
        '{"no_entries_key": 1}',
        "[" * 200 + "]" * 200,  # deeply nested
    ],
)
def test_corrupt_cache_file_is_treated_as_empty(tmp_path, content):
    (tmp_path / "c.json").write_text(content)
    assert cache(tmp_path).get("k") is _MISS


def test_missing_cache_file_is_fine(tmp_path):
    assert cache(tmp_path).get("k") is _MISS


def test_unwritable_cache_reports_failure_without_raising(tmp_path):
    """A directory where the file should be — save() must report, not explode."""
    (tmp_path / "c.json").mkdir()
    c = cache(tmp_path)
    c.set("k", RECORD)
    assert c.save() is False


def test_unserializable_value_does_not_raise(tmp_path):
    c = cache(tmp_path)
    c.set("k", {"bad": object()})
    assert c.save() is False


# --- client integration -----------------------------------------------------


def test_crossref_fetch_uses_the_cache_on_a_second_client(tmp_path):
    clock = Clock()
    path = tmp_path / "c.json"
    calls = []
    c1 = DiskCache(path, _now=clock)
    CrossrefClient(cache=c1, _fetch=lambda d: calls.append(d) or RECORD).fetch("10.1/x")
    c1.save()

    c2 = DiskCache(path, _now=clock)
    client = CrossrefClient(cache=c2, _fetch=lambda d: calls.append(d) or RECORD)
    assert client.fetch("10.1/x") == RECORD
    assert calls == ["10.1/x"]  # the second client never called the transport
    assert client.remote_calls == 0


def test_resolve_is_cached(tmp_path):
    clock = Clock()
    path = tmp_path / "c.json"
    c1 = DiskCache(path, _now=clock)
    CrossrefClient(cache=c1, _fetch=lambda d: None, _resolve=lambda d: True).resolve("10.1/x")
    c1.save()

    def boom(d):
        raise AssertionError("should have been cached")

    c2 = DiskCache(path, _now=clock)
    assert CrossrefClient(cache=c2, _fetch=lambda d: None, _resolve=boom).resolve("10.1/x") is True


def test_pubmed_fetch_is_cached(tmp_path):
    clock = Clock()
    path = tmp_path / "c.json"
    rec = {"pubtype": ["Journal Article"]}
    c1 = DiskCache(path, _now=clock)
    PubMedClient(cache=c1, _fetch=lambda p: rec).fetch("123")
    c1.save()

    def boom(p):
        raise AssertionError("should have been cached")

    c2 = DiskCache(path, _now=clock)
    assert PubMedClient(cache=c2, _fetch=boom).fetch("123") == rec


def test_crossref_and_pubmed_share_a_file_without_colliding(tmp_path):
    """Both key on the same bare id "123" — the namespace prefix must separate
    them, or a PMID lookup would return a Crossref record."""
    clock = Clock()
    c = DiskCache(tmp_path / "c.json", _now=clock)
    CrossrefClient(cache=c, _fetch=lambda d: RECORD).fetch("123")
    PubMedClient(cache=c, _fetch=lambda p: {"pubtype": ["x"]}).fetch("123")
    c.save()

    c2 = DiskCache(tmp_path / "c.json", _now=clock)
    assert c2.get("crossref:fetch:123") == RECORD
    assert c2.get("pubmed:fetch:123") == {"pubtype": ["x"]}


def test_a_cached_none_is_not_refetched(tmp_path):
    clock = Clock()
    path = tmp_path / "c.json"
    c1 = DiskCache(path, _now=clock)
    CrossrefClient(cache=c1, _fetch=lambda d: None).fetch("10.1/gone")
    c1.save()

    def boom(d):
        raise AssertionError("a cached 'not found' must not re-fetch")

    c2 = DiskCache(path, _now=clock)
    assert CrossrefClient(cache=c2, _fetch=boom).fetch("10.1/gone") is None


def test_a_poisoned_cache_entry_of_the_wrong_type_is_ignored(tmp_path):
    """A hand-edited cache must not be able to feed a client a bogus shape."""
    clock = Clock()
    path = tmp_path / "c.json"
    path.write_text(
        json.dumps(
            {"entries": {"crossref:fetch:10.1/x": {"stored_at": clock(), "value": "a string"}}}
        )
    )
    c = DiskCache(path, _now=clock)
    assert CrossrefClient(cache=c, _fetch=lambda d: RECORD).fetch("10.1/x") == RECORD


def test_a_poisoned_resolve_entry_of_the_wrong_type_is_ignored(tmp_path):
    clock = Clock()
    path = tmp_path / "c.json"
    path.write_text(
        json.dumps({"entries": {"crossref:resolve:10.1/x": {"stored_at": clock(), "value": "yes"}}})
    )
    c = DiskCache(path, _now=clock)
    client = CrossrefClient(cache=c, _fetch=lambda d: None, _resolve=lambda d: False)
    assert client.resolve("10.1/x") is False


def test_no_cache_means_every_client_is_independent(tmp_path):
    calls = []
    for _ in range(2):
        CrossrefClient(_fetch=lambda d: calls.append(d) or RECORD).fetch("10.1/x")
    assert len(calls) == 2


def test_remote_calls_counts_only_transport_hits(tmp_path):
    c = CrossrefClient(cache=cache(tmp_path), _fetch=lambda d: RECORD)
    c.fetch("10.1/x")
    c.fetch("10.1/x")  # in-run memoisation
    assert c.remote_calls == 1
