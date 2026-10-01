"""CLI-level tests. All use an injected client so no network is touched."""

import json

import pytest

from citecheck.cli import run, _sanitize, _decode
from citecheck.core import CrossrefClient

GOOD = {
    "DOI": "10.1371/journal.pone.0312345",
    "title": ["Sleep as a transdiagnostic node in BELL disorders"],
    "author": [{"family": "Kim"}],
    "issued": {"date-parts": [[2024, 5, 1]]},
    "type": "journal-article",
}


def fake_client(records, resolves=False):
    return CrossrefClient(_fetch=lambda d: records.get(d), _resolve=lambda d: resolves)


def write(tmp_path, name, data, encoding="utf-8"):
    p = tmp_path / name
    if isinstance(data, bytes):
        p.write_bytes(data)
    else:
        p.write_text(data, encoding=encoding)
    return str(p)


# --- exit-code contract -----------------------------------------------------

def test_exit_zero_when_all_ok(tmp_path, capsys):
    path = write(tmp_path, "r.bib", "@article{k, doi={10.1371/journal.pone.0312345}}")
    code = run([path, "--delay", "0"], client=fake_client({"10.1371/journal.pone.0312345": GOOD}))
    assert code == 0


def test_exit_one_on_error(tmp_path):
    path = write(tmp_path, "r.bib", "@article{k, doi={10.9999/nope}}")
    code = run([path, "--delay", "0"], client=fake_client({}, resolves=False))
    assert code == 1


def test_exit_two_on_empty_input(tmp_path):
    path = write(tmp_path, "empty.bib", "   \n  \n")
    code = run([path, "--delay", "0"], client=fake_client({}))
    assert code == 2


def test_exit_two_on_unreadable(tmp_path):
    code = run([str(tmp_path / "missing.bib"), "--delay", "0"], client=fake_client({}))
    assert code == 2


def test_strict_promotes_warning_to_failure(tmp_path):
    # No-DOI reference => WARNING. Normal exit 0, strict exit 1.
    path = write(tmp_path, "r.bib", "@book{k, title={No DOI here}}")
    assert run([path, "--delay", "0"], client=fake_client({})) == 0
    assert run([path, "--delay", "0", "--strict"], client=fake_client({})) == 1


# --- non-UTF-8 input --------------------------------------------------------

def test_non_utf8_file_does_not_crash(tmp_path, capsys):
    data = "@article{k, author={홍길동}, doi={10.1371/journal.pone.0312345}}".encode("cp949")
    path = write(tmp_path, "k.bib", data)
    code = run([path, "--delay", "0"], client=fake_client({"10.1371/journal.pone.0312345": GOOD}))
    assert code == 0
    assert "not UTF-8" in capsys.readouterr().err


def test_decode_helper_fallbacks():
    assert _decode("hello".encode("utf-8")) == ("hello", "utf-8")
    # Assert the decoded TEXT, not merely that some non-utf-8 codec was chosen.
    # Checking only `enc != "utf-8"` passed just as happily when the bytes were
    # mis-decoded into mojibake, which is the only failure that matters here.
    text, enc = _decode("café".encode("latin-1"))
    assert text == "café"
    assert enc != "utf-8"


@pytest.mark.parametrize(
    "name",
    [
        "Deutsches Ärzteblatt",  # a real medical journal
        "Ángel",
        "Müller",
        "Öztürk",
        "Ferreira Gonçalves",
        "Łopez Ñuñez".replace("Ł", "L"),  # Ł is latin-2, not latin-1
        "Kraüter Bäcker Möller",  # several accents in one line
    ],
)
def test_latin1_european_names_are_not_mis_decoded_as_cp949(name):
    """A European reference list must survive decoding intact.

    Regression: cp949 was tried before latin-1 and accepted on "it didn't
    raise". An accented byte followed by an ordinary letter is a legal cp949
    pair, so "Ärzteblatt" decoded cleanly to "훣zteblatt" with only a soft
    stderr note — corrupting author names and journal titles, and producing
    false author-mismatch/journal-mismatch warnings against Crossref.

    Note the corruption was data-dependent ("Müller" survived, because 0xFC 0x6C
    is not a legal cp949 pair), so this is parametrized over several shapes
    rather than trusting one example.
    """
    text, enc = _decode(name.encode("latin-1"))
    assert text == name
    assert enc == "latin-1"


@pytest.mark.parametrize(
    "korean",
    [
        "수면 장애 연구",
        "@article{k, author={홍길동}, title={불면증 인지행동치료}}",
        "김철수. 불면증. 대한수면의학회지. 2024.",
    ],
)
def test_cp949_korean_input_still_decodes_as_cp949(korean):
    """The latin-1 fix must not cost the Korean support it was guarding."""
    text, enc = _decode(korean.encode("cp949"))
    assert text == korean
    assert enc == "cp949"


def test_encoding_override_still_forces_cp949():
    """The Hangul-run heuristic is only a *default*; --encoding still wins.

    This is the escape hatch for the rare Korean file whose only Hangul is one
    isolated syllable, which the heuristic deliberately declines to guess at.
    """
    assert _decode("잠".encode("cp949"), override="cp949") == ("잠", "cp949")


def test_european_bib_does_not_produce_a_false_author_mismatch(tmp_path, capsys):
    """End-to-end: the decoding bug's real-world symptom was a bogus finding."""
    data = (
        "@article{k, author={Ärzte, Anna}, journal={Deutsches Ärzteblatt}, "
        "doi={10.1371/journal.pone.0312345}}"
    ).encode("latin-1")
    path = write(tmp_path, "eu.bib", data)
    record = {
        "DOI": "10.1371/journal.pone.0312345",
        "title": ["Ein Titel"],
        "author": [{"family": "Ärzte", "given": "Anna"}],
        "container-title": ["Deutsches Ärzteblatt"],
        "issued": {"date-parts": [[2024]]},
    }
    code = run([path, "--json", "--delay", "0"],
               client=fake_client({"10.1371/journal.pone.0312345": record}))
    payload = json.loads(capsys.readouterr().out)
    codes = [f["code"] for item in payload for f in item["findings"]]
    assert "author-mismatch" not in codes
    assert "journal-mismatch" not in codes
    assert code == 0


# --- JSON output shape ------------------------------------------------------

def test_json_output_shape(tmp_path, capsys):
    path = write(tmp_path, "r.bib", "@article{k, doi={10.9999/nope}}")
    run([path, "--json", "--delay", "0"], client=fake_client({}, resolves=False))
    out = capsys.readouterr().out
    payload = json.loads(out)
    assert isinstance(payload, list) and len(payload) == 1
    item = payload[0]
    assert set(item) == {"label", "doi", "pmid", "journal", "status", "findings"}
    assert item["status"] == "error"
    assert set(item["findings"][0]) == {"severity", "code", "message"}
    assert item["findings"][0]["code"] == "doi-not-resolving"


# --- ANSI / control-char injection ------------------------------------------

def test_ansi_escape_stripped_from_output(tmp_path, capsys):
    malicious = "@article{k, title={Hi\x1b[2J\x1b[31mFAKE}, doi={10.1/x}}"
    path = write(tmp_path, "m.bib", malicious)
    records = {"10.1/x": dict(GOOD, title=["totally different penguin paper"])}
    run([path, "--delay", "0", "--verbose"], client=fake_client(records))
    out = capsys.readouterr().out
    assert "\x1b" not in out  # no raw escape bytes reach the terminal


def test_sanitize_strips_controls():
    assert _sanitize("a\x1b[31mb\x07c") == "a[31mbc"
    assert _sanitize("normal text") == "normal text"


# --- duplicate DOI detection ------------------------------------------------

def test_duplicate_doi_flagged(tmp_path, capsys):
    bib = (
        "@article{a, doi={10.1371/journal.pone.0312345}}\n"
        "@article{b, doi={10.1371/journal.pone.0312345}}"
    )
    path = write(tmp_path, "d.bib", bib)
    run([path, "--json", "--delay", "0"], client=fake_client({"10.1371/journal.pone.0312345": GOOD}))
    payload = json.loads(capsys.readouterr().out)
    assert all(any("Duplicate DOI" in f["message"] for f in item["findings"]) for item in payload)


# --- malformed bibtex reporting ---------------------------------------------

def test_malformed_entry_reported(tmp_path, capsys):
    bib = "@article{good, doi={10.1371/journal.pone.0312345}}\n@article{bad, title={oops"
    path = write(tmp_path, "b.bib", bib)
    run([path, "--delay", "0"], client=fake_client({"10.1371/journal.pone.0312345": GOOD}))
    assert "malformed" in capsys.readouterr().err


# --- decoding: BOM / UTF-16 -------------------------------------------------

def test_decode_utf8_bom_stripped():
    text, enc = _decode("café".encode("utf-8-sig"))
    assert text == "café" and enc == "utf-8-sig"


def test_decode_utf16_bom():
    text, enc = _decode("@article{k}".encode("utf-16"))
    assert "article" in text and enc == "utf-16"


def test_decode_override():
    text, enc = _decode("café".encode("latin-1"), override="latin-1")
    assert text == "café" and enc == "latin-1"


# --- --delay validation -----------------------------------------------------

@pytest.mark.parametrize("bad", ["-1", "nan", "inf", "-inf", "abc"])
def test_delay_rejects_bad_values(tmp_path, bad, capsys):
    path = write(tmp_path, "r.bib", "@article{k, doi={10.1/x}}\n@article{k2, doi={10.1/y}}")
    with pytest.raises(SystemExit) as exc:
        run([path, "--delay", bad], client=fake_client({}))
    assert exc.value.code == 2  # argparse usage error, not a traceback


# --- exit code 3 on lookup failure (offline) --------------------------------

def test_offline_lookup_failure_is_inconclusive(tmp_path):
    def boom(doi):
        raise TimeoutError("offline")

    client = CrossrefClient(_fetch=boom, _resolve=lambda d: False)
    path = write(tmp_path, "r.bib", "@article{k, doi={10.1371/journal.pone.0312345}}")
    code = run([path, "--delay", "0"], client=client)
    assert code == 3  # not 0 — must not read as a clean pass


# --- --strict in JSON mode --------------------------------------------------

def test_strict_json_mode(tmp_path):
    path = write(tmp_path, "r.bib", "@book{k, title={No DOI}}")
    assert run([path, "--json", "--delay", "0"], client=fake_client({})) == 0
    assert run([path, "--json", "--strict", "--delay", "0"], client=fake_client({})) == 1


# --- text-mode heuristic suppression ----------------------------------------

def test_text_mode_suppresses_year_author_false_positives(tmp_path, capsys):
    # "2000 patients" misparses the year; author "Effect" is bogus. With a DOI
    # whose real title IS mentioned in the line, no year/author/title warning.
    record = dict(
        GOOD,
        title=["Effect of aspirin in patients with acute ischaemic stroke"],
        author=[{"family": "Chen"}],
        issued={"date-parts": [[2024]]},
    )
    records = {"10.1371/journal.pmed.0020124": record}
    text = ("Effect of aspirin in 2000 patients with acute ischaemic stroke. "
            "Lancet. 2019. doi:10.1371/journal.pmed.0020124")
    path = write(tmp_path, "refs.txt", text)
    code = run([path, "--format", "text", "--json", "--delay", "0"], client=fake_client(records))
    out = capsys.readouterr().out
    assert "mismatch" not in out.lower()
    assert "wrong doi" not in out.lower()
    assert code == 0


def test_text_mode_catches_swapped_doi_via_title(tmp_path, capsys):
    # A DOI pointing to a *different* real paper: the cited prose won't mention
    # the Crossref title, so text mode flags it even though author/year are off.
    records = {"10.1371/journal.pmed.0020124": dict(GOOD, title=["Penguin foraging in Antarctic waters"])}
    text = "Smith J. A randomized trial of aspirin in stroke patients. Lancet. 2019. doi:10.1371/journal.pmed.0020124"
    path = write(tmp_path, "refs.txt", text)
    run([path, "--format", "text", "--json", "--delay", "0"], client=fake_client(records))
    payload = json.loads(capsys.readouterr().out)
    assert any("wrong DOI" in f["message"] for f in payload[0]["findings"])


def test_text_mode_bare_doi_not_flagged(tmp_path, capsys):
    # A bare-DOI line has no prose — must NOT be flagged as a wrong-title.
    records = {"10.1371/journal.pmed.0020124": dict(GOOD, title=["Anything at all here"])}
    text = "10.1371/journal.pmed.0020124"
    path = write(tmp_path, "refs.txt", text)
    code = run([path, "--format", "text", "--json", "--delay", "0"], client=fake_client(records))
    out = capsys.readouterr().out
    assert "wrong doi" not in out.lower()
    assert code == 0


# --- more end-to-end coverage -----------------------------------------------

def test_unknown_encoding_exit_two(tmp_path, capsys):
    path = write(tmp_path, "r.bib", "@article{k, doi={10.1/x}}")
    code = run([path, "--encoding", "bogus-codec", "--delay", "0"], client=fake_client({}))
    assert code == 2
    assert "unknown encoding" in capsys.readouterr().err


def test_resolvable_not_in_crossref_cli(tmp_path, capsys):
    path = write(tmp_path, "r.bib", "@article{k, doi={10.5281/zenodo.1}}")
    code = run([path, "--delay", "0"], client=fake_client({}, resolves=True))
    out = capsys.readouterr().out
    assert code == 0  # warning, not error
    assert "not in Crossref" in out


def test_retraction_cli_exit_one(tmp_path, capsys):
    rec = dict(GOOD, **{"updated-by": [{"type": "retraction", "DOI": "10.1/notice"}]})
    path = write(tmp_path, "r.bib", "@article{k, doi={10.1371/journal.pone.0312345}}")
    code = run([path, "--delay", "0"], client=fake_client({"10.1371/journal.pone.0312345": rec}))
    out = capsys.readouterr().out
    assert code == 1
    assert "RETRACTED" in out


def test_summary_counts(tmp_path, capsys):
    bib = (
        "@article{ok, doi={10.1371/journal.pone.0312345}}\n"
        "@article{bad, doi={10.9999/nope}}\n"
        "@book{nodoi, title={No DOI}}"
    )
    path = write(tmp_path, "m.bib", bib)
    run([path, "--delay", "0"], client=fake_client({"10.1371/journal.pone.0312345": GOOD}, resolves=False))
    out = capsys.readouterr().out
    assert "checked 3 references: 1 ok, 1 warnings, 1 errors" in out
    # The two buckets must ACCOUNT FOR EVERY reference. The old wording excluded
    # hard errors from both, so "1 verified, 1 could not be verified" silently
    # lost the broken-DOI reference and did not sum to 3.
    assert "1 of 3 compared against a Crossref record; 2 could not be" in out


def test_summary_buckets_always_sum_to_the_total(tmp_path, capsys):
    import re

    bib = "\n".join(
        [
            "@article{ok, doi={10.1371/journal.pone.0312345}}",
            "@article{bad, doi={10.9999/nope}}",
            "@book{nodoi, title={No DOI}}",
            "@article{ok2, doi={10.1371/journal.pone.0312345}}",
        ]
    )
    path = write(tmp_path, "m.bib", bib)
    run([path, "--delay", "0"], client=fake_client({"10.1371/journal.pone.0312345": GOOD}))
    out = capsys.readouterr().out
    m = re.search(r"\((\d+) of (\d+) compared .*?; (\d+) could not be", out)
    checked, total, not_checked = (int(g) for g in m.groups())
    assert checked + not_checked == total == 4


def test_resolve_transient_failure_not_hard_error(tmp_path, capsys):
    # doi.org glitch on a not-in-Crossref DOI must be inconclusive, not an error.
    def flaky_resolve(doi):
        raise TimeoutError("doi.org timeout")

    client = CrossrefClient(_fetch=lambda d: None, _resolve=flaky_resolve)
    path = write(tmp_path, "r.bib", "@article{k, doi={10.1234/maybe.real}}")
    code = run([path, "--delay", "0"], client=client)
    out = capsys.readouterr().out
    assert "does not resolve" not in out  # no false hard error
    assert code == 3  # inconclusive


# --- new input formats end-to-end -------------------------------------------

RIS_DOC = """TY  - JOUR
AU  - Ioannidis, John P. A.
TI  - Why Most Published Research Findings Are False
JO  - PLoS Medicine
PY  - 2005
DO  - 10.1371/journal.pmed.0020124
ER  - 
"""


def test_ris_input_auto_detected(tmp_path, capsys):
    rec = dict(GOOD, title=["Why Most Published Research Findings Are False"],
               author=[{"family": "Ioannidis"}], issued={"date-parts": [[2005]]})
    path = write(tmp_path, "refs.ris", RIS_DOC)
    code = run([path, "--delay", "0", "--json"],
               client=fake_client({"10.1371/journal.pmed.0020124": rec}))
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload[0]["doi"] == "10.1371/journal.pmed.0020124"


def test_csl_json_input_auto_detected(tmp_path, capsys):
    doc = ('[{"id":"k","type":"article-journal","DOI":"10.9999/nope",'
           '"title":"Broken","author":[{"family":"Smith"}],'
           '"issued":{"date-parts":[[2020]]}}]')
    path = write(tmp_path, "refs.json", doc)
    code = run([path, "--delay", "0", "--json"], client=fake_client({}, resolves=False))
    payload = json.loads(capsys.readouterr().out)
    assert code == 1  # broken DOI -> error
    assert payload[0]["status"] == "error"


# --- report formats ---------------------------------------------------------

def test_csv_report(tmp_path, capsys):
    path = write(tmp_path, "r.bib", "@article{k, doi={10.9999/nope}, journal={Nature}}")
    run([path, "--report", "csv", "--delay", "0"], client=fake_client({}, resolves=False))
    out = capsys.readouterr().out
    lines = out.strip().splitlines()
    assert lines[0] == "label,doi,pmid,journal,status,codes,findings"
    assert "10.9999/nope" in lines[1]
    assert "error" in lines[1]
    assert "doi-not-resolving" in lines[1]  # the machine-readable code column


def test_csv_injection_neutralised(tmp_path, capsys):
    # A journal field starting with '=' must be quote-prefixed, not left as a
    # live spreadsheet formula.
    path = write(tmp_path, "r.bib", '@article{k, doi={10.1/x}, journal={=HYPERLINK("evil")}}')
    run([path, "--report", "csv", "--delay", "0"], client=fake_client({"10.1/x": GOOD}))
    out = capsys.readouterr().out
    assert "'=HYPERLINK" in out  # neutralised
    assert ",=HYPERLINK" not in out  # never a bare leading '='


def test_markdown_report(tmp_path, capsys):
    path = write(tmp_path, "r.bib", "@article{k, doi={10.9999/nope}}")
    run([path, "--report", "markdown", "--delay", "0"], client=fake_client({}, resolves=False))
    out = capsys.readouterr().out
    assert "# citecheck report" in out
    assert "| Status | Reference | DOI | Findings |" in out
    assert "✗" in out


def test_json_flag_still_works(tmp_path, capsys):
    path = write(tmp_path, "r.bib", "@article{k, doi={10.9999/nope}}")
    run([path, "--json", "--delay", "0"], client=fake_client({}, resolves=False))
    json.loads(capsys.readouterr().out)  # valid JSON


def test_report_json_equivalent_to_json_flag(tmp_path, capsys):
    path = write(tmp_path, "r.bib", "@article{k, doi={10.9999/nope}}")
    run([path, "--report", "json", "--delay", "0"], client=fake_client({}, resolves=False))
    json.loads(capsys.readouterr().out)


# --- duplicate PMID ---------------------------------------------------------

def test_duplicate_pmid_flagged(tmp_path, capsys):
    bib = ("@article{a, title={T}, note={PMID: 16060722}}\n"
           "@article{b, title={T}, note={PMID: 16060722}}")
    path = write(tmp_path, "d.bib", bib)
    run([path, "--json", "--delay", "0"], client=fake_client({}))
    payload = json.loads(capsys.readouterr().out)
    assert all(any("Duplicate PMID" in f["message"] for f in item["findings"])
               for item in payload)


def test_duplicate_pmid_not_double_reported_with_doi(tmp_path, capsys):
    # Same DOI twice already flags Duplicate DOI; don't also add Duplicate PMID.
    bib = ("@article{a, doi={10.1/x}, note={PMID: 111}}\n"
           "@article{b, doi={10.1/x}, note={PMID: 111}}")
    path = write(tmp_path, "d.bib", bib)
    run([path, "--json", "--delay", "0"], client=fake_client({"10.1/x": GOOD}))
    payload = json.loads(capsys.readouterr().out)
    for item in payload:
        msgs = [f["message"] for f in item["findings"]]
        assert any("Duplicate DOI" in m for m in msgs)
        assert not any("Duplicate PMID" in m for m in msgs)


def test_duplicate_pmid_suppression_keys_on_code_not_prose():
    """The "already reported as a duplicate DOI" test must key on the finding
    CODE, not on the English wording of the duplicate-DOI message.

    Regression: it used to test ``"Duplicate DOI" in f.message``, so rewording
    that one f-string would silently restore a double warning on every
    same-paper duplicate. Here the duplicate-DOI finding carries the right code
    but deliberately different prose — suppression must still hold.
    """
    from citecheck.cli import _flag_duplicate_pmids
    from citecheck.core import CheckResult, WARNING
    from citecheck.parsers import Reference

    results = [
        CheckResult(reference=Reference(raw="a", doi="10.1/x", pmid="111")),
        CheckResult(reference=Reference(raw="b", doi="10.1/x", pmid="111")),
    ]
    for r in results:
        r.add(WARNING, "This DOI is cited by 2 references: 10.1/x", "duplicate-doi")

    _flag_duplicate_pmids(results)

    for r in results:
        assert [f.code for f in r.findings] == ["duplicate-doi"]


def test_duplicate_pmid_still_flagged_when_no_duplicate_doi_code():
    """The suppression must not over-fire: a finding that merely *mentions* a
    duplicate DOI, without carrying the code, does not count as one."""
    from citecheck.cli import _flag_duplicate_pmids
    from citecheck.core import CheckResult, WARNING
    from citecheck.parsers import Reference

    results = [
        CheckResult(reference=Reference(raw="a", pmid="111")),
        CheckResult(reference=Reference(raw="b", pmid="111")),
    ]
    for r in results:
        r.add(WARNING, "Duplicate DOI mentioned in prose only", "title-mismatch")

    _flag_duplicate_pmids(results)

    for r in results:
        assert "duplicate-pmid" in [f.code for f in r.findings]


# --- --pubmed cross-check (injected client) ---------------------------------

def test_cli_pubmed_retraction_exit_one(tmp_path, capsys):
    from citecheck.core import PubMedClient
    rec = {"pubtype": ["Retracted Publication"],
           "articleids": [{"idtype": "doi", "value": "10.1371/journal.pone.0312345"}]}
    pm = PubMedClient(_fetch=lambda p: {"999": rec}.get(p))
    path = write(tmp_path, "r.bib",
                 "@article{k, doi={10.1371/journal.pone.0312345}, pmid={999}}")
    code = run([path, "--pubmed", "--delay", "0"],
               client=fake_client({"10.1371/journal.pone.0312345": GOOD}), pubmed=pm)
    out = capsys.readouterr().out
    assert code == 1
    assert "RETRACTED according to PubMed" in out


def test_cli_pubmed_flag_gates_the_crosscheck(tmp_path, capsys):
    # A PMID-bearing ref whose PubMed record is RETRACTED. Without --pubmed and
    # with no injected PubMed client, the cross-check must NOT run (exit 0);
    # nothing may reach the network (the conftest guard would fail the test if it
    # tried). This proves PubMed is genuinely opt-in.
    path = write(tmp_path, "r.bib",
                 "@article{k, doi={10.1371/journal.pone.0312345}, pmid={999}}")
    code = run([path, "--delay", "0"],
               client=fake_client({"10.1371/journal.pone.0312345": GOOD}))
    assert code == 0  # no PubMed => no retraction error


def test_cli_injected_pubmed_used_even_without_flag(tmp_path, capsys):
    # When a PubMed client is injected (as tests do), it is honoured directly —
    # the --pubmed flag only controls creation of a *real* network client.
    from citecheck.core import PubMedClient
    rec = {"pubtype": ["Retracted Publication"],
           "articleids": [{"idtype": "doi", "value": "10.1371/journal.pone.0312345"}]}
    pm = PubMedClient(_fetch=lambda p: {"999": rec}.get(p))
    path = write(tmp_path, "r.bib",
                 "@article{k, doi={10.1371/journal.pone.0312345}, pmid={999}}")
    code = run([path, "--delay", "0"],
               client=fake_client({"10.1371/journal.pone.0312345": GOOD}), pubmed=pm)
    assert code == 1  # injected PubMed client runs, retraction => error


def test_cli_pubmed_lookup_failure_exit_three(tmp_path, capsys):
    # A PubMed lookup failure must make the run exit 3 (inconclusive), never a
    # false clean pass — the exit-3 contract the docs advertise for --pubmed.
    from citecheck.core import PubMedClient

    def boom(p):
        raise TimeoutError("pubmed offline")

    pm = PubMedClient(_fetch=boom)
    path = write(tmp_path, "r.bib",
                 "@article{k, doi={10.1371/journal.pone.0312345}, pmid={999}}")
    code = run([path, "--delay", "0"],
               client=fake_client({"10.1371/journal.pone.0312345": GOOD}), pubmed=pm)
    assert code == 3


def test_cli_retraction_notice_not_flagged_exit_zero(tmp_path, capsys):
    # "Retraction of Publication" is the notice, not a retracted source — the
    # PMID cross-check must NOT error on it.
    from citecheck.core import PubMedClient
    notice = {"pubtype": ["Retraction of Publication"],
              "articleids": [{"idtype": "doi", "value": "10.1371/journal.pone.0312345"}]}
    pm = PubMedClient(_fetch=lambda p: {"999": notice}.get(p))
    path = write(tmp_path, "r.bib",
                 "@article{k, doi={10.1371/journal.pone.0312345}, pmid={999}}")
    code = run([path, "--delay", "0"],
               client=fake_client({"10.1371/journal.pone.0312345": GOOD}), pubmed=pm)
    assert code == 0


def test_json_doi_field_sanitized(tmp_path, capsys):
    # A DOI carrying a C1 control char (0x9b = CSI) must be stripped in the JSON
    # report too, consistent with text/csv/markdown.
    path = write(tmp_path, "r.bib", "@article{k, title={T}, doi={10.1234/abc\x9b31mPWNED}}")
    run([path, "--json", "--delay", "0"], client=fake_client({}, resolves=False))
    out = capsys.readouterr().out
    assert "\x9b" not in out
    payload = json.loads(out)
    assert "\x9b" not in (payload[0]["doi"] or "")


# --- csv input format (was reachable only via auto-detect) -------------------

CSV_TABLE = "Study ID,Article DOI,Title\nS1,10.1371/journal.pone.0312345,Sleep as a transdiagnostic node in BELL disorders\n"


def test_csv_reference_table_is_autodetected(tmp_path, capsys):
    path = write(tmp_path, "refs.csv", CSV_TABLE)
    assert run([path, "--delay", "0", "--no-color", "-v"], client=fake_client({GOOD["DOI"]: GOOD})) == 0
    assert "Verified" in capsys.readouterr().out


def test_csv_format_can_be_forced(tmp_path, capsys):
    """--format csv was accepted by the parser layer but rejected by argparse."""
    path = write(tmp_path, "refs.txt", CSV_TABLE)
    code = run(
        [path, "--format", "csv", "--delay", "0", "--no-color", "-v"],
        client=fake_client({GOOD["DOI"]: GOOD}),
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "S1" in out  # the key column became the label => the csv parser ran


def test_forcing_csv_on_a_tsv_table_works(tmp_path, capsys):
    table = "id\tdoi\nS1\t10.1371/journal.pone.0312345\n"
    path = write(tmp_path, "refs.tsv", table)
    assert run(["--format", "csv", path, "--delay", "0", "--no-color"], client=fake_client({GOOD["DOI"]: GOOD})) == 0


def test_csv_is_offered_in_the_help_text(capsys):
    with pytest.raises(SystemExit):
        run(["--help"])
    assert "csv" in capsys.readouterr().out


# --- --suggest-doi ----------------------------------------------------------

SEARCH_HIT = {
    "DOI": "10.1371/journal.pone.0312345",
    "title": ["Sleep as a transdiagnostic node in BELL disorders"],
    "author": [{"family": "Kim"}],
    "issued": {"date-parts": [[2024]]},
}


def searching_client(items):
    return CrossrefClient(
        _fetch=lambda d: None, _resolve=lambda d: False, _search=lambda q: items
    )


def test_suggest_doi_names_the_missing_doi(tmp_path, capsys):
    entry = (
        "@article{k, title={Sleep as a transdiagnostic node in BELL disorders}, "
        "author={Kim, Gildong}, year={2024}}"
    )
    path = write(tmp_path, "r.bib", entry)
    run([path, "--suggest-doi", "--delay", "0", "--no-color"], client=searching_client([SEARCH_HIT]))
    assert "10.1371/journal.pone.0312345" in capsys.readouterr().out


def test_without_the_flag_no_doi_is_suggested(tmp_path, capsys):
    entry = "@article{k, title={Sleep as a transdiagnostic node in BELL disorders}, year={2024}}"
    path = write(tmp_path, "r.bib", entry)
    run([path, "--delay", "0", "--no-color"], client=searching_client([SEARCH_HIT]))
    out = capsys.readouterr().out
    assert "No DOI found" in out
    assert "10.1371" not in out


def test_suggested_doi_reaches_the_json_report(tmp_path, capsys):
    entry = "@article{k, title={Sleep as a transdiagnostic node in BELL disorders}, year={2024}}"
    path = write(tmp_path, "r.bib", entry)
    run([path, "--suggest-doi", "--json", "--delay", "0"], client=searching_client([SEARCH_HIT]))
    payload = json.loads(capsys.readouterr().out)
    assert any("10.1371/journal.pone.0312345" in f["message"] for f in payload[0]["findings"])


# --- --cache ----------------------------------------------------------------

def test_cache_flag_writes_a_file_and_serves_the_second_run(tmp_path, capsys):
    path = write(tmp_path, "r.bib", "@article{k, doi={10.1371/journal.pone.0312345}}")
    cache_path = str(tmp_path / "cache.json")
    calls = []

    def client():
        return CrossrefClient(_fetch=lambda d: calls.append(d) or GOOD, _resolve=lambda d: False)

    # The CLI only builds a cache for clients it creates, so exercise the real
    # wiring by patching the transport rather than injecting a whole client.
    import citecheck.cli as cli

    real = cli.CrossrefClient
    cli.CrossrefClient = lambda mailto=None, cache=None: CrossrefClient(
        cache=cache, _fetch=lambda d: calls.append(d) or GOOD, _resolve=lambda d: False
    )
    try:
        assert run([path, "--cache", cache_path, "--delay", "0", "--no-color"]) == 0
        assert calls == ["10.1371/journal.pone.0312345"]
        capsys.readouterr()
        assert run([path, "--cache", cache_path, "--delay", "0", "--no-color", "-v"]) == 0
        assert calls == ["10.1371/journal.pone.0312345"]  # second run hit no transport
        assert "served from the cache" in capsys.readouterr().out
    finally:
        cli.CrossrefClient = real


def test_cache_default_path_is_used_for_a_bare_flag(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    import importlib

    import citecheck.cli as cli

    importlib.reload(cli)
    try:
        assert cli.DEFAULT_CACHE_PATH.startswith(str(tmp_path))
        parsed = cli.build_parser().parse_args(["x.bib", "--cache"])
        assert parsed.cache == cli.DEFAULT_CACHE_PATH
    finally:
        importlib.reload(cli)


def test_cache_is_off_by_default():
    from citecheck.cli import build_parser

    args = build_parser().parse_args(["x.bib"])
    assert args.cache is None
    assert args.suggest_doi is False


def test_unwritable_cache_does_not_change_the_verdict(tmp_path, capsys):
    path = write(tmp_path, "r.bib", "@article{k, doi={10.1371/journal.pone.0312345}}")
    bad_cache = tmp_path / "cache_dir"
    bad_cache.mkdir()

    import citecheck.cli as cli

    real = cli.CrossrefClient
    cli.CrossrefClient = lambda mailto=None, cache=None: CrossrefClient(
        cache=cache, _fetch=lambda d: GOOD, _resolve=lambda d: False
    )
    try:
        assert run([path, "--cache", str(bad_cache), "--delay", "0", "--no-color"]) == 0
        assert "could not write the cache" in capsys.readouterr().err
    finally:
        cli.CrossrefClient = real


def test_negative_cache_ttl_is_rejected(tmp_path):
    with pytest.raises(SystemExit):
        run(["x.bib", "--cache-ttl", "-1"])


# --- 0.2.0: skipped BibTeX entries are named and make the run inconclusive ---

UNBALANCED_BIB = (
    "@article{a,\n  title = {Sleep as a transdiagnostic node in BELL disorders},\n"
    "  doi = {10.1371/journal.pone.0312345}\n}\n"
    "@article{bad,\n  title = {Effects of {IL-6 on sleep},\n  doi = {10.1000/bad}\n}\n"
    "@article{c,\n  title = {Sleep as a transdiagnostic node in BELL disorders},\n"
    "  doi = {10.1371/journal.pone.0312345}\n}\n"
)


def test_skipped_entry_is_named_and_exit_is_inconclusive(tmp_path, capsys):
    path = write(tmp_path, "unbalanced.bib", UNBALANCED_BIB)
    code = run([path, "--delay", "0", "--json"],
               client=fake_client({"10.1371/journal.pone.0312345": GOOD}))
    captured = capsys.readouterr()
    labels = [item["label"] for item in json.loads(captured.out)]
    assert sorted(labels) == ["a", "c"]  # c is checked, not swallowed
    assert "bad" in captured.err
    assert "could not be parsed" in captured.err
    assert code == 3


def test_skipped_entry_does_not_mask_a_real_error(tmp_path, capsys):
    bib = UNBALANCED_BIB.replace("10.1371/journal.pone.0312345}\n}\n@article{bad",
                                 "10.9999/broken}\n}\n@article{bad")
    path = write(tmp_path, "unbalanced.bib", bib)
    code = run([path, "--delay", "0"], client=fake_client({"10.1371/journal.pone.0312345": GOOD}))
    assert code == 1


def test_file_whose_only_entry_is_unterminated_names_it(tmp_path, capsys):
    path = write(tmp_path, "one.bib", "@article{lonely,\n  title = {Effects of {IL-6 on sleep}\n")
    code = run([path, "--delay", "0"], client=fake_client({}))
    err = capsys.readouterr().err
    assert code == 2
    assert "lonely" in err
    assert "no references found" in err


def test_skipped_entry_keys_are_sanitized_and_capped(tmp_path, capsys):
    bad = "".join("@article{bad%d\x1b[2K,\n  title = {oops\n" % i for i in range(15))
    good = "@article{ok,\n  doi = {10.1371/journal.pone.0312345}\n}\n"
    path = write(tmp_path, "many.bib", good + bad)
    code = run([path, "--delay", "0"], client=fake_client({"10.1371/journal.pone.0312345": GOOD}))
    err = capsys.readouterr().err
    assert "\x1b" not in err
    assert "15 malformed BibTeX entries" in err
    assert "bad0[2K" in err and "and 5 more" in err
    assert code == 3


# --- 0.2.0: no input on a terminal is a usage hint, not a silent wait ---------

class _TerminalStdin:
    """A stdin that is a terminal; reading it would wait for the keyboard."""

    def isatty(self):
        return True

    @property
    def buffer(self):
        raise AssertionError("citecheck read from an interactive terminal")

    def read(self, *args):
        raise AssertionError("citecheck read from an interactive terminal")


@pytest.mark.parametrize("argv", [[], ["-"]])
def test_no_input_on_a_terminal_prints_a_hint_and_exits_2(monkeypatch, capsys, argv):
    import sys

    monkeypatch.setattr(sys, "stdin", _TerminalStdin())
    monkeypatch.setenv("FORCE_COLOR", "1")  # Python 3.14 argparse would colour usage
    monkeypatch.delenv("NO_COLOR", raising=False)
    code = run(argv, client=fake_client({}))
    err = capsys.readouterr().err
    assert code == 2
    assert "usage: citecheck" in err
    assert "give a reference file, or pipe one in" in err
    assert "\x1b" not in err


def test_piped_stdin_still_works(monkeypatch, capsys):
    import io
    import sys

    class Piped:
        buffer = io.BytesIO(b"@article{k, doi={10.1371/journal.pone.0312345}}")

        def isatty(self):
            return False

    monkeypatch.setattr(sys, "stdin", Piped())
    code = run(["--delay", "0"], client=fake_client({"10.1371/journal.pone.0312345": GOOD}))
    assert code == 0


@pytest.mark.parametrize("argv", [[], ["-"]])
def test_no_stdin_at_all_is_a_usage_error(monkeypatch, capsys, argv):
    """pythonw, a service or a GUI launcher can start Python with sys.stdin None."""
    import sys

    monkeypatch.setattr(sys, "stdin", None)
    code = run(argv, client=fake_client({}))
    assert code == 2
    assert "give a reference file, or pipe one in" in capsys.readouterr().err


def test_classic_mac_cr_only_csv_is_read_as_rows(tmp_path, capsys):
    """Excel for Mac 2011 saves "CSV (Macintosh)" with bare CR line endings."""
    table = (
        "Title,DOI\r"
        "Sleep as a transdiagnostic node in BELL disorders,10.1371/journal.pone.0312345\r"
        "A second synthetic title,10.9999/second\r"
    )
    path = write(tmp_path, "mac.csv", table.encode("utf-8"))
    run([path, "--delay", "0", "--json"], client=fake_client({"10.1371/journal.pone.0312345": GOOD}))
    payload = json.loads(capsys.readouterr().out)
    assert sorted(item["doi"] for item in payload) == [
        "10.1371/journal.pone.0312345", "10.9999/second",
    ]
