#!/usr/bin/env python3
"""Preflight checks. Run this before you trust the app with anything.

Your tokens stay on your machine. Nothing here uploads them anywhere except
to Canvas, ntfy, and Anthropic, which is where they already belong.

    python3 check_setup.py             # read-only, safe to run any time
    python3 check_setup.py --redact    # same, but masks names so output is
                                       # safe to paste to someone else
    python3 check_setup.py --submit COURSE_ID:ASSIGNMENT_ID
                                       # creates a REAL submission, asks first

Every check is independent. A failure explains what to fix.
"""
import argparse
import asyncio
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

PASS, FAIL, WARN, SKIP = "PASS", "FAIL", "WARN", "SKIP"
results = []
REDACT = False


def record(status, label, detail=""):
    results.append((status, label, detail))
    mark = {PASS: "  ok  ", FAIL: " FAIL ", WARN: " warn ", SKIP: " skip "}[status]
    print(f"[{mark}] {label}" + (f"\n         {detail}" if detail else ""))


def hide(text, keep=0):
    """Mask a name when --redact is on."""
    if not REDACT or not text:
        return text
    text = str(text)
    return text[:keep] + "*" * max(3, len(text) - keep)


def load_env():
    """Read .env if present so you do not have to export anything."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if not os.path.exists(path):
        return
    with open(path) as handle:
        for line in handle:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


# --- 1. configuration ------------------------------------------------------

def check_config():
    from app.config import (ANTHROPIC_API_KEY, APP_PASSWORD, CANVAS_BASE_URL,
                            CANVAS_TOKEN, DATA_DIR, NTFY_TOPIC, SECRET_KEY)

    raw = os.environ.get("CANVAS_BASE_URL", "")
    if not CANVAS_BASE_URL:
        record(FAIL, "CANVAS_BASE_URL is set", "Set it to https://yourschool.instructure.com")
    elif not CANVAS_BASE_URL.startswith("https://"):
        record(FAIL, "CANVAS_BASE_URL uses https", f"Got {CANVAS_BASE_URL!r}")
    elif raw.endswith("/"):
        record(WARN, "CANVAS_BASE_URL has no trailing slash",
               "The app strips it, so this still works. Tidier without it.")
    elif "/api" in CANVAS_BASE_URL:
        record(FAIL, "CANVAS_BASE_URL is the plain address",
               "Drop the /api/v1 part. The app adds it.")
    else:
        record(PASS, f"CANVAS_BASE_URL looks right ({hide(CANVAS_BASE_URL, 8)})")

    record(PASS if CANVAS_TOKEN else FAIL, "CANVAS_TOKEN is set",
           "" if CANVAS_TOKEN else "Canvas > Account > Settings > New Access Token")

    if not APP_PASSWORD:
        record(FAIL, "APP_PASSWORD is set")
    elif len(APP_PASSWORD) < 12:
        record(WARN, "APP_PASSWORD is long enough",
               f"{len(APP_PASSWORD)} characters. Your app URL is public. Use 16 or more.")
    else:
        record(PASS, "APP_PASSWORD is set and long")

    if not SECRET_KEY:
        record(FAIL, "SECRET_KEY is set")
    elif SECRET_KEY == APP_PASSWORD:
        record(FAIL, "SECRET_KEY differs from APP_PASSWORD",
               "Reusing the password here weakens the cookie. Use a separate random string.")
    elif len(SECRET_KEY) < 24:
        record(WARN, "SECRET_KEY is long enough", f"{len(SECRET_KEY)} characters. Use 32 or more.")
    else:
        record(PASS, "SECRET_KEY is set and distinct")

    if not NTFY_TOPIC:
        record(FAIL, "NTFY_TOPIC is set")
    elif len(NTFY_TOPIC) < 12 or NTFY_TOPIC.lower() in ("canvas", "test", "alerts"):
        record(WARN, "NTFY_TOPIC is hard to guess",
               "The topic name is the only thing keeping your alerts private. "
               "Use something like canvas-alerts-7f3a91b2c4e8.")
    else:
        record(PASS, "NTFY_TOPIC is set and hard to guess")

    record(PASS if ANTHROPIC_API_KEY else WARN, "ANTHROPIC_API_KEY is set",
           "" if ANTHROPIC_API_KEY else "Only the AI Approved deck feature needs this.")

    parent = os.path.dirname(DATA_DIR) or "/"
    if os.path.isdir(DATA_DIR) and os.access(DATA_DIR, os.W_OK):
        record(PASS, f"DATA_DIR is writable ({DATA_DIR})")
    elif os.access(parent, os.W_OK):
        record(PASS, f"DATA_DIR can be created ({DATA_DIR})")
    else:
        record(FAIL, f"DATA_DIR is writable ({DATA_DIR})",
               "On Railway this must be a volume mounted at /data. Locally try DATA_DIR=./data")


# --- 2. canvas -------------------------------------------------------------

async def check_canvas():
    import httpx
    from app import canvas
    from app.config import CANVAS_BASE_URL, CANVAS_TOKEN

    if not (CANVAS_BASE_URL and CANVAS_TOKEN):
        record(SKIP, "Canvas checks", "CANVAS_BASE_URL or CANVAS_TOKEN missing")
        return None

    try:
        async with httpx.AsyncClient(timeout=30, headers={"Authorization": f"Bearer {CANVAS_TOKEN}"}) as client:
            response = await client.get(f"{CANVAS_BASE_URL}/api/v1/users/self")
    except Exception as exc:
        record(FAIL, "Canvas is reachable",
               f"{exc}\n         Check CANVAS_BASE_URL. Open it in a browser to confirm.")
        return None

    if response.status_code == 401:
        record(FAIL, "Canvas token is valid",
               "401 Unauthorized. The token is wrong, expired, or was revoked. Make a new one.")
        return None
    if response.status_code == 404:
        record(FAIL, "Canvas API is enabled",
               "404. Either CANVAS_BASE_URL is wrong or your school blocks API access.")
        return None
    if response.status_code != 200:
        record(FAIL, "Canvas token is valid", f"HTTP {response.status_code}: {response.text[:200]}")
        return None

    me = response.json()
    record(PASS, "Canvas token is valid",
           f"Signed in as {hide(me.get('name'), 2)} (id {hide(me.get('id'))})")

    try:
        courses = await canvas.list_active_courses()
    except Exception as exc:
        record(FAIL, "Courses load", str(exc))
        return None

    if not courses:
        record(WARN, "Active courses found",
               "Zero came back. Either the term has not started or your enrollments are not active.")
        return []
    record(PASS, f"Active courses found ({len(courses)})",
           ", ".join(hide(c.get("name"), 4) for c in courses[:6]))

    total = uploadable = graded = 0
    open_targets = []
    for course in courses:
        try:
            assignments = await canvas.list_assignments(course["id"])
        except Exception as exc:
            record(WARN, f"Assignments load for {hide(course.get('name'), 4)}", str(exc))
            continue
        for assignment in assignments:
            total += 1
            submission = assignment.get("submission") or {}
            if submission.get("workflow_state") == "graded" and submission.get("grade") is not None:
                graded += 1
            if "online_upload" in (assignment.get("submission_types") or []):
                uploadable += 1
                if not submission.get("submitted_at"):
                    open_targets.append((course, assignment))

    record(PASS if total else WARN, f"Assignments visible ({total})",
           f"{uploadable} accept file uploads, {graded} already graded")

    if not any((a.get("submission") is not None) for _, a in open_targets[:1]) and open_targets:
        record(WARN, "Submissions come inlined with assignments",
               "Canvas is not inlining them, so the app falls back to one extra call each. Works, just slower.")
    elif open_targets:
        record(PASS, "Submissions come inlined with assignments", "Keeps the poll light.")

    if open_targets:
        record(PASS, f"Assignments you could submit to ({len(open_targets)})",
               "\n         ".join(
                   f"{c['id']}:{a['id']}  {hide(c.get('name'), 4)} / {hide(a.get('name'), 4)}"
                   for c, a in open_targets[:8]))
    else:
        record(WARN, "Assignments you could submit to (0)",
               "Nothing open takes file uploads right now. The upload dropdown will be empty.")
    return open_targets


# --- 3. ntfy ---------------------------------------------------------------

async def check_ntfy():
    from app.config import NTFY_TOPIC
    from app.notify import notify

    if not NTFY_TOPIC:
        record(SKIP, "ntfy push", "NTFY_TOPIC missing")
        return
    sent = await notify("Canvas Assistant", "Setup check. If you see this, alerts work.",
                        tags=["white_check_mark"])
    if sent:
        record(PASS, "ntfy accepted a push", "Check your phone. Nothing there means the app "
                                             "topic does not match NTFY_TOPIC exactly.")
    else:
        record(FAIL, "ntfy accepted a push", "Check NTFY_SERVER and your network.")


# --- 4. anthropic ----------------------------------------------------------

async def check_anthropic():
    from app.config import ANTHROPIC_API_KEY, ANTHROPIC_MODEL

    if not ANTHROPIC_API_KEY:
        record(SKIP, "Anthropic key", "Not set. AI Approved mode will fail until it is.")
        return
    try:
        from anthropic import AsyncAnthropic
        response = await AsyncAnthropic(api_key=ANTHROPIC_API_KEY).messages.create(
            model=ANTHROPIC_MODEL, max_tokens=16,
            messages=[{"role": "user", "content": "Reply with the word ready."}],
        )
        text = next((b.text for b in response.content if b.type == "text"), "")
        record(PASS, f"Anthropic key works ({ANTHROPIC_MODEL})", f"Replied: {text.strip()[:40]}")
    except Exception as exc:
        hint = ""
        message = str(exc)
        if "credit" in message.lower() or "billing" in message.lower():
            hint = "\n         Add credit at console.anthropic.com."
        elif "authentication" in message.lower() or "401" in message:
            hint = "\n         The key is wrong. Copy it again from console.anthropic.com."
        elif "not_found" in message.lower() or "404" in message:
            hint = f"\n         Your account may not have {ANTHROPIC_MODEL}. Try ANTHROPIC_MODEL=claude-sonnet-5."
        record(FAIL, "Anthropic key works", message[:300] + hint)


# --- 5. local storage ------------------------------------------------------

def check_storage():
    from app import db
    from app.config import UPLOAD_DIR
    try:
        db.init_db()
        db.log("setup_check", "preflight ran")
        assert db.get_recent_log(1), "log write did not come back"
        os.makedirs(UPLOAD_DIR, exist_ok=True)
        probe = os.path.join(UPLOAD_DIR, ".probe")
        with open(probe, "w") as handle:
            handle.write("ok")
        os.remove(probe)
        record(PASS, "Database and upload folder work")
    except Exception as exc:
        record(FAIL, "Database and upload folder work", str(exc))


# --- 6. real submission (opt in) ------------------------------------------

async def check_submit(target):
    from app import canvas
    try:
        course_id, assignment_id = target.split(":", 1)
    except ValueError:
        record(FAIL, "Submit test", "Use --submit COURSE_ID:ASSIGNMENT_ID")
        return

    try:
        assignment = await canvas.get_assignment(course_id, assignment_id)
    except Exception as exc:
        record(FAIL, "Submit test", f"Could not read that assignment: {exc}")
        return

    name = assignment.get("name")
    print("\n" + "!" * 68)
    print("This creates a REAL submission to a REAL assignment.")
    print(f"  Assignment: {name}")
    print(f"  Due:        {assignment.get('due_at') or 'no due date'}")
    print("Your instructor will see the attempt in the submission history.")
    print("Canvas cannot delete a submission once it is made.")
    print("!" * 68)
    if input('Type the assignment name exactly to go ahead: ').strip() != (name or "").strip():
        record(SKIP, "Submit test", "Cancelled. Nothing was sent.")
        return

    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as handle:
        handle.write("Canvas Assistant setup check. Please disregard.\n")
        path = handle.name
    try:
        result = await canvas.submit_file(course_id, assignment_id, path, "setup-check.txt")
        record(PASS, "Submit test", f"Canvas accepted it. Submission id {result.get('id')}. "
                                    "Delete or resubmit over it in Canvas.")
    except Exception as exc:
        record(FAIL, "Submit test", f"{exc}\n         This is the exact error the app would "
                                    "report as a failed submit.")
    finally:
        os.unlink(path)


# --- main ------------------------------------------------------------------

async def main():
    global REDACT
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--redact", action="store_true",
                        help="mask course and account names so the output is safe to share")
    parser.add_argument("--submit", metavar="COURSE_ID:ASSIGNMENT_ID",
                        help="also make a real test submission, with confirmation")
    args = parser.parse_args()
    REDACT = args.redact

    load_env()
    print("Canvas Assistant setup check")
    print("Tokens are read locally and sent only to Canvas, ntfy, and Anthropic.")
    print("-" * 68)

    print("\nConfiguration")
    check_config()
    print("\nLocal storage")
    check_storage()
    print("\nCanvas")
    await check_canvas()
    print("\nNotifications")
    await check_ntfy()
    print("\nDeck generation")
    await check_anthropic()
    if args.submit:
        print("\nSubmission")
        await check_submit(args.submit)

    print("-" * 68)
    counts = {s: sum(1 for r, _, _ in results if r == s) for s in (PASS, FAIL, WARN, SKIP)}
    print(f"{counts[PASS]} passed, {counts[FAIL]} failed, "
          f"{counts[WARN]} warnings, {counts[SKIP]} skipped")
    if counts[FAIL]:
        print("\nFix the FAIL lines before deploying. Each one says what to do.")
    else:
        print("\nReady to deploy.")
    return 1 if counts[FAIL] else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
