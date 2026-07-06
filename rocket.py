"""Next rocket launch for the player card.

Uses The Space Devs' Launch Library 2 (free, no key; anonymous requests are
throttled to ~15/hour, so cache with a long TTL). get_next_launch() is blocking
(call via an executor); format_launch() is a cheap offline formatter.
"""
import json
import urllib.request
from datetime import datetime, timezone

_URL = ("https://ll.thespacedevs.com/2.2.0/launch/upcoming/"
        "?limit=1&hide_recent_previous=true&mode=list")


def _countdown(delta_seconds: float) -> str:
    if delta_seconds < 0:
        return ""
    if delta_seconds < 3600:
        return "soon"
    if delta_seconds < 86400:
        return f"in {int(delta_seconds // 3600)}h"
    return f"in {int(delta_seconds // 86400)}d"


def get_next_launch() -> dict | None:
    """Fetch the next launch as {"name", "dt"} (dt = ISO-8601 UTC), or None."""
    try:
        req = urllib.request.Request(_URL, headers={"User-Agent": "discord-music-bot"})
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read().decode())
        results = data.get("results") or []
        if not results:
            return None
        launch = results[0]
        name = (launch.get("name") or "Launch").replace(" | ", " – ").strip()
        if len(name) > 48:
            name = name[:47].rstrip() + "…"
        net = launch.get("net")
        dt_iso = None
        if net:
            try:
                dt = datetime.fromisoformat(net.replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                dt_iso = dt.isoformat()
            except ValueError:
                dt_iso = None
        return {"name": name, "dt": dt_iso}
    except Exception:
        return None


def format_launch(launch: dict | None, tz_name: str | None = None, now: datetime | None = None) -> str:
    """Render a cached launch as 'Launch: <name> (in 2d)', '' if none."""
    if not launch or not launch.get("name"):
        return ""
    name = launch["name"]
    dt_iso = launch.get("dt")
    if not dt_iso:
        return f"Launch: {name}"
    try:
        dt = datetime.fromisoformat(dt_iso)
    except ValueError:
        return f"Launch: {name}"
    now = now or datetime.now(timezone.utc)
    cd = _countdown((dt - now).total_seconds())
    return f"Launch: {name}" + (f" ({cd})" if cd else "")
