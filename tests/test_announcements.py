"""Announcement forums: which forums count, and the watcher's first read of the named ones. A fake
portal stands in for SPeCTRUM, so nothing is read or sent."""
from pathlib import Path
import sqlite3
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'mcp_servers'))
sys.path.insert(0, str(ROOT / 'scripts'))
import deadline_watch  # noqa: E402
from common import is_announcement_forum, match_course, post_text  # noqa: E402

FORUMS = [
    {'id': 1, 'course': 10, 'type': 'news', 'name': 'Announcements'},
    {'id': 2, 'course': 20, 'type': 'general', 'name': 'Announcements (PLEASE READ)'},
    {'id': 3, 'course': 20, 'type': 'general', 'name': "Please Post Your Group's Answer Here"},
]


class FakePortal:
    def __init__(self):
        self.posts = {1: [{'discussion': 101, 'subject': 'Old news', 'message': '<p>old</p>'}],
                      2: [{'discussion': 201, 'subject': 'Online class', 'message': '<p>Tue is online</p>'}],
                      3: [{'discussion': 301, 'subject': 'Group 9, Q3', 'message': ''}]}

    def call(self, function, **params):
        if function == 'mod_forum_get_forums_by_courses':
            return FORUMS
        return {'discussions': self.posts[params['forumid']]}


class AnnouncementForums(unittest.TestCase):
    def test_news_and_named_forums_count_others_do_not(self):
        self.assertEqual([f['id'] for f in FORUMS if is_announcement_forum(f)], [1, 2])
        self.assertTrue(is_announcement_forum({'type': 'general', 'name': 'Pengumuman kursus'}))

    def test_post_text_is_plain(self):
        self.assertEqual(post_text('<p>Class is <b>online</b> &amp; at 3pm</p>\n'), 'Class is online & at 3pm')

    def test_maths_matches_mathematical_methods(self):
        courses = {'SIF1017': {'name': 'Mathematical Methods I'}, 'SIF1003': {'name': 'Thermal Physics'}}
        with patch('common.config', return_value={'courses': courses}):
            self.assertEqual(match_course('maths')[0], 'SIF1017')
            self.assertEqual(match_course('thermal')[0], 'SIF1003')


class WatcherFirstRead(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.db.execute('CREATE TABLE meta (name TEXT PRIMARY KEY, value TEXT)')
        self.portal = FakePortal()
        self.patches = [patch('deadline_watch.moodle', return_value=self.portal),
                        patch('deadline_watch.dl._courses', return_value={10: 'SIF1014', 20: 'SIF1017'})]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def test_existing_posts_in_named_forums_are_not_announced(self):
        self.db.execute("INSERT INTO meta VALUES ('announcements:seen', '101')")
        self.assertEqual(deadline_watch.announcement_messages(self.db), [])

    def test_news_post_after_the_change_is_still_announced(self):
        self.db.execute("INSERT INTO meta VALUES ('announcements:seen', '')")
        self.assertEqual(deadline_watch.announcement_messages(self.db), ['New announcement in SIF1014: Old news — old'])

    def test_new_post_in_named_forum_is_announced_after_the_first_read(self):
        self.db.execute("INSERT INTO meta VALUES ('announcements:seen', '101')")
        deadline_watch.announcement_messages(self.db)
        self.portal.posts[2].append({'discussion': 202, 'subject': 'Test 1 moved', 'message': '<p>to Friday</p>'})
        self.assertEqual(deadline_watch.announcement_messages(self.db),
                         ['New announcement in SIF1017: Test 1 moved — to Friday'])


if __name__ == '__main__':
    unittest.main()
