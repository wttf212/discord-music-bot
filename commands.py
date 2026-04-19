import asyncio
import discord
from discord.ext import commands
from track_queue import Track
from audio_player import is_playlist_url, extract_playlist_info, get_audio_url_with_retry
from guild_settings import get_allowed_channel, set_allowed_channel, get_bitrate, set_bitrate, get_admins, add_admin, remove_admin


PLAYLIST_EMOJI = "\u2705"  # ✅


def create_np_embed(bot, title: str, extra_desc: str = "",
                    thumbnail: str = "", url: str = "",
                    requester_name: str = "",
                    queue_tracks: list | None = None,
                    guild_id: int | None = None) -> discord.Embed:
    """Creates an embed for Now Playing with thumbnail, link, and queue preview."""
    gs = bot.get_guild_state(guild_id) if guild_id else None
    kbps = gs.player.get_bitrate_for_guild(guild_id) // 1000 if gs else bot.config.get("audio", {}).get("bitrate", 128)
    p = bot.command_prefix

    # Make the title a clickable link if we have a URL
    if url:
        desc = f"**[{title}]({url})**"
    else:
        desc = f"**{title}**"
    if requester_name:
        desc += f"\n*Requested by {requester_name}*"
    if extra_desc:
        desc += f"\n\n{extra_desc}"

    # Force the embed to be wider using an invisible spacer line
    desc += "\n\n" + "⠀" * 45

    embed = discord.Embed(
        title="▶️ Now Playing",
        description=desc,
        color=0x3498db,
    )

    # Set thumbnail from the track
    if thumbnail:
        embed.set_thumbnail(url=thumbnail)

    # Add queue preview (next 5 songs)
    if queue_tracks:
        lines = []
        for i, t in enumerate(queue_tracks[:5], 1):
            req_tag = ""
            if t.requested_by:
                req_tag = f" — *<@{t.requested_by}>*"
            if t.url:
                lines.append(f"`{i}.` [{t.title}]({t.url}){req_tag}")
            else:
                lines.append(f"`{i}.` {t.title}{req_tag}")
        remaining = (len(gs.queue.list()) - 5) if gs else (len(queue_tracks) - 5)
        if remaining > 0:
            lines.append(f"*...and {remaining} more*")
        embed.add_field(name="Up Next", value="\n".join(lines), inline=False)
    else:
        embed.add_field(name="Up Next", value="*No songs in queue*", inline=False)

    embed.set_footer(text=f"Audio: {kbps} kbps • {p}bitrate <kbps> to change")
    return embed


def _get_requester_name(bot, user_id: str) -> str:
    """Resolve a user ID to their mention."""
    if not user_id:
        return ""
    return f"<@{user_id}>"


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


async def _do_update_np_embed(bot, channel, msg_id, embed):
    """Helper to update NP embed in a separate task."""
    try:
        msg = await channel.fetch_message(msg_id)
        view = _create_player_controls(bot, channel.id)
        await msg.edit(embed=embed, view=view)
    except Exception as e:
        print(f"[commands] Failed to update NP embed: {e}")

async def update_np_embed(bot, channel_id: int, embed: discord.Embed):
    """Edit the existing NP message embed in-place (no new message)."""
    channel = bot.get_channel(channel_id)
    if not channel or not hasattr(channel, 'guild') or not channel.guild:
        return
    guild_id = channel.guild.id
    gs = bot.get_guild_state(guild_id)
    msg_id = gs.np_message_id
    if not msg_id:
        return
    asyncio.create_task(_do_update_np_embed(bot, channel, msg_id, embed))


class LoadPlaylistButton(discord.ui.Button):
    def __init__(self, bot, channel_id):
        super().__init__(label="Load Playlist Tracks", style=discord.ButtonStyle.success, emoji="✅", custom_id="btn_load_playlist")
        self.bot = bot
        self.channel_id = channel_id

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        guild_id = interaction.guild_id
        gs = self.bot.get_guild_state(guild_id)
        pending = self.bot.pending_playlists.pop(str(self.channel_id), None)
        if not pending:
            await interaction.followup.send("Playlist already loaded or expired.", ephemeral=True)
            view = _create_player_controls(self.bot, self.channel_id)
            await interaction.message.edit(view=view)
            return

        tracks = pending["tracks"]
        user_id = str(interaction.user.id)

        for t in tracks:
            track = Track(query=t["url"], title=t["title"], requested_by=user_id, url=t["url"])
            gs.queue.add(track)

        current = gs.queue.current
        if current:
            embed = create_np_embed(self.bot, current.title,
                                    thumbnail=current.thumbnail,
                                    url=current.url,
                                    requester_name=f"<@{current.requested_by}>" if getattr(current, 'requested_by', None) else "",
                                    queue_tracks=gs.queue.preview_fair_order(),
                                    guild_id=guild_id)

            view = _create_player_controls(self.bot, self.channel_id)
            await interaction.message.edit(embed=embed, view=view)

            channel = self.bot.get_channel(self.channel_id)
            if channel:
                await channel.send(f"📋 Added **{len(tracks)}** tracks to the queue.")


async def _resolve_track_info(bot, channel_id: int, track: Track):
    """Silently resolve missing track metadata via yt-dlp and update the NP embed."""
    if track.url:  # Already resolved
        return
    try:
        yt_client = bot.config.get("youtube", {}).get("client", "web")
        cookies_file = bot.config.get("youtube", {}).get("cookies_file") or None
        info = await asyncio.get_event_loop().run_in_executor(
            None, get_audio_url_with_retry, track.query, yt_client, False, cookies_file
        )
        track.title = info["title"]
        track.thumbnail = info.get("thumbnail", "")
        track.url = info.get("webpage_url", "")

        _ch = bot.get_channel(channel_id)
        guild_id = _ch.guild.id if _ch and hasattr(_ch, 'guild') and _ch.guild else None
        if not guild_id:
            return
        gs = bot.get_guild_state(guild_id)
        current = gs.queue.current
        if current:
            requester_name = _get_requester_name(bot, current.requested_by)
            embed = create_np_embed(
                bot,
                current.title,
                thumbnail=current.thumbnail,
                url=current.url,
                requester_name=requester_name,
                queue_tracks=gs.queue.preview_fair_order(),
                guild_id=guild_id,
            )
            asyncio.create_task(update_np_embed(bot, channel_id, embed))
    except Exception as e:
        print(f"[commands] Failed background resolve for {track.query}: {e}")

async def send_new_np(bot, channel_id: int, embed: discord.Embed):
    channel = bot.get_channel(channel_id)
    if not channel or not hasattr(channel, 'guild') or not channel.guild:
        return
    guild_id = channel.guild.id
    gs = bot.get_guild_state(guild_id)

    # Reset votes whenever a new NP message is sent / track changes
    gs.prev_votes.clear()
    gs.playpause_votes.clear()
    gs.stop_votes.clear()
    gs.next_votes.clear()

    old_msg_id = gs.np_message_id
    if old_msg_id:
        try:
            old_msg = await channel.fetch_message(old_msg_id)
            await old_msg.delete()
        except Exception:
            pass

    view = _create_player_controls(bot, channel_id)
    try:
        new_msg = await channel.send(embed=embed, view=view)
        gs.np_message_id = new_msg.id
    except Exception as e:
        print(f"[commands] Failed to send NP message: {e}")

def _create_player_controls(bot, channel_id):
    view = PlayerControls(bot, channel_id)
    if str(channel_id) in bot.pending_playlists:
        view.add_item(LoadPlaylistButton(bot, channel_id))
    return view

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

        guild_id = interaction.guild_id
        gs = self.bot.get_guild_state(guild_id)
        current = gs.queue.current

        if action in ["next", "prev", "playpause"]:
            if current and current.requested_by == user_id:
                return True
        elif action == "stop":
            all_reqs = set()
            if current: all_reqs.add(current.requested_by)
            for t in gs.queue.list(): all_reqs.add(t.requested_by)
            if len(all_reqs) == 1 and user_id in all_reqs:
                return True

        # Voting required
        pct = gs.fairness_pct
        votes_set = getattr(gs, f"{action}_votes")
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
        if not await self.evaluate_vote(interaction, "prev"): return
        await interaction.response.defer()
        guild_id = interaction.guild_id
        gs = self.bot.get_guild_state(guild_id)
        prev_track = gs.queue.previous()
        if not prev_track:
            await interaction.followup.send("No previous track in history.", ephemeral=True)
            return

        if gs.auto_next_task and not gs.auto_next_task.done():
            gs.auto_next_task.cancel()
            gs.auto_next_task = None
        gs.auto_next_gen += 1
        gs.player.stop_playback()

        try:
            info = await gs.player.play(prev_track.query)
            title = info["title"]
            prev_track.title = title
            prev_track.thumbnail = info.get("thumbnail", "")
            prev_track.url = info.get("webpage_url", "")
            embed = create_np_embed(self.bot, title,
                                    thumbnail=prev_track.thumbnail,
                                    url=prev_track.url,
                                    requester_name=_get_requester_name(self.bot, prev_track.requested_by),
                                    queue_tracks=gs.queue.preview_fair_order(),
                                    guild_id=guild_id)
            await send_new_np(self.bot, self.channel_id, embed)
            _start_auto_next(self.bot, self.channel_id, guild_id)
        except Exception as e:
            await interaction.channel.send(f"Error playing previous track: {e}")

    @discord.ui.button(label="⏯️ Play/Pause", style=discord.ButtonStyle.primary, custom_id="btn_playpause")
    async def playpause_callback(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self.evaluate_vote(interaction, "playpause"): return
        guild_id = interaction.guild_id
        gs = self.bot.get_guild_state(guild_id)
        if gs.player.is_playing and not gs.player.is_paused:
            gs.player.pause()
        elif gs.player.is_paused:
            gs.player.resume()

        embed = interaction.message.embeds[0] if interaction.message.embeds else None
        if embed and gs.player.is_paused:
            embed.title = "⏸️ Paused"
        elif embed:
            embed.title = "▶️ Now Playing"

        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="⏹️ Stop", style=discord.ButtonStyle.danger, custom_id="btn_stop")
    async def stop_callback(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self.evaluate_vote(interaction, "stop"): return
        await interaction.response.defer()
        guild_id = interaction.guild_id
        gs = self.bot.get_guild_state(guild_id)
        if gs.auto_next_task and not gs.auto_next_task.done():
            gs.auto_next_task.cancel()
            gs.auto_next_task = None
        gs.player.stop_playback()
        gs.queue.clear()
        await gs.player.disconnect()

        for child in self.children:
            child.disabled = True
        await interaction.message.edit(view=self)

    @discord.ui.button(label="⏭️ Next", style=discord.ButtonStyle.secondary, custom_id="btn_next")
    async def next_callback(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self.evaluate_vote(interaction, "next"): return
        await interaction.response.defer()
        guild_id = interaction.guild_id
        gs = self.bot.get_guild_state(guild_id)
        if gs.auto_next_task and not gs.auto_next_task.done():
            gs.auto_next_task.cancel()
            gs.auto_next_task = None
        gs.auto_next_gen += 1
        gs.player.stop_playback()
        next_track = gs.queue.next()
        if next_track:
            try:
                info = await gs.player.play(next_track.query)
                title = info["title"]
                next_track.title = title
                next_track.thumbnail = info.get("thumbnail", "")
                next_track.url = info.get("webpage_url", "")
                embed = create_np_embed(self.bot, title,
                                        thumbnail=next_track.thumbnail,
                                        url=next_track.url,
                                        requester_name=_get_requester_name(self.bot, next_track.requested_by),
                                        queue_tracks=gs.queue.preview_fair_order(),
                                        guild_id=guild_id)
                await send_new_np(self.bot, self.channel_id, embed)
                _start_auto_next(self.bot, self.channel_id, guild_id)
            except Exception as e:
                await interaction.channel.send(f"Error playing next track: {e}")
        else:
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
        gs = self.bot.get_guild_state(ctx.guild.id)

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

            # Delete the user's command message to keep chat clean
            try:
                await ctx.message.delete()
            except Exception:
                pass

            # Check if audio is actually playing in this guild
            voice_actually_playing = (
                gs.player.is_playing
                and ctx.guild.voice_client
                and ctx.guild.voice_client.is_playing()
            )

            channel_id = ctx.channel.id
            user_id = str(ctx.author.id)

            if voice_actually_playing:
                # Add the first track to the queue silently
                track = Track(query=first_track_info["url"], title=first_track_info["title"], requested_by=user_id, url=first_track_info["url"])
                gs.queue.add(track)

                try:
                    await ctx.message.delete()
                except Exception:
                    pass

                count = len(remaining_tracks)
                if count > 0:
                    extra = (
                        f"📋 **{playlist_title}** has **{count}** more tracks.\n"
                        f"Click 'Load Playlist Tracks' to add them to the queue."
                    )
                else:
                    extra = f"📋 Added **{playlist_title}** (1 track) to the queue."

                current = gs.queue.current
                if current:
                    embed = create_np_embed(self.bot, current.title, extra,
                                            thumbnail=current.thumbnail,
                                            url=current.url,
                                            requester_name=f"<@{current.requested_by}>" if getattr(current, 'requested_by', None) else "",
                                            queue_tracks=gs.queue.preview_fair_order(),
                                            guild_id=ctx.guild.id)
                    await status_msg.delete()
                    await update_np_embed(self.bot, channel_id, embed)

            else:
                # Join voice if not already connected
                voice_client = ctx.guild.voice_client
                if not voice_client or not voice_client.is_connected():
                    try:
                        voice_client = await voice_channel.connect()
                        gs.player.set_voice_client(voice_client)
                        # Restore saved bitrate for this guild
                        saved_br = get_bitrate(guild_id)
                        if saved_br:
                            await gs.player.set_bitrate(ctx.guild.id, saved_br)
                    except Exception as e:
                        await status_msg.edit(content=f"Failed to join voice channel: {e}")
                        return

                # Play the first track immediately
                try:
                    info = await gs.player.play(first_track_info["url"])
                    title = info["title"]
                    track_thumbnail = info.get("thumbnail", "")
                    track_url = info.get("webpage_url", "")

                    # Explicitly set the current track state since we bypassed queueing
                    track = Track(query=first_track_info["url"], title=title, requested_by=user_id, thumbnail=track_thumbnail, url=track_url)
                    gs.queue.current = track
                except Exception as e:
                    await status_msg.edit(content=f"Error playing first track: {e}")
                    return

                if not remaining_tracks:
                    embed = create_np_embed(self.bot, title,
                                            f"From playlist: **{playlist_title}**",
                                            thumbnail=track_thumbnail,
                                            url=track_url,
                                            requester_name=f"<@{user_id}>",
                                            queue_tracks=gs.queue.preview_fair_order(),
                                            guild_id=ctx.guild.id)
                    await status_msg.delete()
                    await send_new_np(self.bot, channel_id, embed)
                    _start_auto_next(self.bot, channel_id, ctx.guild.id)
                    return

                # Show offer to load the rest
                count = len(remaining_tracks)
                extra = (
                    f"📋 **{playlist_title}** has **{count}** more tracks.\n"
                    f"Click 'Load Playlist Tracks' to add them to the queue."
                )
                embed = create_np_embed(self.bot, title, extra,
                                        thumbnail=track_thumbnail,
                                        url=track_url,
                                        requester_name=f"<@{user_id}>",
                                        queue_tracks=gs.queue.preview_fair_order(),
                                        guild_id=ctx.guild.id)
                await status_msg.delete()
                await send_new_np(self.bot, channel_id, embed)

            # Store pending playlist
            self.bot.pending_playlists[str(channel_id)] = {
                "query": query,
                "user_id": str(ctx.author.id),
                "guild_id": guild_id,
                "channel_id": channel_id,
                "tracks": remaining_tracks,
                "playlist_title": playlist_title,
            }

            if voice_actually_playing:
                await update_np_embed(self.bot, channel_id, embed)
            else:
                await send_new_np(self.bot, channel_id, embed)
                _start_auto_next(self.bot, channel_id, ctx.guild.id)

            # Auto-expire after 120 seconds
            async def _expire_playlist(ch_id: int):
                await asyncio.sleep(120)
                removed = self.bot.pending_playlists.pop(str(ch_id), None)
                if removed:
                    try:
                        expired_extra = (
                            f"📋 **{playlist_title}** had **{count}** more tracks.\n"
                            f"~~Click Load Playlist Tracks~~ *(expired)*"
                        )
                        expired_embed = create_np_embed(self.bot, title, expired_extra, guild_id=ctx.guild.id)
                        await update_np_embed(self.bot, ch_id, expired_embed)
                    except Exception:
                        pass

            asyncio.create_task(_expire_playlist(channel_id))
            return

        # ---------------------------------------------------------------
        # Single track flow
        # ---------------------------------------------------------------

        # Join voice if not already connected
        voice_client = ctx.guild.voice_client
        if not voice_client or not voice_client.is_connected():
            try:
                voice_client = await voice_channel.connect()
                gs.player.set_voice_client(voice_client)
                # Restore saved bitrate for this guild
                saved_br = get_bitrate(guild_id)
                if saved_br:
                    await gs.player.set_bitrate(ctx.guild.id, saved_br)
            except Exception as e:
                await ctx.send(f"Failed to join voice channel: {e}")
                return

        user_id = str(ctx.author.id)
        channel_id = ctx.channel.id

        # Check if audio is actually playing (verify with voice client, not just the flag)
        voice_actually_playing = (
            gs.player.is_playing
            and ctx.guild.voice_client
            and ctx.guild.voice_client.is_playing()
        )

        if voice_actually_playing:
            track = Track(query=query, title=query, requested_by=user_id)
            gs.queue.add(track)

            # Start background task to fetch actual title/thumbnail
            asyncio.create_task(_resolve_track_info(self.bot, channel_id, track))

            # Delete the user's command message to keep chat clean
            try:
                await ctx.message.delete()
            except Exception:
                pass

            # Rebuild and update the existing NP embed with the new queue
            current = gs.queue.current
            if current:
                requester_name = f"<@{current.requested_by}>" if getattr(current, 'requested_by', None) else ""
                embed = create_np_embed(
                    self.bot,
                    current.title,
                    thumbnail=current.thumbnail,
                    url=current.url,
                    requester_name=requester_name,
                    queue_tracks=gs.queue.preview_fair_order(),
                    guild_id=ctx.guild.id,
                )
                await update_np_embed(self.bot, channel_id, embed)
        else:
            # Delete the user's command message to keep chat clean
            try:
                await ctx.message.delete()
            except Exception:
                pass
            status_msg = await ctx.send("▶️ Resolving...")
            try:
                info = await gs.player.play(query)
                title = info["title"]
                track_thumbnail = info.get("thumbnail", "")
                track_url = info.get("webpage_url", "")

                # Store as current track so queue-add updates can reference it
                gs.queue.current = Track(
                    query=query, title=title, requested_by=user_id,
                    thumbnail=track_thumbnail, url=track_url,
                )

                requester_name = f"<@{user_id}>"

                embed = create_np_embed(self.bot, title,
                                        thumbnail=track_thumbnail,
                                        url=track_url,
                                        requester_name=requester_name,
                                        queue_tracks=gs.queue.preview_fair_order(),
                                        guild_id=ctx.guild.id)
                await status_msg.delete()
                await send_new_np(self.bot, channel_id, embed)
                _start_auto_next(self.bot, channel_id, ctx.guild.id)
            except Exception as e:
                await status_msg.edit(content=f"Error playing track: {e}")

    @commands.command(name="pause")
    async def pause(self, ctx: commands.Context):
        if not await check_channel(ctx):
            return
        if not ctx.guild:
            return
        gs = self.bot.get_guild_state(ctx.guild.id)
        if not gs.player.is_playing:
            await ctx.send("Nothing is playing.")
            return
        if gs.player.is_paused:
            await ctx.send("Already paused.")
            return
        gs.player.pause()
        await ctx.send(f"Paused: **{gs.player.current_track_title}**")

    @commands.command(name="resume")
    async def resume(self, ctx: commands.Context):
        if not await check_channel(ctx):
            return
        if not ctx.guild:
            return
        gs = self.bot.get_guild_state(ctx.guild.id)
        if not gs.player.is_paused:
            await ctx.send("Not paused.")
            return
        gs.player.resume()
        await ctx.send(f"Resumed: **{gs.player.current_track_title}**")

    @commands.command(name="stop")
    async def stop(self, ctx: commands.Context):
        if not await check_channel(ctx):
            return
        if not ctx.guild:
            return
        gs = self.bot.get_guild_state(ctx.guild.id)
        gs.player.stop_playback()
        gs.queue.clear()
        await gs.player.disconnect()

        await ctx.send("Stopped playback and left voice.")

    @commands.command(name="skip")
    async def skip(self, ctx: commands.Context):
        if not await check_channel(ctx):
            return
        if not ctx.guild:
            return

        gs = self.bot.get_guild_state(ctx.guild.id)
        channel_id = ctx.channel.id
        # Cancel the existing auto-next task and invalidate its generation
        if gs.auto_next_task and not gs.auto_next_task.done():
            gs.auto_next_task.cancel()
            gs.auto_next_task = None
        gs.auto_next_gen += 1
        gs.player.stop_playback()
        next_track = gs.queue.next()
        if next_track:
            try:
                info = await gs.player.play(next_track.query)
                title = info["title"]
                next_track.title = title
                next_track.thumbnail = info.get("thumbnail", "")
                next_track.url = info.get("webpage_url", "")
                embed = create_np_embed(self.bot, title,
                                        thumbnail=next_track.thumbnail,
                                        url=next_track.url,
                                        requester_name=_get_requester_name(self.bot, next_track.requested_by),
                                        queue_tracks=gs.queue.preview_fair_order(),
                                        guild_id=ctx.guild.id)
                await ctx.send("Skipped.", delete_after=3)
                await send_new_np(self.bot, channel_id, embed)
                _start_auto_next(self.bot, channel_id, ctx.guild.id)
            except Exception as e:
                await ctx.send(f"Error playing next track: {e}")
        else:
            await ctx.send("Skipped. Queue is empty.")

    @commands.command(name="queue")
    async def queue(self, ctx: commands.Context):
        if not await check_channel(ctx):
            return
        if not ctx.guild:
            return

        gs = self.bot.get_guild_state(ctx.guild.id)
        tracks = gs.queue.list()
        if not tracks:
            msg = "Queue is empty."
            if gs.player.current_track_title:
                msg = f"Now playing: **{gs.player.current_track_title}**\nQueue is empty."
        else:
            lines = []
            if gs.player.current_track_title:
                lines.append(f"Now playing: **{gs.player.current_track_title}**")
            for i, t in enumerate(tracks, 1):
                req_tag = ""
                if t.requested_by:
                    req_tag = f" — *<@{t.requested_by}>*"
                lines.append(f"{i}. {t.title}{req_tag}")
            msg = "\n".join(lines)
        await ctx.send(msg)

    @commands.command(name="loadall")
    async def loadall(self, ctx: commands.Context):
        """Load remaining playlist tracks from the most recent pending playlist."""
        channel_id = ctx.channel.id
        pending = self.bot.pending_playlists.pop(str(channel_id), None)

        if not pending:
            await ctx.send("No pending playlist to load.")
            return

        if not await check_channel(ctx):
            return
        if not ctx.guild:
            return

        gs = self.bot.get_guild_state(ctx.guild.id)
        tracks = pending["tracks"]
        user_id = str(ctx.author.id)

        for t in tracks:
            track = Track(query=t["url"], title=t["title"], requested_by=user_id, url=t["url"])
            gs.queue.add(track)

        await ctx.send(f"📋 Added **{len(tracks)}** tracks to the queue.")

        # Start playback if nothing is currently playing
        if not gs.player.is_playing:
            next_track = gs.queue.next()
            if next_track:
                try:
                    info = await gs.player.play(next_track.query)
                    title = info["title"]
                    next_track.title = title
                    next_track.thumbnail = info.get("thumbnail", "")
                    next_track.url = info.get("webpage_url", "")
                    embed = create_np_embed(self.bot, title,
                                            thumbnail=next_track.thumbnail,
                                            url=next_track.url,
                                            requester_name=f"<@{next_track.requested_by}>",
                                            queue_tracks=gs.queue.preview_fair_order(),
                                            guild_id=ctx.guild.id)
                    await send_new_np(self.bot, channel_id, embed)
                    _start_auto_next(self.bot, channel_id, ctx.guild.id)
                except Exception as e:
                    await ctx.send(f"Error playing track: {e}")

    @commands.command(name="bitrate")
    async def bitrate(self, ctx: commands.Context, kbps: str = None):
        if not await check_channel(ctx):
            return
        if not ctx.guild:
            return

        gs = self.bot.get_guild_state(ctx.guild.id)
        current_kbps = gs.player.get_bitrate_for_guild(ctx.guild.id) // 1000

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

        await gs.player.set_bitrate(ctx.guild.id, kbps_int)
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
        for gs in list(self.bot._guild_states.values()):
            if gs.auto_next_task and not gs.auto_next_task.done():
                gs.auto_next_task.cancel()
            gs.player.stop_playback()
            gs.queue.clear()
        for vc in list(self.bot.voice_clients):
            try:
                await vc.disconnect()
            except Exception:
                pass

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
        if not ctx.guild:
            return

        gs = self.bot.get_guild_state(ctx.guild.id)
        if toggle and toggle.lower() in ["off", "false", "0"]:
            gs.queue.fair_play = False
            await ctx.send("Fair play mode disabled (FIFO queue).")
        elif toggle and toggle.lower() in ["on", "true", "1"]:
            gs.queue.fair_play = True
            await ctx.send("Fair play mode enabled (queued songs will alternate users).")
        else:
            current = "enabled" if getattr(gs.queue, "fair_play", True) else "disabled"
            await ctx.send(f"Fair play mode is currently **{current}**. Toggle with `{self.bot.command_prefix}fairplay on|off`.")

    @commands.command(name="fairness")
    async def fairness(self, ctx: commands.Context, pct: int | None = None):
        if not await check_channel(ctx): return
        if not await self._check_admin(ctx): return
        if not ctx.guild:
            return

        gs = self.bot.get_guild_state(ctx.guild.id)
        if pct is None:
            await ctx.send(f"Current fairness requirement: **{gs.fairness_pct}%** of voice channel members to vote skip/stop. Usage: `{self.bot.command_prefix}fairness <0-100>`")
            return

        if pct < 0 or pct > 100:
            await ctx.send("Fairness percentage must be between 0 and 100.")
            return

        gs.fairness_pct = pct
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

def _start_auto_next(bot, channel_id, guild_id):
    """Cancel any existing auto-next chain for this guild and start a fresh one."""
    gs = bot.get_guild_state(guild_id)
    gs.current_text_channel_id = channel_id
    if gs.auto_next_task and not gs.auto_next_task.done():
        gs.auto_next_task.cancel()
    gen = gs.auto_next_gen + 1
    gs.auto_next_gen = gen
    gs.auto_next_task = asyncio.create_task(_auto_next(bot, channel_id, guild_id, gen))


async def _auto_next(bot, channel_id, guild_id, generation):
    """Wait for current track to end, then play next in queue for this guild."""
    gs = bot.get_guild_state(guild_id)
    consecutive_errors = 0
    MAX_CONSECUTIVE_ERRORS = 3
    try:
        while True:
            # If a newer auto-next was started for this guild, this one is a zombie — exit
            if gs.auto_next_gen != generation:
                return
            await gs.player.wait_for_playback()
            # Check again after waking up
            if gs.auto_next_gen != generation:
                return
            if gs.player.is_playing:
                break  # something else started playing
            next_track = gs.queue.next()
            if not next_track:
                break  # queue empty
            try:
                info = await gs.player.play(next_track.query)
                title = info["title"]
                next_track.title = title
                next_track.thumbnail = info.get("thumbnail", "")
                next_track.url = info.get("webpage_url", "")
                consecutive_errors = 0  # reset on success
                embed = create_np_embed(bot, title,
                                        thumbnail=next_track.thumbnail,
                                        url=next_track.url,
                                        requester_name=_get_requester_name(bot, next_track.requested_by),
                                        queue_tracks=gs.queue.preview_fair_order(),
                                        guild_id=guild_id)
                await send_new_np(bot, channel_id, embed)
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
        guild = bot.get_guild(guild_id)
        voice_client = guild.voice_client if guild else None

        if voice_client and voice_client.is_connected() and gs.auto_next_gen == generation:
            # Count non-bot members in the voice channel
            members = [m for m in voice_client.channel.members if not m.bot]
            if not members:
                gs.player.stop_playback()
                await voice_client.disconnect()
                gs.player._voice_client = None

                # Reset fair play stats when leaving
                gs.queue.fair_play = True
                gs.fairness_pct = 50

                channel = bot.get_channel(channel_id)
                if channel:
                    await channel.send("Queue finished and no one is in the voice channel. Leaving.")
    except asyncio.CancelledError:
        pass  # chain cancelled by _start_auto_next or !stop


async def setup(bot):
    await bot.add_cog(MusicCog(bot))
