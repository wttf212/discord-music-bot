"""Tests for YouTube work scheduling across guilds (resolve lanes).

The resolve pool used to be one FIFO queue shared by every guild and every kind of work:
a user's cold !play could wait behind another guild's prefetches, queue-add lookups, a
retry parked in a multi-second time.sleep, or a ~20-page playlist enumeration. These
tests pin the replacement down:
  * user-facing work is admitted before background work, and background work can never
    fill the resolve pool;
  * retry backoff waits on the event loop, not in a worker;
  * resolve-cache hits are answered without touching the pool;
  * playlist enumeration and autoplay refills run in their own small lane, and a
    superseded enumeration is cancelled instead of paging YouTube for nobody;
  * concurrent prefetches really are spaced by the global floor.
No test here touches the network.
"""
import asyncio
import contextlib
import os
import sys
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import audio_player
import commands
from audio_player import _Lane
from track_queue import Track, TrackQueue
from yt_dlp.utils import DownloadCancelled, DownloadError

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WATCH = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
RATE_LIMITED = DownloadError("ERROR: [youtube] abc: HTTP Error 429: Too Many Requests")
_REAL_SLEEP = asyncio.sleep   # some tests compress the retry backoff by patching asyncio.sleep


def _fresh_info(title="Song"):
    return {
        "url": f"https://r1.googlevideo.com/videoplayback?expire={int(time.time()) + 6 * 3600}",
        "title": title, "thumbnail": "", "webpage_url": "", "is_audio_only": True,
        "protocol": "https", "is_live": False,
    }


async def _until(predicate, timeout=2.0):
    """Yield to the loop until predicate() holds (worker threads report via lists)."""
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("condition never became true")
        await _REAL_SLEEP(0.005)


class _LaneTest(unittest.TestCase):
    """A private pool + lane per test, and a recorder for what ran in which order."""

    def setUp(self):
        self.hold = threading.Event()
        self.ran = []
        self.addCleanup(self.hold.set)   # never leave a worker parked

    def lane(self, workers, background_slots, spare_workers=0):
        """spare_workers > 0 gives the pool more threads than the lane has slots, so a
        test sees what the LANE admits instead of the pool's own serialisation."""
        pool = ThreadPoolExecutor(max_workers=workers + spare_workers,
                                  thread_name_prefix="test-lane")
        self.addCleanup(pool.shutdown, wait=False)
        return _Lane(pool, workers, background_slots)

    def job(self, name, block=False):
        self.ran.append(name)
        if block:
            self.hold.wait(5)
        return name


class TestLaneAdmission(_LaneTest):

    def test_background_work_cannot_fill_the_pool(self):
        """Background may hold only its share of workers, so a user-facing job finds a
        free one even while background work is still queueing."""
        lane = self.lane(workers=2, background_slots=1)

        async def scenario():
            bg1 = asyncio.create_task(lane.run(self.job, "bg1", True, background=True))
            bg2 = asyncio.create_task(lane.run(self.job, "bg2", True, background=True))
            await _until(lambda: "bg1" in self.ran)
            out = await asyncio.wait_for(lane.run(self.job, "user"), 2)
            self.assertEqual(out, "user")
            self.assertNotIn("bg2", self.ran, "bg2 exceeded the background cap")
            self.hold.set()
            await asyncio.gather(bg1, bg2)

        asyncio.run(scenario())
        self.assertEqual(self.ran, ["bg1", "user", "bg2"])

    def test_user_facing_work_is_admitted_before_queued_background_work(self):
        lane = self.lane(workers=1, background_slots=1)

        async def scenario():
            first = asyncio.create_task(lane.run(self.job, "first", True))
            await _until(lambda: "first" in self.ran)
            bg = asyncio.create_task(lane.run(self.job, "background", background=True))
            await asyncio.sleep(0)
            user = asyncio.create_task(lane.run(self.job, "user"))
            await asyncio.sleep(0)
            self.hold.set()
            await asyncio.gather(first, bg, user)

        asyncio.run(scenario())
        self.assertEqual(self.ran, ["first", "user", "background"],
                         "a waiting user-facing job must not queue behind background work")

    def test_fifo_within_a_class(self):
        lane = self.lane(workers=1, background_slots=1)

        async def scenario():
            first = asyncio.create_task(lane.run(self.job, "first", True))
            await _until(lambda: "first" in self.ran)
            rest = []
            for name in ("a", "b", "c"):
                rest.append(asyncio.create_task(lane.run(self.job, name)))
                await asyncio.sleep(0)
            self.hold.set()
            await asyncio.gather(first, *rest)

        asyncio.run(scenario())
        self.assertEqual(self.ran, ["first", "a", "b", "c"])

    def test_slot_is_held_until_the_thread_finishes_even_if_the_caller_gives_up(self):
        """A cancelled await does not stop a running thread. Releasing its slot early
        would let the lane oversubscribe the pool, and queueing inside the pool is
        exactly the FIFO the lane exists to replace."""
        lane = self.lane(workers=1, background_slots=1, spare_workers=1)

        async def scenario():
            abandoned = asyncio.create_task(lane.run(self.job, "abandoned", True))
            await _until(lambda: "abandoned" in self.ran)
            abandoned.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await abandoned
            nxt = asyncio.create_task(lane.run(self.job, "next"))
            await asyncio.sleep(0.05)
            self.assertEqual(self.ran, ["abandoned"], "admitted while the worker was busy")
            self.hold.set()
            self.assertEqual(await asyncio.wait_for(nxt, 2), "next")

        asyncio.run(scenario())

    def test_a_cancelled_waiter_gives_up_its_place(self):
        lane = self.lane(workers=1, background_slots=1)

        async def scenario():
            first = asyncio.create_task(lane.run(self.job, "first", True))
            await _until(lambda: "first" in self.ran)
            gone = asyncio.create_task(lane.run(self.job, "cancelled"))
            await asyncio.sleep(0)
            nxt = asyncio.create_task(lane.run(self.job, "next"))
            await asyncio.sleep(0)
            gone.cancel()
            self.hold.set()
            await asyncio.gather(first, nxt)
            with contextlib.suppress(asyncio.CancelledError):
                await gone

        asyncio.run(scenario())
        self.assertEqual(self.ran, ["first", "next"])

    def test_errors_propagate_and_free_the_slot(self):
        lane = self.lane(workers=1, background_slots=1)

        def boom():
            raise ValueError("resolve failed")

        async def scenario():
            with self.assertRaises(ValueError):
                await lane.run(boom)
            return await asyncio.wait_for(lane.run(self.job, "after"), 2)

        self.assertEqual(asyncio.run(scenario()), "after")

    def test_survives_a_new_event_loop(self):
        """Production has one loop; every asyncio.run() in the test suite is a new one.
        A job abandoned on an old loop must not leak a slot into the next."""
        lane = self.lane(workers=2, background_slots=1)

        async def abandon_one():
            task = asyncio.create_task(lane.run(self.job, "old-loop", True, background=True))
            await _until(lambda: "old-loop" in self.ran)
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

        asyncio.run(abandon_one())

        async def next_loop():
            return await asyncio.wait_for(lane.run(self.job, "new-loop", background=True), 2)

        self.assertEqual(asyncio.run(next_loop()), "new-loop")
        self.hold.set()


class TestResolveLaneSizing(unittest.TestCase):

    def test_resolve_lane_caps_concurrent_deno_solves_at_the_pool_size(self):
        """Every resolve runs Deno; the lane's slots are the pool's workers, so the cap
        on concurrent solves did not grow when work was split into classes."""
        lane = audio_player._RESOLVE_LANE
        self.assertIs(lane._executor, audio_player._RESOLVE_EXECUTOR)
        self.assertEqual(lane._slots, audio_player._RESOLVE_EXECUTOR._max_workers)

    def test_background_resolves_leave_room_for_users(self):
        lane = audio_player._RESOLVE_LANE
        self.assertGreaterEqual(lane._background_slots, 1)
        self.assertLessEqual(lane._background_slots, lane._slots // 2)

    def test_playlist_lane_is_small_and_separate(self):
        lane = audio_player._PLAYLIST_LANE
        self.assertIs(lane._executor, audio_player._PLAYLIST_EXECUTOR)
        self.assertIsNot(audio_player._PLAYLIST_EXECUTOR, audio_player._RESOLVE_EXECUTOR)
        self.assertLessEqual(lane._slots, 4)
        # Enumerations (background) can never take every slot, so an autoplay refill —
        # which can sit on a track transition — always finds one.
        self.assertLess(lane._background_slots, lane._slots)

    def test_shutdown_covers_the_playlist_pool(self):
        import inspect
        self.assertIn("_PLAYLIST_EXECUTOR.shutdown", inspect.getsource(audio_player.shutdown_audio))


class TestResolveAudio(unittest.TestCase):

    def test_cache_hit_is_answered_without_the_pool(self):
        audio_player._resolve_cache_put("dQw4w9WgXcQ", _fresh_info("Cached"))
        self.addCleanup(audio_player.invalidate_resolve_cache, WATCH)

        class _NoPool:
            async def run(self, *a, **k):
                raise AssertionError("a cache hit was queued for a resolve worker")

        with patch.object(audio_player, "_RESOLVE_LANE", _NoPool()):
            out = asyncio.run(audio_player.resolve_audio(WATCH, "web"))
        self.assertEqual(out["title"], "Cached")

    def test_retry_backoff_does_not_hold_a_worker(self):
        """With one worker: A fails with a 429 and backs off. B must run during A's
        backoff — before, the backoff slept inside the worker and B waited it out."""
        pool = ThreadPoolExecutor(max_workers=1)
        self.addCleanup(pool.shutdown, wait=False)
        lane = _Lane(pool, 1, 1)
        calls, slept = [], []
        failed_once = []
        real_sleep = asyncio.sleep

        def fake_get_audio_url(query, *a, **k):
            calls.append(query)
            if query == "A" and not failed_once:
                failed_once.append(True)
                raise RATE_LIMITED
            return {"url": "u", "title": query}

        async def compressed_backoff(seconds):
            slept.append(seconds)
            await real_sleep(0.05)

        async def scenario():
            a = asyncio.create_task(audio_player.resolve_audio("A", "web"))
            await _until(lambda: slept)            # A is now backing off
            b = asyncio.create_task(audio_player.resolve_audio("B", "web"))
            return await asyncio.gather(a, b)

        with patch.object(audio_player, "_RESOLVE_LANE", lane), \
             patch.object(audio_player, "get_audio_url", fake_get_audio_url), \
             patch.object(audio_player.asyncio, "sleep", compressed_backoff):
            a_info, b_info = asyncio.run(scenario())

        self.assertEqual((a_info["title"], b_info["title"]), ("A", "B"))
        self.assertEqual(calls, ["A", "B", "A"], "B queued behind A's backoff")
        self.assertEqual(len(slept), 1)
        self.assertGreaterEqual(slept[0], 3.75)    # 429 keeps the slow, anti-ban tier
        self.assertLessEqual(slept[0], 6.25)

    def test_background_flag_reaches_the_lane(self):
        lane = MagicMock()
        lane.run = AsyncMock(return_value={"url": "u", "title": "t"})
        with patch.object(audio_player, "_RESOLVE_LANE", lane):
            asyncio.run(audio_player.resolve_audio("q", "web", background=True))
            asyncio.run(audio_player.resolve_audio("q", "web"))
        self.assertEqual([c.kwargs["background"] for c in lane.run.call_args_list],
                         [True, False])
        self.assertIs(lane.run.call_args.args[0], audio_player.get_audio_url)


class _Gs:
    def __init__(self):
        self.queue = TrackQueue()
        self.prefetch_task = None
        self.autoplay = True
        self.autoplay_pool = []
        self.autoplay_history = set()


class _Bot:
    config = {"youtube": {"client": "web"}, "debug": False}

    def __init__(self):
        self.gs = _Gs()

    def get_guild_state(self, guild_id):
        return self.gs

    def get_channel(self, channel_id):
        return None


class TestBackgroundWorkIsMarkedBackground(unittest.TestCase):

    def setUp(self):
        commands._last_prefetch_monotonic = 0.0
        warm = patch.object(commands, "warm_stream_url", return_value=True)
        warm.start()
        self.addCleanup(warm.stop)

    def test_prefetch_resolves_as_background(self):
        bot = _Bot()
        bot.gs.queue.add(Track(query="q1", title="q1", requested_by="u1"))
        with patch.object(commands, "resolve_audio", return_value=_fresh_info()) as m:
            asyncio.run(commands._prefetch_next_track(bot, 1))
        self.assertIs(m.call_args.kwargs.get("background"), True)

    def test_queue_add_lookup_resolves_as_background(self):
        bot = _Bot()
        track = Track(query="some song", title="some song", requested_by="u1")
        with patch.object(commands, "resolve_audio", return_value=_fresh_info()) as m:
            asyncio.run(commands._resolve_track_info(bot, 123, track))
        self.assertIs(m.call_args.kwargs.get("background"), True)
        self.assertEqual(track.title, "Song")

    def test_user_facing_resolves_are_not_background(self):
        import inspect
        sources = {
            "AudioPlayer.play": inspect.getsource(audio_player.AudioPlayer.play),
            "!play": inspect.getsource(commands.MusicCog.play.callback),
            "search pick": inspect.getsource(commands._play_selected),
        }
        for where, src in sources.items():
            with self.subTest(where=where):
                self.assertIn("resolve_audio(", src)
                self.assertNotIn("background=True", src)


class TestPlaylistLane(unittest.TestCase):

    def test_autoplay_refill_runs_in_the_playlist_lane(self):
        seen = []

        class _Recorder:
            def __init__(self, name):
                self.name = name

            async def run(self, fn, *args, background=False):
                seen.append((self.name, fn, background))
                return [{"url": "https://www.youtube.com/watch?v=A", "title": "A"}]

        bot = _Bot()
        seed = Track(query="q", title="Seed", requested_by="u1",
                     url="https://www.youtube.com/watch?v=SEED")
        with patch.object(commands, "_PLAYLIST_LANE", _Recorder("playlist")), \
             patch.object(commands, "_RESOLVE_LANE", _Recorder("resolve")):
            pick = asyncio.run(commands._autoplay_pick(bot, 1, seed))
        self.assertEqual(pick.url, "https://www.youtube.com/watch?v=A")
        self.assertEqual(seen, [("playlist", commands.get_related_tracks, False)],
                         "a refill can sit on a track transition — never behind enumerations")

    def test_enumeration_runs_in_the_playlist_lane_as_background(self):
        lane = MagicMock()
        lane.run = AsyncMock(return_value={"title": "P", "tracks": []})
        with patch.object(audio_player, "_PLAYLIST_LANE", lane):
            asyncio.run(audio_player.enumerate_playlist(
                "https://www.youtube.com/playlist?list=PL1", "web"))
        args, kwargs = lane.run.call_args
        self.assertIs(args[0], audio_player.extract_playlist_info)
        self.assertEqual(args[3], audio_player.MAX_PLAYLIST_TRACKS)
        self.assertIsInstance(args[4], threading.Event)
        self.assertIs(kwargs["background"], True)

    def test_cancelling_an_enumeration_stops_the_worker_between_pages(self):
        pages = []
        stopped = threading.Event()

        def fake_extract(query, client, limit, cancel):
            for page in range(200):
                if cancel.is_set():
                    stopped.set()
                    raise DownloadCancelled("superseded")
                pages.append(page)
                time.sleep(0.01)
            return {"title": "P", "tracks": []}

        async def scenario():
            task = asyncio.create_task(audio_player.enumerate_playlist(
                "https://www.youtube.com/playlist?list=PL1", "web"))
            await _until(lambda: pages)
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            await asyncio.get_running_loop().run_in_executor(None, stopped.wait, 2)

        with patch.object(audio_player, "extract_playlist_info", fake_extract):
            asyncio.run(scenario())
        self.assertTrue(stopped.is_set(), "the worker kept paging after cancellation")
        self.assertLess(len(pages), 200)


def _mock_ydl(cm):
    ydl_class = MagicMock()
    ydl_class.return_value.__enter__.return_value = cm
    ydl_class.return_value.__exit__.return_value = False
    return ydl_class


class TestEnumerationPageGuard(unittest.TestCase):
    """Every page an extractor fetches goes through YoutubeDL.urlopen — that is where a
    cancelled enumeration stops, before the request leaves the box."""

    URL = "https://www.youtube.com/playlist?list=PL1"

    def _ydl_that_fetches_a_page(self):
        cm = MagicMock()
        real_urlopen = cm.urlopen

        def extract_info(url, download=False):
            cm.urlopen("https://www.youtube.com/youtubei/v1/browse")
            return {"title": "P", "entries": [{"url": "id1", "title": "T1"}]}

        cm.extract_info.side_effect = extract_info
        return cm, real_urlopen

    def test_cancelled_enumeration_stops_before_its_next_request(self):
        cm, real_urlopen = self._ydl_that_fetches_a_page()
        cancel = threading.Event()
        cancel.set()
        with patch("audio_player.YoutubeDL", _mock_ydl(cm)):
            with self.assertRaises(DownloadCancelled):
                audio_player.extract_playlist_info(self.URL, "web", cancel=cancel)
        real_urlopen.assert_not_called()

    def test_live_enumeration_requests_normally(self):
        cm, real_urlopen = self._ydl_that_fetches_a_page()
        with patch("audio_player.YoutubeDL", _mock_ydl(cm)):
            out = audio_player.extract_playlist_info(self.URL, "web", cancel=threading.Event())
        real_urlopen.assert_called_once()
        self.assertEqual(out["tracks"][0]["title"], "T1")

    def test_shutdown_stops_an_enumeration(self):
        """Without this, Ctrl+C waits for a 2000-track enumeration to finish paging."""
        cm, real_urlopen = self._ydl_that_fetches_a_page()
        audio_player._shutdown.set()
        self.addCleanup(audio_player._shutdown.clear)
        with patch("audio_player.YoutubeDL", _mock_ydl(cm)):
            with self.assertRaises(DownloadCancelled):
                audio_player.extract_playlist_info(self.URL, "web")
        real_urlopen.assert_not_called()

    def test_guard_works_on_a_real_youtubedl_instance(self):
        """The guard is an instance attribute shadowing YoutubeDL.urlopen; make sure the
        real class lets it, and that it refuses before any network I/O."""
        from yt_dlp import YoutubeDL
        cancel = threading.Event()
        cancel.set()
        with YoutubeDL({"quiet": True}) as ydl:
            audio_player._stop_requests_when(ydl, cancel)
            with self.assertRaises(DownloadCancelled):
                ydl.urlopen("https://www.youtube.com/youtubei/v1/browse")


class TestSupersededPlaylistOffer(unittest.TestCase):
    """A second playlist in the same channel cancels the first one's enumeration, and the
    first !play invocation ends quietly instead of raising or editing the card."""

    PL1 = "https://www.youtube.com/playlist?list=PLfirst"
    PL2 = "https://www.youtube.com/playlist?list=PLsecond"

    def setUp(self):
        self.started = {self.PL1: threading.Event(), self.PL2: threading.Event()}
        self.release = {self.PL1: threading.Event(), self.PL2: threading.Event()}
        self.cancelled = []
        self.addCleanup(lambda: [e.set() for e in self.release.values()])

        voice_channel = SimpleNamespace(id=55)
        vc = SimpleNamespace(channel=voice_channel, is_connected=lambda: True,
                             is_playing=lambda: True)
        gs = SimpleNamespace(queue=TrackQueue(), player=SimpleNamespace(is_playing=True))
        gs.queue.current = Track(query="now", title="Now Playing", requested_by="u0")
        self.bot = SimpleNamespace(config={"youtube": {"client": "web"}},
                                   pending_playlists={}, command_prefix="!",
                                   get_guild_state=lambda gid: gs)
        status = SimpleNamespace(edit=AsyncMock(), delete=AsyncMock())
        self.ctx = SimpleNamespace(
            author=SimpleNamespace(id=7, voice=SimpleNamespace(channel=voice_channel)),
            guild=SimpleNamespace(id=1, voice_client=vc),
            channel=SimpleNamespace(id=99), interaction=None,
            send=AsyncMock(return_value=status),
        )
        self.cog = commands.MusicCog(self.bot)

        for p in (patch.object(commands, "check_channel", AsyncMock(return_value=True)),
                  patch.object(commands, "extract_playlist_info", self._fake_extract),
                  patch.object(audio_player, "extract_playlist_info", self._fake_extract),
                  patch.object(commands, "_schedule_prefetch", lambda *a: None),
                  patch.object(commands, "build_player_view", MagicMock()),
                  patch.object(commands, "update_np_embed", AsyncMock())):
            p.start()
            self.addCleanup(p.stop)

    def _fake_extract(self, query, client, limit=2000, cancel=None):
        """limit=1 answers at once; a full enumeration pages until released or cancelled."""
        if limit == 1:
            return {"title": query[-7:], "tracks": [
                {"url": "https://www.youtube.com/watch?v=first", "title": "first"}]}
        self.started[query].set()
        for _ in range(500):
            if cancel is not None and cancel.is_set():
                self.cancelled.append(query)
                raise DownloadCancelled("superseded")
            if self.release[query].wait(0.01):
                return {"title": query[-7:], "tracks": [
                    {"url": f"https://www.youtube.com/watch?v={i}", "title": str(i)}
                    for i in range(3)]}
        raise AssertionError("enumeration never released")

    def _play(self, query):
        return asyncio.create_task(
            commands.MusicCog.play.callback(self.cog, self.ctx, query=query))

    async def _enumerating(self, query):
        await asyncio.get_running_loop().run_in_executor(None, self.started[query].wait, 2)

    def test_second_playlist_cancels_the_first_enumeration(self):
        async def scenario():
            first = self._play(self.PL1)
            await self._enumerating(self.PL1)
            second = self._play(self.PL2)
            await asyncio.wait_for(first, 3)      # ends quietly, no exception
            await _until(lambda: self.cancelled)  # the first worker stopped paging
            await self._enumerating(self.PL2)
            self.release[self.PL2].set()
            await asyncio.wait_for(second, 3)

        asyncio.run(scenario())

        self.assertEqual(self.cancelled, [self.PL1], "the superseded enumeration kept paging")
        pending = self.bot.pending_playlists["99"]
        self.assertEqual(pending["query"], self.PL2)
        self.assertEqual(len(pending["tracks"]), 2)

    def test_cancelling_the_command_itself_still_propagates(self):
        """Only an abandoned enumeration is swallowed. When the !play task is the one
        being cancelled (shutdown), it must end cancelled — and asyncio cancels the
        enumeration it awaits, so the worker stops paging too."""
        async def scenario():
            first = self._play(self.PL1)
            await self._enumerating(self.PL1)
            first.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await first
            await _until(lambda: self.cancelled)

        asyncio.run(scenario())
        self.assertEqual(self.cancelled, [self.PL1])

    def test_abandon_paths_cancel_rather_than_just_discard(self):
        """Voice-connect failure, a failed first track and offer expiry all leave an
        enumeration nobody will read — each must cancel it, not let it page on."""
        import inspect
        src = inspect.getsource(commands.MusicCog.play.callback)
        self.assertNotIn("_discard_future(full_fut)", src)
        self.assertNotIn('_discard_future(old["future"])', src)
        self.assertNotIn('_discard_future(entry["future"])', src)
        self.assertGreaterEqual(src.count("_abandon_enumeration("), 4)

    def test_discarding_a_cancelled_future_is_silent(self):
        """_discard_future's callback must not raise CancelledError into the loop's
        exception handler when handed a cancelled enumeration."""
        errors = []

        async def scenario():
            loop = asyncio.get_running_loop()
            loop.set_exception_handler(lambda l, ctx: errors.append(ctx))
            task = asyncio.create_task(asyncio.sleep(10))
            await asyncio.sleep(0)
            commands._abandon_enumeration(task)
            await asyncio.sleep(0.01)

        asyncio.run(scenario())
        self.assertEqual(errors, [])


class TestPrefetchFloorAcrossGuilds(unittest.TestCase):

    def test_concurrent_prefetches_are_spaced_by_the_floor(self):
        """Guilds that hit the floor together used to wake together and all fire: the
        floor only spaced the first of them. Each must re-check after sleeping."""
        floor = 0.1
        bots = [_Bot() for _ in range(3)]
        for i, bot in enumerate(bots):
            bot.gs.queue.add(Track(query=f"q{i}", title=f"q{i}", requested_by="u1"))
        claims = []

        async def fake_resolve(query, *a, **k):
            claims.append(commands._last_prefetch_monotonic)
            return _fresh_info()

        async def scenario():
            commands._last_prefetch_monotonic = time.monotonic()  # everyone hits the floor
            await asyncio.gather(*(commands._prefetch_next_track(b, 1) for b in bots))

        with patch.object(commands, "_PREFETCH_MIN_INTERVAL", floor), \
             patch.object(commands, "resolve_audio", fake_resolve), \
             patch.object(commands, "warm_stream_url", return_value=True):
            asyncio.run(scenario())

        self.assertEqual(len(claims), 3)
        gaps = [b - a for a, b in zip(claims, claims[1:])]
        for gap in gaps:
            self.assertGreaterEqual(gap, floor, f"prefetches fired together: gaps={gaps}")


class TestChildProcessPriority(unittest.TestCase):
    """Most of the resolve-time stutter is the OS sharing 2 vCPUs with Deno, not the GIL:
    measured p99 packet gap 21 ms -> 125 ms with 4 concurrent resolves, 48 ms with Deno
    and bgutil below normal priority."""

    def _read(self, name):
        with open(os.path.join(_ROOT, name), encoding="utf-8") as fh:
            return fh.read()

    def test_docker_runs_deno_through_a_nice_wrapper_found_first_on_path(self):
        src = self._read("Dockerfile")
        self.assertTrue("exec nice -n 10 %s/bin/deno" in src and '"$DENO_INSTALL"' in src,
                        "no wrapper exec'ing the real deno at nice 10")
        self.assertTrue('/bin/deno "$@"' in src, "the wrapper must pass deno's arguments on")
        path_lines = [line for line in src.splitlines()
                      if line.startswith("ENV PATH=") and "/opt/nice-bin" in line]
        self.assertEqual(len(path_lines), 1, "no ENV PATH line puts the wrapper on PATH")
        self.assertTrue(path_lines[0].startswith('ENV PATH="/opt/nice-bin:'),
                        "the wrapper must come before the real deno on PATH")
        self.assertLess(src.index("deno.land/install.sh"), src.index(path_lines[0]),
                        "PATH must be extended after Deno's own ENV PATH line")

    def test_bgutil_server_starts_below_normal_priority_on_linux(self):
        src = self._read("main.py")
        self.assertTrue("os.nice(10)" in src, "bgutil's preexec_fn must lower its priority")
        self.assertTrue('preexec_fn=_lower_priority if os.name == "posix" else None' in src,
                        "the bgutil server must start through the preexec_fn on POSIX")


if __name__ == "__main__":
    unittest.main(verbosity=2)
