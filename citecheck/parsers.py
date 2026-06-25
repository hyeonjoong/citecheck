"""Reference parsing: BibTeX, plain DOI lists, and free-text reference lists.

The goal is to extract, for each reference, whatever the author *claimed* —
DOI, title, first-author surname, and year — so the verifier can compare those
claims against Crossref's authoritative record.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional


# A DOI is "10." followed by a registrant code and a suffix. This pattern is
# deliberately permissive on the suffix and strips common trailing punctuation.
_DOI_RE = re.compile(r"10\.\d{4,9}/[^\s\"'<>,;)\]}]+", re.IGNORECASE)
_YEAR_RE = re.compile(r"\b(1[6-9]\d{2}|20\d{2})\b")


@dataclass
class Reference:
    """A single citation as the author wrote it."""

    raw: str
    doi: Optional[str] = None
    title: Optional[str] = None
    author: Optional[str] = None  # first-author surname, best effort
    year: Optional[int] = None
    key: Optional[str] = None  # BibTeX cite key, if available
    fields: dict = field(default_factory=dict)

    def label(self) -> str:
        """A short human-readable identifier for reports."""
        if self.key:
            return self.key
        if self.doi:
            return self.doi
        if self.author and self.year:
            return f"{self.author} ({self.year})"
        snippet = self.raw.strip().replace("\n", " ")
        return (snippet[:50] + "…") if len(snippet) > 50 else snippet


def _clean_doi(doi: str) -> str:
    return doi.strip().rstrip(".").lower()


def find_doi(text: str) -> Optional[str]:
    """Return the first DOI found in *text*, normalised, or None."""
    m = _DOI_RE.search(text)
    return _clean_doi(m.group(0)) if m else None


def find_year(text: str) -> Optional[int]:
    m = _YEAR_RE.search(text)
    return int(m.group(0)) if m else None


# --- BibTeX -----------------------------------------------------------------

_ENTRY_RE = re.compile(r"@(\w+)\s*\{\s*([^,]*),", re.IGNORECASE)


def _split_bibtex_entries(text: str) -> list[tuple[str, str, str]]:
    """Yield (entry_type, cite_key, body) for each @entry{...} via brace matching."""
    entries = []
    for m in _ENTRY_RE.finditer(text):
        entry_type, key = m.group(1), m.group(2).strip()
        # Walk forward from the opening brace to its match.
        start = text.index("{", m.start())
        depth = 0
        for i in range(start, len(text)):
            c = text[i]
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    body = text[start + 1 : i]
                    entries.append((entry_type, key, body))
                    break
    return entries


_FIELD_RE = re.compile(r"(\w+)\s*=\s*", re.IGNORECASE)


def _parse_bibtex_fields(body: str) -> dict:
    """Parse `field = {value}` / `field = "value"` / `field = value` pairs."""
    fields: dict = {}
    i = 0
    n = len(body)
    while i < n:
        m = _FIELD_RE.search(body, i)
        if not m:
            break
        name = m.group(1).lower()
        j = m.end()
        if j >= n:
            break
        if body[j] == "{":
            depth = 0
            for k in range(j, n):
                if body[k] == "{":
                    depth += 1
                elif body[k] == "}":
                    depth -= 1
                    if depth == 0:
                        fields[name] = body[j + 1 : k]
                        i = k + 1
                        break
            else:
                break
        elif body[j] == '"':
            for k in range(j + 1, n):
                if body[k] == '"':
                    fields[name] = body[j + 1 : k]
                    i = k + 1
                    break
            else:
                break
        else:
            end = body.find(",", j)
            if end == -1:
                end = n
            fields[name] = body[j:end].strip()
            i = end + 1
    return fields


def _clean_bibtex_value(value: str) -> str:
    value = value.replace("\n", " ")
    value = re.sub(r"[{}]", "", value)
    return re.sub(r"\s+", " ", value).strip()


def _first_author_surname(author_field: str) -> Optional[str]:
    """Best-effort first-author surname from a BibTeX `author` field."""
    if not author_field:
        return None
    first = re.split(r"\band\b", author_field, maxsplit=1)[0].strip()
    first = _clean_bibtex_value(first)
    if "," in first:  # "Surname, Given"
        return first.split(",")[0].strip() or None
    parts = first.split()
    return parts[-1] if parts else None


def parse_bibtex(text: str) -> list[Reference]:
    refs = []
    for entry_type, key, body in _split_bibtex_entries(text):
        fields = _parse_bibtex_fields(body)
        title = _clean_bibtex_value(fields.get("title", "")) or None
        author = _first_author_surname(fields.get("author", ""))
        year = None
        if fields.get("year"):
            year = find_year(fields["year"])
        doi = fields.get("doi")
        doi = _clean_doi(doi) if doi else None
        refs.append(
            Reference(
                raw=body.strip(),
                doi=doi,
                title=title,
                author=author,
                year=year,
                key=key or None,
                fields={"type": entry_type, **fields},
            )
        )
    return refs


# --- Plain text / DOI lists -------------------------------------------------


def parse_text(text: str) -> list[Reference]:
    """Parse a newline- or blank-line-separated list of references.

    Each non-empty line (or paragraph) becomes one reference. A bare DOI line
    is treated as a DOI-only reference.
    """
    # Split on blank lines if present, otherwise on single newlines.
    blocks = re.split(r"\n\s*\n", text.strip()) if "\n\n" in text else text.strip().splitlines()
    refs = []
    for block in blocks:
        block = block.strip()
        if not block:
            continue
        doi = find_doi(block)
        year = find_year(block)
        author = _guess_text_author(block)
        refs.append(Reference(raw=block, doi=doi, year=year, author=author))
    return refs


def _guess_text_author(block: str) -> Optional[str]:
    """Grab a leading surname from a reference string like 'Kim H, Lee S. ...'."""
    m = re.match(r"\s*\[?\d*\]?\.?\s*([A-Z][A-Za-z'\-]+)", block)
    return m.group(1) if m else None


def parse_references(text: str, fmt: str = "auto") -> list[Reference]:
    """Parse *text* into references.

    fmt: "bibtex", "text", or "auto" (detect from content).
    """
    if fmt == "auto":
        fmt = "bibtex" if "@" in text and "{" in text else "text"
    if fmt == "bibtex":
        return parse_bibtex(text)
    return parse_text(text)
