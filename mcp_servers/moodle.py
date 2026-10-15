"""Read-only client for a Moodle site's web-service API (UM's SPeCTRUM is one).

The token comes from Windows Credential Manager (saved by the setup step) and never leaves this
module: it is sent only in the POST body to the site's REST endpoint, never put in a URL that is
logged or opened in a browser. The token also carries write rights, so only functions on READ_ONLY
can be called. Standard library only.
"""
import ctypes
import ctypes.wintypes as wt
import json
import time
import urllib.parse
import urllib.request

READ_ONLY = {
    "core_webservice_get_site_info",
    "core_enrol_get_users_courses",
    "core_course_get_contents",
}


class MoodleError(RuntimeError):
    pass


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

    def call(self, function: str, **params):
        if function not in READ_ONLY:
            raise MoodleError(f"{function} is not on the read-only allowlist")
        key = (function, tuple(sorted(params.items())))
        hit = self._cache.get(key)
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
