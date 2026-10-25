"""Save your university Moodle site's mobile token to Windows Credential Manager, then test it.

Run it yourself in a terminal (the prompt hides what you paste):
    python scripts/save_portal_token.py

The site comes from the twin's config ("portal": {"url": ...}). The script prints a sign-in link:
open it in the browser where you use Moodle and sign in as usual. The browser then tries to open a
moodlemobile://token=... link and fails; copy that link (from the address bar, or the error line in
the browser console, F12) and paste it here. The token is tied to your account, not the machine, and
carries write rights, so the twin calls only a read-only allowlist of functions with it. The token
is never printed. Standard library only.
"""
import base64
import ctypes
import ctypes.wintypes as wt
import getpass
import hashlib
import json
import re
import sys
import urllib.parse
import urllib.request

import os
from pathlib import Path

_DATA = Path(os.environ.get("STUDENT_TWIN_HOME") or Path(os.environ["LOCALAPPDATA"]) / "student-twin")
try:
    _PORTAL = json.loads((_DATA / "config.json").read_text(encoding="utf-8"))["portal"]
except (OSError, KeyError, ValueError):
    sys.exit(f"No portal in {_DATA / 'config.json'}; copy config.example.json there and fill it in first.")
SITE = _PORTAL["url"].rstrip("/")
PASSPORT = "12345"  # any value; Moodle echoes it back so the link can be checked
TARGET = _PORTAL.get("token_credential", "student-twin/spectrum-token")
CRED_TYPE_GENERIC = 1
CRED_PERSIST_LOCAL_MACHINE = 2


class CREDENTIAL(ctypes.Structure):
    _fields_ = [
        ("Flags", wt.DWORD),
        ("Type", wt.DWORD),
        ("TargetName", wt.LPWSTR),
        ("Comment", wt.LPWSTR),
        ("LastWritten", wt.FILETIME),
        ("CredentialBlobSize", wt.DWORD),
        ("CredentialBlob", ctypes.POINTER(ctypes.c_char)),
        ("Persist", wt.DWORD),
        ("AttributeCount", wt.DWORD),
        ("Attributes", ctypes.c_void_p),
        ("TargetAlias", wt.LPWSTR),
        ("UserName", wt.LPWSTR),
    ]


def parse_token(raw: str) -> tuple[str, bool]:
    """Return (token, signature_ok) from a moodlemobile:// link or bare base64 payload."""
    text = urllib.parse.unquote(raw.strip())
    if "token=" in text:
        text = text.split("token=", 1)[1]
    match = re.match(r"[A-Za-z0-9+/=_-]+", text)
    if not match:
        sys.exit("Could not find a token in what was pasted.")
    payload = match.group(0)
    payload += "=" * (-len(payload) % 4)
    try:
        decoded = base64.b64decode(payload, altchars=b"-_" if "-" in payload or "_" in payload else None).decode()
    except Exception:
        sys.exit("The pasted text is not a valid token payload.")
    parts = decoded.split(":::")
    if len(parts) < 2 or not parts[1]:
        sys.exit("Decoded payload has an unexpected shape.")
    expected = hashlib.md5((SITE + PASSPORT).encode()).hexdigest()
    return parts[1], parts[0] == expected


def save_credential(secret: str) -> None:
    data = secret.encode("utf-8")
    blob = ctypes.create_string_buffer(data, len(data))
    cred = CREDENTIAL()
    cred.Type = CRED_TYPE_GENERIC
    cred.TargetName = TARGET
    cred.Comment = "Moodle mobile web-service token for Student Twin (read-only use)"
    cred.CredentialBlobSize = len(data)
    cred.CredentialBlob = ctypes.cast(blob, ctypes.POINTER(ctypes.c_char))
    cred.Persist = CRED_PERSIST_LOCAL_MACHINE
    cred.UserName = "student-twin"
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    if not advapi32.CredWriteW(ctypes.byref(cred), 0):
        sys.exit(f"Saving to Credential Manager failed (Windows error {ctypes.get_last_error()}).")


def call(token: str, function: str) -> dict:
    body = urllib.parse.urlencode({"wstoken": token, "wsfunction": function}).encode()
    req = urllib.request.Request(f"{SITE}/webservice/rest/server.php?moodlewsrestformat=json", data=body)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def main() -> None:
    print(f"Open this link in your browser and sign in:\n  {SITE}/admin/tool/mobile/launch.php"
          f"?service=moodle_mobile_app&passport={PASSPORT}&urlscheme=moodlemobile\n")
    raw = getpass.getpass("Paste the moodlemobile:// link (hidden), then press Enter: ")
    token, signature_ok = parse_token(raw)
    del raw
    print("Signature check:", "OK" if signature_ok else "did not match (token may still work)")

    info = call(token, "core_webservice_get_site_info")
    if "exception" in info:
        sys.exit(f"SPeCTRUM rejected the token: {info.get('errorcode')} - {info.get('message')}")

    save_credential(token)
    print(f"Saved to Windows Credential Manager as '{TARGET}'.")

    functions = {f["name"] for f in info.get("functions", [])}
    wanted = [
        "core_enrol_get_users_courses",
        "mod_assign_get_assignments",
        "mod_quiz_get_quizzes_by_courses",
        "mod_forum_get_forum_discussions",
        "core_course_get_contents",
        "core_calendar_get_action_events_by_timesort",
    ]
    print(f"Site: {info.get('sitename')} | signed in as: {info.get('firstname')}")
    print(f"Web-service functions available: {len(functions)}")
    for name in wanted:
        print(f"  {'yes' if name in functions else 'NO '}  {name}")


if __name__ == "__main__":
    main()
