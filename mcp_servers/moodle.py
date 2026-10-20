"""Read-only client for a Moodle site's web-service API (UM's SPeCTRUM is one).

The token comes from Windows Credential Manager (saved by the setup step) and never leaves this
module: it is sent only in the POST body to the site's REST endpoint, never put in a URL that is
logged or opened in a browser. The token also carries write rights, so only functions on READ_ONLY
can be called. Standard library only.
"""
import ctypes
import ctypes.wintypes as wt
import html
import json
import re
import time
import urllib.parse
import urllib.request

READ_ONLY = {
    "core_webservice_get_site_info",
    "core_enrol_get_users_courses",
    "core_course_get_contents",
    "tool_mobile_get_content",  # guarded below: only the attendance plugin's read-only view
    # Deadlines (slice 3): the dashboard timeline, assignments and quizzes with his own status.
    "core_calendar_get_action_events_by_timesort",
    "mod_assign_get_assignments",
    "mod_assign_get_submission_status",
    "mod_quiz_get_quizzes_by_courses",
    "mod_quiz_get_user_attempts",
}


def _guard(function: str, params: dict, marking: bool) -> None:
    """Extra limits on general-purpose calls. tool_mobile_get_content runs any plugin's mobile
    method; the attendance plugin's view also *records* attendance when given a session id (and a
    status). Reads may pass only cmid, courseid and userid. Marking (Bhanu's own check-in, on his
    request while his class runs) may add sessid, status and studentpass through
    Moodle.attendance_view(..., sessid=...), and may fetch the check-in form (mobile_user_form,
    which only renders the statuses he may choose) through Moodle.attendance_form."""
    if function != "tool_mobile_get_content":
        return
    names = {v for k, v in params.items() if k.startswith("args[") and k.endswith("[name]")}
    method = params.get("method")
    if method == "mobile_view_activity":
        allowed = {"cmid", "courseid", "userid"} | ({"sessid", "status", "studentpass"} if marking else set())
    elif method == "mobile_user_form" and marking:
        allowed = {"cmid", "courseid", "sessid", "appversioncode"}
    else:
        allowed = None
    if params.get("component") != "mod_attendance" or allowed is None or not names <= allowed:
        raise MoodleError("only the attendance plugin's view (and Bhanu's own check-in) is allowed through tool_mobile_get_content")


# English labels for the attendance plugin's summary rows (mod_attendance language strings).
_ATTENDANCE_LABELS = {
    "sessionscompleted": "Taken sessions",
    "pointssessionscompleted": "Points over taken sessions",
    "percentagesessionscompleted": "Percentage over taken sessions",
    "sessionstotal": "Total number of sessions",
    "percentageallsessions": "Percentage over all sessions",
    "maxpossiblepoints": "Maximum possible points",
    "maxpossiblepercentage": "Maximum possible percentage",
    # "pointsallsessions" is left out: UM's plugin version shows a percentage in that row, which
    # contradicts its label.
}


class MoodleError(RuntimeError):
    pass


def _text(fragment: str) -> str:
    return " ".join(html.unescape(re.sub(r"<[^>]+>|\{\{.*?\}\}", " ", fragment)).split())


def _sessions(page: str) -> list[dict]:
    """Session items in the attendance view: an <h2> with the date and time, then <h3>s with the
    description and, once marked, the status (seen 19 Oct 2026: "Mon 19 Oct 2026 8AM - 9AM", "",
    "Present")."""
    out = []
    for chunk in re.split(r"<ion-item\b", page)[1:]:
        when = re.search(r"<h2[^>]*>(.*?)</h2>", chunk, re.S)
        if not when:
            continue
        h3 = [_text(t) for t in re.findall(r"<h3[^>]*>(.*?)</h3>", chunk, re.S)]
        out.append({
            "when": _text(when[1]),
            "description": h3[0] if h3 else "",
            "status": h3[-1] if len(h3) >= 2 else "",
            "open": bool(re.search(r"sessid", chunk)),
        })
    return out


class _CREDENTIAL(ctypes.Structure):
    _fields_ = [("Flags", wt.DWORD), ("Type", wt.DWORD), ("TargetName", wt.LPWSTR), ("Comment", wt.LPWSTR),
                ("LastWritten", wt.FILETIME), ("CredentialBlobSize", wt.DWORD),
                ("CredentialBlob", ctypes.POINTER(ctypes.c_char)), ("Persist", wt.DWORD),
                ("AttributeCount", wt.DWORD), ("Attributes", ctypes.c_void_p),
                ("TargetAlias", wt.LPWSTR), ("UserName", wt.LPWSTR)]


def _load_token(target: str) -> str:
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    pcred = ctypes.POINTER(_CREDENTIAL)()
    if not advapi32.CredReadW(target, 1, 0, ctypes.byref(pcred)):  # CRED_TYPE_GENERIC
        raise MoodleError(f"no portal token saved as {target!r} in Windows Credential Manager")
    try:
        cred = pcred.contents
        return ctypes.string_at(cred.CredentialBlob, cred.CredentialBlobSize).decode("utf-8")
    finally:
        advapi32.CredFree(pcred)


class Moodle:
    def __init__(self, site: str, credential: str, cache_seconds: float = 300):
        self.site = site.rstrip("/")
        self.credential = credential
        self.cache_seconds = cache_seconds
        self._cache: dict[tuple, tuple[float, object]] = {}

    def call(self, function: str, _marking: bool = False, **params):
        if function not in READ_ONLY:
            raise MoodleError(f"{function} is not on the read-only allowlist")
        _guard(function, params, _marking)
        key = (function, tuple(sorted(params.items())))
        hit = None if _marking else self._cache.get(key)
        if hit and time.time() - hit[0] < self.cache_seconds:
            return hit[1]
        fields = {"wstoken": _load_token(self.credential), "wsfunction": function, **params}
        req = urllib.request.Request(f"{self.site}/webservice/rest/server.php?moodlewsrestformat=json",
                                     data=urllib.parse.urlencode(fields).encode(),
                                     headers={"User-Agent": "student-twin"})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.load(resp)
        except OSError as exc:
            raise MoodleError(f"couldn't reach {self.site}: {exc}") from None
        if isinstance(data, dict) and "exception" in data:
            raise MoodleError(f"{function} failed: {data.get('errorcode')} ({data.get('message')})")
        if not _marking:
            self._cache[key] = (time.time(), data)
        return data

    def course_contents(self, course_id: int) -> list[dict]:
        """Sections of a course, each with its visible modules (name, type, view URL, file names)."""
        sections = []
        for sec in self.call("core_course_get_contents", courseid=course_id):
            modules = [{"name": m.get("name", "").strip(),
                        "type": m.get("modname", ""),
                        "url": m.get("url"),
                        "files": [c.get("filename", "") for c in m.get("contents", []) if c.get("type") == "file"]}
                       for m in sec.get("modules", [])
                       if m.get("uservisible", True) and m.get("modname") != "label"]
            if modules:
                sections.append({"name": sec.get("name", "").strip() or f"Section {sec.get('section')}",
                                 "modules": modules})
        return sections

    def user_id(self) -> int:
        return int(self.call("core_webservice_get_site_info")["userid"])

    def attendance(self, course_id: int, fresh: bool = False) -> list[dict]:
        """Bhanu's attendance for each Attendance activity in a course, read the way the Moodle
        app does: [{"name", "cmid", "rows": [(label, value), ...], "sessions": [...]}]. "rows" are
        the summary totals. "sessions" are the sessions the app's view lists, which in practice are
        the current day's: {"when": "Mon 19 Oct 2026 8AM - 9AM", "description", "status": "Present"
        or "" when not marked, "open": whether the item carries a self-marking session id}. Earlier
        sessions appear only in the totals."""
        uid = self.user_id()
        out = []
        for sec in self.call("core_course_get_contents", courseid=course_id):
            for mod in sec.get("modules", []):
                if mod.get("modname") != "attendance" or not mod.get("uservisible", True):
                    continue
                page = self.attendance_view(course_id, mod["id"], fresh=fresh)["html"]
                rows = []
                for key, value in re.findall(
                        r"plugin\.mod_attendance\.(\w+)'\s*\|\s*translate\s*}}\s*</ion-col>\s*<ion-col[^>]*>(.*?)</ion-col>",
                        page, re.S):
                    if key in _ATTENDANCE_LABELS:
                        rows.append((_ATTENDANCE_LABELS[key], " ".join(html.unescape(re.sub(r"<[^>]+>", " ", value)).split())))
                out.append({"name": mod.get("name", "Attendance").strip(), "cmid": mod["id"], "rows": rows,
                            "sessions": _sessions(page)})
        return out

    def attendance_form(self, course_id: int, cmid: int, sessid: int) -> dict:
        """The check-in form for one session, as the app's "Submit attendance" button opens it
        (mobile_user_form): the statuses Bhanu may choose, or a message (password needed, wrong
        network, check-in closed). It records nothing; only mark_attendance calls it."""
        args = [("cmid", cmid), ("courseid", course_id), ("sessid", sessid),
                ("appversioncode", 44000)]  # the plugin picks its current form template from 44000 up
        params = {}
        for i, (name, value) in enumerate(args):
            params[f"args[{i}][name]"] = name
            params[f"args[{i}][value]"] = value
        data = self.call("tool_mobile_get_content", _marking=True, component="mod_attendance",
                         method="mobile_user_form", **params)
        return {"html": "".join(t.get("html", "") for t in data.get("templates", [])),
                "javascript": data.get("javascript") or "",
                "otherdata": json.dumps(data.get("otherdata") or [])}

    def attendance_view(self, course_id: int, cmid: int, sessid: int | None = None,
                        status: int | None = None, fresh: bool = False) -> dict:
        """The attendance plugin's Moodle-app page for an activity: {"html", "javascript",
        "otherdata"}. With `sessid` this is Bhanu's own check-in for that session (with `status`,
        the status he records); it is never cached, and only mark_attendance calls it."""
        args = [("cmid", cmid), ("courseid", course_id), ("userid", self.user_id())]
        if sessid is not None:
            args.append(("sessid", sessid))
            if status is not None:
                args.append(("status", status))
        params = {}
        for i, (name, value) in enumerate(args):
            params[f"args[{i}][name]"] = name
            params[f"args[{i}][value]"] = value
        marking = sessid is not None
        if fresh and not marking:
            self._cache.pop(("tool_mobile_get_content", tuple(sorted(
                {"component": "mod_attendance", "method": "mobile_view_activity", **params}.items()))), None)
        data = self.call("tool_mobile_get_content", _marking=marking, component="mod_attendance",
                         method="mobile_view_activity", **params)
        return {"html": "".join(t.get("html", "") for t in data.get("templates", [])),
                "javascript": data.get("javascript") or "",
                "otherdata": json.dumps(data.get("otherdata") or [])}
