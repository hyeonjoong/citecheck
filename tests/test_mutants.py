"""Tests closing gaps proven by mutation testing.

Each test here kills a mutant that survived the rest of the suite — i.e. a line
of citecheck could be deleted or inverted and everything still went green. The
code was correct in every case; nothing proved it. That is exactly the shape of
the round-1 bug (a field name nothing tested), so these are worth their weight.
"""

import csv
import io
import json

import pytest

from citecheck.cli import _csv_safe, run
from citecheck.core import (
    ERROR,
    OK,
    WARNING,
    _classify_updates,
    _is_retracted,
    _normalize_update_type,
    CrossrefClient,
)
from citecheck.parsers import count_malformed_entries, parse_references

# A record poisoned with terminal escapes, a CSV formula, and a markdown table
# break — everything an attacker controls via a title they got published.
POISONED = {
    "DOI": "10.1/x",
    "title": ["\x1b[2J\x1b[31mFAKE=cmd|' /C calc\x07 | broken"],
    "author": [{"family": "\x1b[31mEvil"}],
    "issued": {"date-parts": [[2024]]},
    "container-title": ["=HYPERLINK(\"http://evil\")"],
}


def write(tmp_path, name, data):
    p = tmp_path / name
    p.write_text(data, encoding="utf-8")
    return str(p)


def poisoned_client():
    return CrossrefClient(_fetch=lambda d: POISONED, _resolve=lambda d: True)


# --- 1. every report format must strip terminal escapes ---------------------


@pytest.mark.parametrize("report", ["text", "json", "csv", "markdown"])
def test_no_report_format_emits_terminal_escapes(tmp_path, capsys, report):
    """Only the text path was tested. _to_json uses ensure_ascii=False, so a
    dropped _sanitize() there would emit raw ESC into a file a co-author opens."""
    bib = "@article{k, title={\x1b[31mcited\x1b[0m}, doi={10.1/x}}"
    path = write(tmp_path, "m.bib", bib)
    run([path, "--report", report, "--delay", "0", "--verbose"], client=poisoned_client())
    out = capsys.readouterr().out
    assert "\x1b" not in out
    assert "\x07" not in out
    assert "\x9b" not in out  # C1 CSI — a single-byte escape introducer


@pytest.mark.parametrize("report", ["json", "csv", "markdown"])
def test_poisoned_label_is_sanitized_in_every_report(tmp_path, capsys, report):
    """The label comes from the *input* file, not the Crossref record."""
    bib = "@article{ev\x1b[31mil, doi={10.1/x}}"
    path = write(tmp_path, "m.bib", bib)
    run([path, "--report", report, "--delay", "0", "--verbose"], client=poisoned_client())
    assert "\x1b" not in capsys.readouterr().out


def _control_chars(value: str) -> set:
    return {c for c in value if ord(c) < 0x20 and c != "\n"} | {
        c for c in value if 0x7F <= ord(c) <= 0x9F
    }


@pytest.mark.parametrize("field", ["label", "message"])
def test_json_report_values_contain_no_control_chars_after_parsing(tmp_path, capsys, field):
    """The assertion has to be on the PARSED value, not the raw bytes.

    json.dumps escapes control chars to "\\u001b" regardless of ensure_ascii, so
    the report file looks clean either way — but a consumer doing
    `jq -r '.[].findings[].message'` decodes that back into a real ESC and prints
    it straight to a terminal. Sanitizing is what makes the *data* safe; checking
    stdout for raw \\x1b bytes cannot tell the two apart.
    """
    bib = "@article{ev\x1b[31mil, title={\x1b[31mcited}, doi={10.1/x}}"
    path = write(tmp_path, "m.bib", bib)
    run([path, "--json", "--delay", "0", "--verbose"], client=poisoned_client())
    payload = json.loads(capsys.readouterr().out)  # must still be valid JSON
    values = [payload[0][field]] if field == "label" else [
        f["message"] for f in payload[0]["findings"]
    ]
    for value in values:
        assert _control_chars(value) == set(), f"control chars survived in {field}"


def test_json_report_keeps_the_legible_text(tmp_path, capsys):
    """Sanitizing must remove only the controls, not gut the message."""
    path = write(tmp_path, "m.bib", "@article{k, title={cited}, doi={10.1/x}}")
    run([path, "--json", "--delay", "0", "--verbose"], client=poisoned_client())
    blob = json.dumps(json.loads(capsys.readouterr().out))
    assert "FAKE" in blob and "broken" in blob


def test_markdown_report_escapes_pipes_so_the_table_survives(tmp_path, capsys):
    """A title containing '|' would otherwise break out of its table cell."""
    path = write(tmp_path, "m.bib", "@article{k, doi={10.1/x}}")
    run([path, "--report", "markdown", "--delay", "0", "--verbose"], client=poisoned_client())
    out = capsys.readouterr().out
    for line in out.splitlines():
        if line.startswith("| ") and "broken" in line:
            assert "\\|" in line  # the raw pipe was escaped, not left live


# --- 2. _find_paren_entry_end: paren entries vs braces ----------------------


def test_balanced_parens_inside_braces_survive_a_paren_entry():
    refs = parse_references("@article(k, title={Aspirin (low dose)}, doi={10.1/x})")
    assert len(refs) == 1
    assert refs[0].doi == "10.1/x"
    assert refs[0].title == "Aspirin (low dose)"


def test_an_unbalanced_close_paren_inside_braces_does_not_truncate_the_entry():
    """The case that actually proves the brace_depth guard.

    *Balanced* inner parens cancel out and pass even without the guard, so they
    prove nothing. A lone ')' inside a braced value is what closes the entry
    early — silently dropping the DOI that follows it.
    """
    refs = parse_references("@article(k, title={Dose was 5 mg) tapered}, doi={10.1/x})")
    assert len(refs) == 1
    assert refs[0].doi == "10.1/x"  # None if the guard is removed
    assert refs[0].title == "Dose was 5 mg) tapered"


def test_an_unbalanced_open_paren_inside_braces_does_not_swallow_the_entry():
    """The mirror case: a lone '(' leaves depth unbalanced, so the entry never
    closes and the whole reference is dropped as unterminated."""
    text = "@article(k, title={A study (part 1 of 2}, doi={10.1/x})\n@article{k2, doi={10.1/y}}"
    refs = parse_references(text)
    assert [r.doi for r in refs] == ["10.1/x", "10.1/y"]
    assert count_malformed_entries(text) == 0


def test_nested_parens_inside_braces_survive():
    refs = parse_references("@article(k, title={A ((very)) odd (title)}, doi={10.1/x})")
    assert refs[0].doi == "10.1/x"
    assert refs[0].title == "A ((very)) odd (title)"


def test_unbalanced_closing_brace_does_not_break_the_paren_scanner():
    """Kills the mutant removing `max(0, ...)`: a stray '}' would drive
    brace_depth negative, permanently disabling the paren counting after it."""
    refs = parse_references("@article(k, title={odd} } brace, doi={10.1/x})\n@article{k2, doi={10.1/y}}")
    assert {r.doi for r in refs} == {"10.1/x", "10.1/y"}


def test_a_paren_entry_does_not_swallow_the_entry_after_it():
    text = "@article(a, doi={10.1/a})\n@article(b, doi={10.1/b})\n"
    assert [r.doi for r in parse_references(text)] == ["10.1/a", "10.1/b"]


def test_brace_entry_with_parens_in_the_doi_is_unaffected():
    refs = parse_references("@article{k, doi={10.1016/S0140-6736(97)11096-0}}")
    assert refs[0].doi == "10.1016/s0140-6736(97)11096-0"


# --- 3. _csv_safe sanitizes BEFORE the formula check ------------------------


def test_csv_safe_strips_controls_not_just_formulas():
    """Kills the mutant deleting `value = _sanitize(value)`."""
    assert "\x1b" not in _csv_safe("a\x1b[31mb")
    assert _csv_safe("\x07bell") == "bell"


def test_csv_safe_neutralises_every_formula_lead():
    for lead in "=+-@":
        assert _csv_safe(lead + "cmd").startswith("'")


def test_csv_safe_catches_a_formula_hidden_behind_a_control_char():
    """Sanitizing must happen FIRST: "\\x1b=cmd" would otherwise slip past the
    formula check (its first char is ESC, not '='), then lose the ESC downstream
    and reach the spreadsheet as a live formula."""
    assert _csv_safe("\x1b=1+1").startswith("'")


def test_csv_safe_leaves_ordinary_text_alone():
    assert _csv_safe("Journal of Medicine") == "Journal of Medicine"


def test_tab_led_cell_is_quoted():
    assert _csv_safe("\tcmd").startswith("'")


def test_csv_report_neutralises_a_poisoned_container(tmp_path, capsys):
    """End to end: the '=HYPERLINK(...)' container must not reach Excel live."""
    path = write(tmp_path, "m.bib", "@article{k, doi={10.1/x}, journal={=HYPERLINK(\"http://evil\")}}")
    run([path, "--report", "csv", "--delay", "0", "--verbose"], client=poisoned_client())
    out = capsys.readouterr().out
    rows = list(csv.reader(io.StringIO(out)))
    journal_col = rows[0].index("journal")
    assert rows[1][journal_col].startswith("'")


# --- 4. @string / @comment / @preamble are not references -------------------


# NOTE: `@string{jl = {...}}` has no comma after the key, so _ENTRY_RE never
# matches it at all — the _NON_REFERENCE_TYPES filter is not what saves it, and a
# fixture built on @string proves nothing. `@comment{a, b}` DOES match the entry
# regex (the comma is there), so it is the case that actually exercises the
# filter. Getting this wrong is how the filter stayed untested.
@pytest.mark.parametrize("kind", ["comment", "COMMENT", "Comment", "preamble", "string"])
def test_non_reference_entry_types_are_not_parsed_as_references(kind):
    text = "@%s{some, text here}\n@article{k, doi={10.1/x}}" % kind
    refs = parse_references(text)
    assert [r.key for r in refs] == ["k"]


@pytest.mark.parametrize("kind", ["comment", "preamble", "string", "COMMENT"])
def test_unterminated_non_reference_entry_is_not_reported_as_malformed(kind):
    """Kills the mutant dropping the filter from count_malformed_entries: an
    unterminated @comment would print a bogus "references after it may have been
    missed" warning at the CLI, about a thing that is not a reference."""
    assert count_malformed_entries("@%s{unterminated, text" % kind) == 0


def test_a_comment_entry_really_does_reach_the_entry_scanner():
    """Guards the test above from rotting into a no-op: if _ENTRY_RE stopped
    matching @comment at all, the filter test would pass vacuously."""
    from citecheck.parsers import _scan_entries

    scanned = _scan_entries("@comment{a, b}")
    assert [t for t, _k, _b in scanned] == ["comment"]


def test_unterminated_real_entry_is_still_reported_as_malformed():
    assert count_malformed_entries("@article{k, title={oops") == 1


def test_a_bogus_malformed_warning_is_not_printed_for_a_comment_entry(tmp_path, capsys):
    bib = "@comment{a, some note}\n@article{k, doi={10.1/x}}"
    path = write(tmp_path, "m.bib", bib)
    run([path, "--delay", "0"], client=poisoned_client())
    assert "malformed" not in capsys.readouterr().err


# --- 5. every _UPDATE_KINDS key and alias, table-driven ---------------------

# The full table Crossref actually emits. Aliases are the exact mechanism that
# hid the round-1 bug (a name nothing tested), so every key gets a case.
ALL_UPDATE_KINDS = [
    ("expression_of_concern", ERROR, "expression-of-concern"),
    ("withdrawal", ERROR, "withdrawal"),
    ("removal", ERROR, "removal"),
    ("correction", WARNING, "correction"),
    ("erratum", WARNING, "correction"),
    ("corrigendum", WARNING, "correction"),
    ("addendum", WARNING, "addendum"),
    ("clarification", WARNING, "clarification"),
    ("new_edition", WARNING, "new-edition"),
    ("new_version", WARNING, "new-edition"),
    # Low-volume real spellings folded by _UPDATE_ALIASES.
    ("err", WARNING, "correction"),
    ("corrected", WARNING, "correction"),
    ("corrected-article", WARNING, "correction"),
]


@pytest.mark.parametrize("raw, severity, code", ALL_UPDATE_KINDS)
def test_every_real_update_type_is_classified(raw, severity, code):
    msg = {"DOI": "10.1/x", "title": ["A paper"], "updated-by": [{"type": raw}]}
    kinds = _classify_updates(msg)
    assert len(kinds) == 1, f"{raw!r} was not classified at all"
    _label, got_severity, _doi, got_code = kinds[0]
    assert (got_severity, got_code) == (severity, code)


@pytest.mark.parametrize("raw, _sev, _code", ALL_UPDATE_KINDS)
def test_no_real_update_type_is_mistaken_for_a_retraction(raw, _sev, _code):
    msg = {"DOI": "10.1/x", "title": ["A paper"], "updated-by": [{"type": raw}]}
    assert _is_retracted(msg) is False


@pytest.mark.parametrize("alias, canonical", [
    ("err", "erratum"),
    ("corrected", "correction"),
    ("corrected-article", "correction"),
    ("corrected_article", "correction"),
])
def test_aliases_fold_to_their_canonical_type(alias, canonical):
    assert _normalize_update_type(alias) == canonical


def test_erratum_and_corrigendum_are_reported_once_not_twice():
    """They dedupe by CODE, not raw type: one thing happened to the paper."""
    msg = {
        "DOI": "10.1/x",
        "title": ["A paper"],
        "updated-by": [{"type": "erratum"}, {"type": "corrigendum"}, {"type": "correction"}],
    }
    assert len(_classify_updates(msg)) == 1


@pytest.mark.parametrize("raw", ["retraction", "partial_retraction", "Retraction"])
def test_retraction_flavoured_types_stay_out_of_the_update_classifier(raw):
    msg = {"DOI": "10.1/x", "title": ["A paper"], "updated-by": [{"type": raw}]}
    assert _classify_updates(msg) == []
    assert _is_retracted(msg) is True


# --- each update kind must resolve ITS OWN notice DOI ------------------------


def test_each_update_kind_gets_its_own_notice_doi():
    """`_classify_updates` builds a closure per kind to look the notice up.
    A late-binding slip (`lambda k: k == kind` instead of `want=kind`) would make
    every kind resolve against the LAST kind seen — so the expression of concern
    would be reported with the correction's notice DOI."""
    msg = {
        "DOI": "10.1/paper",
        "title": ["A paper"],
        "updated-by": [
            {"type": "expression_of_concern", "DOI": "10.1/eoc"},
            {"type": "correction", "DOI": "10.1/corr"},
            {"type": "addendum", "DOI": "10.1/add"},
        ],
    }
    got = {code: doi for _label, _sev, doi, code in _classify_updates(msg)}
    assert got == {
        "expression-of-concern": "10.1/eoc",
        "correction": "10.1/corr",
        "addendum": "10.1/add",
    }


def test_a_kind_whose_only_entry_self_references_reports_no_notice():
    msg = {
        "DOI": "10.1/paper",
        "title": ["A paper"],
        "updated-by": [
            {"type": "expression_of_concern", "DOI": "10.1/paper"},  # self
            {"type": "correction", "DOI": "10.1/corr"},              # real
        ],
    }
    got = {code: doi for _label, _sev, doi, code in _classify_updates(msg)}
    assert got == {"expression-of-concern": None, "correction": "10.1/corr"}
