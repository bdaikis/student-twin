"""Deadline watch (slice 3c), run every 30 minutes by Hermes's scheduler with no model call; whatever
this prints is sent to Bhanu on Telegram.

    hermes cron create "every 30m" --no-agent --script deadline_watch.py --deliver telegram --name deadline-watch

Each run reads SPeCTRUM first (his rule: check before saying something is unsubmitted), then:
- messages each change straight away: a new deadline, a moved due date, one removed, a new
  announcement in a course's news forum;
- reminds him 48 h, 24 h and 3 h before anything he still has to do: assignments not submitted,
  quizzes not attempted, and tests (which SPeCTRUM can't mark done). If the laptop slept through a
  reminder, he gets one message with the time left now, not a burst of stale ones.
Quiet from midnight to 07:00: those runs send nothing, and the 07:00 run catches up.
State (which reminders went out, the last announcement seen) lives in the deadlines store.
"""
import html
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "mcp_servers"))

import deadlines as dl  # noqa: E402
import timetable  # noqa: E402
from common import moodle  # noqa: E402

REMINDERS_H = (48, 24, 3)
QUIET_UNTIL_HOUR = 7  # no messages between midnight and 07:00; they go out on the 07:00 run


def _meta(db, name, value=None):
    if value is None:
        row = db.execute("SELECT value FROM meta WHERE name=?", (name,)).fetchone()
        return row[0] if row else None
    db.execute("INSERT OR REPLACE INTO meta (name, value) VALUES (?, ?)", (name, value))
    return value


def _left(seconds: int) -> str:
    h = seconds // 3600
    if h >= 24:
        d, h = divmod(h, 24)
        return f"{d} day{'s' if d > 1 else ''}" + (f" {h} h" if h else "")
    return f"{h} h {(seconds % 3600) // 60} min" if h else f"{seconds // 60} min"


def change_messages(changes: list[dict], now) -> list[str]:
    out = []
    ts = int(now.timestamp())
    for c in changes:
        it = c["item"]
        name = f"{it['course']} {it['title']}"
        precision = it.get("precision", "time")
        if c["change"] == "new" and (c["new"] or 0) >= ts:
            out.append(f"New on SPeCTRUM: {name}, due {dl.describe_due(c['new'], precision, now)}.")
        elif c["change"] == "moved" and c["old"] and c["new"]:
            out.append(f"Moved on SPeCTRUM: {name}, now due {dl.describe_due(c['new'], precision, now)} "
                       f"(was {dl.describe_due(c['old'], precision, now)}).")
        elif c["change"] == "removed":
            out.append(f"Removed from SPeCTRUM: {name} (was due {dl.describe_due(c['old'], precision, now)}).")
    return out


def announcement_messages(db) -> list[str]:
    """New posts in each course's announcements (news) forum since the last run."""
    m = moodle()
    courses = dl._courses()
    params = {f"courseids[{i}]": cid for i, cid in enumerate(courses)}
    out = []
    first_run = _meta(db, "announcements:seen") is None
    seen = set((_meta(db, "announcements:seen") or "").split(",")) - {""}
    for forum in m.call("mod_forum_get_forums_by_courses", **params):
        if forum.get("type") != "news":
            continue
        for d in m.call("mod_forum_get_forum_discussions", forumid=forum["id"], sortorder=1, perpage=10).get("discussions", []):
            did = str(d["discussion"])
            if did in seen:
                continue
            seen.add(did)
            if not first_run:
                text = " ".join(html.unescape(re.sub(r"<[^>]+>", " ", d.get("message", ""))).split())
                out.append(f"New announcement in {courses.get(forum['course'], '?')}: {d.get('subject', '').strip()}"
                           + (f" — {text[:200]}{'…' if len(text) > 200 else ''}" if text else ""))
    _meta(db, "announcements:seen", ",".join(sorted(seen)))
    return out


def reminder_messages(db, now) -> list[str]:
    ts = int(now.timestamp())
    out = []
    for r in dl.upcoming(3):  # 48 h is inside three days
        if r["due"] < ts or r["status"] in dl.DONE or r["kind"] == "forum":
            continue
        left = r["due"] - ts
        due_ones = [h for h in REMINDERS_H if left <= h * 3600]
        if not due_ones:
            continue
        current = min(due_ones)  # the window we are in now: 3 h, else 24 h, else 48 h
        if _meta(db, f"reminder:{r['key']}:{current}"):
            continue
        for h in due_ones:  # earlier windows missed while asleep are covered by this one message
            _meta(db, f"reminder:{r['key']}:{h}", str(ts))
        what = {"assignment": "not submitted yet", "quiz": "not attempted yet"}.get(r["kind"], "")
        if r["kind"] == "quiz" and r.get("opens") and r["opens"] > ts:
            what = f"opens {dl.describe_due(r['opens'], 'time', now)}"
        verb = {"quiz": "closes", "test": "is"}.get(r["kind"], "is due")
        out.append(f"Reminder: {r['course']} {r['title']} {verb} {dl.describe_due(r['due'], r['precision'], now)}, "
                   f"in {_left(left)}" + (f" ({what})." if what else "."))
    return out


def main() -> None:
    now = timetable.now()
    if now.hour < QUIET_UNTIL_HOUR:
        return  # quiet hours: nothing is marked sent, so the 07:00 run sends what's due then
    messages = []
    try:
        messages += change_messages(dl.refresh(force=True), now)
    except Exception:
        return  # SPeCTRUM unreachable: no reminders from stale statuses; the next run tries again
    db = dl._connect()
    try:
        try:
            messages += announcement_messages(db)
        except Exception:
            pass
        messages += reminder_messages(db, now)
        db.commit()
    finally:
        db.close()
    if messages:
        print("\n\n".join(messages))


if __name__ == "__main__":
    main()
