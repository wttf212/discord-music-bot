import asyncio
import shutil
import subprocess
import sys
import os
import threading

# Load bgutil PO token provider plugin for yt-dlp
_base_dir = os.path.dirname(os.path.abspath(__file__))
_plugin_dir = os.path.join(_base_dir, "yt-dlp-plugins", "bgutil-ytdlp-pot-provider")
if os.path.isdir(_plugin_dir) and _plugin_dir not in sys.path:
    sys.path.insert(0, _plugin_dir)

from yt_dlp import YoutubeDL


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


def get_audio_url(query: str, client: str, debug: bool = False) -> dict:
    """Extract audio URL and title via yt-dlp. Supports YouTube, SoundCloud, and others."""
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
        # client can be comma-separated, e.g. "web,android_vr"
        yt_args = {"player_client": [c.strip() for c in client.split(",")]}
        ydl_opts["extractor_args"] = {"youtube": yt_args}

        # Force bgutil CLI over the HTTP server for PO token generation.
        # The HTTP server parses ytAtR from YouTube's webpage for BotGuard challenge data;
        # when YouTube changes that mechanism the HTTP server breaks ("Failed to extract
        # initial attestation") and falls back to weak tokens that only unlock format 18.
        # The CLI (bgutil-pot.exe) uses its own Rust implementation of the PPA algorithm
        # and does NOT need webpage attestation — its tokens unlock audio-only streams.
        bgutil_exe = os.path.join(_base_dir, "bgutil-pot.exe")
        if os.path.isfile(bgutil_exe):
            ydl_opts["extractor_args"]["youtubepot-bgutilcli"] = {
                "cli_path": [bgutil_exe]
            }
            # Redirect HTTP provider to a dead port so it fails fast and the
            # provider registry falls through to the CLI (preference 1 < HTTP 130,
            # but CLI becomes the only available provider once HTTP is unreachable).
            ydl_opts["extractor_args"]["youtubepot-bgutilhttp"] = {
                "base_url": ["http://127.0.0.1:1"]
            }

    if debug:
        print(f"[debug][yt-dlp] Query: {query}")
        print(f"[debug][yt-dlp] Is YouTube: {is_yt}")
        print(f"[debug][yt-dlp] ydl_opts: { {k: v for k, v in ydl_opts.items() if k != 'extractor_args'} }")
        if is_yt:
            print(f"[debug][yt-dlp] YouTube client(s): {client}")

    if not query.startswith(("http://", "https://")):
        query = f"ytsearch:{query}"

    with YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(query, download=False)
        if "entries" in info:
            info = info["entries"][0]

        # Extract HTTP headers that yt-dlp wants us to use (critical for YouTube)
        http_headers = info.get("http_headers", {})

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
            print(f"[debug][yt-dlp] HTTP headers from yt-dlp: {http_headers}")
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
                      f"but quality is limited to ~128kbps AAC instead of opus. "
                      f"Consider updating bgutil-pot.exe or switching client in config.yaml.")

            # Pass ALL YouTube session cookies to FFmpeg.
            # Audio-only formats (opus/m4a) authenticate via PO token in the URL and
            # don't need cookies. Format 18 (combined MP4) has no PO token — YouTube
            # CDN validates via session cookies (YSC, VISITOR_INFO1_LIVE, etc.) instead,
            # returning HTTP 403 if they're missing.
            if hasattr(ydl, "cookiejar"):
                yt_cookies = [
                    f"{c.name}={c.value}" for c in ydl.cookiejar
                    if any(d in (c.domain or "")
                           for d in (".youtube.com", "youtube.com",
                                     ".googlevideo.com", "googlevideo.com"))
                ]
                if yt_cookies:
                    http_headers["Cookie"] = "; ".join(yt_cookies)
                    names = ", ".join(p.split("=", 1)[0] for p in yt_cookies)
                    print(f"[yt-dlp] Cookies → FFmpeg: {len(yt_cookies)} ({names})")
                else:
                    print("[yt-dlp] Cookies: none found for youtube.com in cookiejar")

            # Referer is required by YouTube CDN for format 18 authentication
            if "Referer" not in http_headers:
                http_headers["Referer"] = "https://www.youtube.com/"

        # Filter headers to only what FFmpeg needs for HTTP video streaming.
        # yt-dlp 2026.03.03+ includes browser-navigation headers (Sec-Fetch-Mode: navigate,
        # Accept: text/html,...) that cause YouTube CDN to serve an HTML page instead of
        # video data when FFmpeg requests the stream — leading to a decoder crash.
        FFMPEG_ALLOWED_HEADERS = {"User-Agent", "Cookie", "Referer", "Origin"}
        http_headers = {k: v for k, v in http_headers.items() if k in FFMPEG_ALLOWED_HEADERS}

        return {
            "url": info["url"],
            "title": info.get("title", "Unknown"),
            "http_headers": http_headers,
            "thumbnail": info.get("thumbnail", ""),
            "webpage_url": info.get("webpage_url", ""),
            "is_audio_only": is_audio_only,
        }


def _start_ytdlp_stream(query: str, client: str) -> subprocess.Popen:
    """Start yt-dlp as a subprocess that pipes audio bytes to stdout.

    FFmpeg reads from this subprocess's stdout (pipe=True), so it never makes
    direct HTTP requests to YouTube CDN. This bypasses YouTube's TLS/JA3
    fingerprinting that returns HTTP 403 for FFmpeg's libavformat HTTP client.
    """
    is_yt = _is_youtube(query) or not query.startswith(("http://", "https://"))
    actual_query = f"ytsearch:{query}" if not query.startswith(("http://", "https://")) else query

    bgutil_exe = os.path.join(_base_dir, "bgutil-pot.exe")

    cmd = [
        sys.executable, "-m", "yt_dlp",
        "-f", "bestaudio/best",
        "--no-playlist",
        "-q",
        "--no-warnings",
        "--no-part",
        "-o", "-",  # pipe audio bytes to stdout
    ]

    if is_yt:
        cmd += ["--extractor-args", f"youtube:player_client={client}"]
        if os.path.isfile(bgutil_exe):
            # Force CLI provider (avoids broken HTTP server attestation)
            cmd += [
                "--extractor-args", f"youtubepot-bgutilcli:cli_path={bgutil_exe}",
                "--extractor-args", "youtubepot-bgutilhttp:base_url=http://127.0.0.1:1",
            ]

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


def is_playlist_url(query: str) -> bool:
    """Check if a URL points to a playlist (YouTube or SoundCloud)."""
    if not query.startswith(("http://", "https://")):
        return False
    # YouTube playlists contain list= parameter
    if _is_youtube(query) and "list=" in query:
        return True
    # SoundCloud sets (playlists)
    if "soundcloud.com" in query and "/sets/" in query:
        return True
    return False


def extract_playlist_info(query: str, client: str) -> dict:
    """Extract playlist title and track list using yt-dlp (metadata only, no streams).

    Returns {"title": str, "tracks": [{"url": str, "title": str}, ...]}
    """
    ydl_opts = {
        "extract_flat": "in_playlist",  # resolve each entry but don't fetch streams
        "quiet": True,
        "no_warnings": True,
        "noplaylist": False,            # allow playlist extraction
    }

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
        "tracks": tracks,
    }


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
        self._playback_done = asyncio.Event()
        self._ytdlp_proc: subprocess.Popen | None = None

        self._sample_rate = config["audio"]["sample_rate"]
        self._channels = config["audio"]["channels"]
        self._ffmpeg_path = _find_ffmpeg(config.get("ffmpeg_path", "ffmpeg"))
        self._debug = config.get("debug", False)
        self._audio_bitrate: int = config["audio"].get("bitrate", 128) * 1000

        if self._debug:
            print(f"[debug][player] Initialized AudioPlayer")
            print(f"[debug][player]   sample_rate={self._sample_rate}, channels={self._channels}")
            print(f"[debug][player]   ffmpeg_path={self._ffmpeg_path}")
            print(f"[debug][player]   bitrate={self._audio_bitrate // 1000} kbps")

    def set_voice_client(self, voice_client):
        """Set the discord.py VoiceClient (called when bot joins a voice channel)."""
        self._voice_client = voice_client

    async def play(self, url_or_query: str) -> dict:
        """Resolve a URL/query and start playback. Returns dict with title, thumbnail, webpage_url.

        Architecture: yt-dlp subprocess pipes audio bytes to FFmpeg's stdin (pipe=True).
        FFmpeg only decodes — it never makes HTTP requests to YouTube CDN.
        This bypasses the HTTP 403 that YouTube returns to FFmpeg's TLS fingerprint.
        """
        import discord

        if self._debug:
            print(f"[debug][player] play() called with: {url_or_query}")

        if not self._voice_client or not self._voice_client.is_connected():
            raise RuntimeError("Not connected to a voice channel")

        yt = self._config["youtube"]
        loop = asyncio.get_event_loop()

        self.stop_playback()

        # Run metadata extraction and subprocess startup concurrently to minimise latency:
        #   get_audio_url  → in-process yt-dlp (download=False) for title/thumbnail/logging
        #   _start_ytdlp_stream → Popen (near-instant); subprocess resolves URL in parallel
        if self._debug:
            print(f"[debug][player] Starting metadata extraction and yt-dlp stream in parallel")

        results = await asyncio.gather(
            loop.run_in_executor(None, get_audio_url, url_or_query, yt["client"], self._debug),
            loop.run_in_executor(None, _start_ytdlp_stream, url_or_query, yt["client"]),
            return_exceptions=True,
        )
        info_result, proc_result = results

        # If the subprocess started but metadata failed (or vice versa), clean up and raise
        if isinstance(info_result, Exception):
            if isinstance(proc_result, subprocess.Popen):
                try:
                    proc_result.terminate()
                except Exception:
                    pass
            raise info_result
        if isinstance(proc_result, Exception):
            raise proc_result

        info: dict = info_result
        self._ytdlp_proc: subprocess.Popen = proc_result

        title = info["title"]
        thumbnail = info.get("thumbnail", "")
        webpage_url = info.get("webpage_url", "")

        if self._debug:
            print(f"[debug][player] Resolved title: {title}")
            print(f"[debug][player] yt-dlp pipe subprocess PID: {self._ytdlp_proc.pid}")

        # FFmpeg reads from yt-dlp's stdout pipe — no HTTP requests to YouTube CDN.
        # Note: FFmpegPCMAudio already appends -f s16le -ar 48000 -ac 2 pipe:1;
        # do NOT add -ar/-ac in options or audio will be corrupted.
        source = discord.FFmpegPCMAudio(
            self._ytdlp_proc.stdout,
            executable=self._ffmpeg_path,
            pipe=True,
            options="-vn",
            stderr=subprocess.PIPE,
        )

        # Log FFmpeg stderr in background so errors are visible in console
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

        # Log yt-dlp subprocess stderr (PO token messages, format selection, errors)
        def _log_ytdlp_pipe_stderr(proc):
            try:
                for raw in proc.stderr:
                    line = raw.decode("utf-8", errors="replace").rstrip()
                    if line:
                        print(f"[yt-dlp-pipe] {line}")
            except Exception:
                pass
        threading.Thread(
            target=_log_ytdlp_pipe_stderr, args=(self._ytdlp_proc,), daemon=True
        ).start()

        self.is_playing = True
        self.is_paused = False
        self.current_track_title = title
        self._playback_done.clear()

        def after_playback(error):
            if error:
                print(f"[player] Playback ended with error: {error}")
            self.is_playing = False
            self.current_track_title = None
            loop.call_soon_threadsafe(self._playback_done.set)

        self._voice_client.play(source, after=after_playback)

        # Configure Opus encoder for music (not voice)
        # discord.py defaults: fec=True, expected_packet_loss=0.15, signal_type='auto'
        # These defaults waste ~15% of bitrate on error correction and don't optimise for music
        encoder = getattr(self._voice_client, 'encoder', None)
        if encoder:
            try:
                encoder.set_signal_type('music')
                encoder.set_fec(False)
                encoder.set_expected_packet_loss_percent(0.01)
                encoder.set_bitrate(self._audio_bitrate // 1000)
                encoder.set_bandwidth('full')
                if self._debug:
                    print(f"[debug][player] Opus encoder: signal=music, bitrate={self._audio_bitrate // 1000}kbps, FEC=off, PLP=0%, bandwidth=full")
            except Exception as e:
                if self._debug:
                    print(f"[debug][player] Could not configure opus encoder: {e}")

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
        if self._voice_client and (self._voice_client.is_playing() or self._voice_client.is_paused()):
            self._voice_client.stop()
        # Terminate the yt-dlp pipe subprocess (frees network connection and CPU)
        ytdlp_proc = self._ytdlp_proc
        self._ytdlp_proc = None
        if ytdlp_proc and ytdlp_proc.poll() is None:
            try:
                ytdlp_proc.terminate()
                ytdlp_proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                ytdlp_proc.kill()
            except Exception:
                pass
        self._playback_done.set()

    async def wait_for_playback(self):
        """Wait for the current track to finish."""
        if self.is_playing:
            await self._playback_done.wait()

    async def set_bitrate(self, kbps: int):
        """Update the Opus encoding bitrate."""
        self._audio_bitrate = kbps * 1000
        if self._voice_client and hasattr(self._voice_client, 'encoder') and self._voice_client.encoder:
            try:
                self._voice_client.encoder.set_bitrate(kbps)  # encoder expects kbps, not bps
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
