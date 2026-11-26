"""Tests for tearing down a stream pipeline whose start was abandoned part-way.

play() builds the stream reader, FFmpeg and the jitter buffer one await at a time. When
the task is cancelled in between (auto-next cancelled by a skip) or voice_client.play()
raises (voice dropped mid-start), whatever was already built must stop. Left alone it
runs forever: FFmpeg blocks writing PCM nobody reads, discord.py's stdin-writer thread
blocks behind it and keeps the source alive, and the jitter buffer's fill thread spins.

Such a start must not leave the player flagged as playing either: auto-next would wait in
wait_for_playback() for a track that never started, and nothing would play again.
"""
import asyncio
import contextlib
import os
import sys
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import discord
import audio_player
import commands
from track_queue import Track, TrackQueue

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

    def play(self, source, after=None, **encoder_settings):
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

    def _start(self, harness, voice, cancel, while_parked=None):
        with patch.object(audio_player, "_find_ffmpeg", return_value="ffmpeg"):
            player = audio_player.AudioPlayer({"audio": {"sample_rate": 48000, "channels": 2},
                                               "youtube": {"client": "web"}})
        player._voice_client = voice
        if harness.stop_at == "gap":
            # A clip waits for the song change, so the start parks until it has been spoken
            # (the fake voice never finishes it): the last await before voice_client.play().
            player.speech.queue_after(FRAME * 3)
            real_wait_for_gap = player.speech.wait_for_gap

            async def wait_for_gap(gen):
                harness.entered.set()
                await real_wait_for_gap(gen)

            player.speech.wait_for_gap = wait_for_gap
        outcome = {}

        async def scenario():
            task = asyncio.ensure_future(player.play("q", prefetched_info=dict(INFO)))
            if cancel or while_parked:
                parked = await asyncio.get_running_loop().run_in_executor(
                    None, harness.entered.wait, 5)
                self.assertTrue(parked, f"play() never reached {harness.stop_at!r}")
                if while_parked:
                    while_parked(player)
                if cancel:
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
        for stage in ("open", "ffmpeg", "first_frame", "prefill", "gap"):
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
        self.assertIs(voice.played.music, harness.buffers[0])
        self.assertIs(player._ytdlp_proc, harness.procs[0])
        self.assertFalse(harness.procs[0].terminated.is_set())
        self.assertFalse(harness.ffmpegs[0].cleaned.is_set())
        self.assertTrue(harness.buffers[0]._thread.is_alive())

    def _assert_player_idle(self, player):
        self.assertFalse(player.is_playing, "a start that never began still flags a track as playing")
        self.assertIsNone(player.current_track_title)
        self.assertTrue(player._playback_done.is_set(),
                        "wait_for_playback() would wait forever for a track that never started")

    def test_an_abandoned_start_leaves_the_player_idle(self):
        """The prefill and voice_client.play() come after play() flags its track as playing."""
        for stage in ("open", "ffmpeg", "first_frame", "prefill", "gap"):
            with self.subTest(stage=stage):
                player, outcome = self._start(_Harness(stop_at=stage), _FakeVoice(), cancel=True)
                self.assertIsInstance(outcome.get("error"), asyncio.CancelledError)
                self._assert_player_idle(player)
        with self.subTest(stage="voice_client.play() raising"):
            voice = _FakeVoice(play_error=discord.ClientException("Not connected to voice."))
            player, outcome = self._start(_Harness(), voice, cancel=False)
            self.assertIsInstance(outcome.get("error"), discord.ClientException)
            self._assert_player_idle(player)

    def test_a_stop_during_the_gap_ends_the_start(self):
        """!stop, ■ or a leave while the song waits out the gap speech. The stop's cut ends the
        wait, and the start must then give up as a cancelled one does, not take the voice client."""
        harness, voice = _Harness(stop_at="gap"), _FakeVoice()
        player, outcome = self._start(harness, voice, cancel=False,
                                      while_parked=lambda p: p.stop_playback())
        self.assertIsInstance(outcome.get("error"), asyncio.CancelledError, outcome)
        self.assertIsNone(voice.played.music, "the song reached the voice client after the stop")
        self._assert_nothing_left_running(harness, player)
        self._assert_player_idle(player)

    def test_a_newer_start_keeps_its_playback_state(self):
        """The skip that cancelled this start has already started its own track."""
        def newer_start(player):
            player._playback_gen += 1   # what a newer play()/play_radio() does first
            player.is_playing = True
            player.current_track_title = "newer"
            player._playback_done.clear()

        player, outcome = self._start(_Harness(stop_at="prefill"), _FakeVoice(), cancel=True,
                                      while_parked=newer_start)
        self.assertIsInstance(outcome.get("error"), asyncio.CancelledError)
        self.assertTrue(player.is_playing)
        self.assertEqual(player.current_track_title, "newer")
        self.assertFalse(player._playback_done.is_set())


class _DroppingVoice(_FakeVoice):
    """The voice socket drops during the first start: voice_client.play() raises as discord.py
    does while it reconnects, and is_connected() reads False for a few polls after."""

    def __init__(self, down_polls=2):
        super().__init__()
        self.channel = SimpleNamespace(members=[SimpleNamespace(bot=False)])
        self.down_polls = down_polls
        self.dropped = False
        self.sources = []

    def is_connected(self):
        if self.dropped and self.down_polls:
            self.down_polls -= 1
            return False
        return True

    def play(self, source, after=None, **encoder_settings):
        if not self.dropped:
            self.dropped = True
            raise discord.ClientException("Not connected to voice.")
        self.sources.append(source)


class TestVoiceDropMidStart(unittest.TestCase):
    """End to end with the real AudioPlayer and the real _auto_next: the voice socket drops
    while a track is starting and comes back a moment later. The chain puts the track back,
    waits the drop out and plays it (PR #8's design); it used to park in wait_for_playback()
    on the state the failed start left behind, so nothing played until someone intervened."""

    def test_auto_play_resumes_after_the_drop(self):
        harness = _Harness()
        voice = _DroppingVoice()
        with patch.object(audio_player, "_find_ffmpeg", return_value="ffmpeg"):
            player = audio_player.AudioPlayer({"audio": {"sample_rate": 48000, "channels": 2},
                                               "youtube": {"client": "web"}})
        player._voice_client = voice
        gs = SimpleNamespace(queue=TrackQueue(), player=player, auto_next_task=None,
                             auto_next_gen=1, autoplay=False)
        for name in ("a", "b"):
            gs.queue.add(Track(query=name, title=name, requested_by="u",
                               resolved_info=dict(INFO), resolved_at=time.time()))
        channel = SimpleNamespace(sent=[])

        async def send(content=None, **kwargs):
            channel.sent.append(content)
        channel.send = send
        guild = SimpleNamespace(id=1, voice_client=voice)
        bot = SimpleNamespace(get_guild_state=lambda gid: gs, get_guild=lambda gid: guild,
                              get_channel=lambda cid: channel)

        async def scenario():
            chain = asyncio.ensure_future(commands._auto_next(bot, 10, 1, 1))
            deadline = time.monotonic() + 5
            while not voice.sources and not chain.done() and time.monotonic() < deadline:
                await asyncio.sleep(0.01)
            chain.cancel()
            await asyncio.gather(chain, return_exceptions=True)

        with harness.patched(), \
                patch.object(commands, "_VOICE_RECOVERY_POLL", 0.01), \
                patch.object(commands, "_schedule_prefetch", lambda *a, **k: None), \
                patch.object(commands, "_schedule_autoplay_topup", lambda *a, **k: None), \
                patch.object(commands, "send_new_np", AsyncMock()), \
                patch.object(commands, "update_np_stopped", AsyncMock()), \
                patch.object(commands, "build_player_view", lambda *a, **k: None), \
                patch.object(commands, "_get_requester_name", lambda *a, **k: "u"):
            asyncio.run(scenario())
        self.addCleanup(lambda: [b.cleanup() for b in harness.buffers])

        self.assertEqual(len(voice.sources), 1, "the requeued track never played")
        self.assertEqual([t.title for t in gs.queue.list()], ["b"])
        self.assertEqual(sum("lost the voice connection" in m.lower() for m in channel.sent), 1,
                         channel.sent)


if __name__ == "__main__":
    unittest.main(verbosity=2)
