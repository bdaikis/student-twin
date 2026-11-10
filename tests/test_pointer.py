"""The window in front and the pointer (2026-11-11): what the voice helper says is in front, when it
reports a shown pointer as stale, and what desktop.py's point_at checks before handing the spot to
the widget. Windows used here are message-only windows this test makes; nothing is clicked or shown."""
import ctypes
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'widget' / 'helper'))
sys.path.insert(0, str(ROOT / 'mcp_servers'))
import voice_helper as vh  # noqa: E402


def window(title):
    """A real (message-only, never visible) window with this title."""
    return vh.user32.CreateWindowExW(0, 'STATIC', title, 0, 0, 0, 0, 0, vh.HWND_MESSAGE, None, None, None)


class Foreground(unittest.TestCase):
    def setUp(self):
        self.made = []

    def tearDown(self):
        for hwnd in self.made:
            vh.user32.DestroyWindow(hwnd)

    def info(self, title):
        hwnd = window(title)
        self.made.append(hwnd)
        return vh.foreground_info(hwnd)

    def test_title_and_program(self):
        info = self.info('Lab 3  -  Google Sheets - Google Chrome')
        self.assertEqual(info['title'], 'Lab 3 - Google Sheets - Google Chrome')  # spaces collapsed
        self.assertTrue(info['process'].lower().startswith('python'))
        self.assertFalse(info['messaging'] or info['desktop'])

    def test_messaging_title_withheld(self):
        for title in ('WhatsApp', 'Inbox (3) - bhanu@example.com - Gmail', 'Chat | Microsoft Teams'):
            info = self.info(title)
            self.assertTrue(info['messaging'], title)
            self.assertEqual(info['title'], '', title)

    def test_same_rule_as_desktop_tools(self):
        import messaging
        self.assertEqual(vh.is_messaging.__module__, 'student_twin_messaging')  # loaded from mcp_servers
        self.assertEqual(vh.is_messaging('Lab 3', 'chrome.exe'), messaging.is_messaging('Lab 3', 'chrome.exe'))

    def test_desktop(self):
        progman = vh.user32.FindWindowW('Progman', None)
        if not progman:
            self.skipTest('no desktop window in this session')
        self.assertTrue(vh.foreground_info(progman)['desktop'])

    def test_none(self):
        with patch.object(vh, '_last_other', None):
            self.assertEqual(vh.foreground_info(), {})


class FakeUser32:
    def __init__(self):
        self.alive, self.iconic, self.fg = True, False, 100

    def IsWindow(self, hwnd): return self.alive
    def IsIconic(self, hwnd): return self.iconic
    def GetForegroundWindow(self): return self.fg


class PointerWatch(unittest.TestCase):
    TARGET = 200

    def setUp(self):
        self.u = FakeUser32()
        self.rect = (0, 0, 800, 600)
        self.events = []
        self.patches = [patch.object(vh, 'user32', self.u),
                        patch.object(vh, '_rect', side_effect=lambda h: self.rect),
                        patch.object(vh, '_counts_as_front', side_effect=lambda h: h != 999),  # 999: the widget
                        patch.object(vh, 'emit', side_effect=lambda **e: self.events.append(e))]
        for p in self.patches:
            p.start()
        self.p = vh.Pointer()
        self.p.show(self.TARGET)

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def test_quiet_while_nothing_changes(self):
        self.assertIsNone(self.p.check())
        self.u.fg = self.TARGET  # he clicks into the pointed window... (a click clears it anyway)
        self.assertIsNone(self.p.check())
        self.u.fg = 999  # ...or into the widget
        self.assertIsNone(self.p.check())
        self.assertTrue(self.p.active())

    def test_click_or_wheel(self):
        self.p.touched.set()
        self.assertEqual(self.p.check(), 'input')
        self.assertEqual(self.events, [{'event': 'pointer_clear', 'reason': 'input'}])
        self.assertFalse(self.p.active())
        self.assertIsNone(self.p.check())  # reported once
        self.assertEqual(len(self.events), 1)

    def test_another_window_in_front(self):
        self.u.fg = 300
        self.assertEqual(self.p.check(), 'focus')

    def test_window_moved_minimized_closed(self):
        self.rect = (10, 0, 810, 600)
        self.assertEqual(self.p.check(), 'moved')
        self.p.show(self.TARGET)
        self.u.iconic = True
        self.assertEqual(self.p.check(), 'moved')
        self.u.iconic = False
        self.p.show(self.TARGET)
        self.u.alive = False
        self.assertEqual(self.p.check(), 'closed')

    def test_a_click_before_the_pointer_does_not_count(self):
        self.p.touched.set()
        self.p.show(self.TARGET)
        self.assertIsNone(self.p.check())

    def test_hidden_by_the_widget(self):
        self.p.show(None)
        self.assertFalse(self.p.active())
        self.assertIsNone(self.p.check())
        self.assertEqual(self.events, [])

    def test_mouse_hook_wanted_only_while_shown(self):
        with patch.object(vh, 'pointer', self.p), patch.object(vh.ptt, 'wants_mouse', return_value=False), \
                patch.object(vh.snip, 'state', vh.SnipGesture.IDLE):
            self.assertTrue(vh.mouse_wanted())
            self.p.show(None)
            self.assertFalse(vh.mouse_wanted())


class PointAt(unittest.TestCase):
    """desktop.py's point_at, with the window lookup and Windows calls faked."""

    @classmethod
    def setUpClass(cls):
        try:
            import desktop
        except ImportError as exc:  # needs the twin's venv (mcp, uiautomation)
            raise unittest.SkipTest(f'desktop.py not importable here: {exc}')
        cls.d = desktop

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.file = Path(self.tmp.name) / 'pointer.json'
        win = {'hwnd': 4242, 'title': 'Lab 3 - Google Sheets - Google Chrome', 'process': 'chrome.exe'}
        self.under = 4242
        fake = type('U', (), {'WindowFromPoint': lambda s, p: 1, 'GetAncestor': lambda s, h, f: self.under})()
        self.patches = [patch.object(self.d, 'POINTER_FILE', self.file),
                        patch.object(self.d, '_ui_window', return_value=(win, None)),
                        patch.object(self.d, '_screen_point', return_value=((1530, 412), None)),
                        patch.object(self.d, 'user32', fake),
                        patch.object(self.d, '_window_title', return_value='Spotify'),
                        patch('confirm.widget_busy', return_value=True)]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def test_hands_the_spot_to_the_widget(self):
        out = self.d.point_at('Sheets', 900, 240, '  Insert\n menu  ')
        self.assertIn("Pointing at 'Insert menu'", out)
        p = json.loads(self.file.read_text(encoding='utf-8'))
        self.assertEqual((p['x'], p['y'], p['hwnd'], p['label']), (1530, 412, 4242, 'Insert menu'))

    def test_not_from_telegram(self):
        with patch('confirm.widget_busy', return_value=False):
            self.assertIn('only appears for a request from the widget', self.d.point_at('Sheets', 1, 1, 'x'))
        self.assertFalse(self.file.exists())

    def test_covered_spot(self):
        self.under = 7
        self.assertIn('Another window covers that point', self.d.point_at('Sheets', 1, 1, 'x'))
        self.assertFalse(self.file.exists())

    def test_needs_a_look_first(self):
        with patch.object(self.d, '_screen_point', return_value=(None, "Look at 'x' with screen_look first.")):
            self.assertIn('screen_look first', self.d.point_at('Sheets', 1, 1, 'x'))
        self.assertFalse(self.file.exists())


if __name__ == '__main__':
    unittest.main()
