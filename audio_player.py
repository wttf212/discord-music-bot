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
    ydl_opts = {
        "format": "bestaudio/best",
        "noplaylist": True,
        "quiet": not debug,
        "no_warnings": not debug,
        "verbose": debug,
    }

    # Only apply YouTube-specific extractor args for YouTube URLs/searches
    is_yt = _is_youtube(query) or not query.startswith(("http://", "https://"))
    if is_yt:
        # client can be comma-separated, e.g. "web,android_vr"
        # PO tokens are generated automatically by the bgutil plugin
        yt_args = {"player_client": [c.strip() for c in client.split(",")]}
        ydl_opts["extractor_args"] = {"youtube": yt_args}

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
        if is_yt:
            # Check if PO token is present in the URL
            if "pot=" in url:
                pot_start = url.index("pot=") + 4
                pot_end = url.index("&", pot_start) if "&" in url[pot_start:] else len(url)
                pot_val = url[pot_start:pot_end]
                print(f"[yt-dlp] PO Token: present ({len(pot_val)} chars)")
            else:
                print("[yt-dlp] PO Token: not present in URL")

            # Check for visitor data in cookies
            cookies = info.get("cookies", "")
            visitor_data = ""
            for cookie in info.get("http_headers", {}).get("Cookie", "").split(";"):
                if "VISITOR_INFO1_LIVE" in cookie:
                    visitor_data = cookie.split("=", 1)[-1].strip()
                    break
            if visitor_data:
                print(f"[yt-dlp] Visitor Data: present ({len(visitor_data)} chars)")
            else:
                print("[yt-dlp] Visitor Data: not present in cookies")

        return {"url": info["url"], "title": info.get("title", "Unknown"), "http_headers": http_headers}


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

    async def play(self, url_or_query: str) -> str:
        """Resolve a URL/query and start playback. Returns track title."""
        import discord

        if self._debug:
            print(f"[debug][player] play() called with: {url_or_query}")

        if not self._voice_client or not self._voice_client.is_connected():
            raise RuntimeError("Not connected to a voice channel")

        yt = self._config["youtube"]
        info = await asyncio.get_event_loop().run_in_executor(
            None, get_audio_url, url_or_query, yt["client"], self._debug
        )
        audio_url = info["url"]
        title = info["title"]
        http_headers = info.get("http_headers", {})

        if self._debug:
            print(f"[debug][player] Resolved title: {title}")
            print(f"[debug][player] Audio URL length: {len(audio_url)}")
            print(f"[debug][player] HTTP headers to send: {http_headers}")

        self.stop_playback()

        # Build ffmpeg options with HTTP headers from yt-dlp
        # YouTube returns 403 Forbidden if these headers (especially User-Agent) are missing
        before_options = "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5"

        if http_headers:
            # ffmpeg expects headers as a single string with \r\n separating each header
            headers_str = "".join(f"{k}: {v}\r\n" for k, v in http_headers.items())
            before_options = f'-headers "{headers_str}" ' + before_options
            if self._debug:
                print(f"[debug][ffmpeg] Injecting headers: {headers_str!r}")

        # Note: FFmpegPCMAudio already adds -f s16le -ar 48000 -ac 2 pipe:1
        # Do NOT duplicate -ar/-ac here or audio will be corrupted
        options = "-vn"

        if self._debug:
            print(f"[debug][ffmpeg] before_options: {before_options}")
            print(f"[debug][ffmpeg] options: {options}")

        source = discord.FFmpegPCMAudio(
            audio_url,
            executable=self._ffmpeg_path,
            before_options=before_options,
            options=options,
        )

        self.is_playing = True
        self.is_paused = False
        self.current_track_title = title
        self._playback_done.clear()

        loop = asyncio.get_event_loop()

        def after_playback(error):
            if error and self._debug:
                print(f"[debug][player] Playback error: {error}")
            self.is_playing = False
            self.current_track_title = None
            loop.call_soon_threadsafe(self._playback_done.set)

        self._voice_client.play(source, after=after_playback)

        # Configure Opus encoder for music (not voice)
        # discord.py defaults: fec=True, expected_packet_loss=0.15, signal_type='auto'
        # These defaults waste ~15% of bitrate on error correction and don't optimize for music
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

        return title

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
        """Stop current playback."""
        if self._debug:
            print(f"[debug][player] stop_playback() called")
        self.is_playing = False
        self.is_paused = False
        self.current_track_title = None
        if self._voice_client and (self._voice_client.is_playing() or self._voice_client.is_paused()):
            self._voice_client.stop()
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
                self._voice_client.encoder.set_bitrate(self._audio_bitrate)
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
