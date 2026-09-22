"""Background poll: watch Canvas, notify, and drain the submit queue.

Every Canvas call sits in its own try/except. One broken course, one deleted
assignment, or one rejected upload must never stop the rest of the sweep.
"""
import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app import canvas, db
from app.config import MAX_SUBMIT_ATTEMPTS, POLL_MINUTES, TIMEZONE
from app.notify import notify

logger = logging.getLogger(__name__)

_scheduler = None

try:
    LOCAL_TZ = ZoneInfo(TIMEZONE)
except (ZoneInfoNotFoundError, ValueError):
    logger.warning("unknown TIMEZONE %r, falling back to UTC", TIMEZONE)
    LOCAL_TZ = timezone.utc


def _format_due(due_at):
    """Render a Canvas UTC timestamp in local time.

    '2026-10-02T03:59:00Z' -> 'Thu 1 Oct, 11:59 PM EDT' in America/New_York.
    Canvas always sends UTC, so skipping the conversion shifts a late-night
    deadline onto the following day.
    """
    if not due_at:
        return "no due date"
    try:
        stamp = datetime.fromisoformat(due_at.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return due_at
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    local = stamp.astimezone(LOCAL_TZ)
    return local.strftime("%a %d %b, %I:%M %p %Z").replace(" 0", " ")


# --- assignments and grades ------------------------------------------------

async def _check_assignment(course, assignment, announce):
    """Notify on a first sighting and on a new grade. Never raises."""
    course_name = course.get("name", "Course")
    assignment_id = assignment.get("id")
    name = assignment.get("name") or "Untitled assignment"

    try:
        if not db.has_seen_assignment(assignment_id):
            db.mark_assignment_seen(
                assignment_id, course.get("id"), name, assignment.get("due_at")
            )
            if announce:
                db.log("new_assignment", f"{course_name}: {name}")
                await notify(
                    course_name,
                    f"New assignment: {name} — due {_format_due(assignment.get('due_at'))}",
                    tags=["memo"],
                )
    except Exception as exc:
        db.log("error", f"assignment check failed for {name}: {exc}", level="error")

    # list_assignments asks Canvas to inline the submission, which saves one
    # request per assignment. Fall back when Canvas leaves it out.
    try:
        submission = assignment.get("submission")
        if submission is None:
            submission = await canvas.get_my_submission(course.get("id"), assignment_id)
        if not submission:
            return
        if submission.get("workflow_state") != "graded":
            return
        grade = submission.get("grade")
        if grade is None:
            return
        submission_id = submission.get("id")
        if db.has_seen_grade(submission_id, grade):
            return
        db.mark_grade_seen(submission_id, grade)
        db.log("graded", f"{course_name}: {name} = {grade}")
        if announce:
            await notify(course_name, f"Graded: {name} — {grade}", tags=["chart_with_upwards_trend"])
    except Exception as exc:
        db.log("error", f"grade check failed for {name}: {exc}", level="error")


async def _sweep_canvas(announce):
    try:
        courses = await canvas.list_active_courses()
    except Exception as exc:
        db.log("error", f"could not list courses: {exc}", level="error")
        return

    for course in courses:
        try:
            assignments = await canvas.list_assignments(course.get("id"))
        except Exception as exc:
            db.log(
                "error",
                f"could not list assignments for {course.get('name')}: {exc}",
                level="error",
            )
            continue
        for assignment in assignments:
            await _check_assignment(course, assignment, announce)


# --- submit queue ----------------------------------------------------------

async def _drain_queue():
    try:
        queued = db.get_unsubmitted_uploads()
    except Exception as exc:
        db.log("error", f"could not read the submit queue: {exc}", level="error")
        return

    for row in queued:
        name = row["assignment_name"]
        try:
            await canvas.submit_file(
                row["course_id"],
                row["assignment_id"],
                row["file_path"],
                row["original_filename"],
            )
        except Exception as exc:
            detail = str(exc)
            db.mark_submitted(row["id"], False, detail)
            attempts = row["attempts"] + 1
            db.log("submit_failed", f"{name}: {detail}", level="error")
            if attempts >= MAX_SUBMIT_ATTEMPTS:
                message = f"Submit FAILED for {name} after {attempts} tries. {detail}"
            else:
                message = f"Submit failed for {name} (try {attempts} of {MAX_SUBMIT_ATTEMPTS}). {detail}"
            await notify("Canvas submit failed", message, priority="urgent", tags=["rotating_light"])
            continue

        db.mark_submitted(row["id"], True)
        db.log("submitted", name)
        await notify("Canvas", f"Submitted: {name}", tags=["white_check_mark"])


# --- entry points ----------------------------------------------------------

async def poll_once():
    """One full sweep. Safe to call by hand from the dashboard."""
    # On a cold database every assignment looks new. Record them quietly the
    # first time so the first poll does not fire off dozens of pushes.
    first_run = not db.get_recent_log(1)
    if first_run:
        db.log("baseline", "first poll, recording current Canvas state silently")

    await _sweep_canvas(announce=not first_run)
    await _drain_queue()

    if first_run:
        db.log("baseline_done", "baseline recorded, future changes will notify")


async def _job():
    try:
        await poll_once()
    except Exception as exc:  # last resort, keeps the job scheduled
        logger.exception("poll failed")
        db.log("error", f"poll crashed: {exc}", level="error")


def start_scheduler():
    """Start the recurring poll. Returns the running scheduler."""
    global _scheduler
    if _scheduler is not None:
        return _scheduler

    _scheduler = AsyncIOScheduler()
    _scheduler.add_job(
        _job,
        "interval",
        minutes=POLL_MINUTES,
        id="canvas_poll",
        next_run_time=datetime.now(),   # sweep once at boot
        max_instances=1,
        coalesce=True,
        misfire_grace_time=300,
    )
    _scheduler.start()
    db.log("scheduler_started", f"polling every {POLL_MINUTES} minutes")
    return _scheduler


def shutdown_scheduler():
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None
