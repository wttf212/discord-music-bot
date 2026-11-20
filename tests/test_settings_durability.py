"""Tests that guild settings survive crashes and concurrent writes.

save_settings() used to open(w) — truncate first, write second — so a crash or an
overlapping write mid-dump left a truncated file and every guild lost its allowed
channel, bitrate, EQ and admin list at once.
"""
import importlib
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class SettingsTestCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "guild_settings.json")
        os.environ["GUILD_SETTINGS_FILE"] = self.path
        import guild_settings
        self.gs = importlib.reload(guild_settings)

    def tearDown(self):
        os.environ.pop("GUILD_SETTINGS_FILE", None)
        import guild_settings
        importlib.reload(guild_settings)


class TestAtomicWrite(SettingsTestCase):

    def test_round_trip(self):
        self.gs.save_settings({"1": {"bitrate": 256}})
        self.assertEqual(self.gs.load_settings(), {"1": {"bitrate": 256}})

    def test_previous_contents_survive_a_failed_write(self):
        """The old file must remain intact if serialisation blows up midway."""
        self.gs.save_settings({"1": {"bitrate": 128}})

        class Unserialisable:
            pass

        with self.assertRaises(TypeError):
            self.gs.save_settings({"1": {"bitrate": Unserialisable()}})
        self.assertEqual(self.gs.load_settings(), {"1": {"bitrate": 128}},
                         "a failed write must not destroy the previous settings")

    def test_no_temp_files_left_behind(self):
        self.gs.save_settings({"1": {"bitrate": 128}})
        try:
            self.gs.save_settings({"1": {"x": object()}})
        except TypeError:
            pass
        leftovers = [f for f in os.listdir(self.dir) if f.startswith(".guild_settings.")]
        self.assertEqual(leftovers, [], f"temp files leaked: {leftovers}")

    def test_file_is_never_observed_truncated(self):
        """Readers only ever see the old contents or the new — never a partial file."""
        self.gs.save_settings({"1": {"bitrate": 128}})
        seen = []
        stop = threading.Event()

        def reader():
            while not stop.is_set():
                try:
                    with open(self.path, "r", encoding="utf-8") as f:
                        seen.append(json.load(f))
                except FileNotFoundError:
                    seen.append("MISSING")
                except json.JSONDecodeError:
                    seen.append("TRUNCATED")
                except PermissionError:
                    # Windows only, and benign: an EXTERNAL reader can momentarily be
                    # locked out while os.replace swaps the file in. It never sees a
                    # partial file, which is the property that matters. In-process
                    # readers take the same lock as the writer and never hit this.
                    seen.append("LOCKED")
                time.sleep(0.001)   # an external reader, not a pathological spin loop

        t = threading.Thread(target=reader, daemon=True)
        t.start()
        for i in range(200):
            self.gs.save_settings({"1": {"bitrate": 128 + i, "pad": "x" * 4000}})
        stop.set()
        t.join(5)
        self.assertNotIn("TRUNCATED", seen)
        self.assertNotIn("MISSING", seen)
        self.assertTrue(seen, "reader never observed the file")

    def test_concurrent_writers_do_not_interleave(self):
        """Two guilds saving at once must both end up in the file."""
        self.gs.save_settings({})

        def writer(guild_id):
            for i in range(40):
                self.gs.set_bitrate(str(guild_id), 100 + i)

        threads = [threading.Thread(target=writer, args=(g,)) for g in (1, 2, 3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)
        final = self.gs.load_settings()
        for g in ("1", "2", "3"):
            self.assertIn(g, final, f"guild {g} lost its settings to a racing write")
            self.assertEqual(final[g]["bitrate"], 139)


class TestCorruptFileTolerance(SettingsTestCase):

    def test_corrupt_file_reads_as_empty_instead_of_crashing(self):
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("{not json")
        self.assertEqual(self.gs.load_settings(), {})

    def test_a_setter_never_overwrites_a_corrupt_file(self):
        """This used to rewrite the file from {}: the setter worked, and every other
        guild's settings were destroyed. A file that won't parse may be half-way
        through a hand edit, or one brace short of fine, so it is left for a human.
        Reads keep falling back to defaults meanwhile (the test above)."""
        corrupt = b'{"1": {"bitrate": 128}, "2": {"allowed_channel": "55"'
        with open(self.path, "wb") as f:
            f.write(corrupt)
        with self.assertRaises(ValueError):   # json.JSONDecodeError
            self.gs.set_bitrate("1", 256)
        with open(self.path, "rb") as f:
            self.assertEqual(f.read(), corrupt)


class TestFailedReadNeverWipesTheFile(SettingsTestCase):
    """load_settings() reads an unreadable file as {}, and setters used to save back
    whatever it returned. One failed read let the next setter, in any guild, replace
    the whole file with that one guild's entry."""

    BEFORE = {"1": {"bitrate": 128}, "2": {"allowed_channel": "55", "admins": ["7"]}}

    def test_a_failed_read_does_not_wipe_the_other_guilds(self):
        """Transient, e.g. another process holding the file open on Windows."""
        self.gs.save_settings(self.BEFORE)
        real_open = open
        failures = [PermissionError(13, "The process cannot access the file")]

        def flaky_open(path, mode="r", *args, **kwargs):
            if "r" in mode and failures:
                raise failures.pop()
            return real_open(path, mode, *args, **kwargs)

        with patch.object(self.gs, "open", flaky_open, create=True):
            with self.assertRaises(OSError):
                self.gs.set_bitrate("1", 256)
        self.assertEqual(self.gs.load_settings(), self.BEFORE)
        self.gs.set_bitrate("1", 256)   # the file reads again: the change lands normally
        self.assertEqual(self.gs.load_settings(), {**self.BEFORE, "1": {"bitrate": 256}})

    def test_a_missing_file_still_starts_empty(self):
        self.assertFalse(os.path.exists(self.path))
        self.gs.set_bitrate("1", 256)
        self.assertEqual(self.gs.load_settings(), {"1": {"bitrate": 256}})


if __name__ == "__main__":
    unittest.main(verbosity=2)
