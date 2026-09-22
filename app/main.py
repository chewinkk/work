"""FastAPI front end: dashboard, upload, and the AI review gate.

One user, one password, a signed cookie. No user table.
"""
import asyncio
import hmac
import logging
import os
import re
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import FastAPI, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from app import ai_generate, canvas, db, scheduler
from app.config import (
    APP_PASSWORD, POLL_MINUTES, SECRET_KEY, SESSION_COOKIE, SESSION_MAX_AGE,
    UPLOAD_DIR, missing_required,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TEMPLATES = Jinja2Templates(directory=os.path.join(os.path.dirname(__file__), "templates"))
PPTX_TYPE = "application/vnd.openxmlformats-officedocument.presentationml.presentation"

# Templates render due dates through the same formatter the notifications use.
TEMPLATES.env.filters["due"] = scheduler._format_due

# SECRET_KEY is validated on the dashboard; fall back so /login can still render
# and tell you what is missing instead of crashing the container on boot.
_serializer = URLSafeTimedSerializer(SECRET_KEY or "unconfigured", salt="canvas-login")


@asynccontextmanager
async def lifespan(app):
    db.init_db()
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    missing = missing_required()
    if missing:
        logger.warning("missing env vars: %s", ", ".join(missing))
        db.log("config_warning", "missing env vars: " + ", ".join(missing), level="error")
    else:
        scheduler.start_scheduler()
    yield
    scheduler.shutdown_scheduler()


app = FastAPI(title="Canvas Assistant", lifespan=lifespan)


# --- auth ------------------------------------------------------------------

def _is_logged_in(request):
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return False
    try:
        _serializer.loads(token, max_age=SESSION_MAX_AGE)
        return True
    except (BadSignature, SignatureExpired):
        return False


async def require_login(request: Request):
    """Dependency. Bounces anonymous visitors to the login page."""
    if not _is_logged_in(request):
        raise HTTPException(
            status_code=307, headers={"Location": "/login"}, detail="login required"
        )
    return True


@app.exception_handler(HTTPException)
async def _redirect_unauthenticated(request: Request, exc: HTTPException):
    if exc.status_code == 307 and "Location" in (exc.headers or {}):
        return RedirectResponse(exc.headers["Location"], status_code=303)
    return TEMPLATES.TemplateResponse(
        request, "error.html", {"code": exc.status_code, "message": exc.detail},
        status_code=exc.status_code,
    )


@app.get("/login", response_class=HTMLResponse)
async def login_form(request: Request):
    if _is_logged_in(request):
        return RedirectResponse("/", status_code=303)
    return TEMPLATES.TemplateResponse(
        request, "login.html", {"error": None, "missing": missing_required()}
    )


@app.post("/login", response_class=HTMLResponse)
async def login_submit(request: Request, password: str = Form(...)):
    if not APP_PASSWORD or not hmac.compare_digest(password, APP_PASSWORD):
        db.log("login_failed", "wrong password", level="error")
        return TEMPLATES.TemplateResponse(
            request, "login.html",
            {"error": "Wrong password.", "missing": missing_required()},
            status_code=401,
        )
    response = RedirectResponse("/", status_code=303)
    response.set_cookie(
        SESSION_COOKIE,
        _serializer.dumps("ok"),
        max_age=SESSION_MAX_AGE,
        httponly=True,
        samesite="lax",
        secure=request.url.scheme == "https",
    )
    return response


@app.post("/logout")
async def logout():
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(SESSION_COOKIE)
    return response


# --- shared Canvas reads ---------------------------------------------------

def _submission_status(assignment):
    submission = assignment.get("submission") or {}
    state = submission.get("workflow_state")
    if state == "graded" and submission.get("grade") is not None:
        return f"Graded: {submission['grade']}"
    if submission.get("submitted_at"):
        return "Submitted"
    if state == "pending_review":
        return "Awaiting review"
    return "Not submitted"


def _is_open(assignment):
    submission = assignment.get("submission") or {}
    if submission.get("submitted_at"):
        return False
    types = assignment.get("submission_types") or []
    return "online_upload" in types


async def _load_courses():
    """[(course, [assignment, ...]), ...] with per-course failures isolated."""
    result = []
    courses = await canvas.list_active_courses()
    for course in courses:
        try:
            assignments = await canvas.list_assignments(course["id"])
        except Exception as exc:
            logger.warning("assignments failed for %s: %s", course.get("name"), exc)
            assignments = []
        result.append((course, assignments))
    return result


async def _open_assignments():
    """Assignments you can still upload to and have not already queued."""
    queued = db.get_queued_assignment_ids()
    options = []
    for course, assignments in await _load_courses():
        for assignment in assignments:
            if not _is_open(assignment):
                continue
            if str(assignment["id"]) in queued:
                continue
            options.append({
                "value": f"{course['id']}|{assignment['id']}",
                "course_name": course.get("display_name") or course.get("name", "Course"),
                "name": assignment.get("name") or "Untitled",
                "due_at": assignment.get("due_at"),
            })
    return options


# --- dashboard -------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request, _=Depends(require_login)):
    error = None
    courses = []
    try:
        courses = await _load_courses()
    except Exception as exc:
        error = f"Could not reach Canvas: {exc}"
        db.log("error", error, level="error")

    return TEMPLATES.TemplateResponse(request, "dashboard.html", {
        "courses": courses,
        "status_of": _submission_status,
        "activity": db.get_recent_log(30),
        "awaiting": len(db.get_uploads_awaiting_review()),
        "queued": len(db.get_unsubmitted_uploads()),
        "poll_minutes": POLL_MINUTES,
        "missing": missing_required(),
        "error": error,
    })


@app.post("/poll")
async def poll_now(_=Depends(require_login)):
    """Run a sweep now instead of waiting for the next interval."""
    asyncio.create_task(scheduler._job())
    return RedirectResponse("/", status_code=303)


# --- upload ----------------------------------------------------------------

def _safe_name(filename):
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", os.path.basename(filename or "file"))
    return cleaned.strip("._") or "file"


def _store_path(filename):
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    return os.path.join(UPLOAD_DIR, f"{uuid.uuid4().hex[:8]}_{_safe_name(filename)}")


async def _save_upload(upload, destination):
    with open(destination, "wb") as handle:
        while chunk := await upload.read(1024 * 1024):
            handle.write(chunk)
    return destination


def _split_target(value):
    course_id, _, assignment_id = (value or "").partition("|")
    if not course_id or not assignment_id:
        raise ValueError("Pick an assignment.")
    return course_id, assignment_id


@app.get("/upload", response_class=HTMLResponse)
async def upload_form(request: Request, _=Depends(require_login)):
    error = None
    options = []
    try:
        options = await _open_assignments()
    except Exception as exc:
        error = f"Could not load assignments: {exc}"
    return TEMPLATES.TemplateResponse(request, "upload.html", {
        "options": options, "error": error, "message": None,
    })


@app.post("/upload", response_class=HTMLResponse)
async def upload_submit(
    request: Request,
    _=Depends(require_login),
    mode: str = Form("manual"),
    assignment: str = Form(""),
    ai_source: str = Form("prompt"),
    prompt: str = Form(""),
    upload_file: UploadFile = File(None),
):
    message = None
    error = None
    try:
        course_id, assignment_id = _split_target(assignment)
        details = await canvas.get_assignment(course_id, assignment_id)
        assignment_name = details.get("name") or "Assignment"
        try:
            course_name = (await canvas.get_course(course_id)).get("name", "")
        except Exception:
            course_name = ""

        if mode == "manual":
            if upload_file is None or not upload_file.filename:
                raise ValueError("Choose a file to upload.")
            stored = await _save_upload(upload_file, _store_path(upload_file.filename))
            db.add_pending_upload(
                course_id, assignment_id, assignment_name, stored,
                upload_file.filename, course_name=course_name,
                ai_generated=False, hold_for_review=False,
            )
            db.log("queued_manual", f"{assignment_name}: {upload_file.filename}")
            message = (
                f"{upload_file.filename} is queued for {assignment_name}. "
                f"It submits within {POLL_MINUTES} minutes."
            )

        else:
            deck_path = os.path.join(
                UPLOAD_DIR,
                f"{uuid.uuid4().hex[:8]}_{_safe_name(assignment_name)}.pptx",
            )
            if ai_source == "source":
                if upload_file is None or not upload_file.filename:
                    raise ValueError("Choose a source file, or switch to Just a prompt.")
                source = await _save_upload(upload_file, _store_path(upload_file.filename))
                try:
                    await ai_generate.generate_presentation(
                        source, upload_file.filename, assignment_name, deck_path
                    )
                finally:
                    try:
                        os.remove(source)
                    except OSError:
                        pass
            else:
                if not prompt.strip():
                    raise ValueError("Write a prompt, or switch to I have source material.")
                await ai_generate.generate_presentation_from_prompt(
                    prompt, assignment_name, deck_path
                )

            db.add_pending_upload(
                course_id, assignment_id, assignment_name, deck_path,
                os.path.basename(deck_path), course_name=course_name,
                ai_generated=True, hold_for_review=True,
            )
            db.log("generated", f"{assignment_name}: deck awaiting review")
            message = (
                f"Deck built for {assignment_name}. Nothing is submitted yet. "
                "Open Review to check it and approve."
            )

    except Exception as exc:
        error = str(exc)
        db.log("upload_failed", error, level="error")

    try:
        options = await _open_assignments()
    except Exception:
        options = []
    return TEMPLATES.TemplateResponse(
        request, "upload.html",
        {"options": options, "error": error, "message": message},
        status_code=400 if error else 200,
    )


# --- review ----------------------------------------------------------------

@app.get("/review", response_class=HTMLResponse)
async def review_list(request: Request, _=Depends(require_login)):
    return TEMPLATES.TemplateResponse(request, "review.html", {
        "uploads": db.get_uploads_awaiting_review(),
        "poll_minutes": POLL_MINUTES,
    })


@app.get("/review/{upload_id}/file")
async def review_file(upload_id: int, _=Depends(require_login)):
    row = db.get_upload(upload_id)
    if row is None or not os.path.exists(row["file_path"]):
        raise HTTPException(status_code=404, detail="That file is gone.")
    return FileResponse(
        row["file_path"],
        media_type=PPTX_TYPE,
        filename=f"{_safe_name(row['assignment_name'])}.pptx",
    )


@app.post("/review/{upload_id}/approve")
async def review_approve(upload_id: int, _=Depends(require_login)):
    row = db.mark_reviewed(upload_id, True)
    if row is None:
        raise HTTPException(status_code=404, detail="No such upload.")
    db.log("approved", f"{row['assignment_name']} queued for submit")
    return RedirectResponse("/review", status_code=303)


@app.post("/review/{upload_id}/reject")
async def review_reject(upload_id: int, _=Depends(require_login)):
    row = db.mark_reviewed(upload_id, False)
    if row is None:
        raise HTTPException(status_code=404, detail="No such upload.")
    db.log("rejected", f"{row['assignment_name']} deck deleted")
    return RedirectResponse("/review", status_code=303)


@app.get("/healthz")
async def healthz():
    return {"ok": True, "time": datetime.now(timezone.utc).isoformat()}
