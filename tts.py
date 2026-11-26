"""Text-to-speech helpers: text cleanup, the half-hourly update wording and its time mark."""
import asyncio
import audioop
import importlib.util
import re
import subprocess
from datetime import datetime
from zoneinfo import ZoneInfo

from speech import DUCK_ATTACK_FRAMES, FRAME_SIZE

MAX_CHARS = 300
VOICES = {"lv": "lv-LV-EveritaNeural", "en": "en-US-AriaNeural"}   # the female voices
MALE_VOICES = {"lv": "lv-LV-NilsNeural", "en": "en-US-AndrewNeural"}
SYNTH_TIMEOUT = 15.0
# Speech is compressed, then limited to PEAK_CEILING. Once the mixer's dip has settled, music is at 25%, so
# speech (at most 0.70) plus music (at most 0.25) stays under full scale. The dip takes DUCK_ATTACK_FRAMES
# to settle, and _ensure_lead_in makes sure speech starts only after that.
SPEECH_FILTER = "acompressor=threshold=-26dB:ratio=4:attack=5:release=80:makeup=8,alimiter=limit=0.70:level=false"
PEAK_CEILING = 0.70
MAX_LEVEL_GAIN = 8.0   # a nearly silent clip is not blown up into noise


class TtsError(Exception):
    """Speech could not be produced (service, decoder, empty audio or timeout)."""

_CUSTOM_EMOJI = re.compile(r"<a?:(\w+):\d+>")
_URL = re.compile(r"https?://\S+")
_STRIP = dict.fromkeys(map(ord, "*_~`|>@\u200b"))


def clean_text(text: str, lang: str) -> str:
    """Make chat text speakable: emoji to names, links to a word, markup dropped."""
    text = _CUSTOM_EMOJI.sub(r"\1", text)
    text = _URL.sub("saite" if lang == "lv" else "link", text)
    text = text.translate(_STRIP)
    return " ".join(text.split())


def announcement_text(lang: str, now_local: datetime, songs_left: int) -> str:
    """The spoken time-and-queue update. songs_left includes the track about to start."""
    clock = now_local.strftime("%H:%M")
    n = songs_left
    if lang == "lv":
        if n == 0:
            tail = "Rinda ir tukša."
        else:
            word = "dziesma" if n % 10 == 1 and n % 100 != 11 else "dziesmas"
            tail = f"Rindā vēl {n} {word}."
        return f"Pulkstenis ir {clock}. {tail}"
    if n == 0:
        tail = "The queue is empty."
    else:
        tail = f"{n} more {'song' if n == 1 else 'songs'} in the queue."
    return f"It's {clock}. {tail}"


_PLACEHOLDER = re.compile(r"(?<!\w)@((?ai:laiks|queue))(?!\w)")


def fill_announcement(template: str, now_local: datetime, songs_left: int) -> str:
    """An admin's update text with @laiks as HH:MM and @queue as the songs left. Only these
    two words change; the result still goes through clean_text."""
    values = {"laiks": now_local.strftime("%H:%M"), "queue": str(songs_left)}
    return _PLACEHOLDER.sub(lambda m: values.get(m[1].lower(), m[0]), template)


def half_hour_mark_passed(started_at: float, now: float, tz_name: str) -> bool:
    """True when the latest local :00/:30 mark at or before now lies strictly after started_at."""
    local = datetime.fromtimestamp(now, ZoneInfo(tz_name))
    mark = local.replace(minute=0 if local.minute < 30 else 30, second=0, microsecond=0)
    return mark.timestamp() > started_at


def wanted(config: dict) -> bool:
    """True when config.yaml turns text-to-speech on (off by default). Only `enabled: true`
    under `tts:` does; any other shape, such as `tts: true` or a quoted "false", leaves it off."""
    section = config.get("tts")
    return isinstance(section, dict) and section.get("enabled") is True


def installed() -> bool:
    """True when the optional edge-tts package can be imported."""
    return importlib.util.find_spec("edge_tts") is not None


def startup_status(config: dict) -> tuple[bool, str]:
    """(enabled, log line): on only when wanted and the package is installed."""
    if not wanted(config):
        return False, "Text-to-speech is off (set tts.enabled: true in config.yaml to turn it on)."
    if not installed():
        return False, ("Warning: tts.enabled is true, but the edge-tts package is not installed, "
                       "so text-to-speech stays off. Install it with: pip install -r requirements-tts.txt "
                       "(Docker: rebuild the image with docker compose up -d --build).")
    return True, "Text-to-speech is on."


async def _fetch_mp3(text: str, voice: str) -> bytes:
    """Ask Microsoft's Edge voice service for the MP3 of text."""
    import edge_tts  # optional package; an ImportError becomes a TtsError in synthesize
    chunks = []
    async for chunk in edge_tts.Communicate(text, voice).stream():
        if chunk["type"] == "audio":
            chunks.append(chunk["data"])
    return b"".join(chunks)


def _decode_to_pcm(mp3: bytes, ffmpeg_path: str) -> bytes:
    """Decode an MP3 to 48 kHz stereo s16le with FFmpeg, compressing and limiting it, then leveling it."""
    done = subprocess.run(
        [ffmpeg_path, "-hide_banner", "-loglevel", "error", "-i", "pipe:0",
         "-af", SPEECH_FILTER, "-f", "s16le", "-ar", "48000", "-ac", "2", "pipe:1"],
        input=mp3, capture_output=True, check=True)
    return _level(done.stdout)


def _level(pcm: bytes) -> bytes:
    """Scale s16le PCM so its sample peak is PEAK_CEILING of full scale (gain capped at MAX_LEVEL_GAIN)."""
    pcm = pcm[:len(pcm) - len(pcm) % 2]
    peak = audioop.max(pcm, 2)
    if not peak:
        return pcm
    return audioop.mul(pcm, 2, min(PEAK_CEILING * 32767 / peak, MAX_LEVEL_GAIN))


def _ensure_lead_in(pcm: bytes) -> bytes:
    """Make speech start only after the mixer's music dip has settled: prepend silent frames
    until the clip has DUCK_ATTACK_FRAMES of quiet (peak under 5% of full scale) at its start."""
    lead = 0
    for i in range(0, len(pcm) - FRAME_SIZE + 1, FRAME_SIZE):
        if audioop.max(pcm[i:i + FRAME_SIZE], 2) >= 0.05 * 32767:
            break
        lead += 1
    return b"\x00" * FRAME_SIZE * max(0, DUCK_ATTACK_FRAMES - lead) + pcm


async def synthesize(text: str, lang: str, ffmpeg_path: str, voice: str = "female") -> bytes:
    """Speak text in the language's female or male voice; PCM padded with zeros to whole frames."""
    async def _run() -> bytes:
        mp3 = await _fetch_mp3(text, (MALE_VOICES if voice == "male" else VOICES)[lang])
        if not mp3:
            raise TtsError("no audio returned")
        loop = asyncio.get_running_loop()
        pcm = await loop.run_in_executor(None, _decode_to_pcm, mp3, ffmpeg_path)
        if not pcm:
            raise TtsError("decoder produced no audio")
        return _ensure_lead_in(pcm + b"\x00" * (-len(pcm) % FRAME_SIZE))

    try:
        return await asyncio.wait_for(_run(), SYNTH_TIMEOUT)
    except TtsError:
        raise
    except asyncio.TimeoutError as e:
        raise TtsError("speech synthesis timed out") from e
    except Exception as e:
        raise TtsError(f"{type(e).__name__}: {e}") from e
