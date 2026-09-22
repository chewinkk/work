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


RENDERERS = {"docx": render_docx, "pptx": render_pptx}
SCHEMAS = {"docx": DOC_SCHEMA, "pptx": DECK_SCHEMA}
EXTENSIONS = {"docx": ".docx", "pptx": ".pptx"}
MEDIA_TYPES = {
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
}
