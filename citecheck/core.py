"""Crossref lookups and claim-vs-record comparison."""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Optional

from .parsers import Reference

CROSSREF_API = "https://api.crossref.org/works/"
DEFAULT_UA = "citecheck/0.1 (https://github.com/hyeonjoong/citecheck; mailto:citecheck@example.com)"

# Severity levels for findings.
OK = "ok"
WARNING = "warning"
ERROR = "error"


@dataclass
class Finding:
    severity: str
    message: str


@dataclass
class CheckResult:
    reference: Reference
    findings: list[Finding] = field(default_factory=list)
    resolved_doi: Optional[str] = None
    crossref: Optional[dict] = None

    @property
    def status(self) -> str:
        if any(f.severity == ERROR for f in self.findings):
            return ERROR
        if any(f.severity == WARNING for f in self.findings):
            return WARNING
        return OK

    def add(self, severity: str, message: str) -> None:
        self.findings.append(Finding(severity, message))


class CrossrefClient:
    """Tiny Crossref client over the standard library (no third-party deps)."""

    def __init__(
        self,
        mailto: Optional[str] = None,
        timeout: float = 15.0,
        retries: int = 2,
        sleep: float = 1.0,
        _fetch=None,
    ):
        self.timeout = timeout
        self.retries = retries
        self.sleep = sleep
        self.user_agent = (
            f"citecheck/0.1 (https://github.com/hyeonjoong/citecheck; mailto:{mailto})"
            if mailto
            else DEFAULT_UA
        )
        # _fetch lets tests inject a fake transport: doi -> dict | None.
        self._fetch = _fetch

    def fetch(self, doi: str) -> Optional[dict]:
        """Return the Crossref `message` for *doi*, or None if not found."""
        if self._fetch is not None:
            return self._fetch(doi)
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
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
                last_err = e
            if attempt < self.retries:
                time.sleep(self.sleep * (attempt + 1))
        if last_err:
            raise last_err
        return None


def _norm(s: str) -> str:
    return " ".join(s.lower().split())


def _similar(a: str, b: str) -> float:
    return SequenceMatcher(None, _norm(a), _norm(b)).ratio()


def _crossref_title(message: dict) -> Optional[str]:
    titles = message.get("title") or []
    return titles[0] if titles else None


def _crossref_year(message: dict) -> Optional[int]:
    for key in ("published-print", "published-online", "published", "issued"):
        parts = (message.get(key) or {}).get("date-parts") or []
        if parts and parts[0] and parts[0][0]:
            return int(parts[0][0])
    return None


def _crossref_first_author(message: dict) -> Optional[str]:
    for a in message.get("author") or []:
        if a.get("family"):
            return a["family"]
    return None


def _is_retracted(message: dict) -> bool:
    if str(message.get("type", "")).lower() == "retraction":
        return True
    for upd in message.get("update-to") or []:
        if "retract" in str(upd.get("type", "")).lower():
            return True
    # Crossref exposes retraction notices via relation/update-policy too.
    for rel in (message.get("relation") or {}):
        if "retract" in rel.lower():
            return True
    return False


def check_reference(
    ref: Reference,
    client: CrossrefClient,
    title_threshold: float = 0.80,
) -> CheckResult:
    """Verify a single reference against Crossref and return findings."""
    result = CheckResult(reference=ref)

    if not ref.doi:
        result.add(WARNING, "No DOI found — cannot verify against Crossref.")
        return result

    try:
        message = client.fetch(ref.doi)
    except Exception as e:  # network/transport failure, not a citation problem
        result.add(WARNING, f"Lookup failed ({type(e).__name__}): {e}")
        return result

    if message is None:
        result.add(ERROR, f"DOI does not resolve on Crossref: {ref.doi}")
        return result

    result.resolved_doi = message.get("DOI", ref.doi)
    result.crossref = message

    if _is_retracted(message):
        result.add(ERROR, "Reference appears to be RETRACTED according to Crossref.")

    # Title comparison.
    cr_title = _crossref_title(message)
    if ref.title and cr_title:
        score = _similar(ref.title, cr_title)
        if score < title_threshold:
            result.add(
                WARNING,
                f"Title mismatch ({score:.0%} similar):\n"
                f"    cited:    {ref.title}\n"
                f"    crossref: {cr_title}",
            )

    # Year comparison.
    cr_year = _crossref_year(message)
    if ref.year and cr_year and ref.year != cr_year:
        result.add(WARNING, f"Year mismatch: cited {ref.year}, Crossref says {cr_year}.")

    # First-author comparison.
    cr_author = _crossref_first_author(message)
    if ref.author and cr_author:
        if _similar(ref.author, cr_author) < 0.85:
            result.add(
                WARNING,
                f"First-author mismatch: cited '{ref.author}', Crossref says '{cr_author}'.",
            )

    if not result.findings:
        result.add(OK, f"Verified: {cr_author or '?'} ({cr_year or '?'}) — {cr_title or ref.doi}")
    return result
