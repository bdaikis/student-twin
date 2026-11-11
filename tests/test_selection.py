"""The highlighted text (2026-11-11): which windows the voice helper reads it in, how long a read is
kept and for which window, that push-to-talk starts a read, and that keys typed while the typing
card waits for one are held back and re-sent in order. Nothing is copied or typed: no read here
reaches the clipboard, and resend is replaced. The real copy was checked live in Chrome, Acrobat's
Chrome viewer and VS Code (Ctrl+Insert; the clipboard came back unchanged)."""
import os
from pathlib import Path
import sys
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'widget' / 'helper'))
import voice_helper as vh  # noqa: E402


def window(title):
    """A real (message-only, never visible) window of this process with this title."""
    return vh.user32.CreateWindowExW(0, 'STATIC', title, 0, 0, 0, 0, 0, vh.HWND_MESSAGE, None, None, None)


def kb(vk, scan, injected=False):
    return vh.KBDLLHOOKSTRUCT(vkCode=vk, scanCode=scan, flags=vh.LLKHF_INJECTED if injected else 0)


class Skip(unittest.TestCase):
    def setUp(self):
        self.hwnd = window('Lecture 7 notes - Google Chrome')
        self.patches = [patch.object(vh, 'modifier_down', return_value=False), patch.object(vh, '_fullscreen', False),
                        patch.object(vh, 'WIDGET_PID', 0)]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        vh.user32.DestroyWindow(self.hwnd)

    def test_an_ordinary_window_is_read(self):
        self.assertIsNone(vh.selection_skip(self.hwnd))

    def test_terminals_never(self):
        for exe in ('WindowsTerminal.exe', 'powershell.exe', 'cmd.exe', 'mintty.exe'):
            with patch.object(vh, '_process', return_value=exe):
                self.assertEqual(vh.selection_skip(self.hwnd), 'terminal', exe)

    def test_messaging_never(self):
        hwnd = window('Inbox (3) - Gmail')
        try:
            self.assertEqual(vh.selection_skip(hwnd), 'messaging')
        finally:
            vh.user32.DestroyWindow(hwnd)

    def test_the_widget_itself(self):
        with patch.object(vh, 'WIDGET_PID', os.getpid()):
            self.assertEqual(vh.selection_skip(self.hwnd), 'widget')

    def test_not_while_a_modifier_is_held(self):
        with patch.object(vh, 'modifier_down', return_value=True):
            self.assertEqual(vh.selection_skip(self.hwnd), 'modifier')  # Shift+Insert would paste

    def test_not_in_full_screen(self):
        with patch.object(vh, '_fullscreen', True):
            self.assertEqual(vh.selection_skip(self.hwnd), 'fullscreen')

    def test_not_while_a_snip_is_due(self):
        vh.snip.watch.set()
        try:
            self.assertEqual(vh.selection_skip(self.hwnd), 'snip')
        finally:
            vh.snip.watch.clear()

    def test_no_window(self):
        self.assertEqual(vh.selection_skip(None), 'none')


class Kept(unittest.TestCase):
    def setUp(self):
        self.sel = vh.Selection()

    def test_for_the_window_it_was_read_in(self):
        self.sel.keep(101, 'F = ma')
        self.assertEqual(self.sel.current(101), 'F = ma')
        self.assertEqual(self.sel.current(202), '')  # he moved to another window
        self.assertEqual(self.sel.current(None), '')

    def test_not_after_keep_s(self):
        self.sel.keep(101, 'F = ma')
        self.sel.at -= vh.Selection.KEEP_S + 1
        self.assertEqual(self.sel.current(101), '')

    def test_widget_in_front_keeps_what_was_read(self):
        self.sel.keep(101, 'F = ma')
        with patch.object(vh, 'selection_skip', return_value='widget'):
            self.assertEqual(self.sel.read(1.0, lambda n: None), 'widget')
        self.assertEqual(self.sel.current(101), 'F = ma')

    def test_another_skip_clears_it(self):
        self.sel.keep(101, 'F = ma')
        with patch.object(vh, 'selection_skip', return_value='terminal'), \
                patch.object(vh.user32, 'GetForegroundWindow', return_value=101):
            self.sel.read(1.0, lambda n: None)
        self.assertEqual(self.sel.current(101), '')

    def test_switched_off_replies_at_once(self):
        self.sel.enabled = False
        with patch.object(vh, 'emit') as emit:
            self.sel.request(0.25, 'r1')
        emit.assert_called_once_with(event='selection', id='r1', chars=0)
        self.assertTrue(self.sel.wait(0))  # nothing pending

    def test_one_reply_per_request_and_wait_settles(self):
        sel = vh.Selection()
        replies = []
        with patch.object(vh, 'emit', side_effect=lambda **e: replies.append(e)), \
                patch.object(sel, 'read', side_effect=lambda wait_s, reply: (reply(12), reply(0))):
            import threading
            threading.Thread(target=sel.run, daemon=True).start()
            sel.request(0.25, 'r2')
            self.assertTrue(sel.wait(2))
        self.assertEqual([e for e in replies if e['event'] == 'selection'], [{'event': 'selection', 'id': 'r2', 'chars': 12}])


class VSCodeLine(unittest.TestCase):
    def test_empty_selection_copy_is_recognised(self):
        blob = b'\x10\x00\x00\x00' + '{"version":1,"isFromEmptySelection":true,"multicursorText":null}'.encode('utf-16-le')
        self.assertTrue(vh.empty_line_copy(blob))
        self.assertFalse(vh.empty_line_copy(blob.replace('true'.encode('utf-16-le'), 'false'.encode('utf-16-le'))))
        self.assertFalse(vh.empty_line_copy(b''))


class PushToTalkReads(unittest.TestCase):
    def test_hold_starts_a_read(self):
        ptt = vh.PushToTalk()
        ptt.state = vh.PushToTalk.PENDING
        with patch.object(vh.selection, 'request') as request, patch.object(vh, 'emit'):
            ptt._threshold()
        request.assert_called_once_with(vh.Selection.WAIT_VOICE_S)
        self.assertFalse(ptt.lock.locked())  # the read is queued after the lock is let go

    def test_a_tap_does_not(self):
        ptt = vh.PushToTalk()
        ptt.state = vh.PushToTalk.IDLE
        with patch.object(vh.selection, 'request') as request:
            ptt._threshold()
        request.assert_not_called()


class HeldKeys(unittest.TestCase):
    def setUp(self):
        self.hold = vh.KeyHold()
        self.sent = []
        self.resend = patch.object(vh, 'resend', side_effect=lambda *i: self.sent.append(i))
        self.resend.start()

    def tearDown(self):
        self.resend.stop()
        self.hold.release(replay=False)

    def test_nothing_held_when_not_holding(self):
        self.assertFalse(self.hold.key(vh.WM_KEYDOWN, kb(0x41, 0x1E)))

    def test_typed_keys_are_held_and_resent_in_order(self):
        self.hold.start()
        for msg, k in ((vh.WM_KEYDOWN, kb(0x48, 0x23)), (vh.WM_KEYUP, kb(0x48, 0x23)),
                       (vh.WM_KEYDOWN, kb(0x49, 0x17)), (vh.WM_KEYUP, kb(0x49, 0x17))):
            self.assertTrue(self.hold.key(msg, k))
        self.hold.release()
        (inputs,) = self.sent
        self.assertEqual([(i.u.ki.wVk, bool(i.u.ki.dwFlags & vh.KEYEVENTF_KEYUP)) for i in inputs],
                         [(0x48, False), (0x48, True), (0x49, False), (0x49, True)])

    def test_release_of_a_key_pressed_before_goes_through(self):
        self.hold.start()
        self.assertFalse(self.hold.key(vh.WM_KEYUP, kb(0x10, 0x2A)))  # Shift held from before

    def test_injected_keys_go_through(self):
        self.hold.start()
        self.assertFalse(self.hold.key(vh.WM_KEYDOWN, kb(0x2D, 0x52, injected=True)))  # the Ctrl+Insert itself

    def test_dropped_when_the_widget_never_takes_the_keyboard(self):
        self.hold.start()
        self.hold.key(vh.WM_KEYDOWN, kb(0x41, 0x1E))
        with patch.object(vh, 'emit'):
            self.hold.release(replay=False)
        self.assertEqual(self.sent, [])
        self.assertFalse(self.hold.key(vh.WM_KEYDOWN, kb(0x41, 0x1E)))  # holding has ended

    def test_holding_ends_on_its_own(self):
        with patch.object(vh.KeyHold, 'LIMIT_S', 0.05), patch.object(vh, 'emit'):
            self.hold.start()
            time.sleep(0.2)
        self.assertFalse(self.hold.active)


if __name__ == '__main__':
    unittest.main()
