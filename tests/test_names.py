"""Author names as BibTeX writes them: LaTeX accents and brace-protected names.

A correct citation must not warn just because the .bib spells Öztürk as
{\\"O}zt{\\"u}rk, or protects a corporate author with an extra brace pair.
"""

import pytest

from citecheck.core import CrossrefClient, check_reference
from citecheck.parsers import Reference, _clean_bibtex_value, parse_bibtex


@pytest.mark.parametrize("latex, plain", [
    (r'{\"O}zt{\"u}rk', "Öztürk"),
    (r"\'e", "é"),
    (r"\'{e}", "é"),
    (r"{\'e}", "é"),
    (r"\`a", "à"),
    (r"\^o", "ô"),
    (r"\~n", "ñ"),
    (r"\c{c}", "ç"),
    (r"\c c", "ç"),
    (r"\v{s}", "š"),
    (r"\H{o}", "ő"),
    (r"\={a}", "ā"),
    (r"\.{z}", "ż"),
    (r"\u{a}", "ă"),
    (r"\'{\i}", "í"),
    (r"\ss{}", "ß"),
    (r"{\o}", "ø"),
    (r"{\aa}", "å"),
    (r"\AA{}", "Å"),
    (r"Multiculture \& Peace", "Multiculture & Peace"),
])
def test_latex_accents_are_decoded(latex, plain):
    assert _clean_bibtex_value(latex) == plain


@pytest.mark.parametrize("text, kept", [
    (r"\url{https://example.org}", r"\url"),
    (r"see \cite{key}", r"\cite"),
    (r"\verb|x|", r"\verb"),
    (r"\Huge title", r"\Huge"),
])
def test_other_commands_are_left_alone(text, kept):
    assert kept in _clean_bibtex_value(text)


def client_for(authors):
    record = {"DOI": "10.1000/n.1", "title": ["A synthetic title"], "author": authors,
              "issued": {"date-parts": [[2020]]}}
    return CrossrefClient(_fetch=lambda d: record, _resolve=lambda d: False)


def codes_for(author_field, authors):
    bib = "@article{k, author = {%s}, doi = {10.1000/n.1}, year = {2020}}" % author_field
    ref = parse_bibtex(bib)[0]
    return ref, [f.code for f in check_reference(ref, client_for(authors)).findings]


def test_accented_surname_in_latex_matches_crossref():
    ref, codes = codes_for(r'{\"O}zt{\"u}rk, A.', [{"family": "Öztürk", "given": "A"}])
    assert ref.author == "Öztürk"
    assert codes == ["verified"]


def test_brace_protected_corporate_author_is_kept_whole():
    """Live shape: the Wakefield retraction notice is authored by
    family='The Editors of The Lancet'."""
    ref, codes = codes_for("{The Editors of The Lancet}",
                           [{"family": "The Editors of The Lancet"}])
    assert ref.author == "The Editors of The Lancet"
    assert codes == ["verified"]


def test_and_inside_braces_does_not_split_authors():
    ref, codes = codes_for("{Johnson and Johnson Research} and Smith, J.",
                           [{"family": "Johnson and Johnson Research"}, {"family": "Smith"}])
    assert ref.author == "Johnson and Johnson Research"
    assert codes == ["verified"]


def test_corporate_name_with_a_comma_is_kept_whole_and_matches_the_organisation():
    ref, codes = codes_for("{Ministry of Health, Labour and Welfare} and Smith, J.",
                           [{"name": "Ministry of Health, Labour and Welfare"},
                            {"family": "Smith", "given": "J"}])
    assert ref.author == "Ministry of Health, Labour and Welfare"
    assert codes == ["verified"]


def test_protected_personal_name_with_a_comma_gives_the_surname():
    ref, codes = codes_for("{Hong, Gildong} and {Lee, Sara}",
                           [{"family": "Hong", "given": "Gildong"}])
    assert ref.author == "Hong"
    assert codes == ["verified"]


def test_protected_personal_name_without_a_comma_still_matches_its_surname():
    """Zotero exports single-field creators as {John Smith}."""
    _, codes = codes_for("{John Smith} and Doe, Jane", [{"family": "Smith", "given": "John"}])
    assert codes == ["verified"]


def test_organisation_name_matches_when_the_record_also_has_people():
    _, codes = codes_for("{WHO Collaborative Group}",
                         [{"name": "WHO Collaborative Group"}, {"family": "Smith"}])
    assert codes == ["verified"]


def test_organisation_only_record_still_skips_the_author_check():
    _, codes = codes_for("Smith, J.", [{"name": "Some Trial Consortium"}])
    assert codes == ["verified"]


def test_a_wrong_author_still_warns():
    _, codes = codes_for("Jones, A.", [{"family": "Smith", "given": "J"}])
    assert codes == ["author-mismatch"]


def test_unicode_hyphen_in_a_crossref_family_name_matches():
    ref = Reference(raw="", doi="10.1000/n.1", author="Lorenzi-Filho", year=2020)
    res = check_reference(ref, client_for([{"family": "Lorenzi‐Filho"}]))
    assert [f.code for f in res.findings] == ["verified"]
