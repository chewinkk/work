"""SQLite storage.

One connection per call. Single user, a poll every POLL_MINUTES, so a pool
buys nothing. WAL mode keeps the scheduler writing while a page reads.

All Canvas ids are stored as TEXT. Canvas returns them as ints in JSON and as
strings in form posts, so normalising on the way in avoids lookups that miss.
"""
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

from app.config import DB_PATH, MAX_SUBMIT_ATTEMPTS

SCHEMA = """
CREATE TABLE IF NOT EXISTS pending_uploads (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    course_id         TEXT    NOT NULL,
    assignment_id     TEXT    NOT NULL,
    assignment_name   TEXT    NOT NULL,
    course_name       TEXT    NOT NULL DEFAULT '',
    file_path         TEXT    NOT NULL,
    original_filename TEXT    NOT NULL,
    ai_generated      INTEGER NOT NULL DEFAULT 0,
    hold_for_review   INTEGER NOT NULL DEFAULT 0,
    submitted         INTEGER NOT NULL DEFAULT 0,
    attempts          INTEGER NOT NULL DEFAULT 0,
    last_error        TEXT,
    created_at        TEXT    NOT NULL,
    submitted_at      TEXT
);

CREATE TABLE IF NOT EXISTS seen_assignments (
    assignment_id TEXT PRIMARY KEY,
    course_id     TEXT,
    name          TEXT,
    due_at        TEXT,
    first_seen    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS seen_grades (
    submission_id TEXT NOT NULL,
    grade         TEXT NOT NULL,
    seen_at       TEXT NOT NULL,
    PRIMARY KEY (submission_id, grade)
);

CREATE TABLE IF NOT EXISTS activity_log (
    id     INTEGER PRIMARY KEY AUTOINCREMENT,
    ts     TEXT NOT NULL,
    level  TEXT NOT NULL DEFAULT 'info',
    event  TEXT NOT NULL,
    detail TEXT
);

CREATE TABLE IF NOT EXISTS chat_messages (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    course_id     TEXT NOT NULL,
    assignment_id TEXT NOT NULL,
    role          TEXT NOT NULL,
    content       TEXT NOT NULL,
    created_at    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_chat_thread
    ON chat_messages (assignment_id, id);
CREATE INDEX IF NOT EXISTS idx_uploads_queue
    ON pending_uploads (submitted, hold_for_review, attempts);
CREATE INDEX IF NOT EXISTS idx_log_ts ON activity_log (ts DESC);
"""


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _connect():
    directory = os.path.dirname(DB_PATH)
    if directory:
        os.makedirs(directory, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


@contextmanager
def _tx():
    conn = _connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    """Create tables. Safe to call on every boot."""
    with _tx() as conn:
        conn.executescript(SCHEMA)


# --- activity log ----------------------------------------------------------

def log(event, detail="", level="info"):
    with _tx() as conn:
        conn.execute(
            "INSERT INTO activity_log (ts, level, event, detail) VALUES (?, ?, ?, ?)",
            (_now(), level, event, str(detail)[:2000]),
        )


def get_recent_log(limit=40):
    with _tx() as conn:
        rows = conn.execute(
            "SELECT * FROM activity_log ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(row) for row in rows]


# --- assignment / grade dedupe --------------------------------------------

def has_seen_assignment(assignment_id):
    with _tx() as conn:
        row = conn.execute(
            "SELECT 1 FROM seen_assignments WHERE assignment_id = ?",
            (str(assignment_id),),
        ).fetchone()
    return row is not None


def mark_assignment_seen(assignment_id, course_id, name, due_at):
    with _tx() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO seen_assignments"
            " (assignment_id, course_id, name, due_at, first_seen)"
            " VALUES (?, ?, ?, ?, ?)",
            (str(assignment_id), str(course_id), name, due_at, _now()),
        )


def has_seen_grade(submission_id, grade):
    with _tx() as conn:
        row = conn.execute(
            "SELECT 1 FROM seen_grades WHERE submission_id = ? AND grade = ?",
            (str(submission_id), str(grade)),
        ).fetchone()
    return row is not None


def mark_grade_seen(submission_id, grade):
    with _tx() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO seen_grades (submission_id, grade, seen_at)"
            " VALUES (?, ?, ?)",
            (str(submission_id), str(grade), _now()),
        )


# --- upload queue ----------------------------------------------------------

def add_pending_upload(course_id, assignment_id, assignment_name, file_path,
                       original_filename, course_name="", ai_generated=False,
                       hold_for_review=False):
    """Queue a file. Returns the new row id.

    hold_for_review=True keeps the row out of get_unsubmitted_uploads() until
    mark_reviewed(id, True) clears the flag.
    """
    with _tx() as conn:
        cur = conn.execute(
            "INSERT INTO pending_uploads"
            " (course_id, assignment_id, assignment_name, course_name, file_path,"
            "  original_filename, ai_generated, hold_for_review, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                str(course_id), str(assignment_id), assignment_name, course_name,
                file_path, original_filename,
                int(bool(ai_generated)), int(bool(hold_for_review)), _now(),
            ),
        )
        return cur.lastrowid


def get_unsubmitted_uploads():
    """Rows the scheduler should submit on its next run."""
    with _tx() as conn:
        rows = conn.execute(
            "SELECT * FROM pending_uploads"
            " WHERE submitted = 0 AND hold_for_review = 0 AND attempts < ?"
            " ORDER BY id",
            (MAX_SUBMIT_ATTEMPTS,),
        ).fetchall()
    return [dict(row) for row in rows]


def get_uploads_awaiting_review():
    with _tx() as conn:
        rows = conn.execute(
            "SELECT * FROM pending_uploads"
            " WHERE hold_for_review = 1 AND submitted = 0"
            " ORDER BY id DESC"
        ).fetchall()
    return [dict(row) for row in rows]


def get_upload(upload_id):
    with _tx() as conn:
        row = conn.execute(
            "SELECT * FROM pending_uploads WHERE id = ?", (upload_id,)
        ).fetchone()
    return dict(row) if row else None


def get_queued_assignment_ids():
    """Assignment ids already queued or submitted, to hide from the dropdown."""
    with _tx() as conn:
        rows = conn.execute(
            "SELECT DISTINCT assignment_id FROM pending_uploads"
            " WHERE submitted = 1 OR attempts < ?",
            (MAX_SUBMIT_ATTEMPTS,),
        ).fetchall()
    return {row["assignment_id"] for row in rows}


def mark_submitted(upload_id, success, error=None):
    """Record the outcome of one Canvas submit attempt."""
    with _tx() as conn:
        if success:
            conn.execute(
                "UPDATE pending_uploads"
                " SET submitted = 1, submitted_at = ?, last_error = NULL"
                " WHERE id = ?",
                (_now(), upload_id),
            )
        else:
            conn.execute(
                "UPDATE pending_uploads"
                " SET attempts = attempts + 1, last_error = ?"
                " WHERE id = ?",
                (str(error)[:2000] if error else "unknown error", upload_id),
            )


def mark_reviewed(upload_id, approved):
    """Approve an AI-generated upload into the submit queue, or reject it.

    Rejecting deletes the row and the generated file from disk.
    """
    row = get_upload(upload_id)
    if row is None:
        return None
    if approved:
        with _tx() as conn:
            conn.execute(
                "UPDATE pending_uploads SET hold_for_review = 0 WHERE id = ?",
                (upload_id,),
            )
        return row

    with _tx() as conn:
        conn.execute("DELETE FROM pending_uploads WHERE id = ?", (upload_id,))
    try:
        os.remove(row["file_path"])
    except OSError:
        pass
    return row


# --- assignment chat -------------------------------------------------------

def add_chat_message(course_id, assignment_id, role, content):
    """Append one turn. role is 'user' or 'assistant'."""
    with _tx() as conn:
        cur = conn.execute(
            "INSERT INTO chat_messages"
            " (course_id, assignment_id, role, content, created_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (str(course_id), str(assignment_id), role, content, _now()),
        )
        return cur.lastrowid


def get_chat_history(assignment_id, limit=80):
    """The thread for one assignment, oldest first."""
    with _tx() as conn:
        rows = conn.execute(
            "SELECT * FROM chat_messages WHERE assignment_id = ?"
            " ORDER BY id DESC LIMIT ?",
            (str(assignment_id), limit),
        ).fetchall()
    return [dict(row) for row in reversed(rows)]


def get_chat_message(message_id):
    with _tx() as conn:
        row = conn.execute(
            "SELECT * FROM chat_messages WHERE id = ?", (message_id,)
        ).fetchone()
    return dict(row) if row else None


def clear_chat(assignment_id):
    with _tx() as conn:
        cur = conn.execute(
            "DELETE FROM chat_messages WHERE assignment_id = ?",
            (str(assignment_id),),
        )
        return cur.rowcount


def get_active_chats():
    """Assignments with a thread, newest activity first, for the nav badge."""
    with _tx() as conn:
        rows = conn.execute(
            "SELECT assignment_id, course_id, COUNT(*) AS messages,"
            "       MAX(created_at) AS last_at"
            " FROM chat_messages GROUP BY assignment_id, course_id"
            " ORDER BY last_at DESC"
        ).fetchall()
    return [dict(row) for row in rows]
