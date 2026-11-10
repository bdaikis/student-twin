"""Which windows count as messaging or email windows. The twin never reads, looks at or acts in
them, so other people's messages don't reach the model. Shared by desktop.py, whose window tools
refuse them, and the widget's voice helper, which leaves their titles out of widget requests (it
loads this file by path: it runs with Hermes's Python, outside this folder)."""
import re

MESSAGING = re.compile(r"(?i)whatsapp|gmail|outlook|telegram|messenger|discord|slack|signal|teams|\bmail\b|inbox")


def is_messaging(title: str, process: str) -> bool:
    return bool(MESSAGING.search(title or "") or MESSAGING.search(process or ""))
