"""Render Claude's structured output as accessible .docx and .pptx files.

Accessibility is the point of this module, not a nicety. Screen readers
navigate a document by its heading structure and a deck by its slide titles.
Text that merely looks like a heading carries no structure, so nothing here
fakes formatting with bold runs or free-floating text boxes.

Word:
  real Heading 1-3 styles, real list styles, document language set on the
  Normal style, title and author in core properties.
PowerPoint:
  every slide carries a filled title placeholder, body text lives in the
  layout placeholder so reading order follows it, empty placeholders are
  removed because they announce as blank, and speaker notes are kept.
"""
import os

from docx import Document
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Pt as DocxPt
from pptx import Presentation
from pptx.util import Inches, Pt

LANGUAGE = "en-US"

# --- schemas Claude fills ---------------------------------------------------

DOC_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "blocks": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "type": {"type": "string",
                             "enum": ["heading", "paragraph", "bullet", "number"]},
                    "level": {"type": "integer", "enum": [1, 2, 3]},
                    "text": {"type": "string"},
                },
                "required": ["type", "level", "text"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["title", "blocks"],
    "additionalProperties": False,
}

TABLE_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "sheets": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "headers": {"type": "array", "items": {"type": "string"}},
                    "rows": {
                        "type": "array",
                        "items": {"type": "array", "items": {"type": "string"}},
                    },
                },
                "required": ["name", "headers", "rows"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["title", "sheets"],
    "additionalProperties": False,
}

DECK_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "subtitle": {"type": "string"},
        "slides": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "heading": {"type": "string"},
                    "bullets": {"type": "array", "items": {"type": "string"}},
                    "notes": {"type": "string"},
                },
                "required": ["heading", "bullets", "notes"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["title", "subtitle", "slides"],
    "additionalProperties": False,
}


# --- word -------------------------------------------------------------------

def _set_language(document, language=LANGUAGE):
    """Tag the document's language so a screen reader picks the right voice."""
    rpr = document.styles["Normal"].element.get_or_add_rPr()
    tag = rpr.find(qn("w:lang"))
    if tag is None:
        tag = OxmlElement("w:lang")
        rpr.append(tag)
    tag.set(qn("w:val"), language)
    tag.set(qn("w:eastAsia"), language)


def render_docx(spec, output_path, author="Canvas Assistant"):
    """Write an accessible Word document from a DOC_SCHEMA payload."""
    document = Document()
    _set_language(document)

    document.core_properties.title = spec.get("title") or "Document"
    document.core_properties.author = author
    document.core_properties.language = LANGUAGE

    # The title uses Heading 0 (Title style), giving the document a real
    # top-level landmark rather than a large bold line.
    document.add_heading(spec.get("title") or "Document", level=0)

    for block in spec.get("blocks", []):
        text = (block.get("text") or "").strip()
        if not text:
            continue
        kind = block.get("type")
        if kind == "heading":
            level = min(max(int(block.get("level", 1)), 1), 3)
            document.add_heading(text, level=level)
        elif kind == "bullet":
            document.add_paragraph(text, style="List Bullet")
        elif kind == "number":
            document.add_paragraph(text, style="List Number")
        else:
            document.add_paragraph(text)

    for paragraph in document.paragraphs:
        for run in paragraph.runs:
            if run.font.size is None and paragraph.style.name == "Normal":
                run.font.size = DocxPt(11)

    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    document.save(output_path)
    return output_path


# --- powerpoint -------------------------------------------------------------

def _drop_empty_placeholders(slide):
    """Remove placeholders left blank. A screen reader announces them."""
    for shape in list(slide.placeholders):
        if not shape.has_text_frame or not shape.text_frame.text.strip():
            shape._element.getparent().remove(shape._element)


def _fill_bullets(placeholder, bullets):
    frame = placeholder.text_frame
    frame.clear()
    size = Pt(20) if len(bullets) <= 4 else Pt(16)
    for index, bullet in enumerate(bullets):
        para = frame.paragraphs[0] if index == 0 else frame.add_paragraph()
        para.text = str(bullet)
        para.level = 0
        for run in para.runs:
            run.font.size = size


def render_pptx(spec, output_path, author="Canvas Assistant"):
    """Write an accessible PowerPoint deck from a DECK_SCHEMA payload."""
    presentation = Presentation()
    presentation.slide_width = Inches(13.333)   # 16:9
    presentation.slide_height = Inches(7.5)

    presentation.core_properties.title = spec.get("title") or "Presentation"
    presentation.core_properties.author = author
    presentation.core_properties.language = LANGUAGE

    cover = presentation.slides.add_slide(presentation.slide_layouts[0])
    cover.shapes.title.text = spec.get("title") or "Presentation"
    subtitle = (spec.get("subtitle") or "").strip()
    if subtitle and len(cover.placeholders) > 1:
        cover.placeholders[1].text = subtitle
    _drop_empty_placeholders(cover)

    for index, slide_spec in enumerate(spec.get("slides", []), start=1):
        slide = presentation.slides.add_slide(presentation.slide_layouts[1])

        # Every slide needs a title. It is how a screen reader user moves
        # through a deck, so an untitled slide is a dead end.
        heading = (slide_spec.get("heading") or "").strip() or f"Slide {index}"
        slide.shapes.title.text = heading

        bullets = [b for b in (slide_spec.get("bullets") or []) if str(b).strip()]
        if bullets:
            _fill_bullets(slide.placeholders[1], bullets)

        notes = (slide_spec.get("notes") or "").strip()
        if notes:
            slide.notes_slide.notes_text_frame.text = notes

        _drop_empty_placeholders(slide)

    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    presentation.save(output_path)
    return output_path


# --- pdf --------------------------------------------------------------------

def render_pdf(spec, output_path, author="Canvas Assistant"):
    """Write a PDF with a heading outline and document metadata.

    Caveat worth knowing: reportlab does not emit a fully tagged PDF/UA file.
    The outline gives real navigation and the language and title are declared,
    but where an instructor accepts either, Word is the more accessible choice.
    """
    from reportlab.lib.enums import TA_LEFT
    from reportlab.lib.pagesizes import LETTER
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import inch
    from reportlab.platypus import (
        BaseDocTemplate, Frame, ListFlowable, ListItem, PageTemplate,
        Paragraph, Spacer,
    )
    from xml.sax.saxutils import escape

    title = spec.get("title") or "Document"
    sheet = getSampleStyleSheet()
    body = ParagraphStyle("Body", parent=sheet["BodyText"], fontSize=11,
                          leading=15, spaceAfter=8, alignment=TA_LEFT)

    class _Outlined(BaseDocTemplate):
        """Adds each heading to the PDF outline so it can be navigated."""

        def afterFlowable(self, flowable):
            style = getattr(flowable, "style", None)
            name = getattr(style, "name", "")
            if not name.startswith("Heading"):
                return
            text = flowable.getPlainText()
            level = {"Heading1": 0, "Heading2": 1, "Heading3": 2}.get(name, 0)
            key = f"h{self.seq.nextf('outline')}"
            self.canv.bookmarkPage(key)
            self.canv.addOutlineEntry(text, key, level=level, closed=False)

    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    document = _Outlined(
        output_path, pagesize=LETTER,
        leftMargin=inch, rightMargin=inch, topMargin=inch, bottomMargin=inch,
        title=title, author=author, subject=title, lang=LANGUAGE,
    )
    frame = Frame(document.leftMargin, document.bottomMargin,
                  document.width, document.height, id="body")
    document.addPageTemplates([PageTemplate(id="main", frames=[frame])])

    story = [Paragraph(escape(title), sheet["Title"]), Spacer(1, 10)]
    pending = []

    def flush(kind):
        if not pending:
            return
        story.append(ListFlowable(
            [ListItem(Paragraph(escape(t), body)) for t in pending],
            bulletType="bullet" if kind == "bullet" else "1",
            leftIndent=24,
        ))
        story.append(Spacer(1, 6))
        pending.clear()

    current = None
    for block in spec.get("blocks", []):
        text = (block.get("text") or "").strip()
        if not text:
            continue
        kind = block.get("type")
        if kind in ("bullet", "number"):
            if current and kind != current:
                flush(current)
            current = kind
            pending.append(text)
            continue
        flush(current)
        current = None
        if kind == "heading":
            level = min(max(int(block.get("level", 1)), 1), 3)
            story.append(Paragraph(escape(text), sheet[f"Heading{level}"]))
        else:
            story.append(Paragraph(escape(text), body))
    flush(current)

    document.build(story)
    return output_path


# --- spreadsheets -----------------------------------------------------------

def render_xlsx(spec, output_path, author="Canvas Assistant"):
    """Write a workbook with marked header rows, which screen readers announce."""
    from openpyxl import Workbook
    from openpyxl.styles import Font
    from openpyxl.utils import get_column_letter
    from openpyxl.worksheet.table import Table, TableStyleInfo

    workbook = Workbook()
    workbook.remove(workbook.active)
    workbook.properties.title = spec.get("title") or "Workbook"
    workbook.properties.creator = author

    for index, sheet_spec in enumerate(spec.get("sheets") or [], start=1):
        name = (sheet_spec.get("name") or f"Sheet{index}")[:31] or f"Sheet{index}"
        sheet = workbook.create_sheet(title=name)
        headers = [str(h) for h in (sheet_spec.get("headers") or [])]
        rows = sheet_spec.get("rows") or []

        if headers:
            sheet.append(headers)
            for cell in sheet[1]:
                cell.font = Font(bold=True)
            sheet.freeze_panes = "A2"
        for row in rows:
            sheet.append([str(value) for value in row])

        # A real table range names the header row in the file itself, rather
        # than relying on the first row merely looking like headings.
        if headers and rows:
            ref = f"A1:{get_column_letter(len(headers))}{len(rows) + 1}"
            table = Table(displayName=f"Table{index}", ref=ref)
            table.tableStyleInfo = TableStyleInfo(
                name="TableStyleLight1", showRowStripes=True)
            sheet.add_table(table)

        for column, header in enumerate(headers, start=1):
            longest = max([len(header)] + [
                len(str(r[column - 1])) for r in rows if len(r) >= column
            ] or [10])
            sheet.column_dimensions[get_column_letter(column)].width = min(longest + 2, 60)

    if not workbook.sheetnames:
        workbook.create_sheet(title="Sheet1")
    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    workbook.save(output_path)
    return output_path


def render_csv(spec, output_path, author="Canvas Assistant"):
    """One sheet only. CSV holds a single table by definition."""
    import csv

    sheets = spec.get("sheets") or []
    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    with open(output_path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        if sheets:
            first = sheets[0]
            if first.get("headers"):
                writer.writerow(first["headers"])
            for row in first.get("rows") or []:
                writer.writerow(row)
    return output_path


# --- text formats -----------------------------------------------------------

def _as_markdown(spec):
    lines = [f"# {spec.get('title') or 'Document'}", ""]
    number = 1
    for block in spec.get("blocks", []):
        text = (block.get("text") or "").strip()
        if not text:
            continue
        kind = block.get("type")
        if kind == "heading":
            level = min(max(int(block.get("level", 1)), 1), 3) + 1
            lines += ["", "#" * level + f" {text}", ""]
            number = 1
        elif kind == "bullet":
            lines.append(f"- {text}")
        elif kind == "number":
            lines.append(f"{number}. {text}")
            number += 1
        else:
            lines += [text, ""]
            number = 1
    return "\n".join(lines).strip() + "\n"


def _as_plain(spec):
    lines = [spec.get("title") or "Document", "=" * len(spec.get("title") or "Document"), ""]
    number = 1
    for block in spec.get("blocks", []):
        text = (block.get("text") or "").strip()
        if not text:
            continue
        kind = block.get("type")
        if kind == "heading":
            lines += ["", text, "-" * len(text), ""]
            number = 1
        elif kind == "bullet":
            lines.append(f"  * {text}")
        elif kind == "number":
            lines.append(f"  {number}. {text}")
            number += 1
        else:
            lines += [text, ""]
            number = 1
    return "\n".join(lines).strip() + "\n"


def _as_html(spec):
    from html import escape

    title = spec.get("title") or "Document"
    parts = [
        "<!doctype html>",
        f'<html lang="{LANGUAGE}">',
        "<head><meta charset=\"utf-8\">",
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        f"<title>{escape(title)}</title></head>",
        "<body>",
        f"<h1>{escape(title)}</h1>",
    ]
    open_list = None
    for block in spec.get("blocks", []):
        text = (block.get("text") or "").strip()
        if not text:
            continue
        kind = block.get("type")
        tag = "ul" if kind == "bullet" else "ol" if kind == "number" else None
        if open_list and tag != open_list:
            parts.append(f"</{open_list}>")
            open_list = None
        if tag:
            if not open_list:
                parts.append(f"<{tag}>")
                open_list = tag
            parts.append(f"<li>{escape(text)}</li>")
        elif kind == "heading":
            level = min(max(int(block.get("level", 1)), 1), 3) + 1
            parts.append(f"<h{level}>{escape(text)}</h{level}>")
        else:
            parts.append(f"<p>{escape(text)}</p>")
    if open_list:
        parts.append(f"</{open_list}>")
    parts += ["</body>", "</html>", ""]
    return "\n".join(parts)


def _write_text(text, output_path):
    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as handle:
        handle.write(text)
    return output_path


def render_md(spec, output_path, author="Canvas Assistant"):
    return _write_text(_as_markdown(spec), output_path)


def render_txt(spec, output_path, author="Canvas Assistant"):
    return _write_text(_as_plain(spec), output_path)


def render_html(spec, output_path, author="Canvas Assistant"):
    return _write_text(_as_html(spec), output_path)


def render_plain(spec, output_path, author="Canvas Assistant"):
    """For an arbitrary extension: the body text with no markup added.

    Used when the assignment wants a format with no dedicated writer, such as
    source code or a .rtf, so nothing decorates content that must stay literal.
    """
    lines = []
    for block in spec.get("blocks", []):
        text = block.get("text") or ""
        if text.strip():
            lines.append(text)
    return _write_text("\n".join(lines).rstrip() + "\n", output_path)


# --- registry ---------------------------------------------------------------

# Three schemas cover every format. Prose formats share DOC_SCHEMA, decks use
# DECK_SCHEMA, spreadsheets use TABLE_SCHEMA.
RENDERERS = {
    "docx": render_docx, "pptx": render_pptx, "pdf": render_pdf,
    "xlsx": render_xlsx, "csv": render_csv,
    "md": render_md, "txt": render_txt, "html": render_html,
    "other": render_plain,
}
SCHEMAS = {
    "docx": DOC_SCHEMA, "pdf": DOC_SCHEMA, "md": DOC_SCHEMA,
    "txt": DOC_SCHEMA, "html": DOC_SCHEMA, "other": DOC_SCHEMA,
    "pptx": DECK_SCHEMA,
    "xlsx": TABLE_SCHEMA, "csv": TABLE_SCHEMA,
}
EXTENSIONS = {
    "docx": ".docx", "pptx": ".pptx", "pdf": ".pdf", "xlsx": ".xlsx",
    "csv": ".csv", "md": ".md", "txt": ".txt", "html": ".html",
}
LABELS = {
    "docx": "Word document", "pptx": "PowerPoint deck", "pdf": "PDF",
    "xlsx": "Excel workbook", "csv": "CSV", "md": "Markdown",
    "txt": "Plain text", "html": "Web page", "other": "Custom file type",
}
MEDIA_TYPES = {
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "pdf": "application/pdf",
    "csv": "text/csv",
    "md": "text/markdown",
    "txt": "text/plain",
    "html": "text/html",
}

# Word and PowerPoint carry real structure a screen reader can navigate.
# The rest either cannot (csv, txt) or do so only partially (pdf).
FULLY_ACCESSIBLE = {"docx", "pptx", "html"}
