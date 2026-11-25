"""SpeechMixer: music passes through untouched, dips under speech, and outlives neither."""
import os, sys, unittest
from array import array

import discord

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import speech
from speech import SpeechMixer, DUCK_ATTACK_FRAMES, DUCK_RELEASE_FRAMES

F = speech.FRAME_SIZE


def _tone(value, frames=1):
    return array("h", [value] * (F // 2 * frames)).tobytes()


def _sample(frame):
    return array("h", frame[:2])[0]


class _Music(discord.AudioSource):
    def __init__(self, frames, value=1000):
        self.left, self.value, self.cleaned = frames, value, False

    def read(self):
        if self.left <= 0:
            return b""
        self.left -= 1
        return _tone(self.value)

    def is_opus(self):
        return False

    def cleanup(self):
        self.cleaned = True


class TestPassThrough(unittest.TestCase):
    def test_no_speech_returns_the_music_frame_unchanged(self):
        mixer = SpeechMixer(_Music(3))
        self.assertEqual([mixer.read() for _ in range(3)], [_tone(1000)] * 3)
        self.assertEqual(mixer.read(), b"")
        self.assertTrue(mixer.finished and mixer.closed)

    def test_cleanup_reaches_the_music_and_closes_the_mixer(self):
        music = _Music(1)
        mixer = SpeechMixer(music)
        mixer.cleanup()
        self.assertTrue(music.cleaned)
        self.assertTrue(mixer.closed and mixer.finished)
        self.assertFalse(mixer.add(_tone(1)))
        self.assertFalse(mixer.is_opus())
        self.assertIs(mixer.music, music)


class TestDip(unittest.TestCase):
    def test_music_dips_under_speech_and_comes_back(self):
        mixer = SpeechMixer(_Music(200, 1000))
        mixer.add(_tone(0, frames=40))            # silent speech isolates the music's gain
        gains = [_sample(mixer.read()) / 1000 for _ in range(40 + DUCK_RELEASE_FRAMES)]
        self.assertAlmostEqual(gains[DUCK_ATTACK_FRAMES - 1], 0.25, places=2)
        self.assertTrue(all(abs(g - 0.25) < 0.01 for g in gains[DUCK_ATTACK_FRAMES:40]))
        self.assertAlmostEqual(gains[-1], 1.0, places=2)
        self.assertEqual(mixer.read(), _tone(1000))   # byte-identical again

    def test_speech_is_added_on_top(self):
        mixer = SpeechMixer(_Music(20, 1000))
        mixer.add(_tone(500, frames=20))
        frames = [mixer.read() for _ in range(DUCK_ATTACK_FRAMES + 1)]
        self.assertEqual(_sample(frames[-1]), 250 + 500)

    def test_sum_is_clipped(self):
        mixer = SpeechMixer(_Music(20, 30000))
        mixer.add(_tone(30000, frames=20))
        frames = [mixer.read() for _ in range(DUCK_ATTACK_FRAMES + 1)]
        self.assertEqual(_sample(frames[-1]), 32767)


class TestEndings(unittest.TestCase):
    def test_speech_outlives_the_music(self):
        mixer = SpeechMixer(_Music(2))
        mixer.add(_tone(700, frames=5))
        frames = [mixer.read() for _ in range(5)]
        self.assertTrue(mixer.closed)
        self.assertEqual([_sample(f) for f in frames[2:]], [700, 700, 700])
        self.assertEqual(mixer.read(), b"")

    def test_closed_mixer_refuses_clips(self):
        mixer = SpeechMixer(_Music(1))
        mixer.read(); mixer.read()
        self.assertFalse(mixer.add(_tone(1)))

    def test_speech_only_mixer_ends_with_its_clips(self):
        mixer = SpeechMixer(None)
        self.assertTrue(mixer.add(_tone(5, frames=2)))
        self.assertEqual([_sample(mixer.read()) for _ in range(2)], [5, 5])
        self.assertEqual(mixer.read(), b"")
        self.assertTrue(mixer.closed and mixer.finished)
        self.assertFalse(mixer.add(_tone(5)))

    def test_partial_frames_are_padded(self):
        mixer = SpeechMixer(None)
        mixer.add(b"\x01\x00" * 10)
        self.assertEqual(mixer.pending_frames(), 1)
        self.assertEqual(len(mixer.read()), F)


class TestBookkeeping(unittest.TestCase):
    def test_pending_counts_and_clear(self):
        mixer = SpeechMixer(_Music(50))
        mixer.add(_tone(1, frames=2)); mixer.add(_tone(2, frames=2))
        self.assertEqual((mixer.pending_clips(), mixer.pending_frames()), (2, 4))
        mixer.read(); mixer.read()
        self.assertEqual(mixer.pending_clips(), 1)
        mixer.clear_speech()
        self.assertEqual(mixer.pending_clips(), 0)

    def test_a_bad_music_frame_never_raises(self):
        class _Short(_Music):
            def read(self):
                return b"\x01" * 100              # not a whole frame: audioop.add would raise
        mixer = SpeechMixer(_Short(5))
        mixer.add(_tone(1, frames=3))
        self.assertEqual(mixer.read(), b"\x01" * 100)
        self.assertEqual(mixer.pending_clips(), 0)
