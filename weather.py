"""Current weather for the player card, for any location.

Uses Open-Meteo (free, no API key) for current conditions and its Geocoding API
to turn a city name into coordinates. Blocking; call via an executor and cache the
result (see the cache in commands.py). Returns short strings like
"Riga 3°C, light rain", or None on failure (so the card just omits it).
"""
import json
import urllib.parse
import urllib.request

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


def get_weather(lat: float, lon: float, label: str = "Weather") -> str | None:
    url = (
        f"https://api.open-meteo.com/v1/forecast?latitude={lat}"
        f"&longitude={lon}&current=temperature_2m,weather_code"
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
        return f"{label} {temp_str}, {desc}" if desc else f"{label} {temp_str}"
    except Exception:
        return None


def get_hourly_sky(lat: float, lon: float):
    """Return [(iso_utc, cloud_cover%, is_day 0/1), ...] for the next ~2 days,
    or None. Used by the aurora viewing-window forecast (needs darkness + clouds)."""
    url = (
        f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}"
        f"&hourly=cloud_cover,is_day&forecast_days=2&timezone=UTC"
    )
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "discord-music-bot"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
        h = data.get("hourly", {}) or {}
        times = h.get("time") or []
        clouds = h.get("cloud_cover") or []
        days = h.get("is_day") or []
        out = [(t, c, d) for t, c, d in zip(times, clouds, days)]
        return out or None
    except Exception:
        return None


def geocode(name: str):
    """Resolve a place name to (display_name, lat, lon), or None if not found."""
    url = "https://geocoding-api.open-meteo.com/v1/search?" + urllib.parse.urlencode(
        {"name": name, "count": 1}
    )
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "discord-music-bot"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
        results = data.get("results") or []
        if not results:
            return None
        top = results[0]
        place = top.get("name", "").strip()
        country = top.get("country_code") or top.get("country") or ""
        display = f"{place}, {country}" if country else place
        return (display or name, top["latitude"], top["longitude"])
    except Exception:
        return None
