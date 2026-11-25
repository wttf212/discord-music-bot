"""Speech mixing: lays spoken clips over the music frame by frame."""
import audioop
import threading
from collections import deque

import discord

try:
    from discord.opus import Encoder as _OpusEncoder
    FRAME_SIZE = _OpusEncoder.FRAME_SIZE
except Exception:
    FRAME_SIZE = 3840

SILENCE = b"\x00" * FRAME_SIZE

DUCK_GAIN = 0.25
DUCK_ATTACK_FRAMES = 8
DUCK_RELEASE_FRAMES = 20


class SpeechMixer(discord.AudioSource):
    """Wraps the music source and adds queued speech frames on top of it.

    read() runs on discord.py's audio thread every 20 ms, so it never blocks or does I/O;
    add() and clear_speech() run on the event loop, hence the lock around the clip queue.
    """

    def __init__(self, music: discord.AudioSource | None):
        self.music = music
        self.closed = False
        self.finished = False
        self._music_ended = music is None
        self._gain = 1.0
        self._clips: deque[memoryview] = deque()
        self._offset = 0
        self._lock = threading.Lock()

    def add(self, pcm: bytes) -> bool:
        if self.closed:
            return False
        rest = len(pcm) % FRAME_SIZE
        if rest:
            pcm = pcm + b"\x00" * (FRAME_SIZE - rest)
        if not pcm:
            return True
        with self._lock:
            if self.closed:
                return False
            self._clips.append(memoryview(pcm))
        return True

    def clear_speech(self) -> None:
        with self._lock:
            self._clips.clear()
            self._offset = 0

    def pending_clips(self) -> int:
        with self._lock:
            return len(self._clips)

    def pending_frames(self) -> int:
        with self._lock:
            return sum(len(c) for c in self._clips) // FRAME_SIZE - self._offset // FRAME_SIZE

    def _next_speech_frame(self) -> bytes | None:
        with self._lock:
            if not self._clips:
                return None
            clip = self._clips[0]
            frame = bytes(clip[self._offset:self._offset + FRAME_SIZE])
            self._offset += FRAME_SIZE
            if self._offset >= len(clip):
                self._clips.popleft()
                self._offset = 0
            return frame

    def read(self) -> bytes:
        music_frame = b""
        if not self._music_ended:
            music_frame = self.music.read()  # exceptions propagate unchanged
            if not music_frame:
                self._music_ended = True
                self.closed = True
        try:
            speech_frame = self._next_speech_frame()
            if speech_frame is None:
                if self._music_ended:
                    self.closed = True
                    self.finished = True
                    return b""
                if self._gain >= 1.0:
                    return music_frame
                self._gain = min(1.0, self._gain + (1 - DUCK_GAIN) / DUCK_RELEASE_FRAMES)
                return audioop.mul(music_frame, 2, self._gain)
            if self._music_ended:
                base = SILENCE
            else:
                self._gain = max(DUCK_GAIN, self._gain - (1 - DUCK_GAIN) / DUCK_ATTACK_FRAMES)
                base = audioop.mul(music_frame, 2, self._gain)
            return audioop.add(base, speech_frame, 2)
        except Exception as e:
            print(f"[tts] mixer error, dropping speech: {e}")
            self.clear_speech()
            self._gain = 1.0
            return music_frame

    def is_opus(self) -> bool:
        return False

    def cleanup(self) -> None:
        self.closed = True
        self.finished = True
        if self.music is not None:
            self.music.cleanup()
