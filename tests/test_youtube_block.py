"""Tests for telling a YouTube-wide block apart from a dead track.

"Sign in to confirm you're not a bot", YouTube's rate limit and an IP block are verdicts on
THIS SERVER, not on one video: every other track fails the same way until the flag decays.
They used to share the "permanent" bucket with removed and private videos, so auto-next
skipped them without counting and moved straight on. With loop off every guild emptied its
queue in seconds; under loop track / loop queue / autoplay the chain never ended, resolving
back to back against an IP YouTube had already flagged.

What these tests pin: per-track errors still skip quietly. Systemic ones keep the track,
count toward the breaker, wait, and hold every guild off YouTube for a while. A stream that
yields no audio at all counts as a failure instead of a 0-second "play".

The error strings are what yt-dlp 2026.07.04 actually produces (captured offline by raising
the YouTube extractor's own exceptions through YoutubeDL), not paraphrases.
"""
import asyncio
import os
import sys
import time
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import audio_player
import commands
from audio_player import _is_retryable_ytdlp_error, is_permanent_resolve_error
from track_queue import Track, TrackQueue
from yt_dlp.utils import DownloadError

def _systemic(exc):
    return audio_player.is_systemic_resolve_error(exc)


def _blocked(retry_after=60.0, probing=False):
    return audio_player.YouTubeBlockedError(retry_after, probing=probing)


def _remaining():
    return audio_player.youtube_block_remaining()


_COOKIE_HINT = (
    "Use --cookies-from-browser or --cookies for the authentication. See  "
    "https://github.com/yt-dlp/yt-dlp/wiki/FAQ#how-do-i-pass-cookies-to-yt-dlp  for how to "
    "manually pass cookies. Also see  https://github.com/yt-dlp/yt-dlp/wiki/Extractors"
    "#exporting-youtube-cookies  for tips on effectively exporting YouTube cookies")

# --- IP- or session-wide: YouTube refusing this server ---------------------------
BOT_CHECK = DownloadError(   # YouTube writes "you’re" with a curly apostrophe
    f"ERROR: [youtube] abc123: Sign in to confirm you’re not a bot. {_COOKIE_HINT}")
BOT_CHECK_STRAIGHT = DownloadError(
    f"ERROR: [youtube] abc123: Sign in to confirm you're not a bot. {_COOKIE_HINT}")
RATE_LIMITED = DownloadError(
    "ERROR: [youtube] abc123: This content isn't available, try again later. The current "
    "session has been rate-limited by YouTube for up to an hour. It is recommended to use "
    "`-t sleep` to add a delay between video requests to avoid exceeding the rate limit. For "
    "more information, refer to  https://github.com/yt-dlp/yt-dlp/wiki/Extractors"
    "#this-content-isnt-available-try-again-later")
TRY_AGAIN_LATER = DownloadError(   # bare reason: yt-dlp only rewrites the straight apostrophe
    "ERROR: [youtube] abc123: This content isn’t available, try again later.")
IP_BLOCKED = DownloadError(
    "ERROR: [youtube] abc123: All player responses are invalid. "
    "Your IP is likely being blocked by Youtube")
CAPTCHA = DownloadError(   # carries "Video unavailable" too — the block must win
    "ERROR: [youtube] abc123: Video unavailable. "
    "YouTube is requiring a captcha challenge before playback")
YOUTUBE_403 = DownloadError(   # an unavailable video is a 200; a 403 refuses the request
    "ERROR: [youtube] abc123: Unable to download API page: HTTP Error 403: Forbidden "
    "(caused by <HTTPError 403: Forbidden>)")

SYSTEMIC = {
    "bot check": BOT_CHECK, "bot check, straight apostrophe": BOT_CHECK_STRAIGHT,
    "rate limit": RATE_LIMITED, "try again later": TRY_AGAIN_LATER,
    "IP block": IP_BLOCKED, "captcha": CAPTCHA, "YouTube 403": YOUTUBE_403,
}

# --- Per-track: a fact about one video --------------------------------------------
REMOVED = DownloadError(
    "ERROR: [youtube] DSa3ttNsI9U: Video unavailable. "
    "This video has been removed by the uploader")
PRIVATE = DownloadError(
    "ERROR: [youtube] abc123: Private video. Sign in if you've been granted access to this "
    f"video. {_COOKIE_HINT}")
AGE_GATED = DownloadError(   # shares "Sign in to confirm" with the bot check
    "ERROR: [youtube] abc123: Sign in to confirm your age. This video may be inappropriate "
    f"for some users. {_COOKIE_HINT}")
GEO_BLOCKED = DownloadError(
    "ERROR: [youtube] abc123: The uploader has not made this video available in your "
    "country.\nThis video is available in Germany.\nYou might want to use a VPN or a proxy "
    "server (with --proxy) to workaround.")
SOUNDCLOUD_403 = DownloadError(   # another site's 403 is about that one track
    "ERROR: [soundcloud] 123456: Unable to download JSON metadata: HTTP Error 403: Forbidden "
    "(caused by <HTTPError 403: Forbidden>)")

PER_TRACK = {
    "removed": REMOVED, "private": PRIVATE, "age-gated": AGE_GATED,
    "geo-blocked": GEO_BLOCKED, "another site's 403": SOUNDCLOUD_403,
}

TOO_MANY_REQUESTS = DownloadError("ERROR: [youtube] abc123: HTTP Error 429: Too Many Requests")

# What a CDN that refuses every fresh URL looks like by the time it reaches auto-next.
DEAD_STREAM_TEXT = ("audio stream failed to open (1) before playback started: "
                    "ERROR: unable to download video data: HTTP Error 403: Forbidden")

MAX_CONSECUTIVE_ERRORS = 3   # the breaker inside commands._auto_next


def _info(title="Resolved", video_id="vid"):
    return {
        "url": (f"https://r.googlevideo.com/videoplayback?expire={int(time.time()) + 6 * 3600}"
                f"&id={video_id}"),
        "title": title, "thumbnail": "", "webpage_url": "", "artist": "", "duration": 1,
        "is_audio_only": True, "protocol": "https", "is_live": False,
    }


def _tracks(n):
    return [Track(query=f"t{i}", title=f"t{i}", requested_by="u") for i in range(n)]


def _reset_block():
    audio_player._yt_block_until = 0.0
    audio_player._yt_block_strikes = 0
    audio_player._yt_block_probing = False


class _CleanBlockState(unittest.TestCase):
    """The block is process-wide on purpose, so one leaking out of a test would fail
    unrelated tests that happen to resolve something. Start and end with YouTube answering."""

    def setUp(self):
        _reset_block()
        self.addCleanup(_reset_block)
        audio_player._resolve_cache.clear()
        audio_player._inflight_resolves.clear()
        self.addCleanup(audio_player._resolve_cache.clear)
        self.addCleanup(audio_player._inflight_resolves.clear)


# --- Classification -----------------------------------------------------------------

class TestClassification(unittest.TestCase):

    def test_blocks_are_systemic(self):
        for name, exc in SYSTEMIC.items():
            with self.subTest(name):
                self.assertTrue(_systemic(exc))

    def test_blocks_are_not_dead_tracks(self):
        """'Permanent' lets auto-next skip without counting and prefetch drop the track,
        which is exactly what emptied every queue during a block."""
        for name, exc in SYSTEMIC.items():
            with self.subTest(name):
                self.assertFalse(is_permanent_resolve_error(exc))

    def test_blocks_are_not_retried_inside_the_resolve(self):
        """Each retry is one more request to an IP YouTube has already flagged."""
        for name, exc in SYSTEMIC.items():
            with self.subTest(name):
                self.assertFalse(_is_retryable_ytdlp_error(exc))

    def test_dead_tracks_stay_dead_tracks(self):
        for name, exc in PER_TRACK.items():
            with self.subTest(name):
                self.assertTrue(is_permanent_resolve_error(exc))
                self.assertFalse(_systemic(exc))

    def test_age_gate_is_not_mistaken_for_the_bot_check(self):
        """Both begin "Sign in to confirm" — only "not a bot" tells them apart."""
        self.assertFalse(_systemic(AGE_GATED))
        self.assertTrue(_systemic(BOT_CHECK))

    def test_transient_errors_are_neither(self):
        for exc in (TOO_MANY_REQUESTS, ConnectionError("reset"),
                    DownloadError("ERROR: something odd")):
            with self.subTest(str(exc)[:40]):
                self.assertFalse(_systemic(exc))
                self.assertFalse(is_permanent_resolve_error(exc))

    def test_the_cooldowns_own_refusal_is_systemic(self):
        self.assertTrue(_systemic(_blocked()))
        self.assertFalse(is_permanent_resolve_error(_blocked()))

    def test_a_stream_with_no_audio_is_a_plain_failure(self):
        """The subprocess's stderr can say "Forbidden". That must not turn a CDN refusal
        into a quiet per-track skip, and it is not a verdict on the IP either."""
        exc = audio_player.StreamFailedError(DEAD_STREAM_TEXT)
        self.assertFalse(is_permanent_resolve_error(exc))
        self.assertFalse(_systemic(exc))


class TestBlocksAreNotRetriedInPlace(unittest.TestCase):

    def test_one_attempt_and_no_sleep(self):
        for name, exc in SYSTEMIC.items():
            with self.subTest(name):
                calls = []

                def fn(*a, **k):
                    calls.append(1)
                    raise exc

                with patch.object(audio_player.time, "sleep") as sleep:
                    with self.assertRaises(DownloadError):
                        audio_player._retry_with_backoff(fn, max_attempts=3, base_delay=5.0)
                self.assertEqual(len(calls), 1)
                sleep.assert_not_called()


# --- The shared cooldown --------------------------------------------------------------

class TestSharedCooldown(_CleanBlockState):
    """A block one guild runs into holds every guild off YouTube until it has cleared."""

    URL_A = "https://www.youtube.com/watch?v=aaaaaaaaaaa"
    URL_B = "https://www.youtube.com/watch?v=bbbbbbbbbbb"

    @staticmethod
    def _resolve(query, resolver):
        with patch.object(audio_player, "_resolve_audio_url", resolver):
            return audio_player.get_audio_url(query, "web_embedded")

    def _trip(self, exc=BOT_CHECK):
        with self.assertRaises(DownloadError):
            self._resolve(self.URL_A, MagicMock(side_effect=exc))

    @staticmethod
    def _cooldown_runs_out():
        audio_player._yt_block_until = time.monotonic() - 1

    def test_one_refusal_holds_every_youtube_resolve_off(self):
        self._trip()
        resolver = MagicMock(return_value=_info())
        for query in (self.URL_B, "some search text"):
            with self.subTest(query):
                with self.assertRaises(audio_player.YouTubeBlockedError):
                    self._resolve(query, resolver)
        resolver.assert_not_called()
        self.assertGreater(_remaining(), 0)

    def test_every_kind_of_block_starts_the_cooldown(self):
        for name, exc in SYSTEMIC.items():
            with self.subTest(name):
                _reset_block()
                self._trip(exc)
                self.assertGreater(_remaining(), 0)

    def test_the_refusal_says_when_to_try_again(self):
        self.assertIn("try again in about 3 min", str(_blocked(150.0)))
        self.assertIn("try again in about 1 min", str(_blocked(5.0)))

    def test_a_held_off_resolve_does_not_sleep_or_retry(self):
        self._trip()
        resolver = MagicMock(return_value=_info())
        with patch.object(audio_player, "_resolve_audio_url", resolver), \
                patch.object(audio_player.time, "sleep") as sleep:
            with self.assertRaises(audio_player.YouTubeBlockedError):
                audio_player.get_audio_url_with_retry(self.URL_B, "web_embedded")
        resolver.assert_not_called()
        sleep.assert_not_called()

    def test_other_sites_are_not_held_off(self):
        self._trip()
        resolver = MagicMock(return_value=_info())
        self._resolve("https://soundcloud.com/artist/track", resolver)
        resolver.assert_called_once()

    def test_cached_tracks_still_play(self):
        """A cache hit needs no request, so a block is no reason to refuse it."""
        audio_player._resolve_cache_put("bbbbbbbbbbb", _info())
        self._trip()
        resolver = MagicMock()
        self.assertEqual(self._resolve(self.URL_B, resolver)["title"], "Resolved")
        resolver.assert_not_called()

    def test_dead_tracks_and_transient_errors_start_no_cooldown(self):
        for exc in (REMOVED, GEO_BLOCKED, TOO_MANY_REQUESTS):
            with self.subTest(str(exc)[:50]):
                self._trip(exc)
                self.assertEqual(_remaining(), 0)

    def test_once_it_runs_out_one_resolve_probes_and_the_rest_wait(self):
        self._trip()
        self._cooldown_runs_out()
        audio_player._youtube_gate()                      # the probe
        with self.assertRaises(audio_player.YouTubeBlockedError) as ctx:
            audio_player._youtube_gate()                  # everyone else
        self.assertTrue(ctx.exception.probing)
        self.assertLessEqual(ctx.exception.retry_after, audio_player._YT_PROBE_WINDOW)

    def test_a_probe_that_gets_through_lifts_the_block(self):
        self._trip()
        self._cooldown_runs_out()
        resolver = MagicMock(return_value=_info())
        self._resolve(self.URL_B, resolver)
        self.assertEqual(_remaining(), 0)
        audio_player._youtube_gate()
        audio_player._youtube_gate()                      # no probe window any more

    def test_a_probe_answered_about_one_video_lifts_the_block(self):
        """"Video unavailable" means YouTube answered — it is not refusing us."""
        self._trip()
        self._cooldown_runs_out()
        with self.assertRaises(DownloadError):
            self._resolve(self.URL_B, MagicMock(side_effect=REMOVED))
        self.assertEqual(_remaining(), 0)

    def test_a_refused_probe_lengthens_the_cooldown(self):
        self._trip()
        first = _remaining()
        self._cooldown_runs_out()
        self._trip()
        self.assertGreater(_remaining(), first + 1)

    def test_resolves_already_in_flight_count_once(self):
        """Several guilds' resolves can be mid-flight when the block lands. They all fail
        the same way, and that is one block, not five escalations."""
        for _ in range(5):
            audio_player._youtube_note_outcome(BOT_CHECK)
        self.assertLessEqual(_remaining(), audio_player._YT_BLOCK_COOLDOWNS[0])

    def test_a_success_from_before_the_block_does_not_lift_it(self):
        """A resolve admitted before the block can still finish fine afterwards; that
        says nothing about whether YouTube is answering now."""
        audio_player._youtube_note_outcome(BOT_CHECK)
        audio_player._youtube_note_outcome(None)
        self.assertGreater(_remaining(), 0)


# --- Auto-next ------------------------------------------------------------------------

class _Spin(BaseException):
    """Raised by the fake player once auto-next has clearly lost control. A BaseException,
    so _auto_next's own `except Exception` cannot swallow it as one more strike."""


class _VC:
    channel = SimpleNamespace(members=[SimpleNamespace(bot=False)])

    def is_connected(self):
        return True

    def is_playing(self):
        return False

    def is_paused(self):
        return False


class _ScriptedPlayer:
    """Fake AudioPlayer whose play() follows a script: an exception to raise, or None to
    succeed. Once the script runs out its last entry repeats."""

    def __init__(self, *script):
        self.script = list(script)
        self.calls = []
        self.is_playing = False
        self._voice_client = _VC()

    async def play(self, query, resolved_info, resolved_at):
        self.calls.append(query)
        if len(self.calls) > 20:
            raise _Spin(f"auto-next is spinning: {len(self.calls)} play() calls")
        outcome = self.script[min(len(self.calls), len(self.script)) - 1]
        if outcome is not None:
            raise outcome
        return {"title": query, "thumbnail": "", "webpage_url": ""}

    async def wait_for_playback(self):
        return

    def set_voice_client(self, vc):
        self._voice_client = vc

    def stop_playback(self):
        pass


class _GS:
    def __init__(self, player):
        self.queue = TrackQueue()
        self.player = player
        self.auto_next_task = None
        self.auto_next_gen = 1
        self.autoplay = False
        self.prefetch_task = None
        self.autoplay_task = None
        self.autoplay_pool = []
        self.autoplay_history = set()
        self.current_text_channel_id = 10
        self.fairness_pct = 50


class _Channel:
    def __init__(self):
        self.sent = []

    async def send(self, content, **kwargs):
        self.sent.append(content)


class _Bot:
    config = {"debug": False}

    def __init__(self, gs):
        self._gs = gs
        self._guild = SimpleNamespace(voice_client=gs.player._voice_client)
        self.channel = _Channel()

    def get_guild_state(self, guild_id):
        return self._gs

    def get_guild(self, guild_id):
        return self._guild

    def get_channel(self, channel_id):
        return self.channel


class _AutoNextHarness(unittest.TestCase):
    """Runs the real _auto_next against a fake player. The now-playing card, prefetch,
    autoplay and the block wait are faked out, and the waits and picks are recorded."""

    def setUp(self):
        self.waits = []
        self.autoplay_picks = []

        async def _record_wait(gs, generation, delay):
            self.waits.append(delay)
            return True

        async def _record_pick(*a, **k):
            self.autoplay_picks.append(a)
            return None

        patches = [
            patch.object(commands, "build_player_view", return_value=None),
            patch.object(commands, "_get_requester_name", return_value="someone"),
            patch.object(commands, "send_new_np", side_effect=self._noop),
            patch.object(commands, "update_np_stopped", side_effect=self._noop),
            patch.object(commands, "_schedule_prefetch", return_value=None),
            patch.object(commands, "_schedule_autoplay_topup", return_value=None),
            patch.object(commands, "_autoplay_pick", _record_pick),
            patch.object(commands, "_wait_out_block", _record_wait),
            patch.object(commands, "youtube_block_remaining", return_value=0.0),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    @staticmethod
    async def _noop(*a, **k):
        return None

    @staticmethod
    def _run(player, tracks, loop_mode="off", autoplay=False, current=None):
        gs = _GS(player)
        gs.queue.loop_mode = loop_mode
        gs.autoplay = autoplay
        gs.queue.current = current
        for t in tracks:
            gs.queue.add(t)
        bot = _Bot(gs)
        asyncio.run(commands._auto_next(bot, 10, 1, 1))
        return gs, bot.channel.sent

    @staticmethod
    def _count(sent, needle):
        return sum(needle in m.lower() for m in sent)


class TestAutoNextDuringABlock(_AutoNextHarness):

    def test_a_block_loses_no_tracks_and_ends_at_the_breaker(self):
        tracks = _tracks(4)
        player = _ScriptedPlayer(BOT_CHECK)
        gs, sent = self._run(player, tracks)
        self.assertEqual(len(player.calls), MAX_CONSECUTIVE_ERRORS)
        self.assertEqual(gs.queue.list(), tracks, "a block is not the tracks' fault")
        self.assertIsNone(gs.queue.current)
        self.assertEqual(self._count(sent, "skipping track"), 0)
        self.assertEqual(self._count(sent, "stopping auto-play"), 1)

    def test_it_retries_the_same_track_rather_than_walking_the_queue(self):
        player = _ScriptedPlayer(RATE_LIMITED)
        self._run(player, _tracks(3))
        self.assertEqual(player.calls, ["t0"] * MAX_CONSECUTIVE_ERRORS)

    def test_it_waits_between_attempts(self):
        self._run(_ScriptedPlayer(BOT_CHECK), _tracks(2))
        self.assertEqual(len(self.waits), MAX_CONSECUTIVE_ERRORS - 1,
                         "one wait between attempts, none after giving up")
        for w in self.waits:
            self.assertGreaterEqual(w, commands._SYSTEMIC_RETRY_FLOOR)

    def test_the_wait_covers_the_shared_cooldown(self):
        with patch.object(commands, "youtube_block_remaining", return_value=600.0):
            self._run(_ScriptedPlayer(BOT_CHECK), _tracks(2))
        self.assertTrue(self.waits)
        for w in self.waits:
            self.assertGreaterEqual(w, 600.0)

    def test_loop_modes_stop_instead_of_spinning(self):
        """The prod failure: a bot check under loop replayed the same doomed resolve forever."""
        for mode in ("track", "queue"):
            with self.subTest(mode):
                self.waits.clear()
                playing = Track(query="now", title="now", requested_by="u")
                player = _ScriptedPlayer(BOT_CHECK)
                gs, sent = self._run(player, _tracks(2), loop_mode=mode, current=playing)
                self.assertEqual(len(player.calls), MAX_CONSECUTIVE_ERRORS)
                self.assertEqual(self._count(sent, "stopping auto-play"), 1)
                queued = [t.query for t in gs.queue.list()]
                self.assertIn("now", queued, "the looping track must survive the block")

    def test_autoplay_stops_instead_of_spinning(self):
        player = _ScriptedPlayer(TRY_AGAIN_LATER)
        gs, sent = self._run(player, _tracks(1), autoplay=True)
        self.assertEqual(len(player.calls), MAX_CONSECUTIVE_ERRORS)
        self.assertEqual(self.autoplay_picks, [], "nothing ran out, so nothing to top up")
        self.assertEqual([t.query for t in gs.queue.list()], ["t0"])

    def test_playback_resumes_when_the_block_lifts(self):
        player = _ScriptedPlayer(BOT_CHECK, None)
        gs, sent = self._run(player, _tracks(2))
        self.assertEqual(player.calls, ["t0", "t0", "t1"])
        self.assertEqual(self._count(sent, "stopping auto-play"), 0)
        self.assertEqual(len(gs.queue), 0)

    def test_the_breaker_resets_after_a_good_play(self):
        player = _ScriptedPlayer(BOT_CHECK, BOT_CHECK, None, BOT_CHECK, BOT_CHECK, None)
        gs, sent = self._run(player, _tracks(2))
        self.assertEqual(player.calls, ["t0", "t0", "t0", "t1", "t1", "t1"])
        self.assertEqual(self._count(sent, "stopping auto-play"), 0)

    def test_users_hear_about_it_once_and_never_as_a_skip(self):
        gs, sent = self._run(_ScriptedPlayer(BOT_CHECK), _tracks(2))
        self.assertEqual(len(sent), 2, f"one heads-up and one give-up, got {sent}")
        self.assertIn("nothing was skipped", sent[0].lower())
        self.assertIn("queue is still here", sent[1].lower())

    def test_waiting_on_another_guilds_probe_costs_no_strike(self):
        probing = _blocked(5.0, probing=True)
        player = _ScriptedPlayer(probing, probing, probing, probing, None)
        gs, sent = self._run(player, _tracks(1))
        self.assertEqual(player.calls, ["t0"] * 5)
        self.assertEqual(self._count(sent, "stopping auto-play"), 0)

    def test_a_stop_during_the_wait_exits_quietly(self):
        async def _superseded(gs, generation, delay):
            gs.auto_next_gen = generation + 1      # !!stop / a fresh !!play
            return False

        with patch.object(commands, "_wait_out_block", _superseded):
            player = _ScriptedPlayer(BOT_CHECK)
            gs, sent = self._run(player, _tracks(2))
        self.assertEqual(player.calls, ["t0"])
        self.assertEqual([t.query for t in gs.queue.list()], ["t0", "t1"])
        self.assertEqual(self._count(sent, "stopping auto-play"), 0)

    def test_dead_tracks_are_still_skipped_without_counting(self):
        player = _ScriptedPlayer(REMOVED, AGE_GATED, GEO_BLOCKED, PRIVATE, None)
        gs, sent = self._run(player, _tracks(5))
        self.assertEqual(player.calls, ["t0", "t1", "t2", "t3", "t4"])
        self.assertEqual(self._count(sent, "skipping track"), 4)
        self.assertEqual(self._count(sent, "consecutive errors"), 0)
        self.assertEqual(self.waits, [], "a dead track is no reason to wait")

    def test_a_stream_with_no_audio_counts_toward_the_breaker(self):
        tracks = _tracks(4)
        player = _ScriptedPlayer(audio_player.StreamFailedError(DEAD_STREAM_TEXT))
        gs, sent = self._run(player, tracks)
        self.assertEqual(len(player.calls), MAX_CONSECUTIVE_ERRORS)
        self.assertEqual(self._count(sent, "too many consecutive errors"), 1)
        self.assertEqual(gs.queue.list(), [tracks[3]])


class _DeadTrackPlayer(_ScriptedPlayer):
    """Fake player for a queue with dead tracks in it. Every play of a dead track fails
    with the same per-track error, the way YouTube answers a removed or private video every
    time, and every other track plays. A healthy loop never ends by itself, so once `plays`
    tracks have played the chain is cancelled, the way !!stop cancels it."""

    def __init__(self, dead, plays=0, error=REMOVED):
        super().__init__()
        self.dead, self.plays, self.error = set(dead), plays, error
        self.played = []

    async def play(self, query, resolved_info, resolved_at):
        self.calls.append(query)
        if len(self.calls) > 20:
            raise _Spin(f"auto-next is spinning: {self.calls}")
        if query in self.dead:
            raise self.error
        self.played.append(query)
        return {"title": query, "thumbnail": "", "webpage_url": ""}

    async def wait_for_playback(self):
        if self.plays and len(self.played) >= self.plays:
            raise asyncio.CancelledError   # !!stop


class TestDeadTracksUnderLoop(_AutoNextHarness):
    """A removed, private, age-gated or geo-blocked track fails the same way every time.
    Loop "track" used to hand the same dead track straight back to play(), and loop "queue"
    cycled it round to fail again every lap: one YouTube request and one "Skipping track"
    message per turn, forever. A loop has to let go of it and move on, still without
    counting it toward the breaker."""

    @staticmethod
    def _on_repeat():
        return Track(query="now", title="now", requested_by="u")

    def test_loop_track_moves_past_a_track_that_died(self):
        """The track on repeat was taken down mid-loop."""
        for name, error in PER_TRACK.items():
            with self.subTest(name):
                player = _DeadTrackPlayer({"now"}, plays=2, error=error)
                gs, sent = self._run(player, _tracks(2), loop_mode="track",
                                     current=self._on_repeat())
                self.assertEqual(player.calls, ["now", "t0", "t0"],
                                 "one try at the dead track, then the next one, on repeat")
                self.assertEqual(self._count(sent, "skipping track"), 1)
                self.assertEqual([t.query for t in gs.queue.list()], ["t1"])

    def test_a_loop_of_nothing_but_dead_tracks_ends(self):
        for mode in ("track", "queue"):
            for shape, queued, current in (("the track on repeat", [], self._on_repeat()),
                                           ("a queue of them", _tracks(3), None)):
                with self.subTest(loop=mode, dead=shape):
                    commands.update_np_stopped.reset_mock()
                    dead = [t.query for t in ([current] if current else []) + queued]
                    player = _DeadTrackPlayer(dead)
                    gs, sent = self._run(player, queued, loop_mode=mode, current=current)
                    self.assertEqual(player.calls, dead, "each dead track is tried once")
                    self.assertEqual(self._count(sent, "skipping track"), len(dead))
                    self.assertEqual(gs.queue.list(), [])
                    self.assertIsNone(gs.queue.current)
                    commands.update_np_stopped.assert_awaited_once()

    def test_dead_tracks_never_count_toward_the_breaker(self):
        """More dead tracks in a row than the breaker allows, then a good one."""
        for mode in ("track", "queue"):
            with self.subTest(loop=mode):
                tracks = _tracks(MAX_CONSECUTIVE_ERRORS + 2)
                dead = [t.query for t in tracks[:-1]]
                player = _DeadTrackPlayer(dead, plays=1)
                gs, sent = self._run(player, tracks, loop_mode=mode)
                self.assertEqual(player.calls, dead + [tracks[-1].query])
                self.assertEqual(self._count(sent, "consecutive errors"), 0)
                self.assertEqual(self.waits, [], "a dead track is no reason to wait")

    def test_loop_queue_drops_a_dead_track_and_keeps_the_rest_cycling(self):
        player = _DeadTrackPlayer({"t1"}, plays=5)
        gs, sent = self._run(player, _tracks(3), loop_mode="queue")
        self.assertEqual(player.played, ["t0", "t2", "t0", "t2", "t0"])
        self.assertEqual(player.calls.count("t1"), 1, "not retried every lap")
        self.assertEqual(self._count(sent, "skipping track"), 1)
        self.assertNotIn("t1", [t.query for t in gs.queue.list()])

    def test_loop_off_still_consumes_a_dead_track_as_before(self):
        """Loop "off" is untouched: the dead track stays current until next() moves on,
        so when it was the last one, autoplay still seeds from it."""
        player = _DeadTrackPlayer({"t0"})
        gs, sent = self._run(player, _tracks(1), autoplay=True)
        self.assertEqual(player.calls, ["t0"])
        self.assertEqual([pick[2].query for pick in self.autoplay_picks], ["t0"])


class TestWaitOutBlock(unittest.TestCase):

    def test_sleeps_the_whole_delay(self):
        gs = SimpleNamespace(auto_next_gen=1)
        with patch.object(commands, "_BLOCK_WAIT_POLL", 0.01):
            started = time.monotonic()
            self.assertTrue(asyncio.run(commands._wait_out_block(gs, 1, 0.05)))
        self.assertGreaterEqual(time.monotonic() - started, 0.05)

    def test_a_newer_chain_cuts_it_short(self):
        gs = SimpleNamespace(auto_next_gen=1)

        async def run():
            async def supersede():
                await asyncio.sleep(0.02)
                gs.auto_next_gen = 2

            task = asyncio.create_task(supersede())
            result = await commands._wait_out_block(gs, 1, 30.0)
            await task
            return result

        with patch.object(commands, "_BLOCK_WAIT_POLL", 0.01):
            started = time.monotonic()
            self.assertFalse(asyncio.run(run()))
        self.assertLess(time.monotonic() - started, 5.0)


# --- Prefetch -------------------------------------------------------------------------

class _PrefetchGS:
    def __init__(self):
        self.queue = TrackQueue()
        self.prefetch_task = None


class _PrefetchBot:
    config = {"youtube": {"client": "web"}, "debug": False}

    def __init__(self):
        self._gs = _PrefetchGS()

    def get_guild_state(self, guild_id):
        return self._gs


class TestPrefetchKeepsTracksThroughABlock(unittest.TestCase):

    def setUp(self):
        commands._last_prefetch_monotonic = 0.0
        for p in (patch.object(commands, "warm_stream_url", return_value=True),
                  patch("asyncio.sleep", self._no_sleep)):
            p.start()
            self.addCleanup(p.stop)

    @staticmethod
    async def _no_sleep(seconds):
        return None

    def _prefetch(self, exc):
        bot = _PrefetchBot()
        tracks = _tracks(3)
        for t in tracks:
            bot._gs.queue.add(t)
        calls = []

        def resolve(query, *a, **k):
            calls.append(query)
            raise exc

        with patch.object(commands, "get_audio_url_with_retry", side_effect=resolve):
            asyncio.run(commands._prefetch_next_track(bot, 1))
        return bot._gs.queue.list(), tracks, calls

    def test_a_block_never_evicts_a_track(self):
        for name, exc in SYSTEMIC.items():
            with self.subTest(name):
                queued, tracks, calls = self._prefetch(exc)
                self.assertEqual(queued, tracks)
                self.assertEqual(calls, ["t0"], "one refusal ends the pass")

    def test_the_cooldowns_refusal_never_evicts_a_track(self):
        queued, tracks, calls = self._prefetch(_blocked())
        self.assertEqual(queued, tracks)
        self.assertEqual(calls, ["t0"])


# --- A stream that yields no audio ----------------------------------------------------

VID = "zzzzzzzzzzz"
WATCH = f"https://www.youtube.com/watch?v={VID}"
FRAME = b"\x01" * 3840


class _FakeProc:
    """The handle _open_audio_stream returns, in the in-process reader's shape."""

    def __init__(self, exit_code=None, error_text=""):
        self._exit = exit_code
        self.error_text = error_text
        self.stdout = object()
        self.stderr = None
        self.pid = -1
        self.terminated = False

    def poll(self):
        return self._exit

    def terminate(self):
        self.terminated = True

    kill = terminate

    def wait(self, timeout=None):
        return 0


class _FakeFFmpeg:
    """discord.FFmpegPCMAudio stand-in: hands out the given PCM frames, then EOF."""

    def __init__(self, frames):
        self._frames = list(frames)
        self._process = SimpleNamespace(stderr=[])
        self.cleaned_up = False

    def read(self):
        return self._frames.pop(0) if self._frames else b""

    def is_opus(self):
        return False

    def cleanup(self):
        self.cleaned_up = True


class _PlayVC:
    def __init__(self):
        self.played = []
        self.guild = SimpleNamespace(id=1)

    def is_connected(self):
        return True

    def is_playing(self):
        return False

    def is_paused(self):
        return False

    def stop(self):
        pass

    def play(self, source, after=None, **encoder_settings):
        self.played.append(source)


class TestStreamWithNoAudio(_CleanBlockState):

    CONFIG = {"audio": {"sample_rate": 48000, "channels": 2}, "debug": False,
              "youtube": {"client": "web_embedded"}}

    def _play(self, *frames_per_attempt, proc=None, prefetched=None):
        srcs, procs = [], []
        frames = iter(frames_per_attempt)

        def ffmpeg(*a, **k):
            srcs.append(_FakeFFmpeg(next(frames)))
            return srcs[-1]

        def open_stream(*a, **k):
            procs.append(proc() if proc else _FakeProc())
            return procs[-1]

        resolve = MagicMock(return_value=_info())
        vc = _PlayVC()
        error = None
        with patch.object(audio_player, "_find_ffmpeg", return_value="ffmpeg"), \
                patch.object(audio_player, "get_audio_url_with_retry", resolve), \
                patch.object(audio_player, "_open_audio_stream", side_effect=open_stream), \
                patch("discord.FFmpegPCMAudio", side_effect=ffmpeg):
            player = audio_player.AudioPlayer(self.CONFIG)
            player.set_voice_client(vc)
            try:
                asyncio.run(player.play(WATCH, prefetched, time.time() if prefetched else 0.0))
            except Exception as e:
                error = e
        return SimpleNamespace(error=error, vc=vc, srcs=srcs, procs=procs,
                               resolve=resolve, player=player)

    def test_audio_plays_normally(self):
        """Control: the harness itself starts playback when there is audio."""
        r = self._play([FRAME])
        self.assertIsNone(r.error)
        self.assertEqual(len(r.vc.played), 1)

    def test_no_audio_from_a_fresh_resolve_is_a_failure(self):
        audio_player._resolve_cache_put(VID, _info())
        r = self._play([])
        self.assertIsInstance(r.error, audio_player.StreamFailedError)
        self.assertFalse(is_permanent_resolve_error(r.error))
        self.assertEqual(r.vc.played, [], "nothing may be handed to the voice client")
        self.assertTrue(r.procs[0].terminated)
        self.assertTrue(r.srcs[0].cleaned_up)
        self.assertIsNone(r.player._ytdlp_proc)
        self.assertIsNone(audio_player._resolve_cache_get(WATCH),
                          "a URL that produced no audio must not be served again")

    def test_a_stream_that_never_opened_is_a_counted_failure(self):
        """Its stderr says "Forbidden"; that used to make it a quiet per-track skip."""
        r = self._play(proc=lambda: _FakeProc(
            exit_code=1, error_text="ERROR: unable to download video data: "
                                    "HTTP Error 403: Forbidden"))
        self.assertIsInstance(r.error, audio_player.StreamFailedError)
        self.assertFalse(is_permanent_resolve_error(r.error))
        self.assertEqual(r.vc.played, [])

    def test_a_dead_cached_url_still_gets_one_fresh_resolve(self):
        r = self._play([], [FRAME], prefetched=_info())
        self.assertIsNone(r.error)
        self.assertEqual(r.resolve.call_count, 1)
        self.assertEqual(len(r.vc.played), 1)

    def test_no_audio_even_after_the_fresh_resolve_is_a_failure(self):
        r = self._play([], [], prefetched=_info())
        self.assertIsInstance(r.error, audio_player.StreamFailedError)
        self.assertEqual(r.resolve.call_count, 1)
        self.assertEqual(r.vc.played, [])


class TestBreakerOrdering(unittest.TestCase):
    """Source-level guard that complements test_dead_track_skip's: a block must be
    counted, and must keep its track."""

    def test_a_block_is_counted_and_requeued(self):
        import inspect
        src = inspect.getsource(commands._auto_next)
        branch = src[src.index("is_systemic_resolve_error(e)"):]
        branch = branch[:branch.index("continue")]
        self.assertIn("requeue_front(next_track)", branch)
        self.assertIn("consecutive_errors += 1", branch)


if __name__ == "__main__":
    unittest.main(verbosity=2)
