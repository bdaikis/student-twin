"""Bhanu's class timetable, read from the private iCal address of his calendar.

The address is kept in Windows Credential Manager (scripts/save_calendar_link.py) and read through
moodle._load_token, which reads any generic credential. Each class is one event:

    SUMMARY      SIF1005 - ELECTRONICS I (1)        course code, name, group
    LOCATION     DKFIZIK / MFT1FIZIK / ONLINE
    DESCRIPTION  Activity: LECTURE\\nLecturer: ...   LECTURE or LAB
    DTSTART/END  UTC times

The calendar is cached in the twin's data folder for an hour, so a slow or missing connection
still gives the last copy. Standard library only.
"""
import os
import re
import time
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

DATA = Path(os.environ.get("STUDENT_TWIN_HOME") or Path(os.environ["LOCALAPPDATA"]) / "student-twin")
CACHE = DATA / "cache" / "timetable.ics"
CREDENTIAL = "student-twin/calendar-ical"
REFRESH = 3600
LOCAL = timezone(timedelta(hours=8))  # Malaysia has no daylight saving


@dataclass
class ClassBlock:
    code: str
    name: str
    group: str
    start: datetime  # local time
    end: datetime
    location: str
    activity: str  # LECTURE, LAB, ...
    lecturer: str

    @property
    def online(self) -> bool:
        return self.location.upper() == "ONLINE"

    def line(self) -> str:
        when = f"{self.start:%a %d %b} {self.start:%H:%M}–{self.end:%H:%M}"
        where = "online" if self.online else self.location
        return f"{when} · {self.code} {self.name.title()} · {self.activity.lower()} · {where}"


def _fetch() -> str:
    try:
        if time.time() - CACHE.stat().st_mtime < REFRESH:
            return CACHE.read_text(encoding="utf-8")
    except OSError:
        pass
    from moodle import _load_token
    try:
        req = urllib.request.Request(_load_token(CREDENTIAL), headers={"User-Agent": "student-twin"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read().decode("utf-8", "replace")
        if "BEGIN:VCALENDAR" in body:
            CACHE.parent.mkdir(parents=True, exist_ok=True)
            CACHE.write_text(body, encoding="utf-8")
            return body
    except Exception:
        pass
    if CACHE.exists():  # offline: the last copy is better than nothing
        return CACHE.read_text(encoding="utf-8")
    raise RuntimeError("The timetable couldn't be read: no saved calendar address, or the calendar is unreachable.")


def _unescape(text: str) -> str:
    return text.replace("\\n", "\n").replace("\\,", ",").replace("\\;", ";").replace("\\\\", "\\")


def _when(value: str) -> datetime:
    if value.endswith("Z"):
        return datetime.strptime(value, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc).astimezone(LOCAL)
    return datetime.strptime(value[:15], "%Y%m%dT%H%M%S").replace(tzinfo=LOCAL)


def classes() -> list[ClassBlock]:
    body = re.sub(r"\r?\n[ \t]", "", _fetch())  # unfold continued lines
    out = []
    for ev in re.findall(r"BEGIN:VEVENT\r?\n(.*?)END:VEVENT", body, re.S):
        f = {}
        for line in ev.splitlines():
            key, _, value = line.partition(":")
            f.setdefault(key.split(";")[0], value.strip())
        if f.get("STATUS", "CONFIRMED") == "CANCELLED" or "DTSTART" not in f:
            continue
        m = re.match(r"([A-Z]{3}\d{4})\s*-\s*(.*?)\s*\(([^)]*)\)\s*$", _unescape(f.get("SUMMARY", "")))
        if not m:
            continue
        desc = _unescape(f.get("DESCRIPTION", ""))
        activity = (re.search(r"Activity:\s*([^\n]+)", desc) or [None, "?"])[1].strip()
        lecturer = (re.search(r"Lecturer:\s*([^\n]+)", desc) or [None, ""])[1].strip()
        start = _when(f["DTSTART"])
        end = _when(f["DTEND"]) if "DTEND" in f else start + timedelta(hours=1)
        out.append(ClassBlock(m[1], m[2], m[3], start, end, _unescape(f.get("LOCATION", "")).strip(),
                              activity.upper(), "" if lecturer.lower() == "unknown lecturer" else lecturer))
    return sorted(out, key=lambda c: c.start)


def now() -> datetime:
    return datetime.now(LOCAL)


def semester_status(day: datetime) -> str:
    """Where `day` falls in the semester, from "semester" in the per-user config: "Semester I week 3
    (lectures)", "mid-semester break", ... ("" when the config has no calendar or the day is outside it)."""
    from common import config
    sem = config().get("semester") or {}
    d = day.date()
    for period in sem.get("periods", []):
        start = datetime.strptime(period["from"], "%Y-%m-%d").date()
        end = datetime.strptime(period["to"], "%Y-%m-%d").date()
        if start <= d <= end:
            name = sem.get("name", "the semester")
            if "first_week" in period:
                week = period["first_week"] + (d - start).days // 7
                return f"{name}, week {week} ({period['label']})"
            return f"{name}: {period['label']}"
    return ""


def on_day(day: datetime) -> list[ClassBlock]:
    return [c for c in classes() if c.start.date() == day.date()]


def in_progress(at: datetime | None = None, early_minutes: int = 10) -> list[ClassBlock]:
    """Classes running at `at`, counting from `early_minutes` before the start."""
    at = at or now()
    return [c for c in classes() if c.start - timedelta(minutes=early_minutes) <= at < c.end]


_WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


def parse_day(text: str) -> datetime | str:
    """A day from "today", "tomorrow", a weekday ("friday", the next one), "next monday", or a date
    ("16 oct", "2026-10-16"); an error sentence otherwise."""
    t = " ".join(text.lower().split()) or "today"
    today = now().replace(hour=0, minute=0, second=0, microsecond=0)
    if t == "today":
        return today
    if t == "tomorrow":
        return today + timedelta(days=1)
    if t == "yesterday":
        return today - timedelta(days=1)
    nxt = t.startswith("next ")
    word = t.removeprefix("next ").strip()
    for i, name in enumerate(_WEEKDAYS):
        if word in (name, name[:3]):
            ahead = (i - today.weekday()) % 7  # "friday": the coming one, today included
            if nxt and ahead == 0:  # "next friday" said on a Friday: a week on
                ahead = 7
            return today + timedelta(days=ahead)
    for fmt in ("%Y-%m-%d", "%d %b %Y", "%d %B %Y", "%d %b", "%d %B", "%b %d", "%B %d"):
        try:
            d = datetime.strptime(word.title() if "%b" in fmt or "%B" in fmt else word, fmt)
        except ValueError:
            continue
        if "%Y" not in fmt:
            d = d.replace(year=today.year)
            if d.date() < (today - timedelta(days=60)).date():
                d = d.replace(year=today.year + 1)
        return d.replace(tzinfo=LOCAL)
    return f"Couldn't read {text!r} as a day; try today, tomorrow, a weekday or a date like 16 Oct."
