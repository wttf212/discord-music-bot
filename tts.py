"""Text-to-speech helpers: text cleanup, the half-hourly update wording and its time mark."""
import re
from datetime import datetime
from zoneinfo import ZoneInfo

MAX_CHARS = 300
VOICES = {"lv": "lv-LV-EveritaNeural", "en": "en-US-AriaNeural"}

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


def half_hour_mark_passed(started_at: float, now: float, tz_name: str) -> bool:
    """True when the latest local :00/:30 mark at or before now lies strictly after started_at."""
    local = datetime.fromtimestamp(now, ZoneInfo(tz_name))
    mark = local.replace(minute=0 if local.minute < 30 else 30, second=0, microsecond=0)
    return mark.timestamp() > started_at
