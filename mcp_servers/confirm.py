"""Confirm card handshake between the MCP servers and the desktop widget (slice 2, step 2d-ii).

An irreversible tool calls `await ask(...)` before acting. It writes a request into the twin's data
folder and waits for the widget's answer:

    <data>/confirm/pending/<id>.json   written here: id, nonce, summary, details, expires
    <data>/confirm/answers/<id>.json   written by the widget: nonce, decision ("confirm" | "cancel")
    <data>/confirm/widget.alive        touched by the widget every few seconds while it runs

A request that didn't come from the widget (Telegram: the widget writes widget.busy only while one
of its own requests runs) is confirmed instead through MCP elicitation, which Hermes shows in the
chat as Confirm/Cancel buttons (ask_where).

The model never sees the nonce (tool results carry only the outcome) and has no file tools, so only
a click in the widget can produce a valid answer. Nothing runs unless the answer is "confirm" with
the right nonce, inside the time limit.
"""
import asyncio
import json
import os
import secrets
import time
from pathlib import Path

DATA = Path(os.environ.get("STUDENT_TWIN_HOME") or Path(os.environ["LOCALAPPDATA"]) / "student-twin")
ROOT = DATA / "confirm"
PENDING = ROOT / "pending"
ANSWERS = ROOT / "answers"
ALIVE = ROOT / "widget.alive"
BUSY = ROOT / "widget.busy"

TIMEOUT = 60  # seconds the card waits; matches the widget's countdown
ALIVE_WITHIN = 15  # the widget touches widget.alive every 5 s


def widget_running() -> bool:
    try:
        return time.time() - ALIVE.stat().st_mtime < ALIVE_WITHIN
    except OSError:
        return False


def widget_busy() -> bool:
    """True while a widget request is running (the widget removes the file when it ends; ten minutes
    covers a crash)."""
    try:
        return widget_running() and time.time() - BUSY.stat().st_mtime < 600
    except OSError:
        return False


async def ask_where(ctx, summary: str, details: list[str], action: str = "Confirm",
                    widget_too: bool = True) -> str:
    """Confirm on the channel the request came from: the widget's card, or Telegram buttons (MCP
    elicitation through Hermes's approval flow). `widget_too=False` skips the card for an action he
    can watch on the widget (an upload) and asks only on Telegram. Returns "confirm", "cancel",
    "timeout", "invalid", "no_widget" or "no_channel"."""
    if widget_busy():
        return await ask(summary, details, action) if widget_too else "confirm"
    if ctx is None:
        return "no_channel"
    from pydantic import BaseModel

    class Confirmation(BaseModel):
        pass

    text = "\n".join([summary, *details[:12], "", f"Approve to {action.lower()}; deny to cancel."])
    try:
        result = await asyncio.wait_for(ctx.elicit(text, Confirmation), timeout=330)
    except Exception:
        return "timeout"
    return {"accept": "confirm", "decline": "cancel"}.get(getattr(result, "action", ""), "timeout")


def _write_json(path: Path, data: dict) -> None:
    # Write then rename, so the widget never reads half a file.
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data), encoding="utf-8")
    os.replace(tmp, path)


def _prune() -> None:
    """Remove requests and answers left by a crash or a late click (older than the time limit)."""
    cutoff = time.time() - 2 * TIMEOUT
    for folder in (PENDING, ANSWERS):
        for p in folder.glob("*.json"):
            try:
                if p.stat().st_mtime < cutoff:
                    p.unlink()
            except OSError:
                pass


async def ask(summary: str, details: list[str], action: str = "Confirm") -> str:
    """Show a confirm card and wait. Returns "confirm", "cancel", "timeout", "invalid" or "no_widget".

    `summary` is the card's question ("Delete 14 files in Downloads?"); `details` are a few short
    lines under it; `action` labels the confirm button."""
    if not widget_running():
        return "no_widget"
    PENDING.mkdir(parents=True, exist_ok=True)
    ANSWERS.mkdir(parents=True, exist_ok=True)
    _prune()
    rid = secrets.token_hex(8)
    nonce = secrets.token_hex(16)
    pending, answer = PENDING / f"{rid}.json", ANSWERS / f"{rid}.json"
    expires = time.time() + TIMEOUT
    _write_json(pending, {"id": rid, "nonce": nonce, "summary": summary, "details": details[:12],
                          "action": action, "expires": expires})
    try:
        while time.time() < expires:
            if answer.exists():
                try:
                    got = json.loads(answer.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    got = None  # still being written; read again next round
                if got is not None:
                    if got.get("nonce") != nonce:
                        return "invalid"  # not from the widget's card
                    return "confirm" if got.get("decision") == "confirm" else "cancel"
            await asyncio.sleep(0.2)
        return "timeout"
    finally:
        for p in (pending, answer):
            try:
                p.unlink()
            except OSError:
                pass


OUTCOME = {
    "cancel": "Bhanu declined it (confirm card or Telegram buttons). Nothing was done; don't retry unless he asks again.",
    "invalid": "An answer arrived that didn't come from the widget's confirm card, so nothing was done.",
    "timeout": "The confirmation got no answer in time and closed. Nothing was done.",
    "no_channel": "Nothing was done: there was no way to ask Bhanu for confirmation from here.",
    "no_widget": ("Nothing was done: this needs Bhanu's confirmation on the desktop widget, and the "
                  "widget isn't running."),
}
