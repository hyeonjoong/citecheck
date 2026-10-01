"""Public files must not carry local paths or invented example output.

An earlier 사용법.md told every reader to cd into the maintainer's own project
folder, and the README showed a --suggest-doi match for a DOI that belongs to an
unrelated paper. These checks keep that from coming back.
"""

import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent

DOCS = ["README.md", "사용법.md", "실행.command", "CHANGELOG.md", "HARDENING.md"]
EXAMPLES = sorted(
    p.relative_to(ROOT).as_posix()
    for p in (ROOT / "examples").iterdir()
    # Dotfiles are skipped: Finder drops a .DS_Store into any folder it opens.
    if p.is_file() and not p.name.startswith(".")
)

# Built at runtime so this file does not match its own search.
FORBIDDEN = ["/" + "Users/", "Down" + "loads/", "BELL" + "_Paper", "02_" + "프로젝트", "이미 " + "설치"]


@pytest.mark.parametrize("name", DOCS + EXAMPLES)
def test_no_local_paths_in_public_files(name):
    text = (ROOT / name).read_text(encoding="utf-8")
    found = [s for s in FORBIDDEN if s in text]
    assert not found, f"{name} contains {found}"


@pytest.mark.parametrize("name", ["README.md", "사용법.md"])
def test_docs_do_not_use_the_fictional_test_doi(name):
    """The mocked tests pair 10.1371/journal.pone.0312345 with a made-up title.
    That pairing must never be shown to readers as real output."""
    assert "journal.pone.0312345" not in (ROOT / name).read_text(encoding="utf-8")
