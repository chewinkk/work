"""Talk to Claude about one specific assignment.

Claude is given the assignment description, the rubric, and any specification
files the instructor attached, so the conversation starts already knowing what
is being marked. When the work is ready, the thread is rendered into an
accessible Word document or PowerPoint deck and sent to the review queue.
"""
import os
import re

from app.ai_generate import check_stop_reason, client, request_json
from app.config import ANTHROPIC_MODEL
from app.extract import extract_text, html_to_text
from app.render import EXTENSIONS, RENDERERS, SCHEMAS

MAX_SPEC_CHARS = 120_000
MAX_HISTORY = 40

PRESENTATION_HINTS = re.compile(
    r"\b(presentation|slide|slides|slideshow|deck|powerpoint|pptx|poster session)\b",
    re.I,
)
DOCUMENT_HINTS = re.compile(
    r"\b(essay|paper|report|memo|analysis|write[- ]?up|reflection|summary|"
    r"annotated bibliography|research|thesis|letter|proposal|case study)\b",
    re.I,
)

CHAT_SYSTEM = """You are helping one university student with one specific assignment.

The assignment brief, the grading rubric, and any attached specification files
appear below. Treat them as the authority on what is being marked. When the
student asks for something vague, anchor your answer in the rubric criteria and
say which one you are addressing.

How to work:
- Be direct. Lead with the answer, not a preamble.
- When the student asks you to draft or write something, produce the real thing
  at full length, not an outline of it, unless they asked for an outline.
- Point out when the brief requires something the student has not mentioned,
  such as a word count, a citation style, or a required section.
- If the rubric awards points for something missing from the draft, say so.
- Never invent a source, a statistic, or a citation. If you need a real
  reference and do not have one, say what kind of source is needed instead.
- Ask a clarifying question only when the answer would change materially.

The student will later convert this conversation into a Word document or a
PowerPoint deck, so keep your drafts structured with clear headings and
sections rather than one undifferentiated block of prose."""

BUILD_SYSTEM = """You turn a finished tutoring conversation into the deliverable
the student will submit.

Use the conversation as the source of truth. Take the most recent and most
complete version of the work discussed, apply any corrections agreed later in
the thread, and produce the whole deliverable. Do not summarise it, do not
add commentary about the conversation, and do not invent content that was
never discussed.

Structure matters for accessibility. A screen reader navigates by heading
levels and slide titles, so give every section a real heading and every slide
a real title. Never use a heading as a substitute for body text."""


# --- assignment context ----------------------------------------------------

def format_rubric(assignment):
    rubric = assignment.get("rubric") or []
    if not rubric:
        return ""
    lines = ["RUBRIC"]
    for criterion in rubric:
        points = criterion.get("points")
        lines.append(f"- {criterion.get('description', 'Criterion')}"
                     + (f" ({points} points)" if points is not None else ""))
        long_description = html_to_text(criterion.get("long_description") or "")
        if long_description:
            lines.append(f"    {long_description}")
        for rating in criterion.get("ratings") or []:
            detail = html_to_text(rating.get("long_description") or "")
            lines.append(f"    {rating.get('points')} pts: {rating.get('description')}"
                         + (f" - {detail}" if detail else ""))
    return "\n".join(lines)


def build_context(course, assignment, attachments=(), due_text=None):
    """The system prompt Claude works from. Stable across a thread, so it caches."""
    parts = ["ASSIGNMENT",
             f"Course: {course.get('display_name') or course.get('name', '')}",
             f"Name: {assignment.get('name', '')}"]
    if due_text:
        parts.append(f"Due: {due_text}")
    if assignment.get("points_possible") is not None:
        parts.append(f"Points possible: {assignment['points_possible']}")
    types = assignment.get("submission_types") or []
    if types:
        parts.append(f"Submission types: {', '.join(types)}")

    description = html_to_text(assignment.get("description") or "")
    parts.append("\nBRIEF\n" + (description or "The instructor left the description blank."))

    rubric = format_rubric(assignment)
    if rubric:
        parts.append("\n" + rubric)

    budget = MAX_SPEC_CHARS
    spec_parts = []
    for attachment in attachments:
        if budget <= 0:
            break
        text = extract_text(attachment["path"], attachment["filename"])
        if not text:
            continue
        clipped = text[:budget]
        budget -= len(clipped)
        note = "" if len(clipped) == len(text) else "\n[truncated, file continues]"
        spec_parts.append(f"--- {attachment['filename']} ---\n{clipped}{note}")
    if spec_parts:
        parts.append("\nATTACHED SPECIFICATIONS\n" + "\n\n".join(spec_parts))

    return "\n".join(parts)


def default_format(assignment):
    """Guess the deliverable from the assignment itself. The student can override."""
    haystack = " ".join([
        assignment.get("name") or "",
        html_to_text(assignment.get("description") or "")[:2000],
    ])
    if PRESENTATION_HINTS.search(haystack):
        return "pptx"
    if DOCUMENT_HINTS.search(haystack):
        return "docx"
    return "docx"


def _system_blocks(context):
    """System prompt as a cacheable block. The assignment text does not change
    between turns, so caching it keeps a long thread cheap."""
    return [
        {"type": "text", "text": CHAT_SYSTEM},
        {"type": "text", "text": context, "cache_control": {"type": "ephemeral"}},
    ]


# --- chat ------------------------------------------------------------------

async def reply(context, history, user_message):
    """One conversational turn. history is [{'role','content'}, ...]."""
    if not user_message.strip():
        raise RuntimeError("Type a message first.")

    messages = [{"role": m["role"], "content": m["content"]}
                for m in history[-MAX_HISTORY:]
                if m.get("role") in ("user", "assistant") and m.get("content")]
    messages.append({"role": "user", "content": user_message.strip()})

    response = await client().messages.create(
        model=ANTHROPIC_MODEL,
        max_tokens=16000,
        system=_system_blocks(context),
        messages=messages,
    )
    check_stop_reason(response)
    text = "\n\n".join(b.text for b in response.content if b.type == "text").strip()
    if not text:
        raise RuntimeError("Claude returned an empty reply.")
    return text


# --- build the deliverable -------------------------------------------------

def _transcript(history):
    turns = []
    for message in history[-MAX_HISTORY:]:
        who = "STUDENT" if message.get("role") == "user" else "CLAUDE"
        turns.append(f"{who}: {message.get('content', '')}")
    return "\n\n".join(turns)


async def build_artifact(context, history, kind, assignment_name, output_dir,
                         instruction=""):
    """Render the conversation into an accessible .docx or .pptx.

    Returns (path, filename).
    """
    if kind not in RENDERERS:
        raise RuntimeError(f"Unknown format {kind!r}.")
    if not history:
        raise RuntimeError("Say something to Claude before saving.")

    wanted = ("a PowerPoint deck" if kind == "pptx" else "a Word document")
    blocks = [{
        "type": "text",
        "text": (
            f"{context}\n\n"
            f"CONVERSATION SO FAR\n{_transcript(history)}\n\n"
            f"Produce the finished deliverable as {wanted} for "
            f"'{assignment_name}'."
            + (f"\n\nExtra instruction from the student: {instruction.strip()}"
               if instruction.strip() else "")
        ),
    }]

    spec = await request_json(BUILD_SYSTEM, blocks, SCHEMAS[kind])
    if kind == "pptx" and not spec.get("slides"):
        raise RuntimeError("Claude returned a deck with no slides.")
    if kind == "docx" and not spec.get("blocks"):
        raise RuntimeError("Claude returned an empty document.")

    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", assignment_name).strip("._") or "assignment"
    filename = f"{safe}{EXTENSIONS[kind]}"
    path = os.path.join(output_dir, f"{os.urandom(4).hex()}_{filename}")
    RENDERERS[kind](spec, path)
    return path, filename
