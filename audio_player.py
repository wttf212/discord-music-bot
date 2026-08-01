import asyncio
import contextlib
import queue
import random
import shutil
import socket
import subprocess
import sys
import os
import threading
import time
from collections import OrderedDict
from urllib.error import URLError
from urllib.parse import urlparse, parse_qs

from concurrent.futures import ThreadPoolExecutor

import discord
from yt_dlp.utils import DownloadError

# Audio work runs on dedicated pools, NOT asyncio's default executor.
#
# The default pool is min(32, cpu+4) — six workers on a 2-vCPU VPS. Starting one track
# can hold two of them for seconds (a resolve, then the CDN settle wait or warm-up),
# so at 10-50 guilds peak transitions would queue behind each other and present as
# exactly the latency this codebase spent a lot of effort removing. Worse, the default
# pool is shared with everything else asyncio hands off, so audio waits would starve
# unrelated work and vice versa.
#
# Two pools, because the two kinds of work want opposite sizing:
#   RESOLVE is CPU-bound — a yt-dlp resolve spends ~2s running the player JS challenge
#     in Deno. Oversubscribing that thrashes a small box, so it scales with cores.
#   STREAM is pure waiting — settle backoffs, 1-byte warm probes, FFmpeg spawn,
#     subprocess reaping. Threads here are blocked, not busy, so the pool can be wide
#     and must not be throttled by core count.
_CPU_COUNT = os.cpu_count() or 2
_RESOLVE_EXECUTOR = ThreadPoolExecutor(
    max_workers=max(4, min(8, _CPU_COUNT)), thread_name_prefix="yt-resolve")
_STREAM_EXECUTOR = ThreadPoolExecutor(
    max_workers=32, thread_name_prefix="audio-stream")

# Set on shutdown. Worker threads park in settle backoffs and retry sleeps for several
# seconds at a time, and concurrent.futures joins its (non-daemon) workers at
# interpreter exit — so without a way to interrupt those sleeps, Ctrl+C hangs for as
# long as the longest one. Every sleep in this module waits on this event instead of
# time.sleep, which turns shutdown into an immediate return.
_shutdown = threading.Event()


def shutdown_audio() -> None:
    """Wake every parked audio worker and stop accepting new work."""
    _shutdown.set()
    _RESOLVE_EXECUTOR.shutdown(wait=False, cancel_futures=True)
    _STREAM_EXECUTOR.shutdown(wait=False, cancel_futures=True)

# Load bgutil PO token provider plugin for yt-dlp
_base_dir = os.path.dirname(os.path.abspath(__file__))
_plugin_dir = os.path.join(_base_dir, "yt-dlp-plugins", "bgutil-ytdlp-pot-provider")
if os.path.isdir(_plugin_dir) and _plugin_dir not in sys.path:
    sys.path.insert(0, _plugin_dir)

from yt_dlp import YoutubeDL

# TLS impersonation availability check (requires curl_cffi via yt-dlp[default,curl-cffi])
# ImpersonateTarget is a pure-Python dataclass; the import always succeeds if yt-dlp is installed.
# The `import curl_cffi` line verifies the actual network backend is present, not just the dataclass.
try:
    from yt_dlp.networking.impersonate import ImpersonateTarget
    import curl_cffi  # noqa: F401 — verify the backend is present, not just the dataclass
    _IMPERSONATE_AVAILABLE = True
except ImportError:
    _IMPERSONATE_AVAILABLE = False


def _build_ffmpeg_af_options(bass_db: int, treble_db: int) -> str:
    """Return the bare FFmpeg filter chain string for the given EQ,
    or an empty string when both bands are 0 dB (flat — no filter needed).

    The caller is responsible for prepending '-af ' before passing to FFmpegPCMAudio.
    No shell quoting — FFmpegPCMAudio uses shlex.split internally, so embedded
    quotes would become literal characters and corrupt the filter graph.

    Examples:
        (0, 0)   -> ""
        (5, 0)   -> "bass=g=5,alimiter=level_out=0.9:attack=5:release=50"
        (0, -3)  -> "treble=g=-3,alimiter=level_out=0.9:attack=5:release=50"
        (5, -2)  -> "bass=g=5,treble=g=-2,alimiter=level_out=0.9:attack=5:release=50"
    """
    parts: list[str] = []
    if bass_db != 0:
        parts.append(f"bass=g={bass_db}")
    if treble_db != 0:
        parts.append(f"treble=g={treble_db}")
    if not parts:
        return ""
    # alimiter prevents clipping when EQ boosts push peaks above 0 dBFS
    parts.append("alimiter=level_out=0.9:attack=5:release=50")
    return ",".join(parts)


def _bgutil_executable() -> str | None:
    """Path to the bgutil-pot binary at the project root, or None if absent."""
    return next(
        (p for p in (
            os.path.join(_base_dir, "bgutil-pot.exe"),
            os.path.join(_base_dir, "bgutil-pot"),
        ) if os.path.isfile(p)),
        None,
    )


def _youtube_extractor_args(client: str, bgutil_exe: str | None,
                            force_cli: bool = False) -> dict:
    """Build yt-dlp's YouTube extractor args, including PO token provider config.

    The provider registry prefers the bgutil HTTP server (preference 130, started by
    main.py on 127.0.0.1:4416) and falls back to the CLI (preference 1) on its own when
    the server is unreachable. Both mint the same token; the server is just warm, so it
    answers in ~0.4s where spawning the 45 MB binary per video costs seconds.

    ``force_cli`` redirects the HTTP provider to a dead port so the registry has no
    choice but the CLI. Used ONLY for the one-shot retry after a degraded (non
    audio-only) resolve, which is the signature of broken HTTP-provider attestation
    (YouTube changing ytAtR) — the CLI has its own Rust PPA implementation and needs no
    webpage attestation. Do not set it on the normal path: the dead-port probe does not
    fail fast, it costs ~2s per resolve.
    """
    args = {
        # client can be comma-separated, e.g. "web,android_vr"
        # mweb needs a PLAYER PO token to pass YouTube's bot-check gate (LOGIN_REQUIRED)
        # and a GVS PO token to unlock stream URLs; "always" makes yt-dlp fetch them
        # proactively (harmless no-op for token-less clients like android_vr).
        "youtube": {
            "player_client": [c.strip() for c in client.split(",")],
            "fetch_pot": ["always"],
        },
    }
    if bgutil_exe:
        # bgutil-pot is at _base_dir, NOT in yt-dlp's default search paths — the CLI
        # provider only registers when handed an explicit path.
        args["youtubepot-bgutilcli"] = {"cli_path": [bgutil_exe]}
        if force_cli:
            args["youtubepot-bgutilhttp"] = {"base_url": ["http://127.0.0.1:1"]}
    return args


def _find_ffmpeg(config_path: str) -> str:
    """Resolve ffmpeg binary: config path > PATH > imageio_ffmpeg fallback."""
    if config_path and config_path != "ffmpeg":
        return config_path
    found = shutil.which("ffmpeg")
    if found:
        return found
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except ImportError:
        return "ffmpeg"


def _is_youtube(query: str) -> bool:
    return any(h in query for h in ("youtube.com", "youtu.be", "music.youtube.com"))


_NON_RETRYABLE_SUBSTRINGS = (
    "video unavailable",
    "sign in to confirm",
    "confirm your age",
    "age-restricted",
    "age restricted",
    "private video",
    "this video has been removed",
    "this video is not available",
    "not available in your country",
    "geo restricted",
    "geo-blocked",
    "members-only",
    "members only",
    "requires payment",
    "copyright",
    "this live event",
    # HTTP 4xx permanent errors — retrying these accelerates IP bans
    "http error 403",
    "forbidden",
    "http error 401",
    "http error 404",
)

_RETRYABLE_SUBSTRINGS = (
    "http error 429",
    "too many requests",
    "http error 5",
    "connection reset",
    "connection aborted",
    "connection refused",
    "remote end closed",
    "timed out",
    "read timed out",
    "temporary failure in name resolution",
)

# Connection/timeout-class errors that are clearly transient (NOT rate-limits).
# These get the fast backoff tier: a dropped connection or read timeout does not
# mean YouTube is throttling us, so a 5 s sleep is pure dead air. Rate-limit
# errors (429 / "too many requests"), 5xx server errors, and any unrecognised
# (ambiguous) error deliberately stay on the slow tier — an unknown error could
# be a soft-block, and pacing those conservatively protects against IP bans.
_FAST_RETRY_SUBSTRINGS = (
    "connection reset",
    "connection aborted",
    "connection refused",
    "remote end closed",
    "timed out",
    "read timed out",
    "temporary failure in name resolution",
)


def _retry_base_delay(exc: BaseException, base_delay: float, fast_delay: float) -> float:
    """Pick the backoff base for this error: fast for transient connection/timeout
    errors, slow (rate-limit-safe) for 429/5xx/ambiguous."""
    if isinstance(exc, (ConnectionError, TimeoutError, socket.timeout, URLError)):
        return fast_delay
    msg = str(exc).lower()
    if any(s in msg for s in _FAST_RETRY_SUBSTRINGS):
        return fast_delay
    return base_delay


def _is_retryable_ytdlp_error(exc: BaseException) -> bool:
    """Classify a yt-dlp (or connection-layer) exception as retryable or not.

    Returns False for permanent policy errors that retrying would accelerate
    IP bans on (video unavailable, sign-in required, age-restricted, geo-blocked).
    Returns True for transient network/rate-limit errors and all ambiguous cases
    (conservative: prefer retry over silent drop for unrecognised errors).
    """
    # Type-based retry: connection/timeout exceptions are always transient
    if isinstance(exc, (ConnectionError, TimeoutError, socket.timeout, URLError)):
        return True

    # String-based classification (case-insensitive substring match)
    msg = str(exc).lower()
    if any(s in msg for s in _NON_RETRYABLE_SUBSTRINGS):
        return False
    if any(s in msg for s in _RETRYABLE_SUBSTRINGS):
        return True
    # Ambiguous → default to retryable (per security brief)
    return True


def is_permanent_resolve_error(exc: BaseException) -> bool:
    """True when re-resolving this track can never succeed — removed by the uploader,
    private, age-gated, geo-blocked, members-only.

    Lets the prefetch drop such a track from the queue instead of leaving it to fail at
    playback, where it costs a failed play plus a cold start for whatever follows.
    """
    return not _is_retryable_ytdlp_error(exc)


def _retry_with_backoff(fn, *args, max_attempts: int = 3, base_delay: float = 5.0,
                        fast_delay: float = 1.5, jitter: float = 0.25, **kwargs):
    """Retry a synchronous callable with exponential backoff + jitter.

    Runs inside the executor thread — uses time.sleep (NOT asyncio.sleep).
    Re-raises immediately on non-retryable errors to avoid accelerating IP bans
    when YouTube returns a permanent policy error (video unavailable, sign-in,
    age-restricted, geo-block).

    The backoff base is tiered per error: transient connection/timeout errors use
    ``fast_delay`` (a dropped socket is not a rate-limit, so a 5 s sleep is wasted
    dead air), while 429/5xx/ambiguous errors keep the conservative ``base_delay``.
    """
    last_exc = None
    for attempt in range(max_attempts):
        try:
            return fn(*args, **kwargs)
        except Exception as exc:
            last_exc = exc
            if not _is_retryable_ytdlp_error(exc):
                # Non-retryable: surface on attempt 1 with no sleep.
                raise
            if attempt == max_attempts - 1:
                # Final attempt exhausted.
                raise
            if _shutdown.is_set():
                raise   # shutting down — don't start another backoff
            exp_delay = _retry_base_delay(exc, base_delay, fast_delay) * (2 ** attempt)
            jitter_mult = random.uniform(1.0 - jitter, 1.0 + jitter)
            sleep_seconds = exp_delay * jitter_mult
            exc_class = type(exc).__name__
            msg_preview = str(exc)[:80].replace("\n", " ")
            print(
                f"[retry] attempt {attempt + 1}/{max_attempts} failed: "
                f"{exc_class}: {msg_preview} — sleeping {sleep_seconds:.1f}s"
            )
            # Deliberately time.sleep, not _shutdown.wait: the retry-tier tests patch
            # time.sleep to assert the backoff values, and that coverage is worth more
            # than interrupting a sleep that only happens when a resolve is already
            # failing. The check above stops FURTHER retries once shutdown starts, and
            # the sleeps that actually run on every track start (settle, warm-up) are
            # interruptible.
            time.sleep(sleep_seconds)
    # Unreachable (loop always either returns or raises), but keep the invariant explicit.
    raise last_exc  # type: ignore[misc]


# A resolved CDN URL may be reused (prefetch / previous-track) only while it is
# comfortably in the future. googlevideo URLs carry an `expire=<unix>` query param
# (~6 h out); we require this much headroom left before trusting a cached URL so a
# transition never hands FFmpeg a URL about to 403. Non-YouTube URLs (e.g.
# SoundCloud) usually lack `expire=`, so a conservative wall-clock TTL is used.
STREAM_URL_SAFETY_MARGIN = 30 * 60      # need >= 30 min left on the CDN URL
STREAM_INFO_FALLBACK_TTL = 4 * 60 * 60  # trust an expiry-less URL for 4 h


def _stream_url_expiry(url: str) -> float | None:
    """Return the unix expiry embedded in a googlevideo CDN URL, or None if absent."""
    if not url:
        return None
    try:
        exp = parse_qs(urlparse(url).query).get("expire", [None])[0]
        return float(exp) if exp else None
    except (ValueError, TypeError):
        return None


def is_stream_info_fresh(info: dict | None, resolved_at: float = 0.0,
                         now: float | None = None) -> bool:
    """True if a cached get_audio_url() result can still be streamed safely.

    Primary guard is the CDN URL's own `expire=` timestamp (with a safety margin);
    when the URL has no expiry param, fall back to a conservative wall-clock TTL
    measured from resolved_at. Returns False for missing/empty info.
    """
    if not info or not info.get("url"):
        return False
    now = time.time() if now is None else now
    expiry = _stream_url_expiry(info["url"])
    if expiry is not None:
        return (expiry - now) > STREAM_URL_SAFETY_MARGIN
    if resolved_at:
        return (now - resolved_at) < STREAM_INFO_FALLBACK_TTL
    return False


# Resolved CDN URLs stay valid until their `expire=` (~6h out), so replaying a video
# within that window can skip the ~3s resolve entirely. This strictly REDUCES calls to
# YouTube (one fewer /player request per repeat), and a cached URL is already past the
# ~2-3s window during which googlevideo 403s a freshly minted URL, so playback starts
# immediately. Bounded LRU — an always-on bot must not grow this without limit.
_RESOLVE_CACHE_MAX = 256
_resolve_cache: "OrderedDict[str, tuple[dict, float]]" = OrderedDict()
_resolve_cache_lock = threading.Lock()


def _copy_stream_info(info: dict) -> dict:
    """Copy a resolve result so a caller mutating a cache hit cannot corrupt the cached
    entry (or another guild's copy). Every value is a scalar, so shallow is enough."""
    return dict(info)


def _resolve_cache_get(query: str) -> dict | None:
    """Return a still-fresh cached resolve for this YouTube URL, or None.

    Misses by construction for text queries and non-YouTube URLs — _youtube_video_id
    only matches youtube.com/youtu.be links.
    """
    video_id = _youtube_video_id(query)
    if not video_id:
        return None
    with _resolve_cache_lock:
        entry = _resolve_cache.get(video_id)
        if entry is None:
            return None
        info, resolved_at = entry
        if not is_stream_info_fresh(info, resolved_at):
            _resolve_cache.pop(video_id, None)  # expired — drop it
            return None
        _resolve_cache.move_to_end(video_id)
        return _copy_stream_info(info)


def _resolve_cache_put(video_id: str | None, info: dict) -> None:
    """Cache a successful resolve. Degraded (combined video+audio) results are NOT
    cached — pinning a format-18 fallback for hours would outlast the breakage."""
    if not video_id or not info.get("is_audio_only") or not info.get("url"):
        return
    with _resolve_cache_lock:
        _resolve_cache[video_id] = (_copy_stream_info(info), time.time())
        _resolve_cache.move_to_end(video_id)
        while len(_resolve_cache) > _RESOLVE_CACHE_MAX:
            _resolve_cache.popitem(last=False)


def invalidate_resolve_cache(query: str) -> None:
    """Drop the cached resolve for this URL — call when its CDN URL turned out dead so
    the retry actually re-resolves instead of getting the same dead URL back."""
    video_id = _youtube_video_id(query)
    if not video_id:
        return
    with _resolve_cache_lock:
        _resolve_cache.pop(video_id, None)


# Building a YoutubeDL is cheap; WARMING one is not. A fresh instance per resolve
# throws away the player-JS/solver state yt-dlp builds on first use. Interleaved A/B
# over 8 videos, order flipped per video: fresh 2.54s vs warm 2.09s, faster on 8/8,
# paired mean -0.443s, t=-7.17 (df=7, p<0.001). A reused instance's FIRST call costs
# the same as a fresh one; every later call is the cheap one.
#
# extract_info() is not documented as thread-safe and resolves run concurrently across
# guilds, so an instance is never shared while busy: try-lock, and on contention build
# a throwaway (exactly the old behaviour). Nothing ever blocks, so no guild can queue
# behind another guild's ~2s resolve.
_RESOLVER_MAX = 4
_resolvers: dict[tuple, tuple] = {}
_resolvers_guard = threading.Lock()


@contextlib.contextmanager
def _resolver(key: tuple, ydl_opts: dict):
    """Yield a warm YoutubeDL for this option set, or a throwaway if one is busy."""
    with _resolvers_guard:
        entry = _resolvers.get(key)
        if entry is None and len(_resolvers) < _RESOLVER_MAX:
            entry = (YoutubeDL(ydl_opts), threading.Lock())
            _resolvers[key] = entry

    if entry is not None and entry[1].acquire(blocking=False):
        try:
            yield entry[0]      # warm, and exclusively ours for the duration
        finally:
            entry[1].release()
    else:
        with YoutubeDL(ydl_opts) as ydl:   # busy or no slot — cold, and closed after
            yield ydl


def get_audio_url_with_retry(query: str, client: str, debug: bool = False, cookies_file: str | None = None) -> dict:
    """Retrying wrapper around get_audio_url (RETRY-01).

    Retries on HTTP 429, 5xx, and connection-layer errors with exponential backoff
    (3 attempts, base 5s, ±25% jitter). Surfaces video-unavailable / sign-in /
    age-restricted / geo-blocked errors immediately — retrying those accelerates
    IP bans.
    """
    return _retry_with_backoff(
        get_audio_url, query, client, debug, cookies_file,
        max_attempts=3, base_delay=5.0, jitter=0.25,
    )


# Two callers can want the same video at once: a skip fires play()'s resolve while the
# background prefetch for that very track is still in flight. Both would issue their own
# /player call — double the API traffic in exactly the skip-heavy pattern where rate
# limiting matters most — and the loser would also get a cold throwaway resolver. So the
# second caller waits for the first and reads its result out of the resolve cache.
_INFLIGHT_WAIT = 20.0   # seconds; longer than a resolve+retries, then give up and self-serve
_inflight_resolves: dict[str, "_InflightResolve"] = {}
_inflight_lock = threading.Lock()


class _InflightResolve:
    """A resolve in progress. The leader publishes its result here directly rather than
    via the resolve cache — results that are deliberately not cached (a degraded
    format-18 fallback) would otherwise still cost every waiter a duplicate call."""

    __slots__ = ("event", "result")

    def __init__(self):
        self.event = threading.Event()
        self.result: dict | None = None


def _claim_resolve(video_id: str) -> "_InflightResolve | None":
    """Claim this video's resolve, or return the in-flight entry to wait on."""
    with _inflight_lock:
        existing = _inflight_resolves.get(video_id)
        if existing is not None:
            return existing
        _inflight_resolves[video_id] = _InflightResolve()
        return None


def _release_resolve(video_id: str, result: dict | None = None) -> None:
    """Publish the outcome (result, or None on failure) and wake anyone waiting."""
    with _inflight_lock:
        entry = _inflight_resolves.pop(video_id, None)
    if entry is not None:
        entry.result = result
        entry.event.set()


def get_audio_url(query: str, client: str, debug: bool = False, cookies_file: str | None = None,
                  *, force_cli: bool = False) -> dict:
    """Resolve a track to a streamable CDN URL + metadata.

    Wraps the actual extraction with the two things that keep repeat and concurrent
    plays cheap: the video-id resolve cache, and in-flight de-duplication so two
    callers racing for the same video make one API call between them.

    ``force_cli`` forces the bgutil CLI PO token provider and bypasses both. It is set
    only by the internal one-shot retry after a degraded resolve.
    """
    if force_cli:
        return _resolve_audio_url(query, client, debug, cookies_file, force_cli=True)

    # A previously resolved CDN URL is good until its `expire=`, so replaying the same
    # video skips the whole ~3s resolve (and one /player call). Direct YouTube URLs only
    # — a text query has no video id to key on until it has been resolved once.
    cached = _resolve_cache_get(query)
    if cached is not None:
        if debug:
            print(f"[debug][yt-dlp] Resolve cache hit: {cached['title']!r}")
        return cached

    video_id = _youtube_video_id(query)
    if not video_id:
        return _resolve_audio_url(query, client, debug, cookies_file)

    inflight = _claim_resolve(video_id)
    if inflight is not None:
        # Someone else is already resolving this exact video — wait for them rather
        # than issuing a second /player call for it.
        if debug:
            print(f"[debug][yt-dlp] Resolve already in flight for {video_id} — waiting")
        inflight.event.wait(timeout=_INFLIGHT_WAIT)
        if inflight.result is not None:
            return _copy_stream_info(inflight.result)
        # Leader failed or timed out — do it ourselves rather than inherit its failure.
        return _resolve_audio_url(query, client, debug, cookies_file)

    result = None
    try:
        result = _resolve_audio_url(query, client, debug, cookies_file)
        return result
    finally:
        _release_resolve(video_id, result)


def _resolve_audio_url(query: str, client: str, debug: bool = False,
                       cookies_file: str | None = None, *, force_cli: bool = False) -> dict:
    """Extract audio URL and title via yt-dlp. Supports YouTube, SoundCloud, and others."""
    original_query = query
    ffmpeg_exe = _find_ffmpeg("ffmpeg")
    ydl_opts = {
        "format": "bestaudio/best",
        "noplaylist": True,
        "quiet": not debug,
        "no_warnings": not debug,
        "verbose": debug,
        "ffmpeg_location": ffmpeg_exe,
    }

    # Only apply YouTube-specific extractor args for YouTube URLs/searches
    is_yt = _is_youtube(query) or not query.startswith(("http://", "https://"))
    if is_yt:
        ydl_opts["extractor_args"] = _youtube_extractor_args(
            client, _bgutil_executable(), force_cli=force_cli
        )

    if debug:
        print(f"[debug][yt-dlp] Query: {query}")
        print(f"[debug][yt-dlp] Is YouTube: {is_yt}")
        print(f"[debug][yt-dlp] ydl_opts: { {k: v for k, v in ydl_opts.items() if k != 'extractor_args'} }")
        if is_yt:
            print(f"[debug][yt-dlp] YouTube client(s): {client}")

    if not query.startswith(("http://", "https://")):
        query = f"ytsearch:{query}"

    if _IMPERSONATE_AVAILABLE:
        ydl_opts['impersonate'] = ImpersonateTarget('chrome')
        # Warn if a non-browser API client is combined with browser TLS impersonation.
        # android_vr + Chrome TLS = detectable contradiction to YouTube's anti-bot stack.
        if is_yt and client and any(
            c.strip().lower() in ('android_vr', 'android', 'android_music', 'ios')
            for c in client.split(',')
        ):
            print(
                f"[yt-dlp] WARNING: youtube_client='{client}' combined with "
                f"--impersonate chrome. Non-browser API client + browser TLS fingerprint "
                f"is a detectable contradiction. Consider switching to 'web' client."
            )

    # COOKIE-01 / per-play re-check: pass operator-exported cookies to yt-dlp.
    # Existence + mtime validated every call (cheap stat syscall; catches mid-session file deletion/refresh).
    if cookies_file:
        if not os.path.isfile(cookies_file):
            print(f"[yt-dlp] Warning: cookies_file '{cookies_file}' not found — running without cookies")
        else:
            age_days = (time.time() - os.path.getmtime(cookies_file)) / 86400
            if age_days > 150:
                print(f"[yt-dlp] Warning: cookies_file is {int(age_days)} days old (>150) — may be stale")
            ydl_opts['cookiefile'] = cookies_file

    # Reuse a warm resolver for this exact option set (see _resolver). The force_cli
    # retry below recurses with a different key, so it takes a different instance and
    # can never re-enter the lock held here.
    resolver_key = (client, cookies_file, debug, force_cli, is_yt)
    with _resolver(resolver_key, ydl_opts) as ydl:
        info = ydl.extract_info(query, download=False)
        if "entries" in info:
            info = info["entries"][0]

        if debug:
            print(f"[debug][yt-dlp] Title: {info.get('title', 'Unknown')}")
            print(f"[debug][yt-dlp] Extractor: {info.get('extractor', 'N/A')}")
            print(f"[debug][yt-dlp] Format: {info.get('format', 'N/A')}")
            print(f"[debug][yt-dlp] Format ID: {info.get('format_id', 'N/A')}")
            print(f"[debug][yt-dlp] Audio codec: {info.get('acodec', 'N/A')}")
            print(f"[debug][yt-dlp] Video codec: {info.get('vcodec', 'N/A')}")
            print(f"[debug][yt-dlp] Audio bitrate (abr): {info.get('abr', 'N/A')}")
            print(f"[debug][yt-dlp] Sample rate: {info.get('asr', 'N/A')}")
            print(f"[debug][yt-dlp] Filesize: {info.get('filesize', 'N/A')}")
            print(f"[debug][yt-dlp] Duration: {info.get('duration', 'N/A')}s")
            url = info.get("url", "")
            print(f"[debug][yt-dlp] URL length: {len(url)}")
            print(f"[debug][yt-dlp] URL prefix: {url[:120]}...")
            print(f"[debug][yt-dlp] URL contains 'googlevideo': {'googlevideo' in url}")
            print(f"[debug][yt-dlp] URL contains 'soundcloud': {'soundcloud' in url}")
            # Log all available formats for comparison
            formats = info.get("formats", [])
            print(f"[debug][yt-dlp] Total formats available: {len(formats)}")
            for i, fmt in enumerate(formats[-5:]):  # Show last 5 (usually best quality)
                print(f"[debug][yt-dlp]   format[{i}]: id={fmt.get('format_id')} "
                      f"ext={fmt.get('ext')} acodec={fmt.get('acodec')} "
                      f"vcodec={fmt.get('vcodec')} abr={fmt.get('abr')} "
                      f"protocol={fmt.get('protocol')}")
        # Always log PO token and visitor data status (even when debug=False)
        url = info.get("url", "")
        vcodec = info.get("vcodec", "none")
        is_audio_only = vcodec in ("none", None, "video only")

        if is_yt:
            # Check if PO token is present in the URL
            if "pot=" in url:
                pot_start = url.index("pot=") + 4
                pot_end = url.index("&", pot_start) if "&" in url[pot_start:] else len(url)
                pot_val = url[pot_start:pot_end]
                print(f"[yt-dlp] PO Token: present ({len(pot_val)} chars)")
            else:
                print("[yt-dlp] PO Token: not present in URL")

            if not is_audio_only:
                print(f"[yt-dlp] WARNING: Combined video+audio format selected "
                      f"(vcodec={vcodec}, format={info.get('format_id')}). "
                      f"Audio-only streams unavailable — bgutil attestation may be broken "
                      f"(YouTube changed ytAtR). Audio will still play (video stripped by FFmpeg) "
                      f"but quality is limited to ~128kbps AAC instead of opus.")
                # One-shot re-resolve with the CLI provider forced. The HTTP server needs
                # ytAtR from the webpage for BotGuard challenge data; when YouTube changes
                # that it degrades to weak tokens that only unlock format 18. The CLI has
                # its own Rust PPA implementation and needs no webpage attestation, so it
                # still unlocks opus. force_cli guards the recursion at depth 1.
                if not force_cli:
                    print("[yt-dlp] Retrying once with the bgutil CLI provider forced…")
                    try:
                        retry = get_audio_url(original_query, client, debug, cookies_file,
                                              force_cli=True)
                    except Exception as e:
                        print(f"[yt-dlp] CLI-forced retry failed ({e}) — keeping the "
                              f"combined format.")
                    else:
                        if retry.get("is_audio_only"):
                            print("[yt-dlp] CLI-forced retry recovered an audio-only format.")
                            return retry
                        print("[yt-dlp] CLI-forced retry also returned a combined format — "
                              "update bgutil-pot or switch client in config.yaml.")

            # Report which session cookies the resolve used. They are NOT sent to the
            # CDN: the stream URL carries a PO token, and adding a Cookie header to a
            # googlevideo request is an immediate 403 (measured). FFmpeg never makes
            # HTTP requests at all — it only reads a pipe. This log exists purely as a
            # diagnostic for cookie auth on the API side (e.g. spotting a missing SOCS,
            # which EU IPs often lack).
            if hasattr(ydl, "cookiejar"):
                yt_cookies = [
                    f"{c.name}={c.value}" for c in ydl.cookiejar
                    if any(d in (c.domain or "")
                           for d in (".youtube.com", "youtube.com",
                                     ".googlevideo.com", "googlevideo.com"))
                ]
                if yt_cookies:
                    names = ", ".join(p.split("=", 1)[0] for p in yt_cookies)
                    print(f"[yt-dlp] Session cookies used for the resolve: "
                          f"{len(yt_cookies)} ({names}) — not sent to the CDN")
                else:
                    print("[yt-dlp] Cookies: none found for youtube.com in cookiejar")

        result = {
            "url": info["url"],
            "title": info.get("title", "Unknown"),
            "thumbnail": info.get("thumbnail", ""),
            "webpage_url": info.get("webpage_url", ""),
            "is_audio_only": is_audio_only,
            "duration": info.get("duration"),
            "artist": info.get("artist") or info.get("uploader") or info.get("channel") or "",
            # Consumed by _can_stream_in_process(): only progressive, non-live sources
            # may use the in-process CDN reader.
            "protocol": info.get("protocol"),
            "is_live": bool(info.get("is_live")),
        }
        if is_yt:
            # Keyed by the RESOLVED video id, so a text search also warms the cache for a
            # later direct-link play of the same track.
            _resolve_cache_put(info.get("id"), result)
        return result


# --- In-process CDN reader -----------------------------------------------------
#
# Fetching an already-resolved googlevideo URL does not need a second Python
# interpreter. `python -m yt_dlp` costs 1.51s to first byte (~0.42s of it just
# importing yt-dlp, the rest the generic extractor probing the URL); the same bytes
# arrive in 0.02s over curl_cffi, sustaining ~60 MB/s. That cost is paid on EVERY
# track — prefetch removes the resolve on transitions, not this.
#
# Everything below was probed against googlevideo rather than assumed:
#   * a rangeless GET is 403 — a range is mandatory;
#   * `&range=a-b` in the URL returns 200 with an exact Content-Length (this is the
#     form the web player uses); a `Range:` header returns 206 and also works;
#   * sending the `Cookie` header is a 403 — the PO token in the URL is the auth;
#   * UA / Referer / Origin make no difference, so impersonate supplies the headers;
#   * `clen=` in the URL equals the real filesize, giving an exact EOF;
#   * a FRESHLY resolved URL 403s for ~2-3s before the edge will serve it, which the
#     1.5s subprocess boot used to mask by accident (and sometimes lost the race to,
#     surfacing as "Error playing track" on a first play).
_STREAM_CHUNK_BYTES = 10 * 1024 * 1024   # matches the old --http-chunk-size 10M

# Settle-window schedule, measured rather than guessed. Polling 6 fresh URLs every
# 250ms: 2 of 6 served instantly (0.02s), the other 4 went live at 2.43 / 2.52 / 2.70 /
# 3.29s. Polling does NOT bring availability forward — a single unpolled request at
# t+3.0s also returned 200 — so tight polling through the dead zone is pure waste
# (13 requests). Instead: one attempt immediately to catch the instant case, then idle
# out the dead zone and poll across the band where URLs actually go live. Costs 1
# request in the fast case and ~2-4 in the slow one.
# Cumulative wake times: 2.2 2.5 2.8 3.1 3.4 3.7 4.2 4.7 5.2 5.7s.
_STREAM_SETTLE_BACKOFF = (2.2, 0.3, 0.3, 0.3, 0.3, 0.3, 0.5, 0.5, 0.5, 0.5)
_STREAM_RESUME_BACKOFF = (0.25, 0.5)     # mid-stream truncation/blip retries
_STREAM_TIMEOUT = 30


def _stream_total_bytes(url: str) -> int | None:
    """Total payload size from the CDN URL's `clen=` param, or None if absent."""
    try:
        clen = parse_qs(urlparse(url).query).get("clen", [None])[0]
        return int(clen) if clen else None
    except (ValueError, TypeError):
        return None


def _range_url(url: str, start: int, end: int) -> str:
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}range={start}-{end}"


def _can_stream_in_process(info: dict | None) -> bool:
    """Whether this resolved track may use the in-process reader.

    Deliberately narrow: googlevideo progressive audio is the only surface actually
    probed. SoundCloud, HLS/DASH, live streams and radio keep the yt-dlp subprocess,
    whose downloader already handles those protocols.
    """
    if not _IMPERSONATE_AVAILABLE or not info:
        return False
    url = info.get("url") or ""
    if not url.startswith(("http://", "https://")):
        return False
    if info.get("is_live"):
        return False
    protocol = info.get("protocol")
    if protocol and protocol not in ("https", "http"):
        return False
    try:
        host = (urlparse(url).hostname or "").lower()
    except Exception:
        return False
    return host.endswith(".googlevideo.com") or host == "googlevideo.com"


def warm_stream_url(url: str, debug: bool = False) -> bool:
    """Poll a freshly resolved CDN URL until the edge will actually serve it.

    Run this OFF the critical path — during the previous track — so the ~2.5s settle
    window is already spent by the time playback starts. Measured: a warmed URL then
    delivers its first byte in 0.02s instead of costing three retries (~2.8s).

    Uses a 1-byte range, so a probe transfers nothing. Two things were verified before
    building this: a 1-byte probe reaches "live" at the same moment a full window would
    (2.55 / 2.58 / 0.02s across three videos, matching the known distribution), and
    probing then abandoning does NOT consume the URL — the real stream afterwards still
    downloaded every byte (3433755/3433755 etc.).

    Returns True once the URL serves. False just means playback will do its own settle
    wait exactly as before, so a failure here is never worse than not calling it.
    """
    if not _IMPERSONATE_AVAILABLE or not url:
        return False
    from curl_cffi import requests as curl_requests

    session = curl_requests.Session(impersonate="chrome")
    try:
        for attempt, delay in enumerate((0.0,) + _STREAM_SETTLE_BACKOFF):
            if _shutdown.is_set():
                return False
            if delay and _shutdown.wait(delay):
                return False        # executor thread: an interruptible blocking wait
            try:
                resp = session.get(_range_url(url, 0, 0), stream=True,
                                   timeout=_STREAM_TIMEOUT)
                next(resp.iter_content(chunk_size=1), b"")
                code = resp.status_code
                resp.close()
            except Exception:
                continue            # transient — the ladder retries
            if code in (200, 206):
                if debug:
                    print(f"[debug][stream] CDN URL warm after {attempt + 1} probe(s)")
                return True
        return False
    finally:
        try:
            session.close()
        except Exception:
            pass


class _StreamStartError(RuntimeError):
    """The in-process reader could not open the stream; caller falls back."""


class _CurlStreamReader:
    """Streams a progressive CDN URL into a pipe FFmpeg can read.

    Duck-types the ``subprocess.Popen`` handle that ``AudioPlayer.play()`` and
    ``stop_playback()`` already drive, so it drops in where ``_start_ytdlp_stream``
    used to sit: ``.stdout``/``.stderr``/``.pid``/``.poll()``/``.terminate()``/
    ``.kill()``/``.wait()``.

    The first window is fetched synchronously in ``__init__`` (the caller already runs
    this in an executor thread). A URL that will not serve therefore shows up as
    ``poll() != None`` right after construction — exactly like a subprocess that exited
    immediately — so play()'s existing stale-cache re-resolve path keeps working.
    """

    def __init__(self, url: str, debug: bool = False):
        from curl_cffi import requests as curl_requests

        self._url = url
        self._debug = debug
        self._total = _stream_total_bytes(url)
        self._closed = threading.Event()
        self._finished = threading.Event()
        self._returncode: int | None = None
        self.error_text = ""
        self.stderr = None       # no child process, nothing to drain
        self.pid = -1

        # No Cookie header (403s a pot-authenticated URL) and no UA override —
        # impersonate="chrome" supplies the full browser header set and TLS fingerprint.
        self._session = curl_requests.Session(impersonate="chrome")

        first = self._open_window(0, first=True)
        if first is None:
            self._returncode = 1
            self._finished.set()
            self._session.close()
            raise _StreamStartError(self.error_text or "CDN refused the stream URL")

        self._rfd, self._wfd = os.pipe()
        self.stdout = os.fdopen(self._rfd, "rb")
        self._thread = threading.Thread(target=self._pump, args=(first,), daemon=True,
                                        name="curl-cdn-reader")
        self._thread.start()

    # -- request helpers ---------------------------------------------------------

    def _open_window(self, offset: int, first: bool = False):
        """GET one range window, retrying transient failures. Returns the streaming
        response, or None once the retry budget is spent."""
        end = offset + _STREAM_CHUNK_BYTES - 1
        if self._total is not None:
            end = min(end, self._total - 1)
            if offset > end:
                return None
        # The settle-window ladder applies to the FIRST window only: a 403 there means
        # the edge has not picked the URL up yet. Mid-stream we only retry blips.
        backoff = _STREAM_SETTLE_BACKOFF if first else _STREAM_RESUME_BACKOFF
        for attempt in range(len(backoff) + 1):
            if self._closed.is_set() or _shutdown.is_set():
                return None
            try:
                resp = self._session.get(_range_url(self._url, offset, end),
                                         stream=True, timeout=_STREAM_TIMEOUT)
            except Exception as e:
                self.error_text = f"{type(e).__name__}: {e}"
            else:
                if resp.status_code in (200, 206):
                    return resp
                # 403/401 on the first window = URL not live at the edge yet (propagation),
                # NOT a rate-limit — this is the one place a 403 is worth retrying, and
                # only here. Mid-stream it means the URL expired; give up and let the
                # caller re-resolve.
                self.error_text = f"HTTP {resp.status_code} at offset {offset}"
                resp.close()
                if not first and resp.status_code in (401, 403):
                    return None
            if attempt < len(backoff):
                if self._debug:
                    print(f"[debug][stream] window at {offset} failed "
                          f"({self.error_text}) — retry in {backoff[attempt]}s")
                if self._closed.wait(backoff[attempt]):
                    return None
        return None

    def _pump(self, response):
        """Write sequential range windows into the pipe until EOF or teardown."""
        offset = 0
        self._complete = False
        try:
            while response is not None and not self._closed.is_set():
                expected = response.headers.get("content-length")
                expected = int(expected) if expected and expected.isdigit() else None
                got = 0
                try:
                    for buf in response.iter_content(chunk_size=65536):
                        if self._closed.is_set():
                            return
                        os.write(self._wfd, buf)   # blocks when full = backpressure
                        got += len(buf)
                finally:
                    response.close()
                offset += got

                if self._total is not None and offset >= self._total:
                    self._complete = True
                    return
                if expected is not None and got < expected:
                    # Truncated mid-window — resume from where we stopped.
                    if self._debug:
                        print(f"[debug][stream] short window ({got}/{expected}) — resuming at {offset}")
                elif self._total is None and (expected is None or got < _STREAM_CHUNK_BYTES):
                    self._complete = True                     # no clen: short window = EOF
                    return
                if got == 0:
                    return                                    # no progress; stop rather than spin
                response = self._open_window(offset)
            if response is None and not self._closed.is_set():
                print(f"[stream] giving up at byte {offset}: {self.error_text}")
        except OSError:
            pass  # read end closed (stop/skip) — normal teardown
        except Exception as e:
            print(f"[stream] reader error at byte {offset}: {type(e).__name__}: {e}")
        finally:
            self._returncode = 0 if self._complete else 1
            try:
                os.close(self._wfd)   # EOF for FFmpeg
            except OSError:
                pass
            try:
                self._session.close()
            except Exception:
                pass
            self._finished.set()

    # -- Popen-compatible surface ------------------------------------------------

    def poll(self):
        return self._returncode if self._finished.is_set() else None

    def terminate(self):
        self._closed.set()
        try:
            self.stdout.close()   # unblocks a pump thread parked in os.write
        except Exception:
            pass

    kill = terminate

    def wait(self, timeout=None):
        if not self._finished.wait(timeout):
            raise subprocess.TimeoutExpired("curl-cdn-reader", timeout or 0)
        return self._returncode


def _open_audio_stream(query: str, client: str, cookies_file: str | None,
                       info: dict, debug: bool = False):
    """Open an audio byte stream for a resolved track.

    Prefers the in-process curl_cffi reader (no second interpreter, ~1.5s faster) and
    falls back to the yt-dlp subprocess for anything it does not cover — or if it
    cannot get the stream open at all, so reliability can only improve.
    """
    direct_url = info.get("url") if info else None
    if _can_stream_in_process(info):
        try:
            return _CurlStreamReader(direct_url, debug=debug)
        except Exception as e:
            print(f"[stream] in-process reader unavailable ({e}) — "
                  f"falling back to the yt-dlp subprocess")
    return _start_ytdlp_stream(query, client, cookies_file, direct_url)


def _stream_error_text(proc) -> str:
    """Failure detail from either stream handle type."""
    if getattr(proc, "stderr", None) is not None:
        try:
            return proc.stderr.read().decode(errors="replace")
        except Exception:
            return ""
    return getattr(proc, "error_text", "")


def _start_ytdlp_stream(
    query: str,
    client: str,
    cookies_file: str | None = None,
    direct_url: str | None = None,
) -> subprocess.Popen:
    """Start yt-dlp as a subprocess that pipes audio bytes to stdout.

    FFmpeg reads from this subprocess's stdout (pipe=True), so it never makes
    direct HTTP requests to YouTube CDN. This bypasses YouTube's TLS/JA3
    fingerprinting that returns HTTP 403 for FFmpeg's libavformat HTTP client.

    When ``direct_url`` is provided (a pre-resolved CDN URL from get_audio_url),
    yt-dlp skips format resolution and downloads the URL directly.  This avoids
    the redundant 2–5 s YouTube API round-trip that the subprocess would otherwise
    incur independently.  Falls back to full query resolution when ``direct_url``
    is None (backward compatibility).
    """
    # When a direct CDN URL is supplied we bypass YouTube-specific logic entirely;
    # the URL is already resolved so no extractor args or bgutil tokens are needed.
    if direct_url is not None:
        actual_query = direct_url
        is_yt = False  # treat as plain URL download — no extractor args required
    else:
        is_yt = _is_youtube(query) or not query.startswith(("http://", "https://"))
        actual_query = f"ytsearch:{query}" if not query.startswith(("http://", "https://")) else query

    bgutil_exe = _bgutil_executable()

    cmd = [
        sys.executable, "-m", "yt_dlp",
        "-f", "bestaudio/best",
        "--no-playlist",
        "-q",
        "--no-warnings",
        "--no-part",
        "-o", "-",  # pipe audio bytes to stdout
        # googlevideo throttles rangeless full-file GETs to ~32 KiB/s after the first
        # ~1 MiB; range-chunked requests (what the YouTube extractor normally configures
        # per-format, lost in the direct-URL handoff) stream at full speed.
        "--http-chunk-size", "10M",
    ]

    if is_yt:
        cmd += ["--extractor-args", f"youtube:player_client={client};fetch_pot=always"]
        if bgutil_exe:
            # Register the CLI provider as a fallback; the registry prefers the warm
            # bgutil HTTP server (see _youtube_extractor_args). Redirecting HTTP to a
            # dead port here would cost ~2s per call — the probe does not fail fast.
            cmd += ["--extractor-args", f"youtubepot-bgutilcli:cli_path={bgutil_exe}"]

    if _IMPERSONATE_AVAILABLE:
        cmd += ["--impersonate", "chrome"]
        if is_yt and any(
            c.strip().lower() in ('android_vr', 'android', 'android_music', 'ios')
            for c in client.split(',')
        ):
            print(
                f"[yt-dlp-pipe] WARNING: youtube_client='{client}' + --impersonate chrome: "
                f"non-browser client contradicts browser TLS fingerprint."
            )

    # COOKIE-01 / per-play re-check (subprocess path) — mirrors get_audio_url logic.
    # Skip cookie injection when using a direct CDN URL: the PO token embedded in the
    # URL already authenticates the request; injecting cookies would be redundant.
    if cookies_file and direct_url is None:
        if not os.path.isfile(cookies_file):
            print(f"[yt-dlp-pipe] Warning: cookies_file '{cookies_file}' not found — running without cookies")
        else:
            age_days = (time.time() - os.path.getmtime(cookies_file)) / 86400
            if age_days > 150:
                print(f"[yt-dlp-pipe] Warning: cookies_file is {int(age_days)} days old (>150) — may be stale")
            cmd += ["--cookies", cookies_file]

    cmd.append(actual_query)

    env = os.environ.copy()
    existing_pythonpath = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = (
        _plugin_dir + (os.pathsep + existing_pythonpath if existing_pythonpath else "")
    )

    kwargs: dict = {
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "cwd": _base_dir,
        "env": env,
    }
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW

    return subprocess.Popen(cmd, **kwargs)


def _youtube_video_id(url: str) -> str | None:
    """Extract the video id from a youtube.com/watch?v= or youtu.be/ URL."""
    if not url:
        return None
    try:
        parsed = urlparse(url)
    except Exception:
        return None
    host = parsed.netloc or ""
    if "youtu.be" in host:
        vid = parsed.path.lstrip("/").split("/")[0]
        return vid or None
    if "youtube.com" in host:
        return parse_qs(parsed.query).get("v", [None])[0]
    return None


def get_related_tracks(seed_url: str, client: str, limit: int = 25) -> list[dict]:
    """Return related YouTube tracks via the seed video's Mix (RD) playlist, for
    autoplay/endless mode. Returns [{"url", "title"}, ...] excluding the seed.
    Empty list for non-YouTube seeds (autoplay only supports YouTube)."""
    vid = _youtube_video_id(seed_url or "")
    if not vid:
        return []
    mix_url = f"https://www.youtube.com/watch?v={vid}&list=RD{vid}"
    seed_watch = f"https://www.youtube.com/watch?v={vid}"
    try:
        info = extract_playlist_info(mix_url, client, limit=limit)
    except Exception:
        return []
    return [t for t in info.get("tracks", []) if t.get("url") and t["url"] != seed_watch]


def _reap_process(proc):
    """Wait on an already-terminate()'d subprocess, killing it if it overruns.

    Meant to run on a daemon thread so the event loop never blocks on the
    wait()/kill() teardown (which can be slow on a throttled/loaded host).
    """
    try:
        proc.wait(timeout=2)
    except subprocess.TimeoutExpired:
        try:
            proc.kill()
        except Exception:
            pass
    except Exception:
        pass


def is_playlist_url(query: str) -> bool:
    """Check if a URL points to a playlist (YouTube or SoundCloud)."""
    if not query.startswith(("http://", "https://")):
        return False
    # YouTube playlists contain list= parameter, but skip auto-generated mixes
    # (Radio/Mix playlists have IDs starting with "RD" — RDMM, RDGMEM, RDCLAK, etc.)
    if _is_youtube(query) and "list=" in query:
        from urllib.parse import urlparse, parse_qs
        list_id = parse_qs(urlparse(query).query).get("list", [""])[0]
        if list_id.startswith("RD"):
            return False
        return True
    # SoundCloud sets (playlists)
    if "soundcloud.com" in query and "/sets/" in query:
        return True
    return False


MAX_PLAYLIST_TRACKS = 2000  # hard cap on tracks loaded from a single playlist (~20 browse pages, paced)


def extract_playlist_info(query: str, client: str, limit: int = MAX_PLAYLIST_TRACKS) -> dict:
    """Extract playlist title and track list using yt-dlp (metadata only, no streams).

    Returns {"title": str, "tracks": [{"url": str, "title": str}, ...]},
    capped at ``limit`` entries (default MAX_PLAYLIST_TRACKS). Pass limit=1 to
    fetch just the first track fast (one page) so playback can start before the
    full enumeration finishes.
    """
    limit = max(1, min(limit, MAX_PLAYLIST_TRACKS))
    ydl_opts = {
        "extract_flat": "in_playlist",  # resolve each entry but don't fetch streams
        "quiet": True,
        "no_warnings": True,
        "noplaylist": False,            # allow playlist extraction
        "playlistend": limit,           # stop enumerating after the cap (huge-playlist guard)
    }
    if limit > 1:
        # Pace continuation-page requests (huge-playlist burst guard). Never on the
        # limit=1 fast fetch: it sits on the track-1 startup path, and yt-dlp sleeps
        # before every request after the first, which would delay playback ~0.5s.
        ydl_opts["sleep_interval_requests"] = 0.5

    if _is_youtube(query):
        yt_args = {"player_client": [c.strip() for c in client.split(",")]}
        ydl_opts["extractor_args"] = {"youtube": yt_args}

    with YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(query, download=False)

    # entries may be a lazy generator; materialize it
    raw_entries = info.get("entries", [])
    entries = list(raw_entries) if raw_entries else []

    tracks = []
    for entry in entries:
        if entry is None:
            continue
        url = entry.get("url") or entry.get("webpage_url") or entry.get("id", "")
        # For YouTube flat extraction, url may be just the video ID
        if _is_youtube(query) and not url.startswith("http"):
            url = f"https://www.youtube.com/watch?v={url}"
        tracks.append({
            "url": url,
            "title": entry.get("title", "Unknown"),
        })

    return {
        "title": info.get("title", "Unknown Playlist"),
        "tracks": tracks[:limit],
    }


class _PrimedAudioSource(discord.AudioSource):
    """Wraps a discord AudioSource with a pre-read first frame.

    discord.py's AudioPlayer thread sets _start = time.perf_counter() before
    calling source.read(). If FFmpeg hasn't produced output yet, that first read
    blocks (codec probe + subprocess startup), causing the timing loop to fall
    behind and rush subsequent frames with delay=0 — audible as ~1-2s of fast
    playback. Pre-reading one frame off the event loop before play() is called
    guarantees the audio thread's first read() returns immediately.
    """

    def __init__(self, source, first_frame: bytes):
        self._source = source
        self._first_frame = first_frame
        self._first_consumed = False

    def read(self) -> bytes:
        if not self._first_consumed:
            self._first_consumed = True
            return self._first_frame
        return self._source.read()

    def is_opus(self) -> bool:
        return self._source.is_opus()

    def cleanup(self):
        self._source.cleanup()


# One 20 ms Discord voice frame = 48000 Hz * 0.02 s * 2 channels * 2 bytes (s16le).
try:
    from discord.opus import Encoder as _OpusEncoder
    _PCM_FRAME_SIZE = _OpusEncoder.FRAME_SIZE
except Exception:  # pragma: no cover - libopus/discord internals absent
    _PCM_FRAME_SIZE = 3840

# How long the jitter buffer may run dry before the track is declared dead. A silent
# frame is the right response to a momentary hiccup; sustained silence means the CDN
# stopped feeding us, and pretending to play for the rest of the track just hides it.
_STALL_SECONDS = 15.0
_STALL_FRAMES = int(_STALL_SECONDS / 0.02)   # one PCM frame = 20 ms


class _BufferedAudioSource(discord.AudioSource):
    """Jitter buffer that decouples discord.py's real-time audio thread from
    yt-dlp/FFmpeg pipeline stalls.

    A daemon thread pre-reads PCM frames from the wrapped source into a bounded
    queue. read() pops a ready frame (near-instant), so the player thread never
    blocks on the pipe — which is what makes it fall behind and then rush frames
    to catch up (audible fast-forward). On a rare underrun (buffer momentarily
    empty but stream not finished) it returns a silent frame instead of blocking:
    a tiny gap rather than a speed-up. On EOF it returns b'' so playback ends.

    The bounded queue also applies backpressure: when full, the reader blocks, so
    FFmpeg/yt-dlp pause — memory stays bounded (~buffer_frames * 3840 bytes).
    """

    _SILENCE = b"\x00" * _PCM_FRAME_SIZE

    def __init__(self, source, first_frame: bytes = b"", buffer_frames: int = 400):
        self._source = source
        self._queue: queue.Queue = queue.Queue(maxsize=buffer_frames)
        self._eof = threading.Event()
        self._closed = threading.Event()
        self._first_frame = first_frame
        self._first_pending = bool(first_frame)
        self._starved_frames = 0   # consecutive underruns; see read()
        self._thread = threading.Thread(target=self._fill, daemon=True, name="pcm-jitter-buffer")
        self._thread.start()

    def _fill(self):
        try:
            while not self._closed.is_set():
                data = self._source.read()
                if not data:
                    break  # EOF
                while not self._closed.is_set():
                    try:
                        self._queue.put(data, timeout=0.5)
                        break
                    except queue.Full:
                        continue  # buffer full → backpressure; retry until space or closed
        except Exception:
            pass
        finally:
            self._eof.set()

    def prefill(self, target: int = 15, timeout: float = 0.5):
        """Block briefly until the buffer has a small cushion (or timeout/EOF)."""
        end = time.monotonic() + timeout
        while (self._queue.qsize() < target and not self._eof.is_set()
               and not self._closed.is_set() and time.monotonic() < end):
            time.sleep(0.02)

    def read(self) -> bytes:
        if self._first_pending:
            self._first_pending = False
            return self._first_frame
        try:
            frame = self._queue.get_nowait()
            self._starved_frames = 0
            return frame
        except queue.Empty:
            if self._eof.is_set():
                return b""  # stream finished and drained → stop playback
            self._starved_frames += 1
            if self._starved_frames >= _STALL_FRAMES:
                # Silence is the right answer to a hiccup, the wrong one to a dead
                # stream: without this the track "plays" inaudibly to its full length
                # while the queue waits, and nobody can tell why. Ending it hands
                # control back to auto-next, which moves on to the next track.
                print(f"[player] Stream stalled for {_STALL_SECONDS:.0f}s with no data "
                      f"— ending the track so playback can continue")
                self._eof.set()
                return b""
            return self._SILENCE  # transient underrun → gap, not a rushed catch-up

    def is_opus(self) -> bool:
        return False

    def cleanup(self):
        self._closed.set()
        try:
            self._source.cleanup()
        except Exception:
            pass


class AudioPlayer:
    """Audio player using discord.py VoiceClient + FFmpegPCMAudio.

    Unlike the Fluxer version (which used LiveKit RTC with manual PCM frame loops),
    this version delegates audio streaming to discord.py's built-in VoiceClient.
    FFmpegPCMAudio handles the ffmpeg subprocess and PCM conversion internally.
    """

    def __init__(self, config: dict):
        self._config = config
        self._voice_client = None  # Set by bot when joining voice
        self.is_playing = False
        self.is_paused = False
        self.current_track_title: str | None = None
        self.current_artist: str = ""
        self.current_duration: int | None = None
        self._playback_done = asyncio.Event()
        self._ytdlp_proc: subprocess.Popen | None = None
        self._playback_gen = 0  # Incremented at start of play()/play_radio() to guard stale after_playback callbacks

        self._sample_rate = config["audio"]["sample_rate"]
        self._channels = config["audio"]["channels"]
        self._ffmpeg_path = _find_ffmpeg(config.get("ffmpeg_path", "ffmpeg"))
        self._debug = config.get("debug", False)
        self._default_bitrate: int = config["audio"].get("bitrate", 128) * 1000
        self._guild_bitrates: dict[int, int] = {}
        self._guild_eq: dict[int, tuple[int, int]] = {}  # guild_id -> (bass_db, treble_db)

        if self._debug:
            print(f"[debug][player] Initialized AudioPlayer")
            print(f"[debug][player]   sample_rate={self._sample_rate}, channels={self._channels}")
            print(f"[debug][player]   ffmpeg_path={self._ffmpeg_path}")
            print(f"[debug][player]   bitrate={self._default_bitrate // 1000} kbps (default)")

    def set_voice_client(self, voice_client):
        """Set the discord.py VoiceClient (called when bot joins a voice channel)."""
        self._voice_client = voice_client

    def get_bitrate_for_guild(self, guild_id: int | None) -> int:
        """Return per-guild bitrate in bps, falling back to the config default."""
        if guild_id is not None:
            return self._guild_bitrates.get(guild_id, self._default_bitrate)
        return self._default_bitrate

    def get_eq_for_guild(self, guild_id: int | None) -> tuple[int, int]:
        """Return (bass_db, treble_db) for this guild, defaulting to (0, 0) = flat."""
        if guild_id is None:
            return (0, 0)
        return self._guild_eq.get(guild_id, (0, 0))

    async def set_eq(self, guild_id: int, bass_db: int, treble_db: int):
        """Store EQ for a guild. Applies to the NEXT track (D-08) — does not
        interrupt current playback. Caller is responsible for range validation
        (done by guild_settings.set_eq_bass/set_eq_treble before reaching here)."""
        self._guild_eq[guild_id] = (int(bass_db), int(treble_db))
        if self._debug:
            print(f"[debug][player] EQ set for guild {guild_id}: bass={bass_db}dB treble={treble_db}dB (applies next track)")

    async def play(self, url_or_query: str, prefetched_info: dict | None = None,
                   prefetched_at: float = 0.0) -> dict:
        """Resolve a URL/query and start playback. Returns dict with title, thumbnail, webpage_url.

        ``prefetched_info`` is an optional get_audio_url() result resolved earlier
        (background prefetch or enqueue). When still fresh it is reused instead of
        resolving again, collapsing the transition resolve to an instant handoff;
        a stale/revoked cached URL transparently falls back to a fresh resolve.

        Architecture: an in-process curl_cffi reader (or, for sources it does not cover,
        the yt-dlp subprocess) pipes audio bytes to FFmpeg's stdin (pipe=True).
        FFmpeg only decodes — it never makes HTTP requests to YouTube CDN.
        This bypasses the HTTP 403 that YouTube returns to FFmpeg's TLS fingerprint.
        """
        import discord

        if self._debug:
            print(f"[debug][player] play() called with: {url_or_query}")

        if not self._voice_client or not self._voice_client.is_connected():
            raise RuntimeError("Not connected to a voice channel")

        yt = self._config["youtube"]
        cookies_file = yt.get("cookies_file") or None  # None if absent or empty string
        loop = asyncio.get_event_loop()

        self._playback_gen += 1
        _gen = self._playback_gen
        self.stop_playback()

        # Resolve → stream. A background prefetch / enqueue may already have resolved
        # this track; reuse that CDN URL while it is comfortably fresh so the transition
        # skips the ~1.3 s resolve entirely. On a stale/revoked cached URL (immediate
        # subprocess exit or an empty first frame) we transparently fall back to a fresh
        # resolve so the user never gets dead air.
        #
        # Streaming itself is unchanged: get_audio_url returns the CDN URL, then
        # _start_ytdlp_stream(direct_url=...) pipes its bytes to FFmpeg's stdin (FFmpeg
        # never makes HTTP requests to YouTube CDN — that would 403 on its TLS fingerprint).
        candidate = prefetched_info if is_stream_info_fresh(prefetched_info, prefetched_at) else None

        # EQ / FFmpeg options are independent of the resolved URL — compute once.
        # Always decode-only (-vn) plus an optional EQ filter chain. When EQ is flat
        # (0,0) _build_ffmpeg_af_options returns "" so we emit exactly "-vn" (no regression).
        guild_id_for_eq = self._voice_client.guild.id if (self._voice_client and hasattr(self._voice_client, 'guild')) else None
        eq_bass, eq_treble = self.get_eq_for_guild(guild_id_for_eq)
        af_flag = _build_ffmpeg_af_options(eq_bass, eq_treble)
        ffmpeg_options = "-vn" if not af_flag else f"-vn -af {af_flag}"
        if self._debug:
            print(f"[debug][player] FFmpeg options: {ffmpeg_options}")

        def _drain_stderr(proc, prefix):
            """Drain a subprocess stderr pipe to console (prevents full-pipe deadlock)."""
            try:
                for raw in proc.stderr:
                    line = raw.decode("utf-8", errors="replace").rstrip()
                    if line:
                        print(f"{prefix} {line}")
            except Exception:
                pass

        info: dict | None = None
        source = None
        first_frame = b""
        for _attempt in range(2):
            used_cache = candidate is not None
            if used_cache:
                info = candidate
                if self._debug:
                    print("[debug][player] Using prefetched resolve (skipping in-process yt-dlp)")
            else:
                if self._debug:
                    print("[debug][player] Resolving audio URL via in-process yt-dlp")
                info = await loop.run_in_executor(
                    _RESOLVE_EXECUTOR, get_audio_url_with_retry,
                    url_or_query, yt["client"], self._debug, cookies_file
                )

            proc = await loop.run_in_executor(
                _STREAM_EXECUTOR, _open_audio_stream, url_or_query, yt["client"],
                cookies_file, info, self._debug
            )
            self._ytdlp_proc = proc  # track immediately so a concurrent stop reaps it

            # The stream may fail to open at all (invalid/expired URL). A cached URL that
            # fails is re-resolved fresh; a fresh resolve that fails is a hard error.
            if proc.poll() is not None:
                stderr = _stream_error_text(proc)
                if used_cache:
                    self._ytdlp_proc = None
                    candidate = None
                    # Also drop the module-level resolve cache entry, or the fresh
                    # re-resolve below would just hand back the same dead URL.
                    invalidate_resolve_cache(url_or_query)
                    if self._debug:
                        print("[debug][player] Cached stream URL failed immediately — re-resolving fresh")
                    continue
                raise RuntimeError(
                    f"audio stream failed to open ({proc.poll()}) before playback started: {stderr[:500]}"
                )

            # Build the FFmpeg source off the event loop — Popen spawn is blocking
            # (~10-80 ms on Windows with AV) and would otherwise stall every guild.
            # FFmpeg still reads only from the yt-dlp stdout pipe (pipe=True).
            proc_stdout = proc.stdout
            src = await loop.run_in_executor(
                _STREAM_EXECUTOR,
                lambda: discord.FFmpegPCMAudio(
                    proc_stdout,
                    executable=self._ffmpeg_path,
                    pipe=True,
                    options=ffmpeg_options,
                    stderr=subprocess.PIPE,
                ),
            )

            # Drain stderr before priming so a full pipe can't deadlock read().
            # The in-process reader has no stderr (no child process) — nothing to drain.
            threading.Thread(target=_drain_stderr, args=(src._process, "[ffmpeg]"), daemon=True).start()
            if proc.stderr is not None:
                threading.Thread(target=_drain_stderr, args=(proc, "[yt-dlp-pipe]"), daemon=True).start()

            # Block in the executor until FFmpeg produces its first PCM frame. An empty
            # frame from a cached URL means the stream produced no audio (stale/revoked) —
            # tear down and re-resolve fresh. A fresh resolve is accepted as-is.
            frame = await loop.run_in_executor(_STREAM_EXECUTOR, src.read)
            if not frame and used_cache:
                if self._debug:
                    print("[debug][player] Cached stream URL produced no audio — re-resolving fresh")
                try:
                    proc.terminate()
                except Exception:
                    pass
                threading.Thread(target=_reap_process, args=(proc,), daemon=True).start()
                try:
                    src.cleanup()
                except Exception:
                    pass
                self._ytdlp_proc = None
                candidate = None
                invalidate_resolve_cache(url_or_query)
                continue

            source = src
            first_frame = frame
            break

        title = info["title"]
        thumbnail = info.get("thumbnail", "")
        webpage_url = info.get("webpage_url", "")

        if self._debug:
            print(f"[debug][player] Resolved title: {title}")
            print(f"[debug][player] audio stream: {type(proc).__name__} (pid {proc.pid})")

        self.is_playing = True
        self.is_paused = False
        self.current_track_title = title
        self.current_artist = info.get("artist") or ""
        self.current_duration = info.get("duration")
        self._playback_done.clear()

        def after_playback(error):
            if self._playback_gen != _gen:
                return  # Stale callback from a previous stream — ignore
            if error:
                print(f"[player] Playback ended with error: {error}")
            self.is_playing = False
            self.current_track_title = None
            loop.call_soon_threadsafe(self._playback_done.set)

        # Wrap in a jitter buffer: a background thread pre-buffers PCM so the audio
        # thread never blocks on a pipeline stall (which makes it rush/catch-up).
        # The already-read first_frame is emitted first; a short prefill builds a
        # small cushion before playback starts.
        source = _BufferedAudioSource(source, first_frame=first_frame)
        await loop.run_in_executor(_STREAM_EXECUTOR, source.prefill)

        # Configure Opus encoder before play() so first frames use music settings.
        # discord.py defaults: fec=True, expected_packet_loss=0.15, signal_type='auto'
        # These defaults waste ~15% of bitrate on error correction and don't optimise for music.
        encoder = getattr(self._voice_client, 'encoder', None)
        if encoder:
            try:
                guild_id = self._voice_client.guild.id if hasattr(self._voice_client, 'guild') else None
                bitrate_bps = self.get_bitrate_for_guild(guild_id)
                encoder.set_signal_type('music')
                encoder.set_fec(False)
                encoder.set_expected_packet_loss_percent(0.01)
                encoder.set_bitrate(bitrate_bps // 1000)
                encoder.set_bandwidth('full')
                if self._debug:
                    print(f"[debug][player] Opus encoder: signal=music, bitrate={bitrate_bps // 1000}kbps, FEC=off, PLP=0%, bandwidth=full")
            except Exception as e:
                if self._debug:
                    print(f"[debug][player] Could not configure opus encoder: {e}")

        self._voice_client.play(source, after=after_playback)

        if self._debug:
            print(f"[debug][player] Playback started for: {title}")

        return {"title": title, "thumbnail": thumbnail, "webpage_url": webpage_url}

    def pause(self):
        """Pause playback."""
        if self._voice_client and self._voice_client.is_playing():
            self._voice_client.pause()
            self.is_paused = True

    def resume(self):
        """Resume paused playback."""
        if self._voice_client and self._voice_client.is_paused():
            self._voice_client.resume()
            self.is_paused = False

    def stop_playback(self):
        """Stop current playback and terminate the yt-dlp stream subprocess."""
        if self._debug:
            print(f"[debug][player] stop_playback() called")
        self.is_playing = False
        self.is_paused = False
        self.current_track_title = None
        self.current_artist = ""
        self.current_duration = None
        if self._voice_client and (self._voice_client.is_playing() or self._voice_client.is_paused()):
            self._voice_client.stop()
        # Tear down the audio stream (frees the network connection and CPU).
        # terminate() is instant; the wait()/kill() reap runs on a daemon thread so a
        # slow-dying subprocess never freezes the event loop mid-skip. The local handle
        # is already detached from self._ytdlp_proc, so the reaper cannot race the next
        # track's subprocess.
        ytdlp_proc = self._ytdlp_proc
        self._ytdlp_proc = None
        if ytdlp_proc and ytdlp_proc.poll() is None:
            try:
                ytdlp_proc.terminate()
            except Exception:
                pass
            threading.Thread(target=_reap_process, args=(ytdlp_proc,), daemon=True).start()
        self._playback_done.set()

    async def play_radio(self, track) -> dict:
        """Stream a live radio URL directly via FFmpegPCMAudio -- no yt-dlp subprocess.

        track.url: HTTP/HTTPS stream URL from radio-browser.info.
        FFmpeg reconnect flags handle transient stream drops transparently (D-16).
        stop_playback() needs zero changes: _ytdlp_proc stays None; voice_client.stop()
        terminates the FFmpegPCMAudio-owned FFmpeg process via discord.py cleanup.
        """
        import discord

        stream_url = track.url
        title = track.title
        thumbnail = track.thumbnail

        if not self._voice_client or not self._voice_client.is_connected():
            raise RuntimeError("Not connected to a voice channel")

        loop = asyncio.get_event_loop()
        self._playback_gen += 1
        _gen = self._playback_gen
        self.stop_playback()

        if self._debug:
            print(f"[debug][player] play_radio() starting: {title} -> {stream_url}")

        # Direct HTTP stream -- FFmpeg handles MP3/AAC/OGG natively.
        # before_options go BEFORE -i (reconnect flags); options go AFTER -i (-vn = no video).
        # Do NOT use pipe=True -- stream_url is a string URL, not a file descriptor.
        # -reconnect_at_eof 1: reconnect when server closes the HTTP connection between chunks
        #   (common with Shoutcast/IceCast stations that restart after each song).
        # -probesize 32k -analyzeduration 0: skip FFmpeg's default 5-second/5MB stream probe.
        #   Without these, FFmpeg blocks for up to 5s before emitting the first PCM frame;
        #   discord.py's AudioPlayer thread sets its clock before that first read, so it rushes
        #   every subsequent frame with delay=0 — audible as choppy/robotic audio at stream start.
        # Build the FFmpeg source off the event loop — Popen spawn is blocking and
        # would otherwise stall every guild's interactions during a radio start.
        source = await loop.run_in_executor(
            _STREAM_EXECUTOR,
            lambda: discord.FFmpegPCMAudio(
                stream_url,
                executable=self._ffmpeg_path,
                before_options=(
                    "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 "
                    "-reconnect_at_eof 1 "
                    "-probesize 32k -analyzeduration 0"
                ),
                options="-vn",
                stderr=subprocess.PIPE,
            ),
        )

        def _log_ffmpeg_stderr(proc):
            try:
                for raw in proc.stderr:
                    line = raw.decode("utf-8", errors="replace").rstrip()
                    if line:
                        print(f"[ffmpeg] {line}")
            except Exception:
                pass
        threading.Thread(
            target=_log_ffmpeg_stderr, args=(source._process,), daemon=True
        ).start()

        self.is_playing = True
        self.is_paused = False
        self.current_track_title = title
        self._playback_done.clear()

        def after_playback(error):
            if self._playback_gen != _gen:
                return  # Stale callback from a previous stream — ignore
            if error:
                print(f"[player] Radio playback ended with error: {error}")
            self.is_playing = False
            self.current_track_title = None
            loop.call_soon_threadsafe(self._playback_done.set)

        # Configure Opus encoder before play() so first frames use music settings.
        encoder = getattr(self._voice_client, "encoder", None)
        if encoder:
            try:
                guild_id = self._voice_client.guild.id if hasattr(self._voice_client, "guild") else None
                bitrate_bps = self.get_bitrate_for_guild(guild_id)
                encoder.set_signal_type("music")
                encoder.set_fec(False)
                encoder.set_expected_packet_loss_percent(0.01)
                encoder.set_bitrate(bitrate_bps // 1000)
                encoder.set_bandwidth("full")
            except Exception:
                pass

        # Prime with one frame so the audio thread's first read() is instant.
        # Reduced probesize means FFmpeg produces the first frame within ~100ms,
        # so a 3-second timeout is conservative. Falls back gracefully on slow stations.
        try:
            first_frame = await asyncio.wait_for(
                loop.run_in_executor(_STREAM_EXECUTOR, source.read),
                timeout=3.0,
            )
            if first_frame:
                source = _PrimedAudioSource(source, first_frame)
        except asyncio.TimeoutError:
            pass

        self._voice_client.play(source, after=after_playback)

        if self._debug:
            print(f"[debug][player] Radio playback started: {title}")

        return {"title": title, "thumbnail": thumbnail, "webpage_url": ""}

    async def wait_for_playback(self):
        """Wait for the current track to finish."""
        if self.is_playing:
            await self._playback_done.wait()

    async def set_bitrate(self, guild_id: int, kbps: int):
        """Update the Opus encoding bitrate for a specific guild."""
        self._guild_bitrates[guild_id] = kbps * 1000
        # Only apply live to the encoder if this guild's voice client is currently active
        vc_guild_id = self._voice_client.guild.id if (self._voice_client and hasattr(self._voice_client, 'guild')) else None
        if vc_guild_id == guild_id and hasattr(self._voice_client, 'encoder') and self._voice_client.encoder:
            try:
                self._voice_client.encoder.set_bitrate(kbps)
            except Exception as e:
                if self._debug:
                    print(f"[debug][player] Could not set encoder bitrate: {e}")

    async def disconnect(self):
        """Stop playback and disconnect from voice."""
        self.stop_playback()
        if self._voice_client and self._voice_client.is_connected():
            await self._voice_client.disconnect()
        self._voice_client = None
        print("[audio] Disconnected from voice channel")
