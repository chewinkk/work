"""Canvas LMS REST wrapper.

Async throughout, on httpx. Every function raises httpx.HTTPStatusError on a
non-2xx response; callers decide what a failure means.
"""
import mimetypes
import os
import re

import httpx

from app.config import CANVAS_BASE_URL, CANVAS_TOKEN

TIMEOUT = httpx.Timeout(120.0, connect=15.0)

_NEXT_LINK = re.compile(r'<([^>]+)>;\s*rel="next"')


def _headers():
    return {"Authorization": f"Bearer {CANVAS_TOKEN}"}


def _url(path):
    return f"{CANVAS_BASE_URL}/api/v1/{path.lstrip('/')}"


async def _get_paginated(path, params=None):
    """Follow Canvas Link-header pagination and return every item."""
    items = []
    url = _url(path)
    params = dict(params or {})
    params.setdefault("per_page", 100)

    async with httpx.AsyncClient(timeout=TIMEOUT, headers=_headers()) as client:
        while url:
            response = await client.get(url, params=params)
            response.raise_for_status()
            page = response.json()
            if not isinstance(page, list):
                return page
            items.extend(page)
            match = _NEXT_LINK.search(response.headers.get("link", ""))
            url = match.group(1) if match else None
            params = None  # the next link already carries the query string
    return items


def display_name(course):
    """Strip the term and section code schools bolt onto course names.

    "2026FA-BMK-2510-LD01 Introduction to Marketing" -> "Introduction to Marketing"

    Only strips when the leading token really looks like a course code, so a
    plain name such as "Introduction to Marketing" is left alone.
    """
    name = (course.get("name") or "").strip()
    head, separator, tail = name.partition(" ")
    if separator and tail.strip() and head.count("-") >= 2 and any(c.isdigit() for c in head):
        return tail.strip()
    return name


async def list_active_courses():
    """Courses with an active enrollment for the token owner."""
    courses = await _get_paginated(
        "courses",
        {"enrollment_state": "active", "state[]": "available"},
    )
    # Canvas returns restricted courses as stubs with no name. Drop those.
    courses = [c for c in courses if c.get("name")]
    for course in courses:
        course["display_name"] = display_name(course)
    return courses


async def list_assignments(course_id):
    """Assignments for one course, earliest due date first."""
    assignments = await _get_paginated(
        f"courses/{course_id}/assignments",
        {"order_by": "due_at", "include[]": "submission"},
    )
    return [a for a in assignments if not a.get("locked_for_user")]


async def get_my_submission(course_id, assignment_id):
    """The token owner's submission, or None if Canvas has no record."""
    async with httpx.AsyncClient(timeout=TIMEOUT, headers=_headers()) as client:
        response = await client.get(
            _url(f"courses/{course_id}/assignments/{assignment_id}/submissions/self")
        )
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return response.json()


async def submit_file(course_id, assignment_id, file_path, filename=None):
    """Upload a file and attach it as an online_upload submission.

    Canvas needs three round trips:
      1. tell Canvas about the file, get a signed upload target back
      2. POST the bytes to that target (no Canvas token on this request)
      3. attach the resulting file id to a new submission

    Returns the created submission object.
    """
    name = filename or os.path.basename(file_path)
    size = os.path.getsize(file_path)
    content_type = mimetypes.guess_type(name)[0] or "application/octet-stream"

    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        # Step 1 - request an upload slot.
        start = await client.post(
            _url(f"courses/{course_id}/assignments/{assignment_id}/submissions/self/files"),
            headers=_headers(),
            data={"name": name, "size": size,
                  "content_type": content_type, "on_duplicate": "rename"},
        )
        start.raise_for_status()
        slot = start.json()
        upload_url = slot["upload_url"]
        upload_params = slot.get("upload_params", {})

        # Step 2 - send the bytes. The signed URL carries its own auth, and
        # forwarding the Canvas token to it is rejected by some storage backends.
        with open(file_path, "rb") as handle:
            upload = await client.post(
                upload_url,
                data={k: str(v) for k, v in upload_params.items()},
                files={"file": (name, handle, content_type)},
                follow_redirects=False,
            )
        if upload.status_code >= 400:
            upload.raise_for_status()

        # Canvas either returns the file object directly or 3xx-redirects to a
        # confirmation endpoint that must be called with the Canvas token.
        if upload.status_code in (301, 302, 303, 307, 308):
            confirm = await client.get(
                upload.headers["location"], headers=_headers()
            )
            confirm.raise_for_status()
            uploaded = confirm.json()
        else:
            uploaded = upload.json()

        file_id = uploaded["id"]

        # Step 3 - attach the uploaded file to a submission.
        submit = await client.post(
            _url(f"courses/{course_id}/assignments/{assignment_id}/submissions"),
            headers=_headers(),
            data={
                "submission[submission_type]": "online_upload",
                "submission[file_ids][]": file_id,
            },
        )
        submit.raise_for_status()
        return submit.json()


async def get_course(course_id):
    """One course by id."""
    async with httpx.AsyncClient(timeout=TIMEOUT, headers=_headers()) as client:
        response = await client.get(_url(f"courses/{course_id}"))
        response.raise_for_status()
        return response.json()


async def get_assignment(course_id, assignment_id):
    """One assignment by id, with the caller's submission inlined."""
    async with httpx.AsyncClient(timeout=TIMEOUT, headers=_headers()) as client:
        response = await client.get(
            _url(f"courses/{course_id}/assignments/{assignment_id}"),
            params={"include[]": "submission"},
        )
        response.raise_for_status()
        return response.json()
