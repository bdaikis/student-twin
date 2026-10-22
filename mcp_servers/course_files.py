"""Course files from the portal, saved into Bhanu's own course folders (method 1 of the NotebookLM plan,
2026-10-22). Downloads go straight from the portal with the token in the request body; no browser.

Where: config "files": {"root": "...", "course_folders": {"SIF1003": "Thermal Physics", ...}}. A course
with no folder named there gets one named after the course. Inside it, one subfolder per portal
section, so files keep the order the lecturer posted them in.
What: "notes" (documents and slides, the default) or "all" (any file type); `match` narrows by
words in the section, activity or file name ("module 3", "tutorial"). Videos are never downloaded,
and courses in config files.skip_courses are left out entirely (Bhanu, 2026-10-22).
A file already anywhere under the root with the same name and size is skipped, so files he saved
himself (or an earlier run) aren't fetched twice. A file whose name exists with a different size is
saved beside it with the portal's date, never over it.
"""
import html
import os
import re
import time
from pathlib import Path

from common import config, course_id, match_course, moodle, norm

NOTES = {"pdf", "ppt", "pptx", "doc", "docx", "xls", "xlsx", "odt", "odp", "ods", "rtf", "txt", "md", "csv"}
VIDEOS = {"mp4", "m4v", "mov", "mkv", "avi", "webm", "wmv"}
ASK_ABOVE_MB = 200


def root() -> Path:
    return Path(config().get("files", {}).get("root") or Path.home() / "Documents" / "Course files")


def _safe(name: str) -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "-", " ".join(html.unescape(name).split()))
    return name.strip(" .")[:80] or "Untitled"


def course_folder(code: str, info: dict) -> Path:
    return root() / _safe(config().get("files", {}).get("course_folders", {}).get(code) or info["name"])


def _kind(filename: str) -> str:
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    return "notes" if ext in NOTES else "videos" if ext in VIDEOS else "other"


def _key(name: str) -> str:
    """A file's name as saved (cleaned for Windows, without the portal-date suffix added on a clash),
    so a second run recognises what the first one saved."""
    return re.sub(r" \(SPeCTRUM \d{4}-\d{2}-\d{2}\)(?=\.[^.]*$|$)", "", _safe(name)).lower()


def _have() -> set[tuple[str, int]]:
    out = set()
    if root().exists():
        for dirpath, _, names in os.walk(root()):
            for n in names:
                try:
                    out.add((_key(n), os.path.getsize(os.path.join(dirpath, n))))
                except OSError:
                    pass
    return out


def plan(code: str, info: dict, what: str = "notes", match: str = "") -> dict:
    """What a download would fetch: {"get": [(file, dest)], "have": [...], "left_out": {kind: (n, bytes)}, "links": [...]}."""
    want = set(norm(match).split())
    have = _have()
    base = course_folder(code, info)
    get, already, links, left_out, taken = [], [], [], {}, set()
    for sec in moodle().call("core_course_get_contents", courseid=course_id(info)):
        section = _safe(sec.get("name") or f"Section {sec.get('section')}")
        for mod in sec.get("modules", []):
            if not mod.get("uservisible", True):
                continue
            if mod.get("modname") == "url":
                links.append(html.unescape(mod.get("name", "")).strip())
                continue
            for f in mod.get("contents", []):
                if f.get("type") != "file":
                    continue
                words = set(norm(f"{sec.get('name', '')} {mod.get('name', '')} {f['filename']}").split())
                if want and not want <= words:
                    continue
                kind = _kind(f["filename"])
                if kind == "videos":
                    continue
                if what != "all" and kind != what:
                    n, size = left_out.get(kind, (0, 0))
                    left_out[kind] = (n + 1, size + f.get("filesize", 0))
                    continue
                if (_key(f["filename"]), f.get("filesize", 0)) in have:
                    already.append(f["filename"])
                    continue
                folder = base / section
                if mod.get("modname") == "folder":  # a folder activity keeps its own name and subfolders
                    folder = folder / _safe(mod.get("name", "")) / Path(*[_safe(p) for p in f.get("filepath", "/").split("/") if p])
                dest = folder / _safe(f["filename"])
                if dest.exists() or dest in taken:  # same name, different content: keep his, add the portal's
                    stem, dot, ext = dest.name.rpartition(".")
                    date = time.strftime("%Y-%m-%d", time.localtime(f.get("timemodified", 0)))
                    dest = dest.with_name(f"{stem or ext} (SPeCTRUM {date}){dot}{ext if stem else ''}")
                    if dest.exists() or dest in taken:
                        already.append(f["filename"])
                        continue
                taken.add(dest)
                get.append((f, dest))
    return {"code": code, "base": base, "get": get, "have": already, "left_out": left_out, "links": links}


def _mb(n: int) -> str:
    return f"{n / 1e6:.0f} MB" if n >= 1e6 else f"{max(n, 1) / 1e3:.0f} KB"


def download(course: str = "", what: str = "notes", match: str = "", allow_large: bool = False) -> str:
    if what not in ("notes", "all"):
        return 'what must be "notes" or "all". Videos are never downloaded.'
    skip = set(config().get("files", {}).get("skip_courses", []))
    if course:
        found = match_course(course)
        if isinstance(found, str):
            return found
        if found[0] in skip:
            return f"{found[0]} is excluded from downloads by Bhanu's own choice; nothing was downloaded. The twin can't change that; Claude Code can, in its settings."
        courses = [found]
    else:
        courses = [(c, i) for c, i in config().get("courses", {}).items() if c not in skip]
    plans = [plan(code, info, what, match) for code, info in courses]
    total = sum(f.get("filesize", 0) for p in plans for f, _ in p["get"])
    if total > ASK_ABOVE_MB * 1e6 and not allow_large:
        return (f"That's {sum(len(p['get']) for p in plans)} files, {_mb(total)}. Ask Bhanu whether to go ahead; "
                "if he says yes, call again with allow_large=True.")
    m = moodle()
    lines, failed = [], []
    for p in plans:
        saved = 0
        for f, dest in p["get"]:
            try:
                m.download(f["fileurl"], dest, f.get("timemodified"), f.get("filesize"))
                saved += f.get("filesize", 0)
            except Exception as exc:
                failed.append(f"{p['code']} {f['filename']} ({exc})")
        done = len(p["get"]) - sum(1 for x in failed if x.startswith(p["code"] + " "))
        parts = []
        if done:
            parts.append(f"saved {done} file{'s' if done > 1 else ''} ({_mb(saved)})")
        if p["have"]:
            parts.append(f"{len(p['have'])} already there")
        for kind, (n, size) in sorted(p["left_out"].items()):
            if kind != "other" or n:
                parts.append(f"{n} {kind if kind != 'other' else 'other files'} left out ({_mb(size)})")
        if p["links"]:
            parts.append(f"{len(p['links'])} links to other sites not downloaded ({'; '.join(p['links'][:3])})")
        if parts or len(plans) == 1:
            lines.append(f"{p['code']}: " + (", ".join(parts) or "no files match") + f". Folder: {p['base']}")
    if failed:
        lines.append("Couldn't download: " + "; ".join(failed[:5]))
    return "\n".join(lines) or "No course has files that match."
