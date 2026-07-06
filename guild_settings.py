import json
import os

SETTINGS_FILE = os.environ.get(
    "GUILD_SETTINGS_FILE",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "guild_settings.json")
)


def load_settings() -> dict:
    if not os.path.isfile(SETTINGS_FILE):
        return {}
    with open(SETTINGS_FILE, "r") as f:
        return json.load(f)


def save_settings(data: dict):
    with open(SETTINGS_FILE, "w") as f:
        json.dump(data, f, indent=2)


def get_allowed_channel(guild_id: str) -> str | None:
    settings = load_settings()
    guild = settings.get(guild_id, {})
    return guild.get("allowed_channel")


def set_allowed_channel(guild_id: str, channel_id: str):
    settings = load_settings()
    if guild_id not in settings:
        settings[guild_id] = {}
    settings[guild_id]["allowed_channel"] = channel_id
    save_settings(settings)


def get_bitrate(guild_id: str) -> int | None:
    settings = load_settings()
    guild = settings.get(guild_id, {})
    return guild.get("bitrate")


def set_bitrate(guild_id: str, kbps: int):
    settings = load_settings()
    if guild_id not in settings:
        settings[guild_id] = {}
    settings[guild_id]["bitrate"] = kbps
    save_settings(settings)


def get_admins(guild_id: str) -> list[str]:
    settings = load_settings()
    guild = settings.get(guild_id, {})
    return guild.get("admins", [])


def add_admin(guild_id: str, user_id: str):
    settings = load_settings()
    if guild_id not in settings:
        settings[guild_id] = {}
    admins = settings[guild_id].get("admins", [])
    if user_id not in admins:
        admins.append(user_id)
    settings[guild_id]["admins"] = admins
    save_settings(settings)


def remove_admin(guild_id: str, user_id: str):
    settings = load_settings()
    if guild_id not in settings:
        return
    admins = settings[guild_id].get("admins", [])
    if user_id in admins:
        admins.remove(user_id)
        settings[guild_id]["admins"] = admins
        save_settings(settings)


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
    settings = load_settings()
    if guild_id not in settings:
        settings[guild_id] = {}
    settings[guild_id]["eq_bass"] = db
    save_settings(settings)


def get_eq_treble(guild_id: str) -> int:
    """Return stored treble gain in dB for this guild (default 0 = flat)."""
    settings = load_settings()
    guild = settings.get(guild_id, {})
    return int(guild.get("eq_treble", 0))


def set_eq_treble(guild_id: str, db: int):
    """Persist treble gain in dB. Raises ValueError if outside -10..+10 integer range."""
    _validate_eq_db(db, "treble")
    settings = load_settings()
    if guild_id not in settings:
        settings[guild_id] = {}
    settings[guild_id]["eq_treble"] = db
    save_settings(settings)


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
    settings = load_settings()
    if guild_id not in settings:
        settings[guild_id] = {}
    settings[guild_id]["weather_location"] = {"name": name, "lat": lat, "lon": lon}
    save_settings(settings)


def get_timezone(guild_id: str) -> str:
    """Return this guild's IANA timezone for F1 race times (default Europe/Riga)."""
    guild = load_settings().get(guild_id, {})
    return guild.get("timezone") or DEFAULT_TIMEZONE


def set_timezone(guild_id: str, tz: str):
    settings = load_settings()
    if guild_id not in settings:
        settings[guild_id] = {}
    settings[guild_id]["timezone"] = tz
    save_settings(settings)


def get_display_prefs(guild_id: str) -> dict:
    """Weather location + timezone in one read (used by the card build)."""
    guild = load_settings().get(guild_id, {})
    return {
        "location": guild.get("weather_location") or dict(DEFAULT_WEATHER_LOCATION),
        "timezone": guild.get("timezone") or DEFAULT_TIMEZONE,
    }
