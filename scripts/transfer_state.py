"""Carry the twin's state from one machine to another, without secrets (desktop host plan, phases 0
and 2).

    python scripts\\transfer_state.py export <folder>          settings: shared rules, memory, saved skills, config
    python scripts\\transfer_state.py export <folder> --data   also the live data (at the cutover)
    python scripts\\transfer_state.py import <folder>          put a bundle in place on this machine

Settings are Hermes's SOUL.md, memories\\MEMORY.md and the skills Hermes saved itself (any skill
folder not in its .bundled_manifest), plus the twin's config.json and evals folder (the scored
checks' questions, so they can run on the new machine). Live data is deadlines.db (form
dates, dismissed items, which reminders were sent), reminders_state.json, attendance_seen.json,
attendance_log.jsonl and attendance_snapshots: copy these only at the cutover, after the old
machine's jobs are paused, so no reminder is sent twice. Secrets (.env, Credential Manager) are never
exported; enter them on the new machine with the setup scripts.
Import keeps a dated .bak copy of every file it replaces. Standard library only.
"""
import argparse
import json
import os
import shutil
import sys
import time
from pathlib import Path

LOCAL = Path(os.environ["LOCALAPPDATA"])
HERMES_HOME = Path(os.environ.get("HERMES_HOME") or LOCAL / "hermes")
DATA = Path(os.environ.get("STUDENT_TWIN_HOME") or LOCAL / "student-twin")
SETTINGS = {"hermes/SOUL.md": HERMES_HOME / "SOUL.md",
            "hermes/memories/MEMORY.md": HERMES_HOME / "memories" / "MEMORY.md",
            "twin/config.json": DATA / "config.json",
            "twin/evals": DATA / "evals"}  # the scored checks' questions, scripts and past results
LIVE = ["deadlines.db", "reminders_state.json", "attendance_seen.json", "attendance_log.jsonl", "attendance_snapshots"]


def own_skills() -> list[Path]:
    """Skill folders Hermes created itself: not listed in its bundled-skills manifest."""
    skills = HERMES_HOME / "skills"
    manifest = skills / ".bundled_manifest"
    bundled = set(manifest.read_text(encoding="utf-8").split()) if manifest.exists() else set()
    return [p for p in skills.iterdir() if p.is_dir() and not p.name.startswith(".")
            and (p / "SKILL.md").exists() and p.name not in bundled and not any(b.startswith(p.name + ":") for b in bundled)]


def _copy(src: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if src.is_dir():
        shutil.copytree(src, dest, dirs_exist_ok=True)
    else:
        shutil.copy2(src, dest)


def export(folder: Path, data: bool) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    done = []
    for name, src in SETTINGS.items():
        if src.exists():
            _copy(src, folder / name); done.append(name)
    for skill in own_skills():
        _copy(skill, folder / "hermes" / "skills" / skill.name); done.append(f"hermes/skills/{skill.name}")
    if data:
        for name in LIVE:
            if (DATA / name).exists():
                _copy(DATA / name, folder / "twin" / name); done.append(f"twin/{name}")
    (folder / "manifest.json").write_text(json.dumps({"exported": time.strftime("%Y-%m-%d %H:%M"),
                                                      "from": os.environ.get("COMPUTERNAME", "?"), "items": done},
                                                     indent=1), encoding="utf-8")
    print(f"Exported {len(done)} item(s) to {folder}:" + "".join(f"\n- {d}" for d in done))


def import_(folder: Path) -> None:
    manifest = folder / "manifest.json"
    if not manifest.exists():
        sys.exit(f"{folder} has no manifest.json; export first.")
    items = json.loads(manifest.read_text(encoding="utf-8"))["items"]
    stamp = time.strftime("%Y%m%d-%H%M")
    for name in items:
        src = folder / name
        if name.startswith("hermes/"):
            dest = HERMES_HOME / Path(*Path(name).parts[1:])
        else:
            dest = DATA / Path(*Path(name).parts[1:])
        if dest.exists():
            backup = dest.with_name(dest.name + f".bak-{stamp}")
            (shutil.copytree if dest.is_dir() else shutil.copy2)(dest, backup)
        _copy(src, dest)
    print(f"Imported {len(items)} item(s) into {HERMES_HOME} and {DATA}; replaced files kept as .bak-{stamp}.\n"
          "Check config.json for this machine (browser_profiles folder names), then restart Hermes.")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("action", choices=["export", "import"])
    ap.add_argument("folder", type=Path)
    ap.add_argument("--data", action="store_true", help="export: include the live data (only at the cutover)")
    args = ap.parse_args()
    export(args.folder, args.data) if args.action == "export" else import_(args.folder)


if __name__ == "__main__":
    main()
