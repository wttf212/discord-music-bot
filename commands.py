import asyncio
import json
import re
import urllib.parse
import urllib.request
import discord
import yt_dlp
from discord.ext import commands
from discord import app_commands
from track_queue import Track
from audio_player import is_playlist_url, extract_playlist_info, get_audio_url_with_retry
from guild_settings import (
    get_allowed_channel, set_allowed_channel,
    get_bitrate, set_bitrate,
    get_admins, add_admin, remove_admin,
    get_eq_bass, set_eq_bass, get_eq_treble, set_eq_treble,
    EQ_PRESETS, get_eq_preset_name,
)


PLAYLIST_EMOJI = "\u2705"  # ✅



def _fmt_duration(seconds):
    """Convert seconds (int|float|None) to 'mm:ss' string. None -> '?'."""
    if seconds is None:
        return "?"
    s = int(seconds)
    return f"{s // 60}:{s % 60:02d}"


def _is_search_query(query: str) -> bool:
    """True if query should go through the search picker (plain text, including ytsearch: prefix).
    False for direct http://, https:// URLs (case-insensitive)."""
    if not query:
        return False
    lowered = query.lower()
    return not (lowered.startswith("http://") or lowered.startswith("https://"))


def _friendly_ytdlp_error(exc: Exception) -> str:
    """Return a short, user-readable error message from a yt-dlp exception.

    yt-dlp errors look like:
      ERROR: [youtube] <id>: <human reason>. Use --cookies... See https://...
    We want only the human reason, without the noisy CLI suggestions.
    """
    msg = str(exc)
    msg = re.sub(r"^ERROR:\s*\[\w+\]\s*[^:]*:\s*", "", msg, count=1)
    # Cut off at the first "Use --" or "See http" instruction
    for cutoff in (" Use --", " See http", "\nUse --", "\nSee http"):
        idx = msg.find(cutoff)
        if idx != -1:
            msg = msg[:idx]
    return msg.strip() or "Unknown error"


def _strip_ytsearch_prefix(query: str) -> str:
    """Strip a leading 'ytsearch:' prefix (exact, lowercase) so the embed title shows the
    underlying query. 'ytsearch5:' and other variants are NOT stripped."""
    if query.startswith("ytsearch:"):
        return query[len("ytsearch:"):]
    return query


def _search_youtube(query: str) -> list[dict]:
    """Run ytsearch5 with extract_flat (1.3s vs 7.4s) and return normalized result dicts.
    BLOCKS: must be called via asyncio.run_in_executor.
    Returns up to 5 items shaped {title, url, uploader, duration_str, thumbnail}."""
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": "in_playlist",
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(f"ytsearch5:{query}", download=False)
    entries = (info or {}).get("entries") or []
    results = []
    for e in entries:
        if not e:
            continue
        duration_s = e.get("duration")
        duration_str = _fmt_duration(duration_s)
        thumbnails = e.get("thumbnails") or []
        thumbnail_url = thumbnails[0].get("url", "") if thumbnails else ""
        results.append({
            "title": e.get("title") or "Unknown",
            "url": e.get("url") or "",                     # NOT webpage_url -- None in flat mode
            "uploader": e.get("uploader") or e.get("channel") or "Unknown",
            "duration_str": duration_str,
            "thumbnail": thumbnail_url,
        })
    return results


def _build_search_embed(query: str, results: list[dict]) -> discord.Embed:
    """Build the picker embed. Title shows query truncated at 50 chars; one numbered line
    per result with channel + duration on the indented sub-line. Color matches create_np_embed."""
    query_display = query[:50]
    lines = []
    for i, r in enumerate(results, 1):
        lines.append(f"**{i}.** {r['title']}")
        lines.append(f"    {r['uploader']} • {r['duration_str']}")
    embed = discord.Embed(
        title=f"🔍 Results for \"{query_display}\"",
        description="\n".join(lines) if lines else "No results.",
        color=0x3498db,
    )
    embed.set_footer(text="Select a result below • Expires in 60s")
    return embed


_RADIO_REGIONS = [
    ("🌐 Worldwide", "worldwide"),
    ("🇪🇺 Northern Europe", "europe_north"),
    ("🇪🇺 Western Europe", "europe_west"),
    ("🇪🇺 Southern Europe", "europe_south"),
    ("🇪🇺 Eastern Europe", "europe_east"),
    ("🌎 Americas", "americas"),
    ("🌏 Asia & Pacific", "asia_pacific"),
    ("🌍 Middle East & Africa", "mideast_africa"),
]

_RADIO_REGION_COUNTRIES: dict[str, list[tuple[str, str]]] = {
    "worldwide": [("Any country", "any_country")],
    "europe_north": [
        ("Any", "any_country"),
        ("Denmark", "DK"), ("Estonia", "EE"), ("Finland", "FI"),
        ("Iceland", "IS"), ("Latvia", "LV"), ("Lithuania", "LT"),
        ("Norway", "NO"), ("Sweden", "SE"),
    ],
    "europe_west": [
        ("Any", "any_country"),
        ("Austria", "AT"), ("Belgium", "BE"), ("France", "FR"),
        ("Germany", "DE"), ("Ireland", "IE"), ("Luxembourg", "LU"),
        ("Netherlands", "NL"), ("Switzerland", "CH"), ("United Kingdom", "GB"),
    ],
    "europe_south": [
        ("Any", "any_country"),
        ("Albania", "AL"), ("Bosnia & Herzegovina", "BA"), ("Croatia", "HR"),
        ("Cyprus", "CY"), ("Greece", "GR"), ("Italy", "IT"),
        ("Malta", "MT"), ("Portugal", "PT"), ("Serbia", "RS"),
        ("Slovenia", "SI"), ("Spain", "ES"),
    ],
    "europe_east": [
        ("Any", "any_country"),
        ("Belarus", "BY"), ("Bulgaria", "BG"), ("Czech Republic", "CZ"),
        ("Hungary", "HU"), ("Moldova", "MD"), ("Poland", "PL"),
        ("Romania", "RO"), ("Russia", "RU"), ("Slovakia", "SK"),
        ("Ukraine", "UA"),
    ],
    "americas": [
        ("Any", "any_country"),
        ("Argentina", "AR"), ("Bolivia", "BO"), ("Brazil", "BR"),
        ("Canada", "CA"), ("Chile", "CL"), ("Colombia", "CO"),
        ("Ecuador", "EC"), ("Mexico", "MX"), ("Paraguay", "PY"),
        ("Peru", "PE"), ("United States", "US"), ("Uruguay", "UY"),
        ("Venezuela", "VE"),
    ],
    "asia_pacific": [
        ("Any", "any_country"),
        ("Australia", "AU"), ("China", "CN"), ("India", "IN"),
        ("Indonesia", "ID"), ("Japan", "JP"), ("Malaysia", "MY"),
        ("New Zealand", "NZ"), ("Philippines", "PH"), ("Singapore", "SG"),
        ("South Korea", "KR"), ("Thailand", "TH"), ("Vietnam", "VN"),
    ],
    "mideast_africa": [
        ("Any", "any_country"),
        ("Egypt", "EG"), ("Ethiopia", "ET"), ("Ghana", "GH"),
        ("Israel", "IL"), ("Kenya", "KE"), ("Morocco", "MA"),
        ("Nigeria", "NG"), ("Saudi Arabia", "SA"), ("South Africa", "ZA"),
        ("Turkey", "TR"), ("UAE", "AE"),
    ],
}

_RADIO_GENRES = [
    # value must be unique across ALL selects in the message — use "any_genre" sentinel
    ("Any genre", "any_genre"),
    ("Pop", "pop"), ("Rock", "rock"), ("Jazz", "jazz"), ("Electronic", "electronic"),
    ("Classical", "classical"), ("Hip-Hop", "hip-hop"), ("Country", "country"),
    ("Metal", "metal"), ("R&B", "rnb"), ("Reggae", "reggae"), ("Folk", "folk"),
    ("News", "news"), ("Talk", "talk"), ("Dance", "dance"), ("Techno", "techno"),
]


def _fetch_radio_stations(query: str | None, country: str = "", genre: str = "") -> list[dict]:
    """Fetch stations from radio-browser.info. BLOCKS: call via run_in_executor.

    No query, no filters -> top 50 stations by vote count (/json/stations/topvote).
    With query           -> fuzzy name search (/json/stations/byname/{encoded}).
    With country/genre   -> filtered search (/json/stations/search).
    Returns list of dicts: name, url_resolved, favicon, tags, country, bitrate.
    User-Agent header required -- radio-browser.info blocks requests without it.
    """
    base = "https://de1.api.radio-browser.info/json"
    if query:
        encoded = urllib.parse.quote(query, safe="")
        url = f"{base}/stations/byname/{encoded}?limit=50&order=votes&reverse=true&hidebroken=true"
    elif country or genre:
        params = urllib.parse.urlencode({
            "countrycode": country,
            "tagList": genre,
            "limit": 50,
            "order": "votes",
            "reverse": "true",
            "hidebroken": "true",
        })
        url = f"{base}/stations/search?{params}"
    else:
        url = f"{base}/stations/topvote?limit=50&hidebroken=true"
    req = urllib.request.Request(url, headers={"User-Agent": "discord-music-bot/1.0"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read())


def _build_radio_embed(
    stations: list[dict],
    query: str | None,
    page: int,
    total_pages: int,
) -> discord.Embed:
    """Build the paginated radio picker embed for RadioPickerView.

    Title: radio emoji + Radio Stations (no query) or Results for query (search mode).
    Each station: bold name line, then indented genre/country/bitrate on the next line.
    Footer: Page N of M, Powered by radio-browser.info.
    Color: 0x3498db (matches create_np_embed and _build_search_embed).
    """
    radio_icon = "\U0001f4fb"  # 📻
    bullet = "•"         # •
    if query:
        title = f"{radio_icon} Results for \"{query[:40]}\""
    else:
        title = f"{radio_icon} Radio Stations"

    lines = []
    for s in stations:
        name = s.get("name") or "Unknown"
        tags = s.get("tags") or ""
        genre = tags.split(",")[0].strip().title() if tags else "Unknown"
        country = s.get("country") or "Unknown"
        bitrate = s.get("bitrate") or 0
        lines.append(f"**{name}**")
        lines.append(f"    {genre} {bullet} {country} {bullet} {bitrate}kbps")

    embed = discord.Embed(
        title=title,
        description="\n".join(lines) if lines else "No stations found.",
        color=0x3498db,
    )
    embed.set_footer(text=f"Page {page} of {total_pages} {bullet} Powered by radio-browser.info")
    return embed


class RadioDiscoveryView(discord.ui.View):
    """Region → country → genre cascade picker for /radio with no search term.

    Step 1: user picks a region — country select updates to that region's countries.
    Step 2: user picks a country (or leaves "Any") and a genre.
    Step 3: Browse fetches filtered stations and hands off to RadioPickerView.
    """

    def __init__(self, bot, ctx: commands.Context, status_msg: discord.Message):
        super().__init__(timeout=60)
        self.bot = bot
        self.ctx = ctx
        self.status_msg = status_msg
        self.region = "worldwide"
        self.country = ""   # ISO-3166-1 alpha-2, or "" for no filter
        self.genre = ""     # tag string, or "" for no filter
        self._build_items()

    def _country_options(self) -> list[discord.SelectOption]:
        entries = _RADIO_REGION_COUNTRIES.get(self.region, [("Any country", "any_country")])
        return [discord.SelectOption(label=label, value=code) for label, code in entries]

    def _build_items(self):
        self.clear_items()

        region_select = discord.ui.Select(
            placeholder="🌍 Region...",
            options=[discord.SelectOption(label=label, value=code) for label, code in _RADIO_REGIONS],
            custom_id="discovery_region",
        )
        region_select.callback = self._on_region
        self.add_item(region_select)

        country_select = discord.ui.Select(
            placeholder="🏳️ Country...",
            options=self._country_options(),
            custom_id="discovery_country",
        )
        country_select.callback = self._on_country
        self.add_item(country_select)

        genre_select = discord.ui.Select(
            placeholder="🎵 Genre...",
            options=[discord.SelectOption(label=label, value=tag) for label, tag in _RADIO_GENRES],
            custom_id="discovery_genre",
        )
        genre_select.callback = self._on_genre
        self.add_item(genre_select)

        browse_btn = discord.ui.Button(
            label="Browse Stations",
            style=discord.ButtonStyle.primary,
            custom_id="discovery_browse",
        )
        browse_btn.callback = self._on_browse
        self.add_item(browse_btn)

    async def _on_region(self, interaction: discord.Interaction):
        self.region = interaction.data["values"][0]
        self.country = ""   # reset when region changes
        self._build_items()
        await interaction.response.edit_message(view=self)

    async def _on_country(self, interaction: discord.Interaction):
        value = interaction.data["values"][0]
        self.country = "" if value == "any_country" else value
        await interaction.response.defer()

    async def _on_genre(self, interaction: discord.Interaction):
        value = interaction.data["values"][0]
        self.genre = "" if value == "any_genre" else value
        await interaction.response.defer()

    async def _on_browse(self, interaction: discord.Interaction):
        self.stop()
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(content="📻 Loading stations...", embed=None, view=self)
        try:
            stations = await asyncio.get_event_loop().run_in_executor(
                None, _fetch_radio_stations, None, self.country, self.genre
            )
        except Exception as e:
            await self.status_msg.edit(content=f"Radio catalog error: {e}", embed=None, view=None)
            return
        if not stations:
            await self.status_msg.edit(content="No stations found for those filters.", embed=None, view=None)
            return
        total_pages = max(1, (len(stations) + RadioPickerView.PAGE_SIZE - 1) // RadioPickerView.PAGE_SIZE)
        page_stations = stations[:RadioPickerView.PAGE_SIZE]
        # Build embed title from active filters
        label_parts = []
        if self.country:
            for entries in _RADIO_REGION_COUNTRIES.values():
                for name, code in entries:
                    if code == self.country:
                        label_parts.append(name)
                        break
        if self.genre:
            label_parts.append(dict(_RADIO_GENRES).get(self.genre, self.genre).title())
        filter_label = " • ".join(label_parts) or None
        embed = _build_radio_embed(page_stations, filter_label, 1, total_pages)
        view = RadioPickerView(self.bot, self.ctx, stations, self.status_msg, query=filter_label)
        await self.status_msg.edit(content=None, embed=embed, view=view)

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True
        try:
            await self.status_msg.edit(
                content="Discovery expired — use `/radio` again.",
                embed=None,
                view=self,
            )
        except Exception:
            pass


class RadioPickerView(discord.ui.View):
    """Paginated radio station picker. Shows 10 stations per page.

    After a station is selected via the Select dropdown, _play_radio_selected()
    starts radio playback. Prev/Next buttons navigate pages without fetching new
    data from radio-browser.info -- the full station list is held in self.stations.
    On 60-second timeout, controls are disabled and the message is edited to an
    expiry notice per D-07.
    """

    PAGE_SIZE = 10

    def __init__(self, bot, ctx: commands.Context, stations: list[dict],
                 status_msg: discord.Message, query: str | None = None):
        super().__init__(timeout=60)
        self.bot = bot
        self.ctx = ctx
        # Deduplicate by url_resolved and drop stations with no stream URL.
        # radio-browser.info can return duplicate stream URLs; Discord rejects
        # SelectOption lists with duplicate values (error code 50035).
        seen_urls: set[str] = set()
        self.stations = []
        for s in stations:
            url = s.get("url_resolved") or ""
            if url and url not in seen_urls:
                seen_urls.add(url)
                self.stations.append(s)
        self.status_msg = status_msg
        self.query = query
        self.page = 0
        self.selected = False
        self._total_pages = max(1, (len(self.stations) + self.PAGE_SIZE - 1) // self.PAGE_SIZE)
        self._rebuild_items()

    def _page_stations(self) -> list[dict]:
        start = self.page * self.PAGE_SIZE
        return self.stations[start:start + self.PAGE_SIZE]

    def _rebuild_items(self):
        """Clear children and rebuild Select + Prev/Next buttons for the current page."""
        self.clear_items()
        page_stations = self._page_stations()
        options = []
        for s in page_stations:
            name = (s.get("name") or "Unknown")[:100]
            tags = s.get("tags") or ""
            genre = tags.split(",")[0].strip().title() if tags else "Unknown"
            country = s.get("country") or "Unknown"
            bitrate = s.get("bitrate") or 0
            desc = f"{genre} • {country} • {bitrate}kbps"
            options.append(discord.SelectOption(
                label=name,
                value=s.get("url_resolved") or "",
                description=desc[:100],
            ))
        select = discord.ui.Select(
            placeholder="Pick a station...",
            options=options,
        )
        select.callback = self._on_select
        self.add_item(select)

        btn_prev = discord.ui.Button(
            label="⏮️ Prev",
            style=discord.ButtonStyle.secondary,
            custom_id="radio_btn_prev",
            disabled=(self.page == 0),
        )
        btn_prev.callback = self._on_prev
        self.add_item(btn_prev)

        btn_next = discord.ui.Button(
            label="Next ⏭️",
            style=discord.ButtonStyle.secondary,
            custom_id="radio_btn_next",
            disabled=(self.page >= self._total_pages - 1),
        )
        btn_next.callback = self._on_next
        self.add_item(btn_next)

    async def _on_select(self, interaction: discord.Interaction):
        self.selected = True
        self.stop()
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(content="▶️ Loading...", embed=None, view=self)
        selected_url = interaction.data["values"][0]
        # Find full station dict by url_resolved so _play_radio_selected has all metadata
        station = next(
            (s for s in self.stations if s.get("url_resolved") == selected_url),
            {"url_resolved": selected_url, "name": selected_url, "favicon": "", "tags": "", "country": "", "bitrate": 0},
        )
        # Validate stream URL scheme before passing to FFmpeg (T-09-05)
        url = station.get("url_resolved") or ""
        if not (url.startswith("http://") or url.startswith("https://")):
            await self.status_msg.edit(content="Invalid stream URL scheme -- only http:// and https:// are supported.", embed=None, view=None)
            return
        await _play_radio_selected(self.bot, self.ctx, station, self.status_msg)

    async def _on_prev(self, interaction: discord.Interaction):
        self.page = max(0, self.page - 1)
        self._rebuild_items()
        embed = _build_radio_embed(self._page_stations(), self.query, self.page + 1, self._total_pages)
        await interaction.response.edit_message(embed=embed, view=self)

    async def _on_next(self, interaction: discord.Interaction):
        self.page = min(self._total_pages - 1, self.page + 1)
        self._rebuild_items()
        embed = _build_radio_embed(self._page_stations(), self.query, self.page + 1, self._total_pages)
        await interaction.response.edit_message(embed=embed, view=self)

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True
        try:
            await self.status_msg.edit(
                content="Radio browser expired — use `/radio` again.",
                embed=None,
                view=self,
            )
        except Exception:
            pass


async def _play_radio_selected(bot, ctx: commands.Context, station: dict,
                               picker_msg: discord.Message):
    """Continue the play flow with a radio station selected from RadioPickerView.

    Mirrors _play_selected() but calls gs.player.play_radio(track) instead of
    gs.player.play(url). The Track has is_radio=True so it is excluded from history.
    NP embed shows extra_desc="\U0001f534 LIVE" instead of a duration.
    """
    guild_id = str(ctx.guild.id)
    gs = bot.get_guild_state(ctx.guild.id)
    voice_channel = ctx.author.voice.channel
    user_id = str(ctx.author.id)
    channel_id = ctx.channel.id

    stream_url = station.get("url_resolved") or ""
    station_name = station.get("name") or stream_url
    favicon = station.get("favicon") or ""

    # --- Join voice if not already connected ---
    voice_client = ctx.guild.voice_client
    if not voice_client or not voice_client.is_connected():
        if voice_client:
            try:
                await voice_client.disconnect(force=True)
            except Exception:
                pass
            gs.player._voice_client = None
        try:
            voice_client = await voice_channel.connect()
            gs.player.set_voice_client(voice_client)
            saved_br = get_bitrate(guild_id)
            if saved_br:
                await gs.player.set_bitrate(ctx.guild.id, saved_br)
            saved_bass = get_eq_bass(guild_id)
            saved_treble = get_eq_treble(guild_id)
            if saved_bass != 0 or saved_treble != 0:
                await gs.player.set_eq(ctx.guild.id, saved_bass, saved_treble)
        except Exception as e:
            await picker_msg.edit(
                content=f"Failed to join voice channel: {e}",
                embed=None, view=None,
            )
            return

    # Stop current playback if any
    if gs.player.is_playing:
        gs.player.stop_playback()

    # Build the Track with is_radio=True (D-11: excluded from _history)
    track = Track(
        query=station_name,
        title=station_name,
        requested_by=user_id,
        thumbnail=favicon,
        url=stream_url,
        is_radio=True,
    )

    try:
        await gs.player.play_radio(track)
        gs.queue.current = track

        embed = create_np_embed(
            bot, station_name,
            extra_desc="\U0001f534 LIVE",
            thumbnail=favicon,
            url="",
            requester_name=f"<@{user_id}>",
            queue_tracks=gs.queue.preview_fair_order(),
            guild_id=ctx.guild.id,
        )
        await picker_msg.delete()
        await send_new_np(bot, channel_id, embed)
        _start_auto_next(bot, channel_id, ctx.guild.id)
    except Exception as e:
        await picker_msg.edit(
            content=f"Error starting radio: {e}",
            embed=None, view=None,
        )


class SearchPickerView(discord.ui.View):
    """Discord Select UI for YouTube search results. Shown after !play / /play plain-text query.

    After a result is selected, _play_selected() continues the normal playback flow.
    On 60-second timeout, the picker message is edited to an expiry notice.
    """

    def __init__(self, bot, ctx: commands.Context, results: list[dict],
                 status_msg: discord.Message):
        super().__init__(timeout=60)   # D-07: 60-second picker window
        self.bot = bot
        self.ctx = ctx
        self.results = results
        self.status_msg = status_msg
        self.selected = False

        options = [
            discord.SelectOption(
                label=r["title"][:100],
                value=r["url"],
                description=f"{r['uploader']} • {r['duration_str']}"[:100],
            )
            for r in results
        ]
        select = discord.ui.Select(
            placeholder="Pick a result...",
            options=options,
        )
        select.callback = self._on_select
        self.add_item(select)

    async def _on_select(self, interaction: discord.Interaction):
        self.selected = True
        self.stop()
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(content="▶️ Loading...", embed=None, view=self)
        selected_url = interaction.data["values"][0]
        await _play_selected(self.bot, self.ctx, selected_url, self.status_msg)

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True
        try:
            await self.status_msg.edit(
                content="Search expired — use `!play` again.",
                embed=None,
                view=self,
            )
        except Exception:
            pass


async def _play_selected(bot, ctx: commands.Context, url: str,
                         picker_msg: discord.Message):
    """Continue the play flow with a pre-resolved YouTube URL chosen from the picker.

    Mirrors the single-track flow in MusicCog.play() (lines 624-712) but uses
    picker_msg for status instead of a fresh ctx.send().
    """
    guild_id = str(ctx.guild.id)
    gs = bot.get_guild_state(ctx.guild.id)
    voice_channel = ctx.author.voice.channel
    user_id = str(ctx.author.id)
    channel_id = ctx.channel.id

    # --- Join voice if not already connected ---
    voice_client = ctx.guild.voice_client
    if not voice_client or not voice_client.is_connected():
        if voice_client:  # stale client — force-disconnect before reconnecting
            try:
                await voice_client.disconnect(force=True)
            except Exception:
                pass
            gs.player._voice_client = None
        try:
            voice_client = await voice_channel.connect()
            gs.player.set_voice_client(voice_client)
            saved_br = get_bitrate(guild_id)
            if saved_br:
                await gs.player.set_bitrate(ctx.guild.id, saved_br)
            saved_bass = get_eq_bass(guild_id)
            saved_treble = get_eq_treble(guild_id)
            if saved_bass != 0 or saved_treble != 0:
                await gs.player.set_eq(ctx.guild.id, saved_bass, saved_treble)
        except Exception as e:
            await picker_msg.edit(
                content=f"Failed to join voice channel: {e}",
                embed=None, view=None,
            )
            return

    # --- Determine playback state ---
    voice_actually_playing = (
        gs.player.is_playing
        and ctx.guild.voice_client
        and ctx.guild.voice_client.is_playing()
    )

    if voice_actually_playing:
        # Queue-add path: bot already playing something
        track = Track(query=url, title=url, requested_by=user_id)
        gs.queue.add(track)
        asyncio.create_task(_resolve_track_info(bot, channel_id, track))
        await picker_msg.edit(content="Added to queue.", embed=None, view=None)
        current = gs.queue.current
        if current:
            requester_name = f"<@{current.requested_by}>" if getattr(current, "requested_by", None) else ""
            embed = create_np_embed(
                bot, current.title,
                thumbnail=current.thumbnail,
                url=current.url,
                requester_name=requester_name,
                queue_tracks=gs.queue.preview_fair_order(),
                guild_id=ctx.guild.id,
            )
            await update_np_embed(bot, channel_id, embed)
    else:
        # Start-playback path: nothing playing yet
        try:
            info = await gs.player.play(url)
            title = info["title"]
            track_thumbnail = info.get("thumbnail", "")
            track_url = info.get("webpage_url", url)

            gs.queue.current = Track(
                query=url, title=title, requested_by=user_id,
                thumbnail=track_thumbnail, url=track_url,
            )

            embed = create_np_embed(
                bot, title,
                thumbnail=track_thumbnail,
                url=track_url,
                requester_name=f"<@{user_id}>",
                queue_tracks=gs.queue.preview_fair_order(),
                guild_id=ctx.guild.id,
            )
            await picker_msg.delete()
            await send_new_np(bot, channel_id, embed)
            _start_auto_next(bot, channel_id, ctx.guild.id)
        except Exception as e:
            await picker_msg.edit(
                content=f"Error playing track: {e}",
                embed=None, view=None,
            )


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

    if gs and guild_id is not None:
        eq_bass, eq_treble = gs.player.get_eq_for_guild(guild_id)
    else:
        eq_bass, eq_treble = (0, 0)
    eq_label = get_eq_preset_name(eq_bass, eq_treble)
    embed.set_footer(text=f"Audio: {kbps} kbps • EQ: {eq_label} • {p}bitrate | {p}eq to change")
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
            await interaction.channel.send(f"Skipping track: {_friendly_ytdlp_error(e)}")

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
                await interaction.channel.send(f"Skipping track: {_friendly_ytdlp_error(e)}")
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

    @commands.hybrid_command(name="play", description="Play a song or search YouTube")
    @app_commands.describe(query="Song name or URL to play")
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
        # Search picker flow (D-08: plain text → picker, URL → bypass)
        # ---------------------------------------------------------------
        # Strip bare "ytsearch:" prefix so embed title shows the raw query (D-08)
        query = _strip_ytsearch_prefix(query)

        if _is_search_query(query):
            # Extend slash command 3-second response window (no-op for prefix)
            await ctx.defer()
            status_msg = await ctx.send("🔍 Searching...")
            try:
                results = await asyncio.get_event_loop().run_in_executor(
                    None, _search_youtube, query
                )
            except Exception as e:
                await status_msg.edit(content=f"Search error: {e}")
                return
            if not results:
                await status_msg.edit(content=f"No results found for \"{query[:50]}\".")
                return
            embed = _build_search_embed(query, results)
            view = SearchPickerView(self.bot, ctx, results, status_msg)
            await status_msg.edit(content=None, embed=embed, view=view)
            return  # SearchPickerView._on_select → _play_selected continues the flow

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
                    if voice_client:  # stale client — force-disconnect before reconnecting
                        try:
                            await voice_client.disconnect(force=True)
                        except Exception:
                            pass
                        gs.player._voice_client = None
                    try:
                        voice_client = await voice_channel.connect()
                        gs.player.set_voice_client(voice_client)
                        # Restore saved bitrate for this guild
                        saved_br = get_bitrate(guild_id)
                        if saved_br:
                            await gs.player.set_bitrate(ctx.guild.id, saved_br)
                        # Restore saved EQ for this guild (per D-09)
                        saved_bass = get_eq_bass(guild_id)
                        saved_treble = get_eq_treble(guild_id)
                        if saved_bass != 0 or saved_treble != 0:
                            await gs.player.set_eq(ctx.guild.id, saved_bass, saved_treble)
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
            if voice_client:  # stale client — force-disconnect before reconnecting
                try:
                    await voice_client.disconnect(force=True)
                except Exception:
                    pass
                gs.player._voice_client = None
            try:
                voice_client = await voice_channel.connect()
                gs.player.set_voice_client(voice_client)
                # Restore saved bitrate for this guild
                saved_br = get_bitrate(guild_id)
                if saved_br:
                    await gs.player.set_bitrate(ctx.guild.id, saved_br)
                # Restore saved EQ for this guild (per D-09)
                saved_bass = get_eq_bass(guild_id)
                saved_treble = get_eq_treble(guild_id)
                if saved_bass != 0 or saved_treble != 0:
                    await gs.player.set_eq(ctx.guild.id, saved_bass, saved_treble)
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

    @commands.hybrid_command(name="radio", description="Browse or search internet radio stations")
    @app_commands.describe(query="Station name to search, or leave blank to browse by country/genre")
    async def radio(self, ctx: commands.Context, *, query: str = None):
        """Browse and stream internet radio stations.

        No query: shows country + genre picker for discovery.
        With query: fuzzy name search across 30k+ stations.
        Uses radio-browser.info REST API (no API key required, D-01).
        """
        if not await check_channel(ctx):
            return

        if not ctx.author.voice or not ctx.author.voice.channel:
            await ctx.send("You need to be in a voice channel.")
            return

        await ctx.defer()

        if not query:
            # Discovery mode: let user narrow by country + genre before loading stations
            status_msg = await ctx.send("📻 Discover Radio")
            embed = discord.Embed(
                title="📻 Discover Radio",
                description="Pick a country and genre to browse stations, or leave either as \"Any\" to browse all.",
                color=0x3498db,
            )
            view = RadioDiscoveryView(self.bot, ctx, status_msg)
            await status_msg.edit(content=None, embed=embed, view=view)
            return

        # Search mode: go straight to byname results
        status_msg = await ctx.send("📻 Searching stations...")
        try:
            stations = await asyncio.get_event_loop().run_in_executor(
                None, _fetch_radio_stations, query
            )
        except Exception as e:
            await status_msg.edit(content=f"Radio catalog error: {e}")
            return

        if not stations:
            await status_msg.edit(content=f"No stations found for \"{query[:50]}\".")
            return

        total_pages = max(1, (len(stations) + RadioPickerView.PAGE_SIZE - 1) // RadioPickerView.PAGE_SIZE)
        page_stations = stations[:RadioPickerView.PAGE_SIZE]
        embed = _build_radio_embed(page_stations, query, 1, total_pages)
        view = RadioPickerView(self.bot, ctx, stations, status_msg, query=query)
        await status_msg.edit(content=None, embed=embed, view=view)
        # RadioPickerView._on_select -> _play_radio_selected continues the flow

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
                await ctx.send(f"Skipping track: {_friendly_ytdlp_error(e)}")
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

    @commands.hybrid_command(name="shuffle", description="Shuffle the current queue")
    async def shuffle(self, ctx: commands.Context):
        if not await check_channel(ctx):
            return
        if not ctx.guild:
            return
        gs = self.bot.get_guild_state(ctx.guild.id)
        count = gs.queue.shuffle()
        if count == 0:
            await ctx.send("The queue is empty — nothing to shuffle.")
        elif count == 1:
            await ctx.send("Only one track in the queue — nothing to shuffle.")
        else:
            await ctx.send(f"Shuffled {count} tracks in the queue.")

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
                    await ctx.send(f"Skipping track: {_friendly_ytdlp_error(e)}")

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

    @commands.command(name="eq")
    async def eq(self, ctx: commands.Context, *args: str):
        """Per-guild equalizer — admin only. See !eq for usage."""
        if not await check_channel(ctx):
            return
        if not ctx.guild:
            return
        if not await self._check_admin(ctx):
            return

        gs = self.bot.get_guild_state(ctx.guild.id)
        guild_id_str = str(ctx.guild.id)
        p = self.bot.command_prefix
        preset_list = ", ".join(EQ_PRESETS.keys())

        # No args: show current state + usage.
        if not args:
            cur_bass = get_eq_bass(guild_id_str)
            cur_treble = get_eq_treble(guild_id_str)
            cur_name = get_eq_preset_name(cur_bass, cur_treble)
            await ctx.send(
                f"Current EQ: **{cur_name}** (bass={cur_bass:+d} dB, treble={cur_treble:+d} dB).\n"
                f"Usage: `{p}eq bass <-10..+10>` | `{p}eq treble <-10..+10>` | "
                f"`{p}eq <preset>` (presets: {preset_list}) | `{p}eq reset`"
            )
            return

        sub = args[0].lower()

        # Reset / flat
        if sub in ("reset", "flat"):
            try:
                set_eq_bass(guild_id_str, 0)
                set_eq_treble(guild_id_str, 0)
            except ValueError as e:
                await ctx.send(f"EQ error: {e}")
                return
            await gs.player.set_eq(ctx.guild.id, 0, 0)
            await ctx.send("EQ reset to **flat** (applies starting next track).")
            return

        # Single-band adjustment: !eq bass <N> or !eq treble <N>
        if sub in ("bass", "treble"):
            if len(args) < 2:
                await ctx.send(f"Usage: `{p}eq {sub} <-10..+10>`")
                return
            try:
                db = int(args[1])
            except ValueError:
                await ctx.send(f"EQ {sub} value must be an integer between -10 and +10.")
                return
            try:
                if sub == "bass":
                    set_eq_bass(guild_id_str, db)
                else:
                    set_eq_treble(guild_id_str, db)
            except ValueError as e:
                await ctx.send(f"EQ error: {e}")
                return
            new_bass = get_eq_bass(guild_id_str)
            new_treble = get_eq_treble(guild_id_str)
            await gs.player.set_eq(ctx.guild.id, new_bass, new_treble)
            preset = get_eq_preset_name(new_bass, new_treble)
            await ctx.send(
                f"EQ {sub} set to **{db:+d} dB** (preset: {preset}). Applies starting next track."
            )
            return

        # Preset selection
        if sub in EQ_PRESETS:
            b, t = EQ_PRESETS[sub]
            try:
                set_eq_bass(guild_id_str, b)
                set_eq_treble(guild_id_str, t)
            except ValueError as e:
                await ctx.send(f"EQ error: {e}")
                return
            await gs.player.set_eq(ctx.guild.id, b, t)
            await ctx.send(
                f"EQ preset **{sub}** applied (bass={b:+d} dB, treble={t:+d} dB). "
                f"Applies starting next track."
            )
            return

        # Unknown subcommand
        await ctx.send(
            f"Unknown EQ subcommand `{sub}`. Valid: bass, treble, reset, {preset_list}."
        )

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
            f"`{p}shuffle` — Shuffle the current queue\n"
            f"`{p}loadall` — Load all remaining tracks from the last pending playlist\n"
            f"`{p}bitrate [kbps]` — Show or set audio encoding bitrate\n"
            f"`{p}eq [bass|treble <N> | preset | reset]` — Per-guild equalizer, -10..+10 dB *(admin only)*\n"
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
                    await channel.send(f"Skipping track: {_friendly_ytdlp_error(e)}")
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
