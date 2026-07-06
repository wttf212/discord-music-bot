"""Spotify link support.

Spotify audio is DRM-protected and cannot be streamed directly, so a Spotify
link is expanded into "artist - title" search strings that are then resolved to
YouTube via the bot's normal plain-text search path.

Track links work with no configuration (via Spotify's public oEmbed endpoint).
Playlist/album links need Spotify API credentials (client-credentials flow):
add `spotify.client_id` / `spotify.client_secret` to config.yaml. Uses only the
standard library — no new dependencies. All calls are blocking; run in an executor.
"""
import base64
import json
import time
import urllib.error
import urllib.parse
import urllib.request

MAX_TRACKS = 200  # mirror MAX_PLAYLIST_TRACKS


class SpotifyError(Exception):
    """User-facing Spotify resolution error (message is safe to show in chat)."""


def is_spotify_url(url: str) -> bool:
    if not url:
        return False
    u = url.lower()
    return "open.spotify.com/" in u or u.startswith("spotify:")


def _parse(url: str):
    """Return (kind, id) for track/playlist/album links, else (None, None).
    Handles open.spotify.com/[intl-xx/]<kind>/<id> and spotify:<kind>:<id> URIs."""
    if url.startswith("spotify:"):
        parts = url.split(":")
        if len(parts) >= 3 and parts[1] in ("track", "playlist", "album"):
            return parts[1], parts[2]
        return None, None
    try:
        parsed = urllib.parse.urlparse(url)
    except Exception:
        return None, None
    segs = [s for s in parsed.path.split("/") if s]
    for i, s in enumerate(segs):
        if s in ("track", "playlist", "album") and i + 1 < len(segs):
            return s, segs[i + 1]
    return None, None


_token_cache = {"token": None, "exp": 0.0}


def _get_token(client_id: str, client_secret: str) -> str:
    now = time.time()
    if _token_cache["token"] and _token_cache["exp"] - 30 > now:
        return _token_cache["token"]
    auth = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    data = urllib.parse.urlencode({"grant_type": "client_credentials"}).encode()
    req = urllib.request.Request(
        "https://accounts.spotify.com/api/token", data=data,
        headers={"Authorization": f"Basic {auth}",
                 "Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            j = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        raise SpotifyError("Spotify auth failed — check spotify.client_id / client_secret.") from e
    tok = j.get("access_token")
    if not tok:
        raise SpotifyError("Spotify auth failed — no access token returned.")
    _token_cache["token"] = tok
    _token_cache["exp"] = now + int(j.get("expires_in", 3600))
    return tok


def _api(url: str, token: str) -> dict:
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode())


def _fmt(name: str, artists: list) -> str:
    joined = ", ".join(a for a in artists if a)
    return f"{joined} - {name}" if joined else name


def _artist_names(obj: dict) -> list:
    return [a.get("name", "") for a in (obj.get("artists") or [])]


def _oembed_title(url: str) -> str | None:
    o = "https://open.spotify.com/oembed?" + urllib.parse.urlencode({"url": url})
    try:
        with urllib.request.urlopen(o, timeout=15) as resp:
            return json.loads(resp.read().decode()).get("title")
    except Exception:
        return None


def resolve_spotify(url: str, client_id: str | None = None,
                    client_secret: str | None = None, limit: int = MAX_TRACKS) -> dict:
    """Expand a Spotify link into search-query tracks.

    Returns {"kind": ..., "title": <collection or track title>,
             "tracks": [{"query": "artist - title", "title": "artist - title"}, ...]}.
    Raises SpotifyError with a chat-safe message on failure.
    """
    kind, sid = _parse(url)
    if not kind or not sid:
        raise SpotifyError("Unrecognised Spotify link.")
    have_creds = bool(client_id and client_secret)

    try:
        if kind == "track":
            if have_creds:
                tok = _get_token(client_id, client_secret)
                t = _api(f"https://api.spotify.com/v1/tracks/{sid}", tok)
                q = _fmt(t.get("name", ""), _artist_names(t))
            else:
                q = _oembed_title(url)
                if not q:
                    raise SpotifyError("Couldn't read that Spotify track.")
            return {"kind": "track", "title": q, "tracks": [{"query": q, "title": q}]}

        # playlist / album need API credentials
        if not have_creds:
            raise SpotifyError(
                "Spotify playlists and albums need API credentials. Add "
                "`spotify.client_id` and `spotify.client_secret` to config.yaml "
                "(single track links work without them)."
            )
        tok = _get_token(client_id, client_secret)
        tracks: list[dict] = []

        if kind == "playlist":
            meta = _api(f"https://api.spotify.com/v1/playlists/{sid}?fields=name", tok)
            coll_title = meta.get("name", "Spotify Playlist")
            page = (f"https://api.spotify.com/v1/playlists/{sid}/tracks"
                    f"?limit=100&fields=items(track(name,artists(name))),next")
            while page and len(tracks) < limit:
                j = _api(page, tok)
                for it in j.get("items", []):
                    tr = (it or {}).get("track") or {}
                    if not tr.get("name"):
                        continue
                    q = _fmt(tr["name"], _artist_names(tr))
                    tracks.append({"query": q, "title": q})
                    if len(tracks) >= limit:
                        break
                page = j.get("next")
        else:  # album
            meta = _api(f"https://api.spotify.com/v1/albums/{sid}", tok)
            coll_title = meta.get("name", "Spotify Album")
            block = meta.get("tracks", {}) or {}
            while block and len(tracks) < limit:
                for tr in block.get("items", []):
                    if not tr or not tr.get("name"):
                        continue
                    q = _fmt(tr["name"], _artist_names(tr))
                    tracks.append({"query": q, "title": q})
                    if len(tracks) >= limit:
                        break
                nxt = block.get("next")
                block = _api(nxt, tok) if (nxt and len(tracks) < limit) else None

        if not tracks:
            raise SpotifyError("No playable tracks found on that Spotify link.")
        return {"kind": kind, "title": coll_title, "tracks": tracks}

    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise SpotifyError("That Spotify link wasn't found (private or deleted?).") from e
        raise SpotifyError(f"Spotify request failed (HTTP {e.code}).") from e
    except urllib.error.URLError as e:
        raise SpotifyError("Couldn't reach Spotify.") from e
