"""Korean input: Excel tables with Korean headers, Hangul titles and names.

Typical users keep reference tables in Korean Excel exports (CP949 or UTF-8
with a BOM, Korean column names) and cite KCI journals, whose Crossref records
may be in Korean, in English, or both. Fixtures are trimmed from live Crossref
records (fetched 2026-10-01); author names in cited text are synthetic.
"""

import json
import unicodedata

import pytest

from citecheck.cli import run
from citecheck.core import OK, WARNING, CrossrefClient, check_reference
from citecheck.parsers import (
    Reference,
    _csv_header_fields,
    _normalize_header,
    parse_bibtex,
    parse_csv,
    parse_references,
    parse_text,
)

# api.crossref.org/works/10.1371/journal.pmed.0020124 (trimmed).
IOANNIDIS = {
    "DOI": "10.1371/journal.pmed.0020124",
    "type": "journal-article",
    "title": ["Why Most Published Research Findings Are False"],
    "author": [{"family": "Ioannidis", "given": "John P. A."}],
    "container-title": ["PLoS Medicine"],
    "short-container-title": ["PLoS Med"],
    "issued": {"date-parts": [[2005, 8, 30]]},
}

# api.crossref.org/works/10.13078/jksrs.04006 (trimmed): Korean title, author
# romanised.
KCI = {
    "DOI": "10.13078/jksrs.04006",
    "type": "journal-article",
    "title": ["폐쇄성 수면 무호흡 증후군의 진단과 치료"],
    "author": [{"family": "Yun"}],
    "container-title": ["Journal of Korean Sleep Research Society"],
    "short-container-title": ["J Korean Sleep Res Soc"],
    "issued": {"date-parts": [[2004, 6, 30]]},
}

# api.crossref.org/works/10.5124/jkma.2020.63.1.30 (trimmed): English only.
JKMA = {
    "DOI": "10.5124/jkma.2020.63.1.30",
    "type": "journal-article",
    "title": ["Drug-induced nephrotoxicity"],
    "author": [{"family": "Bae"}, {"family": "Lee"}, {"family": "Park"}],
    "container-title": ["Journal of the Korean Medical Association"],
    "short-container-title": ["J Korean Med Assoc"],
    "issued": {"date-parts": [[2020]]},
}


def client_for(record):
    return CrossrefClient(_fetch=lambda d: record if d == record["DOI"] else None,
                          _resolve=lambda d: False)


def codes(result):
    return [f.code for f in result.findings]


# --- headers ------------------------------------------------------------------

def test_korean_header_keeps_hangul():
    assert _normalize_header("논문 제목") == "논문제목"
    assert _normalize_header("제1저자") == "제1저자"
    assert _normalize_header("연구 ID") == "연구id"


def test_decomposed_hangul_header_is_folded_to_the_same_key():
    """macOS can hand over NFD text; it must map like the composed form."""
    assert _normalize_header(unicodedata.normalize("NFD", "논문제목")) == "논문제목"


def test_korean_header_row_maps_every_column():
    header = ["번호", "제1저자", "발행연도", "논문제목", "학술지", "DOI"]
    assert _csv_header_fields(header) == {
        "key": 0, "author": 1, "year": 2, "title": 3, "journal": 4, "doi": 5,
    }


@pytest.mark.parametrize("name, field", [
    ("제목", "title"), ("논문명", "title"), ("표제", "title"),
    ("저자", "author"), ("저자명", "author"), ("주저자", "author"), ("제1저자명", "author"),
    ("연도", "year"), ("출판연도", "year"), ("게재연도", "year"), ("발행년도", "year"),
    ("출판년도", "year"), ("학술지명", "journal"), ("저널", "journal"),
    ("저널명", "journal"), ("게재지", "journal"), ("연번", "key"), ("연구ID", "key"),
    ("연구번호", "key"),
])
def test_korean_header_aliases(name, field):
    assert _csv_header_fields([name, "DOI"]).get(field) == 0


@pytest.mark.parametrize("name, field", [
    ("Title(제목)", "title"), ("제목(Title)", "title"), ("Author(저자)", "author"),
    ("저자 (Authors)", "author"), ("Year(연도)", "year"), ("Journal(학술지)", "journal"),
    ("DOI(링크)", "doi"), ("DOI 번호", "doi"), ("DOI 주소", "doi"), ("논문 DOI", "doi"),
    ("PMID(번호)", "pmid"), ("No(번호)", "key"), ("제1저자 (First author)", "author"),
    ("제목 1", "title"), ("년도", "year"),
])
def test_bilingual_and_variant_headers_are_recognised(name, field):
    """Korean lab sheets often write both languages in one header cell. Keeping
    Hangul in the key must not stop "Title(제목)" from meaning title."""
    assert _csv_header_fields(["비고", name]).get(field) == 1


def test_unrelated_bilingual_header_is_not_mapped():
    assert _csv_header_fields(["Notes(비고)", "인용문 (Citation)", "DOI"]) == {"doi": 2}


def test_bilingual_headers_decide_the_delimiter():
    """A tab-separated sheet whose header cell holds commas splits into as many
    cells on "," as on tab; only the recognised bilingual names pick the tab."""
    text = (
        "Title(제목)\tDOI(링크)\tNotes(비고, 메모, 기타)\r\n"
        "Sleep, mood and memory: a synthetic title\t10.1000/syn.1\tnone\r\n"
    )
    (ref,) = parse_references(text)
    assert ref.title == "Sleep, mood and memory: a synthetic title"
    assert ref.doi == "10.1000/syn.1"


BILINGUAL_CSV = (
    "번호,Author(저자),Year(연도),Title(제목),Journal(학술지),DOI(링크)\r\n"
    "1,Smith J,2019,A completely different paper about penguins,Nature,"
    "10.1371/journal.pmed.0020124\r\n"
)


@pytest.mark.parametrize("doi_header", ["DOI(링크)", "DOI", "DOI 번호"])
def test_bilingual_header_table_with_a_swapped_doi_is_caught(tmp_path, capsys, doi_header):
    path = tmp_path / "refs.csv"
    path.write_bytes(BILINGUAL_CSV.replace("DOI(링크)", doi_header).encode("utf-8-sig"))
    code = run([str(path), "--json", "--delay", "0"], client=client_for(IOANNIDIS))
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert len(payload) == 1  # the header row is not a phantom reference
    found = {f["code"] for f in payload[0]["findings"]}
    assert {"title-mismatch", "year-mismatch", "author-mismatch",
            "journal-mismatch"} <= found
    assert "Columns not used" not in captured.err
    assert code == 0  # warnings only


SWAPPED_CSV = (
    "번호,제1저자,발행연도,논문제목,학술지,DOI\r\n"
    "1,Smith,2019,A completely different paper about penguins,Nature,"
    "10.1371/journal.pmed.0020124\r\n"
)


@pytest.mark.parametrize("encoding", ["utf-8-sig", "cp949"])
def test_korean_header_table_with_a_swapped_doi_is_caught(tmp_path, capsys, encoding):
    path = tmp_path / "refs.csv"
    path.write_bytes(SWAPPED_CSV.encode(encoding))
    code = run([str(path), "--json", "--delay", "0"], client=client_for(IOANNIDIS))
    payload = json.loads(capsys.readouterr().out)
    assert len(payload) == 1
    found = {f["code"] for f in payload[0]["findings"]}
    assert {"title-mismatch", "year-mismatch", "author-mismatch",
            "journal-mismatch"} <= found
    assert code == 0  # warnings only


def test_table_with_only_a_doi_column_names_the_unused_columns(tmp_path, capsys):
    path = tmp_path / "refs.csv"
    path.write_text("연번,인용문,비고,DOI\n1,some text,note,10.1371/journal.pmed.0020124\n",
                    encoding="utf-8")
    run([str(path), "--delay", "0"], client=client_for(IOANNIDIS))
    err = capsys.readouterr().err
    assert "not compared" in err
    assert "인용문" in err and "비고" in err
    assert "연번" not in err.split("not used:")[-1]  # the key column is used


def test_unused_column_note_strips_terminal_escapes(tmp_path, capsys):
    path = tmp_path / "refs.csv"
    path.write_text("연번,인용문\x1b[2K\x1b[32m,DOI\n1,some text,10.1371/journal.pmed.0020124\n",
                    encoding="utf-8")
    run([str(path), "--delay", "0"], client=client_for(IOANNIDIS))
    err = capsys.readouterr().err
    assert "Columns not used" in err and "인용문" in err
    assert "\x1b" not in err


def test_table_with_a_title_column_prints_no_unused_column_note(tmp_path, capsys):
    path = tmp_path / "refs.csv"
    path.write_text("논문제목,비고,DOI\nWhy Most Published Research Findings Are False,x,"
                    "10.1371/journal.pmed.0020124\n", encoding="utf-8")
    run([str(path), "--delay", "0"], client=client_for(IOANNIDIS))
    assert "not compared" not in capsys.readouterr().err


# --- text mode: Hangul titles ---------------------------------------------------

KCI_LINE = ("홍길동. 폐쇄성 수면 무호흡 증후군의 진단과 치료. "
            "J Korean Sleep Res Soc. 2004. doi:10.13078/jksrs.04006")


def test_korean_text_citation_of_a_korean_record_is_ok():
    ref = parse_text(KCI_LINE)[0]
    res = check_reference(ref, client_for(KCI))
    assert res.status == OK, codes(res)


def test_korean_text_citation_with_the_wrong_title_still_warns():
    line = KCI_LINE.replace("폐쇄성 수면 무호흡 증후군의 진단과 치료",
                            "소아 천식 환자의 흡입기 사용 교육 효과")
    res = check_reference(parse_text(line)[0], client_for(KCI))
    assert "text-title-missing" in codes(res)


def test_korean_text_citation_of_an_english_only_record_is_not_compared():
    line = "홍길동. 약물 유발 신독성. 대한의사협회지. 2020. doi:10.5124/jkma.2020.63.1.30"
    res = check_reference(parse_text(line)[0], client_for(JKMA))
    assert res.status == OK, codes(res)
    assert "not compared" in res.findings[0].message


def test_urls_do_not_make_a_korean_text_citation_read_as_english():
    """The DOI and URLs are Latin letters whatever language the citation is in.
    Counted as text, this long URL outweighs the Hangul and the line was judged
    English, so the English-only record raised text-title-missing."""
    url = "https://www.jkma.org/journal/view.php?doi=10.5124/jkma.2020.63.1.30"
    line = f"홍길동. 약물 유발 신독성. 대한의사협회지. 2020. {url}"
    ref = parse_text(line)[0]
    assert ref.doi == JKMA["DOI"]
    res = check_reference(ref, client_for(JKMA))
    assert res.status == OK, codes(res)
    assert "not compared" in res.findings[0].message


def test_strict_run_on_korean_text_list_exits_0(tmp_path, capsys):
    path = tmp_path / "ko_refs.txt"
    path.write_text(KCI_LINE + "\n", encoding="utf-8")
    assert run([str(path), "--strict", "--delay", "0"], client=client_for(KCI)) == 0


# --- structured Korean citations of English-only records --------------------------

KOREAN_BIB = """@article{bae2020,
  title   = {약물 유발 신독성},
  author  = {홍길동 and 김철수},
  journal = {대한의사협회지},
  year    = {2020},
  doi     = {10.5124/jkma.2020.63.1.30}
}
"""


def test_korean_bibtex_against_an_english_record_is_ok_and_says_what_was_skipped():
    ref = parse_bibtex(KOREAN_BIB)[0]
    res = check_reference(ref, client_for(JKMA))
    assert res.status == OK, codes(res)
    msg = res.findings[0].message
    assert msg.startswith("Verified: Bae (2020)")
    assert "(title/author/journal not compared: cited in Korean, Crossref record in English)" in msg


def test_year_is_still_compared_across_scripts():
    ref = parse_bibtex(KOREAN_BIB.replace("{2020}", "{2019}"))[0]
    assert "year-mismatch" in codes(check_reference(ref, client_for(JKMA)))


def test_english_citation_of_a_korean_record_is_not_compared():
    record = dict(KCI, author=[{"family": "홍", "given": "길동"}])
    ref = Reference(raw="", doi=KCI["DOI"], title="Diagnosis and treatment of obstructive "
                    "sleep apnea syndrome", author="Hong", year=2004)
    res = check_reference(ref, client_for(record))
    assert res.status == OK, codes(res)
    assert "cited in English, Crossref record in Korean" in res.findings[0].message


def test_latin_vs_latin_swapped_title_still_warns():
    ref = Reference(raw="", doi=JKMA["DOI"], title="A completely unrelated paper about penguins")
    assert "title-mismatch" in codes(check_reference(ref, client_for(JKMA)))


def test_korean_vs_korean_different_title_still_warns():
    ref = Reference(raw="", doi=KCI["DOI"], title="소아 천식 환자의 흡입기 사용 교육 효과")
    assert "title-mismatch" in codes(check_reference(ref, client_for(KCI)))


def test_original_title_is_a_title_candidate():
    record = dict(KCI, **{"original-title": ["Diagnosis and Treatment of Obstructive "
                                             "Sleep Apnea Syndrome"]})
    ref = Reference(raw="", doi=KCI["DOI"], title="Diagnosis and treatment of obstructive "
                    "sleep apnea syndrome", year=2004)
    res = check_reference(ref, client_for(record))
    assert res.status == OK, codes(res)
    # Compared against the English original-title, not skipped across scripts.
    assert "not compared" not in res.findings[0].message


ENGLISH_MAIN_KOREAN_ORIGINAL = dict(JKMA)
ENGLISH_MAIN_KOREAN_ORIGINAL["original-title"] = ["약물 유발 신독성"]


def test_korean_original_title_is_compared_with_a_korean_citation():
    ref = Reference(raw="", doi=JKMA["DOI"], title="약물 유발 신독성", year=2020)
    res = check_reference(ref, client_for(ENGLISH_MAIN_KOREAN_ORIGINAL))
    assert res.status == OK, codes(res)
    assert "not compared" not in res.findings[0].message


def test_wrong_korean_title_against_a_korean_original_title_warns():
    ref = Reference(raw="", doi=JKMA["DOI"], title="소아 천식 환자의 흡입기 사용 교육 효과",
                    year=2020)
    res = check_reference(ref, client_for(ENGLISH_MAIN_KOREAN_ORIGINAL))
    assert "title-mismatch" in codes(res)


# --- Hangul author names -----------------------------------------------------------

SPLIT = {"DOI": "10.1000/ko.1", "title": ["가상 논문 제목 예시"],
         "author": [{"family": "홍", "given": "길동"}, {"family": "김", "given": "철수"}],
         "issued": {"date-parts": [[2021]]}}
WHOLE = dict(SPLIT, author=[{"family": "홍길동"}])


def author_codes(cited, record):
    ref = Reference(raw="", doi=record["DOI"], author=cited, year=2021)
    return codes(check_reference(ref, client_for(record)))


@pytest.mark.parametrize("record", [SPLIT, WHOLE], ids=["split", "whole"])
def test_full_hangul_name_matches_split_and_whole_records(record):
    assert author_codes("홍길동", record) == ["verified"]


def test_family_name_alone_matches():
    assert author_codes("홍", SPLIT) == ["verified"]
    assert author_codes("홍", WHOLE) == ["verified"]


def test_a_different_hangul_name_still_warns():
    """Syllables are compared exactly: one different syllable is another person."""
    record = dict(SPLIT, author=[{"family": "김", "given": "민수"}])
    assert "author-mismatch" in author_codes("김민주", record)
    assert "author-mismatch" in author_codes("이길동", SPLIT)


def test_hangul_organisation_author_matches_its_name():
    """Crossref keeps an organisation author in `name`; a record may list one
    next to people, and citing the organisation is correct."""
    record = dict(SPLIT, author=SPLIT["author"] + [{"name": "가상수면연구회"}])
    assert author_codes("가상수면연구회", record) == ["verified"]
    assert "author-mismatch" in author_codes("다른연구회", record)


def test_hangul_name_with_a_space_in_bibtex_is_read_whole():
    bib = "@article{k, author = {홍 길동 and 김 철수}, doi = {10.1000/ko.1}, year = {2021}}"
    ref = parse_bibtex(bib)[0]
    assert ref.author == "홍길동"
    assert codes(check_reference(ref, client_for(SPLIT))) == ["verified"]


def test_hangul_surname_comma_given_in_bibtex():
    ref = parse_bibtex("@article{k, author = {홍, 길동}, doi = {10.1000/ko.1}}")[0]
    assert ref.author == "홍"


@pytest.mark.parametrize("cell", ["홍길동 외", "홍길동 외 2인", "홍길동 외 3명", "홍길동외 2인",
                                  "홍길동 등", "홍길동, 김철수", "홍 길동, 김 철수", "홍길동; 김철수"])
def test_korean_et_al_markers_are_not_read_as_the_surname(cell):
    table = "논문제목,제1저자,DOI\n가상 논문 제목 예시,%s,10.1000/ko.1\n" % (
        '"%s"' % cell if "," in cell else cell)
    ref = parse_csv(table)[0]
    assert ref.author == "홍길동"
    assert codes(check_reference(ref, client_for(SPLIT))) == ["verified"]


@pytest.mark.parametrize("cell", ["Smith et al.", "Smith et al", "Smith J, et al.", "Smith J et al",
                                  "Smith J; Lee K", "Smith JP; Lee K; Park M", "Smith, John; Lee, Kim"])
def test_english_et_al_is_not_read_as_the_surname(cell):
    table = 'Title,Authors,DOI\nA synthetic title,"%s",10.1000/en.1\n' % cell
    assert parse_csv(table)[0].author == "Smith"


def test_glued_korean_marker_without_a_count_is_kept():
    """"외" or "등" glued to a name with no count is ambiguous; leave the name alone."""
    table = "논문제목,제1저자,DOI\n가상 제목,김등,10.1000/ko.1\n"
    assert parse_csv(table)[0].author == "김등"
