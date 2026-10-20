"""The twice-daily digest (slice 3b), run by Hermes's scheduler at 07:00 and 21:00 with no model
call; whatever this prints is sent to Bhanu on Telegram.

    hermes cron create "0 7,21 * * *" --no-agent --script digest.py --deliver telegram --name digest

Morning: today's classes. Evening: tomorrow's. Both: anything overdue and not submitted, what's due
in the next 7 days (SPeCTRUM read first), and what changed since the last digest. Sent every day,
so a missing digest means something broke. After the laptop has slept through a slot, a digest is
still sent if it is less than three hours late; each slot is sent once.
"""
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "mcp_servers"))

import deadlines as dl  # noqa: E402
import timetable  # noqa: E402

SLOTS = {"morning": 7, "evening": 21}
LATE_LIMIT = timedelta(hours=3)


def _slot(now: datetime) -> tuple[str, datetime] | None:
    for name, hour in SLOTS.items():
        start = now.replace(hour=hour, minute=0, second=0, microsecond=0)
        if start <= now < start + LATE_LIMIT:
            return name, start
    return None


def _meta(db, name: str, value: str | None = None) -> str | None:
    if value is None:
        row = db.execute("SELECT value FROM meta WHERE name=?", (name,)).fetchone()
        return row[0] if row else None
    db.execute("INSERT OR REPLACE INTO meta (name, value) VALUES (?, ?)", (name, value))
    db.commit()
    return value


def _changes_since(db, since: int, now: datetime) -> list[str]:
    out = []
    rows = db.execute("""SELECT h.change, h.old, h.new, i.course, i.title, i.precision, i.source FROM history h
                         JOIN items i ON i.key = h.key WHERE h.at > ? AND i.source = 'spectrum'
                         ORDER BY h.at""", (since,)).fetchall()
    for change, old, new, course, title, precision, _ in rows:
        if change == "new":
            out.append(f"New on SPeCTRUM: {course} {title}, due {dl.describe_due(int(new), precision, now)}")
        elif change == "moved" and old and new:
            out.append(f"Moved: {course} {title}, from {dl.describe_due(int(old), precision, now)} "
                       f"to {dl.describe_due(int(new), precision, now)}")
        elif change == "removed":
            out.append(f"Removed from SPeCTRUM: {course} {title}")
    return out


def compose(now: datetime, slot: str, since: int, refresh_note: str = "") -> str:
    day = now if slot == "morning" else now + timedelta(days=1)
    label = "Today" if slot == "morning" else "Tomorrow"
    status = timetable.semester_status(day)  # "2026/2027 Semester I, week 3 (lectures)" or "...: mid-semester break"
    week = status.split(", ")[-1].split(" (")[0] if ", week" in status else status.split(": ")[-1]
    lines = [f"{label}, {day:%a %d %b}" + (f", {week}" if week else "") + ":"]
    try:
        classes = timetable.on_day(day)
        lines += [f"- {b.start:%H:%M}–{b.end:%H:%M} {b.code} {b.name.title()} {b.activity.lower()}, "
                  f"{'online' if b.online else b.location}" for b in classes] or ["- No classes."]
    except RuntimeError as exc:
        lines.append(f"- Timetable unavailable: {exc}")
    ts = int(now.timestamp())
    rows = dl.upcoming(7)
    overdue = [r for r in rows if r["due"] < ts and r["kind"] in ("assignment", "quiz") and r["status"] not in dl.DONE]
    soon = [r for r in rows if r["due"] >= ts]
    if overdue:
        lines += ["", "Overdue, not submitted:"] + [f"- {dl.line(r, now)}" for r in overdue]
    lines += ["", "Due in the next 7 days:"] + ([f"- {dl.line(r, now)}" for r in soon] or ["- Nothing."])
    db = dl._connect()
    try:
        changes = _changes_since(db, since, now)
    finally:
        db.close()
    if changes:
        lines += ["", "Changed since the last digest:"] + [f"- {c}" for c in changes]
    return "\n".join(lines) + refresh_note


def main() -> None:
    now = timetable.now()
    found = _slot(now)
    if not found:
        return
    slot, start = found
    db = dl._connect()
    try:
        sent_key = f"digest:{start:%Y-%m-%d}:{slot}"
        if _meta(db, sent_key):
            return
        since = int(_meta(db, "digest:last") or 0) or int(time.time()) - 86400
    finally:
        db.close()
    note = ""
    try:
        dl.refresh(force=True)
    except Exception as exc:
        note = f"\n\n(Couldn't read SPeCTRUM just now: {exc}. Deadlines are as last read.)"
    text = compose(now, slot, since, note)
    db = dl._connect()
    try:
        _meta(db, sent_key, "sent")
        _meta(db, "digest:last", str(int(time.time())))
    finally:
        db.close()
    print(text)


if __name__ == "__main__":
    main()
