"""Crossref lookups and claim-vs-record comparison."""

from __future__ import annotations

import html
import json
import os
import re
import socket
import tempfile
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Optional

from ._version import __version__
from .parsers import _KO_TWO_SYLLABLE_SURNAMES, Reference, _has_hangul, _mostly_hangul

CROSSREF_API = "https://api.crossref.org/works/"
CROSSREF_SEARCH = "https://api.crossref.org/works"
DOI_HANDLE_API = "https://doi.org/api/handles/"
PUBMED_ESUMMARY = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi"


def _user_agent(mailto: Optional[str] = None) -> str:
    """The User-Agent sent to Crossref, doi.org and PubMed, built from the version."""
    return (
        f"citecheck/{__version__} (https://github.com/hyeonjoong/citecheck; "
        f"mailto:{mailto or 'citecheck@example.com'})"
    )


DEFAULT_UA = _user_agent()

# Severity levels for findings.
OK = "ok"
WARNING = "warning"
ERROR = "error"

# C0/C1 control characters (incl. ESC). Any externally sourced text — a BibTeX
# title, a Crossref journal name, a spreadsheet cell — is stripped of these
# before it reaches a report, so it cannot inject ANSI/terminal escapes into the
# user's terminal or into a file a co-author opens. It lives here, not in
# cli.py, so every module that renders untrusted text uses the *same* control
# (citecheck.profile does too); two copies would be one copy waiting to drift.
CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")


def sanitize_text(text: str) -> str:
    """Strip C0/C1 control characters from externally sourced *text*."""
    return CONTROL_CHARS_RE.sub("", text)

# Transport failures worth retrying. ``socket.timeout`` is listed explicitly
# because it only became an alias of ``TimeoutError`` in Python 3.10 (bpo-42413)
# — on 3.9, which pyproject.toml still supports, a read timeout would otherwise
# escape the retry loop entirely and skip every remaining attempt.
_TRANSPORT_ERRORS = (
    urllib.error.URLError,
    TimeoutError,
    socket.timeout,
    json.JSONDecodeError,
    UnicodeDecodeError,
)


# Stable machine-readable codes for every finding the tool can emit. They exist
# so a CI gate can say *which* problems matter (`--ignore no-doi`) and so a JSON
# report is filterable without regex-matching English prose. Treat them as API:
# renaming one breaks a user's --ignore list and their scripts.
CODES = {
    "verified": "The reference matched its Crossref record.",
    "no-doi": "The reference cites no DOI, so nothing could be verified.",
    "doi-not-resolving": "The DOI does not resolve at Crossref or doi.org.",
    "doi-not-in-crossref": "The DOI resolves but is not in Crossref's works index.",
    "lookup-failed": "A Crossref/PubMed lookup failed (e.g. offline).",
    "bad-record": "Crossref returned a record we could not read.",
    "retracted": "The work is marked retracted.",
    "expression-of-concern": "An expression of concern has been issued.",
    "withdrawal": "The work has been withdrawn.",
    "removal": "The work has been removed.",
    "correction": "A correction/erratum/corrigendum has been issued.",
    "addendum": "An addendum has been issued.",
    "clarification": "A clarification has been issued.",
    "new-edition": "A newer edition/version exists.",
    "preprint-published": "A peer-reviewed version of this preprint exists.",
    "title-mismatch": "The cited title does not match Crossref's.",
    "year-mismatch": "The cited year does not match any Crossref date.",
    "author-mismatch": "The cited first author is not among Crossref's authors.",
    "journal-mismatch": "The cited journal does not match Crossref's container.",
    "text-title-missing": "A free-text citation does not mention the Crossref title.",
    "duplicate-doi": "The same DOI is cited by more than one reference.",
    "duplicate-pmid": "The same PMID is cited by more than one reference.",
    "pmid-doi-mismatch": "The cited PMID and DOI belong to different papers.",
    "pmid-not-found": "The cited PMID is not in PubMed.",
    "doi-suggestion": "Crossref holds a confident match for a DOI-less reference.",
}


@dataclass
class Finding:
    severity: str
    message: str
    # A CODES key. Defaults to "" rather than being required so a third-party
    # caller constructing a Finding directly keeps working.
    code: str = ""


@dataclass
class CheckResult:
    reference: Reference
    findings: list[Finding] = field(default_factory=list)
    resolved_doi: Optional[str] = None
    crossref: Optional[dict] = None
    # (field, reason) pairs for fields that were not compared because the
    # citation and the Crossref record are in different scripts.
    not_compared: list = field(default_factory=list)

    @property
    def status(self) -> str:
        if any(f.severity == ERROR for f in self.findings):
            return ERROR
        if any(f.severity == WARNING for f in self.findings):
            return WARNING
        return OK

    def add(self, severity: str, message: str, code: str = "") -> None:
        self.findings.append(Finding(severity, message, code))


class DiskCache:
    """A tiny expiring JSON cache of Crossref/PubMed lookups.

    Re-checking a 150-reference manuscript through a round of revisions means
    150 network round-trips each time, most of them for records that have not
    changed. This makes the second run instant and spares Crossref the load.

    Entries expire (default 7 days) because the whole point of the tool is to
    catch a *newly* retracted reference — an indefinite cache would eventually
    report a stale clean pass, which is the one failure this tool must not have.

    Every operation is best-effort: an unreadable, corrupt, or unwritable cache
    file degrades to "no cache", never to an error. A cache is an optimisation,
    and must never be able to fail a citation check.
    """

    def __init__(self, path, ttl_seconds: float = 7 * 24 * 3600, _now=None):
        self.path = str(path)
        self.ttl_seconds = ttl_seconds
        self._now = _now or time.time
        self._entries: dict[str, dict] = {}
        self.dirty = False
        # Live entries actually served. The CLI reports this, so it must count
        # real hits — inferring it from "this reference made no network call"
        # instead credited the cache for references that never had a DOI to look
        # up, and claimed hits against a cache file that did not exist.
        self.hits = 0
        self.load()

    def load(self) -> None:
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError, RecursionError):
            return  # missing/corrupt cache is simply an empty one
        entries = data.get("entries") if isinstance(data, dict) else None
        if isinstance(entries, dict):
            self._entries = {k: v for k, v in entries.items() if isinstance(v, dict)}

    def get(self, key: str):
        """Return the cached value for *key*, or ``_MISS`` when absent/expired."""
        entry = self._entries.get(key)
        if not isinstance(entry, dict) or "value" not in entry:
            return _MISS
        stored = entry.get("stored_at")
        if not isinstance(stored, (int, float)):
            return _MISS
        age = self._now() - stored
        # A negative age means a clock change (or a doctored file) — treat the
        # entry as expired rather than trusting it forever.
        if age < 0 or age > self.ttl_seconds:
            return _MISS
        self.hits += 1
        return entry["value"]

    def set(self, key: str, value) -> None:
        self._entries[key] = {"stored_at": self._now(), "value": value}
        self.dirty = True

    def save(self) -> bool:
        """Persist the cache. Returns True on success; never raises."""
        if not self.dirty:
            return True
        try:
            directory = os.path.dirname(os.path.abspath(self.path))
            if directory:
                os.makedirs(directory, exist_ok=True)
            # Write via a temp file in the same directory + atomic replace, so an
            # interrupted run (or two concurrent ones) cannot leave a truncated
            # cache that the next run would have to treat as corrupt.
            fd, tmp = tempfile.mkstemp(dir=directory or ".", suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump({"version": 1, "entries": self._entries}, fh)
                os.replace(tmp, self.path)
            except BaseException:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
        except (OSError, ValueError, TypeError, RecursionError):
            return False
        self.dirty = False
        return True


class _Miss:
    """Sentinel: distinct from a cached ``None`` (a real 'not in Crossref')."""

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "<MISS>"

    def __bool__(self) -> bool:
        return False


_MISS = _Miss()


class CrossrefClient:
    """Tiny Crossref client over the standard library (no third-party deps)."""

    def __init__(
        self,
        mailto: Optional[str] = None,
        timeout: float = 15.0,
        retries: int = 2,
        sleep: float = 1.0,
        cache: Optional[DiskCache] = None,
        _fetch=None,
        _resolve=None,
        _search=None,
    ):
        self.cache = cache
        self.timeout = timeout
        self.retries = retries
        self.sleep = sleep
        self.user_agent = _user_agent(mailto)
        # _fetch/_resolve/_search let tests inject fake transports:
        #   _fetch:   doi -> message dict | None
        #   _resolve: doi -> bool (does the DOI resolve at doi.org?)
        #   _search:  query string -> list of candidate message dicts
        self._fetch = _fetch
        self._resolve = _resolve
        self._search = _search
        # Counts lookups that went out to the transport rather than being served
        # from a cache. The CLI reads it to decide whether a rate-limiting delay
        # is owed (a cache hit costs Crossref nothing) and to report cache use.
        self.remote_calls = 0
        # In-run memoisation so a manuscript that cites the same DOI twice (or a
        # re-run over the same file) hits the network only once per DOI.
        self._fetch_cache: dict[str, Optional[dict]] = {}
        self._resolve_cache: dict[str, bool] = {}
        self._search_cache: dict[str, list] = {}

    def fetch(self, doi: str) -> Optional[dict]:
        """Return the Crossref `message` for *doi*, or None if not found."""
        if doi in self._fetch_cache:
            return self._fetch_cache[doi]
        if self.cache is not None:
            cached = self.cache.get(f"crossref:fetch:{doi}")
            if cached is not _MISS and (cached is None or isinstance(cached, dict)):
                self._fetch_cache[doi] = cached
                return cached
        self.remote_calls += 1
        result = self._fetch(doi) if self._fetch is not None else self._fetch_network(doi)
        self._fetch_cache[doi] = result
        if self.cache is not None and (result is None or isinstance(result, dict)):
            self.cache.set(f"crossref:fetch:{doi}", result)
        return result

    def _fetch_network(self, doi: str) -> Optional[dict]:
        url = CROSSREF_API + urllib.parse.quote(doi, safe="")
        req = urllib.request.Request(url, headers={"User-Agent": self.user_agent})
        last_err: Optional[Exception] = None
        for attempt in range(self.retries + 1):
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                    return data.get("message")
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    return None
                last_err = e
            except _TRANSPORT_ERRORS as e:
                last_err = e
            if attempt < self.retries:
                time.sleep(self.sleep * (attempt + 1))
        if last_err:
            raise last_err
        return None

    def search(self, query: str) -> list:
        """Return candidate Crossref records for a bibliographic *query*.

        Backs the "this reference has no DOI — here is the DOI Crossref holds
        for it" recovery path. Crossref's ``query.bibliographic`` is built for
        exactly this: it accepts a whole reference string or a title+author+year
        blob and ranks works against it.
        """
        if not query:
            return []
        if query in self._search_cache:
            return self._search_cache[query]
        self.remote_calls += 1
        result = self._search(query) if self._search is not None else self._search_network(query)
        result = result if isinstance(result, list) else []
        self._search_cache[query] = result
        return result

    def _search_network(self, query: str, rows: int = 5) -> list:
        params = {
            "query.bibliographic": query,
            "rows": str(rows),
            # Ask only for the fields we actually use — a full record per
            # candidate would be many KB of payload we immediately discard.
            # `updated-by`/`relation`/`type` are NOT optional: without them a
            # candidate's retraction is invisible and the tool would cheerfully
            # recommend citing a retracted paper (it did — see _suggest_doi).
            "select": "DOI,title,subtitle,author,issued,published-print,"
            "published-online,container-title,short-container-title,"
            "updated-by,relation,type",
        }
        url = CROSSREF_SEARCH + "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers={"User-Agent": self.user_agent})
        last_err: Optional[Exception] = None
        for attempt in range(self.retries + 1):
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                message = data.get("message") if isinstance(data, dict) else None
                items = message.get("items") if isinstance(message, dict) else None
                return [i for i in _as_list(items) if isinstance(i, dict)]
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    return []
                last_err = e
            except _TRANSPORT_ERRORS as e:
                last_err = e
            if attempt < self.retries:
                time.sleep(self.sleep * (attempt + 1))
        if last_err:
            raise last_err
        return []

    def resolve(self, doi: str) -> bool:
        """Return True if *doi* resolves at doi.org (independent of Crossref).

        Used to distinguish a genuinely broken DOI from one that resolves but is
        simply not in Crossref's ``works`` index (e.g. a DataCite/dataset DOI).
        """
        if doi in self._resolve_cache:
            return self._resolve_cache[doi]
        if self._fetch is not None and self._resolve is None:
            # A caller that injected only a Crossref transport (the 0.1.0 test
            # contract) gets no doi.org request either: fully offline, and a
            # DOI missing from that transport reads as not registered.
            return False
        if self.cache is not None:
            cached = self.cache.get(f"crossref:resolve:{doi}")
            if isinstance(cached, bool):
                self._resolve_cache[doi] = cached
                return cached
        self.remote_calls += 1
        result = self._resolve(doi) if self._resolve is not None else self._resolve_network(doi)
        self._resolve_cache[doi] = result
        if self.cache is not None and isinstance(result, bool):
            self.cache.set(f"crossref:resolve:{doi}", result)
        return result

    def _resolve_network(self, doi: str) -> bool:
        """Ask the doi.org handle server whether `doi` is registered.

        A GET to the handle API answers from the DOI registry itself and never
        follows the redirect to the publisher's landing page. The earlier HEAD
        to https://doi.org/<doi> did follow it, so a landing page with a broken
        TLS certificate made a registered DOI inconclusive, and a landing page
        that answers 404 to HEAD was reported as "does not resolve anywhere".

        Answers: responseCode 1 is registered (True). HTTP 404, or responseCode
        100 (handle not found) or 200 (no values), is not registered (False).
        Anything else (5xx, 429, a body that is not the expected JSON, another
        code) raises, so the caller reports lookup-failed (inconclusive) and
        never a false hard error.
        """
        url = DOI_HANDLE_API + urllib.parse.quote(doi, safe="/()")
        req = urllib.request.Request(
            url, headers={"User-Agent": self.user_agent, "Accept": "application/json"}
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                body = resp.read()
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return False  # the handle server does not know this DOI
            raise  # 5xx/429 and the like are transient: inconclusive
        # URLError/TimeoutError propagate for the same reason.
        data = json.loads(body.decode("utf-8"))
        code = data.get("responseCode") if isinstance(data, dict) else None
        if code == 1:
            return True
        if code in (100, 200):
            return False
        raise ValueError(f"unexpected doi.org handle API answer (responseCode {code!r})")


class PubMedClient:
    """Tiny NCBI E-utilities client for PubMed esummary (standard library only).

    Used, when the caller opts in, to (a) catch retractions PubMed marks but
    Crossref misses, and (b) confirm a cited PMID and DOI point to the *same*
    paper. Mirrors :class:`CrossrefClient`: an injectable ``_fetch`` transport
    (pmid -> esummary record dict | None) keeps it fully offline-testable.
    """

    def __init__(
        self,
        mailto: Optional[str] = None,
        timeout: float = 15.0,
        retries: int = 2,
        sleep: float = 1.0,
        api_key: Optional[str] = None,
        cache: Optional[DiskCache] = None,
        _fetch=None,
    ):
        self.cache = cache
        self.timeout = timeout
        self.retries = retries
        self.sleep = sleep
        self.api_key = api_key
        self.mailto = mailto
        self.user_agent = _user_agent(mailto)
        self._fetch = _fetch
        self.remote_calls = 0  # see CrossrefClient.remote_calls
        self._cache: dict[str, Optional[dict]] = {}

    def fetch(self, pmid: str) -> Optional[dict]:
        """Return the PubMed esummary record for *pmid*, or None if absent."""
        if pmid in self._cache:
            return self._cache[pmid]
        if self.cache is not None:
            cached = self.cache.get(f"pubmed:fetch:{pmid}")
            if cached is not _MISS and (cached is None or isinstance(cached, dict)):
                self._cache[pmid] = cached
                return cached
        self.remote_calls += 1
        result = self._fetch(pmid) if self._fetch is not None else self._fetch_network(pmid)
        self._cache[pmid] = result
        if self.cache is not None and (result is None or isinstance(result, dict)):
            self.cache.set(f"pubmed:fetch:{pmid}", result)
        return result

    def _fetch_network(self, pmid: str) -> Optional[dict]:
        params = {"db": "pubmed", "id": pmid, "retmode": "json"}
        if self.api_key:
            params["api_key"] = self.api_key
        if self.mailto:
            params["email"] = self.mailto
            params["tool"] = "citecheck"
        url = PUBMED_ESUMMARY + "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers={"User-Agent": self.user_agent})
        last_err: Optional[Exception] = None
        for attempt in range(self.retries + 1):
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                    return _pubmed_record(data, pmid)
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    return None
                last_err = e
            except _TRANSPORT_ERRORS as e:
                last_err = e
            if attempt < self.retries:
                time.sleep(self.sleep * (attempt + 1))
        if last_err:
            raise last_err
        return None


def _pubmed_record(data, pmid: str) -> Optional[dict]:
    """Extract the per-PMID record from an esummary JSON envelope.

    esummary returns ``{"result": {"uids": [...], "<pmid>": {...}}}``. An error
    for a bad id surfaces as a record carrying an ``error`` key, which we treat
    as "not found".
    """
    if not isinstance(data, dict):
        return None
    result = data.get("result")
    if not isinstance(result, dict):
        return None
    rec = result.get(str(pmid))
    if not isinstance(rec, dict) or rec.get("error"):
        return None
    return rec


def _pubmed_doi(record: dict) -> Optional[str]:
    """The DOI PubMed lists for a record (from its ``articleids``), normalised."""
    from .parsers import normalize_doi_field

    for aid in _as_list(record.get("articleids")):
        if isinstance(aid, dict) and str(aid.get("idtype", "")).lower() == "doi":
            doi = normalize_doi_field(str(aid.get("value", "")))
            if doi:
                return doi
    return None


def _pubmed_is_retracted(record: dict) -> bool:
    """True if PubMed marks this record as a retracted publication.

    PubMed tags the retracted *article* with the publication type "Retracted
    Publication"; the retraction *notice* is "Retraction of Publication" (which
    we deliberately do NOT treat as a retracted source).
    """
    for pt in _as_list(record.get("pubtype")):
        if isinstance(pt, str) and pt.strip().lower() == "retracted publication":
            return True
    return False


def _fold(s: str) -> str:
    """Strip diacritics so "Müller" and "Muller" compare equal."""
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))


# Unicode hyphens and dashes that publishers deposit where a citation has "-":
# U+2010 hyphen (Wiley writes COVID-19 with U+2010 as the hyphen), U+2011
# non-breaking hyphen, U+2012 figure dash, U+2013 en dash and U+2212 minus sign.
_DASHES = str.maketrans({"\u2010": "-", "\u2011": "-", "\u2012": "-", "\u2013": "-", "\u2212": "-"})


def _norm(s: str) -> str:
    return " ".join(_fold(s.translate(_DASHES)).lower().split())


# A JATS/HTML tag as Crossref titles carry them: <scp>, <sub>, <i>, <mml:math>.
# A tag must start with a letter right after "<", so prose such as
# "aged <65 vs >65 years" is left alone.
_MARKUP_TAG_RE = re.compile(r"</?[A-Za-z][A-Za-z0-9:_.-]*(?:\s[^<>]*)?/?>")


def _plain(text: str) -> str:
    """A Crossref string as plain text: tags removed, entities decoded, one line.

    Wiley and others deposit titles such as "Treating <scp>OSA</scp>:
    <scp>C</scp>urrent and emerging therapies". Compared raw, a correct citation
    scored below the title threshold, and the Verified line printed the tags.
    """
    text = _MARKUP_TAG_RE.sub("", text)
    return " ".join(html.unescape(text).split())


def _similar(a: str, b: str) -> float:
    """Similarity of two strings, 0.0–1.0, diacritic-folded and case-insensitive.

    ``autojunk=False`` is load-bearing, not a tuning knob. difflib's autojunk
    heuristic is designed for diffing source code: once the second sequence
    reaches 200 elements it treats any element occurring more than
    ``len(b)//100 + 1`` times as junk and refuses to match on it. On *characters*
    that means common letters stop counting, so the score collapses — and
    clinical trial titles routinely pass 200 characters:

        cited:    "...ventilator associated pneumonia ... stepped wedge..."
        crossref: "...ventilator-associated pneumonia ... stepped-wedge..."
        (identical but for the author dropping the hyphens)
          with autojunk:  0.864     <- below the 0.80 title threshold's headroom
          without:        0.986

    It also made the score *discontinuous* in the length of the Crossref title:
    truncating the same pair at 199 vs 200 characters scored 0.930 vs 0.856. The
    visible symptoms were false ``title-mismatch`` warnings on long trial names,
    and `--suggest-doi` (threshold 0.92) silently discarding the correct DOI.
    """
    return SequenceMatcher(None, _norm(a), _norm(b), autojunk=False).ratio()


def _as_list(value) -> list:
    """Coerce a Crossref field that should be a list into one, defensively."""
    if isinstance(value, list):
        return value
    if value is None:
        return []
    return [value]


def _plain_strings(value) -> list[str]:
    """The non-empty strings of a Crossref list field, as plain text."""
    out = []
    for v in _as_list(value):
        if isinstance(v, str):
            v = _plain(v)
            if v:
                out.append(v)
    return out


def _crossref_title_candidates(message: dict) -> list[str]:
    """Return candidate titles: the main title, main+subtitle, and original-title.

    Crossref stores subtitles separately, so a citation that (correctly)
    includes the subtitle must be compared against the concatenation too, or it
    falsely reads as a title mismatch. A record may also carry the title in its
    original language (``original-title``), which a citation may quote instead.
    The first candidate is always the main title, the one shown in reports.
    Markup and entities are removed (see _plain).
    """
    titles = _plain_strings(message.get("title"))
    if not titles:
        return []
    # Drop a publisher's "RETRACTED: " marker: the author cited the paper's
    # actual title, so comparing against the marked-up one reports a false title
    # mismatch on every correctly-cited retracted paper (the retraction itself is
    # reported separately, by _is_retracted).
    main = _strip_retracted_prefix(titles[0])[0].strip() or titles[0]
    candidates = [main]
    subs = _plain_strings(message.get("subtitle"))
    if subs:
        candidates.append(f"{main}: {subs[0]}")
    for original in _plain_strings(message.get("original-title")):
        if original not in candidates:
            candidates.append(original)
    return candidates


def _crossref_title(message: dict) -> Optional[str]:
    cands = _crossref_title_candidates(message)
    return cands[0] if cands else None


def _crossref_years(message: dict) -> set[int]:
    """All plausible publication years across Crossref's date fields.

    A paper published online in year N and in print in N+1 legitimately carries
    both; the citation matches if it equals *any* of them.

    ``created`` is deliberately excluded. It is Crossref's *deposit* timestamp,
    not a publication year, and for back-deposited papers it is wildly wrong —
    Wakefield 1998 has ``created`` 2002, so including it silently accepted "2002"
    as a valid year for a 1998 paper. Any record with only a ``created`` date now
    yields an empty set, which skips the year check rather than misjudging it.
    """
    years: set[int] = set()
    for key in ("published-print", "published-online", "published", "issued"):
        field_val = message.get(key)
        if not isinstance(field_val, dict):
            continue
        parts = field_val.get("date-parts")
        if not isinstance(parts, list):
            continue
        for part in parts:
            if isinstance(part, list) and part and isinstance(part[0], int):
                years.add(part[0])
            elif isinstance(part, list) and part:
                try:
                    years.add(int(part[0]))
                except (TypeError, ValueError, OverflowError):
                    # OverflowError: a poisoned record (or cache file) can hold
                    # `1e400`, which JSON decodes to float('inf'); int(inf)
                    # raises OverflowError, and this runs outside the
                    # "one weird record must not abort the batch" guards.
                    pass
    return years


def _crossref_container_candidates(message: dict) -> list[str]:
    """Journal / container names Crossref holds (full and abbreviated).

    As plain text: a deposited "Multiculture &amp; Peace" is "Multiculture & Peace".
    """
    out: list[str] = []
    for key in ("container-title", "short-container-title"):
        out.extend(_plain_strings(message.get(key)))
    return out


def _journal_words(s: str) -> list[str]:
    """All diacritic-folded alphabetic words of *s* (any length).

    Unlike ``_alpha_tokens`` (which drops sub-3-char words for content overlap),
    this keeps short words because they ARE the abbreviations — "Am", "J", "Br"
    in "Am J Med" carry the signal, and dropping them broke matching for common
    medical-journal abbreviations.
    """
    return re.findall(r"[a-z]+", _norm(s).replace("-", " "))


def _is_word_contraction(token: str, word: str) -> bool:
    """True if *token* is an ISO-4-style abbreviation of *word*.

    Same first letter, and *token*'s letters appear in *word* in order (a
    subsequence). This subsumes a plain prefix ("engl" of "england") and also
    accepts *contracted* abbreviations that drop interior letters — "natl" of
    "national", "dtsch" of "deutsche" — which a prefix test wrongly rejects,
    producing false "Journal mismatch" warnings on PNAS / JNCI and the like.
    """
    if not token or not word or token[0] != word[0]:
        return False
    it = iter(word)
    return all(ch in it for ch in token)


def _is_abbrev_of(cited: str, full: str) -> bool:
    """True if *cited* reads as an ISO-4-style abbreviation of *full*.

    Each word of the abbreviation must be an ISO-4 contraction of a word of the
    full name (see :func:`_is_word_contraction`), matched greedily in order (so
    stop-words the abbreviation drops — "of", "the" — are simply skipped in the
    full name). Handles both directions at the call site. e.g. "Am J Med" ↔
    "American Journal of Medicine", "Proc Natl Acad Sci" ↔ "Proceedings of the
    National Academy of Sciences". Requires at least two abbreviation words so a
    single short token can't match everything.
    """
    ct = _journal_words(cited)
    ft = _journal_words(full)
    if len(ct) < 2 or not ft:
        return False
    j = 0
    for token in ct:
        while j < len(ft) and not _is_word_contraction(token, ft[j]):
            j += 1
        if j == len(ft):
            return False
        j += 1
    return True


# Words an initialism skips: "JAMA" is Journal (of the) American Medical
# Association, "PNAS" is Proceedings (of the) National Academy (of) Sciences.
_INITIALISM_STOPWORDS = {"of", "the", "and", "for", "a", "an", "in", "on", "to"}


def _is_initialism_of(cited: str, full: str) -> bool:
    """True if *cited* is the all-caps initialism of *full*.

    "NEJM" ↔ "New England Journal of Medicine", "JAMA" ↔ "Journal of the
    American Medical Association", "BMJ" ↔ "British Medical Journal". Clinical
    authors write these constantly, and ``_is_abbrev_of`` structurally cannot
    match them: it requires at least two abbreviation words, so any single-token
    journal name failed and produced a false "Journal mismatch" warning on some
    of the most-cited journals in medicine.

    Deliberately narrow, because a short token is weak evidence:

    * *cited* must be a single token that is ALL-CAPS **as written** — the case
      is the signal that the author meant an initialism, not a word. So "Cancer"
      never matches "Cancer Nursing", while "AIDS" may match its expansion.
    * The letters must equal the initials of *full*'s significant words exactly,
      in order — not a subsequence, not a prefix.
    * At least two letters, and *full* must be a multi-word name; a one-word
      journal has no initialism to speak of.
    """
    token = cited.strip().replace(".", "").replace(" ", "")
    if len(token) < 2 or not token.isalpha() or not token.isupper():
        return False
    words = [w for w in _journal_words(full) if w not in _INITIALISM_STOPWORDS]
    if len(words) < 2:
        return False
    return "".join(w[0] for w in words) == token.lower()


_LEADING_ARTICLE_RE = re.compile(r"^(?:the|a|an)\s+", re.IGNORECASE)


def _journal_key(s: str) -> str:
    """Normalise a journal name for comparison: fold diacritics, lowercase, and
    drop a leading article so "Lancet" and "The Lancet" compare equal."""
    return _LEADING_ARTICLE_RE.sub("", _norm(s)).strip()


def _journal_matches(cited: str, candidates: list[str], threshold: float) -> bool:
    """Does the *cited* journal reasonably match any Crossref candidate?

    Deliberately permissive: journal names vary wildly in abbreviation, leading
    articles, and punctuation across reference styles, so we accept a match when
    the article-stripped names are equal, on high fuzzy similarity, or on an
    abbreviation relationship in either direction. Only a clear mismatch (e.g.
    "Nature" cited, Crossref says "Lancet") fails.

    Multi-word ISO-4 abbreviations ("Am J Med", "N Engl J Med") are handled, as
    are all-caps initialisms ("NEJM", "JAMA", "BMJ" — see
    :func:`_is_initialism_of`). A single-token *lower*-case abbreviation
    ("Circ" → "Circulation") is only matched via Crossref's
    ``short-container-title`` (which real records carry), because expanding one
    such token can't be distinguished from a genuinely different journal
    ("Cancer" vs "Cancer Research").
    """
    cited_key = _journal_key(cited)
    if not cited_key:
        return True  # nothing comparable — don't guess
    for cand in candidates:
        cand_key = _journal_key(cand)
        if cited_key == cand_key:
            return True
        # autojunk=False for the same reason as `_similar` — a long
        # society-journal name plus subtitle can clear 200 characters.
        if (
            cand_key
            and SequenceMatcher(None, cited_key, cand_key, autojunk=False).ratio() >= threshold
        ):
            return True
        if _is_abbrev_of(cited, cand) or _is_abbrev_of(cand, cited):
            return True
        if _is_initialism_of(cited, cand) or _is_initialism_of(cand, cited):
            return True
    return False


def _crossref_first_author(message: dict) -> Optional[str]:
    for a in _as_list(message.get("author")):
        if isinstance(a, dict) and a.get("family"):
            return a["family"]
    return None


def _crossref_all_author_families(message: dict) -> list[str]:
    return [
        a["family"]
        for a in _as_list(message.get("author"))
        if isinstance(a, dict) and a.get("family")
    ]


def _crossref_organisation_names(message: dict) -> list[str]:
    """Names of organisation authors (Crossref puts them in `name`, not `family`)."""
    return [
        _plain(a["name"])
        for a in _as_list(message.get("author"))
        if isinstance(a, dict) and not a.get("family") and isinstance(a.get("name"), str)
        and a["name"].strip()
    ]


# --- comparing across scripts ---------------------------------------------------
#
# A Korean citation of a record that Crossref holds only in English (or the
# reverse) cannot be compared letter by letter: "약물 유발 신독성" vs
# "Drug-induced nephrotoxicity" scores near zero, and "홍길동" vs "Hong" is the
# same person. Such a field is skipped, and the Verified line says so.

CITED_KO_RECORD_EN = "cited in Korean, Crossref record in English"
CITED_EN_RECORD_KO = "cited in English, Crossref record in Korean"


def _script_gap(cited: str, candidates: list[str]) -> Optional[str]:
    """Why `cited` cannot be compared with `candidates`, or None if it can.

    Korean cited text against candidates with no Hangul at all, or cited text
    with no Hangul against candidates that are all mostly Hangul. A record that
    holds both scripts (say an English original-title) is compared as usual.
    """
    cands = [c for c in candidates if isinstance(c, str) and any(ch.isalpha() for ch in c)]
    if not cited or not cands or not any(ch.isalpha() for ch in cited):
        return None
    if _mostly_hangul(cited) and not any(_has_hangul(c) for c in cands):
        return CITED_KO_RECORD_EN
    if not _has_hangul(cited) and all(_mostly_hangul(c) for c in cands):
        return CITED_EN_RECORD_KO
    return None


def _not_compared_note(skipped: list) -> str:
    """The note the Verified line carries for fields skipped across scripts.

    For example " (title/author not compared: cited in Korean, Crossref record
    in English)", or "" when nothing was skipped.
    """
    if not skipped:
        return ""
    by_reason: dict = {}
    for name, reason in skipped:
        by_reason.setdefault(reason, [])
        if name not in by_reason[reason]:
            by_reason[reason].append(name)
    parts = [f"{'/'.join(names)} not compared: {reason}" for reason, names in by_reason.items()]
    return f" ({'; '.join(parts)})"


def _hangul_key(text: str) -> str:
    return unicodedata.normalize("NFC", text).replace(" ", "")


def _hangul_author_matches(cited: str, message: dict) -> bool:
    """Is the Hangul name `cited` one of the record's Hangul authors?

    Compared syllable for syllable after NFC, not by fuzzy similarity: on
    decomposed jamo a one-letter difference in a three-syllable name still
    scored above 0.85, so a different person passed. KCI records hold a name
    either whole in `family` ("홍길동") or split ("홍" + "길동"); both forms are
    accepted, and so is the surname cited on its own.
    """
    key = _hangul_key(cited)
    for a in _as_list(message.get("author")):
        if not isinstance(a, dict) or not isinstance(a.get("family"), str):
            continue
        family = _hangul_key(a["family"])
        if not family or not _has_hangul(family):
            continue
        given = a.get("given")
        full = family + (_hangul_key(given) if isinstance(given, str) else "")
        if key in (family, full):
            return True
        # A surname cited on its own: one syllable, or a known two-syllable
        # surname ("남궁"). Any other two-syllable string is a whole name.
        surname_only = len(key) == 1 or key in _KO_TWO_SYLLABLE_SURNAMES
        if surname_only and len(family) > len(key) and family.startswith(key):
            return True
    return any(_hangul_key(org) == key for org in _crossref_organisation_names(message))


def _author_check(
    cited: str, message: dict, threshold: float
) -> tuple[Optional[bool], Optional[str]]:
    """Compare a cited first author with a Crossref record.

    Returns (matched, gap). `matched` is None when there was nothing to compare:
    the record names no person (an organisation-only record), or the two sides
    are in different scripts (`gap` then says which).

    Any listed author counts, so co-author order and equal-contribution
    differences do not warn. Organisation names are extra candidates for a
    cited corporate author. A multi-word cited name that fails as a whole is
    also tried by its last word, for a protected "{John Smith}" from Zotero.
    """
    families = [f for f in _crossref_all_author_families(message) if isinstance(f, str)]
    if not families:
        return None, None
    candidates = families + _crossref_organisation_names(message)
    gap = _script_gap(cited, candidates)
    if gap:
        return None, gap
    if _mostly_hangul(cited):
        return _hangul_author_matches(cited, message), None
    if max(_similar(cited, c) for c in candidates) >= threshold:
        return True, None
    words = cited.split()
    if len(words) > 1 and max(_similar(words[-1], f) for f in families) >= threshold:
        return True, None
    return False, None


def _self_doi(message: dict) -> str:
    doi = message.get("DOI") or message.get("doi") or ""
    return str(doi).strip().lower()


def _update_notice_doi(message: dict, kinds_match) -> Optional[str]:
    """The DOI of a notice issued against *message*, skipping self-references.

    Publishers overwhelmingly deposit ``updated-by`` entries whose DOI is the
    record's *own* DOI rather than the notice's: sampling 400 live records with
    a retraction in ``updated-by``, 185 of 186 publisher-deposited entries
    self-referenced (Retraction Watch-sourced entries are usually right). Emitting
    those produced "Reference appears to be RETRACTED (retraction notice:
    <the paper itself>)", which is nonsense and sends the reader in a circle.

    So a self-referencing DOI is skipped, and if no entry names a *different*
    document we return None and the caller simply omits the notice — an honest
    "we don't know where the notice is" beats a wrong pointer.
    """
    own = _self_doi(message)
    for upd in _updates(message):
        if not kinds_match(_normalize_update_type(upd.get("type"))):
            continue
        doi = upd.get("DOI") or upd.get("doi")
        if not doi:
            continue
        doi = str(doi).strip()
        if doi.lower() == own:
            continue  # points at this very record — not a notice
        return doi
    return None


def _retraction_notice(message: dict) -> Optional[str]:
    """Return the DOI of the retraction notice issued against *message*, if any.

    Read from ``updated-by`` only — never ``update-to``. Elsevier deposits
    ``update-to`` symmetrically (the retracted Lancet paper
    10.1016/S0140-6736(20)31180-6 carries ``update-to`` retractions pointing at
    its own notices), so it identifies neither side of the relationship.
    """
    return _update_notice_doi(message, lambda kind: "retract" in kind)


# The Crossref field naming the notices issued *against* a work. THIS IS THE
# REAL FIELD NAME: an article carries ``updated-by``; the notice carries
# ``update-to`` pointing back at the article. (An earlier version of this file
# read a non-existent ``update-by``, which silently disabled retraction
# detection entirely — see tests/test_retraction_real_shapes.py, whose fixtures
# are copied from live api.crossref.org payloads to keep that honest.)
UPDATED_BY = "updated-by"
UPDATE_TO = "update-to"

# Crossref "update" types other than retraction that a clinical/pharma author
# needs to know about before citing a paper. Maps the normalised Crossref type
# to (human label, severity). Retraction-flavoured types are deliberately absent
# — ``_is_retracted`` already owns those (it substring-matches "retract", which
# also covers "partial_retraction") and reporting both would double up.
#
# The key set is taken from the live `update-type` facet on api.crossref.org
# (i.e. what Crossref actually emits, by volume), not from the schema docs:
# correction 209k, erratum 114k, retraction 74k, new_version 44k, new_edition
# 11k, corrigendum 8.9k, expression_of_concern 4.1k, withdrawal 3.4k, addendum
# 1.7k, removal 697, clarification 503.
#
# Rationale for the severities: an expression of concern, a withdrawal, or a
# removal means the literature itself has flagged the work's integrity — citing
# it silently is exactly the mistake this tool exists to prevent, so those fail
# the run. A correction/erratum/corrigendum/addendum does NOT invalidate the
# paper; it means some reported value may have changed, so it warns and asks the
# author to look.
# Values are (human label, severity, finding code — a CODES key).
_UPDATE_KINDS: dict[str, tuple[str, str, str]] = {
    "expression_of_concern": (
        "has an EXPRESSION OF CONCERN against it", ERROR, "expression-of-concern",
    ),
    "withdrawal": ("has been WITHDRAWN", ERROR, "withdrawal"),
    "removal": ("has been REMOVED", ERROR, "removal"),
    "correction": ("has a correction issued", WARNING, "correction"),
    "erratum": ("has an erratum issued", WARNING, "correction"),
    "corrigendum": ("has a corrigendum issued", WARNING, "correction"),
    "addendum": ("has an addendum", WARNING, "addendum"),
    "clarification": ("has a clarification issued", WARNING, "clarification"),
    "new_edition": ("has a newer edition", WARNING, "new-edition"),
    "new_version": ("has a newer version", WARNING, "new-edition"),
}

# Low-volume spellings Crossref really carries for the same thing (from the same
# facet): "err" (192), "corrected" (54), "corrected-article" (38). Folded rather
# than listed above so the severity table stays readable.
_UPDATE_ALIASES: dict[str, str] = {
    "err": "erratum",
    "corrected": "correction",
    "corrected_article": "correction",
}


def _normalize_update_type(value) -> str:
    """Fold a Crossref update ``type`` to its ``_UPDATE_KINDS`` key.

    Crossref mostly writes these snake_cased and lowercase, but the live data
    also contains ``expression-of-concern``, ``Erratum`` and ``Corrigendum``, so
    case and separator are normalised and known aliases folded.
    """
    key = re.sub(r"[\s\-]+", "_", str(value or "").strip().lower())
    return _UPDATE_ALIASES.get(key, key)


def _updates(message: dict) -> list:
    """The Crossmark update entries issued against *message*.

    Only ``updated-by`` is consulted — on an *article* it names the notices that
    update it, which is the thing we want to warn the author about. ``update-to``
    is the mirror: a record carrying it *is* the notice, and reporting "this has
    a correction issued" about an erratum notice would be backwards.
    """
    return [u for u in _as_list(message.get(UPDATED_BY)) if isinstance(u, dict)]


def _classify_updates(message: dict) -> list[tuple[str, str, Optional[str], str]]:
    """Non-retraction Crossmark updates against *message*, deduplicated.

    Returns (label, severity, notice_doi, code) per distinct update kind.
    Deduplication is by *code*, not by raw type: a paper carrying both an
    "erratum" and a "corrigendum" has had one thing happen to it, and should say
    so once.
    """
    out: list[tuple[str, str, Optional[str], str]] = []
    seen: set[str] = set()
    for upd in _updates(message):
        kind = _normalize_update_type(upd.get("type"))
        if kind not in _UPDATE_KINDS:
            continue
        label, severity, code = _UPDATE_KINDS[kind]
        if code in seen:
            continue
        seen.add(code)
        # Same self-reference problem as retractions — see _update_notice_doi.
        notice = _update_notice_doi(message, lambda k, want=kind: k == want)
        out.append((label, severity, notice, code))
    return out


# Elsevier, Springer, Wiley and NEJM all mark a retracted article by prefixing
# its Crossref title with "RETRACTED:" (and withdrawn preprints with
# "WITHDRAWN:"). This is an entirely independent signal from Crossmark, and it is
# load-bearing: it is often present when Crossmark data is thin. It also has to
# be stripped before comparing titles, or every correctly-cited retracted paper
# reads as a title mismatch (the cited title lacks the publisher's prefix).
_RETRACTED_TITLE_RE = re.compile(
    r"^\s*(?:retracted(?:\s+article)?|withdrawn(?:\s+article)?|removed|temporary\s+removal)"
    r"\s*[:\-–—]\s*",
    re.IGNORECASE,
)


def _strip_retracted_prefix(title: str) -> tuple[str, bool]:
    """Split a publisher "RETRACTED: " marker off *title*.

    Returns (title_without_marker, marker_was_present).
    """
    stripped = _RETRACTED_TITLE_RE.sub("", title, count=1)
    return stripped, stripped != title


def _title_marks_retracted(message: dict) -> bool:
    """True if the publisher prefixed this work's Crossref title with RETRACTED."""
    for t in _as_list(message.get("title")):
        if isinstance(t, str) and _RETRACTED_TITLE_RE.match(_plain(t)):
            return True
    return False


def _relation_ids(message: dict, wanted: str) -> list[str]:
    """DOIs listed under Crossref ``relation[<wanted>]``, normalised.

    Crossref keys are hyphenated (``is-preprint-of``); tolerate underscores too.
    """
    from .parsers import normalize_doi_field

    relation = message.get("relation")
    if not isinstance(relation, dict):
        return []
    out: list[str] = []
    for key, entries in relation.items():
        if not isinstance(key, str):
            continue
        if key.strip().lower().replace("_", "-") != wanted:
            continue
        for entry in _as_list(entries):
            if not isinstance(entry, dict):
                continue
            if str(entry.get("id-type", "doi")).lower() != "doi":
                continue
            doi = normalize_doi_field(str(entry.get("id", "")))
            if doi:
                out.append(doi)
    return out


def _published_version_doi(message: dict) -> Optional[str]:
    """The DOI of the peer-reviewed version of a preprint, if Crossref knows one.

    A preprint record carries ``relation["is-preprint-of"]`` pointing at the
    version of record. Citing the preprint when the paper has since been
    published in a journal is one of the most common (and most correctable)
    slips in a clinical manuscript — the numbers frequently change in peer
    review, so the preprint's results may no longer be what the author means.
    """
    dois = _relation_ids(message, "is-preprint-of")
    return dois[0] if dois else None


def _is_retracted(message: dict) -> bool:
    """Best-effort retraction detection against a Crossref *article* record.

    Three independent signals, any of which is sufficient:

    1. ``updated-by`` carries a retraction-flavoured update — the Crossmark
       model, and the primary signal. This is the field an *article* carries to
       name the notices issued against it.
    2. An ``is-retracted-by`` relation (the retracted work pointing at its
       notice). We deliberately do NOT match ``is-retraction-of`` — that
       identifies the notice itself, not a retracted paper.
    3. The publisher prefixed the title with "RETRACTED:" — see
       ``_RETRACTED_TITLE_RE``. Independent of Crossmark and often present when
       Crossmark data is thin.

    ``update-to`` is deliberately NOT consulted. In principle it marks the
    notice, but Elsevier deposits it *symmetrically* — the retracted Lancet
    paper 10.1016/S0140-6736(20)31180-6 carries ``update-to`` retractions
    pointing at its own notices — so it identifies neither side reliably. Every
    real record we checked is classified correctly from ``updated-by`` alone.

    Note: Crossref's retraction coverage is incomplete, so a clean result is not
    a guarantee — see the README caveat.
    """
    if str(message.get("type", "")).lower() == "retraction":
        return True
    for upd in _updates(message):
        if "retract" in _normalize_update_type(upd.get("type")):
            return True
    relation = message.get("relation")
    if isinstance(relation, dict):
        for key in relation:
            if isinstance(key, str) and "retracted-by" in key.lower():
                return True
    return _title_marks_retracted(message)


def _bibliographic_query(ref: Reference) -> str:
    """Build a Crossref ``query.bibliographic`` string for a DOI-less reference.

    With structured fields, a title+author+journal+year blob matches best. With
    only free text (a pasted reference list), the raw line *is* the bibliographic
    string Crossref's matcher expects — it is trimmed only to keep the URL sane.

    The raw-line fallback is deliberately restricted to *free-text* references
    (``heuristic_fields``), and this restriction is load-bearing for privacy.
    ``ref.raw`` is the whole input line in text mode, but in CSV mode it is
    **every cell of the row joined together** (``parsers.parse_csv``), and in
    BibTeX/RIS it is the whole entry. A clinical screening table exported from
    Excel or Covidence routinely carries Study-ID, MRN, Notes and Comments
    columns next to the bibliographic ones, so a row whose Title cell happened
    to be empty used to be URL-encoded verbatim into a GET query string to
    api.crossref.org — e.g. ``query.bibliographic=S-002,,Kim,,subject 88213
    relapsed, PHQ9=19, MRN 4429981`` — where it lands in third-party access logs
    and any intercepting institutional proxy. That is patient data leaving the
    machine, for a lookup that could not have worked anyway.

    It could not have worked because the same text is *also* useless as a query:
    a structured reference with no title has no bibliographic string to match
    on, so Crossref ranks against clinical notes and ``_score_candidate`` throws
    the result away. So we skip the search entirely rather than pay a network
    call to leak a row. The reference still reports ``no-doi`` as before.
    """
    if ref.title:
        parts = [ref.title]
        for extra in (ref.author, ref.journal, str(ref.year) if ref.year else None):
            if extra:
                parts.append(extra)
        return " ".join(" ".join(p.split()) for p in parts)[:500]
    if ref.heuristic_fields:
        return " ".join(ref.raw.split())[:500]
    return ""


# A suggestion is only worth showing if it is almost certainly the same paper —
# a wrong DOI confidently offered is worse than no suggestion at all, because the
# author may paste it in unchecked. Both thresholds are far above the ones used
# to *compare* an already-cited DOI (0.80), where the DOI itself is the evidence.
SUGGEST_TITLE_THRESHOLD = 0.92
SUGGEST_OVERLAP_THRESHOLD = 0.80
# A title must carry this many distinctive words before a similarity score means
# anything. Without it, `title={Editorial}` scores a perfect 1.00 against any of
# the thousands of works titled "Editorial" and we confidently emit whichever one
# Crossref's ranker happened to put first. A high similarity on a short string
# discriminates typos, not papers.
SUGGEST_MIN_TITLE_TOKENS = 4


def _author_families_match(ref: Reference, candidate: dict, threshold: float = 0.85) -> bool:
    """Does *ref*'s cited surname appear among *candidate*'s authors?

    Only meaningful when both sides state one; "unknown" must not mean "reject",
    or every author-less reference loses its suggestion.
    """
    if not ref.author:
        return True
    matched, _gap = _author_check(ref.author, candidate, threshold)
    return matched is not False


def _score_candidate(ref: Reference, candidate: dict) -> float:
    """How confidently does *candidate* match a DOI-less *ref*? 0.0 = reject.

    Titles are compared directly when the reference has one. For a free-text
    reference we instead require the cited line to *contain* most of the
    candidate's title words — the same signal ``_check_text_mentions_title``
    uses, since a real citation quotes its source's title.

    Three independent hard rejects, each guarding a way a plausible-looking
    candidate turns out to be a different paper:

    * **Year disagreement.** Crossref's ranker happily returns a same-titled
      paper from a different year (a later edition, a conference/journal pair).
    * **Author disagreement.** Title similarity alone cannot tell two papers
      with the same generic title apart; the surname can.
    * **Too little title to judge.** See ``SUGGEST_MIN_TITLE_TOKENS``.

    A field neither side states is never a reject — only a stated *disagreement*
    is. And a field the parser *guessed* is not "stated" at all: when
    ``ref.heuristic_fields`` is set, year and author were scraped out of free
    text and are not evidence either way, so neither can veto. This mirrors
    ``_compare``, which already skips exactly these two checks for the same
    references and the same reason — the two had drifted apart, so a guess good
    enough to be distrusted for *reporting* was still trusted to silently delete
    a correct suggestion:

    * ``find_year`` takes the first 4-digit token in the line, so "a randomised
      trial of digital therapy in 2000 patients … 2019;42:11-19" yields
      ``year=2000`` and vetoed the perfect 2019 match.
    * ``_guess_text_author`` takes the first capitalised word, so a line
      beginning "In: Smith J, editor." yields ``author='In'``, which matches no
      Crossref surname and vetoed everything.

    Both produced score 0.0 — no suggestion, no explanation — on references
    where the title matched exactly. What remains is still a high bar: the
    free-text branch below requires 80% of the candidate's title words to appear
    in the cited line.
    """
    cr_titles = _crossref_title_candidates(candidate)
    if not cr_titles:
        return 0.0

    if not ref.heuristic_fields:
        cr_years = _crossref_years(candidate)
        if ref.year and cr_years and ref.year not in cr_years:
            return 0.0

        if not _author_families_match(ref, candidate):
            return 0.0

    if ref.title:
        # DISTINCT words, not word occurrences. Counting the list let a repeated
        # word satisfy the guard that exists precisely to reject titles with too
        # little signal: "Erratum. Erratum. Erratum. Erratum." counts as 4 tokens
        # but carries exactly 1 distinctive word, scored 1.00 against any work
        # titled "Erratum", and was emitted as "Crossref has a 100%-confident
        # match — consider citing DOI ...". The candidate side of this same guard
        # (below) already counted a set; the two disagreed.
        if len(set(_alpha_tokens(ref.title))) < SUGGEST_MIN_TITLE_TOKENS:
            return 0.0
        score = max(_similar(ref.title, t) for t in cr_titles)
        return score if score >= SUGGEST_TITLE_THRESHOLD else 0.0

    block_tokens = set(_alpha_tokens(ref.raw))
    if len(block_tokens) < 4:
        return 0.0  # a bare "no DOI" stub — nothing to corroborate against
    best = 0.0
    for title in cr_titles:
        title_tokens = set(_alpha_tokens(title))
        if len(title_tokens) < SUGGEST_MIN_TITLE_TOKENS:
            continue  # a generic candidate title can't be confidently matched
        best = max(best, len(title_tokens & block_tokens) / len(title_tokens))
    return best if best >= SUGGEST_OVERLAP_THRESHOLD else 0.0


def _suggest_doi(ref: Reference, result: CheckResult, client: CrossrefClient) -> None:
    """Add a DOI suggestion for a DOI-less *ref*, if Crossref holds a confident match.

    Best-effort and never raising: a search failure becomes a "Lookup failed"
    warning (so the run reads as inconclusive rather than a false clean pass),
    and a merely-plausible match is silently dropped rather than guessed at.
    """
    query = _bibliographic_query(ref)
    if not query:
        return
    try:
        candidates = client.search(query)
    except Exception as e:
        result.add(
            WARNING,
            f"Lookup failed while searching Crossref for a missing DOI "
            f"({type(e).__name__}) — no suggestion available.",
            "lookup-failed",
        )
        return

    best_doi: Optional[str] = None
    best_score = 0.0
    best_record: Optional[dict] = None
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        score = _score_candidate(ref, candidate)
        if score > best_score:
            doi = candidate.get("DOI") or candidate.get("doi")
            if isinstance(doi, str) and doi.strip():
                best_doi, best_score, best_record = doi.strip(), score, candidate

    if not best_doi or best_record is None:
        return
    cr_title = _crossref_title(best_record) or "?"
    years = sorted(_crossref_years(best_record))
    year_disp = years[0] if years else "?"
    who = f"{_crossref_first_author(best_record) or '?'} ({year_disp}) — {cr_title}"

    # NEVER recommend citing a retracted work. This path used to skip the
    # retraction check entirely — and, because `_crossref_title_candidates`
    # strips the publisher's "RETRACTED: " marker, it also deleted the one
    # visual cue the author had. The result: "Crossref has a 100%-confident
    # match — consider citing DOI 10.1016/s0140-6736(97)11096-0", i.e. the tool
    # actively recommended Wakefield 1998, at exit code 0.
    #
    # `_title_marks_retracted` is checked alongside `_is_retracted` because a
    # Hindawi/MDPI retraction *notice* is titled "Retracted: <original title>",
    # which — once stripped — matches the original paper perfectly and would be
    # suggested as if it were the paper. Either way the author must go look.
    if _is_retracted(best_record) or _title_marks_retracted(best_record):
        result.add(
            ERROR,
            f"This reference cites no DOI, and the Crossref record that matches "
            f"it ({best_score:.0%}) is marked RETRACTED — do not cite it without "
            f"checking:\n"
            f"    match: {who}\n"
            f"    Crossref DOI: {best_doi}",
            "retracted",
        )
        return

    result.add(
        WARNING,
        f"Crossref has a {best_score:.0%}-confident match for this reference — "
        f"consider citing DOI {best_doi}\n"
        f"    match: {who}",
        "doi-suggestion",
    )


def check_reference(
    ref: Reference,
    client: CrossrefClient,
    title_threshold: float = 0.80,
    author_threshold: float = 0.85,
    journal_threshold: float = 0.82,
    resolve_unknown: bool = True,
    pubmed: Optional["PubMedClient"] = None,
    suggest_missing: bool = False,
) -> CheckResult:
    """Verify a single reference against Crossref and return findings.

    Never raises on a malformed Crossref record or lookup failure — such
    problems become WARNINGs so one bad record cannot abort a whole batch.

    When a *pubmed* client is supplied and the reference carries a PMID, also
    cross-checks PubMed for a retraction PubMed marks but Crossref may miss, and
    for PMID↔DOI consistency (a classic copy-paste-from-two-records error).

    When *suggest_missing* is set, a reference with no DOI is looked up by its
    bibliographic details so the report can name the DOI the author should add,
    instead of only saying it could not be verified.
    """
    result = CheckResult(reference=ref)

    if pubmed is not None and ref.pmid:
        _pubmed_crosscheck(ref, result, pubmed)

    if not ref.doi:
        pmid_note = (
            f" (PMID {ref.pmid} present — look it up at "
            f"https://pubmed.ncbi.nlm.nih.gov/{ref.pmid}/)"
            if ref.pmid
            else ""
        )
        result.add(
            WARNING, f"No DOI found — cannot verify against Crossref.{pmid_note}", "no-doi"
        )
        if suggest_missing:
            _suggest_doi(ref, result, client)
        return result

    try:
        message = client.fetch(ref.doi)
    except Exception as e:  # network/transport failure, not a citation problem
        result.add(WARNING, f"Lookup failed ({type(e).__name__}): {e}", "lookup-failed")
        return result

    if message is None:
        # Not in Crossref's works index. Distinguish "broken DOI" from "resolves
        # but not a Crossref journal article" (e.g. a DataCite/dataset DOI) — and
        # never turn a transient doi.org failure into a false hard error.
        if resolve_unknown:
            try:
                resolved = client.resolve(ref.doi)
            except Exception as e:
                result.add(
                    WARNING,
                    f"Lookup failed while confirming the DOI resolves "
                    f"({type(e).__name__}) — could not verify: {ref.doi}",
                    "lookup-failed",
                )
                return result
        else:
            resolved = False
        if resolved:
            result.add(
                WARNING,
                f"DOI resolves but is not in Crossref — metadata not verified: {ref.doi}. "
                f"It may be a dataset/preprint/DataCite DOI.",
                "doi-not-in-crossref",
            )
        else:
            result.add(
                ERROR,
                f"DOI does not resolve anywhere (Crossref or doi.org) — "
                f"check for a typo: {ref.doi}",
                "doi-not-resolving",
            )
        return result

    if not isinstance(message, dict):
        result.add(
            WARNING,
            "Crossref returned an unexpected record shape — could not verify.",
            "bad-record",
        )
        return result

    result.resolved_doi = message.get("DOI", ref.doi)
    result.crossref = message

    try:
        _compare(ref, message, result, title_threshold, author_threshold, journal_threshold)
    except Exception as e:  # never let one weird record abort the batch
        result.add(WARNING, f"Could not fully compare metadata ({type(e).__name__}).", "bad-record")

    if not result.findings:
        cr_author = _crossref_first_author(message)
        cr_year = sorted(_crossref_years(message))
        cr_title = _crossref_title(message)
        year_disp = cr_year[0] if cr_year else "?"
        note = _not_compared_note(result.not_compared)
        result.add(
            OK,
            f"Verified: {cr_author or '?'} ({year_disp}) — {cr_title or ref.doi}{note}",
            "verified",
        )
    return result


def _pubmed_crosscheck(ref: Reference, result: CheckResult, pubmed: "PubMedClient") -> None:
    """Cross-check a reference's PMID against PubMed (retraction + DOI match).

    Best-effort: a lookup failure becomes a "Lookup failed" warning (so the run
    reads as inconclusive, never a false clean pass) and never raises.
    """
    try:
        record = pubmed.fetch(ref.pmid)
    except Exception as e:
        result.add(
            WARNING,
            f"Lookup failed (PubMed, {type(e).__name__}) — PMID {ref.pmid} "
            f"could not be cross-checked.",
            "lookup-failed",
        )
        return
    if not isinstance(record, dict):
        # None (not found) or, defensively, any non-dict a transport might return
        # — mirror the Crossref path's shape guard rather than trusting the record.
        result.add(
            WARNING,
            f"PMID {ref.pmid} not found in PubMed — could not cross-check.",
            "pmid-not-found",
        )
        return
    if _pubmed_is_retracted(record):
        result.add(
            ERROR,
            f"Reference appears to be RETRACTED according to PubMed (PMID {ref.pmid}).",
            "retracted",
        )
    pdoi = _pubmed_doi(record)
    if pdoi and ref.doi and pdoi.lower() != ref.doi.lower():
        result.add(
            WARNING,
            f"PMID/DOI mismatch: PMID {ref.pmid} is registered to DOI {pdoi}, "
            f"but the citation gives {ref.doi} — one of the two is wrong.",
            "pmid-doi-mismatch",
        )
    elif pdoi and not ref.doi:
        result.add(
            WARNING,
            f"No DOI cited, but PMID {ref.pmid} maps to DOI {pdoi} in PubMed — "
            f"consider adding it.",
            "pmid-doi-mismatch",
        )


def _compare(
    ref: Reference,
    message: dict,
    result: CheckResult,
    title_threshold: float,
    author_threshold: float,
    journal_threshold: float = 0.82,
) -> None:
    if _is_retracted(message):
        notice = _retraction_notice(message)
        extra = f" (retraction notice: {notice})" if notice else ""
        result.add(
            ERROR, f"Reference appears to be RETRACTED according to Crossref.{extra}", "retracted"
        )
    else:
        # Only report the softer integrity flags when the work is not already
        # retracted — a retraction subsumes them, and stacking both is noise.
        for label, severity, notice, code in _classify_updates(message):
            extra = f" (notice: {notice})" if notice else ""
            result.add(severity, f"Crossref says this reference {label}.{extra}", code)

    published = _published_version_doi(message)
    if published:
        result.add(
            WARNING,
            f"This is a preprint — a peer-reviewed version is published as "
            f"{published}. Cite that instead (results often change in review).",
            "preprint-published",
        )

    # Title comparison — compare against the best-matching Crossref candidate
    # (main title, or main+subtitle) so a cited subtitle is not a false mismatch.
    cr_titles = _crossref_title_candidates(message)
    if ref.title and cr_titles:
        gap = _script_gap(ref.title, cr_titles)
        if gap:
            result.not_compared.append(("title", gap))
        else:
            best = max(_similar(ref.title, t) for t in cr_titles)
            if best < title_threshold:
                result.add(
                    WARNING,
                    f"Title mismatch ({best:.0%} similar):\n"
                    f"    cited:    {ref.title}\n"
                    f"    crossref: {cr_titles[0]}",
                    "title-mismatch",
                )

    # Year and first-author were guessed from free text when heuristic_fields is
    # set (e.g. "2000 patients … 2019" or a title-first line); those guesses are
    # unreliable, so skip their mismatch checks to avoid false alarms. DOI and
    # retraction checks still apply, and — since a free-text reference usually
    # quotes the paper's title verbatim — we can still catch a swapped DOI by
    # checking the cited text actually mentions the Crossref title.
    if ref.heuristic_fields:
        gap = _script_gap(_without_identifiers(ref.raw), cr_titles) if cr_titles else None
        if gap:
            result.not_compared.append(("title", gap))
        else:
            _check_text_mentions_title(ref, cr_titles, result)
        return

    # Year comparison — match against *any* of Crossref's dates.
    cr_years = _crossref_years(message)
    if ref.year and cr_years and ref.year not in cr_years:
        shown = ", ".join(str(y) for y in sorted(cr_years))
        result.add(
            WARNING, f"Year mismatch: cited {ref.year}, Crossref says {shown}.", "year-mismatch"
        )

    # First-author comparison — tolerate diacritics; also accept a match against
    # any listed author (co-author ordering / equal-contribution differences).
    if ref.author:
        matched, gap = _author_check(ref.author, message, author_threshold)
        if gap:
            result.not_compared.append(("author", gap))
        elif matched is False:
            families = _crossref_all_author_families(message)
            result.add(
                WARNING,
                f"First-author mismatch: cited '{ref.author}', "
                f"Crossref says '{families[0]}'.",
                "author-mismatch",
            )

    # Journal / container comparison — a second, independent signal for a
    # swapped DOI. Permissive on purpose (journal names are heavily abbreviated
    # across styles): only a clear mismatch, not an abbreviation, warns.
    if ref.journal:
        containers = _crossref_container_candidates(message)
        gap = _script_gap(ref.journal, containers) if containers else None
        if gap:
            result.not_compared.append(("journal", gap))
        elif containers and not _journal_matches(ref.journal, containers, journal_threshold):
            result.add(
                WARNING,
                f"Journal mismatch: cited '{ref.journal}', "
                f"Crossref says '{containers[0]}'.",
                "journal-mismatch",
            )


# URLs and DOIs inside a free-text citation. They are Latin letters whatever
# the language of the citation, so they are left out when judging its script.
_IDENTIFIER_RE = re.compile(r"https?://\S+|\b10\.\d{4,}/\S+|\bdoi:\s*", re.IGNORECASE)


def _without_identifiers(text: str) -> str:
    return _IDENTIFIER_RE.sub(" ", text or "")


def _alpha_tokens(text: str) -> list[str]:
    """Diacritic-folded words of letters (any script), of length >= 3.

    Splits on any non-letter so "COVID-19" -> ["covid"] and "neural-networks" ->
    ["neural", "networks"], rather than dropping such tokens wholesale (which
    made overlap sensitive to punctuation differences between the citation and
    the Crossref record).

    Letters of every script count. Only a-z did before, so a Korean title
    produced no tokens at all and a correct Korean citation of a Korean record
    was reported as text-title-missing. The length is counted after NFKD, where
    a Hangul syllable is two or three jamo, so a one-syllable particle such as
    "의" does not count as a word.
    """
    return [t for t in re.findall(r"[^\W\d_]+", _norm(text)) if len(t) >= 3]


def _check_text_mentions_title(
    ref: Reference, cr_titles: list[str], result: CheckResult
) -> None:
    """For a free-text reference, warn if it doesn't mention the Crossref title.

    A correct free-text citation quotes the paper's title, so a low word overlap
    is a strong signal the DOI points to a *different* paper (a swapped DOI).
    Conservative on purpose: skipped entirely for bare-DOI lines (no prose to
    match), and only fires when overlap is well below half.
    """
    if not cr_titles:
        return
    block_tokens = set(_alpha_tokens(ref.raw))
    if len(block_tokens) < 4:
        return  # bare DOI / too little prose to judge — don't guess
    best_overlap = 0.0
    judged = False
    for title in cr_titles:
        title_tokens = set(_alpha_tokens(title))
        if not title_tokens:
            continue
        judged = True
        overlap = len(title_tokens & block_tokens) / len(title_tokens)
        best_overlap = max(best_overlap, overlap)
    if not judged:
        return  # a title of only short words gives nothing to look for
    if best_overlap < 0.5:
        result.add(
            WARNING,
            f"Cited text does not mention the Crossref title — possibly the wrong DOI:\n"
            f"    crossref: {cr_titles[0]}",
            "text-title-missing",
        )
