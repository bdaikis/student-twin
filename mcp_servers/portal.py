"""Student Twin portal tools: timetable, course material, attendance and Bhanu's own check-in.

An MCP server Hermes starts over stdio. Unlike desktop.py it touches nothing on the computer, so it
is enabled for Telegram as well as the widget and CLI: Bhanu can ask for his timetable or check in
to a class from his phone.

Reads go through moodle.py's read-only allowlist. The one write is mark_attendance: Bhanu's own
SPeCTRUM check-in for the class he is in, made only when he asks and only while that class runs.
UM counts a student present only with both the SPeCTRUM check-in and a signature on the paper
sheet; a check-in without the signature is recorded absent and flagged for proxy review. So the
twin never checks in on its own initiative, on a schedule, or for a class that isn't running.
"""
import json
import re
import time
from datetime import timedelta

from mcp.server.mcpserver import MCPServer

import timetable
from common import (DATA, PRESENT, course_contents, course_id, match_course, moodle, remember_sessions,
                    remembered_session, session_for, totals_verdict)

server = MCPServer(
    "portal",
    instructions=(
        "Bhanu's university timetable, course material and attendance on SPeCTRUM (UM's Moodle). "
        "When he says he is in class (\"here\", \"I'm here\", \"check me in\", \"mark me\"), usually in "
        "reply to a class reminder, call mark_attendance. For whether he has been marked today, call "
        "attendance_today."
    ),
)

SNAPSHOTS = DATA / "attendance_snapshots"
LOG = DATA / "attendance_log.jsonl"


@server.tool()
def timetable_day(day: str = "today") -> str:
    """Bhanu's classes on a day, from his calendar: time, course, lecture or lab, room (or online).
    `day` is "today", "tomorrow", a weekday ("friday": the coming one) or a date ("16 Oct")."""
    when = timetable.parse_day(day)
    if isinstance(when, str):
        return when
    try:
        blocks = timetable.on_day(when)
    except RuntimeError as exc:
        return str(exc)
    if not blocks:
        return f"No classes on {when:%A %d %b}."
    return f"{when:%A %d %b}:\n" + "\n".join(f"- {b.line()}" for b in blocks)


@server.tool()
def course_materials(course: str) -> str:
    """List what is posted in one of Bhanu's courses on the university portal, section by section:
    lecture notes, slides, tutorials, assignments, quizzes, forums. `course` is a code ("SIF1014")
    or part of a name ("vibrations")."""
    got = course_contents(course)
    if isinstance(got, str):
        return got
    code, info, sections = got
    lines = [f"{code} {info['name']}:"]
    for sec in sections:
        lines.append(f"## {sec['name']}")
        for m in sec["modules"]:
            files = f" (files: {', '.join(m['files'][:4])}{'…' if len(m['files']) > 4 else ''})" if m["files"] else ""
            lines.append(f"- [{m['type']}] {m['name']}{files}")
    return "\n".join(lines)


def _activities(code: str, cid: int, fresh: bool = False) -> list[dict]:
    """The course's Attendance activities that have sessions (others belong to other class groups)."""
    return [a for a in moodle().attendance(cid, fresh=fresh) if dict(a["rows"]).get("Total number of sessions") != "0"]


def _summary(rows: dict) -> str:
    if "Taken sessions" not in rows:
        return "; ".join(f"{k}: {v}" for k, v in rows.items()) or "no summary shown"
    return (f"{rows['Taken sessions']} of {rows.get('Total number of sessions', '?')} sessions taken, "
            f"{rows.get('Points over taken sessions', '?')} points ({rows.get('Percentage over taken sessions', '?')} so far)")


@server.tool()
def attendance(course: str = "") -> str:
    """Bhanu's attendance totals on the university portal, per course: sessions taken, points and
    percentage, plus today's session and its status where SPeCTRUM lists one. `course` is a code or
    part of a name; empty means every course. For "was I marked today?" use attendance_today."""
    from moodle import MoodleError
    from common import config
    codes = [course] if course.strip() else list(config().get("courses", {}))
    lines, errors = [], []
    for c in codes:
        match = match_course(c)
        if isinstance(match, str):
            return match
        code, info = match
        cid = course_id(info)
        if not cid:
            continue
        try:
            acts = _activities(code, cid, fresh=True)
            remember_sessions(code, acts)
            for act in acts:
                today = "".join(f"; {s['when']}: {s['status'] or 'not marked yet'}" for s in act.get("sessions", []))
                lines.append(f"- {code} {info['name']} ({act['name']}): {_summary(dict(act['rows']))}{today}")
        except MoodleError as exc:
            errors.append(f"{code}: {exc}")
    if not lines and not errors:
        return "No Attendance activity with sessions" + (f" in {codes[0]}." if course.strip() else " in any course.")
    return "\n".join(lines + ([f"Couldn't read: {'; '.join(errors)}"] if errors else []))


@server.tool()
def attendance_today() -> str:
    """Whether Bhanu has been marked for each of today's classes, read live from SPeCTRUM: for each
    class in his timetable today, the session SPeCTRUM lists and its status (Present, Absent, Late,
    Excused) or "not marked yet". Use it for "was I marked?", "did my check-in go through?" or
    "check my attendance for today". Read-only: it never checks him in (that is mark_attendance)."""
    from moodle import MoodleError
    try:
        now = timetable.now()
        blocks = timetable.on_day(now)
    except RuntimeError as exc:
        return str(exc)
    if not blocks:
        return f"No classes today ({now:%A %d %b})."
    fetched: dict[str, list[dict] | str] = {}
    lines = []
    for b in blocks:
        head = f"{b.start:%H:%M}–{b.end:%H:%M} {b.code} {b.name.title()} {b.activity.lower()}"
        if b.activity == "LAB":
            lines.append(f"- {head}: labs are marked in person, not on SPeCTRUM.")
            continue
        match = match_course(b.code)
        if isinstance(match, str):
            lines.append(f"- {head}: not in the twin's course list.")
            continue
        code, info = match
        if code not in fetched:
            try:
                fetched[code] = _activities(code, course_id(info), fresh=True)
            except MoodleError as exc:
                fetched[code] = f"couldn't reach SPeCTRUM ({exc})"
        acts = fetched[code]
        if isinstance(acts, str):
            lines.append(f"- {head}: {acts}.")
            continue
        if not acts:
            lines.append(f"- {head}: no attendance on SPeCTRUM for this course.")
            continue
        remember_sessions(code, acts)
        s = session_for(b, acts)
        if s and s["status"]:
            lines.append(f"- {head}: {_status(s['status'])} on SPeCTRUM.")
            continue
        if s:
            state = "check-in is open" if s["open"] else "not marked yet"
            lines.append(f"- {head}: {state} on SPeCTRUM" + (" (class not started yet)." if now < b.start else "."))
            continue
        if now < b.start:
            lines.append(f"- {head}: hasn't started; SPeCTRUM lists no session for it yet.")
            continue
        # SPeCTRUM lists a session only around its own time; after that, use what was seen then.
        seen = remembered_session(code, b)
        if seen:
            lines.append(f"- {head}: {_status(seen['status'])} (as SPeCTRUM showed at {seen['seen'][-5:]}; "
                         f"it no longer lists the session).")
            continue
        lines.append(f"- {head}: {totals_verdict(b, acts)[1]}")
    return f"{now:%A %d %b}, as of {now:%H:%M}:\n" + "\n".join(lines)


def _status(status: str) -> str:
    return f"✓ {status}" if PRESENT.search(status) else status


def _snapshot(code: str, step: str, page: dict) -> str:
    SNAPSHOTS.mkdir(parents=True, exist_ok=True)
    path = SNAPSHOTS / f"{time.strftime('%Y%m%d-%H%M%S')}-{code}-{step}.json"
    path.write_text(json.dumps(page, ensure_ascii=False, indent=1), encoding="utf-8")
    return str(path)


def _session_ids(page: dict) -> list[int]:
    text = page["html"] + page["javascript"] + page["otherdata"]
    return sorted({int(x) for x in re.findall(r"sessid['\"]?\s*[:=]\s*['\"]?(\d+)", text)})


def _statuses(html: str) -> list[tuple[int, str]]:
    """(status id, label) pairs offered on the check-in form: <ion-radio value="ID">Label</ion-radio>."""
    return [(int(v), " ".join(re.sub(r"<[^>]+>", " ", label).split())[:60])
            for v, label in re.findall(r"<ion-radio\b[^>]*\bvalue=\"(\d+)\"[^>]*>(.*?)</ion-radio>", html, re.S)]


def _form_messages(html: str) -> list[str]:
    """The plugin's message keys shown on a page ('subnetwrong', 'invalidstatus', ...)."""
    return re.findall(r"<span class=\"messages\">.*?plugin\.mod_attendance\.(\w+)", html, re.S)


_MESSAGES = {
    "subnetwrong": "SPeCTRUM only accepts this check-in from the university network",
    "incorrectpasswordshort": "the session needs a password from the lecturer",
    "attendance_no_status": "no status is available to choose right now",
    "invalidstatus": "SPeCTRUM refused the status (the check-in window may have closed)",
}


def _log(entry: dict) -> None:
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"at": time.strftime("%Y-%m-%d %H:%M:%S"), **entry}) + "\n")


@server.tool()
def mark_attendance(course: str = "") -> str:
    """Check Bhanu in on SPeCTRUM for the class he is in right now. Call it only when he says he is
    in class ("here", "I'm here", "check me in"), usually in reply to a class reminder; never on your
    own initiative. `course` may be empty: the class running now in his timetable is used. Works
    only while that class runs (from 10 minutes before the start until the end). Labs are marked in
    person. Don't remind him to sign the paper sheet: he asked not to be."""
    from moodle import MoodleError
    try:
        running = timetable.in_progress()
    except RuntimeError as exc:
        return str(exc)
    if course.strip():
        want = match_course(course)
        if isinstance(want, str):
            return want
        running = [b for b in running if b.code == want[0]]
    if not running:
        try:
            upcoming = [b for b in timetable.classes() if b.start > timetable.now()][:1]
        except RuntimeError:
            upcoming = []
        nxt = f" Next: {upcoming[0].line()}." if upcoming else ""
        return ("Not checked in: none of your classes is running now"
                + (f" for {course}" if course.strip() else "") + ", and check-in only works during the class." + nxt)
    block = running[0]
    if block.activity == "LAB":
        return f"{block.code} is a lab ({block.location}); lab attendance is taken in person, not on SPeCTRUM."
    match = match_course(block.code)
    if isinstance(match, str):
        return f"{block.code} isn't in the twin's course list: {match}"
    code, info = match
    cid = course_id(info)
    try:
        acts = _activities(code, cid, fresh=True)
        if not acts:
            return f"{code} has no attendance activity on SPeCTRUM."
        snapshots, tried = [], []
        for act in acts:
            before = dict(act["rows"])
            view = moodle().attendance_view(cid, act["cmid"], fresh=True)
            snapshots.append(_snapshot(code, "view", view))
            for sessid in _session_ids(view):
                # The app's "Submit attendance" button opens this form; submitting the chosen status
                # goes back through the view (mod_attendance classes/output/mobile.php).
                form = moodle().attendance_form(cid, act["cmid"], sessid)
                snapshots.append(_snapshot(code, f"form-{sessid}", form))
                options = _statuses(form["html"])
                notes = [_MESSAGES.get(m, m) for m in _form_messages(form["html"])]
                if 'name="studentpass"' in form["html"]:
                    notes.append(_MESSAGES["incorrectpasswordshort"])
                present = [sid for sid, label in options if PRESENT.search(label)]
                result = form
                if present and 'name="studentpass"' not in form["html"]:
                    result = moodle().attendance_view(cid, act["cmid"], sessid=sessid, status=present[0])
                    snapshots.append(_snapshot(code, f"result-{sessid}", result))
                after = next((dict(a["rows"]) for a in moodle().attendance(cid, fresh=True)
                              if a["cmid"] == act["cmid"]), {})
                changed = after.get("Points over taken sessions") != before.get("Points over taken sessions") \
                    or after.get("Taken sessions") != before.get("Taken sessions")
                _log({"course": code, "cmid": act["cmid"], "sessid": sessid, "options": options,
                      "status": present[0] if present else None, "changed": changed,
                      "before": before, "after": after})
                if changed:
                    return (f"Checked in on SPeCTRUM for {code} {info['name']} ({block.start:%H:%M}–{block.end:%H:%M}): "
                            f"now {_summary(after)}.")
                tried.append(f"session {sessid} (status options: {', '.join(l for _, l in options) or 'none'}"
                             + (f"; {'; '.join(notes)}" if notes else "") + ")")
        if not tried:
            return (f"SPeCTRUM shows no open check-in for {code} right now; the lecturer may not have opened it "
                    f"yet. Try again in a few minutes. (Saved the page for Claude Code: {snapshots[-1]})")
        return (f"Tried to check in for {code} ({'; '.join(tried)}), but SPeCTRUM's totals didn't change, so "
                f"it may not have been recorded. Check SPeCTRUM yourself. (Saved pages: {SNAPSHOTS})")
    except MoodleError as exc:
        return f"Couldn't reach SPeCTRUM for {code}: {exc}. Check in yourself if you can."


if __name__ == "__main__":
    server.run("stdio")
