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
