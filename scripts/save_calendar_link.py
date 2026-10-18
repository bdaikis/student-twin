"""Save the private iCal address of the calendar that holds your classes, for the twin's timetable.

Run it yourself in a terminal (the prompt hides what you paste):
    python scripts/save_calendar_link.py

In Google Calendar: Settings → select the calendar → Integrate calendar → "Secret address in iCal
format". The address gives read access to that calendar, so it is kept in Windows Credential
Manager (as student-twin/calendar-ical), never in the repo or a config file, and never printed.
The script checks that the address returns a calendar before saving it. Standard library only.
"""
import ctypes
import ctypes.wintypes as wt
import getpass
import sys
import urllib.error
import urllib.request

TARGET = "student-twin/calendar-ical"


class _CREDENTIAL(ctypes.Structure):
    _fields_ = [("Flags", wt.DWORD), ("Type", wt.DWORD), ("TargetName", wt.LPWSTR), ("Comment", wt.LPWSTR),
                ("LastWritten", wt.FILETIME), ("CredentialBlobSize", wt.DWORD),
                ("CredentialBlob", ctypes.POINTER(ctypes.c_char)), ("Persist", wt.DWORD),
                ("AttributeCount", wt.DWORD), ("Attributes", ctypes.c_void_p),
                ("TargetAlias", wt.LPWSTR), ("UserName", wt.LPWSTR)]


def save(target: str, secret: str) -> None:
    blob = secret.encode("utf-8")
    buf = ctypes.create_string_buffer(blob, len(blob))
    cred = _CREDENTIAL(Type=1, TargetName=target, CredentialBlobSize=len(blob),  # CRED_TYPE_GENERIC
                       CredentialBlob=ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)),
                       Persist=2, UserName="student-twin")  # CRED_PERSIST_LOCAL_MACHINE
    if not ctypes.WinDLL("advapi32", use_last_error=True).CredWriteW(ctypes.byref(cred), 0):
        sys.exit(f"Windows refused to save the credential (error {ctypes.get_last_error()}).")


def main() -> None:
    url = getpass.getpass("Paste the secret iCal address (hidden): ").strip()
    if not url.startswith("https://") or not url.lower().split("?")[0].endswith(".ics"):
        sys.exit("That doesn't look like an iCal address (https://….ics). Nothing was saved.")
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "student-twin"}),
                                    timeout=30) as resp:
            body = resp.read().decode("utf-8", "replace")
    except (urllib.error.URLError, OSError) as exc:
        sys.exit(f"Couldn't fetch the address ({exc}). Nothing was saved.")
    if "BEGIN:VCALENDAR" not in body:
        sys.exit("The address didn't return a calendar. Nothing was saved.")
    save(TARGET, url)
    print(f"Saved as {TARGET} in Windows Credential Manager: a calendar with "
          f"{body.count('BEGIN:VEVENT')} event(s).")


if __name__ == "__main__":
    main()
