"""Tests for reading .xlsx / .docx reference lists (citecheck.office).

Fixtures are built here with ``zipfile`` rather than committed as binaries, so
what each test asserts about the file's XML is visible in the test itself.
"""

import io
import json
import zipfile

import pytest

from citecheck.cli import run
from citecheck.office import (
    MAX_MEMBERS,
    OfficeError,
    convert_office_bytes,
    docx_to_text,
    looks_like_zip,
    xlsx_to_csv_text,
)
from citecheck.parsers import parse_references

SHEET_NS = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
REL_NS = 'xmlns="http://schemas.openxmlformats.org/package/2006/relationships"'
DOC_REL_NS = 'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'
WORD_NS = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'


def _sheet_xml(rows, shared_index):
    """A worksheet part; strings go through the shared-string table like Excel's."""
    out = [f"<worksheet {SHEET_NS}><sheetData>"]
    for r, row in enumerate(rows, start=1):
        out.append(f'<row r="{r}">')
        for c, value in enumerate(row):
            if value is None:
                continue  # Excel omits an empty cell entirely
            ref = f"{chr(ord('A') + c)}{r}"
            if isinstance(value, (int, float)):
                out.append(f'<c r="{ref}"><v>{value}</v></c>')
            else:
                out.append(f'<c r="{ref}" t="s"><v>{shared_index[value]}</v></c>')
        out.append("</row>")
    out.append("</sheetData></worksheet>")
    return "".join(out)


def make_xlsx(sheets):
    """Build an .xlsx from {sheet name: rows}. Rows may hold None for empty cells."""
    strings = []
    index = {}
    for rows in sheets.values():
        for row in rows:
            for value in row:
                if isinstance(value, str) and value not in index:
                    index[value] = len(strings)
                    strings.append(value)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        sheet_tags = "".join(
            f'<sheet name="{name}" sheetId="{i}" r:id="rId{i}"/>'
            for i, name in enumerate(sheets, start=1)
        )
        zf.writestr(
            "xl/workbook.xml",
            f"<workbook {SHEET_NS} {DOC_REL_NS}><sheets>{sheet_tags}</sheets></workbook>",
        )
        rels = "".join(
            f'<Relationship Id="rId{i}" Target="worksheets/sheet{i}.xml"/>'
            for i in range(1, len(sheets) + 1)
        )
        zf.writestr("xl/_rels/workbook.xml.rels", f"<Relationships {REL_NS}>{rels}</Relationships>")
        si = "".join(
            "<si><t>" + s.replace("&", "&amp;").replace("<", "&lt;") + "</t></si>"
            for s in strings
        )
        zf.writestr("xl/sharedStrings.xml", f"<sst {SHEET_NS}>{si}</sst>")
        for i, rows in enumerate(sheets.values(), start=1):
            zf.writestr(f"xl/worksheets/sheet{i}.xml", _sheet_xml(rows, index))
    return buf.getvalue()


def make_docx(paragraphs):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        body = "".join(
            "<w:p>" + "".join(f"<w:r><w:t>{p}</w:t></w:r>" for p in [para]) + "</w:p>"
            for para in paragraphs
        )
        zf.writestr("word/document.xml", f"<w:document {WORD_NS}><w:body>{body}</w:body></w:document>")
    return buf.getvalue()


TABLE = [
    ["Study ID", "Title", "First author", "Year", "Journal", "DOI"],
    ["S-01", "Aspirin in ACS", "Kim H", 2024, "Lancet", "10.1/a"],
    ["S-02", "Statins and myalgia", "Park J", 2004, "BMJ", "https://doi.org/10.1/b"],
]


def test_looks_like_zip():
    assert looks_like_zip(make_xlsx({"Sheet1": TABLE}))
    assert not looks_like_zip(b"@article{a, doi={10.1/a}}")
    assert not looks_like_zip(b"")


def test_xlsx_round_trips_into_references():
    text, kind, sheet = convert_office_bytes(make_xlsx({"Included": TABLE}))
    assert (kind, sheet) == ("xlsx", "Included")
    refs = parse_references(text)
    assert [r.doi for r in refs] == ["10.1/a", "10.1/b"]
    assert [r.year for r in refs] == [2024, 2004]
    assert [r.author for r in refs] == ["Kim", "Park"]
    assert [r.journal for r in refs] == ["Lancet", "BMJ"]
    assert [r.key for r in refs] == ["S-01", "S-02"]


def test_numeric_cells_do_not_become_floats():
    """A year cell stored as a number must read as 2024, never as '2024.0'."""
    rows = [["Title", "Year", "DOI"], ["A trial", 2024.0, "10.1/a"]]
    text = xlsx_to_csv_text(make_xlsx({"S": rows}))
    assert "2024" in text and "2024.0" not in text


def test_missing_cells_do_not_shift_the_columns():
    """Excel omits empty cells; a blank DOI must not slide Year into the DOI column."""
    rows = [
        ["Title", "DOI", "Year"],
        ["Has a DOI", "10.1/a", 2024],
        ["No DOI", None, 2011],
    ]
    refs = parse_references(xlsx_to_csv_text(make_xlsx({"S": rows})))
    assert [(r.doi, r.year) for r in refs] == [("10.1/a", 2024), (None, 2011)]


def test_the_reference_sheet_is_picked_over_a_cover_sheet():
    """Real workbooks open on a README/PRISMA tab with the table on sheet 2."""
    cover = [["Systematic review of aspirin"], ["Screened", 812], ["Included", 2]]
    text = xlsx_to_csv_text(make_xlsx({"Cover": cover, "Included studies": TABLE}))
    refs = parse_references(text)
    assert [r.doi for r in refs] == ["10.1/a", "10.1/b"]


def test_sheet_order_follows_the_workbook_tabs_not_the_part_numbers():
    """Reordering tabs in Excel does not renumber sheetN.xml — order comes from
    workbook.xml, so a workbook whose tab order is reversed still reads right."""
    raw = make_xlsx({"First": TABLE, "Second": [["Note"], ["nothing here"]]})
    buf = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(raw)) as src, zipfile.ZipFile(buf, "w") as dst:
        for name in src.namelist():
            data = src.read(name)
            if name == "xl/workbook.xml":
                data = data.replace(b'r:id="rId1"', b'r:id="rTMP"')
                data = data.replace(b'r:id="rId2"', b'r:id="rId1"')
                data = data.replace(b'r:id="rTMP"', b'r:id="rId2"')
            dst.writestr(name, data)
    # Both sheets are still present; the scored pick must still find the table.
    assert "10.1/a" in xlsx_to_csv_text(buf.getvalue())


def test_a_cell_containing_the_delimiter_survives():
    rows = [["Title", "DOI"], ["Aspirin, warfarin, and bleeding", "10.1/a"]]
    refs = parse_references(xlsx_to_csv_text(make_xlsx({"S": rows})))
    assert len(refs) == 1
    assert refs[0].title == "Aspirin, warfarin, and bleeding"
    assert refs[0].doi == "10.1/a"


def test_empty_workbook_is_an_error_not_a_silent_empty_run():
    with pytest.raises(OfficeError):
        xlsx_to_csv_text(make_xlsx({"S": []}))


def test_corrupt_zip_raises_officeerror():
    with pytest.raises(OfficeError):
        convert_office_bytes(b"PK\x03\x04 this is not really a zip")


def test_malformed_sheet_xml_raises_officeerror():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("xl/worksheets/sheet1.xml", "<worksheet><sheetData><row>")
    with pytest.raises(OfficeError):
        convert_office_bytes(buf.getvalue())


def test_a_zip_that_is_not_an_office_file_returns_none():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("refs.bib", "@article{a, doi={10.1/a}}")
    assert convert_office_bytes(buf.getvalue()) is None


def test_zip_bomb_is_refused_by_the_declared_size():
    """The expanded size is checked from the directory before anything is read."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("xl/worksheets/sheet1.xml", b"\0" * (80 * 1024 * 1024))
    assert len(buf.getvalue()) < 1024 * 1024  # tiny on disk, huge on expansion
    with pytest.raises(OfficeError) as exc:
        convert_office_bytes(buf.getvalue())
    assert "refusing to read" in str(exc.value)


def test_too_many_members_is_refused():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for i in range(MAX_MEMBERS + 1):
            zf.writestr(f"junk/{i}", "x")
    with pytest.raises(OfficeError):
        convert_office_bytes(buf.getvalue())


def test_xml_entity_expansion_is_not_performed():
    """"Billion laughs" must not expand — xml.etree does expand internal
    entities, so a part declaring a DOCTYPE is refused outright."""
    bomb = (
        '<?xml version="1.0"?><!DOCTYPE lolz [<!ENTITY lol "lol">'
        '<!ENTITY lol2 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;">]>'
        "<worksheet><sheetData><row><c t=\"inlineStr\"><is><t>&lol2;</t></is></c>"
        "</row></sheetData></worksheet>"
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("xl/worksheets/sheet1.xml", bomb)
    with pytest.raises(OfficeError):
        convert_office_bytes(buf.getvalue())


# --- .docx -------------------------------------------------------------------

PARAGRAPHS = [
    "References",
    "1. Kim H, Lee S. Aspirin in acute coronary syndrome. Lancet. 2024;403:11-19. "
    "doi:10.1016/S0140-6736(24)00001-1",
    "2. Park J. Statins and myalgia. BMJ. 2004;328:1-5. https://doi.org/10.1136/bmj.328.7431.1",
]


def test_docx_becomes_one_reference_per_paragraph():
    text, kind, detail = convert_office_bytes(make_docx(PARAGRAPHS))
    assert (kind, detail) == ("docx", "")
    refs = parse_references(text)
    assert [r.doi for r in refs] == [
        None,
        "10.1016/s0140-6736(24)00001-1",
        "10.1136/bmj.328.7431.1",
    ]
    assert all(r.heuristic_fields for r in refs), "a Word list is free text"


def test_docx_with_no_text_is_an_error():
    with pytest.raises(OfficeError):
        docx_to_text(make_docx([]))


def test_docx_tabs_become_spaces_but_a_line_break_starts_a_new_reference():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(
            "word/document.xml",
            f"<w:document {WORD_NS}><w:body><w:p><w:r><w:t>Kim H.</w:t>"
            "<w:tab/><w:t>Lancet.</w:t><w:br/><w:t>2024.</w:t></w:r></w:p>"
            "</w:body></w:document>",
        )
    # A tab is within-reference whitespace; a <w:br/> (Shift+Enter) is how a
    # one-paragraph reference list separates entries, so it must split.
    assert docx_to_text(buf.getvalue()) == "Kim H. Lancet.\n2024."


# --- CLI wiring --------------------------------------------------------------


class FakeClient:
    remote_calls = 0

    def fetch(self, doi):
        return {
            "DOI": doi,
            "title": ["Aspirin in ACS"],
            "issued": {"date-parts": [[2024]]},
            "container-title": ["Lancet"],
            "author": [{"family": "Kim"}],
            "type": "journal-article",
        }

    def resolve(self, doi):
        return True


def test_cli_reads_an_xlsx(tmp_path, capsys):
    path = tmp_path / "included.xlsx"
    path.write_bytes(make_xlsx({"Included": TABLE}))
    code = run([str(path), "--verbose", "--no-color", "--delay", "0"], client=FakeClient())
    captured = capsys.readouterr()
    assert code == 0
    assert "read" in captured.err and "xlsx" in captured.err
    assert "checked 2 references" in captured.out


def test_cli_reads_an_xlsx_with_bilingual_headers(tmp_path, capsys):
    """Headers such as "Title(제목)" and "DOI(링크)" pick the sheet as a reference
    table and map every column, so a swapped DOI is caught."""
    from citecheck.core import CrossrefClient

    record = {
        "DOI": "10.1371/journal.pmed.0020124",
        "title": ["Why Most Published Research Findings Are False"],
        "author": [{"family": "Ioannidis", "given": "John P. A."}],
        "container-title": ["PLoS Medicine"],
        "issued": {"date-parts": [[2005, 8, 30]]},
    }
    client = CrossrefClient(_fetch=lambda d: record if d == record["DOI"] else None,
                            _resolve=lambda d: False)
    rows = [
        ["번호", "Author(저자)", "Year(연도)", "Title(제목)", "Journal(학술지)", "DOI(링크)"],
        ["S1", "Smith J", 2019, "A completely different paper about penguins", "Nature",
         "10.1371/journal.pmed.0020124"],
    ]
    path = tmp_path / "bilingual.xlsx"
    path.write_bytes(make_xlsx({"Sheet1": rows}))
    code = run([str(path), "--json", "--delay", "0"], client=client)
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert len(payload) == 1
    found = {f["code"] for f in payload[0]["findings"]}
    assert {"title-mismatch", "year-mismatch", "author-mismatch",
            "journal-mismatch"} <= found


def test_cli_reads_a_docx(tmp_path, capsys):
    path = tmp_path / "manuscript.docx"
    path.write_bytes(make_docx(PARAGRAPHS))
    run([str(path), "--no-color", "--delay", "0"], client=FakeClient())
    captured = capsys.readouterr()
    assert "docx" in captured.err
    assert "checked 3 references" in captured.out


def test_cli_rejects_a_zip_that_is_not_office(tmp_path, capsys):
    path = tmp_path / "refs.zip"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("refs.bib", "@article{a, doi={10.1/a}}")
    path.write_bytes(buf.getvalue())
    code = run([str(path), "--delay", "0"], client=FakeClient())
    assert code == 2
    assert "looks like a ZIP archive but not an .xlsx or .docx" in capsys.readouterr().err


def test_cli_reports_a_corrupt_office_file_clearly(tmp_path, capsys):
    path = tmp_path / "broken.xlsx"
    path.write_bytes(b"PK\x03\x04not a zip at all")
    code = run([str(path), "--delay", "0"], client=FakeClient())
    assert code == 2
    assert "cannot read" in capsys.readouterr().err


def test_cli_xlsx_with_profile(tmp_path, capsys):
    path = tmp_path / "included.xlsx"
    path.write_bytes(make_xlsx({"Included": TABLE}))
    run([str(path), "--profile", "--as-of", "2026", "--no-color", "--delay", "0"],
        client=FakeClient())
    out = capsys.readouterr().out
    assert "Reference profile" in out
    assert "references            2" in out


# --- regression tests for the 2026-07-31 review round -------------------------


def test_a_banner_row_above_the_header_does_not_destroy_the_column_mapping():
    """Extraction sheets carry a title row above the header. Without trimming it
    became the header: every column mapping was lost, the sheet fell through to
    the free-text parser (which is also the PII-leak path), and the banner and
    header rows were reported as two bogus references."""
    rows = [["Included studies (n=2), extracted by J Kim", "", "", "", "", ""]] + TABLE
    text = xlsx_to_csv_text(make_xlsx({"S": rows}))
    from citecheck.parsers import detect_format

    assert detect_format(text) == "csv"
    refs = parse_references(text)
    assert [r.doi for r in refs] == ["10.1/a", "10.1/b"]
    assert [r.title for r in refs] == ["Aspirin in ACS", "Statins and myalgia"]


def test_a_label_column_cover_sheet_never_beats_the_real_table():
    """A 2-column cover tab whose first cell reads "Title" scores as a perfectly
    good header row — so the sheet must be chosen on the references it yields."""
    cover = [["Title", "Systematic review of aspirin"], ["Author", "Kim H"], ["Date", "2024-01"]]
    real = [["Included studies (n=2)"]] + TABLE
    text, kind, sheet = convert_office_bytes(make_xlsx({"Cover": cover, "Included": real}))
    assert sheet == "Included"
    assert [r.doi for r in parse_references(text)] == ["10.1/a", "10.1/b"]


def test_sheet_can_be_chosen_explicitly_by_name_or_tab_number():
    decoy = [["Title", "DOI"], ["Excluded paper", "10.9/decoy"]]
    raw = make_xlsx({"Excluded": decoy, "Included": TABLE})
    assert "10.9/decoy" in xlsx_to_csv_text(raw, sheet="Excluded")
    assert "10.9/decoy" in xlsx_to_csv_text(raw, sheet="1")
    assert "10.1/a" in xlsx_to_csv_text(raw, sheet="included")  # case-insensitive
    with pytest.raises(OfficeError) as exc:
        xlsx_to_csv_text(raw, sheet="Nope")
    assert "Excluded, Included" in str(exc.value)


def test_a_tie_on_score_goes_to_the_earlier_tab():
    """Tab order is the tie-break, so it must come from workbook.xml."""
    first = [["Title", "DOI"], ["First tab paper", "10.1/first"]]
    second = [["Title", "DOI"], ["Second tab paper", "10.1/second"]]
    assert "10.1/first" in xlsx_to_csv_text(make_xlsx({"A": first, "B": second}))
    assert "10.1/second" in xlsx_to_csv_text(make_xlsx({"B": second, "A": first}))


def test_negative_shared_string_index_yields_an_empty_cell():
    """Python indexes backwards from a negative index without raising, so a
    corrupt "-1" silently returned another cell's text as this cell's."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("xl/sharedStrings.xml", f"<sst {SHEET_NS}><si><t>Title</t></si>"
                    "<si><t>DOI</t></si><si><t>10.1/a</t></si></sst>")
        zf.writestr(
            "xl/worksheets/sheet1.xml",
            f"<worksheet {SHEET_NS}><sheetData>"
            '<row r="1"><c r="A1" t="s"><v>0</v></c><c r="B1" t="s"><v>1</v></c></row>'
            '<row r="2"><c r="A2" t="s"><v>-1</v></c><c r="B2" t="s"><v>99</v></c></row>'
            "</sheetData></worksheet>",
        )
    assert xlsx_to_csv_text(buf.getvalue()) == "Title,DOI\n,\n"


def test_a_missing_shared_string_table_is_an_error_not_a_blank_table():
    """Rendering every t="s" cell as empty turned a full table into a blank one
    that was then reported as a clean check at exit 0."""
    raw = make_xlsx({"S": TABLE})
    buf = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(raw)) as src, zipfile.ZipFile(buf, "w") as dst:
        for name in src.namelist():
            if name != "xl/sharedStrings.xml":
                dst.writestr(name, src.read(name))
    with pytest.raises(OfficeError) as exc:
        convert_office_bytes(buf.getvalue())
    assert "sharedStrings" in str(exc.value)


def test_duplicate_or_backwards_cell_refs_overwrite_instead_of_shifting():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(
            "xl/worksheets/sheet1.xml",
            f'<worksheet {SHEET_NS}><sheetData><row r="1">'
            '<c r="A1" t="inlineStr"><is><t>title</t></is></c>'
            '<c r="B1" t="inlineStr"><is><t>doi</t></is></c></row>'
            '<row r="2"><c r="B2" t="inlineStr"><is><t>10.1/a</t></is></c>'
            '<c r="A2" t="inlineStr"><is><t>A paper</t></is></c>'
            '<c r="B2" t="inlineStr"><is><t>10.1/a</t></is></c></row>'
            "</sheetData></worksheet>",
        )
    assert xlsx_to_csv_text(buf.getvalue()) == "title,doi\nA paper,10.1/a\n"


def test_phonetic_reading_hints_are_not_appended_to_the_title():
    """Excel stores an IME reading in <rPh>; appending it guaranteed a false
    title-mismatch on every Japanese/Korean sheet."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(
            "xl/sharedStrings.xml",
            f"<sst {SHEET_NS}><si><t>title</t></si><si><t>doi</t></si>"
            '<si><t>睡眠障害の臨床試験</t><rPh sb="0" eb="4"><t>スイミンショウガイ</t></rPh></si>'
            "<si><t>10.1/a</t></si></sst>",
        )
        zf.writestr(
            "xl/worksheets/sheet1.xml",
            f"<worksheet {SHEET_NS}><sheetData>"
            '<row r="1"><c r="A1" t="s"><v>0</v></c><c r="B1" t="s"><v>1</v></c></row>'
            '<row r="2"><c r="A2" t="s"><v>2</v></c><c r="B2" t="s"><v>3</v></c></row>'
            "</sheetData></worksheet>",
        )
    assert xlsx_to_csv_text(buf.getvalue()) == "title,doi\n睡眠障害の臨床試験,10.1/a\n"


def test_too_many_rows_is_an_error_rather_than_a_silent_partial_read(monkeypatch):
    import citecheck.office as office

    monkeypatch.setattr(office, "MAX_ROWS", 2)
    with pytest.raises(OfficeError) as exc:
        xlsx_to_csv_text(make_xlsx({"S": TABLE}))
    assert "partial" in str(exc.value)


def test_an_oversized_member_is_refused():
    """Reachable below the whole-archive cap: one 33 MB member, 40 MB total."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("xl/worksheets/sheet1.xml", b"<worksheet>" + b" " * (33 * 1024 * 1024))
    with pytest.raises(OfficeError) as exc:
        convert_office_bytes(buf.getvalue())
    assert "limit" in str(exc.value)


def test_an_empty_zip_is_recognised_as_a_zip():
    """An empty archive starts PK\\x05\\x06, not PK\\x03\\x04 — it used to fall
    through to the text parser and be 'checked' as a reference named 'PK'."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w"):
        pass
    assert looks_like_zip(buf.getvalue())
    assert convert_office_bytes(buf.getvalue()) is None


def test_office_error_messages_cannot_carry_terminal_escapes():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("xl/worksheets/sheet\x1b[31mPWNED\x1b[0m1.xml", "<worksheet><row>")
    with pytest.raises(OfficeError) as exc:
        convert_office_bytes(buf.getvalue())
    assert "\x1b" not in str(exc.value) and "PWNED" in str(exc.value)


def test_binary_files_are_named_rather_than_parsed_as_mojibake():
    from citecheck.office import binary_input_hint

    assert "legacy Office file" in binary_input_hint(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1rest")
    assert "PDF" in binary_input_hint(b"%PDF-1.7\n1 0 obj")
    assert "binary file" in binary_input_hint(b"ab\x00cd")
    assert binary_input_hint(b"@article{a, doi={10.1/a}}") is None
    assert binary_input_hint("Kim H. Lancet. 2024.".encode("utf-16")) is None


def test_docx_footnotes_and_endnotes_are_read():
    """A footnote-cited manuscript used to be checked as zero references."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(
            "word/document.xml",
            f"<w:document {WORD_NS}><w:body><w:p><w:r><w:t>Introduction.</w:t>"
            "</w:r></w:p></w:body></w:document>",
        )
        zf.writestr(
            "word/footnotes.xml",
            f"<w:footnotes {WORD_NS}><w:footnote><w:p><w:r><w:t>Kim H. Lancet. "
            "2024. doi:10.1016/j.sleep.2021.01.001</w:t></w:r></w:p></w:footnote>"
            "</w:footnotes>",
        )
    refs = parse_references(docx_to_text(buf.getvalue()))
    assert [r.doi for r in refs] == [None, "10.1016/j.sleep.2021.01.001"]


def test_a_line_break_separated_reference_list_keeps_every_doi():
    """Three references in ONE paragraph, Shift+Enter separated: treating the
    break as a space kept only the first DOI and reported a clean pass."""
    runs = "<w:br/>".join(
        f"<w:t>Ref {i}. doi:10.1016/j.test.2024.0{i}</w:t>" for i in (1, 2, 3)
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(
            "word/document.xml",
            f"<w:document {WORD_NS}><w:body><w:p><w:r>{runs}</w:r></w:p>"
            "</w:body></w:document>",
        )
    refs = parse_references(docx_to_text(buf.getvalue()))
    assert [r.doi for r in refs] == [
        "10.1016/j.test.2024.01",
        "10.1016/j.test.2024.02",
        "10.1016/j.test.2024.03",
    ]


def test_a_text_box_paragraph_is_read_once_and_not_welded_to_its_neighbour():
    """Word duplicates shape content under mc:Fallback, and gluing the copies
    together invented a DOI ("…fignote" + "Figure…") reported as broken."""
    inner = (
        "<w:p><w:r><w:t>Figure note doi:10.1016/j.inner.2024.01</w:t></w:r></w:p>"
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(
            "word/document.xml",
            f"<w:document {WORD_NS} "
            'xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006">'
            "<w:body><w:p><w:r><w:t>Outer doi:10.1016/j.outer.2024.01</w:t></w:r>"
            f"<mc:AlternateContent><mc:Choice>{inner}</mc:Choice>"
            f"<mc:Fallback>{inner}</mc:Fallback></mc:AlternateContent>"
            "</w:p></w:body></w:document>",
        )
    refs = parse_references(docx_to_text(buf.getvalue()))
    assert [r.doi for r in refs] == ["10.1016/j.outer.2024.01", "10.1016/j.inner.2024.01"]


def test_cli_reports_a_legacy_binary_file_instead_of_checking_mojibake(tmp_path, capsys):
    path = tmp_path / "old.xls"
    path.write_bytes(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 512)
    assert run([str(path), "--delay", "0"], client=FakeClient()) == 2
    err = capsys.readouterr().err
    assert "legacy Office file" in err and "save as" in err


def test_cli_xlsx_is_parsed_as_a_table_never_as_free_text(tmp_path, capsys):
    """A clinical workbook that the CSV *detector* would reject must still be
    parsed by column, because the free-text path sends the whole row — MRNs,
    notes — to Crossref when --suggest-doi is on."""
    rows = [
        ["Study ID", "MRN", "Clinical notes", "Title", "Year", "DOI"],
        ["S-001", "4429981", "subject 88213 relapsed, PHQ9=19", "Aspirin in ACS", 2024, ""],
    ]
    path = tmp_path / "extraction.xlsx"
    path.write_bytes(make_xlsx({"Data": rows}))

    searched = []

    class RecordingClient(FakeClient):
        def search(self, query):
            searched.append(query)
            return []

    run([str(path), "--suggest-doi", "--no-color", "--delay", "0"], client=RecordingClient())
    capsys.readouterr()
    assert searched, "a DOI-less reference should still be searched for"
    for query in searched:
        assert "4429981" not in query and "PHQ9" not in query and "S-001" not in query
        assert query.startswith("Aspirin in ACS")


def test_cli_says_so_when_a_workbook_holds_no_reference_table(tmp_path, capsys):
    path = tmp_path / "cover.xlsx"
    path.write_bytes(make_xlsx({"Cover": [["PRISMA counts"], ["Records identified", 412]]}))
    assert run([str(path), "--delay", "0"], client=FakeClient()) == 2
    err = capsys.readouterr().err
    assert "no reference table found" in err and "--sheet" in err


def test_cli_sheet_option_and_note_name_the_worksheet(tmp_path, capsys):
    path = tmp_path / "wb.xlsx"
    path.write_bytes(make_xlsx({"Excluded": [["Title", "DOI"], ["Nope", "10.9/x"]],
                                "Included": TABLE}))
    run([str(path), "--sheet", "Excluded", "--no-color", "--delay", "0"], client=FakeClient())
    captured = capsys.readouterr()
    assert "'Excluded'" in captured.err
    assert "checked 1 references" in captured.out


def test_cli_encoding_is_reported_as_ignored_for_office_input(tmp_path, capsys):
    path = tmp_path / "wb.xlsx"
    path.write_bytes(make_xlsx({"S": TABLE}))
    run([str(path), "--encoding", "latin-1", "--no-color", "--delay", "0"], client=FakeClient())
    assert "--encoding does not apply" in capsys.readouterr().err
