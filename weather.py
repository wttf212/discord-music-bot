"""Current weather for Riga, Latvia — shown as a small footer line on the card.

Uses Open-Meteo (free, no API key). Blocking; call via an executor and cache the
result (see the cache in commands.py). Returns a short string like "Riga 3°C,
light rain" or None on any failure (so the footer just omits it).
"""
import json
import urllib.request

_RIGA_LAT = 56.9496
_RIGA_LON = 24.1052

# WMO weather interpretation codes → short human description.
_WMO = {
    0: "clear sky", 1: "mainly clear", 2: "partly cloudy", 3: "overcast",
    45: "fog", 48: "rime fog",
    51: "light drizzle", 53: "drizzle", 55: "heavy drizzle",
    56: "freezing drizzle", 57: "freezing drizzle",
    61: "light rain", 63: "rain", 65: "heavy rain",
    66: "freezing rain", 67: "freezing rain",
    71: "light snow", 73: "snow", 75: "heavy snow", 77: "snow grains",
    80: "light showers", 81: "showers", 82: "heavy showers",
    85: "snow showers", 86: "snow showers",
    95: "thunderstorm", 96: "thunderstorm with hail", 99: "thunderstorm with hail",
}


def get_riga_weather() -> str | None:
    url = (
        f"https://api.open-meteo.com/v1/forecast?latitude={_RIGA_LAT}"
        f"&longitude={_RIGA_LON}&current=temperature_2m,weather_code"
    )
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "discord-music-bot"})
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read().decode())
        cur = data.get("current", {}) or {}
        temp = cur.get("temperature_2m")
        if temp is None:
            return None
        code = cur.get("weather_code")
        desc = _WMO.get(int(code), "") if code is not None else ""
        temp_str = f"{round(temp)}°C"
        return f"Riga {temp_str}, {desc}" if desc else f"Riga {temp_str}"
    except Exception:
        return None
