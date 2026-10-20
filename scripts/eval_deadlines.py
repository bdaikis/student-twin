"""Scored check for slice 3 (3d): ask the deployed twin deadline questions and score its answers
against a key built straight from the portal (raw read-only calls, not deadlines.py) plus the
deadlines that come from course forms or that Bhanu added (the portal doesn't have those).

    python scripts\\eval_deadlines.py [questions.json]

Questions live in the per-user data folder (<data>/evals/deadline_questions.json), since they name
his courses. Each is asked in a fresh Hermes session with no hints, as Telegram would ask it:
  {"q": "...", "type": "items", "days": 7}               every item due in the next N days is named with its date
  {"q": "...", "type": "item", "course": "X", "title": "Y"}  that item is named with its date
  {"q": "...", "type": "overdue"}                        every overdue unsubmitted assignment or quiz is named
  {"q": "...", "type": "none"}                           the answer says there's nothing, and names no date
Results are printed and saved to <data>/evals/.
"""
import json
import re
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "mcp_servers"))

import deadlines as dl  # noqa: E402  (only for the form and own items, and the course list)
import timetable  # noqa: E402
from common import DATA, moodle  # noqa: E402

HERMES = "http://127.0.0.1:8642/v1/chat/completions"
ENV = Path.home() / "AppData" / "Local" / "hermes" / ".env"


def key_items(now: int) -> list[dict]:
    """The truth: portal assignments and quizzes (raw calls) plus form/own items from the store."""
    m = moodle()
    courses = dl._courses()
    params = {f"courseids[{i}]": c for i, c in enumerate(courses)}
    items = []
    for c in m.call("mod_assign_get_assignments", **params).get("courses", []):
        for a in c["assignments"]:
            if a.get("duedate", 0) > now - 30 * 86400:
                last = m.call("mod_assign_get_submission_status", assignid=a["id"]).get("lastattempt") or {}
                done = (last.get("submission") or {}).get("status") == "submitted"
                items.append({"course": courses.get(c["id"], "?"), "title": dl._clean(a["name"]), "due": a["duedate"],
                              "kind": "assignment", "done": done})
    for q in m.call("mod_quiz_get_quizzes_by_courses", **params).get("quizzes", []):
        if (q.get("timeclose") or 0) > now - 30 * 86400:
            att = m.call("mod_quiz_get_user_attempts", quizid=q["id"], status="all").get("attempts", [])
            items.append({"course": courses.get(q["course"], "?"), "title": dl._clean(q["name"]), "due": q["timeclose"],
                          "kind": "quiz", "done": any(a.get("state") == "finished" for a in att)})
    db = dl._connect()
    try:
        for r in db.execute("SELECT course, title, due, kind FROM items WHERE source != 'spectrum'"):
            items.append({"course": r["course"], "title": r["title"], "due": r["due"], "kind": r["kind"], "done": False})
    finally:
        db.close()
    return items


def ask(question: str) -> str:
    key = next(l.split("=", 1)[1].strip() for l in ENV.read_text(encoding="utf-8").splitlines()
               if l.startswith("API_SERVER_KEY="))
    body = json.dumps({"model": "hermes-agent", "stream": False,
                       "messages": [{"role": "user", "content": question}]}).encode()
    req = urllib.request.Request(HERMES, data=body, headers={"Authorization": f"Bearer {key}",
                                                             "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=300) as r:
        return json.load(r)["choices"][0]["message"]["content"]


def mentions(answer: str, item: dict) -> bool:
    """The answer names the item (its course or a distinctive title word) and its date."""
    a = answer.lower()
    d = datetime.fromtimestamp(item["due"], timetable.LOCAL)
    dates = {f"{d.day} {d:%b}".lower(), f"{d.day} {d:%B}".lower(), f"{d:%b} {d.day}".lower(), f"{d:%B} {d.day}".lower()}
    words = [w for w in re.findall(r"[a-z0-9&]+", item["title"].lower()) if len(w) > 3]
    named = item["course"].lower() in a or any(w in a for w in words)
    return named and any(x in a for x in dates)


def _dates(text: str, year: int):
    """Day-month dates named in text (a month before September means next year)."""
    for day, mon in re.findall(r"\b(\d{1,2}) (jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)", text.lower()):
        try:
            d = datetime.strptime(f"{day} {mon} {year}", "%d %b %Y").date()
        except ValueError:
            continue
        yield d.replace(year=year + 1) if d.month < 9 else d


def score(q: dict, answer: str, items: list[dict], now: int) -> tuple[bool, str]:
    t = q["type"]
    if t == "none":
        # Only a future date counts as inventing a deadline ("as of Tue 20 Oct" or a past item is fine).
        today = datetime.fromtimestamp(now, timetable.LOCAL).date()
        dated = any(d > today for d in _dates(answer, today.year))
        nothing = re.search(r"(?i)\bno(thing| deadlines?| assignments?| tests?)?\b|isn't|aren't|don't see|not (find|listed|on)", answer)
        return bool(nothing) and not dated, "says nothing, no dates" if nothing and not dated else "named a date or didn't say none"
    if t == "items":
        want = [i for i in items if now <= i["due"] <= now + q.get("days", 7) * 86400 and not i["done"]]
    elif t == "overdue":
        want = [i for i in items if i["due"] < now and not i["done"] and i["kind"] in ("assignment", "quiz")]
    else:
        want = [i for i in items if i["course"] == q["course"] and q["title"].lower() in i["title"].lower()]
    missed = [f"{i['course']} {i['title']}" for i in want if not mentions(answer, i)]
    return not missed and bool(want), (f"missed: {'; '.join(missed)}" if missed else f"all {len(want)} named")


def main() -> None:
    qfile = Path(sys.argv[1]) if len(sys.argv) > 1 else DATA / "evals" / "deadline_questions.json"
    questions = json.loads(qfile.read_text(encoding="utf-8"))
    now = int(time.time())
    items = key_items(now)
    results = []
    for q in questions:
        t0 = time.time()
        answer = ask(q["q"])
        ok, why = score(q, answer, items, now)
        results.append({**q, "answer": answer, "pass": ok, "why": why, "seconds": round(time.time() - t0, 1)})
        print(f"{'PASS' if ok else 'FAIL'}  {q['q']}  ({why}, {results[-1]['seconds']} s)")
    passed = sum(r["pass"] for r in results)
    print(f"\n{passed}/{len(results)} passed")
    out = DATA / "evals" / f"deadlines-{time.strftime('%Y%m%d-%H%M')}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"at": now, "passed": passed, "results": results}, indent=1, ensure_ascii=False), encoding="utf-8")
    print(f"saved {out}")


if __name__ == "__main__":
    main()
