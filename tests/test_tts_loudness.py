import audioop
import os
import struct
import sys
import unittest
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import asyncio

import speech
import tts

F = speech.FRAME_SIZE
LOUD = struct.pack("<h", 20000) * (F // 2)
QUIET = struct.pack("<h", 100) * (F // 2)
SILENT = b"\x00" * F


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


class LeadInTests(unittest.TestCase):
    def test_a_loud_start_gets_the_full_attack_of_silence(self):
        out = tts._ensure_lead_in(LOUD * 2)
        self.assertEqual(out, SILENT * speech.DUCK_ATTACK_FRAMES + LOUD * 2)
        self.assertEqual(speech.DUCK_ATTACK_FRAMES, 8)

    def test_quiet_frames_count_toward_the_lead_in(self):
        out = tts._ensure_lead_in(QUIET * 3 + LOUD)
        self.assertEqual(out, SILENT * 5 + QUIET * 3 + LOUD)

    def test_a_clip_that_already_starts_quietly_is_unchanged(self):
        pcm = QUIET * 10 + LOUD
        self.assertEqual(tts._ensure_lead_in(pcm), pcm)

    def test_synthesize_output_starts_with_the_silent_attack(self):
        with patch.object(tts, "_fetch_mp3", AsyncMock(return_value=b"mp3")),                 patch.object(tts, "_decode_to_pcm", return_value=LOUD * 2):
            out = asyncio.run(tts.synthesize("hi", "en", "ffmpeg"))
        self.assertEqual(out[:F * speech.DUCK_ATTACK_FRAMES], SILENT * speech.DUCK_ATTACK_FRAMES)
        self.assertEqual(len(out) % F, 0)


if __name__ == "__main__":
    unittest.main()
