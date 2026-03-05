import asyncio
import discord
from discord.ext import commands
from track_queue import Track
from audio_player import is_playlist_url, extract_playlist_info
from guild_settings import get_allowed_channel, set_allowed_channel, get_bitrate, set_bitrate


PLAYLIST_EMOJI = "\u2705"  # ✅


def create_np_embed(bot, title: str, extra_desc: str = "") -> discord.Embed:
    """Creates an embed for Now Playing."""
    kbps = bot.player._audio_bitrate // 1000
    p = bot.command_prefix

    desc = f"**{title}**"
    if extra_desc:
        desc += f"\n\n{extra_desc}"

    embed = discord.Embed(
        title="▶️ Now Playing",
        description=desc,
        color=0x3498db,
    )
    embed.set_footer(text=f"Audio: {kbps} kbps • {p}bitrate <kbps> to change")
    return embed


async def update_channel_topic(bot, channel_id: int, topic_text: str):
    """Updates the textual channel topic."""
    try:
        channel = bot.get_channel(channel_id)
        if channel:
            await channel.edit(topic=topic_text)
    except Exception as e:
        print(f"[commands] Failed to update channel topic: {e}")


async def check_channel(ctx: commands.Context) -> bool:
    """Check if command is in the allowed channel. Deletes message and notifies if not."""
    guild_id = str(ctx.guild.id) if ctx.guild else None
    if not guild_id:
        return True

    allowed = get_allowed_channel(guild_id)
    if not allowed:
        return True

    if str(ctx.channel.id) == allowed:
        return True

    # Wrong channel: delete and notify
    try:
        await ctx.message.delete()
    except Exception:
        pass
    allowed_channel = ctx.bot.get_channel(int(allowed))
    if allowed_channel:
        await allowed_channel.send(
            f"{ctx.author.mention}, please use commands in <#{allowed}>.",
        )
    return False


class MusicCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.command(name="settc")
    async def settc(self, ctx: commands.Context):
        owner_id = str(self.bot.config.get("owner_id", ""))
        if str(ctx.author.id) != owner_id:
            await ctx.send("Only the bot owner can use this command.")
            return

        if not ctx.guild:
            await ctx.send("This command can only be used in a server.")
            return

        guild_id = str(ctx.guild.id)
        channel_id = str(ctx.channel.id)
        set_allowed_channel(guild_id, channel_id)
        await ctx.send(f"Commands are now restricted to <#{channel_id}>.")

    @commands.command(name="play")
    async def play(self, ctx: commands.Context, *, query: str = None):
        if not await check_channel(ctx):
            return

        if not query:
            await ctx.send(f"Usage: `{self.bot.command_prefix}play <url or search>`")
            return

        if not ctx.author.voice or not ctx.author.voice.channel:
            await ctx.send("You need to be in a voice channel.")
            return

        voice_channel = ctx.author.voice.channel
        guild_id = str(ctx.guild.id)

        # ---------------------------------------------------------------
        # Playlist detection: play first track, offer to add the rest
        # ---------------------------------------------------------------
        if is_playlist_url(query):
            status_msg = await ctx.send("🔍 Fetching playlist info...")
            try:
                yt_client = self.bot.config.get("youtube", {}).get("client", "web")
                playlist_info = await asyncio.get_event_loop().run_in_executor(
                    None, extract_playlist_info, query, yt_client
                )
            except Exception as e:
                await status_msg.edit(content=f"Error fetching playlist: {e}")
                return

            tracks = playlist_info["tracks"]
            if not tracks:
                await status_msg.edit(content="No tracks found in this playlist.")
                return

            playlist_title = playlist_info["title"]
            first_track_info = tracks[0]
            remaining_tracks = tracks[1:]

            # Join voice if not already connected
            voice_client = ctx.guild.voice_client
            if not voice_client or not voice_client.is_connected():
                try:
                    voice_client = await voice_channel.connect()
                    self.bot.player.set_voice_client(voice_client)
                    # Restore saved bitrate for this guild
                    saved_br = get_bitrate(guild_id)
                    if saved_br:
                        await self.bot.player.set_bitrate(saved_br)
                except Exception as e:
                    await status_msg.edit(content=f"Failed to join voice channel: {e}")
                    return

            # Play the first track immediately
            try:
                title = await self.bot.player.play(first_track_info["url"])
            except Exception as e:
                await status_msg.edit(content=f"Error playing first track: {e}")
                return

            channel_id = ctx.channel.id

            if not remaining_tracks:
                embed = create_np_embed(self.bot, title, f"From playlist: **{playlist_title}**")
                await status_msg.edit(content="", embed=embed)
                await update_channel_topic(self.bot, channel_id, f"▶️ Now playing: {title}")
                _start_auto_next(self.bot, channel_id)
                return

            # Show offer to load the rest
            count = len(remaining_tracks)
            extra = (
                f"📋 **{playlist_title}** has **{count}** more tracks.\n"
                f"React ✅ or type `{self.bot.command_prefix}loadall` to add them to the queue."
            )
            embed = create_np_embed(self.bot, title, extra)
            await status_msg.edit(content="", embed=embed)
            await update_channel_topic(self.bot, channel_id, f"▶️ Now playing: {title}")

            # Try adding ✅ reaction
            try:
                await status_msg.add_reaction(PLAYLIST_EMOJI)
            except Exception as e:
                print(f"[commands] Could not add reaction: {e}")

            # Store pending playlist for reaction or !loadall
            self.bot.pending_playlists[str(status_msg.id)] = {
                "query": query,
                "user_id": str(ctx.author.id),
                "guild_id": guild_id,
                "channel_id": channel_id,
                "tracks": remaining_tracks,
                "playlist_title": playlist_title,
            }
            # Also store by channel for !loadall lookup
            self.bot.pending_playlists[f"channel_{channel_id}"] = str(status_msg.id)

            _start_auto_next(self.bot, channel_id)

            # Auto-expire after 120 seconds
            async def _expire_playlist(msg_id: str, ch_id: int):
                await asyncio.sleep(120)
                removed = self.bot.pending_playlists.pop(msg_id, None)
                # Also clean up channel reference
                if self.bot.pending_playlists.get(f"channel_{ch_id}") == msg_id:
                    self.bot.pending_playlists.pop(f"channel_{ch_id}", None)
                if removed:
                    try:
                        expired_extra = (
                            f"📋 **{playlist_title}** had **{count}** more tracks.\n"
                            f"~~React ✅ or type `{self.bot.command_prefix}loadall`~~ *(expired)*"
                        )
                        expired_embed = create_np_embed(self.bot, title, expired_extra)
                        channel = self.bot.get_channel(ch_id)
                        if channel:
                            msg = await channel.fetch_message(int(msg_id))
                            await msg.edit(content="", embed=expired_embed)
                    except Exception:
                        pass

            asyncio.create_task(_expire_playlist(str(status_msg.id), channel_id))
            return

        # ---------------------------------------------------------------
        # Single track flow
        # ---------------------------------------------------------------

        # Join voice if not already connected
        voice_client = ctx.guild.voice_client
        if not voice_client or not voice_client.is_connected():
            try:
                voice_client = await voice_channel.connect()
                self.bot.player.set_voice_client(voice_client)
                # Restore saved bitrate for this guild
                saved_br = get_bitrate(guild_id)
                if saved_br:
                    await self.bot.player.set_bitrate(saved_br)
            except Exception as e:
                await ctx.send(f"Failed to join voice channel: {e}")
                return

        user_id = str(ctx.author.id)
        channel_id = ctx.channel.id

        if self.bot.player.is_playing:
            track = Track(query=query, title="Resolving...", requested_by=user_id)
            self.bot.queue.add(track)
            await ctx.send(f"Added to queue: **{query}**")
        else:
            status_msg = await ctx.send("▶️ Resolving...")
            try:
                title = await self.bot.player.play(query)
                embed = create_np_embed(self.bot, title)
                await status_msg.edit(content="", embed=embed)
                await update_channel_topic(self.bot, channel_id, f"▶️ Now playing: {title}")
                _start_auto_next(self.bot, channel_id)
            except Exception as e:
                await status_msg.edit(content=f"Error playing track: {e}")

    @commands.command(name="pause")
    async def pause(self, ctx: commands.Context):
        if not await check_channel(ctx):
            return
        if not self.bot.player.is_playing:
            await ctx.send("Nothing is playing.")
            return
        if self.bot.player.is_paused:
            await ctx.send("Already paused.")
            return
        self.bot.player.pause()
        await ctx.send(f"Paused: **{self.bot.player.current_track_title}**")

    @commands.command(name="resume")
    async def resume(self, ctx: commands.Context):
        if not await check_channel(ctx):
            return
        if not self.bot.player.is_paused:
            await ctx.send("Not paused.")
            return
        self.bot.player.resume()
        await ctx.send(f"Resumed: **{self.bot.player.current_track_title}**")

    @commands.command(name="stop")
    async def stop(self, ctx: commands.Context):
        if not await check_channel(ctx):
            return

        self.bot.player.stop_playback()
        self.bot.queue.clear()
        await self.bot.player.disconnect()
        await update_channel_topic(self.bot, ctx.channel.id, "Queue is empty.")
        await ctx.send("Stopped playback and left voice.")

    @commands.command(name="skip")
    async def skip(self, ctx: commands.Context):
        if not await check_channel(ctx):
            return

        channel_id = ctx.channel.id
        # Cancel the existing auto-next task and invalidate its generation
        if self.bot._auto_next_task and not self.bot._auto_next_task.done():
            self.bot._auto_next_task.cancel()
            self.bot._auto_next_task = None
        self.bot._auto_next_gen = getattr(self.bot, '_auto_next_gen', 0) + 1
        self.bot.player.stop_playback()
        next_track = self.bot.queue.next()
        if next_track:
            try:
                title = await self.bot.player.play(next_track.query)
                next_track.title = title
                embed = create_np_embed(self.bot, title)
                await ctx.send("Skipped.", embed=embed)
                await update_channel_topic(self.bot, channel_id, f"▶️ Now playing: {title}")
                _start_auto_next(self.bot, channel_id)
            except Exception as e:
                await ctx.send(f"Error playing next track: {e}")
        else:
            await update_channel_topic(self.bot, channel_id, "Queue is empty.")
            await ctx.send("Skipped. Queue is empty.")

    @commands.command(name="queue")
    async def queue(self, ctx: commands.Context):
        if not await check_channel(ctx):
            return

        tracks = self.bot.queue.list()
        if not tracks:
            msg = "Queue is empty."
            if self.bot.player.current_track_title:
                msg = f"Now playing: **{self.bot.player.current_track_title}**\nQueue is empty."
        else:
            lines = []
            if self.bot.player.current_track_title:
                lines.append(f"Now playing: **{self.bot.player.current_track_title}**")
            for i, t in enumerate(tracks, 1):
                lines.append(f"{i}. {t.query}")
            msg = "\n".join(lines)
        await ctx.send(msg)

    @commands.command(name="loadall")
    async def loadall(self, ctx: commands.Context):
        """Load remaining playlist tracks from the most recent pending playlist."""
        channel_id = ctx.channel.id
        msg_id = self.bot.pending_playlists.get(f"channel_{channel_id}")
        if not msg_id:
            await ctx.send("No pending playlist to load.")
            return

        if not await check_channel(ctx):
            return

        pending = self.bot.pending_playlists.pop(msg_id, None)
        self.bot.pending_playlists.pop(f"channel_{channel_id}", None)
        if not pending:
            await ctx.send("No pending playlist to load.")
            return

        tracks = pending["tracks"]
        user_id = str(ctx.author.id)

        for t in tracks:
            track = Track(query=t["url"], title=t["title"], requested_by=user_id)
            self.bot.queue.add(track)

        await ctx.send(f"📋 Added **{len(tracks)}** tracks to the queue.")

        # Start playback if nothing is currently playing
        if not self.bot.player.is_playing:
            next_track = self.bot.queue.next()
            if next_track:
                try:
                    title = await self.bot.player.play(next_track.query)
                    next_track.title = title
                    embed = create_np_embed(self.bot, title)
                    await ctx.send(embed=embed)
                    await update_channel_topic(self.bot, channel_id, f"▶️ Now playing: {title}")
                    _start_auto_next(self.bot, channel_id)
                except Exception as e:
                    await ctx.send(f"Error playing track: {e}")

    @commands.command(name="bitrate")
    async def bitrate(self, ctx: commands.Context, kbps: str = None):
        if not await check_channel(ctx):
            return

        current_kbps = self.bot.player._audio_bitrate // 1000

        if not kbps:
            await ctx.send(f"Current audio bitrate: **{current_kbps} kbps**. Usage: `{self.bot.command_prefix}bitrate <1-512>` (higher values may improve quality)")
            return

        try:
            kbps_int = int(kbps)
        except ValueError:
            await ctx.send(f"Invalid value — provide a number. Usage: `{self.bot.command_prefix}bitrate <kbps>`")
            return

        if kbps_int > 512:
            await ctx.send(f"**{kbps_int} kbps** is too high. Max is **512 kbps**. Try `{self.bot.command_prefix}bitrate 128` for standard or `{self.bot.command_prefix}bitrate 384` for boosted servers.")
            return

        if kbps_int < 1:
            await ctx.send("Bitrate must be at least 1 kbps.")
            return

        await self.bot.player.set_bitrate(kbps_int)
        # Persist per-guild
        if ctx.guild:
            set_bitrate(str(ctx.guild.id), kbps_int)
        await ctx.send(f"Audio bitrate set to **{kbps_int} kbps** (saved).")

    @commands.command(name="shutdown")
    async def shutdown(self, ctx: commands.Context):
        if not await check_channel(ctx):
            return

        owner_id = str(self.bot.config.get("owner_id", ""))
        if str(ctx.author.id) != owner_id:
            await ctx.send("Only the bot owner can use this command.")
            return

        await ctx.send("Shutting down...")
        if self.bot._auto_next_task and not self.bot._auto_next_task.done():
            self.bot._auto_next_task.cancel()
            self.bot._auto_next_task = None
        self.bot.player.stop_playback()
        self.bot.queue.clear()
        await self.bot.player.disconnect()
        await update_channel_topic(self.bot, ctx.channel.id, "Queue is empty.")
        print("[main] Shutdown requested via command.")
        import os
        os._exit(0)

    @commands.command(name="help")
    async def help_cmd(self, ctx: commands.Context):
        p = self.bot.command_prefix
        await ctx.send(
            f"**Available commands:**\n"
            f"`{p}play <url or search>` — Play a track or playlist (join voice first)\n"
            f"`{p}pause` — Pause playback\n"
            f"`{p}resume` — Resume paused playback\n"
            f"`{p}skip` — Skip the current track\n"
            f"`{p}stop` — Stop playback, clear queue, and leave voice\n"
            f"`{p}queue` — Show the current queue\n"
            f"`{p}loadall` — Load all remaining tracks from the last pending playlist\n"
            f"`{p}bitrate [kbps]` — Show or set audio encoding bitrate\n"
            f"`{p}settc` — Restrict bot commands to this channel *(owner only)*\n"
            f"`{p}shutdown` — Shut down the bot *(owner only)*"
        )

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        """Handle playlist confirmation reactions."""
        if str(payload.emoji) != PLAYLIST_EMOJI:
            return

        # Ignore bot's own reaction
        if payload.user_id == self.bot.user.id:
            return

        message_id = str(payload.message_id)
        pending = self.bot.pending_playlists.get(message_id)
        if not pending:
            return

        # Remove from pending so it can't be triggered twice
        self.bot.pending_playlists.pop(message_id, None)
        channel_id = pending["channel_id"]
        if self.bot.pending_playlists.get(f"channel_{channel_id}") == message_id:
            self.bot.pending_playlists.pop(f"channel_{channel_id}", None)

        tracks = pending["tracks"]
        if not tracks:
            channel = self.bot.get_channel(channel_id)
            if channel:
                await channel.send("No remaining tracks to load.")
            return

        # Enqueue all remaining tracks
        for t in tracks:
            track = Track(query=t["url"], title=t["title"], requested_by=str(payload.user_id))
            self.bot.queue.add(track)

        channel = self.bot.get_channel(channel_id)
        if channel:
            await channel.send(f"📋 Added **{len(tracks)}** tracks to the queue.")


def _start_auto_next(bot, channel_id):
    """Cancel any existing auto-next chain and start a fresh one."""
    bot.current_text_channel_id = channel_id
    if bot._auto_next_task and not bot._auto_next_task.done():
        bot._auto_next_task.cancel()
    # Increment generation so any surviving zombie tasks self-terminate
    gen = getattr(bot, '_auto_next_gen', 0) + 1
    bot._auto_next_gen = gen
    bot._auto_next_task = asyncio.create_task(_auto_next(bot, channel_id, gen))


async def _auto_next(bot, channel_id, generation):
    """Wait for current track to end, then play next in queue."""
    consecutive_errors = 0
    MAX_CONSECUTIVE_ERRORS = 3
    try:
        while True:
            # If a newer auto-next was started, this one is a zombie — exit
            if getattr(bot, '_auto_next_gen', 0) != generation:
                return
            await bot.player.wait_for_playback()
            # Check again after waking up
            if getattr(bot, '_auto_next_gen', 0) != generation:
                return
            if bot.player.is_playing:
                break  # something else started playing
            next_track = bot.queue.next()
            if not next_track:
                await update_channel_topic(bot, channel_id, "Queue is empty.")
                break  # queue empty
            try:
                title = await bot.player.play(next_track.query)
                next_track.title = title
                consecutive_errors = 0  # reset on success
                embed = create_np_embed(bot, title)
                channel = bot.get_channel(channel_id)
                if channel:
                    await channel.send(embed=embed)
                await update_channel_topic(bot, channel_id, f"▶️ Now playing: {title}")
            except Exception as e:
                consecutive_errors += 1
                channel = bot.get_channel(channel_id)
                if channel:
                    await channel.send(f"Error playing track, skipping: {e}")
                if consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                    if channel:
                        await channel.send(f"Too many consecutive errors ({MAX_CONSECUTIVE_ERRORS}), stopping auto-play.")
                    break
                continue  # try the next track instead of dying

        # Queue drained — leave if channel is empty
        voice_client = None
        for vc in bot.voice_clients:
            if vc.guild and vc.guild.id == getattr(bot, '_current_guild_id', None):
                voice_client = vc
                break

        if voice_client and voice_client.is_connected() and getattr(bot, '_auto_next_gen', 0) == generation:
            # Count non-bot members in the voice channel
            members = [m for m in voice_client.channel.members if not m.bot]
            if not members:
                bot.player.stop_playback()
                await voice_client.disconnect()
                bot.player._voice_client = None
                channel = bot.get_channel(channel_id)
                if channel:
                    await channel.send("Queue finished and no one is in the voice channel. Leaving.")
    except asyncio.CancelledError:
        pass  # chain cancelled by _start_auto_next or !stop


async def setup(bot):
    await bot.add_cog(MusicCog(bot))
