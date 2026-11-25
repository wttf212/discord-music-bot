"""YouTube format pick: an HLS manifest must never win.

On yt-dlp 2026.10.21+ web_embedded is sent with a Safari User-Agent, so a session that gets
no adaptive URLs still gets HLS formats 91-96, and "bestaudio/best" then picks format 96, a
1080p playlist the in-process reader cannot take. These run yt-dlp's own format selection
with audio_player._YT_FORMAT on hand-made format lists (no network: process_video_result
with download=False only sorts and selects) and pin the lookup that passes it on.
"""
import copy
import os
import sys
import unittest
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from yt_dlp import YoutubeDL
from yt_dlp.utils import qualities

import audio_player

_Q = qualities(["tiny", "audio_quality_ultralow", "audio_quality_low", "audio_quality_medium",
                "audio_quality_high", "small", "medium", "large", "hd720", "hd1080"])
# The sort order yt-dlp's YouTube extractor hands to the selector.
_SORT = ("quality", "res", "fps", "hdr:12", "source", "vcodec", "channels", "acodec", "lang", "proto")
_GV = "https://rr1---sn-x.googlevideo.com/videoplayback?expire=1795000000&itag={}&clen={}&c=X"
_HLS = "https://manifest.googlevideo.com/api/manifest/hls_playlist/expire/1795000000/itag/{}/playlist/index.m3u8"


def _https_audio(itag, ext, acodec, abr, quality, note=None):
    return {"format_id": itag, "url": _GV.format(itag, 4000000), "ext": ext, "acodec": acodec,
            "vcodec": "none", "abr": abr, "tbr": abr, "asr": 48000 if "opus" in acodec else 44100,
            "filesize": 4000000, "protocol": "https", "quality": _Q(quality), "audio_channels": 2,
            "source_preference": -1, "has_drm": False, "container": ext + "_dash",
            "format_note": note, "language_preference": -1}


def _https_video(itag, ext, vcodec, height, quality):
    return {"format_id": itag, "url": _GV.format(itag, 40000000), "ext": ext, "acodec": "none",
            "vcodec": vcodec, "tbr": height * 2, "vbr": height * 2, "height": height,
            "width": height * 16 // 9, "fps": 30, "filesize": 40000000, "protocol": "https",
            "quality": _Q(quality), "source_preference": -1, "has_drm": False,
            "container": ext + "_dash", "language_preference": -1}


def _format_18():
    return {"format_id": "18", "url": _GV.format("18", 16000000), "ext": "mp4",
            "acodec": "mp4a.40.2", "vcodec": "avc1.42001E", "tbr": 450, "height": 360,
            "width": 640, "fps": 30, "filesize": 16000000, "protocol": "https",
            "quality": _Q("medium"), "audio_channels": 2, "source_preference": -1,
            "has_drm": False, "language_preference": -1}


def _hls_audio(itag, abr):
    return {"format_id": itag, "url": _HLS.format(itag), "ext": "mp4",
            "acodec": "mp4a.40.2" if itag == "234" else "mp4a.40.5",
            "vcodec": "none", "abr": abr, "tbr": abr, "protocol": "m3u8_native", "quality": -1,
            "source_preference": -1, "has_drm": False, "manifest_url": _HLS.format("master")}


def _hls_combined(itag, height, quality):
    return {"format_id": itag, "url": _HLS.format(itag), "ext": "mp4", "acodec": "mp4a.40.2",
            "vcodec": "avc1.4d401f", "tbr": height * 3, "height": height, "width": height * 16 // 9,
            "fps": 30, "protocol": "m3u8_native", "quality": quality, "source_preference": -2,
            "has_drm": False, "manifest_url": _HLS.format("master")}


_HLS_HEIGHTS = [("91", 144), ("92", 240), ("93", 360), ("94", 480), ("95", 720), ("96", 1080)]


def _hls_9x(quality=None):
    return [_hls_combined(i, h, _Q("medium") if quality is None else quality) for i, h in _HLS_HEIGHTS]


def _audio(note=None):
    return [_https_audio("139", "m4a", "mp4a.40.5", 49, "audio_quality_low", note),
            _https_audio("249", "webm", "opus", 50, "audio_quality_low", note),
            _https_audio("250", "webm", "opus", 70, "audio_quality_low", note),
            _https_audio("140", "m4a", "mp4a.40.2", 129, "audio_quality_medium", note),
            _https_audio("251", "webm", "opus", 130, "audio_quality_medium", note)]


def _video():
    return [_https_video("134", "mp4", "avc1.4d401e", 360, "medium"),
            _https_video("136", "mp4", "avc1.4d401f", 720, "hd720"),
            _https_video("248", "webm", "vp9", 1080, "hd1080")]


def _pick(formats, selector=None):
    ydl = YoutubeDL({"format": selector or audio_player._YT_FORMAT, "quiet": True,
                     "no_warnings": True, "noplaylist": True, "check_formats": False})
    info = {"id": "abc12345678", "title": "t", "extractor": "youtube", "extractor_key": "Youtube",
            "webpage_url": "https://www.youtube.com/watch?v=abc12345678",
            "formats": copy.deepcopy(formats), "_format_sort_fields": _SORT,
            "is_live": False, "live_status": "not_live"}
    return ydl.process_video_result(info, download=False)["format_id"]


class TestYouTubeFormatSelector(unittest.TestCase):

    def test_visionos_answer_picks_https_opus_not_hls_audio(self):
        formats = _audio() + _video() + [_hls_audio("233", 48), _hls_audio("234", 128)]
        self.assertEqual(_pick(formats), "251")

    def test_normal_web_embedded_answer_picks_https_opus_over_hls(self):
        formats = _audio()[3:] + _video() + [_format_18()] + _hls_9x()
        self.assertEqual(_pick(formats), "251")

    def test_session_without_adaptive_urls_picks_progressive_18(self):
        self.assertEqual(_pick([_format_18()] + _hls_9x()), "18")

    def test_hls_only_picks_the_360p_playlist_never_the_1080p_one(self):
        picked = _pick(_hls_9x(quality=-1))
        self.assertEqual(picked, "93")
        self.assertNotEqual(picked, "96")

    def test_missing_pot_https_audio_is_still_picked(self):
        formats = _audio("medium, MISSING POT") + _video() + [_format_18()] + _hls_9x()
        self.assertEqual(_pick(formats), "251")

    def test_old_selector_would_have_picked_the_1080p_playlist(self):
        # Guards the premise of the whole change: if this stops holding, so does the reason for it.
        self.assertEqual(_pick(_hls_9x(quality=-1), "bestaudio/best"), "96")


class TestLookupPassesTheFormat(unittest.TestCase):

    def _opts_for(self, query):
        seen = []

        @contextmanager
        def fake_resolver(key, opts):
            seen.append(opts)
            yield SimpleNamespace(extract_info=lambda q, download=False: {
                "id": "abc12345678", "url": "https://r1.googlevideo.com/videoplayback?expire=99999999999",
                "vcodec": "none", "title": "T", "thumbnail": "", "webpage_url": query, "duration": 100})

        with patch.object(audio_player, "_resolver", fake_resolver), \
                patch.object(audio_player, "_resolve_cache_put", lambda *a, **k: None):
            audio_player._resolve_audio_url(query, "web_embedded")
        self.assertEqual(len(seen), 1)
        return seen[0]

    def test_youtube_url_uses_the_youtube_format(self):
        opts = self._opts_for("https://www.youtube.com/watch?v=abc12345678")
        self.assertEqual(opts["format"], audio_player._YT_FORMAT)

    def test_plain_text_search_uses_the_youtube_format(self):
        self.assertEqual(self._opts_for("some artist - some song")["format"], audio_player._YT_FORMAT)

    def test_other_sites_keep_bestaudio_best(self):
        opts = self._opts_for("https://soundcloud.com/artist/track")
        self.assertEqual(opts["format"], "bestaudio/best")


if __name__ == "__main__":
    unittest.main()
