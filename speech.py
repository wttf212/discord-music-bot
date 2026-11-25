"""Speech in the voice channel: SpeechMixer lays spoken clips over the music frame by frame,
and Speech decides where each clip goes and runs the gap between songs."""
import asyncio
import audioop
import threading
import time
from collections import deque
from collections.abc import Awaitable, Callable

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

UPDATE_TIMEOUT = 5.0   # the longest a song start waits for the half-hourly update's audio
GAP_MARGIN = 10.0      # slack beyond the gap speech's own length before it counts as stuck


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
                if self._music_ended:
                    self.closed = True  # same locked block as the empty check: add() can't slip in after
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
        with self._lock:
            self.closed = True
        self.finished = True
        if self.music is not None:
            self.music.cleanup()


class SpeechUnavailable(Exception):
    """The voice connection can't take speech right now."""


def _busy(voice_client) -> bool:
    """True while the voice client holds a source; discord.py calls a paused one not playing."""
    return voice_client is not None and (voice_client.is_playing() or voice_client.is_paused())


class Speech:
    """Where each clip goes for one player, and the gap between songs.

    A clip plays over the song in its live mixer, alone in a speech-only "solo" source,
    held for the next song's mixer, or after the current song. Everything here runs on
    the event loop; discord.py's audio thread reaches it only through a solo's
    after-callback, which hops back to the loop.
    """

    def __init__(self, player):
        self._player = player  # _voice_client, _encoder_settings() and is_paused are read at call time
        self.between_songs: Callable[[float | None, bool], Awaitable[bytes | None]] | None = None
        self.track_started_at: float | None = None
        self._gen: int | None = None
        self._starting = False
        self._mixer: SpeechMixer | None = None    # the song's mixer; kept after a natural end until the next change
        self._solo: SpeechMixer | None = None     # speech-only source on the voice client
        self._solo_done: asyncio.Event | None = None
        self._held: list[bytes] = []              # for the next song's mixer
        self._backlog: list[bytes] = []           # behind a finished solo the voice client still holds
        self._after_song: list[bytes] = []        # for the next song change
        self._update_task: asyncio.Task | None = None

    @property
    def starting(self) -> bool:
        return self._starting

    @property
    def in_gap(self) -> bool:
        fetching = self._update_task is not None and not self._update_task.done()
        return self._starting and (self.solo_active or fetching)  # a backlog only exists behind a solo

    @property
    def solo_active(self) -> bool:
        return self._solo is not None

    def waiting_clips(self) -> int:
        count = len(self._held) + len(self._backlog) + len(self._after_song)
        for source in (self._mixer, self._solo):
            if source is not None:
                count += source.pending_clips()
        return count

    async def say(self, pcm: bytes) -> str:
        """Speak pcm as soon as the gap rules allow; returns where it went."""
        vc = self._player._voice_client
        if vc is None or not vc.is_connected():
            raise SpeechUnavailable("not connected to a voice channel")
        mixer = self._mixer
        if mixer is not None and mixer.add(pcm):  # add() refuses once the song's audio has ended
            return "paused" if self._player.is_paused else "mixer"
        # Held while a song starts, or while the refused mixer is still on the voice client,
        # finishing the song's speech. Once the client lets go of it the song is over and the
        # clip plays alone, although that mixer stays remembered until the next song change.
        if self._starting or (mixer is not None and _busy(vc) and getattr(vc, "source", None) is mixer):
            self._held.append(pcm)
            return "held"
        self._queue_solo([pcm])
        return "solo"

    def queue_after(self, pcm: bytes) -> None:
        self._after_song.append(pcm)

    def cut(self, drop_after_song: bool = False) -> None:
        """Silence all speech now. The after-song list survives unless drop_after_song."""
        if self._update_task is not None:
            self._update_task.cancel()
            self._update_task = None
        self._held.clear()
        self._backlog.clear()
        vc = self._player._voice_client
        busy = _busy(vc)
        if self._mixer is not None:
            self._mixer.clear_speech()
            if not busy:
                self._mixer = None  # its song was stopped
        if self._solo is not None:
            if busy:
                vc.stop()
            self._retire_solo()  # a clip sent right after this starts a new solo
        if drop_after_song:
            self._after_song.clear()

    def reset(self) -> None:
        self.cut(drop_after_song=True)
        self._mixer = None
        self._starting = False
        self.track_started_at = None

    def song_change(self, gen: int) -> None:
        """A track is about to start: speak the after-song clips and fetch the update."""
        self._gen = gen
        self._starting = True
        self._mixer = None  # the old song is over
        after, self._after_song = self._after_song, []
        self._play_solo(after)
        # Taken, not just read: a retry after a failed start is another song change for the
        # same finished track, and must not fetch and speak its update a second time. For the
        # same reason a fetch still running from that failed start is kept, not doubled.
        started_at, self.track_started_at = self.track_started_at, None
        if self.between_songs is not None and (self._update_task is None or self._update_task.done()):
            self._update_task = asyncio.create_task(
                self._fetch_update(self.between_songs, started_at, True))

    async def wait_for_gap(self, gen: int) -> None:
        """Hold the song until the gap's speech has played. Every wait is bounded."""
        task = self._update_task
        if gen == self._gen and task is not None and not task.done():
            await asyncio.wait({task}, timeout=UPDATE_TIMEOUT)
            if gen == self._gen and not task.done():
                task.cancel()
                print(f"[tts] Half-hourly update skipped: not ready within {UPDATE_TIMEOUT:g} s")
        while gen == self._gen and self._solo is not None:  # a backlog only exists behind a solo
            solo, done = self._solo, self._solo_done
            timeout = solo.pending_frames() * 0.02 + GAP_MARGIN  # 20 ms frames
            try:
                await asyncio.wait_for(done.wait(), timeout)
            except asyncio.TimeoutError:
                if gen == self._gen and self._solo is solo:  # stuck: the song gets the voice client
                    vc = self._player._voice_client
                    if vc is not None:
                        vc.stop()
                    self._retire_solo()
                    self._backlog.clear()
                    return

    def wrap(self, music: discord.AudioSource, gen: int) -> SpeechMixer:
        """The mixer a song plays through. The current start's mixer takes the held clips."""
        mixer = SpeechMixer(music)
        if gen == self._gen:
            self._starting = False
            held, self._held = self._held, []
            for pcm in held:
                mixer.add(pcm)
            self.track_started_at = time.time()
            self._mixer = mixer
        return mixer

    def start_failed(self, gen: int) -> None:
        """The start for gen failed: its held clips play alone instead."""
        if gen != self._gen:
            return
        self._starting = False
        self._mixer = None  # a mixer wrapped for this start never reached the voice client
        held, self._held = self._held, []
        self._play_solo(held)

    async def end_of_queue(self) -> None:
        """Nothing plays next: speak the waiting clips, then the update."""
        clips = self._after_song + self._held
        self._after_song, self._held = [], []
        self._play_solo(clips)
        # Taken before the wait, so a song that starts meanwhile keeps its own start time.
        started_at, self.track_started_at = self.track_started_at, None
        if self.between_songs is None:
            return
        try:
            await asyncio.wait_for(self._fetch_update(self.between_songs, started_at, False),
                                   UPDATE_TIMEOUT)
        except asyncio.TimeoutError:
            print(f"[tts] Half-hourly update skipped: not ready within {UPDATE_TIMEOUT:g} s")

    async def _fetch_update(self, provider, started_at: float | None, starting_next: bool) -> None:
        """Speak the half-hourly update if the provider has one."""
        try:
            pcm = await provider(started_at, starting_next)
            if pcm:
                self._play_solo([pcm])
        except Exception as e:
            print(f"[tts] Half-hourly update skipped: {e}")

    def _play_solo(self, clips: list[bytes]) -> None:
        """_queue_solo for the song-change paths, which must never fail a song start."""
        if not clips:
            return
        try:
            self._queue_solo(clips)
        except SpeechUnavailable as e:
            print(f"[tts] Speech dropped: {e}")

    def _queue_solo(self, clips: list[bytes]) -> None:
        """Speech with no music under it: join the open solo, wait behind a finished one the
        voice client still holds, or start a new one. Raises SpeechUnavailable."""
        solo = self._solo
        rest = list(clips)
        while rest and solo is not None and solo.add(rest[0]):
            rest.pop(0)
        if not rest:
            return
        if solo is not None and _busy(self._player._voice_client):
            self._backlog.extend(rest)
            return
        self._start_solo(rest)

    def _start_solo(self, clips: list[bytes]) -> None:
        """Put a new speech-only source on the voice client. Raises SpeechUnavailable."""
        clips = self._backlog + clips  # older clips first
        self._backlog = []
        self._retire_solo()  # it has finished; its after-callback may not have reached the loop yet
        vc = self._player._voice_client
        if vc is None or _busy(vc):
            raise SpeechUnavailable("the voice client can't take speech right now")
        solo = SpeechMixer(None)
        for pcm in clips:
            solo.add(pcm)
        loop = asyncio.get_running_loop()

        def after(error):  # runs on discord.py's audio thread
            loop.call_soon_threadsafe(self._solo_ended, solo, error)

        try:
            vc.play(solo, after=after, **self._player._encoder_settings())
        except discord.ClientException as e:
            raise SpeechUnavailable(str(e)) from e
        self._solo, self._solo_done = solo, asyncio.Event()

    def _solo_ended(self, solo: SpeechMixer, error: Exception | None) -> None:
        """The event-loop side of a solo's after-callback."""
        if error:
            print(f"[tts] Speech playback ended with error: {error}")
        if solo is not self._solo:
            return  # stopped by cut() or wait_for_gap(), or replaced: no longer ours to act on
        self._retire_solo()
        if self._backlog:
            clips, self._backlog = self._backlog, []
            self._play_solo(clips)

    def _retire_solo(self) -> None:
        """Forget the current solo and wake whoever waits for it to end."""
        if self._solo is not None:
            self._solo = None
            self._solo_done.set()
