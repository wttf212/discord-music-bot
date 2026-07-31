"""Tests for the PCM jitter buffer that stops the audio thread from rushing/catching up."""
import os
import sys
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import audio_player
from audio_player import _BufferedAudioSource, _PCM_FRAME_SIZE

F = _PCM_FRAME_SIZE
SILENCE = b"\x00" * F


class _ListSource:
    """A source that yields a fixed list of frames then EOF."""
    def __init__(self, frames):
        self._frames = list(frames)
        self.cleaned = False

    def read(self):
        return self._frames.pop(0) if self._frames else b""

    def is_opus(self):
        return False

    def cleanup(self):
        self.cleaned = True


class _BlockingSource:
    """A source whose read() blocks until released (simulates an underrun)."""
    def __init__(self):
        self._go = threading.Event()

    def read(self):
        self._go.wait()
        return b""

    def is_opus(self):
        return False

    def cleanup(self):
        self._go.set()


class TestBufferedAudioSource(unittest.TestCase):
    def _wait_eof(self, buf, timeout=2.0):
        end = time.time() + timeout
        while not buf._eof.is_set() and time.time() < end:
            time.sleep(0.01)

    def test_emits_first_frame_then_buffered_in_order(self):
        frames = [b"A" * F, b"B" * F, b"C" * F]
        src = _ListSource(frames)
        buf = _BufferedAudioSource(src, first_frame=b"F" * F, buffer_frames=10)
        self._wait_eof(buf)
        self.assertEqual(buf.read(), b"F" * F)  # primed first frame
        self.assertEqual(buf.read(), b"A" * F)
        self.assertEqual(buf.read(), b"B" * F)
        self.assertEqual(buf.read(), b"C" * F)
        self.assertEqual(buf.read(), b"")       # drained + EOF → stop

    def test_underrun_returns_silence_not_block(self):
        src = _BlockingSource()
        buf = _BufferedAudioSource(src, first_frame=b"")
        # No first frame, buffer empty, stream not finished → silence (must not block).
        self.assertEqual(buf.read(), SILENCE)
        self.assertEqual(len(buf.read()), F)
        buf.cleanup()

    def test_first_frame_before_underrun(self):
        buf = _BufferedAudioSource(_BlockingSource(), first_frame=b"X" * F)
        self.assertEqual(buf.read(), b"X" * F)  # first frame emitted
        self.assertEqual(buf.read(), SILENCE)   # then underrun → silence
        buf.cleanup()

    def test_cleanup_closes_inner(self):
        src = _ListSource([b"A" * F])
        buf = _BufferedAudioSource(src, first_frame=b"")
        self._wait_eof(buf)
        buf.cleanup()
        self.assertTrue(src.cleaned)

    def test_frame_size_is_3840(self):
        self.assertEqual(F, 3840)


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestStallDetection(unittest.TestCase):
    """Silence is right for a hiccup, wrong for a dead stream.

    Without a stall cut-off the track "plays" inaudibly for its full remaining length
    while the queue waits and nothing in the log says why.
    """

    class _NeverYields:
        """A source that is open but never produces data — a stalled CDN."""
        def __init__(self):
            self.blocked = threading.Event()

        def read(self):
            self.blocked.wait(30)     # hold the fill thread; never deliver a frame
            return b""

        def cleanup(self):
            self.blocked.set()

    def test_brief_underrun_yields_silence_not_eof(self):
        src = audio_player._BufferedAudioSource(self._NeverYields(), first_frame=b"x" * 8)
        try:
            self.assertEqual(src.read(), b"x" * 8)          # primed frame
            for _ in range(5):
                self.assertEqual(src.read(), src._SILENCE,
                                 "a momentary gap must not end the track")
        finally:
            src.cleanup()

    def test_sustained_stall_ends_the_track(self):
        src = audio_player._BufferedAudioSource(self._NeverYields())
        try:
            for _ in range(audio_player._STALL_FRAMES - 1):
                self.assertEqual(src.read(), src._SILENCE)
            self.assertEqual(src.read(), b"",
                             "a stalled stream must end so auto-next can move on")
        finally:
            src.cleanup()

    def test_stall_threshold_is_a_sane_duration(self):
        self.assertGreaterEqual(audio_player._STALL_SECONDS, 5.0)
        self.assertLessEqual(audio_player._STALL_SECONDS, 30.0)

    def test_recovered_stream_resets_the_stall_counter(self):
        """A gap followed by data must not leave the track primed to die later."""
        class _Resumes:
            def __init__(self):
                self.sent = False
            def read(self):
                if not self.sent:
                    self.sent = True
                    return b"y" * audio_player._PCM_FRAME_SIZE
                time.sleep(30)
                return b""
            def cleanup(self):
                pass

        src = audio_player._BufferedAudioSource(_Resumes())
        try:
            deadline = time.monotonic() + 2
            got_data = False
            while time.monotonic() < deadline and not got_data:
                if src.read() == b"y" * audio_player._PCM_FRAME_SIZE:
                    got_data = True
            self.assertTrue(got_data, "the real frame never arrived")
            self.assertEqual(src._starved_frames, 0,
                             "receiving data must reset the stall counter")
        finally:
            src.cleanup()
