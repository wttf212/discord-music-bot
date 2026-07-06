"""Next Formula 1 race info for the player card.

Uses the Jolpica API (free, no key — the maintained successor to Ergast).
get_next_race() is blocking (call via an executor and cache the result);
format_race() is a cheap local formatter that renders the cached race in a given
timezone with a live countdown, so it can run on every card build.
"""
import json
import urllib.request
from datetime import datetime, timezone

_URL = "https://api.jolpi.ca/ergast/f1/current/next.json"


def get_next_race() -> dict | None:
    """Fetch the next race as {"name", "circuit", "dt"} (dt = ISO-8601 UTC string),
    or None if unavailable / off-season."""
    try:
        req = urllib.request.Request(_URL, headers={"User-Agent": "discord-music-bot"})
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read().decode())
        races = data.get("MRData", {}).get("RaceTable", {}).get("Races", [])
        if not races:
            return None
        rr = races[0]
        name = (rr.get("raceName") or "Grand Prix").replace("Grand Prix", "GP").strip()
        circuit = (rr.get("Circuit") or {}).get("circuitName", "")
        date = rr.get("date")
        tm = rr.get("time")  # e.g. "13:00:00Z"
        dt_iso = None
        if date:
            iso = date + ("T" + tm.replace("Z", "+00:00") if tm else "T00:00:00+00:00")
            try:
                dt = datetime.fromisoformat(iso)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                dt_iso = dt.isoformat()
            except ValueError:
                dt_iso = None
        return {"name": name, "circuit": circuit, "dt": dt_iso}
    except Exception:
        return None


def _countdown(delta_seconds: float) -> str:
    if delta_seconds < 0:
        return ""
    if delta_seconds < 3600:
        return "soon"
    if delta_seconds < 86400:
        return f"in {int(delta_seconds // 3600)}h"
    return f"in {int(delta_seconds // 86400)}d"


def format_race(race: dict | None, tz_name: str | None = None, now: datetime | None = None) -> str:
    """Render a cached race dict as a short string, e.g.
    'F1: Belgian GP Sun 19 Jul 16:00 (in 13d)'. Times shown in tz_name when
    resolvable, otherwise UTC (suffixed). Returns '' for no race."""
    if not race or not race.get("name"):
        return ""
    name = race["name"]
    dt_iso = race.get("dt")
    if not dt_iso:
        return f"F1: {name}"
    try:
        dt = datetime.fromisoformat(dt_iso)
    except ValueError:
        return f"F1: {name}"
    now = now or datetime.now(timezone.utc)

    localized = False
    local = dt
    if tz_name:
        try:
            from zoneinfo import ZoneInfo
            local = dt.astimezone(ZoneInfo(tz_name))
            localized = True
        except Exception:
            local = dt
    when = local.strftime("%a %d %b %H:%M") + ("" if localized else " UTC")

    cd = _countdown((dt - now).total_seconds())
    return f"F1: {name} {when}" + (f" ({cd})" if cd else "")
