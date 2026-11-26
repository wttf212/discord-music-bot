"""Text-to-speech commands: an optional cog the bot loads only when text-to-speech is on.

!tts speaks now, over the music; !ttsafter waits for the next song change. Admins set the
server's speech language, the half-hourly update and the server's own off switch.
"""
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from discord import app_commands
from discord.ext import commands

import speech
import tts
from commands import MusicCog, check_channel, _on_cooldown, _speech
from guild_settings import (
    TTS_LANGUAGES, get_tts_language, set_tts_language,
    get_tts_allowed, set_tts_allowed, get_announce, set_announce,
    get_timezone, get_bitrate, get_eq_bass, get_eq_treble,
)

TTS_COOLDOWN = 10.0   # seconds per user, shared by !tts and !ttsafter
TTS_MAX_WAITING = 5   # clips waiting per server


async def _update_clip(bot, guild_id: int, started_at: float | None, starting_next: bool) -> bytes | None:
    """The half-hourly update's audio for this song change, or None when none is due.

    starting_next: a song is about to start, and counts as waiting. It is False when the
    queue has run out and when a radio station is starting. A broken time zone or a failed
    synthesis raises; Speech logs it and skips the update.
    """
    gid = str(guild_id)
    if not get_announce(gid) or started_at is None:
        return None
    tz = get_timezone(gid)
    now = time.time()
    if not tts.half_hour_mark_passed(started_at, now, tz):
        return None
    gs = bot.get_guild_state(guild_id)
    lang = get_tts_language(gid)
    count = len(gs.queue) + (1 if starting_next else 0)  # the song about to start counts
    return await tts.synthesize(
        tts.announcement_text(lang, datetime.fromtimestamp(now, ZoneInfo(tz)), count),
        lang, gs.player._ffmpeg_path)


def attach_speech(bot, guild_id: int, player) -> None:
    """Give a new player the half-hourly update. main.get_guild_state calls this when TTS is on."""
    player.speech.between_songs = lambda started_at, starting_next: _update_clip(
        bot, guild_id, started_at, starting_next)


async def _join_author_voice(ctx, gs):
    """Join the author's voice channel as !radio does, restoring the saved bitrate and EQ.

    For a bot with no voice client only: unlike !radio it never drops one that isn't
    connected (see _tts_flow). Returns the voice client, or None after telling the author
    why the join failed.
    """
    guild_id = str(ctx.guild.id)
    try:
        voice_client = await ctx.author.voice.channel.connect()
        gs.player.set_voice_client(voice_client)
        saved_br = get_bitrate(guild_id)
        if saved_br:
            await gs.player.set_bitrate(ctx.guild.id, saved_br)
        saved_bass = get_eq_bass(guild_id)
        saved_treble = get_eq_treble(guild_id)
        if saved_bass != 0 or saved_treble != 0:
            await gs.player.set_eq(ctx.guild.id, saved_bass, saved_treble)
    except Exception as e:
        await ctx.send(f"Failed to join voice channel: {e}")
        return None
    return voice_client


async def _tts_flow(cog, ctx, text: str | None, after: bool) -> None:
    """!tts and !ttsafter: refuse what the limits forbid, make the audio, hand it to Speech."""
    if not await check_channel(ctx):
        return
    if not ctx.guild:
        return
    gid = str(ctx.guild.id)
    usage = f"Usage: `{cog.bot.command_prefix}{'ttsafter' if after else 'tts'} <text>`"
    if not get_tts_allowed(gid):
        await ctx.send("Text-to-speech is turned off on this server.")
        return
    if not text:
        await ctx.send(usage)
        return
    if not ctx.author.voice or not ctx.author.voice.channel:
        await ctx.send("You need to be in a voice channel.")
        return
    bot_voice = ctx.guild.voice_client
    if bot_voice and bot_voice.is_connected() and ctx.author.voice.channel != bot_voice.channel:
        await ctx.send("You must be in the same voice channel as the bot.")
        return

    lang = get_tts_language(gid)
    # Mentions become names; without fix_channel_mentions a channel's id would be read out.
    mentions_as_names = commands.clean_content(fix_channel_mentions=True, use_nicknames=True)
    text = tts.clean_text(await mentions_as_names.convert(ctx, text), lang)
    if not text:
        await ctx.send(usage)
        return
    if len(text) > tts.MAX_CHARS:
        await ctx.send(f"That's too long — {tts.MAX_CHARS} characters max.")
        return
    gs = cog.bot.get_guild_state(ctx.guild.id)
    speech_ = _speech(gs)
    if speech_.waiting_clips() >= TTS_MAX_WAITING:
        await ctx.send("Too many messages waiting — try again in a moment.")
        return
    if _on_cooldown(ctx.guild.id, ctx.author.id, "tts", TTS_COOLDOWN):
        await ctx.send(f"Easy — one message every {TTS_COOLDOWN:g} seconds.")
        return

    if ctx.interaction:
        await ctx.defer()  # synthesis can outlast the 3 s token; the reply is public
    try:
        pcm = await tts.synthesize(text, lang, gs.player._ffmpeg_path)
    except tts.TtsError as e:
        print(f"[tts] Synthesis failed: {e}")
        await ctx.send("Couldn't make that into speech right now.")
        return

    # Synthesis takes a second or so: what was checked before it may have changed since.
    if not get_tts_allowed(gid):
        await ctx.send("Text-to-speech is turned off on this server.")
        return
    if speech_.waiting_clips() >= TTS_MAX_WAITING:
        await ctx.send("Too many messages waiting — try again in a moment.")
        return
    # Join only when the bot isn't in voice at all. A voice client that isn't connected is
    # discord.py connecting or reconnecting: dropping it would stop the song, so say()
    # reports the outage instead.
    if ctx.guild.voice_client is None:
        if not ctx.author.voice or not ctx.author.voice.channel:
            await ctx.send("You need to be in a voice channel.")
            return
        if await _join_author_voice(ctx, gs) is None:
            return
    current = gs.queue.current
    # While a station starts, current is still the song that ended, but a station never ends
    # by itself: the clip goes with its start, as a !tts would.
    if (after and current is not None and not current.is_radio and not speech_.starting_radio
            and (gs.player.is_playing or speech_.starting)):
        speech_.queue_after(pcm)
        where = "after"
    else:
        try:
            where = await speech_.say(pcm)
        except speech.SpeechUnavailable:
            await ctx.send("Voice connection is down — try again in a moment.")
            return

    if ctx.interaction:
        note = {"after": " *(after this song)*", "paused": " *(when playback resumes)*"}.get(where, "")
        await ctx.send(f"🔊 {text}{note}")
        return
    try:
        await ctx.message.add_reaction("🔊")
    except Exception:
        await ctx.send("🔊")
    if where == "paused":
        await ctx.send("Playback is paused — it'll play when you resume.")


def _on_off(state: str | None) -> bool | None:
    """True for "on", False for "off", None for anything else, including nothing."""
    return {"on": True, "off": False}.get((state or "").strip().lower())


class TtsCog(commands.Cog):
    """The text-to-speech commands. setup() in commands.py adds this cog only when TTS is on."""

    def __init__(self, bot):
        self.bot = bot

    _check_admin = MusicCog._check_admin  # needs only self.bot and ctx

    @commands.hybrid_command(name="tts", description="Say something in the voice channel right now (the music dips)")
    @app_commands.describe(text="What the bot should say")
    async def tts(self, ctx: commands.Context, *, text: str = None):
        await _tts_flow(self, ctx, text, after=False)

    @commands.hybrid_command(name="ttsafter", description="Say something after the current song")
    @app_commands.describe(text="What the bot should say")
    async def ttsafter(self, ctx: commands.Context, *, text: str = None):
        await _tts_flow(self, ctx, text, after=True)

    @commands.hybrid_command(name="ttslang", description="Speech language for this server")
    @app_commands.describe(lang="lv (Latvian) or en (English)")
    async def ttslang(self, ctx: commands.Context, lang: str = None):
        if not await check_channel(ctx):
            return
        if not ctx.guild:
            return
        if not await self._check_admin(ctx):
            return
        gid = str(ctx.guild.id)
        if not lang:
            await ctx.send(f"Speech language: **{get_tts_language(gid)}**. "
                           f"Usage: `{self.bot.command_prefix}ttslang lv|en`")
            return
        lang = lang.strip().lower()
        if lang not in TTS_LANGUAGES:
            await ctx.send("Use `lv` or `en`.")
            return
        set_tts_language(gid, lang)
        await ctx.send(f"Speech language set to **{lang}**.")

    @commands.hybrid_command(name="announce", description="Say the time and songs left every half hour, between songs")
    @app_commands.describe(state="on or off")
    async def announce(self, ctx: commands.Context, state: str = None):
        if not await check_channel(ctx):
            return
        if not ctx.guild:
            return
        if not await self._check_admin(ctx):
            return
        gid = str(ctx.guild.id)
        on = _on_off(state)
        if on is None:  # nothing, or not on/off: show the current value
            current = "on" if get_announce(gid) else "off"
            await ctx.send(f"Half-hourly updates are **{current}**. "
                           f"Usage: `{self.bot.command_prefix}announce on|off`")
            return
        set_announce(gid, on)
        await ctx.send(f"Half-hourly updates turned **{'on' if on else 'off'}**.")

    @commands.hybrid_command(name="ttsallow", description="Turn text-to-speech on or off for this server")
    @app_commands.describe(state="on or off")
    async def ttsallow(self, ctx: commands.Context, state: str = None):
        if not await check_channel(ctx):
            return
        if not ctx.guild:
            return
        if not await self._check_admin(ctx):
            return
        gid = str(ctx.guild.id)
        on = _on_off(state)
        if on is None:  # nothing, or not on/off: show the current value
            current = "on" if get_tts_allowed(gid) else "off"
            await ctx.send(f"Text-to-speech is **{current}** on this server. "
                           f"Usage: `{self.bot.command_prefix}ttsallow on|off`")
            return
        set_tts_allowed(gid, on)
        if not on:  # also silence what is playing and drop every waiting clip
            _speech(self.bot.get_guild_state(ctx.guild.id)).cut(drop_after_song=True)
        await ctx.send(f"Text-to-speech turned **{'on' if on else 'off'}** for this server.")
