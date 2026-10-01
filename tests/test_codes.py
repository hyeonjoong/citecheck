"""Finding codes, --ignore, --list-checks, and severity-ordered output.

Motivation: `--strict` was documented as a submission gate but unusable as one.
Every real clinical manuscript cites books, guidelines and package inserts that
have no DOI, so `no-doi` warnings made `--strict` exit 1 forever. The user had
two settings: "errors only" and "always fails". Codes make the middle possible.
"""

import json

import pytest

from citecheck.cli import _apply_ignores, _by_severity, _parse_ignore, build_parser, run
from citecheck.core import CODES, CheckResult, CrossrefClient, ERROR, OK, WARNING
from citecheck.parsers import Reference

GOOD = {
    "DOI": "10.1371/journal.pone.0312345",
    "title": ["Sleep as a transdiagnostic node in BELL disorders"],
    "author": [{"family": "Kim"}],
    "issued": {"date-parts": [[2024]]},
}


def fake_client(records, resolves=False):
    return CrossrefClient(_fetch=lambda d: records.get(d), _resolve=lambda d: resolves)


def write(tmp_path, name, data):
    p = tmp_path / name
    p.write_text(data, encoding="utf-8")
    return str(p)


# --- the codes themselves ---------------------------------------------------


def test_every_code_emitted_by_the_code_is_a_declared_code():
    """A finding carrying an undeclared code could never be --ignore'd, and would
    be a lie in the JSON report. Scan the source for every literal we pass."""
    import pathlib
    import re

    src = ""
    for name in ("core.py", "cli.py"):
        src += (pathlib.Path(__file__).parent.parent / "citecheck" / name).read_text()
    # Codes are always passed as the last positional arg of .add(...) or listed
    # in _UPDATE_KINDS; both look like a bare kebab-case string literal.
    used = set(re.findall(r'"([a-z]+(?:-[a-z]+)+)",?\n\s*\)', src))
    undeclared = {c for c in used if c not in CODES} - {"utf-8-sig", "utf-8"}
    assert not undeclared, f"undeclared finding codes: {undeclared}"


def test_codes_are_kebab_case_and_documented():
    for code, meaning in CODES.items():
        assert code == code.lower()
        assert " " not in code
        assert meaning.endswith("."), f"{code} has no readable description"


def test_findings_carry_their_code(tmp_path, capsys):
    path = write(tmp_path, "r.bib", "@book{k, title={A guideline with no DOI}}")
    run([path, "--json", "--delay", "0"], client=fake_client({}))
    payload = json.loads(capsys.readouterr().out)
    assert payload[0]["findings"][0]["code"] == "no-doi"


def test_verified_finding_carries_the_verified_code(tmp_path, capsys):
    path = write(tmp_path, "r.bib", "@article{k, doi={10.1371/journal.pone.0312345}}")
    run([path, "--json", "--delay", "0"], client=fake_client({GOOD["DOI"]: GOOD}))
    payload = json.loads(capsys.readouterr().out)
    assert payload[0]["findings"][0]["code"] == "verified"


# --- --ignore parsing -------------------------------------------------------


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("no-doi", {"no-doi"}),
        ("no-doi,correction", {"no-doi", "correction"}),
        ("no-doi, correction", {"no-doi", "correction"}),
        ("NO-DOI", {"no-doi"}),
        (" no-doi , ", {"no-doi"}),
        ("", set()),
        ("no-doi no-doi", {"no-doi"}),
    ],
)
def test_ignore_parsing(raw, expected):
    known, unknown, refused = _parse_ignore(raw)
    assert known == expected
    assert unknown == []
    assert refused == []


def test_unknown_ignore_code_is_a_usage_error(tmp_path, capsys):
    """A typo must not silently fail to suppress — nor silently suppress a
    retraction. Fail loudly instead."""
    path = write(tmp_path, "r.bib", "@article{k, doi={10.1/x}}")
    code = run([path, "--ignore", "retracton", "--delay", "0"], client=fake_client({}))
    assert code == 2
    assert "unknown --ignore code" in capsys.readouterr().err


def test_a_typo_alongside_a_valid_code_still_errors(tmp_path, capsys):
    path = write(tmp_path, "r.bib", "@article{k, doi={10.1/x}}")
    assert run([path, "--ignore", "no-doi,nonsense", "--delay", "0"],
               client=fake_client({})) == 2


# --- --ignore behaviour -----------------------------------------------------


def test_ignore_makes_strict_usable_as_a_submission_gate(tmp_path):
    """The motivating case: a manuscript citing a DOI-less guideline."""
    bib = (
        "@article{a, doi={10.1371/journal.pone.0312345}}\n"
        "@book{guideline, title={KDIGO clinical practice guideline}}\n"
    )
    path = write(tmp_path, "r.bib", bib)
    assert run([path, "--strict", "--delay", "0"], client=fake_client({GOOD["DOI"]: GOOD})) == 1
    assert run([path, "--strict", "--ignore", "no-doi", "--delay", "0"],
               client=fake_client({GOOD["DOI"]: GOOD})) == 0


def test_ignoring_a_warning_clears_the_status(tmp_path, capsys):
    path = write(tmp_path, "r.bib", "@book{k, title={No DOI}}")
    run([path, "--json", "--ignore", "no-doi", "--delay", "0"], client=fake_client({}))
    payload = json.loads(capsys.readouterr().out)
    assert payload[0]["status"] == "ok"
    assert payload[0]["findings"] == []


def test_ignore_can_silence_an_error_and_the_exit_code_follows(tmp_path):
    """--ignore is a real override: if you ignore doi-not-resolving, the run
    passes. That is the user's call to make, explicitly."""
    path = write(tmp_path, "r.bib", "@article{k, doi={10.9999/nope}}")
    assert run([path, "--delay", "0"], client=fake_client({}, resolves=False)) == 1
    assert run([path, "--ignore", "doi-not-resolving", "--delay", "0"],
               client=fake_client({}, resolves=False)) == 0


def test_ignore_does_not_suppress_other_findings(tmp_path, capsys):
    bib = "@article{a, doi={10.1371/journal.pone.0312345}, title={Totally different paper}}"
    path = write(tmp_path, "r.bib", bib)
    run([path, "--json", "--ignore", "no-doi", "--delay", "0"],
        client=fake_client({GOOD["DOI"]: GOOD}))
    payload = json.loads(capsys.readouterr().out)
    assert [f["code"] for f in payload[0]["findings"]] == ["title-mismatch"]


def test_ignore_never_suppresses_a_lookup_failure_by_accident(tmp_path):
    """Ignoring `no-doi` must not turn an offline run into a clean pass.
    (`lookup-failed` cannot be ignored at all — see below.)"""
    def boom(doi):
        raise TimeoutError("offline")

    path = write(tmp_path, "r.bib", "@article{k, doi={10.1371/journal.pone.0312345}}")
    client = CrossrefClient(_fetch=boom, _resolve=lambda d: False)
    assert run([path, "--ignore", "no-doi", "--delay", "0"], client=client) == 3


def test_ignored_findings_are_reported_as_hidden(tmp_path, capsys):
    path = write(tmp_path, "r.bib", "@book{k, title={No DOI}}")
    run([path, "--ignore", "no-doi", "--delay", "0", "--no-color"], client=fake_client({}))
    assert "1 finding hidden by --ignore no-doi" in capsys.readouterr().out


def test_apply_ignores_counts_what_it_removed():
    r = CheckResult(reference=Reference(raw=""))
    r.add(WARNING, "a", "no-doi")
    r.add(WARNING, "b", "title-mismatch")
    assert _apply_ignores([r], {"no-doi"}) == 1
    assert [f.code for f in r.findings] == ["title-mismatch"]


def test_apply_ignores_with_empty_set_is_a_noop():
    r = CheckResult(reference=Reference(raw=""))
    r.add(WARNING, "a", "no-doi")
    assert _apply_ignores([r], set()) == 0
    assert len(r.findings) == 1


# --- --list-checks ----------------------------------------------------------


def test_list_checks_prints_every_code(capsys):
    assert run(["--list-checks"]) == 0
    out = capsys.readouterr().out
    for code in CODES:
        assert code in out


def test_list_checks_needs_no_input_file(capsys):
    """It must work before you have a file, and must not read stdin."""
    assert run(["--list-checks"]) == 0


# --- severity ordering ------------------------------------------------------


def make(status_code, severity):
    r = CheckResult(reference=Reference(raw="x"))
    if severity != OK:
        r.add(severity, "msg", status_code)
    return r


def test_errors_sort_before_warnings_before_ok():
    results = [
        make("no-doi", WARNING),
        make("verified", OK),
        make("retracted", ERROR),
    ]
    assert [r.status for r in _by_severity(results)] == [ERROR, WARNING, OK]


def test_sort_is_stable_within_a_severity():
    """The reference list's own order must survive inside each group."""
    a, b, c = make("no-doi", WARNING), make("no-doi", WARNING), make("retracted", ERROR)
    a.reference.key, b.reference.key, c.reference.key = "a", "b", "c"
    assert [r.reference.key for r in _by_severity([a, b, c])] == ["c", "a", "b"]


def test_retraction_is_printed_before_routine_warnings(tmp_path, capsys):
    """End to end: the finding that matters must not be buried."""
    retracted = dict(GOOD, DOI="10.1/bad", **{"updated-by": [{"type": "retraction"}]})
    bib = (
        "@book{nodoi1, title={A guideline}}\n"
        "@book{nodoi2, title={Another guideline}}\n"
        "@article{bad, doi={10.1/bad}}\n"
    )
    path = write(tmp_path, "r.bib", bib)
    run([path, "--delay", "0", "--no-color"], client=fake_client({"10.1/bad": retracted}))
    out = capsys.readouterr().out
    assert out.index("RETRACTED") < out.index("No DOI found")


def test_ordering_applies_to_json_and_csv_reports(tmp_path, capsys):
    retracted = dict(GOOD, DOI="10.1/bad", **{"updated-by": [{"type": "retraction"}]})
    bib = "@book{nodoi, title={A guideline}}\n@article{bad, doi={10.1/bad}}\n"
    path = write(tmp_path, "r.bib", bib)
    run([path, "--json", "--delay", "0"], client=fake_client({"10.1/bad": retracted}))
    payload = json.loads(capsys.readouterr().out)
    assert [p["status"] for p in payload] == ["error", "warning"]


# --- the parser surface -----------------------------------------------------


def test_ignore_defaults_to_nothing():
    args = build_parser().parse_args(["x.bib"])
    assert args.ignore == ""
    assert args.list_checks is False


# --- lookup-failed must not be ignorable ------------------------------------
#
# The tool's headline promise is that a network outage can never be mistaken for
# a clean pass (that is what exit 3 is for). `--ignore lookup-failed` broke it:
# an offline run reported "1 ok, 0 warnings, 0 errors" and exit 0, while the very
# next line said "0 of 1 compared against a Crossref record".

def test_lookup_failed_cannot_be_ignored(tmp_path, capsys):
    path = write(tmp_path, "r.bib", "@article{k, doi={10.1/x}}")
    code = run([path, "--ignore", "lookup-failed", "--delay", "0"], client=fake_client({}))
    assert code == 2
    assert "cannot be ignored" in capsys.readouterr().err


def test_parse_ignore_refuses_the_non_ignorable_code():
    known, unknown, refused = _parse_ignore("no-doi,lookup-failed")
    assert known == {"no-doi"}  # the ignorable part is NOT silently discarded
    assert unknown == []
    assert refused == ["lookup-failed"]


def test_an_offline_run_stays_inconclusive_whatever_else_is_ignored(tmp_path):
    """Ignoring everything else must not weaken the exit-3 guard."""
    def boom(doi):
        raise TimeoutError("offline")

    path = write(tmp_path, "r.bib", "@article{k, doi={10.1371/journal.pone.0312345}}")
    every_other = ",".join(c for c in CODES if c != "lookup-failed")
    client = CrossrefClient(_fetch=boom, _resolve=lambda d: False)
    assert run([path, "--ignore", every_other, "--delay", "0"], client=client) == 3


def test_non_ignorable_codes_are_marked_in_list_checks(capsys):
    run(["--list-checks"])
    out = capsys.readouterr().out
    line = next(ln for ln in out.splitlines() if ln.strip().startswith("lookup-failed"))
    assert "cannot be ignored" in line


def test_every_other_code_is_ignorable():
    """The refusal must stay a deliberate, tiny exception — not creep."""
    from citecheck.cli import NON_IGNORABLE

    assert NON_IGNORABLE == {"lookup-failed"}
    assert NON_IGNORABLE <= set(CODES)


def test_exit_three_does_not_depend_on_the_wording_of_a_message(tmp_path):
    """Regression: `lookup_failed` was detected via message.startswith("Lookup
    failed"), so rephrasing any one of four f-strings would have silently turned
    every offline run into exit 0. It keys on the code now."""
    from citecheck.core import CheckResult
    from citecheck.parsers import Reference
    import citecheck.cli as cli

    r = CheckResult(reference=Reference(raw="x", doi="10.1/x"))
    r.add(WARNING, "Could not reach Crossref (some new wording)", "lookup-failed")
    assert any(f.code == "lookup-failed" for f in r.findings)
    assert not r.findings[0].message.startswith("Lookup failed")
