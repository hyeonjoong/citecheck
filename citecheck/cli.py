"""Command-line interface for citecheck."""

from __future__ import annotations

import argparse
import csv
import io
import json
import math
import os
import re
import sys
import time
from collections import Counter
from typing import Optional

from . import __version__
from .core import (
    CODES,
    CONTROL_CHARS_RE,
    CheckResult,
    CrossrefClient,
    DiskCache,
    ERROR,
    OK,
    PubMedClient,
    WARNING,
    check_reference,
    sanitize_text,
)
from .office import (
    OfficeError,
    binary_input_hint,
    convert_office_bytes,
    looks_like_zip,
)
from .parsers import (
    csv_unmatched_columns,
    detect_format,
    malformed_entry_keys,
    parse_references,
)
from .profile import build_profile, profile_lines, profile_markdown

DEFAULT_CACHE_PATH = os.path.join(
    os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache"),
    "citecheck",
    "lookups.json",
)

# Exit codes.
EXIT_OK = 0
EXIT_PROBLEM = 1  # a hard error (broken DOI / retraction), or --strict warning
EXIT_USAGE = 2  # bad input: unreadable file, no references
EXIT_INCONCLUSIVE = 3  # a lookup failed (e.g. offline) — could not verify

_COLORS = {OK: "\033[32m", WARNING: "\033[33m", ERROR: "\033[31m", "reset": "\033[0m"}
_SYMBOL = {OK: "✓", WARNING: "!", ERROR: "✗"}

# Strip C0/C1 control characters (incl. ESC) from any externally sourced text
# before printing, so a crafted BibTeX title or poisoned Crossref record cannot
# inject ANSI/terminal escape sequences into the terminal. The pattern itself
# lives in core so citecheck.profile shares this one control (see core).
_CONTROL_RE = CONTROL_CHARS_RE
_sanitize = sanitize_text


def _color(text: str, severity: str, enabled: bool) -> str:
    if not enabled:
        return text
    return f"{_COLORS[severity]}{text}{_COLORS['reset']}"


# Hangul (syllables, conjoining jamo, compatibility jamo) in runs of two or more.
#
# This is what tells a genuinely cp949-encoded Korean file from a latin-1
# European one that merely *happens* to decode as cp949. Korean words are
# multi-syllable, so real Korean text is full of runs; latin-1 mojibake produces
# LONE syllables, because each accented byte pairs with the ASCII letter that
# follows it and the rest of the word survives as ASCII —
# "Ärzteblatt" (0xC4 0x72 …) becomes "훣zteblatt", one syllable adrift in Latin.
_HANGUL_RUN_RE = re.compile(r"[가-힣ᄀ-ᇿ㄰-㆏]{2,}")


def _decode(raw: bytes, override: Optional[str] = None) -> tuple[str, str]:
    """Decode input bytes, tolerating common non-UTF-8 encodings.

    Returns (text, encoding_used). Never raises: an explicit *override* (and the
    latin-1 / ``errors='replace'`` fallbacks) always succeed. Byte-order marks
    are detected first so a UTF-16/UTF-8-BOM file is handled and the BOM stripped
    (rather than surfacing as a phantom ``\\ufeff`` reference).
    """
    if override:
        return raw.decode(override, errors="replace"), override
    if raw[:3] == b"\xef\xbb\xbf":  # UTF-8 BOM — strip it (else a phantom ﻿ ref)
        return raw.decode("utf-8-sig"), "utf-8-sig"
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):  # UTF-16 LE/BE BOM
        try:
            return raw.decode("utf-16"), "utf-16"
        except UnicodeDecodeError:
            pass
    for enc in ("utf-8", "cp949", "latin-1"):
        try:
            decoded = raw.decode(enc)
        except UnicodeDecodeError:
            continue
        # cp949 is tried before latin-1 because a Korean file must not be
        # silently mangled — but cp949 accepts far more byte sequences than it
        # should, so "it decoded" is not evidence that it was right. A European
        # reference list in latin-1/cp1252 decodes *cleanly* as cp949 whenever an
        # accented byte is followed by a valid trail byte, which an ordinary
        # letter is:
        #
        #     "Deutsches Ärzteblatt" -> "Deutsches 훣zteblatt"   (cp949)
        #     "Ángel"                -> "햚gel"                  (cp949)
        #
        # No exception is raised, so the old fall-through loop accepted it and
        # reported only a soft "input was not UTF-8" note. The corruption is
        # data-dependent — "Müller" survives, because 0xFC 0x6C is not a legal
        # cp949 pair — which makes it worse, not better: it mangles some author
        # names and journal titles in a file and leaves others intact, and the
        # damage surfaces downstream as false `author-mismatch` and
        # `journal-mismatch` warnings against Crossref.
        #
        # So cp949 must also *look* like Korean before we believe it. If it does
        # not, fall through to latin-1, which is what such a file almost always
        # is. (`--encoding cp949` still forces the issue for the rare Korean file
        # whose only Hangul is a single isolated syllable.)
        if enc == "cp949" and not _HANGUL_RUN_RE.search(decoded):
            continue
        return decoded, enc
    return raw.decode("utf-8", errors="replace"), "utf-8/replace"


def _print_result(result: CheckResult, use_color: bool, verbose: bool) -> None:
    status = result.status
    if status == OK and not verbose:
        return
    header = f"{_SYMBOL[status]} {_sanitize(result.reference.label())}"
    print(_color(header, status, use_color))
    for f in result.findings:
        if f.severity == OK and not verbose:
            continue
        lines = f.message.splitlines() or [""]
        for i, line in enumerate(lines):
            indent = "    " if i == 0 else "      "
            print(f"{indent}{_sanitize(line)}")


def _to_json(results: list[CheckResult]) -> str:
    return json.dumps(_json_payload(results), indent=2, ensure_ascii=False)


def _json_payload(results: list[CheckResult]) -> list:
    payload = []
    for r in results:
        payload.append(
            {
                # Sanitize the same C0/C1 control chars the text/csv/markdown
                # paths strip, so a poisoned title can't smuggle terminal escapes
                # through the JSON report either (ensure_ascii=False emits them raw).
                "label": _sanitize(r.reference.label()),
                "doi": _sanitize(r.reference.doi) if r.reference.doi else None,
                "pmid": r.reference.pmid,
                "journal": _sanitize(r.reference.journal) if r.reference.journal else None,
                "status": r.status,
                "findings": [
                    # `code` is the stable, machine-readable identity of the
                    # finding — CI should branch on it, never on the prose.
                    {
                        "severity": f.severity,
                        "code": f.code,
                        "message": _sanitize(f.message),
                    }
                    for f in r.findings
                ],
            }
        )
    return payload


def _csv_safe(value: str) -> str:
    """Neutralise CSV-injection: a leading =/+/-/@ makes a spreadsheet treat the
    cell as a formula, so prefix such a cell with a single quote."""
    value = _sanitize(value)
    if value and value[0] in "=+-@\t\r":
        return "'" + value
    return value


def _to_csv(results: list[CheckResult]) -> str:
    """One row per reference — openable in Excel by a co-author."""
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["label", "doi", "pmid", "journal", "status", "codes", "findings"])
    for r in results:
        messages = " | ".join(f.message.replace("\n", " ").strip() for f in r.findings)
        # A codes column lets a co-author filter/pivot the sheet without reading
        # every prose message.
        codes = " ".join(dict.fromkeys(f.code for f in r.findings if f.code))
        writer.writerow(
            [
                _csv_safe(r.reference.label()),
                _csv_safe(r.reference.doi or ""),
                _csv_safe(r.reference.pmid or ""),
                _csv_safe(r.reference.journal or ""),
                r.status,
                _csv_safe(codes),
                _csv_safe(messages),
            ]
        )
    return buf.getvalue().rstrip("\r\n")


def _md_cell(text: str) -> str:
    return _sanitize(text).replace("|", "\\|").replace("\n", " ").strip()


def _to_markdown(results: list[CheckResult]) -> str:
    """A Markdown report for pasting into a PR / issue / lab notebook."""
    n_err = sum(r.status == ERROR for r in results)
    n_warn = sum(r.status == WARNING for r in results)
    n_ok = sum(r.status == OK for r in results)
    lines = [
        "# citecheck report",
        "",
        f"Checked **{len(results)}** references: "
        f"**{n_ok}** ok, **{n_warn}** warnings, **{n_err}** errors.",
        "",
        "| Status | Reference | DOI | Findings |",
        "| :----: | --------- | --- | -------- |",
    ]
    symbol = {OK: "✓", WARNING: "!", ERROR: "✗"}
    for r in results:
        findings = "<br>".join(_md_cell(f.message) for f in r.findings) or "—"
        lines.append(
            f"| {symbol[r.status]} | {_md_cell(r.reference.label())} "
            f"| {_md_cell(r.reference.doi or '—')} | {findings} |"
        )
    return "\n".join(lines)


def _flag_duplicate_dois(results: list[CheckResult]) -> None:
    """Warn on DOIs that appear under more than one reference in the manuscript."""
    counts = Counter(r.reference.doi for r in results if r.reference.doi)
    dups = {doi for doi, n in counts.items() if n > 1}
    for r in results:
        if r.reference.doi in dups:
            r.add(
                WARNING,
                f"Duplicate DOI — cited by {counts[r.reference.doi]} references: "
                f"{r.reference.doi}",
                "duplicate-doi",
            )


def _flag_duplicate_pmids(results: list[CheckResult]) -> None:
    """Warn on PMIDs cited more than once — but only where it isn't already
    caught as a duplicate DOI (same paper, two entries), to avoid double noise.

    The "already reported" test is keyed on the finding *code*, not on the
    English prose of ``_flag_duplicate_dois``'s message. Matching
    ``"Duplicate DOI" in f.message`` made the de-duplication silently depend on
    one f-string's exact wording: rephrasing it to "This DOI is cited twice"
    would have restored the double warning on every same-paper duplicate, with
    no test failing. That is precisely the coupling the codes exist to retire
    (see the note on ``lookup-failed`` and exit codes below).
    """
    counts = Counter(r.reference.pmid for r in results if r.reference.pmid)
    dups = {pmid for pmid, n in counts.items() if n > 1}
    for r in results:
        pmid = r.reference.pmid
        if pmid in dups and not any(f.code == "duplicate-doi" for f in r.findings):
            r.add(
                WARNING, f"Duplicate PMID — cited by {counts[pmid]} references: {pmid}",
                "duplicate-pmid",
            )


# `lookup-failed` is deliberately NOT ignorable. It does not mean "a citation is
# wrong"; it means **we could not check**, and the tool's headline promise is
# that a network outage can never be mistaken for a clean pass:
#
#   $ citecheck refs.bib --ignore lookup-failed     # offline
#     checked 1 references: 1 ok, 0 warnings, 0 errors      <- a lie
#     (0 of 1 compared against a Crossref record)           <- contradicting it
#   exit 0
#
# It is an attractive nuisance precisely because it looks like noise to silence:
# a user who hides it once gets false clean passes forever after. Every other
# code is a judgement call the user is entitled to make; this one is a fact about
# whether the run happened.
NON_IGNORABLE = {"lookup-failed"}


def _parse_ignore(raw: str) -> tuple[set, list, list]:
    """Split an --ignore value into (usable codes, unknown tokens, refused codes).

    Unrecognised codes are returned rather than dropped so the caller can fail
    loudly: a typo'd `--ignore no-doi,retracton` must never quietly fail to
    suppress what the user asked for — nor quietly suppress a retraction.
    """
    wanted = [tok.strip().lower() for tok in raw.replace(" ", ",").split(",") if tok.strip()]
    known = {tok for tok in wanted if tok in CODES}
    unknown = [tok for tok in wanted if tok not in CODES]
    refused = sorted(known & NON_IGNORABLE)
    return known - NON_IGNORABLE, unknown, refused


def _apply_ignores(results: list[CheckResult], ignored: set) -> int:
    """Drop ignored findings in place. Returns how many were removed."""
    if not ignored:
        return 0
    removed = 0
    for r in results:
        keep = [f for f in r.findings if f.code not in ignored]
        removed += len(r.findings) - len(keep)
        r.findings = keep
    return removed


_SEVERITY_ORDER = {ERROR: 0, WARNING: 1, OK: 2}


def _by_severity(results: list[CheckResult]) -> list[CheckResult]:
    """Errors first, then warnings, then verified — input order within each.

    On a 200-reference manuscript the one retraction is otherwise buried among
    60 "No DOI found" lines, with identical visual weight. Python's sort is
    stable, so the reference list's own order survives inside each group.
    """
    return sorted(results, key=lambda r: _SEVERITY_ORDER.get(r.status, 3))


def _print_checks() -> None:
    print("Finding codes (use with --ignore):\n")
    width = max(len(c) for c in CODES)
    for code, meaning in sorted(CODES.items()):
        note = "  [cannot be ignored]" if code in NON_IGNORABLE else ""
        print(f"  {code.ljust(width)}  {meaning}{note}")


def _network_calls(client, pubmed) -> int:
    """Total lookups the clients have sent to their transport so far.

    Used only to tell a cache hit from a real call, so the inter-request delay is
    charged only when we actually went out to Crossref/PubMed.
    """
    return getattr(client, "remote_calls", 0) + getattr(pubmed, "remote_calls", 0)


def _nonneg_float(value: str) -> float:
    f = float(value)
    if not math.isfinite(f) or f < 0:
        raise argparse.ArgumentTypeError("must be a finite number >= 0")
    return f


# Ten years. A TTL beyond this is indistinguishable from "never expire", which
# defeats the whole point of expiry (catching a newly-retracted reference).
# Bounding it here also stops `--cache-ttl 1e308` from overflowing to `inf` once
# multiplied into seconds, which silently made every entry immortal.
MAX_CACHE_TTL_DAYS = 3650.0


def _cache_ttl_days(value: str) -> float:
    f = _nonneg_float(value)
    if f > MAX_CACHE_TTL_DAYS:
        raise argparse.ArgumentTypeError(
            f"must be <= {MAX_CACHE_TTL_DAYS:g} days — a longer cache could hide "
            f"a retraction indefinitely"
        )
    return f


# Bounds for --as-of. Not a style preference: an age is `as_of - year`, so a
# typo'd `--as-of 20255` silently turns every reference into an 18,000-year-old
# one and reports a Price index of 0.00 with a straight face.
MIN_PROFILE_YEAR = 1600
MAX_PROFILE_YEAR = 2200


def _profile_year(value: str) -> int:
    try:
        year = int(value, 10)
    except ValueError:
        raise argparse.ArgumentTypeError("must be a 4-digit year, e.g. 2026") from None
    if not MIN_PROFILE_YEAR <= year <= MAX_PROFILE_YEAR:
        raise argparse.ArgumentTypeError(
            f"must be between {MIN_PROFILE_YEAR} and {MAX_PROFILE_YEAR}"
        )
    return year


def _parse_self_cite(raw: str) -> tuple:
    """Split a --self-cite value into surnames (deduplicated, order preserved)."""
    names = [tok.strip() for tok in (raw or "").replace(";", ",").split(",") if tok.strip()]
    return tuple(dict.fromkeys(names))


_MAX_NAMED_SKIPPED = 10


def _skipped_entries_warning(keys: list) -> str:
    """The stderr line naming BibTeX entries that could not be parsed."""
    shown = [_sanitize(k) or "(no key)" for k in keys[:_MAX_NAMED_SKIPPED]]
    more = len(keys) - len(shown)
    tail = f" and {more} more" if more else ""
    noun = "entry" if len(keys) == 1 else "entries"
    return (
        f"citecheck: warning: {len(keys)} malformed BibTeX {noun} could not be parsed "
        f"and {'was' if len(keys) == 1 else 'were'} not checked (unbalanced braces or "
        f"parentheses): {', '.join(shown)}{tail}."
    )


def _unused_columns_note(columns: list) -> str:
    """The stderr note for a table that is checked on its DOI/PMID column only."""
    shown = ", ".join(_sanitize(c) for c in columns[:_MAX_NAMED_SKIPPED])
    more = len(columns) - min(len(columns), _MAX_NAMED_SKIPPED)
    tail = f" and {more} more" if more else ""
    return (
        "citecheck: note: no title, author, year or journal column was recognised, "
        "so only the DOI/PMID is checked and the rest is not compared. "
        f"Columns not used: {shown}{tail}."
    )


def _stdin_is_terminal() -> bool:
    """True when stdin is an interactive terminal (nothing was piped in)."""
    try:
        return bool(sys.stdin is not None and sys.stdin.isatty())
    except (AttributeError, ValueError, OSError):
        return False


# ANSI SGR sequences. Python 3.14's argparse colours its usage text when the
# environment asks for colour; the hint below must stay plain text.
_ANSI_SGR_RE = re.compile(r"\x1b\[[0-9;]*m")


def _no_input_hint(parser: argparse.ArgumentParser) -> str:
    usage = _ANSI_SGR_RE.sub("", parser.format_usage()).rstrip()
    return (
        f"{usage}\n"
        "citecheck: give a reference file, or pipe one in "
        "(파일 경로를 주거나 파이프로 넘기세요)."
    )


def _read_input(path: str) -> tuple[Optional[bytes], Optional[str]]:
    """Read the input file (or stdin) as bytes. Returns (bytes, error message)."""
    if path == "-":
        if sys.stdin is None:
            return None, "citecheck: no input: give a reference file, or pipe one in."
        return sys.stdin.buffer.read(), None
    try:
        with open(path, "rb") as fh:
            return fh.read(), None
    except OSError as e:
        return None, f"citecheck: cannot read {path}: {e}"


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="citecheck",
        description="Verify manuscript citations against Crossref: catch broken DOIs, "
        "metadata mismatches, retractions and expressions of concern, and preprints "
        "that now have a published version. Requires internet access to Crossref.",
    )
    p.add_argument(
        "input",
        nargs="?",
        default="-",
        help="Input file (.bib / .ris / .json / .csv / .xlsx / .docx / text). "
        "'-' for stdin.",
    )
    p.add_argument(
        "--format",
        choices=["auto", "bibtex", "ris", "csljson", "csv", "text"],
        default="auto",
        help="Input format: bibtex, ris (EndNote/Zotero), csljson (Zotero/pandoc), "
        "csv (Excel/Sheets/Covidence reference table, also TSV), text, or "
        "auto-detect (default). .xlsx/.docx are recognised from the file's own "
        "bytes and converted first (a worksheet is then read as csv).",
    )
    p.add_argument(
        "--sheet",
        metavar="NAME|N",
        help="For an .xlsx input, the worksheet to read (name, or 1-based tab "
        "number). Default: the sheet yielding the most references.",
    )
    p.add_argument(
        "--report",
        choices=["text", "json", "csv", "markdown"],
        default="text",
        help="Output report format (default: text). csv/markdown are shareable "
        "with co-authors.",
    )
    p.add_argument(
        "--json",
        action="store_true",
        help="Shorthand for --report json.",
    )
    p.add_argument(
        "--encoding",
        help="Force the input file encoding (e.g. latin-1, cp949). "
        "Default: auto-detect (UTF-8/BOM, then cp949, then latin-1). "
        "Does not apply to .xlsx/.docx (their XML is always UTF-8).",
    )
    p.add_argument("--mailto", help="Your email — sent to Crossref/PubMed in the request header to "
                   "join the faster 'polite' API pool.")
    p.add_argument(
        "--pubmed",
        action="store_true",
        help="Also cross-check references that carry a PMID against PubMed: "
        "catch retractions Crossref misses and PMID↔DOI mismatches. "
        "Requires internet access to eutils.ncbi.nlm.nih.gov.",
    )
    p.add_argument(
        "--suggest-doi",
        action="store_true",
        help="For references with no DOI, search Crossref by title/author/year "
        "and report the DOI of a confident match, so you can add it.",
    )
    p.add_argument(
        "--cache",
        nargs="?",
        const=DEFAULT_CACHE_PATH,
        metavar="PATH",
        help=f"Cache Crossref/PubMed lookups on disk so re-running over the same "
        f"manuscript is instant. Bare --cache uses {DEFAULT_CACHE_PATH}.",
    )
    p.add_argument(
        "--cache-ttl",
        type=_cache_ttl_days,
        default=7.0,
        metavar="DAYS",
        help="Days a cached lookup stays valid (default: 7). Entries expire so a "
        "newly-retracted reference cannot hide behind a stale cache.",
    )
    p.add_argument(
        "--delay",
        type=_nonneg_float,
        default=0.2,
        help="Seconds to wait between Crossref calls (default: 0.2).",
    )
    p.add_argument(
        "--ignore",
        metavar="CHECK[,CHECK...]",
        default="",
        help="Suppress findings by code, e.g. --ignore no-doi,correction. "
        "Makes --strict usable as a submission gate: a real manuscript cites "
        "books and guidelines that have no DOI, which would otherwise fail it "
        "forever. Use --list-checks to see every code.",
    )
    p.add_argument(
        "--list-checks",
        action="store_true",
        help="Print every finding code (for --ignore) with its meaning, and exit.",
    )
    p.add_argument(
        "--strict",
        action="store_true",
        help="Exit non-zero on warnings too (not just errors).",
    )
    p.add_argument(
        "--profile",
        action="store_true",
        help="Also report descriptive statistics for the reference list as a whole: "
        "DOI coverage, publication-year median/IQR, median age, Price index "
        "(share published within the last 5 years), journal spread, Crossref "
        "document types and integrity flags. Costs no extra lookups.",
    )
    p.add_argument(
        "--as-of",
        type=_profile_year,
        metavar="YEAR",
        help="Year that --profile measures reference ages against "
        "(default: the current year).",
    )
    p.add_argument(
        "--self-cite",
        metavar="SURNAME[,SURNAME...]",
        default="",
        help="Count how many references are self-citations of these author "
        "surnames (implies --profile).",
    )
    p.add_argument("-v", "--verbose", action="store_true", help="Also show verified references.")
    p.add_argument("--no-color", action="store_true", help="Disable coloured output.")
    p.add_argument("--version", action="version", version=f"citecheck {__version__}")
    return p


def run(
    argv: Optional[list[str]] = None,
    client: Optional[CrossrefClient] = None,
    pubmed: Optional[PubMedClient] = None,
) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.list_checks:
        _print_checks()
        return EXIT_OK

    ignored, unknown_ignores, refused_ignores = _parse_ignore(args.ignore)
    if unknown_ignores:
        print(
            f"citecheck: unknown --ignore code{'' if len(unknown_ignores) == 1 else 's'}: "
            f"{', '.join(unknown_ignores)}. Run --list-checks to see valid codes.",
            file=sys.stderr,
        )
        return EXIT_USAGE
    if refused_ignores:
        print(
            f"citecheck: {', '.join(refused_ignores)} cannot be ignored — it means "
            f"a lookup did not happen, not that a citation is wrong. Hiding it "
            f"would let an offline run report a clean pass.",
            file=sys.stderr,
        )
        return EXIT_USAGE

    self_cite = _parse_self_cite(args.self_cite)
    # `--self-cite ",,"` parses to nothing. Silently treating that as "no
    # profile requested" meant a user who asked for a self-citation count got a
    # normal run and no hint that their argument was empty.
    if args.self_cite.strip() and not self_cite:
        print(
            "citecheck: --self-cite lists no surname — give one or more, "
            "e.g. --self-cite Kim,Park.",
            file=sys.stderr,
        )
        return EXIT_USAGE
    want_profile = args.profile or bool(self_cite)
    if args.as_of is not None and not want_profile:
        print(
            "citecheck: note: --as-of has no effect without --profile "
            "(it only sets the year reference ages are measured against).",
            file=sys.stderr,
        )

    # Reading stdin from a terminal waits for the keyboard with no prompt, so a
    # bare `citecheck` looked like a hang. Say what is expected instead.
    if args.input == "-" and _stdin_is_terminal():
        print(_no_input_hint(parser), file=sys.stderr)
        return EXIT_USAGE

    raw, read_error = _read_input(args.input)
    if raw is None:
        print(read_error, file=sys.stderr)
        return EXIT_USAGE

    # An .xlsx / .docx arrives as a ZIP container, not text. Convert it here —
    # before decoding — so the whole downstream pipeline (format detection, the
    # CSV column-alias table, the free-text parser) is untouched by it.
    office_kind = office_detail = None
    if looks_like_zip(raw):
        try:
            converted = convert_office_bytes(raw, sheet=args.sheet)
        except OfficeError as e:
            # `e` embeds names from inside an untrusted archive — sanitize before
            # it reaches a terminal (office._echo already does, belt and braces).
            print(
                f"citecheck: cannot read {args.input} as an Office file: "
                f"{_sanitize(str(e))}",
                file=sys.stderr,
            )
            return EXIT_USAGE
        if converted is None:
            print(
                f"citecheck: {args.input} looks like a ZIP archive but not an .xlsx "
                f"or .docx. Export your references to .bib/.ris/.csv/.txt first.",
                file=sys.stderr,
            )
            return EXIT_USAGE
        text, office_kind, office_detail = converted
        encoding = office_kind
    else:
        # A .doc/.xls/.pdf is not a ZIP and would otherwise decode as latin-1
        # mojibake and be "checked" as a reference at exit 0.
        hint = binary_input_hint(raw)
        if hint:
            print(f"citecheck: cannot read {args.input}: {hint}.", file=sys.stderr)
            return EXIT_USAGE
        try:
            text, encoding = _decode(raw, override=args.encoding)
        except LookupError:
            print(f"citecheck: unknown encoding: {args.encoding}", file=sys.stderr)
            return EXIT_USAGE
    if office_kind == "xlsx":
        print(
            f"citecheck: note: read {args.input} as an Excel workbook — "
            f"worksheet {_sanitize(office_detail or '?')!r}"
            f"{'' if args.sheet else ' (the sheet yielding the most references; use --sheet to choose)'}.",
            file=sys.stderr,
        )
    elif office_kind == "docx":
        print(
            f"citecheck: note: read {args.input} as a Word document — one reference "
            f"per paragraph, including footnotes/endnotes. Word files carry no "
            f"structured fields, so author/year/journal are not compared "
            f"(see the README); every non-reference paragraph is reported as "
            f"'No DOI found'.",
            file=sys.stderr,
        )
    elif encoding not in ("utf-8", "utf-8-sig") and not args.encoding:
        print(
            f"citecheck: note: input was not UTF-8; decoded as {encoding}. "
            f"Use --encoding to override if author names look wrong.",
            file=sys.stderr,
        )
    if office_kind and args.encoding:
        print(
            "citecheck: note: --encoding does not apply to .xlsx/.docx "
            "(their XML is always UTF-8); it was ignored.",
            file=sys.stderr,
        )

    # CRLF (Windows) and bare CR (classic Mac, Excel's "CSV (Macintosh)") become
    # LF once, here, so every parser sees one kind of line ending. A CR-only CSV
    # used to read as a single row.
    if "\r" in text:
        text = text.replace("\r\n", "\n").replace("\r", "\n")

    # A converted worksheet is *structured* data and must be parsed as a table,
    # not auto-detected. This is a privacy control, not a nicety: if the CSV
    # detector rejects the sheet, `auto` falls through to the free-text parser,
    # which marks every row `heuristic_fields=True` — and `--suggest-doi` then
    # sends that row's raw text (Study ID, MRN, clinical notes and all) to
    # api.crossref.org as a query string. Forcing `csv` keeps the row's fields
    # confined to their named columns, exactly as for a hand-saved .csv.
    fmt = args.format
    if office_kind == "xlsx" and fmt == "auto":
        fmt = "csv"

    refs = parse_references(text, fmt=fmt)

    # BibTeX entries that were present but could not be parsed. Named before the
    # "no references" exit too, so a file whose only entry is broken says which.
    resolved_fmt = detect_format(text) if fmt == "auto" else fmt
    skipped_keys = malformed_entry_keys(text) if resolved_fmt == "bibtex" else []
    if skipped_keys:
        print(_skipped_entries_warning(skipped_keys), file=sys.stderr)

    # A table whose only recognised column is the DOI (or PMID) is checked on
    # that alone. Name the other columns, so a header the alias table does not
    # know is visible instead of silently skipping the comparison.
    unused_columns = csv_unmatched_columns(text) if refs and resolved_fmt == "csv" else []
    if unused_columns:
        print(_unused_columns_note(unused_columns), file=sys.stderr)

    if not refs:
        if office_kind == "xlsx":
            print(
                f"citecheck: no reference table found in {args.input} "
                f"(worksheet {_sanitize(office_detail or '?')!r}). A reference table "
                f"needs a header row naming a DOI, PMID or Title column; use "
                f"--sheet to pick a different worksheet.",
                file=sys.stderr,
            )
            return EXIT_USAGE
        print("citecheck: no references found in input.", file=sys.stderr)
        return EXIT_USAGE

    # The cache is only built for clients we create — an injected client (tests,
    # or a library caller) brings its own caching policy and must not be
    # second-guessed here.
    cache = None
    if args.cache and (client is None or (args.pubmed and pubmed is None)):
        cache = DiskCache(args.cache, ttl_seconds=args.cache_ttl * 24 * 3600)

    if client is None:
        client = CrossrefClient(mailto=args.mailto, cache=cache)
    # A PubMed client is created only when --pubmed is set (or one is injected
    # for tests); otherwise the PMID cross-check is skipped entirely.
    if pubmed is None and args.pubmed:
        pubmed = PubMedClient(mailto=args.mailto, cache=cache)
    use_color = sys.stdout.isatty() and not args.no_color

    # --pubmed only acts on references that carry a PMID. On a file with none it
    # is a silent no-op, and the user — who asked for PubMed-grade retraction
    # coverage — gets byte-identical output and no hint that nothing happened.
    # Say so plainly rather than let the flag imply coverage it did not provide.
    if args.pubmed:
        with_pmid = sum(1 for r in refs if r.pmid)
        if not with_pmid:
            print(
                f"citecheck: warning: --pubmed had no effect — none of the "
                f"{len(refs)} references carry a PMID, so nothing was "
                f"cross-checked against PubMed.",
                file=sys.stderr,
            )
        elif with_pmid < len(refs):
            print(
                f"citecheck: note: --pubmed cross-checked {with_pmid} of "
                f"{len(refs)} references (the rest carry no PMID).",
                file=sys.stderr,
            )

    results: list[CheckResult] = []
    for i, ref in enumerate(refs):
        before = _network_calls(client, pubmed)
        results.append(
            check_reference(ref, client, pubmed=pubmed, suggest_missing=args.suggest_doi)
        )
        hit_network = _network_calls(client, pubmed) > before
        # Only pause when we actually called out — a cache hit costs Crossref
        # nothing, so rate-limiting it would just make a cached run slow for no
        # reason (the whole point of the cache).
        if args.delay and hit_network and i < len(refs) - 1:
            time.sleep(args.delay)

    if cache is not None and not cache.save():
        print(
            f"citecheck: note: could not write the cache at {args.cache} — "
            f"results are unaffected.",
            file=sys.stderr,
        )

    _flag_duplicate_dois(results)
    _flag_duplicate_pmids(results)

    # Built BEFORE --ignore is applied, deliberately: the profile answers "what
    # is this reference list actually made of", and a user who hides `correction`
    # from the per-reference report has not made those corrections stop existing.
    # --ignore silences the report; it must not quietly edit the statistics.
    profile = None
    if want_profile:
        as_of = args.as_of if args.as_of is not None else time.localtime().tm_year
        profile = build_profile(results, as_of_year=as_of, self_cite=self_cite)

    # Applied after every check has run, so --ignore only silences the *report*
    # — it never skips a lookup, and so can never change what else was found.
    n_ignored = _apply_ignores(results, ignored)

    # Errors first: on a long reference list the one retraction must not be
    # buried among the routine warnings.
    results = _by_severity(results)

    report = "json" if args.json else args.report
    if report == "json":
        # Without --profile the payload stays exactly what it has always been: a
        # JSON *array* of references. Wrapping it unconditionally would break
        # every CI script already indexing into it, so the object form appears
        # only when the user asked for the extra data.
        if profile is not None:
            print(json.dumps({"references": _json_payload(results), "profile": profile},
                             indent=2, ensure_ascii=False))
        else:
            print(_to_json(results))
    elif report == "csv":
        print(_to_csv(results))
        if profile is not None:
            # The CSV report is a table with one row per reference; appending
            # profile rows would corrupt it for the co-author who opens it in
            # Excel. So it goes to stderr, leaving `--report csv > out.csv` clean.
            for line in profile_lines(profile):
                print(line, file=sys.stderr)
    elif report == "markdown":
        print(_to_markdown(results))
        if profile is not None:
            print(profile_markdown(profile))
    else:
        for r in results:
            _print_result(r, use_color, args.verbose)
        _print_summary(results, use_color)
        if n_ignored:
            print(
                f"  ({n_ignored} finding{'' if n_ignored == 1 else 's'} hidden by "
                f"--ignore {','.join(sorted(ignored))})"
            )
        if cache is not None and cache.hits:
            print(
                f"  ({cache.hits} lookup{'' if cache.hits == 1 else 's'} served "
                f"from the cache at {args.cache}; entries expire after "
                f"{args.cache_ttl:g} days)"
            )
        if profile is not None:
            print()
            for line in profile_lines(profile):
                print(_sanitize(line))

    has_error = any(r.status == ERROR for r in results)
    has_warning = any(r.status == WARNING for r in results)
    # A lookup failure (typically offline) means we could not actually verify —
    # never let that read as a clean pass in a CI gate.
    #
    # Keyed on the CODE, not on the message text. Matching `startswith("Lookup
    # failed")` made this headline safety property depend on the exact English
    # wording of four separate f-strings: rephrasing any one of them to "Could
    # not reach Crossref" would silently turn every offline run into exit 0.
    # That is the class of bug the codes exist to retire.
    lookup_failed = any(f.code == "lookup-failed" for r in results for f in r.findings)
    if has_error or (args.strict and has_warning):
        return EXIT_PROBLEM
    # A BibTeX entry that could not be parsed was never checked, which is the
    # same "could not verify" class as a failed lookup.
    if lookup_failed or skipped_keys:
        return EXIT_INCONCLUSIVE
    return EXIT_OK


def _print_summary(results: list[CheckResult], use_color: bool) -> None:
    n_err = sum(r.status == ERROR for r in results)
    n_warn = sum(r.status == WARNING for r in results)
    n_ok = sum(r.status == OK for r in results)
    # Two disjoint buckets that add up to len(results): either we retrieved a
    # Crossref record and compared against it, or we did not. The previous
    # wording ("N verified, M could not be verified") silently excluded
    # hard-error references from both, so the two numbers did not sum to the
    # total and the reader was left to wonder where the rest went.
    n_checked = sum(1 for r in results if r.crossref is not None)
    n_not_checked = len(results) - n_checked
    print()
    summary = f"checked {len(results)} references: {n_ok} ok, {n_warn} warnings, {n_err} errors"
    sev = ERROR if n_err else (WARNING if n_warn else OK)
    print(_color(summary, sev, use_color))
    print(
        f"  ({n_checked} of {len(results)} compared against a Crossref record; "
        f"{n_not_checked} could not be — no DOI, broken DOI, or not in Crossref)"
    )


def main() -> None:  # console-script entry point
    sys.exit(run())


if __name__ == "__main__":
    main()
