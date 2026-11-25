"""main.py starts no bgutil-pot server while lookups ask for no PO tokens (fetch_pot=never).

It would only sit idle, and its "server started" line read as if bgutil were in use. Every import of
main.py, test runs included, also left one running on 127.0.0.1:4416.
"""
import contextlib
import importlib
import io
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class TestNoIdleBgutilServer(unittest.TestCase):

    def test_importing_main_starts_no_bgutil_server(self):
        # main.py's dependencies first: their first import can run other subprocesses (on
        # Windows, `platform` runs `ver`), which have nothing to do with bgutil.
        import audio_player, discord, track_queue  # noqa: E401,F401
        out = io.StringIO()
        # A binary is always "found", so the old code would have tried to start it.
        with patch("subprocess.Popen") as popen, \
                patch("shutil.which", return_value="/opt/bgutil-pot"), \
                contextlib.redirect_stdout(out):
            import main
            importlib.reload(main)
        launched = [c for c in popen.call_args_list if c.args and "bgutil" in str(c.args[0])]
        self.assertEqual(launched, [])
        self.assertIsNone(main._bgutil_proc)
        self.assertIn("bgutil-pot not started: lookups do not ask for PO tokens", out.getvalue())

    def test_lookups_still_ask_for_no_tokens(self):
        """The server stays off only as long as nothing would ask it for a token."""
        import audio_player
        args = audio_player._youtube_extractor_args("web_embedded", None)
        self.assertEqual(args["youtube"]["fetch_pot"], ["never"])


if __name__ == "__main__":
    unittest.main()
