"""A combined-format (format 18) resolve is retried with the bgutil CLI forced only when the
result carried a PO token.

The retry exists for weak HTTP-provider tokens that unlock only format 18. With fetch_pot
"never", web_embedded resolves carry no token at all, so forcing the CLI provider changes
nothing: the retry asked YouTube the same question again, ~3 s more before the track started,
and got format 18 again (seen on the first track of a reported playlist start). yt-dlp is faked.
"""
import io
import os
import sys
import unittest
from contextlib import contextmanager, redirect_stdout
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import audio_player

WATCH = "https://www.youtube.com/watch?v=abc12345678"


def _combined(pot):
    url = "https://r1.googlevideo.com/videoplayback?expire=99999999999&id=abc&itag=18"
    if pot:
        url += "&pot=TOKEN"
    return {"id": "abc12345678", "url": url, "vcodec": "avc1.42001E", "acodec": "mp4a.40.2",
            "format_id": "18", "title": "T", "thumbnail": "", "webpage_url": WATCH,
            "duration": 100, "protocol": "https"}


class TestDegradedRetry(unittest.TestCase):

    def _resolve(self, info):
        keys = []

        @contextmanager
        def fake_resolver(key, opts):
            keys.append(key)
            yield SimpleNamespace(extract_info=lambda query, download=False: dict(info))

        with patch.object(audio_player, "_resolver", fake_resolver), \
                patch.object(audio_player, "_resolve_cache_put", lambda *a, **k: None):
            out = audio_player._resolve_audio_url(WATCH, "web_embedded")
        return keys, out

    def test_a_tokenless_combined_format_is_not_retried(self):
        log = io.StringIO()
        with redirect_stdout(log):
            keys, out = self._resolve(_combined(pot=False))
        self.assertEqual(len(keys), 1, "a CLI-forced retry cannot help a resolve with no token")
        self.assertFalse(out["is_audio_only"])
        # With no token involved, bgutil cannot be the cause; point at the client instead.
        self.assertNotIn("bgutil attestation", log.getvalue())
        self.assertIn("youtube.client", log.getvalue())

    def test_a_combined_format_with_a_token_is_retried_once_with_the_cli(self):
        keys, out = self._resolve(_combined(pot=True))
        self.assertEqual(len(keys), 2)
        self.assertFalse(keys[0][3])
        self.assertTrue(keys[1][3], "the retry must force the CLI provider (force_cli in the key)")


if __name__ == "__main__":
    unittest.main(verbosity=2)
