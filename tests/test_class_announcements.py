"""Which day an announcement is about, and what class reminders and the digest say with it. The
posts are real ones from SPeCTRUM (Oct-Nov 2026); nothing is read or sent."""
from datetime import date, datetime
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'mcp_servers'))
from common import class_announcements, dates_in, post_about_day  # noqa: E402
from timetable import LOCAL, ClassBlock  # noqa: E402

MEET = ('<p>Hi. Reflected from the poll, majority asks for an online class. So, tomorrow will be an online '
        'class ya</p><p>Google meet link is similar to last time: meet.google.com/fdy-wkam-vzp</p>')
POSTS = {
    'online_tue': (datetime(2026, 11, 9, 17, 36, tzinfo=LOCAL), 'SIF1017',
                   'Tuesday, 10/11/2026 is an online class ya', MEET),
    'poll': (datetime(2026, 11, 5, 16, 0, tzinfo=LOCAL), 'SIF1017', "Polling for Tuesday's Class is up!",
             'Polling for Tuesday Class (Online/Offline) is up in the general section. Please fill this in '
             'before Monday 5pm.'),
    'tomorrow': (datetime(2026, 11, 5, 13, 20, tzinfo=LOCAL), 'GUL1001',
                 'Online class for OCC 6', "Dear all, Tomorrow's class shall be conducted online."),
    'voov': (datetime(2026, 11, 6, 8, 7, tzinfo=LOCAL), 'GUL1001', 'Link to online Lecture_6 Nov 2026',
             'Time: 2026/11/06 08:06-09:06 (GMT+08:00) Click the link to join: https://voovmeeting.com/dm/LkOT9R7CNjFp'),
    'this_week': (datetime(2026, 10, 26, 23, 38, tzinfo=LOCAL), 'SIF1017',
                  'This week will be a face-to-face class ya!', 'As the title suggest See you at the class!'),
    'groups': (datetime(2026, 10, 30, 15, 34, tzinfo=LOCAL), 'SIF1014', 'Formation of Groups for Group Presentation',
               'You are required to form groups for the upcoming group presentation. 88 students, 10 groups.'),
}


def about(name: str, day: date) -> bool:
    posted, _, subject, text = POSTS[name]
    return post_about_day(f'{subject} {text}', posted, day)


def as_posts(*names):
    return [(int(POSTS[n][0].timestamp()), POSTS[n][1], 'Announcements',
             {'subject': POSTS[n][2], 'message': POSTS[n][3], 'userfullname': 'DR. IBNU SYAFIQ IMADUDDIN'})
            for n in sorted(names, key=lambda n: -POSTS[n][0].timestamp())]


def block(code, start_hour, day, location='DKFIZIK', activity='LECTURE'):
    return ClassBlock(code, 'MATHEMATICAL METHODS I', '1', datetime(*day, start_hour, tzinfo=LOCAL),
                      datetime(*day, start_hour + 1, tzinfo=LOCAL), location, activity, '')


class WhichDay(unittest.TestCase):
    def test_written_dates_decide(self):
        self.assertTrue(about('online_tue', date(2026, 11, 10)))
        self.assertFalse(about('online_tue', date(2026, 11, 12)))
        self.assertTrue(about('voov', date(2026, 11, 6)))

    def test_tomorrow_counts_from_the_post(self):
        self.assertTrue(about('tomorrow', date(2026, 11, 6)))
        self.assertFalse(about('tomorrow', date(2026, 11, 5)))

    def test_weekday_within_the_week_after(self):
        self.assertTrue(about('poll', date(2026, 11, 10)))
        self.assertFalse(about('poll', date(2026, 11, 17)))
        self.assertFalse(about('poll', date(2026, 11, 5)))  # posted that Thursday, about Tuesday

    def test_this_week(self):
        self.assertTrue(about('this_week', date(2026, 10, 29)))
        self.assertFalse(about('this_week', date(2026, 11, 3)))

    def test_unrelated_post_is_about_no_class(self):
        self.assertFalse(about('groups', date(2026, 10, 30)))
        self.assertFalse(about('online_tue', date(2026, 11, 8)))  # before the post

    def test_times_and_fractions_are_not_dates(self):
        posted = date(2026, 11, 7)
        self.assertEqual(dates_in('Class on Tuesday 3.30pm, room 10-11am, solve Q3/4', posted), set())
        self.assertEqual(dates_in('Thursday, November 5 · 2:00 – 4:00pm', date(2026, 11, 4)), {date(2026, 11, 5)})
        self.assertEqual(dates_in('Test on 8/1', date(2026, 12, 20)), {date(2027, 1, 8)})


class ReminderText(unittest.TestCase):
    def test_online_announcement_replaces_the_room(self):
        where, notes = class_announcements(block('SIF1017', 15, (2026, 11, 10)), as_posts('online_tue', 'poll'))
        self.assertEqual(where, 'online, per the announcement below (the timetable says DKFIZIK)')
        self.assertEqual(len(notes), 2)
        self.assertIn('Mon 09 Nov 17:36', notes[0])
        self.assertIn('meet.google.com/fdy-wkam-vzp', notes[0])
        self.assertEqual(notes[1], "Also: Thu 05 Nov 16:00, Polling for Tuesday's Class is up!")

    def test_newer_post_without_a_mode_does_not_hide_the_online_one(self):
        POSTS['calculators'] = (datetime(2026, 11, 10, 8, 0, tzinfo=LOCAL), 'SIF1017',
                                "Today's class", '<p>Bring your calculators and form teams of 4.</p>')
        try:
            where, notes = class_announcements(block('SIF1017', 15, (2026, 11, 10)),
                                               as_posts('online_tue', 'calculators'))
        finally:
            del POSTS['calculators']
        self.assertTrue(where.startswith('online, per the announcement below'))
        self.assertIn('10/11/2026 is an online class', notes[0])
        self.assertEqual(notes[1], "Also: Tue 10 Nov 08:00, Today's class")

    def test_link_kept_when_text_is_cut(self):
        long = as_posts('online_tue')
        long[0][3]['message'] = '<p>' + 'x ' * 200 + '</p>' + MEET
        _, notes = class_announcements(block('SIF1017', 15, (2026, 11, 10)), long)
        self.assertIn('Link: meet.google.com/fdy-wkam-vzp', notes[0])

    def test_poll_alone_does_not_say_online(self):
        where, notes = class_announcements(block('SIF1017', 15, (2026, 11, 10)), as_posts('poll'))
        self.assertEqual(where, 'DKFIZIK')
        self.assertEqual(len(notes), 1)

    def test_face_to_face_week_and_other_courses(self):
        where, notes = class_announcements(block('SIF1017', 14, (2026, 10, 29)), as_posts('this_week', 'tomorrow'))
        self.assertEqual((where, len(notes)), ('DKFIZIK', 1))
        self.assertEqual(class_announcements(block('SIF1014', 9, (2026, 11, 10)), as_posts('online_tue')),
                         ('DKFIZIK', []))


class CheckInCourse(unittest.TestCase):
    """mark_attendance with the course the twin guessed: on 10 Nov it sent SIF1017 for a "here"
    meant for SIF1014. The tool now names the class that is running instead of only refusing."""

    def test_wrong_course_names_the_running_class(self):
        try:
            import portal
        except ImportError:  # the mcp package lives in the twin's venv
            self.skipTest('mcp not installed')
        courses = {'SIF1017': {'name': 'Mathematical Methods I'}, 'SIF1014': {'name': 'Vibrations and Waves'}}
        running = [block('SIF1014', 9, (2026, 11, 10))]
        with patch('common.config', return_value={'courses': courses}), \
                patch('portal.timetable.in_progress', return_value=running):
            reply = portal.mark_attendance('SIF1017')
        self.assertIn("SIF1017 isn't running now", reply)
        self.assertIn('course "SIF1014"', reply)


if __name__ == '__main__':
    unittest.main()
