"""tts.py text helpers: cleanup, the update's wording, the half-hour mark."""
import os, sys, unittest
from datetime import datetime
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import tts

RIGA = ZoneInfo("Europe/Riga")


def _ts(*args, tz=RIGA):
    return datetime(*args, tzinfo=tz).timestamp()


class TestCleanText(unittest.TestCase):
    def test_custom_emoji_become_their_name(self):
        self.assertEqual(tts.clean_text("hi <:wave:123> <a:party:456>", "en"), "hi wave party")

    def test_links_become_a_word(self):
        self.assertEqual(tts.clean_text("see https://x.y/z?q=1 now", "lv"), "see saite now")
        self.assertEqual(tts.clean_text("see http://x.y now", "en"), "see link now")

    def test_markdown_mentions_and_whitespace(self):
        self.assertEqual(tts.clean_text("**bold**  _it_ ~~s~~ `c` ||sp|| > q", "en"), "bold it s c sp q")
        self.assertEqual(tts.clean_text("@\u200beveryone @Jānis hi", "lv"), "everyone Jānis hi")

    def test_empty_after_cleanup(self):
        self.assertEqual(tts.clean_text("  ** __ ", "en"), "")


class TestAnnouncementText(unittest.TestCase):
    NOW = datetime(2026, 11, 25, 14, 34, tzinfo=RIGA)

    def test_latvian(self):
        for n, tail in ((6, "Rindā vēl 6 dziesmas."), (1, "Rindā vēl 1 dziesma."),
                        (21, "Rindā vēl 21 dziesma."), (11, "Rindā vēl 11 dziesmas."),
                        (0, "Rinda ir tukša.")):
            self.assertEqual(tts.announcement_text("lv", self.NOW, n), "Pulkstenis ir 14:34. " + tail)

    def test_english(self):
        self.assertEqual(tts.announcement_text("en", self.NOW, 6), "It's 14:34. 6 more songs in the queue.")
        self.assertEqual(tts.announcement_text("en", self.NOW, 1), "It's 14:34. 1 more song in the queue.")
        self.assertEqual(tts.announcement_text("en", self.NOW, 0), "It's 14:34. The queue is empty.")

    def test_time_is_zero_padded(self):
        early = datetime(2026, 1, 1, 9, 5, tzinfo=RIGA)
        self.assertTrue(tts.announcement_text("en", early, 0).startswith("It's 09:05."))


class TestHalfHourMark(unittest.TestCase):
    def test_mark_inside_the_window(self):
        self.assertTrue(tts.half_hour_mark_passed(_ts(2026, 11, 25, 14, 28), _ts(2026, 11, 25, 14, 33), "Europe/Riga"))

    def test_no_mark(self):
        self.assertFalse(tts.half_hour_mark_passed(_ts(2026, 11, 25, 14, 31), _ts(2026, 11, 25, 14, 59), "Europe/Riga"))

    def test_two_marks_count_once(self):
        self.assertTrue(tts.half_hour_mark_passed(_ts(2026, 11, 25, 13, 50), _ts(2026, 11, 25, 15, 5), "Europe/Riga"))

    def test_mark_exactly_at_start_does_not_count(self):
        self.assertFalse(tts.half_hour_mark_passed(_ts(2026, 11, 25, 14, 30), _ts(2026, 11, 25, 14, 45), "Europe/Riga"))

    def test_uses_the_servers_zone(self):
        # 14:20-14:40 UTC holds a :30 mark in UTC, but in Kathmandu (UTC+5:45) it is 20:05-20:25.
        utc = ZoneInfo("UTC")
        start, end = _ts(2026, 11, 25, 14, 20, tz=utc), _ts(2026, 11, 25, 14, 40, tz=utc)
        self.assertTrue(tts.half_hour_mark_passed(start, end, "UTC"))
        self.assertFalse(tts.half_hour_mark_passed(start, end, "Asia/Kathmandu"))

    def test_dst_change_day(self):
        # Riga leaves summer time on 2026-10-25 (04:00 -> 03:00); 02:50 -> 03:10 still holds 03:00.
        self.assertTrue(tts.half_hour_mark_passed(_ts(2026, 10, 25, 2, 50), _ts(2026, 10, 25, 3, 10), "Europe/Riga"))
