import asyncio
import discord
from discord.ext import commands
from track_queue import Track
from audio_player import is_playlist_url, extract_playlist_info
from guild_settings import get_allowed_channel, set_allowed_channel, get_bitrate, set_bitrate, get_admins, add_admin, remove_admin


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


async def _do_update_topic(channel, text):
    try:
        await channel.edit(topic=text)
    except discord.errors.HTTPException as e:
        status = getattr(e, "status", 0)
        if status != 429:
            print(f"[commands] Failed to update channel topic: {e}")

async def update_channel_topic(bot, channel_id: int, topic_text: str):
    """Updates the textual channel topic asynchronously to avoid blocking on rate limits."""
    channel = bot.get_channel(channel_id)
    if channel:
        asyncio.create_task(_do_update_topic(channel, topic_text))


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


async def send_new_np(bot, channel_id: int, embed: discord.Embed):
    # Reset votes whenever a new NP message is sent / track changes
    bot.prev_votes = set()
    bot.playpause_votes = set()
    bot.stop_votes = set()
    bot.next_votes = set()
    
    channel = bot.get_channel(channel_id)
    if not channel:
        return
        
    old_msg_id = getattr(bot, "np_message_id", None)
    if old_msg_id:
        try:
            old_msg = await channel.fetch_message(old_msg_id)
            await old_msg.delete()
        except Exception:
            pass

    view = PlayerControls(bot, channel_id)
    try:
        new_msg = await channel.send(embed=embed, view=view)
        bot.np_message_id = new_msg.id
    except Exception as e:
        print(f"[commands] Failed to send NP message: {e}")


class PlayerControls(discord.ui.View):
    def __init__(self, bot, channel_id):
        super().__init__(timeout=None)
        self.bot = bot
        self.channel_id = channel_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        guild_id = str(interaction.guild_id) if interaction.guild_id else None
        if not guild_id:
            return True
        allowed = get_allowed_channel(guild_id)
        if allowed and str(interaction.channel_id) != allowed:
            await interaction.response.send_message(f"Please use controls in <#{allowed}>.", ephemeral=True)
            return False
            
        bot_voice = interaction.guild.voice_client
        if not interaction.user.voice or not interaction.user.voice.channel:
            await interaction.response.send_message("You need to be in a voice channel to use controls.", ephemeral=True)
            return False
            
        if bot_voice and bot_voice.is_connected():
            if interaction.user.voice.channel != bot_voice.channel:
                await interaction.response.send_message("You must be in the same voice channel as the bot to use controls.", ephemeral=True)
                return False
                
        return True

    async def evaluate_vote(self, interaction: discord.Interaction, action: str) -> bool:
        user_id = str(interaction.user.id)
        owner_id = str(self.bot.config.get("owner_id", ""))
        if user_id == owner_id:
            return True
            
        bot_voice = interaction.guild.voice_client
        if not bot_voice or not bot_voice.channel:
            return True
            
        vc_members = [m for m in bot_voice.channel.members if not m.bot]
        total = len(vc_members)
        if total <= 1:
            return True
            
        current = self.bot.queue.current
        
        if action in ["next", "prev", "playpause"]:
            if current and current.requested_by == user_id:
                return True
        elif action == "stop":
            all_reqs = set()
            if current: all_reqs.add(current.requested_by)
            for t in self.bot.queue.list(): all_reqs.add(t.requested_by)
            if len(all_reqs) == 1 and user_id in all_reqs:
                return True
                
        # Voting required
        pct = getattr(self.bot, "fairness_pct", 50)
        attr = f"{action}_votes"
        if not hasattr(self.bot, attr):
            setattr(self.bot, attr, set())
        votes_set = getattr(self.bot, attr)
        
        votes_set.add(user_id)
        
        import math
        req = max(1, math.ceil((pct / 100.0) * total))
        
        if len(votes_set) >= req:
            votes_set.clear()
            return True
            
        await interaction.response.send_message(f"🗳️ `{action}` vote from {interaction.user.display_name} recorded! ({len(votes_set)}/{req} votes needed, fairness: {pct}%)", ephemeral=False)
        return False

    @discord.ui.button(label="⏮️ Prev", style=discord.ButtonStyle.secondary, custom_id="btn_prev")
    async def prev_callback(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        prev_track = self.bot.queue.previous()
        if not prev_track:
            await interaction.followup.send("No previous track in history.", ephemeral=True)
            return
            
        if self.bot._auto_next_task and not self.bot._auto_next_task.done():
            self.bot._auto_next_task.cancel()
            self.bot._auto_next_task = None
        self.bot._auto_next_gen = getattr(self.bot, '_auto_next_gen', 0) + 1
        self.bot.player.stop_playback()
        
        try:
            title = await self.bot.player.play(prev_track.query)
            prev_track.title = title
            embed = create_np_embed(self.bot, title)
            await send_new_np(self.bot, self.channel_id, embed)
            await update_channel_topic(self.bot, self.channel_id, f"▶️ Now playing: {title}")
            _start_auto_next(self.bot, self.channel_id)
        except Exception as e:
            await interaction.channel.send(f"Error playing previous track: {e}")

    @discord.ui.button(label="⏯️ Play/Pause", style=discord.ButtonStyle.primary, custom_id="btn_playpause")
    async def playpause_callback(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.bot.player.is_playing and not self.bot.player.is_paused:
            self.bot.player.pause()
        elif self.bot.player.is_paused:
            self.bot.player.resume()
        
        embed = interaction.message.embeds[0] if interaction.message.embeds else None
        if embed and getattr(self.bot.player, 'is_paused', False):
            embed.title = "⏸️ Paused"
        elif embed:
            embed.title = "▶️ Now Playing"
            
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="⏹️ Stop", style=discord.ButtonStyle.danger, custom_id="btn_stop")
    async def stop_callback(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        if self.bot._auto_next_task and not self.bot._auto_next_task.done():
            self.bot._auto_next_task.cancel()
            self.bot._auto_next_task = None
        self.bot.player.stop_playback()
        self.bot.queue.clear()
        await self.bot.player.disconnect()
        await update_channel_topic(self.bot, self.channel_id, "Queue is empty.")
        
        for child in self.children:
            child.disabled = True
        await interaction.message.edit(view=self)

    @discord.ui.button(label="⏭️ Next", style=discord.ButtonStyle.secondary, custom_id="btn_next")
    async def next_callback(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
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
                await send_new_np(self.bot, self.channel_id, embed)
                await update_channel_topic(self.bot, self.channel_id, f"▶️ Now playing: {title}")
                _start_auto_next(self.bot, self.channel_id)
            except Exception as e:
                await interaction.channel.send(f"Error playing next track: {e}")
        else:
            await update_channel_topic(self.bot, self.channel_id, "Queue is empty.")
            for child in self.children:
                child.disabled = True
            await interaction.message.edit(view=self)


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
                await status_msg.delete()
                await send_new_np(self.bot, channel_id, embed)
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
            await status_msg.delete()
            await send_new_np(self.bot, channel_id, embed)
            await update_channel_topic(self.bot, channel_id, f"▶️ Now playing: {title}")

            actual_msg_id = getattr(self.bot, "np_message_id", None)
            if actual_msg_id:
                try:
                    ch = self.bot.get_channel(channel_id)
                    actual_msg = await ch.fetch_message(actual_msg_id)
                    await actual_msg.add_reaction(PLAYLIST_EMOJI)
                except Exception as e:
                    print(f"[commands] Could not add reaction: {e}")

            # Store pending playlist for reaction or !loadall
            self.bot.pending_playlists[str(actual_msg_id)] = {
                "query": query,
                "user_id": str(ctx.author.id),
                "guild_id": guild_id,
                "channel_id": channel_id,
                "tracks": remaining_tracks,
                "playlist_title": playlist_title,
            }
            # Also store by channel for !loadall lookup
            self.bot.pending_playlists[f"channel_{channel_id}"] = str(actual_msg_id)

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
                        channel_obj = self.bot.get_channel(ch_id)
                        if channel_obj:
                            msg = await channel_obj.fetch_message(int(msg_id))
                            await msg.edit(embed=expired_embed)
                    except Exception:
                        pass

            asyncio.create_task(_expire_playlist(str(actual_msg_id), channel_id))
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
                await status_msg.delete()
                await send_new_np(self.bot, channel_id, embed)
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
                await ctx.send("Skipped.", delete_after=3)
                await send_new_np(self.bot, channel_id, embed)
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
                    await send_new_np(self.bot, channel_id, embed)
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
        await self.bot.close()  # gracefully close gateway so bot goes offline immediately
        import os
        os._exit(0)

    async def _check_admin(self, ctx: commands.Context) -> bool:
        """Returns True if user is bot owner or server admin, else False and sends a message."""
        user_id = str(ctx.author.id)
        owner_id = str(self.bot.config.get("owner_id", ""))
        if user_id == owner_id:
            return True
            
        if ctx.guild:
            admins = get_admins(str(ctx.guild.id))
            if user_id in admins:
                return True
                
        await ctx.send("You do not have permission to use this command (requires bot admin).")
        return False

    @commands.command(name="addadmin")
    async def addadmin(self, ctx: commands.Context, member: discord.Member):
        if not await check_channel(ctx): return
        owner_id = str(self.bot.config.get("owner_id", ""))
        if str(ctx.author.id) != owner_id:
            await ctx.send("Only the bot owner can use this command.")
            return
            
        if not ctx.guild:
            await ctx.send("This command must be used in a server.")
            return
            
        add_admin(str(ctx.guild.id), str(member.id))
        await ctx.send(f"Added {member.mention} as a bot admin for this server.")

    @commands.command(name="removeadmin")
    async def removeadmin(self, ctx: commands.Context, member: discord.Member):
        if not await check_channel(ctx): return
        owner_id = str(self.bot.config.get("owner_id", ""))
        if str(ctx.author.id) != owner_id:
            await ctx.send("Only the bot owner can use this command.")
            return
            
        if not ctx.guild:
            await ctx.send("This command must be used in a server.")
            return
            
        remove_admin(str(ctx.guild.id), str(member.id))
        await ctx.send(f"Removed {member.mention} as a bot admin for this server.")

    @commands.command(name="fairplay")
    async def fairplay(self, ctx: commands.Context, toggle: str | None = None):
        if not await check_channel(ctx): return
        if not await self._check_admin(ctx): return
        
        if toggle and toggle.lower() in ["off", "false", "0"]:
            self.bot.queue.fair_play = False
            await ctx.send("Fair play mode disabled (FIFO queue).")
        elif toggle and toggle.lower() in ["on", "true", "1"]:
            self.bot.queue.fair_play = True
            await ctx.send("Fair play mode enabled (queued songs will alternate users).")
        else:
            current = "enabled" if getattr(self.bot.queue, "fair_play", True) else "disabled"
            await ctx.send(f"Fair play mode is currently **{current}**. Toggle with `{self.bot.command_prefix}fairplay on|off`.")

    @commands.command(name="fairness")
    async def fairness(self, ctx: commands.Context, pct: int | None = None):
        if not await check_channel(ctx): return
        if not await self._check_admin(ctx): return
        
        if pct is None:
            current = getattr(self.bot, "fairness_pct", 50)
            await ctx.send(f"Current fairness requirement: **{current}%** of voice channel members to vote skip/stop. Usage: `{self.bot.command_prefix}fairness <0-100>`")
            return
            
        if pct < 0 or pct > 100:
            await ctx.send("Fairness percentage must be between 0 and 100.")
            return
            
        self.bot.fairness_pct = pct
        await ctx.send(f"Fairness requirement set to **{pct}%**.")

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
            f"`{p}fairplay on|off` — Toggle user interleaving mode for queues *(admin only)*\n"
            f"`{p}fairness <0-100>` — Set the percentage of users strictly needed to skip/stop songs *(admin only)*\n"
            f"`{p}addadmin @user` — Add a user as a bot admin for this server *(owner only)*\n"
            f"`{p}removeadmin @user` — Remove a user as a bot admin for this server *(owner only)*\n"
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
                await send_new_np(bot, channel_id, embed)
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
                
                # Reset fair play stats when leaving
                bot.queue.fair_play = True
                bot.fairness_pct = 50
                
                channel = bot.get_channel(channel_id)
                if channel:
                    await channel.send("Queue finished and no one is in the voice channel. Leaving.")
    except asyncio.CancelledError:
        pass  # chain cancelled by _start_auto_next or !stop


async def setup(bot):
    await bot.add_cog(MusicCog(bot))
