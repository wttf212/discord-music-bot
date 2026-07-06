"""Aurora (northern lights) probability for a location, for the player card.

Uses NOAA SWPC's OVATION model (free, no key), which publishes a global grid of
aurora probability by lon/lat. get_aurora_grid() fetches the grid (blocking; call
via an executor and cache it); aurora_at() looks up the probability for a
location — so the reading is dynamic to the guild's configured coordinates.
"""
import json
import urllib.request

_URL = "https://services.swpc.noaa.gov/json/ovation_aurora_latest.json"


def get_aurora_grid() -> dict | None:
    """Fetch OVATION as a {(lon_int, lat_int): probability%} lookup, or None."""
    try:
        req = urllib.request.Request(_URL, headers={"User-Agent": "discord-music-bot"})
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read().decode())
        coords = data.get("coordinates") or []
        if not coords:
            return None
        # Each entry is [longitude(0-359), latitude(-90..90), aurora%]
        return {(int(c[0]), int(c[1])): c[2] for c in coords}
    except Exception:
        return None


def aurora_at(grid: dict | None, lat: float, lon: float):
    """Probability (int %) at the nearest grid point to (lat, lon), or None."""
    if not grid:
        return None
    try:
        return grid.get((int(round(lon)) % 360, int(round(lat))))
    except Exception:
        return None


def format_aurora(pct) -> str:
    """Render an aurora probability as 'Aurora 23%', or '' when unavailable."""
    if pct is None:
        return ""
    return f"Aurora {int(pct)}%"
