"""Lookups someone is waiting on try youtube.fast_client (visionos) first.

A cold first play on web_embedded waits out YouTube's pre-roll ad (3-4 s of 403s on a fresh
URL); visionos gets no ad, needs no JS solve and no PO token, and its opus URL is served on
the first request. YouTube cut android_vr off the same way in August 2026, so the fast client
is only ever tried first: the configured client answers inside the same lookup when it is
refused, a refusal of the fast client alone never arms the shared YouTube block, repeated
refusals bench it, and a refused fast-client stream URL makes play() look the track up again
with the configured client. Queued tracks (background lookups) keep the configured client.

yt-dlp, curl_cffi, FFmpeg and the network are faked, and the client-table lookup is patched so
the tests do not depend on the installed yt-dlp.
"""
import asyncio
import contextlib
import io
import itertools
import os
import sys
import time
import types
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import yaml

import audio_player
from yt_dlp.utils import DownloadError

from test_stream_reader import FakeResponse, FakeSession, _drain
from test_stream_warmup import _warm
from test_youtube_block import (AGE_GATED, BOT_CHECK, FRAME, PRIVATE, SYSTEMIC,
                                TOO_MANY_REQUESTS, _FakeFFmpeg, _FakeProc, _PlayVC)

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VID = "abc12345678"
WATCH = f"https://www.youtube.com/watch?v={VID}"
FAST = "VISIONOS"                 # c= on a visionos stream URL
WEB = "WEB_EMBEDDED_PLAYER"       # c= on a web_embedded stream URL
NOT_ON_APP = DownloadError(f"ERROR: [youtube] {VID}: The following content is not available "
                           "on this app.")
SERVER_ERROR = DownloadError(f"ERROR: [youtube] {VID}: HTTP Error 503: Service Unavailable")
NO_RESULTS = audio_player._no_results_error("some song")


_SIG = itertools.count()


def _result(c, minted_ago=0.0, audio_only=True, protocol="https", is_live=False):
    # Every lookup gets its own URL, as YouTube's signed ones are.
    url = (f"https://r1---sn-x.googlevideo.com/videoplayback?expire={int(time.time()) + 6 * 3600}"
           f"&id={VID}&itag=251&clen=1024&c={c}&sig={next(_SIG)}")
    return {"url": url, "title": c, "thumbnail": "", "webpage_url": WATCH,
            "is_audio_only": audio_only, "duration": 100, "artist": "", "protocol": protocol,
            "is_live": is_live, "client": c, "minted_at": time.time() - minted_ago}


def _manifest(is_live=False):
    """An HLS answer as yt-dlp gives it: its parameters sit in the path, so it names no c=."""
    url = (f"https://manifest.googlevideo.com/api/manifest/hls_playlist/expire/"
           f"{int(time.time()) + 6 * 3600}/ei/x/id/{VID}.1/itag/233/source/youtube/"
           f"playlist/index.m3u8")
    return {**_result(FAST, protocol="m3u8_native", is_live=is_live), "url": url, "client": ""}


class _Clients:
    """Stands in for _resolve_audio_url: each client answers from a script."""

    def __init__(self, **answers):
        self.answers = answers
        self.calls = []             # (client, cookies_file)

    def __call__(self, query, client, debug=False, cookies_file=None, **kwargs):
        self.calls.append((client, cookies_file))
        answer = self.answers[client]
        if callable(answer):
            answer = answer()       # a fresh result, and so a fresh URL, for every lookup
        if isinstance(answer, BaseException):
            raise answer
        return answer

    @property
    def asked(self):
        return [client for client, _ in self.calls]


def _quiet(fn, *args, **kwargs):
    with contextlib.redirect_stdout(io.StringIO()):
        return fn(*args, **kwargs)


def _enable_fast_client():
    with patch.object(audio_player, "_yt_client_name", return_value=FAST):
        return _quiet(audio_player.configure_fast_client, "visionos", "web_embedded", None)


def _reset():
    _quiet(audio_player.configure_fast_client, None, "web_embedded", None)
    audio_player._yt_block_until = 0.0
    audio_player._yt_block_strikes = 0
    audio_player._yt_block_probing = False
    audio_player._resolve_cache.clear()
    audio_player._inflight_resolves.clear()


def _bench_left():
    return audio_player._fast_client_benched_until - time.monotonic()


class _FastClientState(unittest.TestCase):
    """Fast client on, YouTube answering, nothing benched or cached; all of it put back."""

    def setUp(self):
        _reset()
        self.addCleanup(_reset)
        _enable_fast_client()
        impersonate = patch.object(audio_player, "_IMPERSONATE_AVAILABLE", True)
        impersonate.start()
        self.addCleanup(impersonate.stop)

    def _lookup(self, clients, fast=True, cookies_file=None):
        with patch.object(audio_player, "_resolve_audio_url", clients):
            return _quiet(audio_player._resolve_gated, WATCH, "web_embedded", False,
                          cookies_file, fast)


# --- Configuration ---------------------------------------------------------------------

class TestConfigure(unittest.TestCase):

    def setUp(self):
        _reset()
        self.addCleanup(_reset)

    def _configure(self, name, client="web_embedded", cookies_file=None, known=FAST):
        out = io.StringIO()
        with patch.object(audio_player, "_yt_client_name", return_value=known), \
                contextlib.redirect_stdout(out):
            enabled = audio_player.configure_fast_client(name, client, cookies_file)
        return enabled, out.getvalue()

    def test_empty_is_off_and_silent(self):
        for name in (None, "", "  "):
            with self.subTest(name=name):
                enabled, out = self._configure(name)
                self.assertIsNone(enabled)
                self.assertEqual(out, "")
                self.assertIsNone(audio_player._FAST_CLIENT)

    def test_a_name_yt_dlp_does_not_know_is_off_with_a_line(self):
        """yt-dlp would skip it with a warning and silently use its default clients."""
        enabled, out = self._configure("visionos", known=None)
        self.assertIsNone(enabled)
        self.assertEqual(len(out.splitlines()), 1)
        self.assertIn("[yt-dlp]", out)
        self.assertIn("off", out)
        self.assertEqual(audio_player._FAST_CLIENT_C, "")

    def test_the_configured_client_itself_is_off(self):
        for client in ("visionos", "web_embedded, visionos"):
            with self.subTest(client=client):
                enabled, out = self._configure("visionos", client=client)
                self.assertIsNone(enabled)
                self.assertEqual(len(out.splitlines()), 1)

    def test_a_cookies_file_turns_it_off(self):
        """yt-dlp skips visionos for a logged-in session, so every lookup would fall back."""
        enabled, out = self._configure("visionos", cookies_file="/data/cookies.txt")
        self.assertIsNone(enabled)
        self.assertIn("cookies_file", out)

    def test_a_valid_name_is_on_with_the_c_value_from_the_table(self):
        enabled, out = self._configure("visionos")
        self.assertEqual(enabled, "visionos")
        self.assertEqual(audio_player._FAST_CLIENT, "visionos")
        self.assertEqual(audio_player._FAST_CLIENT_C, FAST)
        self.assertEqual(len(out.splitlines()), 1)
        self.assertIn("web_embedded", out, "the line names the fallback")

    def test_configuring_again_clears_the_bench(self):
        self._configure("visionos")
        audio_player._note_fast_attempt(False, "x", bench_now=True)
        self.assertGreater(_bench_left(), 0)
        self._configure("visionos")
        self.assertLessEqual(_bench_left(), 0)
        self.assertEqual(audio_player._fast_client_benches, 0)

    def test_the_table_helper_reads_yt_dlps_client_table(self):
        self.assertEqual(audio_player._yt_client_name("web_embedded"), WEB)
        self.assertEqual(audio_player._yt_client_name("visionos"), FAST,
                         "the c= value the tests assume is the one yt-dlp >= 2026.10.26 uses")
        self.assertIsNone(audio_player._yt_client_name("no_such_client"))


class TestStartupWiring(unittest.TestCase):

    def test_main_configures_it_after_the_cookies_check_and_before_the_bot(self):
        with open(os.path.join(_ROOT, "main.py"), encoding="utf-8") as fh:
            src = " ".join(fh.read().split())
        call = src.index('configure_fast_client(_yt_cfg.get("fast_client", "visionos"), '
                         '_yt_cfg.get("client", "web"), _cookies_in_use)')
        self.assertLess(src.index("COOKIE-02"), call)
        self.assertIn("_cookies_in_use = _cookies_file if _cookies_file and "
                      "os.path.isfile(_cookies_file) else None", src,
                      "a cookies_file that does not exist is not used, so it must not turn it off")
        self.assertLess(call, src.index("bot = MusicBot(config)"))
        self.assertIn("from audio_player import AudioPlayer, shutdown_audio, configure_fast_client",
                      src)

    def test_it_is_on_by_default(self):
        """A config without the line gets visionos, and the example shows the same default.
        An explicit "" is the way to turn it off (test_empty_is_off_and_silent)."""
        with open(os.path.join(_ROOT, "config.example.yaml"), encoding="utf-8") as fh:
            example = yaml.safe_load(fh.read())
        self.assertEqual(example["youtube"]["fast_client"], "visionos")
        with open(os.path.join(_ROOT, "main.py"), encoding="utf-8") as fh:
            self.assertIn('_yt_cfg.get("fast_client", "visionos")', fh.read())


# --- Plumbing: which lookups may use it ----------------------------------------------------

class TestPlumbing(_FastClientState):

    def _lane_fast_args(self, *calls):
        lane = MagicMock()
        lane.run = AsyncMock(return_value=_result(WEB))
        with patch.object(audio_player, "_RESOLVE_LANE", lane):
            for kwargs in calls:
                asyncio.run(audio_player.resolve_audio("q", "web_embedded", **kwargs))
        for call in lane.run.call_args_list:
            self.assertIs(call.args[0], audio_player.get_audio_url)
        return [call.args[5] for call in lane.run.call_args_list]

    def test_resolve_audio_passes_fast_to_the_lookup(self):
        self.assertEqual(self._lane_fast_args({"background": True}, {}, {"fast": False},
                                              {"background": True, "fast": True}),
                         [False, True, False, True])

    def test_get_audio_url_hands_fast_on_on_every_path(self):
        seen = []

        def gated(query, client, debug, cookies_file, fast=False):
            seen.append(fast)
            return _result(WEB)

        with patch.object(audio_player, "_resolve_gated", gated):
            audio_player.get_audio_url("some song", "web_embedded", False, None, True)   # text
            audio_player.get_audio_url(WATCH, "web_embedded", False, None, True)        # leader
            audio_player._resolve_cache.clear()
            failed_leader = audio_player._InflightResolve()
            failed_leader.event.set()                                                    # no result
            audio_player._inflight_resolves[VID] = failed_leader
            audio_player.get_audio_url(WATCH, "web_embedded", False, None, True)        # follower
        self.assertEqual(seen, [True, True, True])

    def test_end_to_end_only_waited_on_lookups_ask_the_fast_client(self):
        for kwargs, expected in (({}, ["visionos"]), ({"background": True}, ["web_embedded"])):
            with self.subTest(**kwargs):
                audio_player._resolve_cache.clear()
                clients = _Clients(visionos=_result(FAST), web_embedded=_result(WEB))
                with patch.object(audio_player, "_resolve_audio_url", clients):
                    _quiet(asyncio.run, audio_player.resolve_audio(WATCH, "web_embedded", **kwargs))
                self.assertEqual(clients.asked, expected)


# --- Which client a lookup asks ------------------------------------------------------------

class TestWhenTheFastClientIsAsked(_FastClientState):

    def _clients(self):
        return _Clients(visionos=_result(FAST), web_embedded=_result(WEB))

    def test_it_answers_alone_when_it_works_and_never_gets_the_cookies_file(self):
        clients = self._clients()
        self.assertEqual(self._lookup(clients, cookies_file="c.txt")["client"], FAST)
        self.assertEqual(clients.calls, [("visionos", None)])

    def test_otherwise_one_lookup_with_the_configured_client(self):
        def off():
            _quiet(audio_player.configure_fast_client, None, "web_embedded", None)

        def benched():
            audio_player._fast_client_benched_until = time.monotonic() + 100

        def probing_a_block():
            audio_player._yt_block_strikes = 1
            audio_player._yt_block_until = time.monotonic() - 1    # cooldown over: a probe

        for name, setup, fast in (("off", off, True), ("not waited on", None, False),
                                  ("benched", benched, True),
                                  ("probing a block", probing_a_block, True)):
            with self.subTest(name):
                _reset()
                _enable_fast_client()
                if setup:
                    setup()
                clients = self._clients()
                self.assertEqual(self._lookup(clients, fast=fast)["client"], WEB)
                self.assertEqual(clients.asked, ["web_embedded"])

    def test_the_probe_that_lifts_a_block_is_the_configured_client(self):
        audio_player._yt_block_strikes = 1
        audio_player._yt_block_until = time.monotonic() - 1
        clients = self._clients()
        self._lookup(clients)
        self.assertEqual(clients.asked, ["web_embedded"])
        self.assertEqual(audio_player._yt_block_strikes, 0)


# --- When the fast client fails --------------------------------------------------------------

class TestVerdicts(_FastClientState):

    def test_transient_and_client_independent_errors_are_raised_as_they_are(self):
        for error in (TOO_MANY_REQUESTS, SERVER_ERROR, ConnectionResetError("connection reset"),
                      DownloadError("ERROR: [youtube] x: Read timed out."), PRIVATE, NO_RESULTS):
            with self.subTest(error=str(error)[:50]):
                _reset()
                _enable_fast_client()
                clients = _Clients(visionos=error, web_embedded=_result(WEB))
                with self.assertRaises(type(error)) as ctx:
                    self._lookup(clients)
                self.assertIs(ctx.exception, error)
                self.assertEqual(clients.asked, ["visionos"])
                self.assertEqual(list(audio_player._fast_client_recent), [])

    def test_an_age_gate_falls_back_and_counts_nothing(self):
        clients = _Clients(visionos=AGE_GATED, web_embedded=_result(WEB))
        for _ in range(3):
            self.assertEqual(self._lookup(clients)["client"], WEB)
        self.assertEqual(clients.asked, ["visionos", "web_embedded"] * 3)
        self.assertEqual(list(audio_player._fast_client_recent), [])
        self.assertLessEqual(_bench_left(), 0)

    def test_a_bot_check_falls_back_and_benches_at_once(self):
        clients = _Clients(visionos=BOT_CHECK, web_embedded=_result(WEB))
        self.assertEqual(self._lookup(clients)["client"], WEB)
        self.assertAlmostEqual(_bench_left(), audio_player._FAST_BENCHES[0], delta=5)
        self._lookup(clients)
        self.assertEqual(clients.asked, ["visionos", "web_embedded", "web_embedded"])

    def test_no_refusal_of_the_fast_client_alone_arms_the_shared_block(self):
        for name, refusal in SYSTEMIC.items():
            with self.subTest(name):
                _reset()
                _enable_fast_client()
                clients = _Clients(visionos=refusal, web_embedded=_result(WEB))
                self.assertEqual(self._lookup(clients)["client"], WEB)
                self.assertEqual(audio_player._yt_block_strikes, 0)
                self.assertEqual(audio_player.youtube_block_remaining(), 0.0)
                self.assertGreater(_bench_left(), 0)

    def test_both_refused_is_a_youtube_block_and_does_not_bench(self):
        configured_refusal = DownloadError(str(BOT_CHECK))
        clients = _Clients(visionos=BOT_CHECK, web_embedded=configured_refusal)
        with self.assertRaises(DownloadError) as ctx:
            self._lookup(clients)
        self.assertIs(ctx.exception, configured_refusal, "the configured client's error is the answer")
        self.assertEqual(clients.asked, ["visionos", "web_embedded"])
        self.assertEqual(audio_player._yt_block_strikes, 1, "both clients refused: a block")
        self.assertLessEqual(_bench_left(), 0, "the server is refused, not the fast client")
        self.assertEqual(list(audio_player._fast_client_recent), [])

    def test_an_unknown_error_benches_on_the_second_failure_in_five_attempts(self):
        failing = _Clients(visionos=NOT_ON_APP, web_embedded=_result(WEB))
        self.assertEqual(self._lookup(failing)["client"], WEB)
        self.assertEqual(failing.asked, ["visionos", "web_embedded"])
        self.assertLessEqual(_bench_left(), 0, "one failure is not enough")
        for _ in range(3):
            _quiet(audio_player._note_fast_attempt, True)    # a fast-client stream played
        self._lookup(failing)                          # 2nd failure within the last five
        self.assertAlmostEqual(_bench_left(), audio_player._FAST_BENCHES[0], delta=5)

    def test_a_failure_outside_the_last_five_attempts_is_forgotten(self):
        for successes in (4, 5):
            with self.subTest(successes=successes):
                _reset()
                _enable_fast_client()
                failing = _Clients(visionos=NOT_ON_APP, web_embedded=_result(WEB))
                self._lookup(failing)
                for _ in range(successes):
                    _quiet(audio_player._note_fast_attempt, True)    # a fast-client stream played
                self._lookup(failing)
                self.assertLessEqual(_bench_left(), 0)

    def _lookup_while_the_block_lands(self, probing):
        """visionos is refused while another guild's lookup arms the shared block."""
        clients = _Clients(visionos=BOT_CHECK, web_embedded=_result(WEB))

        def lookup(query, client, *args, **kwargs):
            if client == "visionos":
                audio_player._yt_block_strikes = 1
                audio_player._yt_block_until = time.monotonic() + (30 if probing else 60)
                audio_player._yt_block_probing = probing
            return clients(query, client, *args, **kwargs)

        return clients, lambda: self._lookup(lookup)

    def test_no_fallback_once_another_lookup_armed_the_shared_block(self):
        clients, lookup = self._lookup_while_the_block_lands(probing=False)
        with self.assertRaises(audio_player.YouTubeBlockedError):
            lookup()
        self.assertEqual(clients.asked, ["visionos"], "no request to a just-flagged address")
        self.assertEqual(audio_player._yt_block_strikes, 1, "the block stays as it was armed")
        self.assertEqual(list(audio_player._fast_client_recent), [])
        self.assertLessEqual(_bench_left(), 0)

    def test_during_a_probe_the_fallback_still_answers(self):
        """Re-raising there would let the fast client's refusal count as a block strike."""
        clients, lookup = self._lookup_while_the_block_lands(probing=True)
        self.assertEqual(lookup()["client"], WEB)
        self.assertEqual(clients.asked, ["visionos", "web_embedded"])

    def test_a_refusal_with_a_working_fallback_leaves_the_block_alone(self):
        clients = _Clients(visionos=BOT_CHECK, web_embedded=_result(WEB))
        with patch.object(audio_player, "_resolve_audio_url", clients):
            _quiet(audio_player.get_audio_url, WATCH, "web_embedded", False, None, True)
        self.assertEqual(audio_player._yt_block_strikes, 0,
                         "visionos's refusal alone must not hold every guild's YouTube off")
        self.assertEqual(audio_player.youtube_block_remaining(), 0.0)


class TestBench(_FastClientState):

    @staticmethod
    def _expire():
        audio_player._fast_client_benched_until = time.monotonic() - 1

    def test_bench_durations_escalate(self):
        hours = []
        for _ in range(len(audio_player._FAST_BENCHES) + 1):
            _quiet(audio_player._note_fast_attempt, False, "refused", bench_now=True)
            hours.append(round(_bench_left() / 3600, 1))
            self._expire()
        self.assertEqual(hours, [0.5, 1.0, 2.0, 4.0, 8.0, 8.0])

    def test_a_success_resets_the_escalation_and_says_it_is_back(self):
        _quiet(audio_player._note_fast_attempt, False, "refused", bench_now=True)
        self._expire()
        _quiet(audio_player._note_fast_attempt, False, "refused", bench_now=True)
        self.assertAlmostEqual(_bench_left(), 3600, delta=5)
        self._expire()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            audio_player._note_fast_attempt(True)
            audio_player._note_fast_attempt(True)
        self.assertEqual(out.getvalue().count("back in use"), 1, "said once, after the bench")
        _quiet(audio_player._note_fast_attempt, False, "refused", bench_now=True)
        self.assertAlmostEqual(_bench_left(), 1800, delta=5)

    def test_one_line_per_bench_and_benching_clears_the_outcomes(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            audio_player._note_fast_attempt(False, "first")
            audio_player._note_fast_attempt(False, "second")
        lines = out.getvalue().splitlines()
        self.assertEqual(len(lines), 1)
        self.assertIn("visionos", lines[0])
        self.assertIn("30 min", lines[0])
        self.assertIn("second", lines[0])
        self.assertEqual(list(audio_player._fast_client_recent), [])

    def test_attempts_that_end_during_a_bench_change_nothing(self):
        _quiet(audio_player._note_fast_attempt, False, "refused", bench_now=True)
        until = audio_player._fast_client_benched_until
        _quiet(audio_player._note_fast_attempt, False, "an older stream cut", bench_now=True)
        _quiet(audio_player._note_fast_attempt, True)
        self.assertEqual(audio_player._fast_client_benched_until, until)
        self.assertEqual(audio_player._fast_client_benches, 1)

    def test_it_says_whether_it_recorded_the_attempt(self):
        self.assertTrue(_quiet(audio_player._note_fast_attempt, True))
        self.assertTrue(_quiet(audio_player._note_fast_attempt, False, "x", bench_now=True))
        self.assertFalse(_quiet(audio_player._note_fast_attempt, False, "y"), "benched")
        self.assertFalse(_quiet(audio_player._note_fast_attempt, True), "benched")


class TestRefusalLines(_FastClientState):
    """What an operator reads when a fast-client stream URL fails before its first frame."""

    def _lines(self, *calls):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            for args in calls:
                audio_player._note_fast_stream_refused(*args)
        return [line for line in out.getvalue().splitlines() if line.startswith("[stream]")]

    def test_each_line_says_what_happened_and_promises_no_next_step(self):
        lines = self._lines((_result(FAST),), (_result(FAST), "gave no audio"))
        self.assertIn("refused", lines[0])
        self.assertIn("counted against visionos", lines[0])
        self.assertIn("gave no audio", lines[1])
        self.assertNotIn("refused", lines[1])
        self.assertFalse(any("looking the track up" in line for line in lines),
                         "on the last try there is no further lookup")

    def test_a_refusal_during_a_bench_says_it_was_not_counted(self):
        _quiet(audio_player._note_fast_attempt, False, "refused", bench_now=True)
        [line] = self._lines((_result(FAST),))
        self.assertIn("not counted", line)
        self.assertNotIn("counted against", line)
        self.assertEqual(audio_player._fast_client_benches, 1)

    def test_one_refused_url_counts_once(self):
        """Guilds can share one URL (in-flight lookups, the resolve cache)."""
        shared = _result(FAST)
        lines = self._lines((shared,), (dict(shared),))
        self.assertEqual(list(audio_player._fast_client_recent), [False])
        self.assertLessEqual(_bench_left(), 0, "one refused URL is one failure")
        self.assertIn("already counted", lines[1])
        self._lines((_result(FAST),))
        self.assertGreater(_bench_left(), 0, "a second URL refused is the second failure")

    def test_a_stale_url_is_named_stale(self):
        [line] = self._lines((_result(FAST, minted_ago=10 * 60), "gave no audio"))
        self.assertIn("gave no audio 10 min after its lookup", line)
        self.assertIn("stale, not counted", line)
        self.assertEqual(list(audio_player._fast_client_recent), [])


# --- What the fast client answered -----------------------------------------------------------

class TestResultHandling(_FastClientState):

    def test_an_accepted_answer_records_nothing(self):
        """android_vr (August 2026) kept answering lookups while every stream URL was refused."""
        audio_player._fast_client_benches = 2
        clients = _Clients(visionos=_result(FAST), web_embedded=_result(WEB))
        self.assertEqual(self._lookup(clients)["client"], FAST)
        self.assertEqual(clients.asked, ["visionos"])
        self.assertEqual(list(audio_player._fast_client_recent), [])
        self.assertEqual(audio_player._fast_client_benches, 2, "the escalation is not reset")

    def test_an_answer_from_another_client_is_kept_without_a_second_lookup(self):
        """A made-for-kids video: yt-dlp appends web_embedded inside the visionos lookup."""
        clients = _Clients(visionos=_result(WEB), web_embedded=_result(WEB))
        self.assertEqual(self._lookup(clients)["client"], WEB)
        self.assertEqual(clients.asked, ["visionos"])
        self.assertEqual(list(audio_player._fast_client_recent), [])

    def test_a_combined_hls_or_live_answer_asks_the_configured_client(self):
        for name, fast_result in (("combined", _result(FAST, audio_only=False)),
                                  ("hls", _manifest()),
                                  ("live", _manifest(is_live=True))):
            with self.subTest(name):
                _reset()
                _enable_fast_client()
                clients = _Clients(visionos=fast_result, web_embedded=_result(WEB))
                self.assertEqual(self._lookup(clients)["client"], WEB)
                self.assertEqual(clients.asked, ["visionos", "web_embedded"])
                self.assertEqual(list(audio_player._fast_client_recent), [])

    def test_the_fast_answer_is_kept_when_the_configured_client_fails(self):
        fast_result = _result(FAST, audio_only=False)
        clients = _Clients(visionos=fast_result, web_embedded=PRIVATE)
        self.assertIs(self._lookup(clients), fast_result)
        self.assertEqual(list(audio_player._fast_client_recent), [])

    def test_a_rejected_fast_answer_does_not_stay_in_the_resolve_cache(self):
        """_resolve_audio_url caches an audio-only answer before it is judged here."""
        clients = _Clients(visionos=_manifest(is_live=True),
                           web_embedded=_result(WEB, audio_only=False))

        def resolve(query, client, debug=False, cookies_file=None, **kwargs):
            answer = clients(query, client, debug, cookies_file)
            audio_player._resolve_cache_put(VID, answer)     # as _resolve_audio_url does
            return answer

        self.assertEqual(self._lookup(resolve)["client"], WEB)
        self.assertEqual(clients.asked, ["visionos", "web_embedded"])
        self.assertIsNone(audio_player._resolve_cache_get(WATCH))


# --- Freshness ---------------------------------------------------------------------------------

class TestCombinedFormatWarning(_FastClientState):
    """A combined answer from the fast client is not kept, so no advice about keeping it."""

    def _out(self, client):
        @contextlib.contextmanager
        def fake_resolver(key, opts):
            yield SimpleNamespace(extract_info=lambda q, download=False: {
                "id": VID, "url": "https://r1.googlevideo.com/videoplayback?expire=99999999999&itag=18",
                "vcodec": "avc1.42001E", "title": "T", "thumbnail": "", "webpage_url": WATCH,
                "duration": 100})

        out = io.StringIO()
        with patch.object(audio_player, "_resolver", fake_resolver), \
                patch.object(audio_player, "_resolve_cache_put", lambda *a, **k: None), \
                contextlib.redirect_stdout(out):
            audio_player._resolve_audio_url(WATCH, client)
        return out.getvalue()

    def test_only_the_configured_clients_combined_answer_gets_the_warning(self):
        self.assertNotIn("Combined video+audio", self._out("visionos"))
        self.assertNotIn("youtube.client", self._out("visionos"))
        self.assertIn("Combined video+audio", self._out("web_embedded"))


class TestPrefetchReplacesARefusedFastUrl(_FastClientState):
    """A background lookup can be handed a fast-client URL (resolve cache, a lookup in flight)."""

    def _prefetch(self, first, warms):
        import commands
        from track_queue import Track, TrackQueue

        class _GS:
            def __init__(self):
                self.queue = TrackQueue()
                self.prefetch_task = None

        class _Bot:
            config = {"youtube": {"client": "web_embedded"}, "debug": False}

            def __init__(self):
                self.gs = _GS()

            def get_guild_state(self, guild_id):
                return self.gs

        bot = _Bot()
        bot.gs.queue.add(Track(query=WATCH, title="t", requested_by="u1"))
        commands._last_prefetch_monotonic = 0.0
        resolve = AsyncMock(side_effect=[first, _result(WEB)])
        probes = iter(warms)
        with patch.object(commands, "resolve_audio", resolve), \
                patch.object(commands, "warm_stream_url", lambda url, debug=False: next(probes)), \
                patch.object(commands, "invalidate_resolve_cache") as invalidate, \
                contextlib.redirect_stdout(io.StringIO()):
            asyncio.run(commands._prefetch_next_track(bot, 1))
        return bot.gs.queue.list()[0], resolve, invalidate

    def test_a_refused_fast_url_is_replaced_by_the_configured_clients(self):
        track, resolve, invalidate = self._prefetch(_result(FAST), [False, True])
        self.assertEqual(track.resolved_info["client"], WEB)
        self.assertEqual(resolve.await_count, 2)
        self.assertTrue(all(c.kwargs.get("background") for c in resolve.await_args_list))
        invalidate.assert_called_once_with(WATCH)

    def test_a_fast_url_that_answers_is_kept(self):
        track, resolve, invalidate = self._prefetch(_result(FAST), [True])
        self.assertEqual(track.resolved_info["client"], FAST)
        self.assertEqual(resolve.await_count, 1)
        invalidate.assert_not_called()

    def test_a_cold_configured_client_url_is_kept_as_before(self):
        track, resolve, _ = self._prefetch(_result(WEB), [False])
        self.assertEqual(track.resolved_info["client"], WEB)
        self.assertEqual(resolve.await_count, 1, "playback waits out its ad as before")


class TestFreshness(_FastClientState):

    def test_a_fast_client_url_is_reused_for_at_most_an_hour(self):
        cap = audio_player._FAST_URL_MAX_AGE
        self.assertFalse(audio_player.is_stream_info_fresh(_result(FAST, minted_ago=cap + 60)))
        self.assertTrue(audio_player.is_stream_info_fresh(_result(FAST, minted_ago=cap - 60)))
        self.assertTrue(audio_player.is_stream_info_fresh(_result(WEB, minted_ago=cap + 60)))

    def test_the_cap_needs_the_fast_client_on(self):
        _quiet(audio_player.configure_fast_client, None, "web_embedded", None)
        old = _result(FAST, minted_ago=audio_player._FAST_URL_MAX_AGE + 60)
        self.assertTrue(audio_player.is_stream_info_fresh(old))

    def test_the_resolve_cache_drops_an_old_fast_client_url(self):
        audio_player._resolve_cache_put(VID, _result(FAST, minted_ago=2 * 3600))
        self.assertIsNone(audio_player._resolve_cache_get(WATCH))
        audio_player._resolve_cache_put(VID, _result(FAST, minted_ago=60))
        self.assertIsNotNone(audio_player._resolve_cache_get(WATCH))


# --- The stream side ---------------------------------------------------------------------------

def _reader(responses, url, **kwargs):
    """A _CurlStreamReader on a FakeSession; returns (reader_or_exception, session)."""
    session = FakeSession(responses)
    fake_requests = types.ModuleType("curl_cffi.requests")
    fake_requests.Session = lambda **kw: session
    fake_root = types.ModuleType("curl_cffi")
    fake_root.requests = fake_requests
    with patch.dict(sys.modules, {"curl_cffi": fake_root, "curl_cffi.requests": fake_requests}):
        try:
            return audio_player._CurlStreamReader(url, **kwargs), session
        except Exception as e:
            return e, session


def _gv(c, clen=1024):
    return (f"https://r5---sn-x.googlevideo.com/videoplayback?expire=99999999999&id=abc"
            f"&clen={clen}&c={c}")


class TestReader(_FastClientState):

    def setUp(self):
        super().setUp()
        for name, value in (("_FAST_STREAM_RETRY", (0.01,)),
                            ("_STREAM_SETTLE_BACKOFF", (0.01, 0.01, 0.01))):
            p = patch.object(audio_player, name, value)
            p.start()
            self.addCleanup(p.stop)

    def test_a_refused_first_window_is_retried_once_then_refused(self):
        result, session = _reader([FakeResponse(403)] * 5, _gv(FAST), settle=False)
        self.assertIsInstance(result, audio_player._StreamRefusedError)
        self.assertEqual(len(session.requests), 2)
        self.assertTrue(session.closed)

    def test_one_blip_on_the_first_window_is_ridden_out(self):
        body = b"x" * 64
        reader, session = _reader([FakeResponse(403), FakeResponse(200, body)],
                                  _gv(FAST, len(body)), settle=False)
        self.assertEqual(_drain(reader), body)
        self.assertEqual(len(session.requests), 2)

    def test_other_first_window_failures_stay_start_errors(self):
        for name, responses in (("404", [FakeResponse(404)] * 2),
                                ("connection error", [ConnectionError("reset")] * 2),
                                ("403, then a connection error",
                                 [FakeResponse(403), ConnectionError("reset")])):
            with self.subTest(name):
                result, session = _reader(responses, _gv(FAST), settle=False)
                self.assertIsInstance(result, audio_player._StreamStartError)
                self.assertNotIsInstance(result, audio_player._StreamRefusedError)
                self.assertEqual(len(session.requests), 2)

    def test_settling_streams_keep_the_ladder_and_never_raise_a_refusal(self):
        result, session = _reader([FakeResponse(403)] * 10, _gv(WEB))
        self.assertIsInstance(result, audio_player._StreamStartError)
        self.assertNotIsInstance(result, audio_player._StreamRefusedError)
        self.assertEqual(len(session.requests), 4, "the whole (patched) settle ladder")

    def _cut_mid_stream(self, **kwargs):
        chunk = audio_player._STREAM_CHUNK_BYTES
        reader, session = _reader([FakeResponse(200, b"a" * chunk), FakeResponse(403)],
                                  _gv(FAST, chunk + 4096), **kwargs)
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(len(_drain(reader)), chunk)
        self.assertEqual(reader.poll(), 1, "the track still ends")
        self.assertEqual(len(session.requests), 2)
        return out.getvalue()

    def test_a_refusal_on_a_later_window_benches_the_fast_client_at_once(self):
        out = self._cut_mid_stream(settle=False, minted_at=time.time() - 42)
        self.assertAlmostEqual(_bench_left(), audio_player._FAST_BENCHES[0], delta=5)
        line = [l for l in out.splitlines() if l.startswith("[stream]")]
        self.assertEqual(len(line), 1, out)
        self.assertIn("visionos", line[0])
        self.assertIn(str(audio_player._STREAM_CHUNK_BYTES), line[0])
        self.assertIn("42 s", line[0])

    def test_a_later_refusal_of_an_old_fast_url_counts_nothing(self):
        """After a long pause the next window can go to a URL older than any measured serving."""
        out = self._cut_mid_stream(settle=False, minted_at=time.time() - 2 * 3600)
        self.assertLessEqual(_bench_left(), 0)
        self.assertEqual(list(audio_player._fast_client_recent), [])
        self.assertIn("stale, not counted", out)

    def test_a_later_refusal_of_a_settling_stream_benches_nothing(self):
        self._cut_mid_stream()
        self.assertLessEqual(_bench_left(), 0)


class TestOpenAudioStream(_FastClientState):

    def test_a_refused_fast_stream_propagates_and_starts_no_subprocess(self):
        info = _result(FAST)
        with patch.object(audio_player, "_CurlStreamReader",
                          side_effect=audio_player._StreamRefusedError("HTTP 403 at offset 0")
                          ) as reader, \
                patch.object(audio_player, "_start_ytdlp_stream") as sub:
            with self.assertRaises(audio_player._StreamRefusedError):
                audio_player._open_audio_stream(WATCH, "web_embedded", None, info)
        sub.assert_not_called()
        self.assertIs(reader.call_args.kwargs["settle"], False)
        self.assertEqual(reader.call_args.kwargs["minted_at"], info["minted_at"])

    def test_other_streams_open_as_before(self):
        cases = (("web_embedded URL", _result(WEB), True),
                 ("visionos URL with the fast client off", _result(FAST), False))
        for name, info, fast_on in cases:
            with self.subTest(name):
                if not fast_on:
                    _quiet(audio_player.configure_fast_client, None, "web_embedded", None)
                with patch.object(audio_player, "_CurlStreamReader",
                                  side_effect=audio_player._StreamStartError("HTTP 403")
                                  ) as reader, \
                        patch.object(audio_player, "_start_ytdlp_stream") as sub:
                    _quiet(audio_player._open_audio_stream, WATCH, "web_embedded", None, info)
                reader.assert_called_once_with(info["url"], debug=False)
                sub.assert_called_once_with(WATCH, "web_embedded", None, info["url"])


class TestWarmUp(_FastClientState):

    def test_a_fast_client_url_gets_one_probe(self):
        ok, session = _warm([403] * 40, url=_gv(FAST))
        self.assertFalse(ok)
        self.assertEqual(len(session.requests), 1)
        ok, _ = _warm([200], url=_gv(FAST))
        self.assertTrue(ok)

    def test_other_urls_keep_the_ladder(self):
        _, session = _warm([403] * 40, url=_gv(WEB))
        self.assertEqual(len(session.requests), len(audio_player._STREAM_SETTLE_BACKOFF) + 1)


# --- play() ------------------------------------------------------------------------------------

class TestPlayAfterARefusal(_FastClientState):

    CONFIG = {"audio": {"sample_rate": 48000, "channels": 2}, "debug": False,
              "youtube": {"client": "web_embedded"}}

    def _play(self, resolves, frames_per_stream, prefetched=None, refuse=True):
        srcs, opened = [], []
        frames = iter(frames_per_stream)

        def ffmpeg(*a, **k):
            srcs.append(_FakeFFmpeg(next(frames)))
            return srcs[-1]

        def open_stream(query, client, cookies_file, info, debug=False):
            opened.append(info["client"])
            if refuse and info["client"] == FAST:
                raise audio_player._StreamRefusedError("HTTP 403 at offset 0")
            return _FakeProc()

        resolve = AsyncMock(side_effect=resolves)
        invalidate = MagicMock(wraps=audio_player.invalidate_resolve_cache)
        vc = _PlayVC()
        error = result = None
        with patch.object(audio_player, "_find_ffmpeg", return_value="ffmpeg"), \
                patch.object(audio_player, "resolve_audio", resolve), \
                patch.object(audio_player, "invalidate_resolve_cache", invalidate), \
                patch.object(audio_player, "_open_audio_stream", side_effect=open_stream), \
                patch("discord.FFmpegPCMAudio", side_effect=ffmpeg), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            player = audio_player.AudioPlayer(self.CONFIG)
            player.set_voice_client(vc)
            try:
                result = asyncio.run(player.play(WATCH, prefetched,
                                                 time.time() if prefetched else 0.0))
            except Exception as e:
                error = e
        return SimpleNamespace(error=error, result=result, vc=vc, opened=opened, player=player,
                               fast_args=[c.kwargs.get("fast") for c in resolve.call_args_list],
                               invalidated=[c.args[0] for c in invalidate.call_args_list],
                               out=out.getvalue())

    def test_a_refused_fresh_url_is_looked_up_again_with_the_configured_client(self):
        r = self._play([_result(FAST), _result(WEB)], [[FRAME]])
        self.assertIsNone(r.error)
        self.assertEqual(r.fast_args, [True, False])
        self.assertEqual(r.opened, [FAST, WEB])
        self.assertEqual(r.invalidated, [WATCH])
        self.assertEqual(r.result["title"], WEB)
        self.assertEqual(len(r.vc.played), 1)
        self.assertEqual(list(audio_player._fast_client_recent), [False], "one failed attempt")
        self.assertLessEqual(_bench_left(), 0)

    def test_a_refused_stale_prefetched_url_counts_nothing(self):
        stale = _result(FAST, minted_ago=10 * 60)
        r = self._play([_result(WEB)], [[FRAME]], prefetched=stale)
        self.assertIsNone(r.error)
        self.assertEqual(r.fast_args, [False])
        self.assertEqual(r.opened, [FAST, WEB])
        self.assertEqual(list(audio_player._fast_client_recent), [])

    def test_no_audio_from_a_fresh_fast_stream_takes_the_same_route(self):
        r = self._play([_result(FAST), _result(WEB)], [[], [FRAME]], refuse=False)
        self.assertIsNone(r.error)
        self.assertEqual(r.fast_args, [True, False])
        self.assertEqual(r.result["title"], WEB)
        self.assertEqual(list(audio_player._fast_client_recent), [False])

    def test_no_audio_from_a_prefetched_fast_stream_goes_to_the_configured_client(self):
        """The play command hands play() its own fresh lookup as a prefetched result: one more
        visionos lookup would only cost another wait before the fallback."""
        r = self._play([_result(WEB)], [[], [FRAME]], prefetched=_result(FAST), refuse=False)
        self.assertIsNone(r.error)
        self.assertEqual(r.fast_args, [False])
        self.assertEqual(r.opened, [FAST, WEB])
        self.assertEqual(r.result["title"], WEB)
        self.assertEqual(list(audio_player._fast_client_recent), [False], "fresh, so it counts")
        self.assertIn("gave no audio", r.out)

    def test_no_audio_from_a_stale_prefetched_fast_stream_counts_nothing(self):
        r = self._play([_result(WEB)], [[], [FRAME]],
                       prefetched=_result(FAST, minted_ago=10 * 60), refuse=False)
        self.assertIsNone(r.error)
        self.assertEqual(r.fast_args, [False])
        self.assertEqual(r.opened, [FAST, WEB])
        self.assertEqual(list(audio_player._fast_client_recent), [])
        self.assertIn("stale, not counted", r.out)

    def test_no_audio_from_another_fresh_stream_is_still_a_failure(self):
        r = self._play([_result(WEB)], [[]], refuse=False)
        self.assertIsInstance(r.error, audio_player.StreamFailedError)
        self.assertEqual(r.fast_args, [True])

    def test_refused_on_every_try_is_a_counted_failure(self):
        """A lookup can still hand back a fast-client URL (another guild's lookup in flight)."""
        r = self._play([_result(FAST), _result(FAST)], [],
                       prefetched=_result(FAST, minted_ago=10 * 60))
        self.assertIsInstance(r.error, audio_player.StreamFailedError)
        self.assertEqual(r.fast_args, [False, False])
        self.assertEqual(r.vc.played, [])
        self.assertIsNone(r.player._ytdlp_proc)
        self.assertFalse(r.player.is_playing)


class TestSuccessIsAStreamThatPlays(_FastClientState):
    """A fast-client success is its stream giving a first frame, never a lookup that returned."""

    CONFIG = TestPlayAfterARefusal.CONFIG
    _play = TestPlayAfterARefusal._play

    def test_a_fast_client_stream_that_plays_records_one_success(self):
        for name, resolves, prefetched in (("fresh", [_result(FAST)], None),
                                           ("prefetched", [], _result(FAST))):
            with self.subTest(name):
                _reset()
                _enable_fast_client()
                r = self._play(resolves, [[FRAME]], prefetched=prefetched, refuse=False)
                self.assertIsNone(r.error)
                self.assertEqual(r.opened, [FAST])
                self.assertEqual(list(audio_player._fast_client_recent), [True])

    def test_lookups_that_answer_do_not_stop_the_bench_escalating(self):
        """android_vr, August 2026: every lookup answered and every stream URL was refused."""
        clients = _Clients(visionos=lambda: _result(FAST), web_embedded=lambda: _result(WEB))

        def resolve(query, client, debug=False, cookies_file=None, fast=False):
            return audio_player._resolve_gated(query, client, debug, cookies_file, fast)

        def two_refused_plays():
            for _ in range(2):
                r = self._play(resolve, [[FRAME]])
                self.assertIsNone(r.error)
                self.assertEqual(r.opened, [FAST, WEB])
            return r.out

        with patch.object(audio_player, "_resolve_audio_url", clients):
            two_refused_plays()
            self.assertAlmostEqual(_bench_left(), audio_player._FAST_BENCHES[0], delta=5)
            TestBench._expire()
            out = two_refused_plays()
        self.assertAlmostEqual(_bench_left(), audio_player._FAST_BENCHES[1], delta=5)
        self.assertEqual(audio_player._fast_client_benches, 2)
        self.assertIn("(2 bench(es) in a row)", out)
        self.assertNotIn("back in use", out)
        self.assertEqual(clients.asked, ["visionos", "web_embedded"] * 4)

    def test_after_a_bench_a_stream_that_plays_brings_it_back_and_a_lookup_does_not(self):
        _quiet(audio_player._note_fast_attempt, False, "refused", bench_now=True)
        TestBench._expire()
        clients = _Clients(visionos=_result(FAST), web_embedded=_result(WEB))
        out = io.StringIO()
        with patch.object(audio_player, "_resolve_audio_url", clients), \
                contextlib.redirect_stdout(out):
            result = audio_player._resolve_gated(WATCH, "web_embedded", False, None, True)
        self.assertEqual(result["client"], FAST)
        self.assertNotIn("back in use", out.getvalue())
        self.assertEqual(audio_player._fast_client_benches, 1, "a lookup alone resets nothing")

        r = self._play([_result(FAST)], [[FRAME]], refuse=False)
        self.assertIsNone(r.error)
        self.assertEqual(r.out.count("back in use"), 1)
        self.assertEqual(audio_player._fast_client_benches, 0)
        _quiet(audio_player._note_fast_attempt, False, "refused", bench_now=True)
        self.assertAlmostEqual(_bench_left(), audio_player._FAST_BENCHES[0], delta=5)


if __name__ == "__main__":
    unittest.main(verbosity=2)
