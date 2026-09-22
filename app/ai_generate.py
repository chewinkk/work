"""Turn a source document or a plain prompt into a .pptx deck.

Claude returns the deck as schema-validated JSON (output_config), and
python-pptx renders it locally. The model never writes the file itself.
"""
import base64
import json
import os

from anthropic import AsyncAnthropic
from pptx import Presentation
from pptx.util import Inches, Pt

from app.config import ANTHROPIC_API_KEY, ANTHROPIC_MODEL

MAX_SOURCE_CHARS = 600_000   # ~150k tokens, well inside the 1M context window
MAX_PDF_BYTES = 25 * 1024 * 1024

TEXT_SUFFIXES = {".txt", ".md", ".markdown", ".rst", ".csv", ".tsv", ".json"}

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
                    "bullets": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
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

SYSTEM = (
    "You build university coursework slide decks. Produce 8 to 14 content "
    "slides. Each slide carries a specific heading and 3 to 5 bullets of at "
    "most 18 words each. Bullets state facts, findings, or steps, never "
    "filler like 'Introduction' or 'Overview of the topic'. Speaker notes "
    "give two or three sentences the presenter can read aloud. Ground every "
    "claim in the supplied material when material is supplied."
)


def _client():
    if not ANTHROPIC_API_KEY:
        raise RuntimeError("ANTHROPIC_API_KEY is not set")
    return AsyncAnthropic(api_key=ANTHROPIC_API_KEY)


# --- source extraction -----------------------------------------------------

def _extract_docx(path):
    from docx import Document

    document = Document(path)
    parts = [p.text for p in document.paragraphs if p.text.strip()]
    for table in document.tables:
        for row in table.rows:
            cells = [c.text.strip() for c in row.cells if c.text.strip()]
            if cells:
                parts.append(" | ".join(cells))
    return "\n".join(parts)


def _extract_pptx(path):
    parts = []
    for slide in Presentation(path).slides:
        for shape in slide.shapes:
            if shape.has_text_frame and shape.text_frame.text.strip():
                parts.append(shape.text_frame.text.strip())
    return "\n".join(parts)


def _source_block(source_file_path, original_filename):
    """Build the content block Claude reads the source material from.

    PDFs go over as native document blocks so tables and layout survive.
    Everything else is extracted to text locally.
    """
    suffix = os.path.splitext(original_filename)[1].lower()

    if suffix == ".pdf":
        size = os.path.getsize(source_file_path)
        if size > MAX_PDF_BYTES:
            raise RuntimeError(
                f"{original_filename} is {size // 1024 // 1024} MB. "
                "Canvas source PDFs must be under 25 MB to send to Claude."
            )
        with open(source_file_path, "rb") as handle:
            data = base64.standard_b64encode(handle.read()).decode("ascii")
        return {
            "type": "document",
            "source": {"type": "base64", "media_type": "application/pdf", "data": data},
        }

    if suffix == ".docx":
        text = _extract_docx(source_file_path)
    elif suffix == ".pptx":
        text = _extract_pptx(source_file_path)
    elif suffix in TEXT_SUFFIXES:
        with open(source_file_path, "r", encoding="utf-8", errors="replace") as handle:
            text = handle.read()
    else:
        raise RuntimeError(
            f"Cannot read {suffix or 'that file type'}. Supported source files: "
            "PDF, DOCX, PPTX, TXT, MD, CSV."
        )

    text = text.strip()
    if not text:
        raise RuntimeError(f"No readable text found in {original_filename}.")
    if len(text) > MAX_SOURCE_CHARS:
        raise RuntimeError(
            f"{original_filename} holds {len(text):,} characters, over the "
            f"{MAX_SOURCE_CHARS:,} limit. Split the document and try again."
        )
    return {"type": "text", "text": text}


# --- model call ------------------------------------------------------------

async def _request_deck(content_blocks):
    response = await _client().messages.create(
        model=ANTHROPIC_MODEL,
        max_tokens=16000,
        system=SYSTEM,
        messages=[{"role": "user", "content": content_blocks}],
        output_config={"format": {"type": "json_schema", "schema": DECK_SCHEMA}},
    )

    if response.stop_reason == "refusal":
        detail = getattr(response.stop_details, "explanation", None)
        raise RuntimeError(f"Claude declined this request. {detail or ''}".strip())
    if response.stop_reason == "max_tokens":
        raise RuntimeError("Claude ran out of output room. Try a shorter source.")

    text = next((b.text for b in response.content if b.type == "text"), None)
    if not text:
        raise RuntimeError("Claude returned no deck content.")

    deck = json.loads(text)
    if not deck.get("slides"):
        raise RuntimeError("Claude returned a deck with no slides.")
    return deck


# --- rendering -------------------------------------------------------------

def _add_bullets(body, bullets):
    frame = body.text_frame
    frame.clear()
    size = Pt(20) if len(bullets) <= 4 else Pt(16)
    for index, bullet in enumerate(bullets):
        para = frame.paragraphs[0] if index == 0 else frame.add_paragraph()
        para.text = str(bullet)
        para.level = 0
        for run in para.runs:
            run.font.size = size


def _render(deck, output_path):
    presentation = Presentation()
    presentation.slide_width = Inches(13.333)   # 16:9
    presentation.slide_height = Inches(7.5)

    cover = presentation.slides.add_slide(presentation.slide_layouts[0])
    cover.shapes.title.text = deck["title"]
    if len(cover.placeholders) > 1:
        cover.placeholders[1].text = deck.get("subtitle", "")

    for slide_spec in deck["slides"]:
        slide = presentation.slides.add_slide(presentation.slide_layouts[1])
        slide.shapes.title.text = slide_spec["heading"]
        _add_bullets(slide.placeholders[1], slide_spec.get("bullets", []))
        notes = slide_spec.get("notes", "")
        if notes:
            slide.notes_slide.notes_text_frame.text = notes

    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    presentation.save(output_path)
    return output_path


# --- public API ------------------------------------------------------------

async def generate_presentation(source_file_path, original_filename,
                                assignment_name, output_path):
    """Build a deck from an uploaded source document."""
    block = _source_block(source_file_path, original_filename)
    instruction = {
        "type": "text",
        "text": (
            f"Build the slide deck for the assignment '{assignment_name}' from "
            f"the attached material ({original_filename}). Cover what the "
            "material actually says. Do not invent sources or figures."
        ),
    }
    deck = await _request_deck([block, instruction])
    return _render(deck, output_path)


async def generate_presentation_from_prompt(user_prompt, assignment_name,
                                            output_path):
    """Build a deck from a written prompt, with no source document."""
    if not user_prompt.strip():
        raise RuntimeError("The prompt is empty.")
    instruction = {
        "type": "text",
        "text": (
            f"Build the slide deck for the assignment '{assignment_name}'.\n\n"
            f"Brief from the student:\n{user_prompt.strip()}"
        ),
    }
    deck = await _request_deck([instruction])
    return _render(deck, output_path)
