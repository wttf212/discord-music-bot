import asyncio
import os
import sys
import yaml

# Load bgutil PO token provider plugin for yt-dlp (must be done before yt-dlp is imported)
# This enables dynamic PO token generation to prevent YouTube IP bans
_base_dir = os.path.dirname(os.path.abspath(__file__))
_plugin_dir = os.path.join(_base_dir, "yt-dlp-plugins", "bgutil-ytdlp-pot-provider")
if os.path.isdir(_plugin_dir) and _plugin_dir not in sys.path:
    sys.path.insert(0, _plugin_dir)

# Ensure deno JS runtime is on PATH (needed for YouTube signature solving)
_deno_dir = os.path.join(os.path.expanduser("~"), ".deno", "bin")
if os.path.isdir(_deno_dir) and _deno_dir not in os.environ.get("PATH", ""):
    os.environ["PATH"] = _deno_dir + os.pathsep + os.environ.get("PATH", "")

# Start bgutil-pot HTTP server in background (PO token provider)
import subprocess
import shutil
_bgutil_name = "bgutil-pot.exe" if sys.platform == "win32" else "bgutil-pot"
_bgutil_path = os.path.join(_base_dir, _bgutil_name)
if not os.path.isfile(_bgutil_path):
    _bgutil_path = shutil.which(_bgutil_name)  # fallback: check PATH
_bgutil_proc = None
if _bgutil_path:
    try:
        _bgutil_proc = subprocess.Popen(
            [_bgutil_path, "server", "--host", "127.0.0.1"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        print(f"[main] bgutil-pot server started (PID {_bgutil_proc.pid})")
    except Exception as e:
        print(f"[main] Warning: could not start bgutil-pot server: {e}")
else:
    print("[main] Warning: bgutil-pot binary not found, PO tokens will not be generated")

import discord
from discord.ext import commands
from audio_player import AudioPlayer
from track_queue import TrackQueue


class MusicBot(commands.Bot):
    def __init__(self, config: dict):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.voice_states = True
        intents.guilds = True
        intents.guild_reactions = True

        super().__init__(
            command_prefix=config.get("prefix", "!"),
            intents=intents,
            help_command=None,  # We use our own help command
        )

        self.config = config
        self.player = AudioPlayer(config)
        self.queue = TrackQueue()
        self.pending_playlists: dict = {}  # message_id -> playlist info
        self._auto_next_task: asyncio.Task | None = None  # prevent duplicate chains
        self._auto_next_gen: int = 0
        self._empty_channel_task: asyncio.Task | None = None  # 1-min leave timer
        self._current_guild_id: int | None = None
        self.current_text_channel_id: int | None = None

    async def setup_hook(self):
        """Called when the bot is starting up — load cogs here."""
        from commands import setup
        await setup(self)


def main():
    # --- TLS-01c: curl_cffi startup check ---
    try:
        import curl_cffi
        print(f"[main] curl_cffi {curl_cffi.__version__} available — TLS impersonation enabled")
    except ImportError:
        print(
            "[main] Warning: curl_cffi is not installed. "
            "TLS impersonation will be DISABLED. "
            "Install with: pip install 'yt-dlp[default,curl-cffi]'"
        )

    with open("config.yaml", "r") as f:
        config = yaml.safe_load(f)

    token = config["bot_token"]

    # --- COOKIE-02: cookies_file startup check ---
    # Mirrors the TLS-01c guard pattern: warn and continue; never sys.exit().
    # Per-play re-check in audio_player.py catches mid-session file changes.
    _cookies_file = config.get("youtube", {}).get("cookies_file") or None
    if _cookies_file:
        import time as _time
        if not os.path.isfile(_cookies_file):
            print(f"[main] Warning: cookies_file '{_cookies_file}' does not exist — cookie auth disabled")
        else:
            _age_days = (_time.time() - os.path.getmtime(_cookies_file)) / 86400
            if _age_days > 150:
                print(
                    f"[main] Warning: cookies_file is {int(_age_days)} days old (>150 days). "
                    f"VISITOR_INFO1_LIVE may be expired — re-export cookies."
                )
            else:
                print(f"[main] cookies_file found ({int(_age_days)} days old) — cookie auth enabled")

    bot = MusicBot(config)

    async def _do_empty_leave(bot, message: str):
        """Shared teardown: stop playback, clear queue, disconnect, and send a message."""
        text_channel_id = bot.current_text_channel_id

        bot.queue.clear()
        bot.player.stop_playback()
        bot._auto_next_gen = getattr(bot, '_auto_next_gen', 0) + 1
        if bot._auto_next_task and not bot._auto_next_task.done():
            bot._auto_next_task.cancel()
            bot._auto_next_task = None
        bot._empty_channel_task = None

        # Disconnect voice
        for vc in bot.voice_clients:
            if vc.is_connected():
                await vc.disconnect()
        bot.player._voice_client = None
        bot._current_guild_id = None

        if text_channel_id:
            channel = bot.get_channel(text_channel_id)
            if channel:
                await channel.send(message)

    async def _leave_after_timeout(bot, voice_client):
        """Wait 1 minute, then leave if the channel is still empty."""
        await asyncio.sleep(60)
        if not voice_client.is_connected():
            return
        members = [m for m in voice_client.channel.members if not m.bot]
        if members:
            return  # someone rejoined
        await _do_empty_leave(bot, "No one in the voice channel for 1 minute. Leaving.")

    async def _handle_empty_channel(bot, voice_client):
        if not voice_client.is_connected():
            return
        # Re-check in case someone rejoined
        members = [m for m in voice_client.channel.members if not m.bot]
        if members:
            return

        # If music is playing, start a 1-minute timeout instead of leaving immediately
        if bot.player.is_playing:
            if not (bot._empty_channel_task and not bot._empty_channel_task.done()):
                bot._empty_channel_task = asyncio.create_task(_leave_after_timeout(bot, voice_client))
            return

        await _do_empty_leave(bot, "Everyone left the voice channel. Leaving.")

    @bot.event
    async def on_ready():
        print(f"[main] Ready as {bot.user}")

    @bot.event
    async def on_voice_state_update(member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
        """Handle voice state changes for auto-leave when channel empties."""
        # Only care about the guild where the bot is in voice
        voice_client = member.guild.voice_client
        if not voice_client or not voice_client.is_connected():
            return

        bot_channel = voice_client.channel

        # Check if this event is relevant to the bot's channel
        was_in_bot_channel = before.channel == bot_channel
        is_in_bot_channel = after.channel == bot_channel

        if not was_in_bot_channel and not is_in_bot_channel:
            return  # Unrelated channel change

        # Someone left the bot's channel
        if was_in_bot_channel and not is_in_bot_channel:
            non_bot_members = [m for m in bot_channel.members if not m.bot]
            if not non_bot_members:
                asyncio.create_task(_handle_empty_channel(bot, voice_client))

        # Someone joined the bot's channel — cancel pending leave
        if is_in_bot_channel and not member.bot:
            if bot._empty_channel_task and not bot._empty_channel_task.done():
                bot._empty_channel_task.cancel()
                bot._empty_channel_task = None

    try:
        bot.run(token)
    finally:
        if _bgutil_proc and _bgutil_proc.poll() is None:
            _bgutil_proc.terminate()
            print("[main] bgutil-pot server stopped")


if __name__ == "__main__":
    main()
