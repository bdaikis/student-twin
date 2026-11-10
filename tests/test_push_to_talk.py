"""The voice helper's push-to-talk hook with a mouse side button: a tap is re-sent from its own
thread, never from inside the hook, and a hook that can't get the lock lets the event through.
On 10 Nov 2026 a tap re-sent from inside the mouse hook deadlocked it (voice_helper.resend). No
real input is sent: send_keys is replaced."""
from pathlib import Path
import sys
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'widget' / 'helper'))
import voice_helper as vh

BACK = {'device': 'mouse', 'button': 1}


def button(msg):
    return msg, vh.MSLLHOOKSTRUCT(mouseData=1 << 16, flags=0)


class SideButton(unittest.TestCase):
    def setUp(self):
        self.ptt = vh.PushToTalk()
        self.ptt.configure(BACK, 300)
        while not vh._resends.empty():
            vh._resends.get_nowait()
        self.sent = []
        self.patches = [patch.object(vh, 'send_keys', side_effect=lambda *i: self.sent.append(i)),
                        patch.object(vh, 'modifier_down', return_value=False)]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        if self.ptt.timer:
            self.ptt.timer.cancel()

    def test_tap_is_queued_not_sent_from_the_hook(self):
        self.assertTrue(self.ptt.handle_mouse(*button(vh.WM_XBUTTONDOWN)))
        self.assertTrue(self.ptt.handle_mouse(*button(vh.WM_XBUTTONUP)))
        self.assertEqual(self.sent, [])  # nothing sent while the hook was running
        inputs = vh._resends.get_nowait()
        self.assertEqual([i.u.mi.dwFlags for i in inputs], [vh.MOUSEEVENTF_XDOWN, vh.MOUSEEVENTF_XUP])
        self.assertFalse(self.ptt.lock.locked())

    def test_hold_starts_talking_and_sends_nothing(self):
        self.ptt.handle_mouse(*button(vh.WM_XBUTTONDOWN))
        time.sleep(0.4)
        self.assertEqual(self.ptt.state, vh.PushToTalk.HOLDING)
        self.assertTrue(self.ptt.handle_mouse(*button(vh.WM_XBUTTONUP)))
        self.assertTrue(vh._resends.empty())

    def test_busy_lock_lets_the_event_through(self):
        holder = threading.Thread(target=lambda: (self.ptt.lock.acquire(), time.sleep(1), self.ptt.lock.release()))
        holder.start()
        time.sleep(0.05)
        t = time.perf_counter()
        self.assertFalse(self.ptt.handle_mouse(*button(vh.WM_XBUTTONDOWN)))
        self.assertLess(time.perf_counter() - t, 0.5)
        holder.join()

    def test_resender_sends_in_order(self):
        jobs = vh.queue.Queue()  # its own queue: the thread outlives the test
        threading.Thread(target=vh.run_resends, args=(jobs,), daemon=True).start()
        jobs.put(('a', 'b'))
        jobs.put(('c',))
        for _ in range(50):
            if len(self.sent) == 2:
                break
            time.sleep(0.01)
        self.assertEqual(self.sent, [('a', 'b'), ('c',)])


if __name__ == '__main__':
    unittest.main()
