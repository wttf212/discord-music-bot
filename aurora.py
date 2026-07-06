"""Aurora VIEWING-WINDOW forecast for the player card.

"When can I expect to see it tonight" — combines three free, no-key sources:
  * NOAA SWPC Kp forecast (geomagnetic activity, 3-hour bins, 3 days),
  * hourly cloud cover + day/night from Open-Meteo (weather.get_hourly_sky), and
  * the location's geomagnetic latitude, to know what Kp is needed to see aurora.

get_kp_forecast() is blocking (call via an executor and cache); forecast_line()
is a cheap offline formatter that picks the best dark, clear, high-Kp hour.
"""
import json
import math
import urllib.request
from datetime import datetime, timedelta, timezone

_KP_URL = "https://services.swpc.noaa.gov/products/noaa-planetary-k-index-forecast.json"

# North geomagnetic pole (approx, IGRF ~2015): 80.65°N, 72.68°W. Used to convert
# geographic → geomagnetic latitude (aurora tracks the geomagnetic frame).
_POLE_LAT = math.radians(80.65)
_POLE_LON = math.radians(-72.68)


def get_kp_forecast():
    """Return [(datetime_utc, kp_float), ...] sorted, from NOAA's 3-day Kp forecast, or None."""
    try:
        req = urllib.request.Request(_KP_URL, headers={"User-Agent": "discord-music-bot"})
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read().decode())
        out = []
        for row in data:
            if isinstance(row, dict):
                tt, kp = row.get("time_tag"), row.get("kp")
            elif isinstance(row, (list, tuple)) and len(row) >= 2:
                tt, kp = row[0], row[1]
                if tt == "time_tag":  # header row on list-style feeds
                    continue
            else:
                continue
            if tt is None or kp is None:
                continue
            try:
                dt = datetime.fromisoformat(str(tt).replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                out.append((dt, float(kp)))
            except (ValueError, TypeError):
                continue
        out.sort(key=lambda x: x[0])
        return out or None
    except Exception:
        return None


def geomag_lat(lat: float, lon: float) -> float:
    """Approximate geomagnetic latitude (centered-dipole) for a geographic point."""
    lat_r, lon_r = math.radians(lat), math.radians(lon)
    s = (math.sin(lat_r) * math.sin(_POLE_LAT)
         + math.cos(lat_r) * math.cos(_POLE_LAT) * math.cos(lon_r - _POLE_LON))
    return math.degrees(math.asin(max(-1.0, min(1.0, s))))


def kp_needed(lat: float, lon: float) -> float:
    """Rough Kp required to see aurora at this location (auroral oval edge heuristic).

    Uses the magnitude of the geomagnetic latitude so it works in both hemispheres
    (aurora borealis in the north, aurora australis in the south)."""
    return max(0.0, (66.5 - abs(geomag_lat(lat, lon))) / 2.5)


def _kp_at(kp_list, dt):
    """Kp of the 3-hour bin covering dt, or None if no bin is within 3h (feed gap/stale)."""
    best_t = best_k = None
    for t, k in kp_list:
        if t <= dt and (best_t is None or t > best_t):
            best_t, best_k = t, k
    if best_t is None or (dt - best_t) > timedelta(hours=3):
        return None
    return best_k


def _hhmm(dt, tz_name):
    local = dt
    if tz_name:
        try:
            from zoneinfo import ZoneInfo
            local = dt.astimezone(ZoneInfo(tz_name))
        except Exception:
            local = dt
    return local.strftime("%H:%M")


def forecast_line(kp_list, hourly_sky, lat, lon, tz_name=None, now=None, hours=12) -> str:
    """Best aurora viewing window in the next `hours`, as a compact card string.

    Returns 'Aurora: best ~HH:MM (Kp N)' for a dark, clear-ish, high-enough-Kp hour;
    'Aurora: cloudy (Kp N)' when the activity is there but every dark hour is overcast;
    and '' (omit) when there's no darkness in the window or Kp never reaches this
    latitude's threshold — so the line only appears when it's actually worth caring.
    """
    if not kp_list or not hourly_sky:
        return ""
    now = now or datetime.now(timezone.utc)
    horizon = now + timedelta(hours=hours)
    needed = kp_needed(lat, lon)

    dark = []
    for t_iso, cloud, is_day in hourly_sky:
        try:
            t = datetime.fromisoformat(str(t_iso).replace("Z", "+00:00"))
            if t.tzinfo is None:
                t = t.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            continue
        if is_day or not (now <= t <= horizon):
            continue
        k = _kp_at(kp_list, t)
        if k is None:
            continue
        dark.append((t, cloud, k))

    if not dark:
        return ""  # no darkness in the window (summer white nights / daytime)
    if max(k for _, _, k in dark) < needed:
        return ""  # geomagnetic activity never reaches this latitude tonight

    viewable = [x for x in dark if x[2] >= needed]
    viewable.sort(key=lambda x: (x[1], x[0]))  # clearest first, then earliest
    t, cloud, k = viewable[0]
    kp_disp = int(round(k))
    if cloud >= 80:
        return f"Aurora: cloudy (Kp {kp_disp})"
    return f"Aurora: best ~{_hhmm(t, tz_name)} (Kp {kp_disp})"
