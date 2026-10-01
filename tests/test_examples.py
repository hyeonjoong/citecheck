"""The shipped example files must actually work.

`실행.command` runs `examples/sample.bib` on a double-click, and README/사용법.md
point at these files — a broken example is a broken first impression, and nothing
else in the suite touches them. These tests are offline: they check the files
parse into the references the docs claim, not what Crossref says about them.
"""

import pathlib

import pytest

from citecheck.parsers import detect_format, parse_references

EXAMPLES = pathlib.Path(__file__).resolve().parent.parent / "examples"

EXPECTED_FORMAT = {
    "sample.bib": "bibtex",
    "sample.ris": "ris",
    "sample.json": "csljson",
    "sample.csv": "csv",
}
# Examples that demonstrate one feature with a single reference, so the
# "good and bad case" rule below does not apply to them.
DEMO_FORMAT = {
    "suggest.bib": "bibtex",
}


def read(name):
    return (EXAMPLES / name).read_text(encoding="utf-8")


def test_every_shipped_example_is_covered_here():
    """A new example file must not silently escape these checks."""
    # Dotfiles are skipped: Finder drops a .DS_Store into any folder it opens.
    shipped = {p.name for p in EXAMPLES.iterdir() if p.is_file() and not p.name.startswith(".")}
    assert shipped == set(EXPECTED_FORMAT) | set(DEMO_FORMAT)


@pytest.mark.parametrize("name, fmt", sorted({**EXPECTED_FORMAT, **DEMO_FORMAT}.items()))
def test_example_autodetects_as_its_format(name, fmt):
    assert detect_format(read(name)) == fmt


@pytest.mark.parametrize("name", sorted(EXPECTED_FORMAT))
def test_example_parses_into_usable_references(name):
    refs = parse_references(read(name))
    assert len(refs) >= 2, "an example must show at least a good and a bad case"
    assert any(r.doi for r in refs), "at least one reference must carry a DOI"
    # Every reference must be labelable — the report prints this for each row.
    assert all(r.label() for r in refs)


@pytest.mark.parametrize("name", sorted({**EXPECTED_FORMAT, **DEMO_FORMAT}))
def test_example_is_valid_utf8(name):
    (EXAMPLES / name).read_bytes().decode("utf-8")


def test_csv_example_exercises_the_column_matching_it_advertises():
    """The README claims columns match by name, DOI-as-URL is normalised, and a
    missing DOI still yields a usable reference. Pin all three."""
    refs = {r.key: r for r in parse_references(read("sample.csv"))}
    assert set(refs) == {"S1", "S2", "S3", "S4"}
    # "Article DOI" / "Study ID" / "Authors" headers matched by alias.
    assert refs["S1"].doi == "10.1371/journal.pmed.0020124"
    assert refs["S1"].author == "Ioannidis"  # "Ioannidis JPA" -> surname
    assert refs["S1"].year == 2005
    assert refs["S1"].journal == "PLoS Medicine"
    # A DOI written as a full URL is normalised.
    assert refs["S3"].doi == "10.1371/journal.pmed.1000097"
    # A row with no DOI still parses, and keeps its PMID for --pubmed.
    assert refs["S4"].doi is None
    # PMID 20332509 is the BMJ copy of CONSORT 2010 (DOI 10.1136/bmj.c332), the
    # journal the row names. 20334633 is the BMC Medicine copy.
    assert refs["S4"].journal == "BMJ"
    assert refs["S4"].pmid == "20332509"


def test_bib_example_still_demonstrates_a_broken_doi():
    """README's sample output shows a `broken_doi` entry — keep it honest."""
    refs = parse_references(read("sample.bib"))
    assert any(r.doi == "10.9999/does.not.exist" for r in refs)


# --- the docs must describe the examples they actually ship ------------------
#
# The README's example block sat stale for a long time: it claimed
# "✓ ioannidis2005 / Verified: Ioannidis (2005)" long after PLoS Medicine issued
# a correction against that paper, and its totals were wrong too. Nothing caught
# it because nothing compared the docs to the files. These tests do — offline, by
# structure, so they stay honest without asserting on live Crossref data.

ROOT = EXAMPLES.parent
README = (ROOT / "README.md").read_text(encoding="utf-8")
USAGE_KO = (ROOT / "사용법.md").read_text(encoding="utf-8")
RUNNER = (ROOT / "실행.command").read_text(encoding="utf-8")


def test_readme_example_block_names_the_real_cite_keys():
    """Every `✓/!/✗ <label>` line in the README's example must be a real entry."""
    import re

    keys = {r.key for r in parse_references(read("sample.bib"))}
    shown = set(re.findall(r"^[✓!✗] (\w+)$", README, re.MULTILINE))
    assert shown, "the README example block disappeared — update this test"
    assert shown <= keys, f"README shows entries that aren't in sample.bib: {shown - keys}"
    assert shown == keys, f"README example omits entries: {keys - shown}"


def test_readme_example_totals_match_the_entry_count():
    import re

    n = len(parse_references(read("sample.bib")))
    m = re.search(r"checked (\d+) references: (\d+) ok, (\d+) warnings, (\d+) errors", README)
    assert m, "the README's summary line is gone — update this test"
    total, ok, warn, err = (int(g) for g in m.groups())
    assert total == n, f"README says {total} references; sample.bib has {n}"
    assert ok + warn + err == total, "the README's own severity counts don't sum"


def test_readme_example_bucket_line_sums():
    import re

    m = re.search(r"\((\d+) of (\d+) compared against a Crossref record; (\d+) could not be", README)
    assert m
    checked, total, not_checked = (int(g) for g in m.groups())
    assert checked + not_checked == total


def test_every_file_the_docs_tell_you_to_run_exists():
    import re

    named = set()
    for doc in (README, USAGE_KO, RUNNER):
        named |= set(re.findall(r"examples/[\w.\-]+", doc))
    assert named, "no example file is referenced by any doc"
    for rel in named:
        assert (ROOT / rel).exists(), f"docs reference a missing file: {rel}"


def test_the_runner_script_runs_a_file_that_exists():
    """실행.command is the double-click entry point 사용법.md calls the easiest way in."""
    import re

    m = re.findall(r"citecheck (examples/[\w.\-]+)", RUNNER)
    assert m, "실행.command no longer runs an example"
    for rel in m:
        assert (ROOT / rel).exists()


def test_docs_do_not_mention_the_phantom_crossref_field():
    """`update-by` is not a Crossref field. It was the bug; the README documented
    it as the design for three rounds. Never again."""
    for name, doc in (("README.md", README), ("사용법.md", USAGE_KO)):
        assert "update-by" not in doc, f"{name} names the non-existent field 'update-by'"


def test_format_lists_in_docs_include_every_supported_format():
    from citecheck.parsers import _PARSERS

    for fmt in _PARSERS:
        assert fmt in README, f"README's format docs omit {fmt!r}"


def test_hardening_log_test_count_is_not_stale():
    """HARDENING.md states a test count for the latest round. A number in a doc
    that nothing checks is a number that drifts — this file's whole subject is
    claims that outlived their truth."""
    import re

    log = (ROOT / "HARDENING.md").read_text(encoding="utf-8")
    # Older rounds wrote the label in bold, newer ones in plain text.
    claimed = [int(n) for n in re.findall(r"(?:\*\*After:\*\*|After:) (\d+) tests", log)]
    assert claimed, "no round states a final test count"
    # The latest round's claim must match what the suite actually collects.
    collected = 0
    for path in (ROOT / "tests").glob("test_*.py"):
        collected += len(re.findall(r"^def test_", path.read_text(encoding="utf-8"), re.M))
    assert max(claimed) >= collected, (
        f"HARDENING.md's latest count ({max(claimed)}) is below the "
        f"{collected} test functions now present — it has gone stale"
    )


# --- the --suggest-doi README block is real output ----------------------------
#
# It once showed a made-up match for a DOI that belongs to an unrelated paper.
# The block is now replayed here, offline, with the Crossref search answer
# trimmed from the live response for examples/suggest.bib (fetched 2026-10-01).

IOANNIDIS_SEARCH = [
    {
        "DOI": "10.1371/journal.pmed.0020124",
        "type": "journal-article",
        "title": ["Why Most Published Research Findings Are False"],
        "author": [{"given": "John P. A.", "family": "Ioannidis"}],
        "issued": {"date-parts": [[2005, 8, 30]]},
        "container-title": ["PLoS Medicine"],
        "updated-by": [{"DOI": "10.1371/journal.pmed.1004085", "type": "correction",
                        "label": "Correction", "source": "publisher"}],
    },
    {
        "DOI": "10.1371/journal.pmed.1004085",
        "type": "journal-article",
        "title": ["Correction: Why Most Published Research Findings Are False"],
        "author": [{"given": "John P. A.", "family": "Ioannidis"}],
        "issued": {"date-parts": [[2022, 8, 25]]},
        "container-title": ["PLOS Medicine"],
    },
    {
        "DOI": "10.1080/09332480.2005.10722754",
        "type": "journal-article",
        "title": ["Why Most Published Research Findings Are False"],
        "author": [{"given": "John P. A.", "family": "Ioannidis"}],
        "issued": {"date-parts": [[2005, 9]]},
        "container-title": ["CHANCE"],
    },
]


def _readme_block(command):
    """The output lines the README shows under `$ <command>`."""
    lines = README.splitlines()
    start = lines.index("$ " + command)
    end = lines.index("```", start)
    block = lines[start + 1 : end]
    while block and not block[0].strip():
        block.pop(0)
    return block


def test_readme_suggest_doi_block_is_what_the_tool_prints(monkeypatch, capsys):
    from citecheck.cli import run
    from citecheck.core import CrossrefClient

    command = "python3 -m citecheck examples/suggest.bib --suggest-doi --verbose --no-color"
    shown = _readme_block(command)
    client = CrossrefClient(
        _fetch=lambda doi: None, _resolve=lambda doi: False,
        _search=lambda query: IOANNIDIS_SEARCH,
    )
    monkeypatch.chdir(ROOT)
    code = run(command.split()[3:], client=client)
    assert capsys.readouterr().out.splitlines() == shown
    assert code == 0
