"""Tests for tearing down a stream pipeline whose start was abandoned part-way.

play() builds the stream reader, FFmpeg and the jitter buffer one await at a time. When
the task is cancelled in between (auto-next cancelled by a skip) or voice_client.play()
raises (voice dropped mid-start), whatever was already built must stop. Left alone it
runs forever: FFmpeg blocks writing PCM nobody reads, discord.py's stdin-writer thread
blocks behind it and keeps the source alive, and the jitter buffer's fill thread spins.
"""
import asyncio
import contextlib
import os
import sys
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import discord
import audio_player

FRAME = b"\x00" * audio_player._PCM_FRAME_SIZE
# Handed to play() as a fresh prefetch, so no test depends on how resolving works.
INFO = {"title": "T", "url": "https://r1.googlevideo.com/videoplayback?id=x&expire=99999999999",
        "webpage_url": "https://www.youtube.com/watch?v=x"}


class _FakeProc:
    """The stream reader / yt-dlp subprocess handle."""

    def __init__(self):
        self.stdout = object()
        self.stderr = None
        self.pid = -1
        self.terminated = threading.Event()

    def poll(self):
        return 1 if self.terminated.is_set() else None

    def terminate(self):
        self.terminated.set()

    kill = terminate

    def wait(self, timeout=None):
        return 1


class _FakeFFmpeg(discord.AudioSource):
    """discord.FFmpegPCMAudio: emits PCM until cleaned up."""

    def __init__(self, harness):
        self._process = SimpleNamespace(stderr=None)   # play() drains its stderr
        self.cleaned = threading.Event()
        self._harness = harness
        self._reads = 0
        harness.ffmpegs.append(self)
        harness.park("ffmpeg")

    def read(self):
        self._reads += 1
        if self._reads == 1:
            self._harness.park("first_frame")        # play()'s first-frame read
        elif self._reads == 2:
            self._harness.park("prefill")            # the jitter buffer's fill thread
        return b"" if self.cleaned.is_set() else FRAME

    def is_opus(self):
        return False

    def cleanup(self):
        self.cleaned.set()


class _FakeVoice:
    def __init__(self, play_error=None):
        self.guild = SimpleNamespace(id=1)
        self.encoder = None
        self.play_error = play_error
        self.played = None

    def is_connected(self):
        return True

    def is_playing(self):
        return False

    def is_paused(self):
        return False

    def play(self, source, after=None):
        if self.play_error:
            raise self.play_error
        self.played = source


class _Harness:
    """Records every pipeline piece play() builds; optionally parks one build stage."""

    def __init__(self, stop_at=None):
        self.stop_at = stop_at
        self.entered = threading.Event()
        self.release = threading.Event()
        self.procs, self.ffmpegs, self.buffers = [], [], []

    def park(self, stage):
        if stage == self.stop_at:
            self.entered.set()
            self.release.wait(5)

    def open_stream(self, *args):
        proc = _FakeProc()
        self.procs.append(proc)
        self.park("open")
        return proc

    def torn_down(self):
        return (all(p.terminated.is_set() for p in self.procs)
                and all(f.cleaned.is_set() for f in self.ffmpegs)
                and not any(b._thread.is_alive() for b in self.buffers))

    @contextlib.contextmanager
    def patched(self):
        harness = self

        class _RecordingBuffer(audio_player._BufferedAudioSource):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                harness.buffers.append(self)

        with patch.object(audio_player, "_open_audio_stream", self.open_stream), \
             patch.object(discord, "FFmpegPCMAudio", lambda *a, **k: _FakeFFmpeg(harness)), \
             patch.object(audio_player, "_BufferedAudioSource", _RecordingBuffer):
            yield


class TestAbortedStartLeavesNothingRunning(unittest.TestCase):

    def _start(self, harness, voice, cancel):
        with patch.object(audio_player, "_find_ffmpeg", return_value="ffmpeg"):
            player = audio_player.AudioPlayer({"audio": {"sample_rate": 48000, "channels": 2},
                                               "youtube": {"client": "web"}})
        player._voice_client = voice
        outcome = {}

        async def scenario():
            task = asyncio.ensure_future(player.play("q", prefetched_info=dict(INFO)))
            if cancel:
                parked = await asyncio.get_running_loop().run_in_executor(
                    None, harness.entered.wait, 5)
                self.assertTrue(parked, f"play() never reached {harness.stop_at!r}")
                task.cancel()
            try:
                outcome["result"] = await task
            except BaseException as e:
                outcome["error"] = e
            harness.release.set()   # let the abandoned executor call finish
            if "error" in outcome:
                # Keep the loop alive while late results land and are discarded.
                deadline = time.monotonic() + 3
                while not harness.torn_down() and time.monotonic() < deadline:
                    await asyncio.sleep(0.01)

        with harness.patched():
            asyncio.run(scenario())
        return player, outcome

    def _assert_nothing_left_running(self, harness, player):
        self.assertTrue(harness.procs, "the stream was never opened")
        for proc in harness.procs:
            self.assertTrue(proc.terminated.is_set(), "stream reader left running")
        for ffmpeg in harness.ffmpegs:
            self.assertTrue(ffmpeg.cleaned.is_set(), "FFmpeg left running")
        for buf in harness.buffers:
            self.assertFalse(buf._thread.is_alive(), "jitter-buffer fill thread left running")
        self.assertIsNone(player._ytdlp_proc, "a dead stream must not stay tracked")

    def test_cancelled_at_each_build_stage(self):
        for stage in ("open", "ffmpeg", "first_frame", "prefill"):
            with self.subTest(stage=stage):
                harness = _Harness(stop_at=stage)
                player, outcome = self._start(harness, _FakeVoice(), cancel=True)
                self.assertIsInstance(outcome.get("error"), asyncio.CancelledError)
                self._assert_nothing_left_running(harness, player)

    def test_voice_client_play_raising(self):
        harness = _Harness()
        voice = _FakeVoice(play_error=discord.ClientException("Not connected to voice."))
        player, outcome = self._start(harness, voice, cancel=False)
        self.assertIsInstance(outcome.get("error"), discord.ClientException)
        self.assertEqual(len(harness.buffers), 1, "precondition: the whole pipeline was built")
        self._assert_nothing_left_running(harness, player)

    def test_successful_start_keeps_its_pipeline(self):
        harness = _Harness()
        voice = _FakeVoice()
        player, outcome = self._start(harness, voice, cancel=False)
        self.addCleanup(lambda: [b.cleanup() for b in harness.buffers])
        self.assertEqual(outcome.get("result", {}).get("title"), "T", outcome)
        self.assertIs(voice.played, harness.buffers[0])
        self.assertIs(player._ytdlp_proc, harness.procs[0])
        self.assertFalse(harness.procs[0].terminated.is_set())
        self.assertFalse(harness.ffmpegs[0].cleaned.is_set())
        self.assertTrue(harness.buffers[0]._thread.is_alive())


if __name__ == "__main__":
    unittest.main(verbosity=2)
