"""Tests for the dedicated audio thread pools.

asyncio's default executor is min(32, cpu+4) — six workers on a 2-vCPU VPS. Starting
one track can hold two of them for seconds (a resolve, then the CDN settle wait), so at
10-50 guilds peak transitions would queue behind each other and look exactly like the
latency this codebase worked hard to remove. These pools keep audio off that shared
budget and size each kind of work correctly.
"""
import os
import re
import sys
import unittest
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import audio_player
import commands

_AUDIO_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "audio_player.py")
_CMD_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "commands.py")


def _src(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


class TestPools(unittest.TestCase):

    def test_both_pools_exist(self):
        self.assertIsInstance(audio_player._RESOLVE_EXECUTOR, ThreadPoolExecutor)
        self.assertIsInstance(audio_player._STREAM_EXECUTOR, ThreadPoolExecutor)

    def test_stream_pool_is_wide_and_not_cpu_bound(self):
        """Settle waits and warm probes are blocked threads, not busy ones — a
        core-count-sized pool would serialise guild transitions."""
        self.assertGreaterEqual(audio_player._STREAM_EXECUTOR._max_workers, 32)

    def test_resolve_pool_is_bounded(self):
        """A resolve burns ~2s of Deno CPU; oversubscribing thrashes a small box."""
        self.assertLessEqual(audio_player._RESOLVE_EXECUTOR._max_workers, 8)
        self.assertGreaterEqual(audio_player._RESOLVE_EXECUTOR._max_workers, 4)

    def test_pools_are_distinct(self):
        self.assertIsNot(audio_player._RESOLVE_EXECUTOR, audio_player._STREAM_EXECUTOR)

    def test_threads_are_named_for_diagnosis(self):
        self.assertEqual(audio_player._RESOLVE_EXECUTOR._thread_name_prefix, "yt-resolve")
        self.assertEqual(audio_player._STREAM_EXECUTOR._thread_name_prefix, "audio-stream")


class TestNoAudioWorkLeftOnTheDefaultPool(unittest.TestCase):
    """run_in_executor(None, ...) puts work on the shared default pool."""

    LONG_RUNNING = ("get_audio_url_with_retry", "_open_audio_stream", "warm_stream_url",
                    "extract_playlist_info", "get_related_tracks", "FFmpegPCMAudio",
                    "src.read", "source.prefill", "source.read")

    def _default_pool_calls(self, path):
        text = "".join(_src(path).split())
        return [fn for fn in self.LONG_RUNNING
                if f"run_in_executor(None,{fn.replace(' ', '')}" in text]

    def test_audio_player_has_no_default_pool_handoffs(self):
        leaked = self._default_pool_calls(_AUDIO_SRC)
        self.assertEqual(leaked, [], f"still on the shared default pool: {leaked}")

    def test_commands_audio_paths_use_the_dedicated_pools(self):
        leaked = self._default_pool_calls(_CMD_SRC)
        self.assertEqual(leaked, [], f"still on the shared default pool: {leaked}")

    def test_resolves_go_to_the_resolve_pool(self):
        text = "".join(_src(_CMD_SRC).split()) + "".join(_src(_AUDIO_SRC).split())
        self.assertIn("run_in_executor(_RESOLVE_EXECUTOR,get_audio_url_with_retry,", text)

    def test_warm_up_goes_to_the_stream_pool(self):
        """The warm-up blocks for up to the whole settle ladder — it must never sit in
        the resolve pool, where it would block real resolves."""
        text = "".join(_src(_CMD_SRC).split())
        self.assertIn("run_in_executor(_STREAM_EXECUTOR,warm_stream_url,", text)
        self.assertNotIn("run_in_executor(_RESOLVE_EXECUTOR,warm_stream_url,", text)

    def test_stream_open_goes_to_the_stream_pool(self):
        text = "".join(_src(_AUDIO_SRC).split())
        self.assertIn("run_in_executor(_STREAM_EXECUTOR,_open_audio_stream,", text)


class TestConcurrencyHeadroom(unittest.TestCase):

    def test_stream_pool_absorbs_many_simultaneous_transitions(self):
        """Two stream-pool threads per starting track; 32 workers means ~16 guilds can
        start at once without queueing, versus 3 on a 2-vCPU default pool."""
        per_track = 2
        self.assertGreaterEqual(
            audio_player._STREAM_EXECUTOR._max_workers // per_track, 16)


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestGracefulShutdown(unittest.TestCase):
    """Workers park in multi-second sleeps; exit must not wait them out."""

    def tearDown(self):
        audio_player._shutdown.clear()

    def test_shutdown_interrupts_a_warm_up_immediately(self):
        import time
        audio_player._shutdown.set()
        started = time.perf_counter()
        # The ladder would otherwise sleep ~5.7s before giving up.
        result = audio_player.warm_stream_url(
            "https://r1.googlevideo.com/videoplayback?id=x")
        self.assertFalse(result)
        self.assertLess(time.perf_counter() - started, 0.5)

    def test_close_shuts_audio_down(self):
        import inspect
        import main
        self.assertIn("shutdown_audio()", inspect.getsource(main.MusicBot.close))

    def test_sleeps_are_interruptible_not_bare(self):
        """time.sleep in a worker cannot be woken; _shutdown.wait can."""
        src = _src(_AUDIO_SRC)
        # The sleeps that run on EVERY track start must be wakeable.
        self.assertIn("_shutdown.wait(delay)", src)      # CDN warm-up ladder
        self.assertIn("_shutdown.is_set()", src)         # settle loop + retry entry
