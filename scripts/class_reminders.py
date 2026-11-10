"""Class reminders, run every 5 minutes by Hermes's scheduler with no model call:

    hermes cron create "every 5m" --no-agent --script class_reminders.py --deliver telegram ...

Whatever this prints is sent to Bhanu on Telegram; printing nothing sends nothing.

- When a class starts: a reminder. For a lecture with SPeCTRUM attendance, SPeCTRUM is read first:
  if it already shows him marked, the reminder says so; otherwise "reply 'here' and I'll check you
  in". For a lab, "taken in person". A course with no SPeCTRUM attendance gets no reminder unless
  it has an announcement about that day. Bhanu's rules (2026-10-19): always check SPeCTRUM before
  telling him he hasn't marked his attendance, and no reminders to sign the paper sheet.
- Every start reminder reads the course's announcements first (Bhanu, 2026-11-10: SIF1017 was
  moved online by announcement and the reminder still said DKFIZIK). Posts about that day are
  quoted under it, and if the newest says the class is online, the reminder says online. Hermes
  records these messages in the Telegram conversation (the job's attach_to_session), so a "here"
  in reply reaches a twin that knows which class it was.
- 15 minutes in: unless SPeCTRUM already shows him present for that session, a second message
  while there is still time to check in (or saying what SPeCTRUM recorded instead, such as Absent).
  Decided by the session's status, not the totals: a lecturer taking the register with him absent
  also changes the totals.

The twin never checks in from here; mark_attendance does, only when Bhanu replies. While a class
runs, the attendance page is also saved (read-only) to the data folder, so the check-in step can be
fitted to how SPeCTRUM shows an open session. State lives in <data>/reminders_state.json.
"""
import json
import sys
import time
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "mcp_servers"))

import timetable  # noqa: E402
from common import (DATA, PRESENT, announcement_posts, class_announcements, course_id,  # noqa: E402
                    match_course, moodle, remember_sessions, remembered_session, session_for, totals_verdict)

STATE = DATA / "reminders_state.json"
SNAPSHOTS = DATA / "attendance_snapshots"
CATCH_UP = timedelta(minutes=30)  # after downtime, don't remind about classes that started long ago
FOLLOW_UP = timedelta(minutes=15)


def load_state() -> dict:
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_state(state: dict) -> None:
    cutoff = (timetable.now() - timedelta(days=2)).isoformat()
    state = {k: v for k, v in state.items() if v.get("start", "") >= cutoff}
    STATE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=1), encoding="utf-8")
    tmp.replace(STATE)


def snapshot(code: str, cid: int, cmids) -> None:
    SNAPSHOTS.mkdir(parents=True, exist_ok=True)
    for cmid in cmids:
        page = moodle().attendance_view(cid, int(cmid), fresh=True)
        path = SNAPSHOTS / f"{time.strftime('%Y%m%d-%H%M%S')}-{code}-reminder-view.json"
        path.write_text(json.dumps(page, ensure_ascii=False, indent=1), encoding="utf-8")


def heading(b, where: str | None = None) -> str:
    where = where or ("online" if b.online else b.location)
    return f"{b.code} {b.name.title()} {b.activity.lower()}, {where}, {b.start:%H:%M}–{b.end:%H:%M}."


def announced(b, cid) -> tuple[str | None, list[str]]:
    """Where the class is and the course's announcements about its day; the timetable's room and a
    note when SPeCTRUM's forums can't be read."""
    try:
        return class_announcements(b, announcement_posts({cid: b.code}) if cid else [])
    except Exception:
        return None, ["(Couldn't read the course's announcements just now.)"]


def message(first: str, notes: list[str]) -> str:
    return "\n".join([first, *notes])


def main() -> None:
    now = timetable.now()
    state = load_state()
    messages = []
    for b in timetable.classes():
        if not (b.start - timedelta(minutes=5) <= now < b.end) or now - b.start > CATCH_UP + FOLLOW_UP:
            continue
        key = f"{b.code}-{b.start.isoformat()}"
        entry = state.setdefault(key, {"start": b.start.isoformat()})
        match = match_course(b.code)
        cid = course_id(match[1]) if not isinstance(match, str) else None
        if b.activity == "LAB":
            if not entry.get("reminded"):
                where, notes = announced(b, cid)
                messages.append(message(f"{heading(b, where)} Lab attendance is taken in person.", notes))
                entry["reminded"] = True
            continue
        if not entry.get("reminded"):
            if now - b.start > CATCH_UP:
                entry["reminded"] = entry["followed_up"] = True  # started too long ago to be useful
                continue
            where, notes = announced(b, cid)
            try:
                acts = [a for a in moodle().attendance(cid, fresh=True)
                        if dict(a["rows"]).get("Total number of sessions") != "0"] if cid else []
                remember_sessions(b.code, acts)
            except Exception as exc:
                messages.append(message(f"{heading(b, where)} I couldn't reach SPeCTRUM ({exc}); check in yourself.",
                                        notes))
                entry["reminded"] = entry["followed_up"] = True
                continue
            entry["before"] = {str(a["cmid"]): dict(a["rows"]) for a in acts}
            entry["reminded"] = True
            # SPeCTRUM first: never ask him to check in for a session it already shows him marked for.
            session = session_for(b, acts)
            status = session["status"] if session else ""
            if not acts:
                entry["followed_up"] = True  # no SPeCTRUM attendance, nothing to check in for
                if notes and notes[0].startswith("Announcement"):
                    messages.append(message(heading(b, where), notes))
            elif PRESENT.search(status):
                messages.append(message(f"{heading(b, where)} SPeCTRUM already shows you {status}.", notes))
                entry["followed_up"] = True
            elif status:
                messages.append(message(f"{heading(b, where)} SPeCTRUM shows you {status} for this class. If you're "
                                        f"there, tell the lecturer.", notes))
                entry["followed_up"] = True
            else:
                messages.append(message(f"{heading(b, where)} Reply \"here\" and I'll check you in on SPeCTRUM.",
                                        notes))
            continue
        if entry.get("followed_up") or now < b.start + FOLLOW_UP:
            continue
        entry["followed_up"] = True
        try:
            acts = [a for a in moodle().attendance(cid, fresh=True)
                    if dict(a["rows"]).get("Total number of sessions") != "0"]
            snapshot(b.code, cid, [a["cmid"] for a in acts])
            remember_sessions(b.code, acts)
        except Exception:
            continue
        session = session_for(b, acts) or remembered_session(b.code, b)
        status = session["status"] if session else ""
        if PRESENT.search(status):
            continue  # already marked present
        if not session and totals_verdict(b, acts)[0]:
            continue  # SPeCTRUM no longer lists the session, but its totals show him present
        if status:
            messages.append(f"{b.code}: SPeCTRUM shows you {status} for {b.start:%H:%M}–{b.end:%H:%M}. "
                            f"If you're there, tell the lecturer.")
        else:
            messages.append(f"{b.code}: SPeCTRUM shows no check-in yet, 15 minutes in. "
                            f"Reply \"here\" and I'll check you in, or do it yourself before {b.end:%H:%M}.")
    save_state(state)
    if messages:
        print("\n\n".join(messages))


if __name__ == "__main__":
    main()
