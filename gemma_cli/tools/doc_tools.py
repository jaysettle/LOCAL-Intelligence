#!/usr/bin/env python3
"""
Document tools: `read_document` extracts text from the binary and structured
formats that `read_file` cannot read (PDF, Word, Excel, PowerPoint, OpenDocument,
RTF, EPUB, email, notebooks, CSV, HTML).

Everything runs locally — no cloud parser, no OCR service, nothing leaves the
machine. Each format has either a small permissively-licensed backend (pypdf,
python-docx, openpyxl, python-pptx, striprtf, xlrd) or is handled with the
standard library alone (OpenDocument, EPUB, .eml, .ipynb, CSV, HTML).

Two ideas shape the design:

1. **Format is decided by content, not by extension.** Files get renamed and
   mislabelled constantly (a .docx saved as .doc, a .csv called .xls). `_sniff`
   reads the magic bytes, and for ZIP containers looks *inside* for the marker
   entry, so the right extractor runs regardless of the name.
2. **Output is structured and capped.** Extractors return labelled sections
   (page / sheet / slide / part) so the model can cite a location and page
   through a long document with offset+limit, and the total is capped by
   max_chars — a 12B model with a 32K context cannot be handed a 400-page PDF.

Backends are imported lazily inside each extractor so a missing optional
dependency degrades to one clear message about one format instead of breaking
the whole tool.
"""

import csv
import io
import json
import os
import re
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

# A section is (label, text) — e.g. ("page 3", "..."), ("sheet 'Q1'", "...").
Section = Tuple[str, str]

_DEFAULT_MAX_CHARS = 20000
_HARD_MAX_CHARS = 200000
_PART_CHARS = 2000       # flowing formats are grouped into pseudo-pages this big
_MAX_ROWS = 200          # per sheet / CSV, before truncating
_MAX_CELL = 200          # per cell, before truncating

# Extensions we claim to handle, for read_file to redirect on and for the error
# message. Kept in one place so the tool description and reality cannot drift.
SUPPORTED_EXTS = (
    ".pdf",
    ".docx", ".docm", ".doc",
    ".xlsx", ".xlsm", ".xls",
    ".pptx", ".pptm", ".ppt",
    ".odt", ".ods", ".odp",
    ".rtf", ".epub", ".eml", ".ipynb",
    ".csv", ".tsv",
    ".htm", ".html", ".xhtml",
)

# Formats that are binary (or effectively unreadable) as plain text. read_file
# redirects these here instead of handing the model a screen of mojibake.
BINARY_DOC_EXTS = (
    ".pdf",
    ".docx", ".docm", ".doc",
    ".xlsx", ".xlsm", ".xls",
    ".pptx", ".pptm", ".ppt",
    ".odt", ".ods", ".odp",
    ".rtf", ".epub",
)


class DocError(Exception):
    """Raised by an extractor with a message meant for the model to read."""


# ---------------------------------------------------------------------------
# Format detection
# ---------------------------------------------------------------------------

def _zip_kind(path: Path) -> Optional[str]:
    """Identify an OOXML / OpenDocument / EPUB container by its entries."""
    try:
        with zipfile.ZipFile(path) as z:
            names = set(z.namelist())
            mimetype = ""
            if "mimetype" in names:
                try:
                    mimetype = z.read("mimetype").decode("ascii", "ignore").strip()
                except Exception:
                    mimetype = ""
    except Exception:
        return None

    if mimetype.startswith("application/vnd.oasis.opendocument."):
        tail = mimetype.rsplit(".", 1)[-1]
        return {"text": "odt", "spreadsheet": "ods", "presentation": "odp"}.get(tail, "odf")
    if mimetype == "application/epub+zip" or "META-INF/container.xml" in names:
        return "epub"
    if "word/document.xml" in names:
        return "docx"
    if "xl/workbook.xml" in names:
        return "xlsx"
    if "ppt/presentation.xml" in names:
        return "pptx"
    if "content.xml" in names and "META-INF/manifest.xml" in names:
        return "odf"
    return None


def _sniff(path: Path) -> Optional[str]:
    """Best-effort format from the file's leading bytes (and zip contents)."""
    try:
        with open(path, "rb") as f:
            head = f.read(8)
    except Exception:
        return None

    if head.startswith(b"%PDF-"):
        return "pdf"
    if head.startswith(b"{\\rt"):
        return "rtf"
    if head.startswith(b"PK\x03\x04"):
        return _zip_kind(path)
    if head.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):
        return "ole"  # legacy Office (.doc/.xls/.ppt 97-2003) — needs conversion
    return None


def _resolve_kind(path: Path) -> Tuple[str, str]:
    """Return (kind, note). `note` warns when content contradicts the extension."""
    ext = path.suffix.lower()
    by_ext = {
        ".pdf": "pdf",
        ".docx": "docx", ".docm": "docx",
        ".xlsx": "xlsx", ".xlsm": "xlsx",
        ".pptx": "pptx", ".pptm": "pptx",
        ".doc": "ole", ".xls": "xls", ".ppt": "ole",
        ".odt": "odt", ".ods": "ods", ".odp": "odp",
        ".rtf": "rtf", ".epub": "epub", ".eml": "eml", ".ipynb": "ipynb",
        ".csv": "csv", ".tsv": "csv",
        ".htm": "html", ".html": "html", ".xhtml": "html",
    }.get(ext)

    sniffed = _sniff(path)

    # Content wins over the name, except where sniffing is uninformative or the
    # container is ambiguous (an .xls that is really an OLE file, say).
    if sniffed and sniffed != "ole" and by_ext and sniffed != by_ext:
        return sniffed, f"note: the extension says {ext} but the content is {sniffed}; read as {sniffed}."
    if sniffed and not by_ext:
        return sniffed, f"note: no known extension, detected {sniffed} from the file content."
    if by_ext:
        return by_ext, ""
    if sniffed:
        return sniffed, ""
    return "text", ""


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _clean(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\xa0", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n[ \t]+", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _group_parts(blocks: List[str], size: int = _PART_CHARS) -> List[Section]:
    """Group flowing text blocks into labelled pseudo-pages so offset/limit work."""
    sections: List[Section] = []
    buf: List[str] = []
    length = 0
    for block in blocks:
        block = block.rstrip()
        if not block:
            continue
        buf.append(block)
        length += len(block) + 1
        if length >= size:
            sections.append((f"part {len(sections) + 1}", "\n".join(buf)))
            buf, length = [], 0
    if buf:
        sections.append((f"part {len(sections) + 1}", "\n".join(buf)))
    return sections or [("part 1", "")]


def _rows_to_text(rows: List[List[Any]]) -> Tuple[str, int]:
    """Render tabular rows as pipe-separated lines. Returns (text, rows_used)."""
    out = []
    for row in rows[:_MAX_ROWS]:
        cells = []
        for c in row:
            s = "" if c is None else str(c)
            s = s.replace("\n", " ").strip()
            cells.append(s[:_MAX_CELL] + "…" if len(s) > _MAX_CELL else s)
        while cells and cells[-1] == "":
            cells.pop()
        if cells:
            out.append(" | ".join(cells))
    return "\n".join(out), min(len(rows), _MAX_ROWS)


def _xml_text(xml_bytes: bytes, para_tags: Tuple[str, ...], cell_tags: Tuple[str, ...] = ()) -> List[str]:
    """Walk an XML document collecting text, breaking at paragraph-ish tags.

    Namespace-agnostic: tags are compared on their local name, so this works for
    OpenDocument content.xml without hard-coding the ODF namespace URIs.
    """
    import xml.etree.ElementTree as ET

    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as e:
        raise DocError(f"malformed XML inside the document: {e}")

    blocks: List[str] = []
    current: List[str] = []

    def local(tag: str) -> str:
        return tag.rsplit("}", 1)[-1] if "}" in tag else tag

    def flush():
        if current:
            joined = "".join(current).strip()
            if joined:
                blocks.append(joined)
            current.clear()

    def walk(el):
        name = local(el.tag)
        is_para = name in para_tags
        if is_para:
            flush()
        if el.text:
            current.append(el.text)
        for child in el:
            walk(child)
            if local(child.tag) in cell_tags:
                current.append(" | ")
            if child.tail:
                current.append(child.tail)
        if is_para:
            flush()

    walk(root)
    flush()
    return blocks


# ---------------------------------------------------------------------------
# Extractors — each returns a list of labelled sections
# ---------------------------------------------------------------------------

def _extract_pdf(path: Path) -> List[Section]:
    try:
        from pypdf import PdfReader
    except ImportError:
        raise DocError("PDF support needs pypdf. Install it with: pip install pypdf")

    try:
        reader = PdfReader(str(path))
    except Exception as e:
        raise DocError(f"could not open the PDF: {e}")

    if reader.is_encrypted:
        try:
            if reader.decrypt("") == 0:  # 0 == failed
                raise DocError(
                    "this PDF is password-protected and cannot be read without the password."
                )
        except DocError:
            raise
        except Exception:
            raise DocError("this PDF is encrypted and could not be decrypted.")

    sections: List[Section] = []
    empty = 0
    for i, page in enumerate(reader.pages, 1):
        try:
            text = page.extract_text() or ""
        except Exception as e:
            text = f"(page {i} could not be extracted: {e})"
        if not text.strip():
            empty += 1
        sections.append((f"page {i}", _clean(text)))

    if sections and empty == len(sections):
        raise DocError(
            f"no text layer found in this PDF ({len(sections)} pages). It is probably a scan "
            "or image-only export; extracting it would need OCR, which is not installed."
        )
    return sections


def _extract_docx(path: Path) -> List[Section]:
    try:
        import docx  # python-docx
    except ImportError:
        raise DocError("Word support needs python-docx. Install it with: pip install python-docx")

    try:
        document = docx.Document(str(path))
    except Exception as e:
        raise DocError(f"could not open the Word document: {e}")

    blocks: List[str] = []
    for para in document.paragraphs:
        text = (para.text or "").strip()
        if not text:
            continue
        style = (getattr(para.style, "name", "") or "")
        if style.startswith("Heading"):
            level = "".join(ch for ch in style if ch.isdigit()) or "1"
            blocks.append(f"{'#' * min(int(level), 6)} {text}")
        else:
            blocks.append(text)

    for t_i, table in enumerate(document.tables, 1):
        rows = [[cell.text for cell in row.cells] for row in table.rows]
        text, used = _rows_to_text(rows)
        if text:
            blocks.append(f"[table {t_i}]\n{text}")
            if len(rows) > used:
                blocks.append(f"(table {t_i} truncated at {used} of {len(rows)} rows)")

    return _group_parts(blocks)


def _extract_xlsx(path: Path) -> List[Section]:
    try:
        import openpyxl
    except ImportError:
        raise DocError("Excel support needs openpyxl. Install it with: pip install openpyxl")

    try:
        wb = openpyxl.load_workbook(str(path), read_only=True, data_only=True)
    except Exception as e:
        raise DocError(f"could not open the workbook: {e}")

    sections: List[Section] = []
    try:
        for ws in wb.worksheets:
            rows = [list(r) for r in ws.iter_rows(values_only=True)]
            rows = [r for r in rows if any(c is not None and str(c).strip() for c in r)]
            text, used = _rows_to_text(rows)
            if len(rows) > used:
                text += f"\n... ({len(rows) - used} more rows not shown)"
            sections.append((f"sheet {ws.title!r}", text or "(empty sheet)"))
    finally:
        try:
            wb.close()
        except Exception:
            pass
    return sections or [("workbook", "(no sheets)")]


def _extract_xls(path: Path) -> List[Section]:
    try:
        import xlrd
    except ImportError:
        raise DocError("Legacy .xls support needs xlrd. Install it with: pip install xlrd")

    try:
        book = xlrd.open_workbook(str(path))
    except Exception as e:
        raise DocError(f"could not open the legacy workbook: {e}")

    sections: List[Section] = []
    for sheet in book.sheets():
        rows = [sheet.row_values(r) for r in range(sheet.nrows)]
        rows = [r for r in rows if any(str(c).strip() for c in r)]
        text, used = _rows_to_text(rows)
        if len(rows) > used:
            text += f"\n... ({len(rows) - used} more rows not shown)"
        sections.append((f"sheet {sheet.name!r}", text or "(empty sheet)"))
    return sections or [("workbook", "(no sheets)")]


def _extract_pptx(path: Path) -> List[Section]:
    try:
        from pptx import Presentation
    except ImportError:
        raise DocError("PowerPoint support needs python-pptx. Install it with: pip install python-pptx")

    try:
        prs = Presentation(str(path))
    except Exception as e:
        raise DocError(f"could not open the presentation: {e}")

    sections: List[Section] = []
    for i, slide in enumerate(prs.slides, 1):
        lines: List[str] = []
        for shape in slide.shapes:
            if shape.has_text_frame:
                text = "\n".join(p.text for p in shape.text_frame.paragraphs if p.text.strip())
                if text.strip():
                    lines.append(text.strip())
            if getattr(shape, "has_table", False):
                rows = [[c.text for c in row.cells] for row in shape.table.rows]
                text, _used = _rows_to_text(rows)
                if text:
                    lines.append(f"[table]\n{text}")
        try:
            if slide.has_notes_slide:
                notes = (slide.notes_slide.notes_text_frame.text or "").strip()
                if notes:
                    lines.append(f"[speaker notes] {notes}")
        except Exception:
            pass
        sections.append((f"slide {i}", _clean("\n".join(lines)) or "(no text on this slide)"))
    return sections or [("presentation", "(no slides)")]


def _extract_odf(path: Path, kind: str) -> List[Section]:
    """OpenDocument text/spreadsheet/presentation — stdlib zip + XML only."""
    try:
        with zipfile.ZipFile(path) as z:
            content = z.read("content.xml")
    except KeyError:
        raise DocError("not a valid OpenDocument file (no content.xml inside).")
    except Exception as e:
        raise DocError(f"could not open the OpenDocument file: {e}")

    if kind == "ods":
        blocks = _xml_text(content, para_tags=("table-row",), cell_tags=("table-cell",))
        return _group_parts(blocks)
    if kind == "odp":
        blocks = _xml_text(content, para_tags=("p", "h"))
        return _group_parts(blocks)
    blocks = _xml_text(content, para_tags=("p", "h"), cell_tags=("table-cell",))
    return _group_parts(blocks)


def _extract_rtf(path: Path) -> List[Section]:
    raw = path.read_text(encoding="utf-8", errors="replace")
    try:
        from striprtf.striprtf import rtf_to_text
    except ImportError:
        raise DocError("RTF support needs striprtf. Install it with: pip install striprtf")
    try:
        text = rtf_to_text(raw, errors="ignore")
    except Exception as e:
        raise DocError(f"could not parse the RTF: {e}")
    return _group_parts(_clean(text).split("\n"))


def _extract_epub(path: Path) -> List[Section]:
    """Spine-ordered EPUB text, using the stdlib plus the HTML stripper."""
    from .web_tools import html_to_text
    import xml.etree.ElementTree as ET

    try:
        z = zipfile.ZipFile(path)
    except Exception as e:
        raise DocError(f"could not open the EPUB: {e}")

    with z:
        names = z.namelist()
        opf_name = None
        try:
            container = ET.fromstring(z.read("META-INF/container.xml"))
            for el in container.iter():
                if el.tag.rsplit("}", 1)[-1] == "rootfile":
                    opf_name = el.get("full-path")
                    break
        except Exception:
            opf_name = None
        if not opf_name:
            opf_name = next((n for n in names if n.lower().endswith(".opf")), None)

        docs: List[str] = []
        if opf_name:
            try:
                opf = ET.fromstring(z.read(opf_name))
                base = os.path.dirname(opf_name)
                ids = {}
                for el in opf.iter():
                    tag = el.tag.rsplit("}", 1)[-1]
                    if tag == "item":
                        ids[el.get("id")] = el.get("href")
                    elif tag == "itemref":
                        href = ids.get(el.get("idref"))
                        if href:
                            docs.append(os.path.join(base, href).replace("\\", "/"))
            except Exception:
                docs = []
        if not docs:
            docs = [n for n in names if n.lower().endswith((".xhtml", ".html", ".htm"))]

        sections: List[Section] = []
        for i, name in enumerate(docs, 1):
            try:
                body = z.read(name).decode("utf-8", errors="replace")
            except Exception:
                continue
            text = _clean(html_to_text(body))
            if text:
                sections.append((f"chapter {i} ({os.path.basename(name)})", text))

    if not sections:
        raise DocError("no readable chapters found in this EPUB.")
    return sections


def _extract_eml(path: Path) -> List[Section]:
    from email import policy
    from email.parser import BytesParser
    from .web_tools import html_to_text

    try:
        msg = BytesParser(policy=policy.default).parse(open(path, "rb"))
    except Exception as e:
        raise DocError(f"could not parse the email: {e}")

    head = [f"{h}: {msg.get(h)}" for h in ("From", "To", "Cc", "Date", "Subject") if msg.get(h)]

    body = ""
    try:
        part = msg.get_body(preferencelist=("plain", "html"))
        if part is not None:
            content = part.get_content()
            body = content if part.get_content_subtype() == "plain" else html_to_text(content)
    except Exception:
        body = ""

    attachments = [
        f"{p.get_filename()} ({p.get_content_type()})"
        for p in msg.iter_attachments()
        if p.get_filename()
    ]

    blocks = ["\n".join(head), _clean(body)]
    if attachments:
        blocks.append("[attachments] " + ", ".join(attachments))
    return _group_parts([b for b in blocks if b.strip()])


def _extract_ipynb(path: Path) -> List[Section]:
    try:
        nb = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except Exception as e:
        raise DocError(f"could not parse the notebook: {e}")

    blocks: List[str] = []
    for i, cell in enumerate(nb.get("cells", []), 1):
        source = cell.get("source", "")
        text = "".join(source) if isinstance(source, list) else str(source)
        kind = cell.get("cell_type", "code")
        if not text.strip():
            continue
        blocks.append(f"[{kind} cell {i}]\n{text.rstrip()}")
        for out in cell.get("outputs", [])[:3]:
            data = out.get("text") or (out.get("data") or {}).get("text/plain") or ""
            otext = "".join(data) if isinstance(data, list) else str(data)
            if otext.strip():
                blocks.append(f"[output {i}] {otext.strip()[:500]}")
    return _group_parts(blocks)


def _extract_csv(path: Path) -> List[Section]:
    raw = path.read_text(encoding="utf-8", errors="replace")
    sample = raw[:8000]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
    except Exception:
        dialect = csv.excel_tab if path.suffix.lower() == ".tsv" else csv.excel
    rows = list(csv.reader(io.StringIO(raw), dialect))
    rows = [r for r in rows if any(str(c).strip() for c in r)]
    text, used = _rows_to_text(rows)
    if len(rows) > used:
        text += f"\n... ({len(rows) - used} more rows not shown)"
    return [(f"{len(rows)} rows", text or "(empty file)")]


def _extract_html(path: Path) -> List[Section]:
    from .web_tools import html_to_text
    body = path.read_text(encoding="utf-8", errors="replace")
    return _group_parts(_clean(html_to_text(body)).split("\n\n"))


def _extract_ole(path: Path) -> List[Section]:
    """Legacy Office (.doc/.ppt 97-2003): convert with LibreOffice if available.

    There is no small, maintained, permissively-licensed pure-Python reader for
    the 1997 OLE compound formats, so rather than ship a fragile binary scraper
    this converts via LibreOffice when it is installed and otherwise explains
    the two ways out.
    """
    soffice = _find_soffice()
    if not soffice:
        raise DocError(
            f"{path.name} is a legacy Office file (Word/PowerPoint 97-2003), which has no "
            "pure-Python reader. Two ways to read it: (1) open it and 'Save As' the modern "
            "format (.docx/.pptx), or (2) install LibreOffice, which this tool will then use "
            "to convert it automatically."
        )

    target = "docx" if path.suffix.lower() in (".doc", "") else "pptx"
    with tempfile.TemporaryDirectory(prefix="gemma_doc_") as tmp:
        try:
            proc = subprocess.run(
                [soffice, "--headless", "--convert-to", target, "--outdir", tmp, str(path)],
                capture_output=True, text=True, timeout=120,
            )
        except subprocess.TimeoutExpired:
            raise DocError("LibreOffice took too long converting this legacy file (120s timeout).")
        except Exception as e:
            raise DocError(f"LibreOffice conversion failed: {e}")

        produced = list(Path(tmp).glob(f"*.{target}"))
        if not produced:
            detail = (proc.stderr or proc.stdout or "").strip()[:200]
            raise DocError(f"LibreOffice could not convert this file. {detail}")
        converted = produced[0]
        return _extract_docx(converted) if target == "docx" else _extract_pptx(converted)


def _find_soffice() -> Optional[str]:
    found = shutil.which("soffice") or shutil.which("libreoffice")
    if found:
        return found
    for candidate in (
        r"C:\Program Files\LibreOffice\program\soffice.exe",
        r"C:\Program Files (x86)\LibreOffice\program\soffice.exe",
        "/usr/bin/soffice",
        "/Applications/LibreOffice.app/Contents/MacOS/soffice",
    ):
        if os.path.isfile(candidate):
            return candidate
    return None


def _extract_text(path: Path) -> List[Section]:
    body = path.read_text(encoding="utf-8", errors="replace")
    return _group_parts(body.split("\n"))


_EXTRACTORS: Dict[str, Callable[[Path], List[Section]]] = {
    "pdf": _extract_pdf,
    "docx": _extract_docx,
    "xlsx": _extract_xlsx,
    "xls": _extract_xls,
    "pptx": _extract_pptx,
    "odt": lambda p: _extract_odf(p, "odt"),
    "ods": lambda p: _extract_odf(p, "ods"),
    "odp": lambda p: _extract_odf(p, "odp"),
    "odf": lambda p: _extract_odf(p, "odt"),
    "rtf": _extract_rtf,
    "epub": _extract_epub,
    "eml": _extract_eml,
    "ipynb": _extract_ipynb,
    "csv": _extract_csv,
    "html": _extract_html,
    "ole": _extract_ole,
    "text": _extract_text,
}

_UNIT = {
    "pdf": "pages", "xlsx": "sheets", "xls": "sheets", "pptx": "slides",
    "epub": "chapters", "csv": "table",
}


# ---------------------------------------------------------------------------
# The tool
# ---------------------------------------------------------------------------

def read_document(inp: Dict[str, Any]) -> str:
    """Extract readable text from a document file. Returns text, never raises."""
    raw = str(inp.get("path", "")).strip().strip('"')
    if not raw:
        return "Error: 'path' is required"

    path = Path(os.path.expanduser(raw))
    if not path.exists():
        return f"Error: File not found: {path}"
    if path.is_dir():
        return f"Error: Path is a directory, not a file: {path}"

    try:
        offset = max(1, int(inp.get("offset", 1) or 1))
    except (TypeError, ValueError):
        offset = 1
    try:
        limit = int(inp.get("limit", 0) or 0)
    except (TypeError, ValueError):
        limit = 0
    try:
        max_chars = int(inp.get("max_chars", _DEFAULT_MAX_CHARS) or _DEFAULT_MAX_CHARS)
    except (TypeError, ValueError):
        max_chars = _DEFAULT_MAX_CHARS
    max_chars = max(500, min(max_chars, _HARD_MAX_CHARS))

    kind, note = _resolve_kind(path)
    extractor = _EXTRACTORS.get(kind)
    if extractor is None:
        return (
            f"Error: don't know how to read '{path.suffix or path.name}' as a document. "
            f"Supported: {', '.join(SUPPORTED_EXTS)}. For plain text or code, use read_file."
        )

    try:
        sections = extractor(path)
    except DocError as e:
        return f"Error reading {path.name}: {e}"
    except Exception as e:
        return f"Error reading {path.name}: unexpected failure in the {kind} reader: {e}"

    total = len(sections)
    unit = _UNIT.get(kind, "parts")
    selected = sections[offset - 1:]
    if limit > 0:
        selected = selected[:limit]
    if not selected:
        return (
            f"{path.name} [{kind}] has {total} {unit}; offset {offset} is past the end. "
            f"Use offset between 1 and {total}."
        )

    header = f"{path.name} [{kind}, {total} {unit}]"
    if note:
        header += f"\n{note}"

    body_parts: List[str] = []
    used_chars = 0
    shown = 0
    for label, text in selected:
        chunk = f"\n--- {label} ---\n{text}" if total > 1 or kind != "text" else f"\n{text}"
        if used_chars + len(chunk) > max_chars:
            remaining = max_chars - used_chars
            if remaining > 200:
                body_parts.append(chunk[:remaining])
                shown += 1
            break
        body_parts.append(chunk)
        used_chars += len(chunk)
        shown += 1

    body = "".join(body_parts).strip()
    last = offset + shown - 1
    footer = ""
    if last < total:
        footer = (
            f"\n\n... (showing {unit} {offset}-{last} of {total}; "
            f"call read_document again with offset={last + 1} for more)"
        )
    elif offset > 1:
        footer = f"\n\n(showing {unit} {offset}-{last} of {total} — end of document)"

    if not body:
        return f"{header}\n\n(no extractable text)"
    return f"{header}\n\n{body}{footer}"
