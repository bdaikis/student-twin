"""Shared by the twin's MCP servers: per-user config, course matching and the portal client.

Per-user settings live outside the repo in %LOCALAPPDATA%\\student-twin\\config.json (or under
STUDENT_TWIN_HOME): the portal URL, the courses' portal pages, browser profiles, music, home city
and protected folders.
"""
import json
import os
import re
from pathlib import Path
from urllib.parse import parse_qs, urlparse

DATA = Path(os.environ.get("STUDENT_TWIN_HOME") or Path(os.environ["LOCALAPPDATA"]) / "student-twin")
CONFIG_PATH = DATA / "config.json"


def config() -> dict:
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def match_course(course: str) -> tuple[str, dict] | str:
    """(code, info) for a course code ("SIF1014") or part of its name ("thermal physics"), or an
    error sentence listing the courses."""
    courses = config().get("courses", {})
    q = norm(course).replace(" ", "")
    for code, info in courses.items():
        if q and (q == code.lower() or q in code.lower()):
            return code, info
    hits = [code for code, info in courses.items() if norm(course) and norm(course) in norm(info["name"])]
    if len(hits) == 1:
        return hits[0], courses[hits[0]]
    known = ", ".join(f"{c} ({i['name']})" for c, i in courses.items())
    return f"No single course matches {course!r}. Courses: {known}."


def course_id(info: dict) -> int | None:
    value = parse_qs(urlparse(info.get("url", "")).query).get("id", [None])[0]
    return int(value) if value else None


_client = None


def moodle():
    from moodle import Moodle  # imported lazily: only the portal tools need it
    global _client
    portal = config().get("portal", {})
    if _client is None or _client.site != portal.get("url", "").rstrip("/"):
        _client = Moodle(portal["url"], portal.get("token_credential", "student-twin/spectrum-token"))
    return _client


def course_contents(course: str):
    """(code, info, sections) or an error sentence."""
    match = match_course(course)
    if isinstance(match, str):
        return match
    code, info = match
    cid = course_id(info)
    if not cid or not config().get("portal", {}).get("url"):
        return f"{code} has no portal course id or the portal isn't configured."
    from moodle import MoodleError
    try:
        return code, info, moodle().course_contents(cid)
    except MoodleError as exc:
        return f"Couldn't read {code} from the portal: {exc}."
