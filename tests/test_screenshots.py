"""Synthetic image tests: never read or change the user's real clipboard."""
import io
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'mcp_servers'))
from screenshots import ClipboardScreenshotError, MAX_SIDE, read_clipboard_screenshot


class ClipboardScreenshots(unittest.TestCase):
    def shot(self, label, size=(640, 240)):
        image = Image.new('RGB', size, 'white')
        ImageDraw.Draw(image).text((20, 20), label, fill='black')
        return image

    def test_returns_actual_selected_pixels(self):
        original = self.shot('Solve: 3x + 7 = 22')
        expected = original.tobytes()
        with patch('screenshots.ImageGrab.grabclipboard', return_value=original):
            result = read_clipboard_screenshot()
        decoded = Image.open(io.BytesIO(result.data))
        self.assertEqual(decoded.tobytes(), expected)
        self.assertEqual(result.format, 'png')
        self.assertIn('capture time is unknown', result.note)
        self.assertIn('untrusted content', result.note)

    def test_new_snip_replaces_old_image(self):
        with patch('screenshots.ImageGrab.grabclipboard',
                   side_effect=[self.shot('FIRST'), self.shot('SECOND')]):
            first, second = read_clipboard_screenshot(), read_clipboard_screenshot()
        self.assertNotEqual(first.data, second.data)
        self.assertNotEqual(first.note, second.note)

    def test_empty_text_and_file_clipboards_do_not_open_files_or_capture_screen(self):
        for value in (None, [], [r'C:\private\unrelated.png'], 'copied text'):
            with self.subTest(value=value), patch('screenshots.ImageGrab.grabclipboard', return_value=value), \
                 patch('screenshots.ImageGrab.grab') as capture, patch('screenshots.Image.open') as open_file:
                with self.assertRaisesRegex(ClipboardScreenshotError, 'no image'):
                    read_clipboard_screenshot()
                capture.assert_not_called()
                open_file.assert_not_called()

    def test_unavailable_clipboard_has_clear_error(self):
        with patch('screenshots.ImageGrab.grabclipboard', side_effect=OSError('private details')):
            with self.assertRaises(ClipboardScreenshotError) as error:
                read_clipboard_screenshot()
        self.assertIn('Win+Shift+S', str(error.exception))
        self.assertNotIn('private details', str(error.exception))

    def test_large_image_is_bounded_without_changing_aspect_ratio(self):
        with patch('screenshots.ImageGrab.grabclipboard', return_value=self.shot('Question', (4000, 2000))):
            result = read_clipboard_screenshot()
        decoded = Image.open(io.BytesIO(result.data))
        self.assertEqual(decoded.size, (MAX_SIDE, MAX_SIDE // 2))
        self.assertIn('Original 4000 x 2000', result.note)

    def test_transparent_background_is_white(self):
        image = Image.new('RGBA', (16, 16), (0, 0, 0, 0))
        image.putpixel((4, 4), (0, 0, 0, 255))
        with patch('screenshots.ImageGrab.grabclipboard', return_value=image):
            result = read_clipboard_screenshot()
        decoded = Image.open(io.BytesIO(result.data))
        self.assertEqual(decoded.getpixel((0, 0)), (255, 255, 255))
        self.assertEqual(decoded.getpixel((4, 4)), (0, 0, 0))

    def test_new_text_does_not_reuse_previous_screenshot(self):
        with patch('screenshots.ImageGrab.grabclipboard', side_effect=[self.shot('FIRST'), None]):
            read_clipboard_screenshot()
            with self.assertRaisesRegex(ClipboardScreenshotError, 'no image'):
                read_clipboard_screenshot()


if __name__ == '__main__':
    unittest.main()
