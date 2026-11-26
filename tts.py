"""Text-to-speech helpers: text cleanup, the half-hourly update wording and its time mark."""
import asyncio
import importlib.util
import re
import subprocess
from datetime import datetime
from zoneinfo import ZoneInfo

from speech import FRAME_SIZE

MAX_CHARS = 300
VOICES = {"lv": "lv-LV-EveritaNeural", "en": "en-US-AriaNeural"}
SYNTH_TIMEOUT = 15.0


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


_PLACEHOLDER = re.compile(r"@(laiks|queue)\b", re.IGNORECASE)


def fill_announcement(template: str, now_local: datetime, songs_left: int) -> str:
    """An admin's update text with @laiks as HH:MM and @queue as the songs left. Only these
    two words change; the result still goes through clean_text."""
    values = {"laiks": now_local.strftime("%H:%M"), "queue": str(songs_left)}
    return _PLACEHOLDER.sub(lambda m: values[m[1].lower()], template)


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
                       "(Docker: set WITH_TTS=true in .env and rebuild).")
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
    """Decode an MP3 to 48 kHz stereo s16le with FFmpeg."""
    done = subprocess.run(
        [ffmpeg_path, "-hide_banner", "-loglevel", "error", "-i", "pipe:0",
         "-f", "s16le", "-ar", "48000", "-ac", "2", "pipe:1"],
        input=mp3, capture_output=True, check=True)
    return done.stdout


async def synthesize(text: str, lang: str, ffmpeg_path: str) -> bytes:
    """Speak text in the language's voice; PCM padded with zeros to whole frames."""
    async def _run() -> bytes:
        mp3 = await _fetch_mp3(text, VOICES[lang])
        if not mp3:
            raise TtsError("no audio returned")
        loop = asyncio.get_running_loop()
        pcm = await loop.run_in_executor(None, _decode_to_pcm, mp3, ffmpeg_path)
        if not pcm:
            raise TtsError("decoder produced no audio")
        return pcm + b"\x00" * (-len(pcm) % FRAME_SIZE)

    try:
        return await asyncio.wait_for(_run(), SYNTH_TIMEOUT)
    except TtsError:
        raise
    except asyncio.TimeoutError as e:
        raise TtsError("speech synthesis timed out") from e
    except Exception as e:
        raise TtsError(f"{type(e).__name__}: {e}") from e
