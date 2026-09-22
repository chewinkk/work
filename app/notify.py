"""Push notifications via ntfy.sh.

Publishes as JSON to the server root rather than with HTTP headers, so titles
containing non-latin-1 characters (an assignment name with a dash or an accent)
do not blow up header encoding.

Swallows every ordinary failure. A dead notification service must not stop the
scheduler from submitting work. Cancellation still propagates, as it should, so
container shutdown is never delayed.
"""
import logging

import httpx

from app.config import NTFY_SERVER, NTFY_TOPIC

logger = logging.getLogger(__name__)

TIMEOUT = httpx.Timeout(20.0, connect=10.0)

PRIORITIES = {"min": 1, "low": 2, "default": 3, "high": 4, "urgent": 5}


async def notify(title, message, priority="default", tags=None):
    """Send one push. Returns True when ntfy accepted it."""
    if not NTFY_TOPIC:
        logger.warning("NTFY_TOPIC is unset, dropping notification: %s", title)
        return False

    payload = {
        "topic": NTFY_TOPIC,
        "title": title,
        "message": message,
        "priority": PRIORITIES.get(priority, 3),
    }
    if tags:
        payload["tags"] = list(tags)

    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            response = await client.post(NTFY_SERVER, json=payload)
            response.raise_for_status()
        return True
    except Exception as exc:
        logger.warning("ntfy publish failed: %s", exc)
        return False
