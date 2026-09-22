"""Environment configuration, read once at import.

Every value has a safe default except the secrets, which are validated by
`missing_required()` and surfaced on the dashboard instead of crashing boot.
"""
import os

CANVAS_BASE_URL = os.environ.get("CANVAS_BASE_URL", "").rstrip("/")
CANVAS_TOKEN = os.environ.get("CANVAS_TOKEN", "")

APP_PASSWORD = os.environ.get("APP_PASSWORD", "")
SECRET_KEY = os.environ.get("SECRET_KEY", "")

NTFY_SERVER = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-opus-5")

POLL_MINUTES = int(os.environ.get("POLL_MINUTES", "30"))

# Canvas returns every due date in UTC. Without this, a deadline of 11:59 PM
# shows up as 3:59 AM the following day, which is the wrong day.
TIMEZONE = os.environ.get("TIMEZONE", "America/New_York")

DATA_DIR = os.environ.get("DATA_DIR", "/data")
UPLOAD_DIR = os.path.join(DATA_DIR, "uploads")
# Specification files downloaded from Canvas, cached per assignment.
SPEC_DIR = os.path.join(DATA_DIR, "specs")
DB_PATH = os.path.join(DATA_DIR, "canvas.db")

# Stop retrying a submission after this many consecutive Canvas failures so a
# permanently broken upload does not notify you every POLL_MINUTES forever.
MAX_SUBMIT_ATTEMPTS = int(os.environ.get("MAX_SUBMIT_ATTEMPTS", "3"))

SESSION_COOKIE = "canvas_session"
SESSION_MAX_AGE = 60 * 60 * 24 * 30  # 30 days

# Railway terminates TLS at its edge, so the app itself sees plain HTTP unless
# uvicorn is told to trust the proxy. Deciding the Secure flag from the observed
# scheme therefore got it wrong in production. Default it on, and let local
# HTTP development opt out explicitly.
COOKIE_SECURE = os.environ.get("COOKIE_SECURE", "true").strip().lower() not in (
    "0", "false", "no", "off",
)


def missing_required():
    """Names of env vars the app needs before anything works."""
    required = {
        "CANVAS_BASE_URL": CANVAS_BASE_URL,
        "CANVAS_TOKEN": CANVAS_TOKEN,
        "APP_PASSWORD": APP_PASSWORD,
        "SECRET_KEY": SECRET_KEY,
        "NTFY_TOPIC": NTFY_TOPIC,
    }
    return [name for name, value in required.items() if not value]
