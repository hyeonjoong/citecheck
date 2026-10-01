"""Reference parsing: BibTeX, RIS, CSL-JSON, CSV/TSV, plain DOI lists, free text.

The goal is to extract, for each reference, whatever the author *claimed* —
DOI, title, first-author surname, year, journal, and PMID — so the verifier can
compare those claims against Crossref's authoritative record. RIS (EndNote /
Zotero / Mendeley export) and CSL-JSON (Zotero / Better BibTeX) are handled
alongside BibTeX because clinical/pharma reference managers export those far
more often than raw ``.bib``; CSV/TSV is handled because reference tables kept
in Excel/Sheets (a very common way to track a manuscript's citations, or the
included-studies table of a systematic review) are otherwise unusable here.
"""

from __future__ import annotations

import bisect
import csv
import io
import json
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Optional


# A DOI is "10." followed by a registrant code and a suffix. The suffix is
# deliberately permissive (DOIs may contain balanced parentheses, e.g.
# Elsevier's ``10.1016/S0140-6736(97)11096-0``); we only exclude whitespace,
# angle brackets, and quote characters, then trim trailing punctuation with
# bracket balancing in ``_clean_doi``. The registrant code keeps a >=4-digit
# lower bound so clinical dosing text like "10.55/kg" is not misread as a DOI.
_DOI_RE = re.compile(r"10\.\d{4,}/[^\s<>\"']+", re.IGNORECASE)
_YEAR_RE = re.compile(r"\b(1[6-9]\d{2}|20\d{2})\b")

# Common human-facing prefixes wrapped around a DOI (reference managers export
# DOIs as full URLs or with a "doi:" label).
_DOI_PREFIX_RE = re.compile(
    r"^\s*(?:https?://(?:dx\.)?doi\.org/|https?://|doi:\s*)",
    re.IGNORECASE,
)
_DOI_TRAIL = ".,;:'\"”’`)]}>"

# Unicode format characters (category Cf) — zero-width space, soft hyphen, BOM,
# bidi marks. Invisible, so a DOI carrying one looks correct to the author while
# resolving nowhere. See ``_clean_doi``.
_INVISIBLE_RE = re.compile(r"[­​-‏‪-‮⁠-⁤﻿]")
# Everything outside printable ASCII. A DOI is truncated here — see ``_clean_doi``.
_NON_ASCII_RE = re.compile(r"[^\x20-\x7e]")

# A PubMed identifier is a bare integer, but we only trust it when it is
# explicitly labelled "PMID" (or given as a PubMed URL), so we never mistake a
# page number, sample size, or accession for one. Up to 9 digits (PubMed's
# current ceiling is ~8), and we forbid a following digit so a longer run isn't
# truncated into a bogus PMID.
# NOTE: the label uses a single ``[\s:]*`` class rather than ``\s*:?\s*``. Two
# adjacent ``\s*`` quantifiers can partition a whitespace run many ways, which
# is catastrophic O(N^2) backtracking on a crafted "pmid<many spaces>" input
# (a ReDoS reachable from any untrusted .bib/.ris/.txt). One character class is
# linear.
_PMID_RE = re.compile(
    r"(?:pmid[\s:]*|pubmed(?:\.ncbi\.nlm\.nih\.gov)?/)(\d{1,9})(?!\d)",
    re.IGNORECASE,
)


@dataclass
class Reference:
    """A single citation as the author wrote it."""

    raw: str
    doi: Optional[str] = None
    title: Optional[str] = None
    author: Optional[str] = None  # first-author surname, best effort
    year: Optional[int] = None
    journal: Optional[str] = None  # container / journal name, if given
    pmid: Optional[str] = None  # PubMed ID, if explicitly labelled
    key: Optional[str] = None  # BibTeX cite key, if available
    fields: dict = field(default_factory=dict)
    # True when author/year were *guessed* from free text (unstructured), so the
    # verifier should not raise noisy author/year mismatches on them.
    heuristic_fields: bool = False

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


def _clean_doi(doi: str) -> Optional[str]:
    """Normalise a raw DOI string: strip URL/``doi:`` prefix and trailing junk.

    Trailing punctuation is stripped, but a closing bracket that has a matching
    opener inside the DOI is preserved (so ``(97)11096-0`` survives while a
    wrapping ``)`` from "(doi: 10.x)" is removed).

    Invisible formatting characters are removed outright, and the DOI is
    truncated at the first non-ASCII character. Both exist because the
    alternative is the worst error this tool can make: telling an author their
    perfectly correct DOI is a typo. ``_DOI_RE`` stops only at whitespace, so
    anything glued to the DOI comes along with it, and the resulting ``✗ DOI
    does not resolve anywhere — check for a typo: 10.5665/sleep.1872。`` is
    indistinguishable on screen from the correct DOI. Real cases, all produced
    by ordinary authoring tools:

    * ``10.1000/x\\u200b`` — zero-width space, from copying out of a web page
    * ``10.1000/x\\u00ad`` — soft hyphen, inserted by Word
    * ``10.1000/x\\ufeff`` — an inline BOM
    * ``10.5665/sleep.1872。`` / ``10.1000/x（2021）`` — CJK full stop and
      full-width parentheses, from a Korean or Japanese manuscript
    * ``10.1000/x입니다`` — Korean text abutting the DOI with no space

    Truncating at the *first* non-ASCII character rather than only stripping
    trailing ones is deliberate: ``10.1000/x（2021）`` needs the full-width
    opening paren dropped too, and stripping from the right alone leaves
    ``10.1000/x（2021``. The DOI spec does permit non-ASCII in a suffix, so this
    could in principle truncate a real DOI — but such a DOI does not exist in
    Crossref in practice, and it was already unusable here (whatever followed it
    was glued on anyway), so this trades nothing away for a common real fix.
    """
    if not doi:
        return None
    doi = _DOI_PREFIX_RE.sub("", doi.strip()).strip()
    # Drop a URL query string / fragment (e.g. "?utm_source=…" from a
    # browser-copied link); real DOIs do not contain '?' or '#'.
    doi = re.split(r"[?#]", doi, maxsplit=1)[0]
    # Unicode format characters (category Cf: ZWSP, soft hyphen, BOM, bidi
    # marks) are invisible by definition, so a DOI carrying one looks correct
    # everywhere the author can inspect it. No DOI legitimately contains one.
    doi = _INVISIBLE_RE.sub("", doi)
    doi = _NON_ASCII_RE.split(doi, maxsplit=1)[0]
    doi = doi.lower()
    while doi and doi[-1] in _DOI_TRAIL:
        if doi[-1] == ")" and doi.count("(") >= doi.count(")"):
            break
        if doi[-1] == "]" and doi.count("[") >= doi.count("]"):
            break
        doi = doi[:-1]
    return doi or None


def find_doi(text: str) -> Optional[str]:
    """Return the first DOI found in *text*, normalised, or None.

    Handles DOIs embedded in URLs or ``doi:`` labels by matching the ``10.…``
    core directly. Uses a >=4-digit registrant lower bound so clinical dosing
    text like "10.55/kg" is not misread as a DOI.

    Invisible characters are removed *before* matching, not just afterwards in
    ``_clean_doi``. A soft hyphen or zero-width space landing inside the
    registrant code (``10.10<ZWSP>00/x`` — Word will insert one at a line break)
    breaks the ``10\\.\\d{4,}/`` match outright, so the DOI is not found at all
    and the reference silently reports ``no-doi`` instead of being verified.
    """
    m = _DOI_RE.search(_INVISIBLE_RE.sub("", text))
    return _clean_doi(m.group(0)) if m else None


# For an *explicit* ``doi={...}`` field there is no free-text ambiguity, so the
# registrant code may have any number of digits.
_DOI_FIELD_RE = re.compile(r"10\.\d+/[^\s<>\"']+", re.IGNORECASE)


def normalize_doi_field(value: str) -> Optional[str]:
    """Normalise the value of an explicit ``doi`` field (BibTeX/CSL).

    Strips a URL/``doi:`` prefix and trailing junk. More lenient than
    ``find_doi`` because the field is known to hold a DOI.
    """
    if not value:
        return None
    # Strip invisibles before matching, for the same reason as `find_doi`.
    value = _INVISIBLE_RE.sub("", value)
    stripped = _DOI_PREFIX_RE.sub("", value.strip()).strip()
    m = _DOI_FIELD_RE.search(stripped)
    if m:
        return _clean_doi(m.group(0))
    # No DOI core present — not a usable DOI.
    return None


def find_year(text: str) -> Optional[int]:
    m = _YEAR_RE.search(text)
    return int(m.group(0)) if m else None


def find_pmid(text: str) -> Optional[str]:
    """Return the first explicitly-labelled PubMed ID in *text*, or None.

    Only matches when preceded by a ``PMID`` label or a PubMed URL, so a page
    number or sample size is never mistaken for a PMID. Leading zeros are
    stripped (``PMID: 0123`` → ``123``) so the same paper matches regardless of
    zero-padding.
    """
    m = _PMID_RE.search(text)
    if not m:
        return None
    return _clean_pmid(m.group(1))


def _clean_pmid(digits: str) -> Optional[str]:
    """Normalise a digit string to a canonical PMID, or None if not valid.

    Strips leading zeros; rejects a zero/empty value (0 is not a real PMID).
    """
    digits = re.sub(r"\D", "", digits or "")
    if not digits:
        return None
    value = int(digits)
    return str(value) if value > 0 else None


# --- BibTeX -----------------------------------------------------------------

# Entry header: ``@type{key,`` or ``@type(key,``. BibTeX accepts either
# delimiter (bibtex, biblatex and JabRef all do), and an export using parentheses
# used to be dropped silently — not even counted as malformed, so the user got no
# signal at all. The key is bounded to exclude commas, both bracket kinds, and
# whitespace so it can never run past its own entry.
#
# The key group is written so that no two quantifiers can share one run of
# whitespace. The earlier form, \s*([^,{}()\s]*)\s*, backtracked quadratically
# when a header was followed by a long whitespace run without a comma: 100,000
# spaces after "@a{" took about a minute. It matches the same headers.
_ENTRY_RE = re.compile(r"@(\w+)\s*([{(])\s*((?:[^,{}()\s]+\s*)?),", re.IGNORECASE)
# BibTeX "entry" types that are not references and must be skipped.
_NON_REFERENCE_TYPES = {"string", "comment", "preamble"}

# A line that opens an entry (the "@" is group 1): a reference header with its
# key and comma, as in _ENTRY_RE, or an @string, @comment or @preamble. After
# an unterminated entry, scanning resumes at the next such line, and the closing
# ")" of a parenthesised entry is only looked for before it. The bound keeps the
# work linear when a file holds many unterminated entries. Requiring the key and
# comma keeps a field line such as "@WHO (World Health Organization) said" from
# cutting a valid parenthesised entry short.
_LINE_HEADER_RE = re.compile(
    r"^[ \t]*(@)(?:(?:string|comment|preamble)\s*[{(]"
    r"|\w+\s*[{(]\s*(?:[^,{}()\s]+\s*)?,)",
    re.MULTILINE | re.IGNORECASE,
)
_BRACE_RE = re.compile(r"[{}]")


def _unify_newlines(text: str) -> str:
    """CRLF and bare CR (classic Mac) become LF, so line anchors work."""
    if "\r" in text:
        text = text.replace("\r\n", "\n").replace("\r", "\n")
    return text


def _brace_pairs(text: str) -> dict:
    """Map the index of every "{" that closes to the index of its "}".

    One pass with a stack. A "{" missing from the map never closes, which is
    what makes an unterminated entry an O(1) lookup instead of a scan to the end
    of the file for every bad entry. A stray "}" with nothing open is ignored.
    """
    pairs: dict = {}
    stack: list = []
    for m in _BRACE_RE.finditer(text):
        if m.group() == "{":
            stack.append(m.start())
        elif stack:
            pairs[stack.pop()] = m.start()
    return pairs


def _line_header_starts(text: str) -> list:
    """Sorted positions of the "@" of every entry header that starts a line."""
    return [m.start(1) for m in _LINE_HEADER_RE.finditer(text)]


def _next_line_header(line_heads: list, after: int) -> Optional[int]:
    """The first line-start header position strictly after `after`, or None."""
    k = bisect.bisect_right(line_heads, after)
    return line_heads[k] if k < len(line_heads) else None


def _find_paren_entry_end(text: str, start: int, limit: Optional[int] = None) -> Optional[int]:
    """Index of the ")" closing the parenthesised entry opened at `start`, or None.

    Parentheses are only counted at brace depth 0, otherwise an ordinary
    ``title={Aspirin (low dose)}`` would close the entry early and truncate it.
    The search stops before `limit` (default: the end of `text`). Brace entries
    do not come here; they are matched through _brace_pairs.
    """
    depth = 0
    brace_depth = 0
    stop = len(text) if limit is None else min(limit, len(text))
    for j in range(start, stop):
        c = text[j]
        if c == "{":
            brace_depth += 1
        elif c == "}":
            brace_depth = max(0, brace_depth - 1)
        elif brace_depth == 0:
            if c == "(":
                depth += 1
            elif c == ")":
                depth -= 1
                if depth == 0:
                    return j
    return None


def _scan_entries(text: str) -> list[tuple[str, str, Optional[str]]]:
    """Scan top-level ``@type{key, …}`` entries in a single left-to-right pass.

    Returns (entry_type, key, body) tuples; ``body`` is None when the entry's
    delimiters never balanced (unterminated/malformed). A ``@type{...}``-looking
    string inside a field value (e.g. a title that discusses BibTeX, or a URL)
    is consumed as part of the enclosing entry's body via brace matching, never
    mistaken for a new top-level entry.

    After an unterminated entry the scan resumes at the next line that opens an
    entry, so one missing brace costs that entry only. It used to stop there,
    and every later entry was silently left unchecked. The work stays linear:
    brace entries are matched through one precomputed table (_brace_pairs) and
    a parenthesised entry is never searched past the next entry line.
    """
    text = _unify_newlines(text)
    results: list[tuple[str, str, Optional[str]]] = []
    pairs: Optional[dict] = None
    line_heads: Optional[list] = None
    i, n = 0, len(text)
    while i < n:
        m = _ENTRY_RE.search(text, i)
        if not m:
            break
        entry_type, opener, key = m.group(1), m.group(2), m.group(3).strip()
        start = m.start(2)
        if opener == "{":
            if pairs is None:
                pairs = _brace_pairs(text)
            end = pairs.get(start)
        else:
            if line_heads is None:
                line_heads = _line_header_starts(text)
            end = _find_paren_entry_end(text, start, _next_line_header(line_heads, m.start()))
        if end is None:
            results.append((entry_type, key, None))
            if line_heads is None:
                line_heads = _line_header_starts(text)
            resume = _next_line_header(line_heads, m.start())
            if resume is None:
                break
            i = resume
            continue
        results.append((entry_type, key, text[start + 1 : end]))
        i = end + 1
    return results


def _split_bibtex_entries(text: str) -> list[tuple[str, str, str]]:
    """Return (entry_type, key, body) for each well-formed reference entry."""
    return [
        (t, k, body)
        for (t, k, body) in _scan_entries(text)
        if body is not None and t.lower() not in _NON_REFERENCE_TYPES
    ]


def malformed_entry_keys(text: str) -> list[str]:
    """Cite keys of reference entries whose delimiters never balanced.

    These entries were skipped, so nothing in them was checked. The CLI names
    them and exits 3 (inconclusive) rather than report a clean pass.
    """
    return [
        k
        for (t, k, body) in _scan_entries(text)
        if body is None and t.lower() not in _NON_REFERENCE_TYPES
    ]


def count_malformed_entries(text: str) -> int:
    """Number of reference entries whose braces never balanced (skipped)."""
    return len(malformed_entry_keys(text))


# --- @string macros ----------------------------------------------------------
#
# JabRef and BibDesk write journal names as @string macros: `journal = jsr`
# with `@string{jsr = {Journal of Sleep Research}}` at the top of the file.
# Without expansion the journal check compared the literal "jsr" with Crossref
# and warned on a correct citation. A macro with no definition in the file
# expands to nothing, as in BibTeX itself, so that field is simply not compared.

_STRING_HEADER_RE = re.compile(r"@string\s*([{(])", re.IGNORECASE)
# A macro name is a word, without ":" or "/": a bare `doi = doi:10.1000/x` or
# `url = https://...` is text, not an undefined macro, and keeps its value.
_STRING_DEF_RE = re.compile(r"\s*([^\W\d][\w\-.+]*)\s*=\s*")
_MACRO_RE = re.compile(r"[^\W\d][\w\-.+]*")
_NUMBER_RE = re.compile(r"\d+")

# The longest value a macro may expand into. A macro built from earlier macros
# can double at each step (`@string{b = a # a}`), so 30 short lines would ask
# for gigabytes, the BibTeX form of an XML entity bomb. No real title, author
# list or journal name stored in a macro comes near this. A longer expansion is
# treated like an undefined macro: the field is left empty and not compared.
_MAX_MACRO_CHARS = 10_000


def _read_value(body: str, j: int, macros: dict):
    """Read a BibTeX value at body[j]: one or more parts joined by "#".

    A part is a {braced} or "quoted" string, a number, or a macro name.
    Returns (text, end, defined): `defined` is False when a macro name has no
    definition, or when expanding it would make the value longer than
    _MAX_MACRO_CHARS. Returns None when no well-formed value starts at `j` (an
    unbalanced brace or quote, or prose that is not a value).
    """
    n = len(body)
    parts: list[str] = []
    length = 0
    defined = True
    while True:
        while j < n and body[j].isspace():
            j += 1
        if j >= n:
            return None
        c = body[j]
        if c == "{":
            depth = 0
            for k in range(j, n):
                if body[k] == "{":
                    depth += 1
                elif body[k] == "}":
                    depth -= 1
                    if depth == 0:
                        break
            else:
                return None
            part: Optional[str] = body[j + 1 : k]
            j = k + 1
        elif c == '"':
            k = body.find('"', j + 1)
            if k == -1:
                return None
            part = body[j + 1 : k]
            j = k + 1
        else:
            m = _NUMBER_RE.match(body, j) or _MACRO_RE.match(body, j)
            if not m:
                return None
            token = m.group()
            if token[0].isdigit():
                part = token
            else:
                part = macros.get(token.lower())
                if part is None or length + len(part) > _MAX_MACRO_CHARS:
                    part = None
                    defined = False
            j = m.end()
        if part is not None:
            parts.append(part)
            length += len(part)
        k = j
        while k < n and body[k].isspace():
            k += 1
        if k < n and body[k] == "#":
            j = k + 1
            continue
        return "".join(parts), j, defined


def _string_macros(text: str) -> dict:
    """Collect the @string definitions of a BibTeX file, in file order.

    Names are case-insensitive. A definition that uses an undefined macro, or
    that would expand past _MAX_MACRO_CHARS, is left out rather than stored as
    partial text, so a chain of doubling macros stops at its first oversized
    link. Unterminated definitions are skipped in O(1) each through the same
    brace table as entries.
    """
    macros: dict = {}
    if "@" not in text:
        return macros
    text = _unify_newlines(text)
    pairs: Optional[dict] = None
    line_heads: Optional[list] = None
    i = 0
    while True:
        m = _STRING_HEADER_RE.search(text, i)
        if not m:
            break
        start = m.start(1)
        if m.group(1) == "{":
            if pairs is None:
                pairs = _brace_pairs(text)
            end = pairs.get(start)
        else:
            if line_heads is None:
                line_heads = _line_header_starts(text)
            end = _find_paren_entry_end(text, start, _next_line_header(line_heads, m.start()))
        if end is None:
            i = m.end()
            continue
        body = text[start + 1 : end]
        d = _STRING_DEF_RE.match(body)
        if d:
            parsed = _read_value(body, d.end(), macros)
            if parsed is not None:
                value, stop, defined = parsed
                if defined and not body[stop:].strip():
                    macros[d.group(1).lower()] = value
        i = end + 1
    return macros


_FIELD_RE = re.compile(r"(\w+)\s*=\s*", re.IGNORECASE)


def _parse_bibtex_fields(body: str, macros: Optional[dict] = None) -> dict:
    """Parse `field = {value}` / `field = "value"` / `field = value` pairs.

    Values may be "#" concatenations and may use @string macros (`macros`,
    from _string_macros). An undefined macro gives an empty value.
    """
    macros = macros or {}
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
        if body[j] in '{"':
            parsed = _read_value(body, j, macros)
            if parsed is None:
                break  # unbalanced brace or quote: the rest cannot be delimited
            value, i, defined = parsed
            fields[name] = value if defined else ""
            continue
        # A bare value: a number, a macro name, or a "#" concatenation.
        parsed = _read_value(body, j, macros)
        if parsed is not None:
            value, stop, defined = parsed
            k = stop
            while k < n and body[k].isspace():
                k += 1
            if k >= n or body[k] == ",":
                fields[name] = value if defined else ""
                i = k + 1
                continue
        # Anything else (a bare DOI, unbraced prose) is read up to the next
        # comma, as before.
        end = body.find(",", j)
        if end == -1:
            end = n
        fields[name] = body[j:end].strip()
        i = end + 1
    return fields


# --- LaTeX accents ------------------------------------------------------------
#
# A .bib file spells Öztürk as {\"O}zt{\"u}rk. Stripping only the braces left
# \"Ozt\"urk, which then failed the author comparison against Crossref's
# "Öztürk". The common accent commands and special letters are decoded; any
# other command (\url, \cite, \verb, \emph) is left as it was.

_LATEX_ACCENTS = {
    '"': "\u0308", "'": "\u0301", "`": "\u0300", "^": "\u0302", "~": "\u0303",
    "=": "\u0304", ".": "\u0307", "c": "\u0327", "v": "\u030c", "H": "\u030b",
    "u": "\u0306",
}
_LATEX_LETTERS = {
    "ss": "ß", "o": "ø", "O": "Ø", "aa": "å", "AA": "Å", "ae": "æ", "AE": "Æ",
    "oe": "œ", "OE": "Œ", "l": "ł", "L": "Ł",
}
# The accented letter: a plain letter, or \i / \j (dotless i and j, which take
# the accent as plain i and j).
_LATEX_BASE = r"(?:[A-Za-z]|\\[ij](?![A-Za-z]))"
# \"O  \"{O}  \" O  (symbol accents take the next letter, braced or not)
_LATEX_SYMBOL_ACCENT_RE = re.compile(
    r"\\([\"'`^~=.])[ \t]*(?:\{[ \t]*(" + _LATEX_BASE + r")[ \t]*\}|(" + _LATEX_BASE + r"))"
)
# \c{c}  \c c  (letter accents need a brace or a space, so \cite stays \cite)
_LATEX_LETTER_ACCENT_RE = re.compile(
    r"\\([cvHu])(?:[ \t]*\{[ \t]*(" + _LATEX_BASE + r")[ \t]*\}|[ \t]+([A-Za-z]))"
)
_LATEX_SPECIAL_RE = re.compile(r"\\(ss|aa|AA|ae|AE|oe|OE|o|O|l|L)(?![A-Za-z])(?:\{\}|[ \t])?")
_LATEX_ESCAPED_RE = re.compile(r"\\([&%$#_])")


def _latex_accent(m: re.Match) -> str:
    base = m.group(2) or m.group(3)
    if base.startswith("\\"):
        base = base[1]  # \i, \j
    return base + _LATEX_ACCENTS[m.group(1)]


def _decode_latex(value: str) -> str:
    """Decode common LaTeX accent commands, special letters and \\& to Unicode."""
    if "\\" not in value:
        return value
    value = _LATEX_SYMBOL_ACCENT_RE.sub(_latex_accent, value)
    value = _LATEX_LETTER_ACCENT_RE.sub(_latex_accent, value)
    value = _LATEX_SPECIAL_RE.sub(lambda m: _LATEX_LETTERS[m.group(1)], value)
    return _LATEX_ESCAPED_RE.sub(r"\1", value)


def _clean_bibtex_value(value: str) -> str:
    value = value.replace("\n", " ")
    value = _decode_latex(value)
    value = re.sub(r"[{}]", "", value)
    value = re.sub(r"\s+", " ", value).strip()
    return unicodedata.normalize("NFC", value)


# --- Hangul -------------------------------------------------------------------

# Hangul syllables, conjoining jamo (what NFD text holds) and compatibility jamo.
_HANGUL_RE = re.compile(r"[\uac00-\ud7a3\u1100-\u11ff\u3130-\u318f]")


def _hangul_count(text: str) -> int:
    return len(_HANGUL_RE.findall(text or ""))


def _has_hangul(text: str) -> bool:
    return bool(_HANGUL_RE.search(text or ""))


def _mostly_hangul(text: str) -> bool:
    """True when at least half of the letters in `text` are Hangul."""
    letters = sum(1 for ch in (text or "") if ch.isalpha())
    return letters > 0 and _hangul_count(text) * 2 >= letters


# Korean surnames of two syllables. "남궁 민수" is one person; "김구 홍길동" is two.
_KO_TWO_SYLLABLE_SURNAMES = {
    "남궁", "황보", "제갈", "선우", "독고", "사공", "서문", "동방", "어금", "망절",
}


def _korean_first_name(name: str) -> Optional[str]:
    """The first author of a Hangul author string with no comma, spaces removed.

    Korean names are written surname first ("홍길동", "홍 길동"), so the
    "Given Surname" rule below would return the given name. A space after a
    one-syllable or two-syllable surname joins it to the given name; a first
    token of three or more syllables is a whole name followed by co-authors.
    """
    tokens = name.split()
    if not tokens:
        return None
    first = tokens[0]
    if len(tokens) > 1 and (len(first) == 1 or first in _KO_TWO_SYLLABLE_SURNAMES):
        return first + tokens[1]
    return first


# --- first author ---------------------------------------------------------------

# An initials token: 1-3 capitals, optionally dotted — "H", "J.", "P.A.", "JP".
_INITIALS_RE = re.compile(r"^[A-Z](?:\.?[A-Z]){0,2}\.?$")

# "et al" at the end of an author cell ("Smith et al.", "Smith J et al").
_EN_ET_AL_RE = re.compile(r"\bet\.?\s*al\b\.?\s*$", re.IGNORECASE)
# Korean "and others": "홍길동 외", "홍길동 외 2인", "홍길동 등 3명", "홍길동외 2인".
_KO_MARKERS = ("외", "등")
_KO_COUNT_RE = re.compile(r"\d+(?:인|명)")
_KO_GLUED_RE = re.compile(r"([가-힣]{2,})(?:외|등)")
_KO_GLUED_COUNT_RE = re.compile(r"([가-힣]{2,})(?:외|등)\d+(?:인|명)")


def _strip_et_al(name: str) -> str:
    """Drop a trailing "et al" or Korean "외"/"등" marker from an author string.

    Without this the marker itself was taken as the surname ("홍길동 외 2인"
    became "2인", "Smith et al." became "al.") and every such row warned
    author-mismatch. A marker glued to the name with no count ("김등") is
    ambiguous and left alone.
    """
    name = _EN_ET_AL_RE.sub("", name).rstrip(" ,;")
    tokens = name.split()
    if len(tokens) >= 2 and _KO_COUNT_RE.fullmatch(tokens[-1]):
        if tokens[-2] in _KO_MARKERS and len(tokens) >= 3:
            tokens = tokens[:-2]
        else:
            glued = _KO_GLUED_RE.fullmatch(tokens[-2])
            if glued:
                tokens = tokens[:-2] + [glued.group(1)]
    elif len(tokens) >= 2 and tokens[-1] in _KO_MARKERS:
        tokens = tokens[:-1]
    elif len(tokens) == 1:
        glued = _KO_GLUED_COUNT_RE.fullmatch(tokens[0])
        if glued:
            tokens = [glued.group(1)]
    return " ".join(tokens)


def _strip_trailing_initials(tokens: list[str]) -> tuple[list[str], bool]:
    """Drop trailing initials tokens from a name, e.g. ["Kim", "H."] -> ["Kim"].

    Returns (remaining, stripped_any). Never strips every token — a name that is
    *only* initials keeps them, since something must be returned.
    """
    out = list(tokens)
    stripped = False
    while len(out) > 1 and _INITIALS_RE.match(out[-1]):
        out.pop()
        stripped = True
    return out, stripped


_AND_RE = re.compile(r"\band\b")


def _split_first_author(author_field: str) -> str:
    """The text before the first " and " that is not inside braces.

    "{Johnson and Johnson} and Smith, J." is two authors, not three.
    """
    depth = 0
    pos = 0
    for m in _AND_RE.finditer(author_field):
        for ch in author_field[pos : m.start()]:
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth = max(0, depth - 1)
        pos = m.start()
        if depth == 0:
            return author_field[: m.start()]
    return author_field


def _is_brace_protected(name: str) -> bool:
    """True when the whole name is one brace group, as in {World Health Organization}."""
    name = name.strip()
    if len(name) < 2 or name[0] != "{" or name[-1] != "}":
        return False
    depth = 0
    for i, ch in enumerate(name):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return i == len(name) - 1
    return False


# Words that do not occur in a given name but do in an organisation name.
_ORG_FUNCTION_WORDS = {"of", "and", "for", "the", "on", "in", "at", "to"}


def _protected_author(name: str) -> str:
    """The author to compare for a brace-protected name.

    BibTeX protects a corporate author with braces so it is not split into
    given and family names: {The Editors of The Lancet} is one name, not the
    surname "Lancet". It is kept whole. A protected personal name in
    "Surname, Given" form ({Hong, Gildong}) still gives its surname, but only
    when the part after the comma reads like a given name, so that
    {Ministry of Health, Labour and Welfare} is not cut at its comma.
    """
    if "," in name:
        surname, given = (part.strip() for part in name.split(",", 1))
        words = given.split()
        if surname and len(words) <= 3 and not any(
            w in _ORG_FUNCTION_WORDS for w in words
        ):
            return surname
    return name


def _first_author_surname(author_field: str) -> Optional[str]:
    """Best-effort first-author surname from an `author` field.

    Handles the two conventions that collide in real reference data:

    * **"Given Surname"** (BibTeX's default, e.g. "William Strunk") — the
      surname is the *last* token.
    * **"Surname Initials"** (PubMed/Vancouver style, e.g. "Kim H", "Smith JP")
      — the surname is the *first* token and the trailing initials must be
      dropped, or the "surname" comes back as "H" and every author comparison
      falsely mismatches. This dominates spreadsheet and RIS reference tables.

    The two are told apart by whether a trailing initials token exists at all,
    which also disambiguates the comma: in "Hong, Gildong J." the comma separates
    surname from given names, while in "Kim H, Lee S" it separates *authors*.
    Multi-word surnames ("van der Berg H") survive intact.

    A brace-protected name is kept whole (see _protected_author), a trailing
    "et al" or Korean "외"/"등" marker is dropped, and a Hangul name is read
    surname first with its spaces removed ("홍 길동" gives "홍길동").
    """
    if not author_field:
        return None
    first = _split_first_author(author_field).strip()
    protected = _is_brace_protected(first)
    first = _clean_bibtex_value(first)
    if not first:
        return None
    if protected:
        return _protected_author(first)
    # A semicolon only ever separates authors ("Smith J; Lee K", as Scopus and
    # Web of Science export them). It used to be read as part of one long name.
    first = first.split(";")[0].strip()
    first = _strip_et_al(first)
    if "," in first:  # "Surname, Given" — or an author list "Kim H, Lee S"
        first = first.split(",")[0].strip()
    if _mostly_hangul(first):
        return _korean_first_name(first)
    parts = first.split()
    if not parts:
        return None
    remaining, stripped = _strip_trailing_initials(parts)
    if stripped:  # "Surname Initials" — the surname is what's left, in order
        return " ".join(remaining)
    return parts[-1]  # "Given Surname"


def parse_bibtex(text: str) -> list[Reference]:
    macros = _string_macros(text)
    refs = []
    for entry_type, key, body in _split_bibtex_entries(text):
        fields = _parse_bibtex_fields(body, macros)
        title = _clean_bibtex_value(fields.get("title", "")) or None
        author = _first_author_surname(fields.get("author", ""))
        # Year: `year` (BibTeX) or the year inside `date` (biblatex, e.g.
        # "2020-03-14"). Better BibTeX / biblatex exports use `date`, so reading
        # only `year` would silently skip the year check for those files.
        year = None
        for key_name in ("year", "date"):
            if fields.get(key_name):
                year = find_year(fields[key_name])
                if year:
                    break
        # The `doi` field may be a bare DOI, a full URL, or a `doi:`-prefixed
        # string — extract the DOI core in every case.
        doi = normalize_doi_field(fields["doi"]) if fields.get("doi") else None
        # Journal / container: `journal` (BibTeX), `journaltitle` (biblatex), or
        # `booktitle` for chapters.
        journal = (
            _clean_bibtex_value(
                fields.get("journal")
                or fields.get("journaltitle")
                or fields.get("booktitle")
                or ""
            )
            or None
        )
        pmid = _bibtex_pmid(fields)
        refs.append(
            Reference(
                raw=body.strip(),
                doi=doi,
                title=title,
                author=author,
                year=year,
                journal=journal,
                pmid=pmid,
                key=key or None,
                fields={"type": entry_type, **fields},
            )
        )
    return refs


def _bibtex_pmid(fields: dict) -> Optional[str]:
    """Extract a PMID from a BibTeX entry (explicit field or note/eprint)."""
    for name in ("pmid", "pubmedid", "eprint"):
        val = fields.get(name)
        if val:
            # `eprint` is only a PMID when eprinttype says so; otherwise skip it.
            if name == "eprint" and "pubmed" not in str(fields.get("eprinttype", "")).lower():
                continue
            cleaned = _clean_pmid(val)
            if cleaned:
                return cleaned
    for name in ("note", "annote", "url", "howpublished"):
        val = fields.get(name)
        if val:
            found = find_pmid(val)
            if found:
                return found
    return None


# --- Plain text / DOI lists -------------------------------------------------


def parse_text(text: str) -> list[Reference]:
    """Parse a newline- or blank-line-separated list of references.

    Each non-empty line (or paragraph) becomes one reference. A bare DOI line
    is treated as a DOI-only reference.
    """
    # Split on blank lines if any are present (allowing whitespace-only blank
    # lines); otherwise split on single newlines. The guard and the split use
    # the same pattern so they can never disagree.
    if re.search(r"\n\s*\n", text):
        blocks = re.split(r"\n\s*\n", text.strip())
    else:
        blocks = text.strip().splitlines()
    refs = []
    for block in blocks:
        block = block.strip()
        if not block:
            continue
        doi = find_doi(block)
        year = find_year(block)
        author = _guess_text_author(block)
        pmid = find_pmid(block)
        refs.append(
            Reference(
                raw=block, doi=doi, year=year, author=author, pmid=pmid,
                heuristic_fields=True,
            )
        )
    return refs


def _guess_text_author(block: str) -> Optional[str]:
    """Grab a leading surname from a reference string like 'Kim H, Lee S. ...'.

    Callers always pass a ``.strip()``-ed block, so there is no leading
    whitespace to consume — the pattern deliberately omits a leading ``\\s*`` that
    would otherwise create two whitespace-matching groups straddling the same run
    (quadratic backtracking on a pathological all-space input).
    """
    m = re.match(r"\[?\d*\]?\.?\s*([A-Z][A-Za-z'\-]+)", block.lstrip())
    return m.group(1) if m else None


# --- RIS (EndNote / Zotero / Mendeley export) -------------------------------

# A RIS tag line: two-to-four uppercase letters/digits, spaces, a hyphen, then
# the value. The canonical form is ``XX  - value`` (two spaces); we accept one
# or more spaces on each side of the hyphen for the sloppier exports in the wild.
_RIS_TAG_RE = re.compile(r"^([A-Z][A-Z0-9]{1,3})\s{1,}-\s?(.*)$")
# Tags that can carry the title, in preference order.
_RIS_TITLE_TAGS = ("TI", "T1", "BT", "CT")
# Tags that can carry the journal / container name, in preference order.
_RIS_JOURNAL_TAGS = ("JF", "JO", "JA", "T2", "J1", "J2")


def looks_like_ris(text: str) -> bool:
    """True if *text* looks like RIS.

    Requires a ``TY  - `` record header AND enough other RIS tag lines (or an
    ``ER`` terminator) that a lone ``TY  - …`` line inside plain-text prose can't
    misroute a whole reference list into a single RIS record (silent data loss).

    It additionally requires the file to *begin* as RIS. ``_ris_records``
    discards everything before the first ``TY``, so without this a plain-text
    reference list that merely happens to contain an RIS-shaped block — a
    methods appendix, a pasted export fragment — routed here and every reference
    above that block vanished, at exit 0 with nothing on stderr. Real RIS
    exports always open with their first record's tag.
    """
    has_ty = has_er = False
    tag_lines = 0
    first_meaningful_is_a_tag = False
    seen_content = False
    for line in text.splitlines():
        m = _RIS_TAG_RE.match(line)
        if not seen_content and line.strip():
            seen_content = True
            first_meaningful_is_a_tag = m is not None
        if not m:
            continue
        tag_lines += 1
        if m.group(1) == "TY":
            has_ty = True
        elif m.group(1) == "ER":
            has_er = True
    return first_meaningful_is_a_tag and has_ty and (has_er or tag_lines >= 3)


def _ris_records(text: str) -> list[list[tuple[str, str]]]:
    """Split RIS *text* into records, each a list of (tag, value) pairs.

    A record runs from a ``TY`` tag to its ``ER`` tag. Continuation lines (no
    tag) are appended to the previous field's value so a wrapped title survives.
    Content before the first ``TY`` is ignored.
    """
    records: list[list[tuple[str, str]]] = []
    current: Optional[list[list]] = None
    for raw_line in text.splitlines():
        line = raw_line.rstrip("\r")
        m = _RIS_TAG_RE.match(line)
        if m:
            tag, value = m.group(1), m.group(2).strip()
            if tag == "TY":
                if current is not None:
                    records.append([(t, v) for t, v in current])
                current = [["TY", value]]
            elif tag == "ER":
                if current is not None:
                    records.append([(t, v) for t, v in current])
                    current = None
            elif current is not None:
                current.append([tag, value])
        elif current is not None and current and line.strip():
            # Continuation of the previous field's value.
            current[-1][1] = (current[-1][1] + " " + line.strip()).strip()
    if current is not None:  # a final record with no explicit ER
        records.append([(t, v) for t, v in current])
    return records


def _ris_first(joined: dict, tags: tuple) -> Optional[str]:
    for tag in tags:
        if joined.get(tag):
            return joined[tag]
    return None


def parse_ris(text: str) -> list[Reference]:
    """Parse an RIS reference list (EndNote / Zotero / Mendeley export)."""
    refs: list[Reference] = []
    for record in _ris_records(text):
        # First value per tag (title/journal/year), plus all authors.
        first: dict = {}
        authors: list[str] = []
        for tag, value in record:
            if not value:
                continue
            if tag in ("AU", "A1"):
                authors.append(value)
            first.setdefault(tag, value)
        raw = "\n".join(f"{t}  - {v}" for t, v in record)

        # DOI: prefer the DO/DOI tag, else scan the whole record.
        doi = None
        for tag in ("DO", "DOI"):
            if first.get(tag):
                doi = normalize_doi_field(first[tag]) or find_doi(first[tag])
                if doi:
                    break
        if not doi:
            doi = find_doi(raw)

        title = None
        for tag in _RIS_TITLE_TAGS:
            if first.get(tag):
                title = _clean_bibtex_value(first[tag])
                break
        journal = _ris_first(first, _RIS_JOURNAL_TAGS)
        journal = _clean_bibtex_value(journal) if journal else None
        year = None
        for tag in ("PY", "Y1", "DA"):
            if first.get(tag):
                year = find_year(first[tag])
                if year:
                    break
        author = _first_author_surname(authors[0]) if authors else None
        pmid = _ris_pmid(first, raw)
        refs.append(
            Reference(
                raw=raw.strip(),
                doi=doi,
                title=title or None,
                author=author,
                year=year,
                journal=journal,
                pmid=pmid,
                fields={"type": "ris:" + (first.get("TY", "") or "?")},
            )
        )
    return refs


def _ris_pmid(first: dict, raw: str) -> Optional[str]:
    """Extract a PMID from an RIS record.

    ``AN`` (accession) is only trusted as a PMID when the data-provider tags
    (``DB``/``DP``) mention PubMed/MEDLINE; otherwise a bare ``AN`` could be any
    database's accession. Also scans free-text notes for a labelled PMID.
    """
    provider = " ".join(
        str(first.get(t, "")) for t in ("DB", "DP", "DptDp", "T3")
    ).lower()
    if first.get("AN") and ("pubmed" in provider or "medline" in provider):
        cleaned = _clean_pmid(first["AN"])
        if cleaned:
            return cleaned
    return find_pmid(raw)


# --- CSV / TSV (Excel / Sheets reference tables) -----------------------------

# Header aliases, normalised (lowercased, non-alphanumerics dropped). Clinical
# reference tables come out of Excel, Covidence, Rayyan, EndNote's "export to
# tab-delimited", and hand-typed sheets — the same column means a dozen things.
_CSV_ALIASES: dict[str, tuple[str, ...]] = {
    "doi": (
        "doi", "dois", "doi10", "articledoi", "paperdoi", "doilink", "doiurl",
        "doinumber", "digitalobjectidentifier", "doihttps",
    ),
    "pmid": ("pmid", "pmids", "pubmedid", "pubmed", "pubmedidentifier", "pmidnumber"),
    "title": (
        "title", "titles", "articletitle", "papertitle", "studytitle",
        "primarytitle", "publicationtitle", "titleofarticle",
        "제목", "논문제목", "논문명", "표제",
    ),
    "author": (
        "author", "authors", "firstauthor", "authorlist", "authorship",
        "firstauthorsurname", "leadauthor", "authorsyear",
        "저자", "저자명", "제1저자", "주저자", "제1저자명",
    ),
    "year": (
        "year", "years", "pubyear", "publicationyear", "publishedyear",
        "yearpublished", "date", "publicationdate", "publisheddate",
        "연도", "년도", "발행연도", "출판연도", "게재연도", "발행년도", "출판년도",
    ),
    "journal": (
        "journal", "journals", "journaltitle", "journalname", "source",
        "sourcetitle", "publication", "container", "containertitle",
        "journalabbreviation", "journalabbrev", "periodical",
        "학술지", "학술지명", "저널", "저널명", "게재지",
    ),
    "key": (
        "key", "id", "refid", "referenceid", "studyid", "citationkey",
        "citekey", "ref", "reference", "no", "num", "number", "studyname",
        "covidencenumber", "recordid",
        "번호", "연번", "연구id", "연구번호",
    ),
}

# Reverse index: normalised header -> canonical field.
_CSV_HEADER_MAP: dict[str, str] = {
    alias: field_name for field_name, aliases in _CSV_ALIASES.items() for alias in aliases
}

# A header row must name at least one of these for us to trust the file as a
# reference table (a "key" or "year" column alone is not evidence of one).
_CSV_REQUIRED_ANY = ("doi", "pmid", "title")

_CSV_DELIMITERS = (",", "\t", ";", "|")


def _normalize_header(name: str) -> str:
    """Fold a spreadsheet column name to its comparison key.

    Lowercases and drops everything but letters and digits, so "Article DOI",
    "article_doi", "DOI:" and "ARTICLE-DOI" all collapse to "articledoi".
    Hangul is kept (after NFC, so decomposed text folds the same way): every
    Korean header used to fold to "", so a table headed 제1저자, 발행연도,
    논문제목, 학술지 had only its DOI column read and a swapped DOI passed.
    """
    folded = unicodedata.normalize("NFC", (name or "").strip().lower())
    return re.sub(r"[^a-z0-9가-힣]+", "", folded)


def _header_field(name) -> Optional[str]:
    """Canonical field named by one header cell, or None.

    Tries these keys in order and takes the first that is a known alias: the
    full key (Hangul kept, so "논문 제목" is "논문제목"), the Latin-only key, the
    Hangul-and-digits key, then the Hangul-only key. Bilingual headers such as
    "Title(제목)", "DOI(링크)", "DOI 번호" or "저자 (Authors)" match by their
    Latin part, as they did before Hangul was kept; "제1저자 (First author)"
    matches by "제1저자" and "제목 1" by "제목".
    """
    if not isinstance(name, str):
        return None
    full = _normalize_header(name)
    if not full:
        return None
    found = _CSV_HEADER_MAP.get(full)
    if found:
        return found
    for pattern in (r"[^a-z0-9]+", r"[^0-9가-힣]+", r"[^가-힣]+"):
        key = re.sub(pattern, "", full)
        if key and key != full:
            found = _CSV_HEADER_MAP.get(key)
            if found:
                return found
    return None


def _sniff_delimiter(text: str) -> str:
    """Pick the delimiter of a CSV/TSV *text* from its header line.

    Chooses the candidate that both splits the header into the most fields and
    yields the most recognised column names — counting recognition first means a
    title cell full of commas in a tab-separated file cannot outvote the tabs.
    """
    header = ""
    for line in text.splitlines():
        if line.strip():
            header = line
            break
    best, best_score = ",", (-1, -1)
    for delim in _CSV_DELIMITERS:
        try:
            row = next(csv.reader([header], delimiter=delim))
        except (csv.Error, StopIteration):
            continue
        known = sum(1 for c in row if _header_field(c))
        score = (known, len(row))
        if score > best_score:
            best, best_score = delim, score
    return best


def _csv_header_fields(header: list) -> dict[str, int]:
    """Map canonical field -> column index for a header row (first wins)."""
    mapping: dict[str, int] = {}
    for idx, name in enumerate(header):
        canonical = _header_field(name)
        if canonical and canonical not in mapping:
            mapping[canonical] = idx
    return mapping


# A column *name* is a label, not a sentence. Real reference-table headers look
# like "Study ID", "Article DOI", "Journal Abbreviation" — never longer than a
# handful of words. Prose split on commas produces cells like
# "and the biomedical literature. PLoS One. 2013. doi:10.1371/journal.pone.0068397".
_CSV_MAX_HEADER_WORDS = 10
# How many data rows to sample when testing column-count consistency.
_CSV_SHAPE_SAMPLE = 20


def _looks_like_a_header_row(header: list) -> bool:
    """Do these cells read as column *names* rather than as prose?

    Two signals, both of which prose reference lines trip and real headers never
    do: a header never contains an actual DOI (it names a DOI column, it does not
    hold one), and a header cell is never a sentence.
    """
    for cell in header:
        if not isinstance(cell, str):
            continue
        if find_doi(cell):
            return False
        if len(cell.split()) > _CSV_MAX_HEADER_WORDS:
            return False
    return True


def _has_tabular_shape(rows: list, ncols: int) -> bool:
    """Do the data rows agree with the header on how many columns there are?

    A delimited table is *rectangular*; a reference list that merely contains
    commas is not — "Kim H, Lee S. Title. J Med. 2024;10:1-5." yields a different
    field count on every line. Ragged real-world exports (trailing empty columns)
    are tolerated by asking only for a majority, not unanimity.
    """
    data = [r for r in rows if any((c or "").strip() for c in r)][:_CSV_SHAPE_SAMPLE]
    if not data:
        return True  # header only — nothing to contradict it
    agreeing = sum(1 for r in data if len(r) == ncols)
    return agreeing * 2 >= len(data)


def looks_like_csv(text: str) -> bool:
    """True if *text* looks like a delimited reference table.

    Deliberately strict, because the failure is *silent*: a plain-text reference
    list misrouted here loses its first reference (consumed as a header row),
    loses author/year/journal, and — because the result is marked structured
    rather than heuristic — also loses the free-text swapped-DOI guard. It ends
    up checked *less* than before, at exit 0.

    So a table must clear four bars: >= 2 columns; at least one column naming a
    DOI/PMID/title; a header that reads as column names rather than prose
    (:func:`_looks_like_a_header_row`); and a rectangular shape
    (:func:`_has_tabular_shape`). A comma-rich sentence such as "Steen RG,
    Casadevall A. Retraction, PubMed, and the biomedical literature. PLoS One.
    2013. doi:10..." clears the first two — " PubMed" normalises to a known PMID
    column alias — and is caught by the last two.
    """
    stripped = text.lstrip()
    if not stripped or stripped[0] == "@":
        return False
    delim = _sniff_delimiter(text)
    try:
        rows = list(csv.reader(io.StringIO(text), delimiter=delim))
    except csv.Error:
        return False
    header_idx = next(
        (i for i, r in enumerate(rows) if any(c.strip() for c in r)), None
    )
    if header_idx is None:
        return False
    header = rows[header_idx]
    if len(header) < 2:
        return False
    fields = _csv_header_fields(header)
    if not any(f in fields for f in _CSV_REQUIRED_ANY):
        return False
    if not _looks_like_a_header_row(header):
        return False
    return _has_tabular_shape(rows[header_idx + 1 :], len(header))


# Fields that are compared with Crossref. A table that maps none of them is
# checked on its DOI/PMID alone, and the user should hear which columns were
# not recognised.
_CSV_COMPARED_FIELDS = ("title", "author", "year", "journal")


def csv_unmatched_columns(text: str) -> list[str]:
    """Header cells of a table that maps only DOI/PMID (and a key column).

    Returns [] when the table maps any compared field, or is not a table. The
    CLI prints these so a skipped comparison is visible instead of silent.
    """
    delim = _sniff_delimiter(text)
    try:
        header = next(
            (r for r in csv.reader(io.StringIO(text), delimiter=delim)
             if any(c.strip() for c in r)),
            None,
        )
    except csv.Error:
        return []
    if header is None:
        return []
    fields = _csv_header_fields(header)
    if not fields or any(f in fields for f in _CSV_COMPARED_FIELDS):
        return []
    used = set(fields.values())
    return [c.strip() for i, c in enumerate(header) if i not in used and c.strip()]


def _cell(row: list, fields: dict, name: str) -> Optional[str]:
    idx = fields.get(name)
    if idx is None or idx >= len(row):
        return None
    value = (row[idx] or "").strip()
    return value or None


def parse_csv(text: str) -> list[Reference]:
    """Parse a CSV/TSV reference table (Excel / Sheets / Covidence export).

    Columns are matched by name (see ``_CSV_ALIASES``) rather than position, so
    the researcher's own column order and capitalisation are irrelevant. A row
    with no recognised DOI column still has its whole text scanned for a DOI, so
    a table that keeps the DOI inside a "Notes" or "URL" column still verifies.

    Fields come from named columns, so they are treated as *structured* (not
    heuristic) — the full title/year/author/journal comparison applies.
    """
    delim = _sniff_delimiter(text)
    try:
        rows = list(csv.reader(io.StringIO(text), delimiter=delim))
    except csv.Error:
        return []
    header_idx = next(
        (i for i, r in enumerate(rows) if any(c.strip() for c in r)), None
    )
    if header_idx is None:
        return []
    fields = _csv_header_fields(rows[header_idx])
    if not any(f in fields for f in _CSV_REQUIRED_ANY):
        return []

    refs: list[Reference] = []
    for row in rows[header_idx + 1 :]:
        if not any((c or "").strip() for c in row):
            continue  # blank separator row
        # Join cells with a SPACE, not with the delimiter. The delimiter is a
        # field separator, never part of a field's value, and `_DOI_RE` stops
        # only at whitespace — so `delim.join` glued the next column straight
        # onto a DOI found in a Notes/URL column:
        #
        #   Title,DOI,Notes,Year
        #   Sleep and CBT,,see doi 10.1016/j.sleep.2021.01.001,2021
        #     -> DOI "10.1016/j.sleep.2021.01.001,2021"
        #     -> "✗ DOI does not resolve anywhere — check for a typo"
        #
        # i.e. it broke the "a table that keeps the DOI in a Notes or URL
        # column still verifies" promise in this function's own docstring, and
        # reported the author's correct DOI as their mistake. Tab-separated
        # files happened to be safe (a tab is whitespace); comma, semicolon and
        # pipe were not.
        raw = " ".join(c.strip() for c in row if c and c.strip())

        doi_cell = _cell(row, fields, "doi")
        doi = normalize_doi_field(doi_cell) if doi_cell else None
        if not doi:
            # No usable DOI column value — the DOI is often parked in a URL or
            # notes column instead, so scan the row. find_doi (not
            # normalize_doi_field) because this text is unstructured.
            doi = find_doi(raw)

        pmid_cell = _cell(row, fields, "pmid")
        pmid = _clean_pmid(pmid_cell) if pmid_cell else None
        if not pmid:
            pmid = find_pmid(raw)

        year_cell = _cell(row, fields, "year")
        year = find_year(year_cell) if year_cell else None

        author_cell = _cell(row, fields, "author")
        author = _first_author_surname(author_cell) if author_cell else None

        title = _cell(row, fields, "title")
        journal = _cell(row, fields, "journal")
        key = _cell(row, fields, "key")

        if not any((doi, pmid, title, author, journal)):
            continue  # a row of nothing usable (e.g. a trailing totals line)

        refs.append(
            Reference(
                raw=raw,
                doi=doi,
                title=_clean_bibtex_value(title) if title else None,
                author=author,
                year=year,
                journal=_clean_bibtex_value(journal) if journal else None,
                pmid=pmid,
                key=key,
                fields={"type": "csv"},
            )
        )
    return refs


# --- CSL-JSON (Zotero / Better BibTeX / pandoc) -----------------------------


def looks_like_csl_json(text: str) -> bool:
    """True if *text* parses as a CSL-JSON array (or single item object)."""
    stripped = text.lstrip()
    if not stripped or stripped[0] not in "[{":
        return False
    try:
        data = json.loads(text)
    except (ValueError, TypeError, RecursionError):
        # RecursionError: pathologically deeply-nested JSON overflows the decoder
        # (it is a RuntimeError, not a ValueError) — treat as "not CSL-JSON".
        return False
    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list):
        return False
    # Must look like citation items, not some other JSON document.
    return any(
        isinstance(item, dict)
        and any(k in item for k in ("DOI", "title", "author", "issued", "id", "type"))
        for item in data
    )


def _csl_first_str(value) -> Optional[str]:
    """A CSL field that may be a str or a list-of-str → the first string."""
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, list):
        for v in value:
            if isinstance(v, str) and v.strip():
                return v.strip()
    return None


def _csl_year(item: dict) -> Optional[int]:
    for key in ("issued", "published", "published-print", "published-online"):
        date = item.get(key)
        if isinstance(date, dict):
            parts = date.get("date-parts")
            if isinstance(parts, list) and parts and isinstance(parts[0], list) and parts[0]:
                try:
                    return int(parts[0][0])
                except (TypeError, ValueError, OverflowError):
                    # OverflowError: `1e400` is *valid* JSON that json.loads
                    # decodes to float('inf'), and int(inf) raises OverflowError
                    # (not ValueError) — which used to abort the whole run with a
                    # traceback on a standards-conformant file.
                    pass
            raw = date.get("raw") or date.get("literal")
            if isinstance(raw, str):
                y = find_year(raw)
                if y:
                    return y
    return None


def _csl_first_author(item: dict) -> Optional[str]:
    authors = item.get("author")
    if not isinstance(authors, list):
        return None
    for a in authors:
        if isinstance(a, dict):
            fam = a.get("family")
            if isinstance(fam, str) and fam.strip():
                return fam.strip()
            literal = a.get("literal")
            if isinstance(literal, str) and literal.strip():
                return _first_author_surname(literal)
    return None


def parse_csl_json(text: str) -> list[Reference]:
    """Parse a CSL-JSON reference list (Zotero / Better BibTeX / pandoc)."""
    try:
        data = json.loads(text)
    except (ValueError, TypeError, RecursionError):
        return []
    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list):
        return []
    refs: list[Reference] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        doi_raw = item.get("DOI") or item.get("doi")
        doi = normalize_doi_field(doi_raw) if isinstance(doi_raw, str) else None
        title = _csl_first_str(item.get("title"))
        journal = _csl_first_str(item.get("container-title")) or _csl_first_str(
            item.get("collection-title")
        )
        pmid_raw = item.get("PMID") or item.get("pmid")
        if pmid_raw is not None and not isinstance(pmid_raw, bool):
            pmid = _clean_pmid(str(pmid_raw))
        else:
            pmid = find_pmid(str(item.get("note", "")))
        key = item.get("id")
        refs.append(
            Reference(
                raw=json.dumps(item, ensure_ascii=False, sort_keys=True),
                doi=doi,
                title=title,
                author=_csl_first_author(item),
                year=_csl_year(item),
                journal=journal,
                pmid=pmid,
                key=str(key) if key is not None else None,
                fields={"type": "csl:" + str(item.get("type", "?"))},
            )
        )
    return refs


def detect_format(text: str) -> str:
    """Auto-detect the reference format of *text*.

    Order matters: CSL-JSON (a JSON document) is unambiguous, RIS needs a
    ``TY  - `` header, BibTeX needs a real ``@type{key,`` entry, CSV needs a
    header row naming a DOI/PMID/title column, everything else is free text. A
    stray ``@`` (e.g. an email address) must not route to BibTeX, and a
    comma-rich plain-text reference list must not route to CSV.
    """
    if looks_like_csl_json(text):
        return "csljson"
    if looks_like_ris(text):
        return "ris"
    if _ENTRY_RE.search(text):
        return "bibtex"
    if looks_like_csv(text):
        return "csv"
    return "text"


_PARSERS = {
    "bibtex": parse_bibtex,
    "ris": parse_ris,
    "csljson": parse_csl_json,
    "csv": parse_csv,
    "text": parse_text,
}


def parse_references(text: str, fmt: str = "auto") -> list[Reference]:
    """Parse *text* into references.

    fmt: "bibtex", "ris", "csljson", "csv", "text", or "auto" (detect).
    """
    if fmt == "auto":
        fmt = detect_format(text)
    parser = _PARSERS.get(fmt, parse_text)
    return parser(text)
