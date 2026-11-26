"""Speech: where a clip goes, and the gap between songs."""
import asyncio, os, sys, time, unittest
from array import array
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import discord

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import speech
from speech import Speech, SpeechMixer, SpeechUnavailable

F = speech.FRAME_SIZE


def _clip(value, frames=3):
    return array("h", [value] * (F // 2 * frames)).tobytes()


class _Music(discord.AudioSource):
    def __init__(self, frames):
        self.left = frames

    def read(self):
        if self.left <= 0:
            return b""
        self.left -= 1
        return _clip(1000, 1)

    def is_opus(self):
        return False


class _VC:
    """discord.VoiceClient stand-in: one source at a time; pump() plays like the audio thread."""

    def __init__(self, connected=True):
        self.connected, self.source, self.after, self.plays, self.stops = connected, None, None, [], 0

    def is_connected(self):
        return self.connected

    def is_playing(self):
        return self.source is not None

    def is_paused(self):
        return False

    def play(self, source, after=None, **encoder_settings):
        if self.source is not None:
            raise discord.ClientException("Already playing audio.")
        self.source, self.after = source, after
        self.plays.append(source)

    def stop(self):
        self.stops += 1
        self.finish()

    def finish(self):
        source, after = self.source, self.after
        self.source = self.after = None
        if source is not None and after:
            after(None)

    def pump(self):
        """Play the current source to its end; return the first sample of each frame."""
        samples = []
        while self.source is not None:
            frame = self.source.read()
            if not frame:
                self.finish()
                break
            samples.append(array("h", frame[:2])[0])
        return samples


def _setup(connected=True):
    vc = _VC(connected)
    player = SimpleNamespace(_voice_client=vc, _encoder_settings=lambda: {}, is_paused=False)
    return vc, player, Speech(player)


async def _settle():
    for _ in range(5):
        await asyncio.sleep(0)


def _run(coro):
    return asyncio.run(coro)


class TestRouting(unittest.TestCase):
    def test_idle_clip_plays_alone(self):
        async def go():
            vc, _, sp = _setup()
            self.assertEqual(await sp.say(_clip(5)), "solo")
            self.assertIsNone(vc.plays[0].music)
            self.assertEqual(vc.pump(), [5, 5, 5])
            await _settle()
            self.assertFalse(sp.solo_active)
        _run(go())

    def test_second_idle_clip_joins_the_first(self):
        async def go():
            vc, _, sp = _setup()
            await sp.say(_clip(5)); await sp.say(_clip(6))
            self.assertEqual(len(vc.plays), 1)
            self.assertEqual(vc.pump(), [5, 5, 5, 6, 6, 6])
        _run(go())

    def test_clip_over_a_song_goes_into_its_mixer(self):
        async def go():
            vc, player, sp = _setup()
            sp.song_change(1)
            mixer = sp.wrap(_Music(100), 1)
            vc.play(mixer)
            self.assertEqual(await sp.say(_clip(5)), "mixer")
            player.is_paused = True
            self.assertEqual(await sp.say(_clip(6)), "paused")
            self.assertEqual((mixer.pending_clips(), len(vc.plays)), (2, 1))
            self.assertIsNotNone(sp.track_started_at)
        _run(go())

    def test_clip_during_a_song_start_is_held_for_the_next_song(self):
        async def go():
            vc, _, sp = _setup()
            sp.song_change(1)
            self.assertEqual(await sp.say(_clip(5)), "held")
            self.assertEqual(sp.wrap(_Music(10), 1).pending_clips(), 1)
            self.assertEqual(vc.plays, [])
        _run(go())

    def test_closed_mixer_still_playing_holds_new_clips(self):
        async def go():
            vc, _, sp = _setup()
            sp.song_change(1)
            mixer = sp.wrap(_Music(0), 1)
            vc.play(mixer)
            self.assertEqual(mixer.read(), b"")       # song over; the audio thread hasn't let go yet
            self.assertEqual(await sp.say(_clip(5)), "held")
            self.assertEqual(len(vc.plays), 1)
        _run(go())

    def test_clips_after_the_queue_runs_out_play_alone(self):
        async def go(update, heard):
            vc, _, sp = _setup()
            sp.song_change(1)
            vc.play(sp.wrap(_Music(2), 1))
            vc.pump()                                 # the song ends by itself and nothing is next
            sp.between_songs = AsyncMock(return_value=update)
            await sp.end_of_queue()
            self.assertEqual(await sp.say(_clip(5)), "solo")
            self.assertEqual(await sp.say(_clip(6)), "solo")
            self.assertEqual(len(vc.plays), 2)        # the song, then one speech-only source
            self.assertEqual(vc.pump(), heard)
            self.assertEqual(sp.waiting_clips(), 0)
        for update, heard in ((None, [5, 5, 5, 6, 6, 6]), (_clip(9), [9, 9, 9, 5, 5, 5, 6, 6, 6])):
            with self.subTest(update=update is not None):
                _run(go(update, heard))

    def test_clips_before_the_next_song_starts_play_alone(self):
        async def go():
            vc, _, sp = _setup()
            sp.song_change(1)
            vc.play(sp.wrap(_Music(2), 1))
            vc.pump()                                 # the song ends; the next start is still waiting
            self.assertEqual(await sp.say(_clip(5)), "solo")
            self.assertEqual(await sp.say(_clip(6)), "solo")
            self.assertEqual(len(vc.plays), 2)
            self.assertEqual(vc.pump(), [5, 5, 5, 6, 6, 6])
            await _settle()
            sp.song_change(2)
            self.assertEqual(sp.wrap(_Music(5), 2).pending_clips(), 0)   # nothing was held for it
        _run(go())

    def test_finished_solo_with_busy_voice_uses_the_backlog(self):
        async def go():
            vc, _, sp = _setup()
            await sp.say(_clip(5))
            while vc.source.read():                   # drained, but after() has not run yet
                pass
            self.assertEqual(await sp.say(_clip(6)), "solo")
            self.assertEqual(len(vc.plays), 1)
            vc.finish()
            await _settle()
            self.assertEqual(len(vc.plays), 2)
            self.assertEqual(vc.pump(), [6, 6, 6])
        _run(go())

    def test_say_without_voice_raises(self):
        async def go():
            _, _, sp = _setup(connected=False)
            with self.assertRaises(SpeechUnavailable):
                await sp.say(_clip(5))
        _run(go())

    def test_waiting_clips_counts_everywhere(self):
        async def go():
            vc, _, sp = _setup()
            await sp.say(_clip(1))                    # solo
            sp.queue_after(_clip(2))                  # after-song
            sp.song_change(1)                         # moves the after-song clip into the solo
            await sp.say(_clip(3))                    # held
            self.assertEqual(sp.waiting_clips(), 3)
        _run(go())


class TestGap(unittest.TestCase):
    def test_after_song_clips_play_at_the_song_change_in_order(self):
        async def go():
            vc, _, sp = _setup()
            sp.queue_after(_clip(1)); sp.queue_after(_clip(2))
            sp.song_change(1)
            self.assertTrue(sp.in_gap)
            self.assertEqual(vc.pump(), [1, 1, 1, 2, 2, 2])
        _run(go())

    def test_no_gap_without_speech(self):
        async def go():
            _, _, sp = _setup()
            sp.song_change(1)
            self.assertTrue(sp.starting)
            self.assertFalse(sp.in_gap)
        _run(go())

    def test_wait_for_gap_returns_when_the_speech_ends(self):
        async def go():
            vc, _, sp = _setup()
            sp.queue_after(_clip(1))
            sp.song_change(1)
            waiter = asyncio.ensure_future(sp.wait_for_gap(1))
            await _settle()
            self.assertFalse(waiter.done())
            vc.pump()
            await asyncio.wait_for(waiter, 1)
        _run(go())

    def test_a_cancel_that_arrives_as_the_speech_ends_is_not_lost(self):
        # A leave stops playback (which ends the speech) and then cancels the start, in one
        # tick. Python 3.11's asyncio.wait_for returned normally then, swallowing the cancel.
        async def go():
            vc, _, sp = _setup()
            sp.queue_after(_clip(1))
            sp.song_change(1)
            waiter = asyncio.ensure_future(sp.wait_for_gap(1))
            await _settle()
            sp.cut()
            waiter.cancel()
            await asyncio.gather(waiter, return_exceptions=True)
            self.assertTrue(waiter.cancelled(), "the cancel was swallowed")
        _run(go())

    def test_wait_for_gap_gives_up_on_stuck_speech(self):
        async def go():
            vc, _, sp = _setup()
            sp.queue_after(_clip(1))
            sp.song_change(1)
            with patch.object(speech, "GAP_MARGIN", 0.05):
                await asyncio.wait_for(sp.wait_for_gap(1), 1)
            self.assertEqual(vc.stops, 1)
        _run(go())

    def test_update_follows_the_after_song_clips(self):
        async def go():
            vc, _, sp = _setup()
            sp.song_change(1); sp.wrap(_Music(1), 1)   # a finished track's start time
            started = sp.track_started_at
            sp.between_songs = AsyncMock(return_value=_clip(9))
            sp.queue_after(_clip(1))
            sp.song_change(2)
            await _settle()
            self.assertEqual(vc.pump(), [1, 1, 1, 9, 9, 9])
            sp.between_songs.assert_awaited_once_with(started, True)
        _run(go())

    def test_update_provider_failure_is_skipped(self):
        async def go():
            vc, _, sp = _setup()
            sp.between_songs = AsyncMock(side_effect=ValueError("bad zone"))
            sp.song_change(1)
            await asyncio.wait_for(sp.wait_for_gap(1), 1)
            self.assertEqual(vc.plays, [])
        _run(go())

    def test_slow_update_is_dropped(self):
        async def go():
            vc, _, sp = _setup()
            async def slow(*args):
                await asyncio.sleep(2)
            sp.between_songs = slow
            sp.song_change(1)
            with patch.object(speech, "UPDATE_TIMEOUT", 0.05):
                t0 = time.monotonic()
                await sp.wait_for_gap(1)
            self.assertLess(time.monotonic() - t0, 0.5)
            self.assertEqual(vc.plays, [])
        _run(go())

    def test_start_failed_plays_held_clips_alone(self):
        async def go():
            vc, _, sp = _setup()
            sp.song_change(1)
            await sp.say(_clip(4))
            sp.start_failed(1)
            self.assertEqual(vc.pump(), [4, 4, 4])
        _run(go())

    def test_stale_generation_is_ignored(self):
        async def go():
            vc, _, sp = _setup()
            sp.song_change(1); sp.song_change(2)
            await sp.say(_clip(4))
            sp.start_failed(1)
            self.assertTrue(sp.starting)
            self.assertEqual(sp.wrap(_Music(5), 1).pending_clips(), 0)
            self.assertEqual(sp.wrap(_Music(5), 2).pending_clips(), 1)
        _run(go())

    def test_a_retried_start_does_not_repeat_the_update(self):
        async def go():
            vc, _, sp = _setup()
            sp.song_change(1); sp.wrap(_Music(1), 1)
            # like the real provider: no start time, nothing to say
            sp.between_songs = AsyncMock(side_effect=lambda started_at, starting_next:
                                         _clip(9) if started_at else None)
            sp.song_change(2); await _settle()        # the update plays in the gap
            sp.start_failed(2)                        # the next track fails to start
            sp.song_change(3); await _settle()        # auto-next moves on to the one after it
            self.assertEqual(vc.pump(), [9, 9, 9])
        _run(go())

    def test_an_update_still_loading_when_a_start_fails_is_kept_for_the_retry(self):
        async def go():
            vc, _, sp = _setup()
            sp.song_change(1); sp.wrap(_Music(1), 1)
            started, ready = sp.track_started_at, asyncio.Event()
            async def update(started_at, starting_next):
                await ready.wait()
                return _clip(9)
            sp.between_songs = AsyncMock(side_effect=update)
            sp.song_change(2); await _settle()
            sp.start_failed(2)                        # the next track fails before the update is ready
            sp.song_change(3)                         # auto-next moves on to the one after it
            self.assertTrue(sp.in_gap)
            ready.set(); await _settle()
            self.assertEqual(vc.pump(), [9, 9, 9])
            sp.between_songs.assert_awaited_once_with(started, True)
        _run(go())

    def test_a_mixer_whose_song_never_started_takes_no_clips(self):
        async def go():
            vc, _, sp = _setup()
            sp.song_change(1)
            sp.wrap(_Music(100), 1)                   # then voice_client.play() raises
            sp.start_failed(1)
            self.assertEqual(await sp.say(_clip(5)), "solo")
            self.assertEqual(len(vc.plays), 1)
        _run(go())

    def test_end_of_queue_speaks_waiting_clips_then_the_update(self):
        async def go():
            vc, _, sp = _setup()
            sp.song_change(1); sp.wrap(_Music(1), 1)
            started = sp.track_started_at
            sp.between_songs = AsyncMock(return_value=_clip(9))
            sp.queue_after(_clip(1))
            await sp.end_of_queue()
            self.assertEqual(vc.pump(), [1, 1, 1, 9, 9, 9])
            sp.between_songs.assert_awaited_once_with(started, False)
            self.assertIsNone(sp.track_started_at)
        _run(go())


class TestCutAndReset(unittest.TestCase):
    def test_cut_stops_solo_and_held_but_keeps_after_song(self):
        async def go():
            vc, _, sp = _setup()
            await sp.say(_clip(1))
            sp.queue_after(_clip(2))
            sp.cut()
            await _settle()
            self.assertEqual(vc.stops, 1)
            self.assertEqual(sp.waiting_clips(), 1)
            sp.cut(drop_after_song=True)
            self.assertEqual(sp.waiting_clips(), 0)
        _run(go())

    def test_clip_after_a_stopped_song_plays_alone(self):
        async def go():
            vc, _, sp = _setup()
            sp.song_change(1)
            vc.play(sp.wrap(_Music(100), 1))
            vc.stop()                                 # an explicit stop: skip, !stop, radio switch
            sp.cut()
            self.assertEqual(await sp.say(_clip(5)), "solo")
            self.assertEqual(len(vc.plays), 2)
        _run(go())

    def test_clip_right_after_cut_starts_a_new_solo(self):
        async def go():
            vc, _, sp = _setup()
            await sp.say(_clip(1))
            sp.cut()                                  # no settle: the stop's after-callback is still pending
            self.assertEqual(await sp.say(_clip(2)), "solo")
            self.assertEqual(len(vc.plays), 2)
            self.assertEqual(vc.pump(), [2, 2, 2])
        _run(go())

    def test_reset_forgets_everything(self):
        async def go():
            vc, _, sp = _setup()
            sp.song_change(1); sp.wrap(_Music(5), 1)
            sp.queue_after(_clip(2))
            sp.reset()
            self.assertEqual(sp.waiting_clips(), 0)
            self.assertIsNone(sp.track_started_at)
            self.assertFalse(sp.starting)
        _run(go())
