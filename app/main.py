"""FastAPI front end: dashboard, upload, and the AI review gate.

One user, one password, a signed cookie. No user table.
"""
import asyncio
import hmac
import logging
import secrets
import os
import re
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import FastAPI, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from app import ai_generate, assistant, canvas, db, render, scheduler
from app.config import (
    APP_PASSWORD, COOKIE_SECURE, POLL_MINUTES, SECRET_KEY, SESSION_COOKIE,
    SESSION_MAX_AGE,
    SPEC_DIR, UPLOAD_DIR, missing_required,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TEMPLATES = Jinja2Templates(directory=os.path.join(os.path.dirname(__file__), "templates"))
def _media_type(path):
    """Serve the right type so the file opens instead of downloading as junk."""
    suffix = os.path.splitext(path)[1].lstrip(".").lower()
    return render.MEDIA_TYPES.get(suffix, "application/octet-stream")

# Templates render due dates through the same formatter the notifications use.
TEMPLATES.env.filters["due"] = scheduler._format_due

# A literal fallback key here would be a full authentication bypass: this repo
# is public, so anyone could sign their own session cookie and reach every route
# without the password. Fail safe instead. An unset SECRET_KEY gets a random key
# per boot, which nobody can forge; the only cost is that logins do not survive
# a restart, and the login page says so.
_serializer = URLSafeTimedSerializer(
    SECRET_KEY or secrets.token_urlsafe(32), salt="canvas-login"
)


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


# The interactive docs and the OpenAPI schema carry no auth dependency and
# cannot be given one, so on a public host they hand an anonymous visitor the
# whole route and form-field inventory. Nothing here needs them.
app = FastAPI(
    title="Canvas Assistant", lifespan=lifespan,
    docs_url=None, redoc_url=None, openapi_url=None,
)


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


def _login_context():
    """What the unauthenticated login page may safely say about configuration.

    Naming every unset secret here would tell an anonymous visitor which
    weakness to attempt. Only APP_PASSWORD is named, because without it nobody
    can log in to read the full list on the dashboard.
    """
    missing = missing_required()
    return {
        "error": None,
        "no_password": "APP_PASSWORD" in missing,
        "unconfigured": bool(missing),
        "ephemeral_sessions": not SECRET_KEY,
    }


@app.get("/login", response_class=HTMLResponse)
async def login_form(request: Request):
    if _is_logged_in(request):
        return RedirectResponse("/", status_code=303)
    return TEMPLATES.TemplateResponse(request, "login.html", _login_context())


@app.post("/login", response_class=HTMLResponse)
async def login_submit(request: Request, password: str = Form(...)):
    if not APP_PASSWORD or not hmac.compare_digest(password, APP_PASSWORD):
        db.log("login_failed", "wrong password", level="error")
        return TEMPLATES.TemplateResponse(
            request, "login.html",
            dict(_login_context(), error="Wrong password."),
            status_code=401,
        )
    response = RedirectResponse("/", status_code=303)
    response.set_cookie(
        SESSION_COOKIE,
        _serializer.dumps("ok"),
        max_age=SESSION_MAX_AGE,
        httponly=True,
        samesite="lax",
        secure=COOKIE_SECURE,
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
                # "Just a prompt" is a conversation, not a one-shot generate.
                return RedirectResponse(
                    f"/chat/{course_id}/{assignment_id}", status_code=303
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
    suffix = os.path.splitext(row["file_path"])[1] or ".bin"
    return FileResponse(
        row["file_path"],
        media_type=_media_type(row["file_path"]),
        filename=f"{_safe_name(row['assignment_name'])}{suffix}",
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


# --- assignment chat -------------------------------------------------------

async def _assignment_specs(assignment):
    """Spec files attached to the assignment, downloaded once and cached.

    Instructors often leave the description nearly empty and put the real
    requirements in an attached PDF, so these matter as much as the brief.
    """
    folder = os.path.join(SPEC_DIR, str(assignment.get("id")))
    if os.path.isdir(folder):
        return [
            {"filename": name.split("_", 1)[-1], "path": os.path.join(folder, name)}
            for name in sorted(os.listdir(folder))
        ]
    try:
        return await canvas.get_assignment_attachments(assignment, folder)
    except Exception as exc:
        logger.warning("spec download failed for %s: %s", assignment.get("name"), exc)
        return []


async def _chat_context(course_id, assignment_id):
    """(course, assignment, specs, context) for one assignment."""
    assignment = await canvas.get_assignment(course_id, assignment_id)
    try:
        course = await canvas.get_course(course_id)
        course["display_name"] = canvas.display_name(course)
    except Exception:
        course = {"id": course_id, "name": "", "display_name": ""}
    specs = await _assignment_specs(assignment)
    context = assistant.build_context(
        course, assignment, specs, due_text=scheduler._format_due(assignment.get("due_at"))
    )
    return course, assignment, specs, context


def _chat_page(request, course, assignment, specs, course_id, assignment_id,
               error=None, message=None, status=200):
    return TEMPLATES.TemplateResponse(request, "chat.html", {
        "course": course,
        "assignment": assignment,
        "specs": specs,
        "course_id": course_id,
        "assignment_id": assignment_id,
        "messages": db.get_chat_history(assignment_id),
        "default_format": assistant.default_format(assignment),
        "error": error,
        "message": message,
        "poll_minutes": POLL_MINUTES,
    }, status_code=status)


@app.get("/chat", response_class=HTMLResponse)
async def chat_index(request: Request, _=Depends(require_login)):
    """Pick an assignment to talk about."""
    error = None
    options = []
    try:
        queued = db.get_queued_assignment_ids()
        for course, assignments in await _load_courses():
            for a in assignments:
                options.append({
                    "value": f"{course['id']}|{a['id']}",
                    "course_name": course.get("display_name") or course.get("name", ""),
                    "name": a.get("name") or "Untitled",
                    "due_at": a.get("due_at"),
                    "queued": str(a["id"]) in queued,
                })
    except Exception as exc:
        error = f"Could not load assignments: {exc}"
    threads = {t["assignment_id"]: t for t in db.get_active_chats()}
    return TEMPLATES.TemplateResponse(request, "chat_index.html", {
        "options": options, "threads": threads, "error": error,
    })


@app.get("/chat/{course_id}/{assignment_id}", response_class=HTMLResponse)
async def chat_thread(request: Request, course_id: str, assignment_id: str,
                      _=Depends(require_login)):
    try:
        course, assignment, specs, _context = await _chat_context(course_id, assignment_id)
    except Exception as exc:
        raise HTTPException(status_code=404, detail=f"Could not load that assignment: {exc}")
    return _chat_page(request, course, assignment, specs, course_id, assignment_id)


@app.post("/chat/{course_id}/{assignment_id}", response_class=HTMLResponse)
async def chat_send(request: Request, course_id: str, assignment_id: str,
                    _=Depends(require_login), message: str = Form("")):
    error = None
    course = assignment = None
    specs = []
    try:
        course, assignment, specs, context = await _chat_context(course_id, assignment_id)
        history = db.get_chat_history(assignment_id)
        answer = await assistant.reply(context, history, message)
        db.add_chat_message(course_id, assignment_id, "user", message.strip())
        db.add_chat_message(course_id, assignment_id, "assistant", answer)
    except Exception as exc:
        error = str(exc)
        db.log("chat_failed", error, level="error")
        if assignment is None:
            raise HTTPException(status_code=404, detail=error)
    return _chat_page(request, course, assignment, specs, course_id, assignment_id,
                      error=error, status=400 if error else 200)


@app.post("/chat/{course_id}/{assignment_id}/save", response_class=HTMLResponse)
async def chat_save(request: Request, course_id: str, assignment_id: str,
                    _=Depends(require_login), kind: str = Form("auto"),
                    instruction: str = Form("")):
    """Render the thread into an accessible file and send it for review."""
    error = None
    message = None
    course = assignment = None
    specs = []
    try:
        course, assignment, specs, context = await _chat_context(course_id, assignment_id)
        chosen = assistant.default_format(assignment) if kind == "auto" else kind
        if chosen not in render.RENDERERS:
            raise ValueError("Pick Word or PowerPoint.")

        assignment_name = assignment.get("name") or "Assignment"
        path, filename = await assistant.build_artifact(
            context, db.get_chat_history(assignment_id), chosen,
            assignment_name, UPLOAD_DIR, instruction,
        )
        db.add_pending_upload(
            course_id, assignment_id, assignment_name, path, filename,
            course_name=course.get("display_name") or course.get("name", ""),
            ai_generated=True, hold_for_review=True,
        )
        db.log("generated", f"{assignment_name}: {filename} awaiting review")
        label = "PowerPoint deck" if chosen == "pptx" else "Word document"
        message = (f"Built the {label}. Nothing is submitted yet. "
                   "Open Review to check it and approve.")
    except Exception as exc:
        error = str(exc)
        db.log("chat_save_failed", error, level="error")
        if assignment is None:
            raise HTTPException(status_code=404, detail=error)
    return _chat_page(request, course, assignment, specs, course_id, assignment_id,
                      error=error, message=message, status=400 if error else 200)


@app.post("/chat/{course_id}/{assignment_id}/clear")
async def chat_clear(course_id: str, assignment_id: str, _=Depends(require_login)):
    removed = db.clear_chat(assignment_id)
    db.log("chat_cleared", f"assignment {assignment_id}: {removed} messages")
    return RedirectResponse(f"/chat/{course_id}/{assignment_id}", status_code=303)


@app.get("/healthz")
async def healthz():
    return {"ok": True, "time": datetime.now(timezone.utc).isoformat()}
