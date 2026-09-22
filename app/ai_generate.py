"""Turn a source document or a written brief into an accessible .pptx deck.

Claude returns the deck as schema-validated JSON (output_config), and
app.render builds the file locally. The model never writes the file itself.
"""
import json

from anthropic import AsyncAnthropic

from app.config import ANTHROPIC_API_KEY, ANTHROPIC_MODEL
from app.extract import source_block
from app.render import DECK_SCHEMA, render_pptx

SYSTEM = (
    "You build university coursework slide decks. Produce 8 to 14 content "
    "slides. Each slide carries a specific heading and 3 to 5 bullets of at "
    "most 18 words each. Bullets state facts, findings, or steps, never "
    "filler like 'Introduction' or 'Overview of the topic'. Speaker notes "
    "give two or three sentences the presenter can read aloud. Every slide "
    "needs a real heading, because screen readers navigate a deck by its "
    "slide titles. Ground every claim in the supplied material when material "
    "is supplied."
)


def client():
    if not ANTHROPIC_API_KEY:
        raise RuntimeError("ANTHROPIC_API_KEY is not set")
    return AsyncAnthropic(api_key=ANTHROPIC_API_KEY)


def check_stop_reason(response):
    """Turn an unusable response into a message worth reading."""
    if response.stop_reason == "refusal":
        detail = getattr(response.stop_details, "explanation", None)
        raise RuntimeError(f"Claude declined this request. {detail or ''}".strip())
    if response.stop_reason == "max_tokens":
        raise RuntimeError("Claude ran out of output room. Try a shorter source.")


async def request_json(system, content_blocks, schema, max_tokens=16000):
    """One structured-output call. Returns the parsed object."""
    response = await client().messages.create(
        model=ANTHROPIC_MODEL,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": content_blocks}],
        output_config={"format": {"type": "json_schema", "schema": schema}},
    )
    check_stop_reason(response)
    text = next((b.text for b in response.content if b.type == "text"), None)
    if not text:
        raise RuntimeError("Claude returned no content.")
    return json.loads(text)


async def _build_deck(content_blocks):
    deck = await request_json(SYSTEM, content_blocks, DECK_SCHEMA)
    if not deck.get("slides"):
        raise RuntimeError("Claude returned a deck with no slides.")
    return deck


async def generate_presentation(source_file_path, original_filename,
                                assignment_name, output_path):
    """Build a deck from an uploaded source document."""
    block = source_block(source_file_path, original_filename)
    instruction = {
        "type": "text",
        "text": (
            f"Build the slide deck for the assignment '{assignment_name}' from "
            f"the attached material ({original_filename}). Cover what the "
            "material actually says. Do not invent sources or figures."
        ),
    }
    return render_pptx(await _build_deck([block, instruction]), output_path)


async def generate_presentation_from_prompt(user_prompt, assignment_name,
                                            output_path):
    """Build a deck from a written brief, with no source document."""
    if not user_prompt.strip():
        raise RuntimeError("The prompt is empty.")
    instruction = {
        "type": "text",
        "text": (
            f"Build the slide deck for the assignment '{assignment_name}'.\n\n"
            f"Brief from the student:\n{user_prompt.strip()}"
        ),
    }
    return render_pptx(await _build_deck([instruction]), output_path)
