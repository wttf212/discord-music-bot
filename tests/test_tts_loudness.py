import audioop
import struct
import unittest

import tts


def _pcm(value: int, n: int = 480) -> bytes:
    return struct.pack("<h", value) * n


class LevelTests(unittest.TestCase):
    def test_quiet_payload_within_the_gain_cap_is_raised_to_the_ceiling(self):
        peak = audioop.max(tts._level(_pcm(4000)), 2)
        self.assertLessEqual(abs(peak - int(0.70 * 32767)), 1)

    def test_loud_payload_is_scaled_down_to_the_ceiling(self):
        peak = audioop.max(tts._level(_pcm(32000)), 2)
        self.assertLessEqual(abs(peak - int(0.70 * 32767)), 1)

    def test_whisper_gain_is_capped(self):
        self.assertEqual(audioop.max(tts._level(_pcm(100)), 2), 800)

    def test_silence_and_empty_are_unchanged(self):
        self.assertEqual(tts._level(b""), b"")
        silence = b"\x00" * 960
        self.assertEqual(tts._level(silence), silence)

    def test_odd_length_input_is_accepted(self):
        out = tts._level(_pcm(1000) + b"\x01")
        self.assertEqual(len(out) % 2, 0)

    def test_filter_chain_is_exact(self):
        self.assertEqual(
            tts.SPEECH_FILTER,
            "acompressor=threshold=-26dB:ratio=4:attack=5:release=80:makeup=8,"
            "alimiter=limit=0.70:level=false")


if __name__ == "__main__":
    unittest.main()
