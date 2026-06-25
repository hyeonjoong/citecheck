"""Command-line interface for citecheck."""

from __future__ import annotations

import argparse
import json
import sys
import time
from typing import Optional

from . import __version__
from .core import CheckResult, CrossrefClient, ERROR, OK, WARNING, check_reference
from .parsers import parse_references

_COLORS = {OK: "\033[32m", WARNING: "\033[33m", ERROR: "\033[31m", "reset": "\033[0m"}
_SYMBOL = {OK: "✓", WARNING: "!", ERROR: "✗"}


def _color(text: str, severity: str, enabled: bool) -> str:
    if not enabled:
        return text
    return f"{_COLORS[severity]}{text}{_COLORS['reset']}"


def _print_result(result: CheckResult, use_color: bool, verbose: bool) -> None:
    status = result.status
    if status == OK and not verbose:
        return
    header = f"{_SYMBOL[status]} {result.reference.label()}"
    print(_color(header, status, use_color))
    for f in result.findings:
        if f.severity == OK and not verbose:
            continue
        for i, line in enumerate(f.message.splitlines()):
            prefix = "    " if i == 0 else ""
            print(f"    {prefix}{line}" if i == 0 else f"      {line}")


def _to_json(results: list[CheckResult]) -> str:
    payload = []
    for r in results:
        payload.append(
            {
                "label": r.reference.label(),
                "doi": r.reference.doi,
                "status": r.status,
                "findings": [{"severity": f.severity, "message": f.message} for f in r.findings],
            }
        )
    return json.dumps(payload, indent=2, ensure_ascii=False)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="citecheck",
        description="Verify manuscript citations against Crossref: catch broken DOIs, "
        "metadata mismatches, and retractions.",
    )
    p.add_argument("input", nargs="?", default="-", help="Input file (.bib or text). '-' for stdin.")
    p.add_argument(
        "--format",
        choices=["auto", "bibtex", "text"],
        default="auto",
        help="Input format (default: auto-detect).",
    )
    p.add_argument("--json", action="store_true", help="Emit a JSON report instead of text.")
    p.add_argument("--mailto", help="Your email — joins Crossref's faster 'polite' API pool.")
    p.add_argument(
        "--delay",
        type=float,
        default=0.2,
        help="Seconds to wait between Crossref calls (default: 0.2).",
    )
    p.add_argument("-v", "--verbose", action="store_true", help="Also show verified references.")
    p.add_argument("--no-color", action="store_true", help="Disable coloured output.")
    p.add_argument("--version", action="version", version=f"citecheck {__version__}")
    return p


def run(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)

    if args.input == "-":
        text = sys.stdin.read()
    else:
        try:
            with open(args.input, "r", encoding="utf-8") as fh:
                text = fh.read()
        except OSError as e:
            print(f"citecheck: cannot read {args.input}: {e}", file=sys.stderr)
            return 2

    refs = parse_references(text, fmt=args.format)
    if not refs:
        print("citecheck: no references found in input.", file=sys.stderr)
        return 2

    client = CrossrefClient(mailto=args.mailto)
    use_color = sys.stdout.isatty() and not args.no_color

    results: list[CheckResult] = []
    for i, ref in enumerate(refs):
        results.append(check_reference(ref, client))
        if args.delay and i < len(refs) - 1:
            time.sleep(args.delay)

    if args.json:
        print(_to_json(results))
    else:
        for r in results:
            _print_result(r, use_color, args.verbose)
        n_err = sum(r.status == ERROR for r in results)
        n_warn = sum(r.status == WARNING for r in results)
        n_ok = sum(r.status == OK for r in results)
        print()
        summary = f"checked {len(results)} references: {n_ok} ok, {n_warn} warnings, {n_err} errors"
        sev = ERROR if n_err else (WARNING if n_warn else OK)
        print(_color(summary, sev, use_color))

    return 1 if any(r.status == ERROR for r in results) else 0


def main() -> None:  # console-script entry point
    sys.exit(run())


if __name__ == "__main__":
    main()
