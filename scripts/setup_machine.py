"""Write this machine's own parts of the twin's setup: the parts that hold absolute paths, so the same
repo installs on any Windows user folder (phase 0 of the desktop host plan, 2026-10-25).

Run it with the twin's own Python, from the repo:
    <venv>\\Scripts\\python.exe scripts\\setup_machine.py            show what would change
    <venv>\\Scripts\\python.exe scripts\\setup_machine.py --apply    change it
Options: --jobs-paused creates missing scheduled jobs switched off and --telegram off keeps Hermes
from polling the bot (both for a second machine before its cutover; --telegram on at the cutover);
--widget-autostart starts the widget at sign-in.

What it writes, each only when different from what is there:
- Hermes's config.yaml: the `desktop` and `portal` MCP servers (this Python, this repo, this data
  folder), exactly the twin's toolsets for the widget (api_server), CLI and Telegram, and the
  Hermes defaults the twin changes (model, context window, no borrowed logins, no speech, speech
  recognition on the CPU), and Whisper's vocabulary hint from config.json's voice.vocabulary. Edited
  with Hermes's own ruamel.yaml, so the file's comments survive.
- Launchers in Hermes's scripts folder for the three scheduled jobs (Hermes runs only scripts from
  there), pointing at this repo.
- The scheduled jobs themselves (class-reminders, digest, deadline-watch) with this Python as their
  interpreter, created through `hermes cron create` when missing.
- With --widget-autostart, a Startup-folder entry that starts the widget at sign-in.
Secrets are not touched: setup_secrets.py, save_calendar_link.py and save_portal_token.py do those.
Restart Hermes afterwards (hermes gateway restart) so it loads the servers.
"""
import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PYTHON = Path(sys.executable)
LOCAL = Path(os.environ["LOCALAPPDATA"])
HERMES_HOME = Path(os.environ.get("HERMES_HOME") or LOCAL / "hermes")
DATA = Path(os.environ.get("STUDENT_TWIN_HOME") or LOCAL / "student-twin")
STARTUP = Path(os.environ["APPDATA"]) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"

SERVERS = ("desktop", "portal")
TOOLSETS = {"api_server": ["web", "memory", "skills", "todo", "desktop", "portal"],
            "cli": ["web", "memory", "skills", "todo", "desktop", "portal"],
            "telegram": ["web", "memory", "skills", "todo", "desktop", "portal"]}
# Hermes defaults the twin changes (slice 1, 2026-10-13): Haiku 5.5 with its real context window; never
# borrow Claude Code's or Codex's logins (single-use refresh tokens, and usage would bill the
# subscription); never speak replies.
SETTINGS = {"model.default": "anthropic/claude-haiku-5.5", "model.provider": "anthropic",
            "model.context_length": 1000000, "auth.adopt_external_logins": False, "voice.auto_tts": False,
            # Speech on the CPU: the GPU path needs NVIDIA's cublas64_12.dll, and without it a first
            # transcription on the desktop's GTX 1080 Ti hung rather than failing (2026-10-27).
            "stt.local.device": "cpu", "stt.local.compute_type": "int8"}
# A fresh Hermes config points model.base_url at OpenRouter. With the anthropic provider the calls
# still go to api.anthropic.com, but the leftover address makes Hermes drop the context_length pin.
REMOVE = ["model.base_url"]
JOBS = [  # name, schedule, script, what it is
    ("class-reminders", "every 5m", "class_reminders.py", "class start reminders and attendance follow-ups"),
    ("digest", "0 7,21 * * *", "digest.py", "the twice-daily digest"),
    ("deadline-watch", "every 30m", "deadline_watch.py", "deadline changes and 48/24/3 h reminders"),
]

# Runs inside Hermes's Python (it has ruamel.yaml): reads {"servers", "toolsets", "apply"} on stdin,
# prints the keys it would change (and changes them with apply).
_YAML_EDIT = r'''
import json, sys
from ruamel.yaml import YAML
req = json.load(sys.stdin)
yaml = YAML(); yaml.preserve_quotes = True; yaml.width = 4096; yaml.indent(mapping=2, sequence=4, offset=2)
path = req["path"]
with open(path, encoding="utf-8") as f:
    doc = yaml.load(f)
changed = []
servers = doc.setdefault("mcp_servers", {})
for name, want in req["servers"].items():
    have = servers.get(name)
    if have is None:
        servers[name] = want; changed.append(f"mcp_servers.{name} (new)")
        continue
    for key, value in want.items():
        old = have.get(key)
        if (dict(old) if hasattr(old, "items") else list(old) if isinstance(old, list) else old) != value:
            have[key] = value; changed.append(f"mcp_servers.{name}.{key}")
for dotted, value in req["settings"].items():
    node, *parents, leaf = [doc, *dotted.split(".")]
    for part in parents:
        node = node.setdefault(part, {})
    if node.get(leaf) != value:
        node[leaf] = value; changed.append(dotted)
for dotted in req["remove"]:
    node, *parents, leaf = [doc, *dotted.split(".")]
    for part in parents:
        node = node.get(part) or {}
    if leaf in node:
        del node[leaf]; changed.append(f"{dotted} (removed)")
# Exactly the twin's toolsets: a fresh Hermes gives each channel a broad default (hermes-telegram
# includes the terminal, file tools and send_message), which adding to would keep.
toolsets = doc.setdefault("platform_toolsets", {})
for platform, want in req["toolsets"].items():
    have = list(toolsets.get(platform) or [])
    if set(have) != set(want):
        toolsets[platform] = want
        changed.append(f"platform_toolsets.{platform} ({', '.join(have) or 'default'} -> {', '.join(want)})")
if changed and req["apply"]:
    with open(path, "w", encoding="utf-8") as f:
        yaml.dump(doc, f)
print(json.dumps(changed))
'''


def hermes_python() -> Path:
    """Hermes's own Python, found the way the widget finds it (installs/*/facts.json)."""
    facts = sorted((HERMES_HOME / "installs").glob("*/facts.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not facts:
        sys.exit(f"Hermes isn't installed under {HERMES_HOME} (no installs/*/facts.json).")
    venv = json.loads(facts[0].read_text(encoding="utf-8"))["packages"]["venv"]["environment"]
    return Path(venv) / "Scripts" / "python.exe"


def hermes_config(apply: bool, telegram: str | None) -> list[str]:
    # Hermes passes its servers only these variables; OneDrive's is needed for "%OneDrive%" paths in
    # the twin's config (common.expand_path also reads it from the registry when missing), and
    # HERMES_HOME for send_screenshot when Hermes is installed outside %LOCALAPPDATA%.
    env = {"STUDENT_TWIN_HOME": str(DATA), "LOCALAPPDATA": str(LOCAL)}
    env.update({v: os.environ[v] for v in ("OneDrive", "OneDriveConsumer", "HERMES_HOME") if os.environ.get(v)})
    servers = {name: {"command": str(PYTHON), "args": [str(REPO / "mcp_servers" / f"{name}.py")],
                      "env": env, "enabled": True} for name in SERVERS}
    settings = dict(SETTINGS)
    # Whisper's vocabulary hint (app names, course codes, command verbs) is personal, so it lives in
    # the twin's config.json, which transfer_state.py carries, not in this repo.
    config = DATA / "config.json"
    vocabulary = (json.loads(config.read_text(encoding="utf-8-sig")).get("voice", {}).get("vocabulary")
                  if config.exists() else None)
    if vocabulary:
        settings["stt.prompt"] = vocabulary
    if telegram:  # an explicit platforms.telegram.enabled wins over the bot token in .env
        settings["platforms.telegram.enabled"] = telegram == "on"
    req = {"path": str(HERMES_HOME / "config.yaml"), "servers": servers, "toolsets": TOOLSETS,
           "settings": settings, "remove": REMOVE, "apply": apply}
    out = subprocess.run([str(hermes_python()), "-c", _YAML_EDIT], input=json.dumps(req), capture_output=True,
                         text=True, encoding="utf-8")
    if out.returncode:
        sys.exit(f"Editing Hermes's config failed:\n{out.stderr.strip()[-800:]}")
    return json.loads(out.stdout.strip().splitlines()[-1])


def launchers(apply: bool) -> list[str]:
    changed = []
    folder = HERMES_HOME / "scripts"
    for name, _, script, what in JOBS:
        text = (f'"""Launcher for Student Twin\'s {what}; the script itself lives in the student-twin repo.\n'
                f'Hermes\'s scheduler only runs scripts from this folder. Written by scripts/setup_machine.py."""\n'
                f"import runpy\n\nrunpy.run_path(r\"{REPO / 'scripts' / script}\", run_name=\"__main__\")\n")
        path = folder / script
        if not path.exists() or path.read_text(encoding="utf-8") != text:
            changed.append(f"launcher {path}")
            if apply:
                folder.mkdir(parents=True, exist_ok=True)
                path.write_text(text, encoding="utf-8")
    return changed


def jobs(apply: bool, paused: bool) -> list[str]:
    jobs_file = HERMES_HOME / "cron" / "jobs.json"
    existing = json.loads(jobs_file.read_text(encoding="utf-8")).get("jobs", []) if jobs_file.exists() else []
    by_name = {j.get("name"): j for j in existing}
    interpreter = PYTHON.as_posix()
    changed, fix = [], {}
    for name, schedule, script, _ in JOBS:
        job = by_name.get(name)
        if job is None:
            changed.append(f"job {name} (new{', paused' if paused else ''})")
            if apply:
                cmd = ["hermes", "cron", "create", schedule, "--no-agent", "--script", script,
                       "--deliver", "telegram", "--name", name, "--interpreter", interpreter]
                out = subprocess.run(cmd + (["--paused"] if paused else []), capture_output=True, text=True)
                if out.returncode:
                    sys.exit(f"hermes cron create {name} failed:\n{(out.stderr or out.stdout).strip()[-600:]}")
        elif job.get("interpreter") != interpreter:
            changed.append(f"job {name}: interpreter")
            fix[name] = interpreter
    if apply and fix:
        data = json.loads(jobs_file.read_text(encoding="utf-8"))
        for job in data["jobs"]:
            if job.get("name") in fix:
                job["interpreter"] = fix[job["name"]]
        jobs_file.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    return changed


def widget_autostart(apply: bool) -> list[str]:
    electron = REPO / "widget" / "node_modules" / "electron" / "dist" / "electron.exe"
    text = ("' Starts the Student Twin widget at sign-in. Written by scripts/setup_machine.py.\r\n"
            'Set sh = CreateObject("WScript.Shell")\r\n'
            f'sh.CurrentDirectory = "{REPO / "widget"}"\r\n'
            f'sh.Run """{electron}"" .", 0, False\r\n')
    path = STARTUP / "Student_Twin_Widget.vbs"
    if path.exists() and path.read_text(encoding="utf-8") == text:
        return []
    if apply:
        path.write_text(text, encoding="utf-8")
    return [f"widget autostart {path}"]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--apply", action="store_true", help="make the changes (default: only show them)")
    ap.add_argument("--jobs-paused", action="store_true", help="create missing scheduled jobs switched off")
    ap.add_argument("--telegram", choices=["on", "off"],
                    help="off: Hermes doesn't poll the bot (a second machine before its cutover); on: it does")
    ap.add_argument("--widget-autostart", action="store_true", help="start the widget at sign-in")
    args = ap.parse_args()
    try:
        import mcp  # noqa: F401
    except ImportError:
        sys.exit(f"{PYTHON} lacks the twin's packages; run this with the twin's venv Python "
                 "(pip install -r requirements.txt first).")
    print(f"Repo {REPO}\nPython {PYTHON}\nHermes {HERMES_HOME}\nData {DATA}\n")
    changes = hermes_config(args.apply, args.telegram) + launchers(args.apply) + jobs(args.apply, args.jobs_paused)
    if args.widget_autostart:
        changes += widget_autostart(args.apply)
    if not changes:
        print("Nothing to change: this machine's setup matches the repo.")
        return
    print(("Changed:" if args.apply else "Would change (run with --apply):") + "".join(f"\n- {c}" for c in changes))
    if args.apply and any(c.startswith(("mcp_servers", "platform_toolsets", "platforms.")) for c in changes):
        print("\nRestart Hermes so it loads them: hermes gateway restart")


if __name__ == "__main__":
    main()
