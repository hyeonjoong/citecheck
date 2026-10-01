"""Read reference lists out of Office files (.xlsx / .docx), standard library only.

Why this exists: clinical and pharma reference lists very often do not live in a
reference manager at all. They live in **Excel** — a screening/included-studies
table from Covidence, Rayyan, or a hand-kept sheet — and in **Word**, as the
reference list of the manuscript itself. Asking the researcher to first export
those to CSV or to a .bib is exactly the friction that stops a citation check
from ever being run.

Both formats are ZIP containers of XML, so both are readable with ``zipfile`` +
``xml.etree`` and no third-party dependency (the tool ships with none, and a
clinician on a locked-down hospital machine often cannot install one).

* ``.xlsx`` → converted to CSV text and parsed by the existing CSV table parser,
  so every column-name alias and DOI-in-a-Notes-column rescue keeps working.
  The sheet is chosen by *how many real references it yields*, and rows above
  the header row (a banner such as "Included studies (n=42)") are dropped, so a
  workbook that opens on a cover tab still reads correctly. ``--sheet``
  overrides the choice.
* ``.docx`` → converted to one line per paragraph — including footnotes and
  endnotes, which is where many manuscripts keep their citations — and parsed as
  free text.

Everything here is defensive about the file it is handed: an .xlsx is untrusted
input (it may have arrived by email), so member reads are size-capped, the
member count and the expanded size are capped, and any part declaring a DOCTYPE
is refused (``xml.etree`` *does* expand entities declared in an internal DTD
subset — the "billion laughs" attack; no legitimate OOXML part carries a
DOCTYPE). Every failure surfaces as an :class:`OfficeError`, never a traceback.
"""

from __future__ import annotations

import csv
import io
import re
import zipfile
from typing import Optional
from xml.etree import ElementTree as ET

# ZIP record signatures. A *non-empty* archive starts with a local file header
# (PK\x03\x04), but an empty one starts with the end-of-central-directory record
# (PK\x05\x06) and a spanned one with PK\x07\x08 — checking only the first meant
# an empty .xlsx fell through to the text parser and was "checked" as one
# reference named "PK", at exit 0.
_ZIP_SIGNATURES = (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")

# Magic numbers of the binary formats a user most plausibly drops in by mistake.
# Without this they decode as latin-1 mojibake and are "checked" as references at
# exit 0 — the tool's one unforgivable failure mode. Each is mapped to the advice
# that actually unblocks the user.
_BINARY_MAGIC = (
    (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1", "a legacy Office file (.doc/.xls/.ppt)",
     "open it in Word/Excel and save as .docx/.xlsx"),
    (b"%PDF-", "a PDF",
     "copy the reference list into a .txt file, or export it from your reference manager"),
    (b"{\\rtf", "an RTF document", "save it as .docx or .txt"),
    (b"\x89PNG", "a PNG image", "citecheck reads reference lists, not images"),
    (b"\xff\xd8\xff", "a JPEG image", "citecheck reads reference lists, not images"),
)

# Caps on what a single Office file may cost us. A 30 MB spreadsheet is already
# far beyond any reference table; the point is that a *compressed* 40 KB file
# must not be able to expand into gigabytes ("zip bomb") inside a citation check.
MAX_MEMBER_BYTES = 32 * 1024 * 1024
MAX_TOTAL_BYTES = 64 * 1024 * 1024
MAX_MEMBERS = 4096
# Row/column caps: a reference table is thousands of rows at most, and a runaway
# `r="XFD1048576"` cell reference must not make us materialise a giant row.
MAX_ROWS = 200_000
MAX_COLS = 1024
# How much of a name/message from inside the archive we are willing to echo.
_MAX_ECHO = 120


class OfficeError(ValueError):
    """The bytes look like an Office file but could not be read as one."""


def looks_like_zip(raw: bytes) -> bool:
    """True if *raw* begins with any ZIP record signature."""
    return raw[:4] in _ZIP_SIGNATURES


def binary_input_hint(raw: bytes) -> Optional[str]:
    """Describe *raw* if it is a binary format citecheck cannot read, else None.

    The returned text is a complete sentence for the user, naming the format and
    the one action that fixes it. Without this a .doc/.xls/.pdf decoded as
    latin-1 mojibake and was reported as a checked reference at exit 0.
    """
    for magic, name, advice in _BINARY_MAGIC:
        if raw.startswith(magic):
            return f"this is {name}, which citecheck cannot read — {advice}"
    # A UTF-16 file legitimately contains NUL bytes; it is BOM-detected in the
    # decoder, so only *un-marked* NULs indicate binary content here.
    if raw[:2] not in (b"\xff\xfe", b"\xfe\xff") and b"\x00" in raw[:4096]:
        return (
            "this looks like a binary file (it contains NUL bytes), not a text "
            "reference list — export your references to .bib/.ris/.csv/.txt first"
        )
    return None


def _echo(text) -> str:
    """Make text from inside an untrusted archive safe to print, and short.

    Member names are attacker-controlled and land in an error message on the
    user's terminal, so control characters (ANSI escapes) are stripped here
    rather than trusted to a caller remembering to do it.
    """
    from .core import sanitize_text

    cleaned = " ".join(sanitize_text(str(text)).split())
    return cleaned[:_MAX_ECHO] + ("…" if len(cleaned) > _MAX_ECHO else "")


def _local(tag: str) -> str:
    """The local name of a namespaced XML tag (``{ns}row`` -> ``row``)."""
    return tag.rpartition("}")[2]


def _read_member(zf: zipfile.ZipFile, name: str) -> bytes:
    """Read one ZIP member with a hard size cap (never trusts the header)."""
    try:
        with zf.open(name) as fh:
            data = fh.read(MAX_MEMBER_BYTES + 1)
    except (KeyError, RuntimeError, zipfile.BadZipFile, OSError, EOFError) as e:
        raise OfficeError(f"could not read {_echo(name)} ({type(e).__name__})") from e
    if len(data) > MAX_MEMBER_BYTES:
        raise OfficeError(f"{_echo(name)} is larger than the {MAX_MEMBER_BYTES} byte limit")
    return data


# A DOCTYPE declaration in an Office part — see the module docstring.
_DOCTYPE_RE = re.compile(rb"<!DOCTYPE", re.IGNORECASE)


def _parse_xml(data: bytes, what: str) -> ET.Element:
    if _DOCTYPE_RE.search(data):
        raise OfficeError(
            f"{_echo(what)} declares a DOCTYPE — refusing to parse (entity expansion)"
        )
    try:
        return ET.fromstring(data)
    except ET.ParseError as e:
        raise OfficeError(f"malformed XML in {_echo(what)}: {_echo(e)}") from e


def _open_zip(raw: bytes) -> zipfile.ZipFile:
    try:
        zf = zipfile.ZipFile(io.BytesIO(raw))
    except (zipfile.BadZipFile, OSError, EOFError) as e:
        raise OfficeError(f"not a readable ZIP container ({type(e).__name__})") from e
    infos = zf.infolist()
    if len(infos) > MAX_MEMBERS:
        raise OfficeError(f"archive has {len(infos)} members (limit {MAX_MEMBERS})")
    total = sum(max(0, i.file_size) for i in infos)
    if total > MAX_TOTAL_BYTES:
        raise OfficeError(
            f"archive expands to {total} bytes (limit {MAX_TOTAL_BYTES}) — refusing to read"
        )
    return zf


# --- .xlsx -------------------------------------------------------------------

_CELL_REF_RE = re.compile(r"^([A-Z]+)(\d+)$")
# Subtrees whose <t> text is NOT cell content: `rPh` is the phonetic reading
# Excel stores for IME-entered CJK text ("睡眠障害" + "スイミンショウガイ"). Collecting it
# appended the reading to the title and guaranteed a false title-mismatch on
# Japanese/Korean sheets — the audience this tool is written for.
_SKIP_TEXT_TAGS = {"rPh", "phoneticPr"}


def _text_of(node: ET.Element) -> str:
    """Concatenate the ``<t>`` runs under *node*, skipping phonetic hints."""
    parts: list = []

    def walk(el):
        for child in el:
            name = _local(child.tag)
            if name in _SKIP_TEXT_TAGS:
                continue
            if name == "t":
                parts.append(child.text or "")
            walk(child)

    if _local(node.tag) == "t":
        return node.text or ""
    walk(node)
    return "".join(parts)


def _column_index(ref: str) -> Optional[int]:
    """Zero-based column index from a cell reference ("A1" -> 0, "AB7" -> 27)."""
    m = _CELL_REF_RE.match((ref or "").strip().upper())
    if not m:
        return None
    idx = 0
    for ch in m.group(1):
        idx = idx * 26 + (ord(ch) - 64)
    return idx - 1


def _shared_strings(zf: zipfile.ZipFile) -> Optional[list]:
    """The workbook's shared-string table, or None when the part is absent.

    None and ``[]`` mean different things and must not be conflated: a workbook
    with no ``sharedStrings.xml`` whose cells nonetheless say ``t="s"`` is
    corrupt, and silently rendering every such cell as empty turned a full
    reference table into a blank one that was then reported as a clean
    2-reference check at exit 0.
    """
    if "xl/sharedStrings.xml" not in set(zf.namelist()):
        return None
    root = _parse_xml(_read_member(zf, "xl/sharedStrings.xml"), "sharedStrings.xml")
    return [_text_of(si) for si in root]


def _sheet_parts(zf: zipfile.ZipFile) -> list:
    """(path, sheet name) for each worksheet, in the workbook's own tab order.

    Order matters: "the first sheet" must mean the first *tab* the researcher
    sees, not ``sheet1.xml`` — Excel does not renumber the parts when tabs are
    reordered, so the two disagree in any workbook that has been rearranged.
    """
    names = set(zf.namelist())
    ordered: list = []
    if "xl/workbook.xml" in names and "xl/_rels/workbook.xml.rels" in names:
        try:
            wb = _parse_xml(_read_member(zf, "xl/workbook.xml"), "workbook.xml")
            rels = _parse_xml(
                _read_member(zf, "xl/_rels/workbook.xml.rels"), "workbook.xml.rels"
            )
        except OfficeError:
            wb = rels = None
        if wb is not None and rels is not None:
            targets = {}
            for rel in rels:
                rid = rel.get("Id")
                target = rel.get("Target") or ""
                if rid and target:
                    path = target[1:] if target.startswith("/") else "xl/" + target.lstrip("./")
                    targets[rid] = path.replace("//", "/")
            for sheet in wb.iter():
                if _local(sheet.tag) != "sheet":
                    continue
                rid = next((v for k, v in sheet.attrib.items() if _local(k) == "id"), None)
                path = targets.get(rid)
                if path in names and path not in [p for p, _n in ordered]:
                    ordered.append((path, sheet.get("name") or path))
    if ordered:
        return ordered
    # Fallback: whatever worksheet parts exist, numerically ordered.
    sheets = [
        n for n in zf.namelist() if n.startswith("xl/worksheets/sheet") and n.endswith(".xml")
    ]

    def _num(name: str) -> tuple:
        m = re.search(r"(\d+)\.xml$", name)
        return (int(m.group(1)) if m else 10**9, name)

    return [(p, p.rsplit("/", 1)[-1]) for p in sorted(sheets, key=_num)]


def _cell_text(cell: ET.Element, shared: Optional[list]) -> str:
    """The displayed text of one ``<c>`` cell."""
    ctype = cell.get("t") or "n"
    if ctype == "inlineStr":
        return _text_of(cell)
    value = next((v.text or "" for v in cell if _local(v.tag) == "v"), "")
    if ctype == "s":  # shared-string index
        if shared is None:
            raise OfficeError(
                "the sheet uses shared strings but the workbook has no "
                "xl/sharedStrings.xml — the file is corrupt or incomplete"
            )
        try:
            idx = int(value)
        except ValueError:
            return ""
        # `0 <= idx` is not pedantry: Python indexes backwards from a negative
        # index without raising, so a corrupt "-3" silently returned some other
        # cell's text as if it were this cell's.
        return shared[idx] if 0 <= idx < len(shared) else ""
    if ctype == "e":  # an error cell (#REF!, #N/A) — no usable value
        return ""
    if ctype in ("str", "b"):
        return value
    # Numeric. Excel stores 2019 as "2019" but a formula result as "2019.0";
    # rendering the float form would break the year column and any DOI-in-a-
    # number cell, so integral floats are rendered as integers.
    if value:
        try:
            f = float(value)
        except ValueError:
            return value
        if f.is_integer() and abs(f) < 1e15:
            return str(int(f))
    return value


def _sheet_rows(zf: zipfile.ZipFile, path: str, shared: Optional[list]) -> list:
    """One worksheet as a list of row-lists, with sparse cells filled in.

    Excel omits empty cells entirely, so a row is rebuilt from each cell's ``r``
    reference. Without that, a blank DOI cell would shift every later column left
    and the whole table would be read against the wrong headers.
    """
    root = _parse_xml(_read_member(zf, path), path)
    rows: list = []
    for row in root.iter():
        if _local(row.tag) != "row":
            continue
        if len(rows) >= MAX_ROWS:
            # Silently truncating would report a partial check as a complete one.
            raise OfficeError(
                f"{_echo(path)} has more than {MAX_ROWS} rows — refusing to read a partial table"
            )
        values: list = []
        next_idx = 0
        for cell in row:
            if _local(cell.tag) != "c":
                continue
            idx = _column_index(cell.get("r") or "")
            if idx is None:
                idx = next_idx
            if idx >= MAX_COLS:
                continue
            while len(values) < idx:
                values.append("")
            text = _cell_text(cell, shared)
            if idx < len(values):
                # A duplicate or out-of-order `r` (buggy third-party writers emit
                # them) must overwrite its own column, not append — appending
                # shifted every later column and put the DOI under the wrong
                # header.
                values[idx] = text
            else:
                values.append(text)
            next_idx = max(next_idx, idx + 1)
        rows.append(values)
    return rows


def _rows_to_csv(rows: list) -> str:
    """Render rows as CSV text, padded to a rectangle.

    Padding matters: ``parsers.looks_like_csv`` requires a rectangular shape, and
    Excel's own rows are ragged (trailing empty cells are simply absent).
    """
    width = max((len(r) for r in rows), default=0)
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    for row in rows:
        # Newlines inside a cell (alt-enter) survive CSV quoting fine, but a
        # stray \r would confuse the reader — normalise to a space.
        writer.writerow([(c or "").replace("\r", " ") for c in row] + [""] * (width - len(row)))
    return buf.getvalue()


def _trim_to_header(rows: list) -> list:
    """Drop the rows above the header row.

    Extraction sheets almost always carry a banner ("Included studies (n=42)",
    "Table 2. Characteristics of included trials") above the real header. The CSV
    parser takes the first non-empty row as the header, so that banner used to
    *become* the header: every column mapping was lost, the table fell through to
    the free-text parser, and the banner and header rows were themselves reported
    as two bogus references.
    """
    from .parsers import _CSV_REQUIRED_ANY, _csv_header_fields

    for i, row in enumerate(rows):
        if not any((c or "").strip() for c in row):
            continue
        fields = _csv_header_fields(row)
        if any(f in fields for f in _CSV_REQUIRED_ANY):
            return rows[i:]
    return rows


def _sheet_score(csv_text: str) -> tuple:
    """How much this sheet looks like a reference table (higher is better).

    Scored on what the sheet actually *yields*, not on what its header looks
    like: the number of references carrying a DOI or PMID dominates, then the
    number of references, then how many columns were recognised. A cover /
    PRISMA-counts / patient-list tab yields no identifiers and so can never beat
    a real reference table — which header-shape scoring got wrong, because a
    two-column cover sheet whose first cell happens to read "Title" scores as a
    perfectly good header.
    """
    from .parsers import _csv_header_fields, parse_csv

    refs = parse_csv(csv_text)
    header = next(
        (r for r in csv.reader(io.StringIO(csv_text)) if any((c or "").strip() for c in r)),
        [],
    )
    with_id = sum(1 for r in refs if r.doi or r.pmid)
    return (with_id, len(refs), len(_csv_header_fields(header)))


def xlsx_sheets(raw: bytes) -> list:
    """The workbook's sheet names, in tab order (for error messages / --sheet)."""
    return [name for _path, name in _sheet_parts(_open_zip(raw))]


def xlsx_to_csv(raw: bytes, sheet: Optional[str] = None) -> tuple:
    """Convert .xlsx bytes to (CSV text, sheet name).

    *sheet* selects a sheet by name (case-insensitive) or by 1-based tab number;
    without it, the sheet that yields the most identifiable references wins, ties
    going to the earlier tab.
    """
    zf = _open_zip(raw)
    shared = _shared_strings(zf)
    parts = _sheet_parts(zf)
    if not parts:
        raise OfficeError("no worksheets found in the workbook")

    if sheet:
        wanted = sheet.strip()
        chosen = [(p, n) for p, n in parts if n.strip().lower() == wanted.lower()]
        if not chosen and wanted.isdigit() and 1 <= int(wanted) <= len(parts):
            chosen = [parts[int(wanted) - 1]]
        if not chosen:
            available = ", ".join(_echo(n) for _p, n in parts)
            raise OfficeError(f"no sheet named {_echo(wanted)} — the workbook has: {available}")
        path, name = chosen[0]
        return _rows_to_csv(_trim_to_header(_sheet_rows(zf, path, shared))), name

    best_text, best_name, best_score = None, None, (-1, -1, -1)
    for path, name in parts:
        rows = _trim_to_header(_sheet_rows(zf, path, shared))
        if not any(any((c or "").strip() for c in row) for row in rows):
            continue
        text = _rows_to_csv(rows)
        score = _sheet_score(text)
        if score > best_score:
            best_text, best_name, best_score = text, name, score
    if best_text is None:
        raise OfficeError("the workbook has no non-empty sheet")
    return best_text, best_name


def xlsx_to_csv_text(raw: bytes, sheet: Optional[str] = None) -> str:
    """Convert .xlsx bytes to CSV text (see :func:`xlsx_to_csv`)."""
    return xlsx_to_csv(raw, sheet)[0]


# --- .docx -------------------------------------------------------------------

# Word writes shape/text-box content twice — once under mc:Choice for modern
# readers and once under mc:Fallback for old ones. Reading both duplicated every
# figure caption and (because the copies were concatenated with no separator)
# welded two DOIs into one non-existent DOI, which was then reported to the
# author as a broken link they had never written.
_SKIP_DOC_TAGS = {"Fallback", "instrText", "delText"}
# Parts a manuscript keeps citations in. A footnote-cited manuscript used to be
# checked as zero references at exit 0 — a silent false clean pass.
_DOC_PARTS = ("word/document.xml", "word/footnotes.xml", "word/endnotes.xml")


def _paragraph_text(para: ET.Element) -> list:
    """The lines of one Word paragraph (a ``w:br`` starts a new line).

    A soft line break is how a reference list written as a *single* paragraph
    separates its entries (Shift+Enter). Treating it as a space merged the whole
    list into one "reference", and the free-text parser then kept only the first
    DOI — every later reference silently vanished, at exit 0. Splitting instead
    can at worst break one reference in two, which is visible and harmless.
    """
    lines: list = []
    parts: list = []

    def flush():
        text = " ".join("".join(parts).split())
        if text:
            lines.append(text)
        parts.clear()

    def walk(el):
        for child in el:
            name = _local(child.tag)
            if name in _SKIP_DOC_TAGS:
                continue
            if name == "t":
                parts.append(child.text or "")
                continue
            if name == "tab":
                parts.append(" ")
                continue
            if name == "br":
                flush()
                continue
            if name == "p":
                # A nested paragraph (text box, or a table inside a shape): emit
                # it as its own line rather than gluing its text onto ours.
                flush()
                lines.extend(_paragraph_text(child))
                continue
            walk(child)

    walk(para)
    flush()
    return lines


def _document_lines(root: ET.Element) -> list:
    """Every top-level paragraph of a Word part, as lines."""
    lines: list = []

    def walk(el):
        for child in el:
            name = _local(child.tag)
            if name in _SKIP_DOC_TAGS:
                continue
            if name == "p":
                lines.extend(_paragraph_text(child))
                continue  # nested paragraphs are handled inside _paragraph_text
            walk(child)

    walk(root)
    return lines


def docx_to_text(raw: bytes) -> str:
    """Convert .docx bytes to plain text — one line per Word paragraph.

    One paragraph per line is exactly the shape the free-text parser wants: a
    Word reference list is one paragraph per reference (numbered or not).
    Footnotes and endnotes are read too, since many manuscripts cite there.

    Known limitation, stated rather than papered over: a DOI that exists *only*
    as a hyperlink target with different display text ("[Link]") is not
    recovered, because the URL lives in the relationships part, not in the
    paragraph. In practice reference lists print the DOI as text.
    """
    zf = _open_zip(raw)
    names = set(zf.namelist())
    if "word/document.xml" not in names:
        raise OfficeError("not a Word document (no word/document.xml)")
    lines: list = []
    for part in _DOC_PARTS:
        if part not in names:
            continue
        lines.extend(_document_lines(_parse_xml(_read_member(zf, part), part)))
    if not lines:
        raise OfficeError("the document contains no text")
    return "\n".join(lines)


def convert_office_bytes(raw: bytes, sheet: Optional[str] = None) -> Optional[tuple]:
    """Convert .xlsx/.docx bytes to text. Returns (text, kind, detail) or None.

    ``detail`` names the worksheet that was read (empty for .docx). ``None``
    means "this ZIP is not an Office document we handle" — the caller then
    reports it as unreadable input rather than guessing.
    """
    if not looks_like_zip(raw):
        return None
    zf = _open_zip(raw)
    names = set(zf.namelist())
    if any(n.startswith("xl/worksheets/sheet") for n in names) or "xl/workbook.xml" in names:
        text, sheet_name = xlsx_to_csv(raw, sheet)
        return text, "xlsx", sheet_name
    if "word/document.xml" in names:
        return docx_to_text(raw), "docx", ""
    return None
