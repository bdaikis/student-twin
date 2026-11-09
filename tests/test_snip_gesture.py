"""The voice helper's snip gesture (Ctrl held alone + pointer movement) and its Win+Shift+S watch,
with Windows' key, cursor and clipboard state faked: no real input, clipboard or screen is used."""
from pathlib import Path
import queue
import sys
import threading
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'widget' / 'helper'))
import voice_helper as vh

CTRL, C_KEY = vh.VK_LCONTROL, 0x43


class FakeUser32:
    """The real user32 with the state the gesture reads replaced."""

    def __init__(self):
        self.real = vh.user32
        self.held = set()
        self.cursor_flags = vh.CURSOR_SHOWING
        self.seq = 100
        self.formats = set()

    def __getattr__(self, name):
        return getattr(self.real, name)

    def GetAsyncKeyState(self, vk):
        return -32768 if vk in self.held else 0

    def GetCursorInfo(self, ref):
        ref._obj.flags = self.cursor_flags
        return 1

    def GetClipboardSequenceNumber(self):
        return self.seq

    def IsClipboardFormatAvailable(self, fmt):
        return fmt in self.formats


def kb(vk):
    return vh.KBDLLHOOKSTRUCT(vkCode=vk)


class SnipGestureTests(unittest.TestCase):
    def setUp(self):
        self.fake = FakeUser32()
        vh.user32 = self.fake
        vh._fullscreen = False
        vh._out = queue.Queue()
        self.snip = vh.SnipGesture()

    def tearDown(self):
        vh.user32 = self.fake.real

    def events(self):
        out = []
        while not vh._out.empty():
            out.append(vh._out.get())
        return out

    def phases(self):
        return [e['phase'] for e in self.events() if e['event'] == 'snip']

    def press(self, vk):
        self.snip.key(vh.WM_KEYDOWN, kb(vk))

    def release(self, vk):
        self.snip.key(vh.WM_KEYUP, kb(vk))

    def test_ctrl_alone_arms_then_releases(self):
        self.press(CTRL)
        self.release(CTRL)
        self.assertEqual(self.phases(), ['arm', 'release'])

    def test_key_repeat_arms_once(self):
        for _ in range(5):
            self.press(CTRL)
        self.release(CTRL)
        self.assertEqual(self.phases(), ['arm', 'release'])

    def test_shortcut_cancels_and_release_is_silent(self):
        self.press(CTRL)
        self.press(C_KEY)  # Ctrl+C
        self.release(C_KEY)
        self.release(CTRL)
        self.assertEqual(self.phases(), ['arm', 'cancel'])

    def test_click_or_wheel_cancels(self):
        self.press(CTRL)
        self.snip.mouse()  # Ctrl+click, Ctrl+scroll
        self.release(CTRL)
        self.assertEqual(self.phases(), ['arm', 'cancel'])

    def test_other_modifier_first_never_arms(self):
        self.fake.held.add(vh.VK_SHIFT)
        self.press(CTRL)
        self.release(CTRL)
        self.fake.held.clear()
        self.assertEqual(self.phases(), [])
        self.press(CTRL)  # the next plain Ctrl still works
        self.assertEqual(self.phases(), ['arm'])

    def test_disabled_full_screen_or_hidden_pointer_never_arms(self):
        self.snip.enabled = False
        self.press(CTRL); self.release(CTRL)
        self.snip.enabled = True
        vh._fullscreen = True
        self.press(CTRL); self.release(CTRL)
        vh._fullscreen = False
        self.fake.cursor_flags = 0  # a game hides the pointer
        self.press(CTRL); self.release(CTRL)
        self.assertEqual(self.phases(), [])

    def test_watchdog_recovers_a_lost_release_after_two_looks(self):
        self.press(CTRL)
        self.events()
        self.snip.watchdog_look()  # Ctrl isn't held: one look is not enough
        self.assertEqual(self.snip.state, vh.SnipGesture.ARMED)
        self.snip.watchdog_look()
        self.assertEqual(self.snip.state, vh.SnipGesture.IDLE)
        self.assertEqual(self.phases(), ['cancel'])

    def test_watchdog_leaves_a_held_ctrl_alone(self):
        self.press(CTRL)
        self.fake.held.add(vh.VK_CONTROL)
        for _ in range(4):
            self.snip.watchdog_look()
        self.assertEqual(self.snip.state, vh.SnipGesture.ARMED)

    def win_shift_s(self):
        self.fake.held.update({vh.VK_LWIN, vh.VK_SHIFT})
        self.press(vh.VK_S)
        self.fake.held.clear()

    def watch_for(self, seconds):
        thread = threading.Thread(target=self.snip.clipboard_watch, daemon=True)
        thread.start()
        time.sleep(seconds)
        return [e for e in self.events() if e['event'] == 'snip_ready']

    def test_win_shift_s_image_is_reported_once(self):
        self.win_shift_s()
        self.fake.seq += 1
        self.fake.formats = {8}  # CF_DIB
        self.assertEqual(self.watch_for(1.2), [{'event': 'snip_ready', 'source': 'snipping'}])
        self.assertFalse(self.snip.watch.is_set())

    def test_win_shift_s_then_text_is_not_a_snip(self):
        self.win_shift_s()
        self.fake.seq += 1
        self.fake.formats = {13}  # CF_UNICODETEXT
        self.assertEqual(self.watch_for(1.2), [])

    def test_clipboard_image_without_win_shift_s_is_ignored(self):
        self.fake.seq += 1
        self.fake.formats = {8}
        self.assertEqual(self.watch_for(0.8), [])

    def test_plain_s_does_not_start_a_watch(self):
        self.press(vh.VK_S)
        self.assertFalse(self.snip.watch.is_set())


class CutOutTests(unittest.TestCase):
    """The freeform trace keeps the inside of the shape and whitens the rest; synthetic images."""

    def test_inside_kept_outside_white(self):
        from PIL import Image
        image = Image.new('RGB', (100, 100), (200, 0, 0))
        # A triangle traced on screen at offset (500, 300); the image is its bounding box.
        points = [(500, 300), (599, 300), (500, 399)]
        out = vh.cut_out(image, points, 500, 300)
        self.assertEqual(out.getpixel((10, 10)), (200, 0, 0))  # inside, near the right angle
        self.assertEqual(out.getpixel((90, 90)), (255, 255, 255))  # beyond the closing line
        self.assertEqual(out.size, (100, 100))

    def test_open_trace_is_closed_back_to_its_start(self):
        from PIL import Image
        image = Image.new('RGB', (60, 60), (0, 0, 200))
        # Three sides of a square; the fourth comes from closing the shape.
        out = vh.cut_out(image, [(0, 0), (59, 0), (59, 59), (0, 59)], 0, 0)
        self.assertEqual(out.getpixel((30, 30)), (0, 0, 200))


if __name__ == '__main__':
    unittest.main()
