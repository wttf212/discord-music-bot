import contextlib
import json
import os
import tempfile
import threading
import time

SETTINGS_FILE = os.environ.get(
    "GUILD_SETTINGS_FILE",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "guild_settings.json")
)

# Every setter is read-modify-write on one shared file, and they run from command
# handlers for different guilds. Without this lock two overlapping saves interleave and
# the later read can miss the earlier write.
_settings_lock = threading.RLock()

# Windows-only: how long to keep retrying os.replace() when something else holds the
# settings file open. Generous because losing a setting is worse than a slow save.
_REPLACE_TIMEOUT = 2.0


def load_settings() -> dict:
    if not os.path.isfile(SETTINGS_FILE):
        return {}
    with _settings_lock:
        try:
            with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            # A corrupt file must not take the bot down on every settings read.
            print(f"[guild_settings] Could not read {SETTINGS_FILE}: {e}")
            return {}


def save_settings(data: dict):
    """Write settings atomically.

    open(SETTINGS_FILE, "w") truncates before writing, so a crash, container stop or
    overlapping write mid-dump left a truncated file — and every guild lost its allowed
    channel, bitrate, EQ and admin list at once. Serialise to a temp file in the same
    directory, then os.replace() it into place: atomic on POSIX and on Windows, so the
    settings file is only ever the old contents or the new ones.
    """
    directory = os.path.dirname(os.path.abspath(SETTINGS_FILE)) or "."
    with _settings_lock:
        os.makedirs(directory, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(dir=directory, prefix=".guild_settings.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
                f.flush()
                os.fsync(f.fileno())   # the rename is atomic; the CONTENT must be on disk first
            # Windows refuses to rename over a file another process has open, and an
            # AV scanner, backup agent or the indexer can hold it briefly. Retry on a
            # time budget rather than a fixed count so a loaded box doesn't turn a
            # transient lock into a lost setting. POSIX never enters this path.
            deadline = time.monotonic() + _REPLACE_TIMEOUT
            while True:
                try:
                    os.replace(tmp_path, SETTINGS_FILE)
                    break
                except PermissionError:
                    if time.monotonic() >= deadline:
                        raise
                    time.sleep(0.02)
        except BaseException:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise


@contextlib.contextmanager
def _guild_entry(guild_id: str):
    """Atomically read-modify-write one guild's settings block.

    The lock has to span the read AND the write. Every setter is
    load -> mutate -> save, so two setters for DIFFERENT guilds interleaving there
    would have the later save write back a stale snapshot and silently drop the
    earlier guild's change.
    """
    with _settings_lock:
        settings = load_settings()
        entry = settings.setdefault(guild_id, {})
        yield entry
        save_settings(settings)


def get_allowed_channel(guild_id: str) -> str | None:
    settings = load_settings()
    guild = settings.get(guild_id, {})
    return guild.get("allowed_channel")


def set_allowed_channel(guild_id: str, channel_id: str):
    with _guild_entry(guild_id) as guild:
        guild["allowed_channel"] = channel_id


def get_bitrate(guild_id: str) -> int | None:
    settings = load_settings()
    guild = settings.get(guild_id, {})
    return guild.get("bitrate")


def set_bitrate(guild_id: str, kbps: int):
    with _guild_entry(guild_id) as guild:
        guild["bitrate"] = kbps


def get_admins(guild_id: str) -> list[str]:
    settings = load_settings()
    guild = settings.get(guild_id, {})
    return guild.get("admins", [])


def add_admin(guild_id: str, user_id: str):
    with _guild_entry(guild_id) as guild:
        admins = guild.get("admins", [])
        if user_id not in admins:
            admins.append(user_id)
        guild["admins"] = admins


def remove_admin(guild_id: str, user_id: str):
    with _guild_entry(guild_id) as guild:
        admins = guild.get("admins", [])
        if user_id in admins:
            admins.remove(user_id)
        guild["admins"] = admins


# --- EQ persistence (Phase 07) -------------------------------------------

EQ_BASS_MIN = -10
EQ_BASS_MAX = 10
EQ_TREBLE_MIN = -10
EQ_TREBLE_MAX = 10

# Canonical preset table. Keys are lowercase preset names used by !eq <preset>.
# Values are (bass_db, treble_db) integer tuples. Per CONTEXT D-05.
EQ_PRESETS: dict[str, tuple[int, int]] = {
    "flat": (0, 0),
    "bass-boost": (5, 0),
    "treble-boost": (0, 5),
    "vocal": (-2, 3),
}


def _validate_eq_db(value: int, band: str) -> None:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(
            f"EQ {band} must be an integer between {EQ_BASS_MIN} and {EQ_BASS_MAX} dB"
        )
    if value < EQ_BASS_MIN or value > EQ_BASS_MAX:
        raise ValueError(
            f"EQ {band} must be between {EQ_BASS_MIN} and {EQ_BASS_MAX} dB (got {value})"
        )


def get_eq_bass(guild_id: str) -> int:
    """Return stored bass gain in dB for this guild (default 0 = flat)."""
    settings = load_settings()
    guild = settings.get(guild_id, {})
    return int(guild.get("eq_bass", 0))


def set_eq_bass(guild_id: str, db: int):
    """Persist bass gain in dB. Raises ValueError if outside -10..+10 integer range."""
    _validate_eq_db(db, "bass")
    with _guild_entry(guild_id) as guild:
        guild["eq_bass"] = db


def get_eq_treble(guild_id: str) -> int:
    """Return stored treble gain in dB for this guild (default 0 = flat)."""
    settings = load_settings()
    guild = settings.get(guild_id, {})
    return int(guild.get("eq_treble", 0))


def set_eq_treble(guild_id: str, db: int):
    """Persist treble gain in dB. Raises ValueError if outside -10..+10 integer range."""
    _validate_eq_db(db, "treble")
    with _guild_entry(guild_id) as guild:
        guild["eq_treble"] = db


def get_eq_preset_name(bass: int, treble: int) -> str:
    """Return the preset name whose (bass, treble) matches, or 'custom' if none match."""
    for name, (b, t) in EQ_PRESETS.items():
        if b == bass and t == treble:
            return name
    return "custom"


# --- Card display prefs: weather location + timezone (Phase: fun trackers) -----

DEFAULT_WEATHER_LOCATION = {"name": "Riga", "lat": 56.9496, "lon": 24.1052}
DEFAULT_TIMEZONE = "Europe/Riga"


def get_weather_location(guild_id: str) -> dict:
    """Return {'name', 'lat', 'lon'} for this guild's weather, defaulting to Riga."""
    guild = load_settings().get(guild_id, {})
    loc = guild.get("weather_location")
    return loc if loc else dict(DEFAULT_WEATHER_LOCATION)


def set_weather_location(guild_id: str, name: str, lat: float, lon: float):
    with _guild_entry(guild_id) as guild:
        guild["weather_location"] = {"name": name, "lat": lat, "lon": lon}


def get_timezone(guild_id: str) -> str:
    """Return this guild's IANA timezone for F1 race times (default Europe/Riga)."""
    guild = load_settings().get(guild_id, {})
    return guild.get("timezone") or DEFAULT_TIMEZONE


def set_timezone(guild_id: str, tz: str):
    with _guild_entry(guild_id) as guild:
        guild["timezone"] = tz


def get_display_prefs(guild_id: str) -> dict:
    """Weather location + timezone in one read (used by the card build)."""
    guild = load_settings().get(guild_id, {})
    return {
        "location": guild.get("weather_location") or dict(DEFAULT_WEATHER_LOCATION),
        "timezone": guild.get("timezone") or DEFAULT_TIMEZONE,
    }
