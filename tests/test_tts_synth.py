"""tts.synthesize: edge-tts MP3 -> FFmpeg -> whole PCM frames, every failure a TtsError."""
import asyncio, importlib.util, os, subprocess, sys, unittest
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import speech, tts

F = speech.FRAME_SIZE


class TestSynthesize(unittest.TestCase):
    def test_pads_to_whole_frames_and_uses_the_language_voice(self):
        fetch = AsyncMock(return_value=b"mp3")
        with patch.object(tts, "_fetch_mp3", fetch), \
             patch.object(tts, "_decode_to_pcm", return_value=b"\x01" * (F + 10)):
            pcm = asyncio.run(tts.synthesize("sveiki", "lv", "ffmpeg"))
        self.assertEqual(len(pcm), 2 * F)
        self.assertEqual(pcm[F + 10:], b"\x00" * (F - 10))
        fetch.assert_awaited_once_with("sveiki", "lv-LV-EveritaNeural")

    def test_no_audio_is_an_error(self):
        with patch.object(tts, "_fetch_mp3", AsyncMock(return_value=b"")):
            with self.assertRaises(tts.TtsError):
                asyncio.run(tts.synthesize("😀😀😀", "en", "ffmpeg"))

    def test_service_failure_is_an_error(self):
        with patch.object(tts, "_fetch_mp3", AsyncMock(side_effect=OSError("blocked"))):
            with self.assertRaises(tts.TtsError):
                asyncio.run(tts.synthesize("hi", "en", "ffmpeg"))

    def test_decoder_failure_is_an_error(self):
        with patch.object(tts, "_fetch_mp3", AsyncMock(return_value=b"mp3")), \
             patch.object(tts, "_decode_to_pcm", side_effect=subprocess.CalledProcessError(1, "ffmpeg")):
            with self.assertRaises(tts.TtsError):
                asyncio.run(tts.synthesize("hi", "en", "ffmpeg"))

    def test_slow_service_times_out(self):
        async def slow(*args):
            await asyncio.sleep(5)
        with patch.object(tts, "_fetch_mp3", slow), patch.object(tts, "SYNTH_TIMEOUT", 0.05):
            with self.assertRaises(tts.TtsError):
                asyncio.run(tts.synthesize("hi", "en", "ffmpeg"))

    def test_decoder_command(self):
        PCM = bytes([16, 0]) * 8
        done = subprocess.CompletedProcess([], 0, stdout=PCM, stderr=b"")
        with patch.object(tts.subprocess, "run", return_value=done) as run:
            self.assertEqual(tts._decode_to_pcm(b"mp3", "ffmpeg"), tts._level(PCM))
        args, kwargs = run.call_args
        self.assertEqual(args[0], ["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", "pipe:0",
                                   "-af", tts.SPEECH_FILTER, "-f", "s16le", "-ar", "48000", "-ac", "2", "pipe:1"])
        self.assertEqual(kwargs["input"], b"mp3")
        self.assertTrue(kwargs["check"])

    @unittest.skipUnless(importlib.util.find_spec("edge_tts"), "edge-tts is not installed")
    def test_fetch_keeps_only_audio_chunks(self):
        class _Communicate:
            def __init__(self, text, voice):
                pass

            async def stream(self):
                for chunk in ({"type": "audio", "data": b"a"}, {"type": "WordBoundary"},
                              {"type": "audio", "data": b"b"}):
                    yield chunk
        with patch("edge_tts.Communicate", _Communicate):
            self.assertEqual(asyncio.run(tts._fetch_mp3("hi", "v")), b"ab")
