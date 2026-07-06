import asyncio
import http.client
import json
import re
import time
import urllib.parse
import urllib.request
import discord
import yt_dlp
from discord.ext import commands
from discord import app_commands
from track_queue import Track
from audio_player import (
    is_playlist_url, extract_playlist_info, get_audio_url_with_retry,
    is_stream_info_fresh, get_related_tracks,
)
from spotify import is_spotify_url, resolve_spotify, SpotifyError
import weather
import f1
import rocket
import aurora
from guild_settings import (
    get_allowed_channel, set_allowed_channel,
    get_bitrate, set_bitrate,
    get_admins, add_admin, remove_admin,
    get_eq_bass, set_eq_bass, get_eq_treble, set_eq_treble,
    EQ_PRESETS, get_eq_preset_name,
    get_weather_location, set_weather_location, get_timezone, set_timezone,
    get_display_prefs, DEFAULT_WEATHER_LOCATION,
)





NP_EMBED_COLOR = 0xF59E0B  # amber/gold — Now Playing accent


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
        title=f"Results for \"{query_display}\"",
        description="\n".join(lines) if lines else "No results.",
        color=0x3498db,
    )
    embed.set_footer(text="Select a result below • Expires in 60s")
    return embed


_RADIO_REGIONS = [
    ("Worldwide", "worldwide"),
    ("Northern Europe", "europe_north"),
    ("Western Europe", "europe_west"),
    ("Southern Europe", "europe_south"),
    ("Eastern Europe", "europe_east"),
    ("Americas", "americas"),
    ("Asia & Pacific", "asia_pacific"),
    ("Middle East & Africa", "mideast_africa"),
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


_RADIO_SNI = "all.api.radio-browser.info"
_RADIO_TIMEOUT = 10


def _fetch_radio_stations(query: str | None, country: str = "", genre: str = "") -> list[dict]:
    """Fetch stations from radio-browser.info. BLOCKS: call via run_in_executor.

    No query, no filters -> top 50 stations by vote count (/json/stations/topvote).
    With query           -> fuzzy name search (/json/stations/byname/{encoded}).
    With country/genre   -> filtered search (/json/stations/search).
    Returns list of dicts: name, url_resolved, favicon, tags, country, bitrate.

    Connects by hostname (not a pinned IP) so the OS resolver tries every address
    (IPv4 + IPv6) and standard SNI/cert validation applies. radio-browser.info has
    consolidated onto a single host, so the old per-mirror IP-pinning + forced-IPv4
    was both pointless and brittle -- it surfaced as the DNS failure
    "[Errno -2] Name or service not known" inside containers.
    User-Agent header required -- radio-browser.info blocks requests without it.
    """
    if query:
        encoded = urllib.parse.quote(query, safe="")
        path = f"/json/stations/byname/{encoded}?limit=50&order=votes&reverse=true&hidebroken=true"
    elif country or genre:
        qs = urllib.parse.urlencode({
            "countrycode": country,
            "tagList": genre,
            "limit": 50,
            "order": "votes",
            "reverse": "true",
            "hidebroken": "true",
        })
        path = f"/json/stations/search?{qs}"
    else:
        path = "/json/stations/topvote?limit=50&hidebroken=true"

    conn = None
    try:
        conn = http.client.HTTPSConnection(_RADIO_SNI, timeout=_RADIO_TIMEOUT)
        conn.request("GET", path, headers={"User-Agent": "discord-music-bot/1.0"})
        resp = conn.getresponse()
        data = resp.read()
    except OSError as e:
        raise OSError(f"Cannot reach radio-browser.info ({e}) — check network/DNS connectivity") from e
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
    return json.loads(data)


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
    bullet = "•"         # •
    if query:
        title = f"Results for \"{query[:40]}\""
    else:
        title = "Radio Stations"

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


async def _picker_author_check(interaction: discord.Interaction, ctx: commands.Context) -> bool:
    """Allow only the user who opened a picker to interact with it."""
    if interaction.user.id != ctx.author.id:
        await interaction.response.send_message(
            "Only the person who ran the command can use this picker.", ephemeral=True
        )
        return False
    return True


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

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await _picker_author_check(interaction, self.ctx)

    def _country_options(self) -> list[discord.SelectOption]:
        entries = _RADIO_REGION_COUNTRIES.get(self.region, [("Any country", "any_country")])
        return [discord.SelectOption(label=label, value=code) for label, code in entries]

    def _build_items(self):
        self.clear_items()

        region_label = {code: label for label, code in _RADIO_REGIONS}.get(self.region, "")
        region_placeholder = region_label if self.region != "worldwide" else "Region…"
        region_select = discord.ui.Select(
            placeholder=region_placeholder,
            options=[
                discord.SelectOption(label=label, value=code)
                for label, code in _RADIO_REGIONS
            ],
            custom_id="discovery_region",
        )
        region_select.callback = self._on_region
        self.add_item(region_select)

        country_select = discord.ui.Select(
            placeholder="Country…",
            options=self._country_options(),
            custom_id="discovery_country",
        )
        country_select.callback = self._on_country
        self.add_item(country_select)

        genre_select = discord.ui.Select(
            placeholder="Genre…",
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
        await interaction.response.edit_message(content="Loading stations…", embed=None, view=self)
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

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await _picker_author_check(interaction, self.ctx)

    def _page_stations(self) -> list[dict]:
        start = self.page * self.PAGE_SIZE
        return self.stations[start:start + self.PAGE_SIZE]

    def _rebuild_items(self):
        """Clear children and rebuild Select + Prev/Next buttons for the current page."""
        self.clear_items()
        page_stations = self._page_stations()
        options = []
        start = self.page * self.PAGE_SIZE
        for i, s in enumerate(page_stations):
            name = (s.get("name") or "Unknown")[:100]
            tags = s.get("tags") or ""
            genre = tags.split(",")[0].strip().title() if tags else "Unknown"
            country = s.get("country") or "Unknown"
            bitrate = s.get("bitrate") or 0
            desc = f"{genre} • {country} • {bitrate}kbps"
            options.append(discord.SelectOption(
                label=name,
                value=str(start + i),   # absolute index — URLs can exceed Discord's 100-char limit
                description=desc[:100],
            ))
        select = discord.ui.Select(
            placeholder="Pick a station...",
            options=options,
        )
        select.callback = self._on_select
        self.add_item(select)

        btn_prev = discord.ui.Button(
            label="◄ Prev",
            style=discord.ButtonStyle.secondary,
            custom_id="radio_btn_prev",
            disabled=(self.page == 0),
        )
        btn_prev.callback = self._on_prev
        self.add_item(btn_prev)

        btn_next = discord.ui.Button(
            label="Next ►",
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
        await interaction.response.edit_message(content="Loading…", embed=None, view=self)
        station = self.stations[int(interaction.data["values"][0])]
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

        view = build_player_view(
            bot, station_name,
            extra_desc="● LIVE",
            thumbnail=favicon,
            url="",
            requester_name=f"<@{user_id}>",
            queue_tracks=gs.queue.preview_fair_order(),
            guild_id=ctx.guild.id,
        )
        await picker_msg.delete()
        await send_new_np(bot, channel_id, view)
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

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await _picker_author_check(interaction, self.ctx)

    async def _on_select(self, interaction: discord.Interaction):
        self.selected = True
        self.stop()
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(content="Loading…", embed=None, view=self)
        selected_url = interaction.data["values"][0]
        # Carry the full result dict so playback reuses the flat-search metadata
        # (title/thumbnail) instead of re-fetching it with a full /player call.
        selected = next((r for r in self.results if r.get("url") == selected_url), None)
        await _play_selected(
            self.bot, self.ctx,
            selected or {"url": selected_url, "title": selected_url},
            self.status_msg,
        )

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True
        try:
            await self.status_msg.edit(
                content="Search expired — use `!search` again.",
                embed=None,
                view=self,
            )
        except Exception:
            pass


async def _play_selected(bot, ctx: commands.Context, result: dict,
                         picker_msg: discord.Message):
    """Continue the play flow with a chosen search result dict {url, title, thumbnail, ...}.

    Mirrors the single-track flow in MusicCog.play() but uses picker_msg for status.
    Trusts the flat-search metadata for display (so a queue-add skips the redundant
    background full resolve) and overlaps the URL resolve with the voice connect.
    """
    guild_id = str(ctx.guild.id)
    gs = bot.get_guild_state(ctx.guild.id)
    voice_channel = ctx.author.voice.channel
    user_id = str(ctx.author.id)
    channel_id = ctx.channel.id
    loop = asyncio.get_event_loop()

    url = result["url"]
    r_title = result.get("title") or url
    r_thumb = result.get("thumbnail", "")

    voice_client = ctx.guild.voice_client
    voice_actually_playing = (
        gs.player.is_playing
        and voice_client
        and voice_client.is_playing()
    )

    if voice_actually_playing:
        # Queue-add path: trust flat-search metadata (url set → no background resolve).
        track = Track(query=url, title=r_title, requested_by=user_id, thumbnail=r_thumb, url=url)
        gs.queue.add(track)
        _schedule_prefetch(bot, ctx.guild.id)
        await picker_msg.edit(content="Added to queue.", embed=None, view=None)
        current = gs.queue.current
        if current:
            requester_name = f"<@{current.requested_by}>" if getattr(current, "requested_by", None) else ""
            view = build_player_view(
                bot, current.title,
                thumbnail=current.thumbnail,
                url=current.url,
                requester_name=requester_name,
                queue_tracks=gs.queue.preview_fair_order(),
                guild_id=ctx.guild.id,
            )
            await update_np_embed(bot, channel_id, view)
        return

    # Start-playback path: overlap the URL resolve with the voice connect.
    yt_cfg = bot.config.get("youtube", {})
    resolve_fut = loop.run_in_executor(
        None, get_audio_url_with_retry, url,
        yt_cfg.get("client", "web"), bot.config.get("debug", False),
        yt_cfg.get("cookies_file") or None,
    )

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
            _discard_future(resolve_fut)
            await picker_msg.edit(
                content=f"Failed to join voice channel: {e}",
                embed=None, view=None,
            )
            return

    try:
        info = await resolve_fut
    except Exception as e:
        await picker_msg.edit(
            content=f"Error playing track: {_friendly_ytdlp_error(e)}",
            embed=None, view=None,
        )
        return

    resolved_at = time.time()
    try:
        played = await gs.player.play(url, info, resolved_at)
        title = played["title"]
        track_thumbnail = played.get("thumbnail", "")
        track_url = played.get("webpage_url", url)

        gs.queue.current = Track(
            query=url, title=title, requested_by=user_id,
            thumbnail=track_thumbnail, url=track_url,
        )

        view = build_player_view(
            bot, title,
            thumbnail=track_thumbnail,
            url=track_url,
            requester_name=f"<@{user_id}>",
            queue_tracks=gs.queue.preview_fair_order(),
            guild_id=ctx.guild.id,
        )
        await picker_msg.delete()
        await send_new_np(bot, channel_id, view)
        _start_auto_next(bot, channel_id, ctx.guild.id)
    except Exception as e:
        await picker_msg.edit(
            content=f"Error playing track: {e}",
            embed=None, view=None,
        )


class _ControlsRow(discord.ui.ActionRow):
    """The five Now-Playing control buttons, rendered INSIDE the player Container.

    Icon-only monochrome glyphs (no colorful emoji). custom_ids are preserved from
    the old PlayerControls so existing interactions/tests keep matching. `self.view`
    is the parent PlayerView; bot/guild/channel come from it and the interaction.
    """

    @discord.ui.button(label="‖", style=discord.ButtonStyle.success, custom_id="btn_playpause")  # ‖
    async def playpause_callback(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self.view.evaluate_vote(interaction, "playpause"): return
        guild_id = interaction.guild_id
        gs = self.view.bot.get_guild_state(guild_id)
        if gs.player.is_playing and not gs.player.is_paused:
            gs.player.pause()
        elif gs.player.is_paused:
            gs.player.resume()
        # Rebuild the card with the paused flag flipped to its new state.
        new_view = build_player_view(self.view.bot, **{**self.view._kwargs, "paused": gs.player.is_paused})
        await interaction.response.edit_message(view=new_view)

    @discord.ui.button(label="◄", style=discord.ButtonStyle.primary, custom_id="btn_prev")  # ◄
    async def prev_callback(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self.view.evaluate_vote(interaction, "prev"): return
        if _on_cooldown(interaction.guild_id, interaction.user.id, "skip", 2.0):
            await interaction.response.send_message("Easy on the skip — give it a moment.", ephemeral=True)
            return
        await interaction.response.defer()
        guild_id = interaction.guild_id
        gs = self.view.bot.get_guild_state(guild_id)
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
            info = await gs.player.play(prev_track.query, prev_track.resolved_info, prev_track.resolved_at)
            title = info["title"]
            prev_track.title = title
            prev_track.thumbnail = info.get("thumbnail", "")
            prev_track.url = info.get("webpage_url", "")
            view = build_player_view(self.view.bot, title,
                                     thumbnail=prev_track.thumbnail,
                                     url=prev_track.url,
                                     requester_name=_get_requester_name(self.view.bot, prev_track.requested_by),
                                     queue_tracks=gs.queue.preview_fair_order(),
                                     guild_id=guild_id)
            await send_new_np(self.view.bot, interaction.channel.id, view)
            _start_auto_next(self.view.bot, interaction.channel.id, guild_id)
        except Exception as e:
            await interaction.channel.send(f"Skipping track: {_friendly_ytdlp_error(e)}")

    @discord.ui.button(label="►", style=discord.ButtonStyle.primary, custom_id="btn_next")  # ►
    async def next_callback(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self.view.evaluate_vote(interaction, "next"): return
        if _on_cooldown(interaction.guild_id, interaction.user.id, "skip", 2.0):
            await interaction.response.send_message("Easy on the skip — give it a moment.", ephemeral=True)
            return
        await interaction.response.defer()
        guild_id = interaction.guild_id
        gs = self.view.bot.get_guild_state(guild_id)
        if gs.auto_next_task and not gs.auto_next_task.done():
            gs.auto_next_task.cancel()
            gs.auto_next_task = None
        gs.auto_next_gen += 1
        gs.player.stop_playback()
        next_track = gs.queue.next(force=True)  # manual skip bypasses track-loop
        if next_track:
            try:
                info = await gs.player.play(next_track.query, next_track.resolved_info, next_track.resolved_at)
                title = info["title"]
                next_track.title = title
                next_track.thumbnail = info.get("thumbnail", "")
                next_track.url = info.get("webpage_url", "")
                view = build_player_view(self.view.bot, title,
                                         thumbnail=next_track.thumbnail,
                                         url=next_track.url,
                                         requester_name=_get_requester_name(self.view.bot, next_track.requested_by),
                                         queue_tracks=gs.queue.preview_fair_order(),
                                         guild_id=guild_id)
                await send_new_np(self.view.bot, interaction.channel.id, view)
                _start_auto_next(self.view.bot, interaction.channel.id, guild_id)
            except Exception as e:
                await interaction.channel.send(f"Skipping track: {_friendly_ytdlp_error(e)}")
        else:
            await update_np_stopped(self.view.bot, interaction.channel.id)

    @discord.ui.button(label="■", style=discord.ButtonStyle.danger, custom_id="btn_stop")  # ■
    async def stop_callback(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self.view.evaluate_vote(interaction, "stop"): return
        await interaction.response.defer()
        guild_id = interaction.guild_id
        gs = self.view.bot.get_guild_state(guild_id)
        if gs.auto_next_task and not gs.auto_next_task.done():
            gs.auto_next_task.cancel()
            gs.auto_next_task = None
        gs.player.stop_playback()
        gs.queue.clear()
        await gs.player.disconnect()
        await update_np_stopped(self.view.bot, interaction.channel.id)

    @discord.ui.button(label="☰", style=discord.ButtonStyle.secondary, custom_id="btn_queue")  # ☰
    async def queue_callback(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Read-only: show the queue privately (ephemeral), no fairness vote required.
        guild_id = interaction.guild_id
        gs = self.view.bot.get_guild_state(guild_id)
        tracks = gs.queue.list()
        lines = []
        if gs.player.current_track_title:
            lines.append(f"**Now playing:** {gs.player.current_track_title}")
        if tracks:
            for i, t in enumerate(tracks, 1):
                req_name = _get_requester_name(self.view.bot, t.requested_by, interaction.guild) if t.requested_by else ""
                req_tag = f" — {req_name}" if req_name else ""
                lines.append(f"`{i}.` {t.title}{req_tag}")
        else:
            lines.append("*Queue is empty.*")
        text = "\n".join(lines)
        if len(text) > 1900:
            text = text[:1900].rsplit("\n", 1)[0] + "\n*…more*"
        await interaction.response.send_message(text, ephemeral=True)


class _LoadPlaylistRow(discord.ui.ActionRow):
    """Single "Load playlist" button folded into the player Container when a pending
    playlist exists for this guild. Adds the remaining tracks then rebuilds the card."""

    @discord.ui.button(label="Load playlist", style=discord.ButtonStyle.success, custom_id="btn_load_playlist")
    async def load_callback(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        bot = self.view.bot
        guild_id = interaction.guild_id
        channel_id = interaction.channel.id
        gs = bot.get_guild_state(guild_id)
        pending = bot.pending_playlists.pop(str(channel_id), None)
        if not pending:
            await interaction.followup.send("Playlist already loaded or expired.", ephemeral=True)
            current = gs.queue.current
            if current:
                view = build_player_view(bot, current.title,
                                         thumbnail=current.thumbnail,
                                         url=current.url,
                                         requester_name=f"<@{current.requested_by}>" if getattr(current, 'requested_by', None) else "",
                                         queue_tracks=gs.queue.preview_fair_order(),
                                         guild_id=guild_id)
                await interaction.message.edit(view=view)
            return

        tracks = pending["tracks"]
        user_id = str(interaction.user.id)

        for t in tracks:
            track = Track(query=t["url"], title=t["title"], requested_by=user_id, url=t["url"])
            gs.queue.add(track)
        _schedule_prefetch(bot, guild_id)

        current = gs.queue.current
        if current:
            view = build_player_view(bot, current.title,
                                     thumbnail=current.thumbnail,
                                     url=current.url,
                                     requester_name=f"<@{current.requested_by}>" if getattr(current, 'requested_by', None) else "",
                                     queue_tracks=gs.queue.preview_fair_order(),
                                     guild_id=guild_id)
            await interaction.message.edit(view=view)

            channel = bot.get_channel(channel_id)
            if channel:
                await channel.send(f"Added **{len(tracks)}** tracks to the queue.")


class _SecondaryRow(discord.ui.ActionRow):
    """Second control row: loop-mode toggle + shuffle, folded into the Container.

    Icon-only monochrome glyphs matching _ControlsRow. Loop cycles off → track →
    queue; the button's colour/label reflect the mode (set in PlayerView._build).
    """

    @discord.ui.button(label="Loop: Off", style=discord.ButtonStyle.secondary, custom_id="btn_loop")
    async def loop_callback(self, interaction: discord.Interaction, button: discord.ui.Button):
        if _on_cooldown(interaction.guild_id, interaction.user.id, "loop", 1.0):
            await interaction.response.send_message("One moment…", ephemeral=True)
            return
        gs = self.view.bot.get_guild_state(interaction.guild_id)
        gs.queue.cycle_loop()
        kwargs = {**self.view._kwargs, "queue_tracks": gs.queue.preview_fair_order()}
        await interaction.response.edit_message(view=build_player_view(self.view.bot, **kwargs))

    @discord.ui.button(label="Shuffle", style=discord.ButtonStyle.secondary, custom_id="btn_shuffle")
    async def shuffle_callback(self, interaction: discord.Interaction, button: discord.ui.Button):
        if _on_cooldown(interaction.guild_id, interaction.user.id, "shuffle", 1.5):
            await interaction.response.send_message("One moment…", ephemeral=True)
            return
        gs = self.view.bot.get_guild_state(interaction.guild_id)
        gs.queue.shuffle()
        kwargs = {**self.view._kwargs, "queue_tracks": gs.queue.preview_fair_order()}
        await interaction.response.edit_message(view=build_player_view(self.view.bot, **kwargs))

    @discord.ui.button(label="Grab", style=discord.ButtonStyle.secondary, custom_id="btn_grab")
    async def grab_callback(self, interaction: discord.Interaction, button: discord.ui.Button):
        if _on_cooldown(interaction.guild_id, interaction.user.id, "grab", 3.0):
            await interaction.response.send_message("You just grabbed this — give it a moment.", ephemeral=True)
            return
        gs = self.view.bot.get_guild_state(interaction.guild_id)
        current = gs.queue.current
        if not current or not gs.player.is_playing:
            await interaction.response.send_message("Nothing is playing to grab.", ephemeral=True)
            return
        try:
            await interaction.user.send(embed=_build_grab_embed(gs, current))
            await interaction.response.send_message("Sent it to your DMs.", ephemeral=True)
        except discord.Forbidden:
            await interaction.response.send_message(
                "I couldn't DM you — enable **Allow direct messages from server members**.",
                ephemeral=True,
            )


class PlayerView(discord.ui.LayoutView):
    """Components V2 Now-Playing card: an accent-barred Container holding the song
    info (with thumbnail Section), the Up-Next preview, a Separator, the control
    row(s), and a small-text footer. Replaces the old embed + separate action row."""

    def __init__(self, bot, *, title: str, extra_desc: str = "",
                 thumbnail: str = "", url: str = "",
                 requester_name: str = "",
                 queue_tracks: list | None = None,
                 guild_id: int | None = None,
                 paused: bool = False,
                 finished: bool = False):
        super().__init__(timeout=None)
        self.bot = bot
        # Store every kwarg so a button can rebuild the card (e.g. play/pause flip).
        self._kwargs = {
            "title": title, "extra_desc": extra_desc, "thumbnail": thumbnail,
            "url": url, "requester_name": requester_name,
            "queue_tracks": queue_tracks, "guild_id": guild_id, "paused": paused,
        }
        self._title = title
        self._extra_desc = extra_desc
        self._thumbnail = thumbnail
        self._url = url
        self._requester_name = requester_name
        self._queue_tracks = queue_tracks
        self._guild_id = guild_id
        self._paused = paused
        self._finished = finished
        self._build()

    def _build(self):
        bot = self.bot
        guild_id = self._guild_id
        finished = self._finished
        paused = self._paused

        gs = bot.get_guild_state(guild_id) if guild_id else None
        guild = bot.get_guild(guild_id) if guild_id else None
        loop_mode = gs.queue.loop_mode if gs else "off"
        kbps = gs.player.get_bitrate_for_guild(guild_id) // 1000 if gs else bot.config.get("audio", {}).get("bitrate", 128)

        accent = NP_EMBED_COLOR if not finished else 0x95a5a6
        c = discord.ui.Container(accent_colour=discord.Colour(accent))

        # Header line
        header = "**Paused**" if paused else ("**Queue finished**" if finished else "**Now Playing**")
        c.add_item(discord.ui.TextDisplay(header))

        if not finished:
            # Artist + duration come from the player's current-track state (set in
            # play()). Guard on the title so a stale artist/duration never attaches
            # to a different track (e.g. radio sets no artist).
            artist = ""
            duration_str = ""
            if gs and gs.player.current_track_title == self._title:
                artist = gs.player.current_artist or ""
                if gs.player.current_duration:
                    duration_str = _fmt_duration(gs.player.current_duration)

            title_md = f"[{self._title}]({self._url})" if self._url else self._title
            body = f"**{title_md}**"
            if artist:
                body += f" by {artist}"
            if duration_str:
                body += f" `[{duration_str}]`"

            if self._thumbnail:
                sec = discord.ui.Section(accessory=discord.ui.Thumbnail(self._thumbnail))
                sec.add_item(discord.ui.TextDisplay(body))
                c.add_item(sec)
            else:
                c.add_item(discord.ui.TextDisplay(body))

            if self._extra_desc:
                c.add_item(discord.ui.TextDisplay(self._extra_desc))

        # Up Next preview (next 5 + "...and N more")
        if not finished and self._queue_tracks:
            lines = []
            for i, t in enumerate(self._queue_tracks[:5], 1):
                req_name = _get_requester_name(bot, t.requested_by, guild) if t.requested_by else ""
                req_tag = f" — *{req_name}*" if req_name else ""
                if t.url:
                    lines.append(f"`{i}.` [{t.title}]({t.url}){req_tag}")
                else:
                    lines.append(f"`{i}.` {t.title}{req_tag}")
            remaining = (len(gs.queue.list()) - 5) if gs else (len(self._queue_tracks) - 5)
            if remaining > 0:
                lines.append(f"*...and {remaining} more*")
            c.add_item(discord.ui.TextDisplay("**Up Next**\n" + "\n".join(lines)))
        elif finished:
            c.add_item(discord.ui.TextDisplay("**Up Next**\n*Queue is empty*"))
        else:
            c.add_item(discord.ui.TextDisplay("**Up Next**\n*No songs in queue*"))

        c.add_item(discord.ui.Separator())

        # Controls live ABOVE the requester/footer line (matching the reference).
        if not finished:
            controls = _ControlsRow()
            # Reflect paused state on the play/pause glyph (▶ = resume, ‖ = pause).
            for _item in controls.children:
                if getattr(_item, "custom_id", None) == "btn_playpause":
                    _item.label = "▶" if paused else "‖"
            c.add_item(controls)

            # Second row: loop toggle (colour/label reflect the mode) + shuffle.
            secondary = _SecondaryRow()
            for _item in secondary.children:
                if getattr(_item, "custom_id", None) == "btn_loop":
                    if loop_mode == "track":
                        _item.style, _item.label = discord.ButtonStyle.success, "Loop: Song"
                    elif loop_mode == "queue":
                        _item.style, _item.label = discord.ButtonStyle.success, "Loop: Queue"
                    else:
                        _item.style, _item.label = discord.ButtonStyle.secondary, "Loop: Off"
            c.add_item(secondary)

            # Add the Load row when any pending playlist exists for this guild.
            # (The channel isn't known at build time, so match on guild_id.)
            has_pending = any(
                p.get("guild_id") in (guild_id, str(guild_id))
                for p in bot.pending_playlists.values()
            ) if guild_id is not None else False
            if has_pending:
                c.add_item(_LoadPlaylistRow())

        # Footer: requester name (plain — never a mention, so card refreshes don't
        # ping) + audio/EQ summary, small text.
        if gs and guild_id is not None:
            eq_bass, eq_treble = gs.player.get_eq_for_guild(guild_id)
        else:
            eq_bass, eq_treble = (0, 0)
        eq_label = get_eq_preset_name(eq_bass, eq_treble)
        requester = _plain_names(bot, guild, self._requester_name)
        loop_suffix = "" if loop_mode == "off" else f" · Loop {loop_mode}"
        autoplay_suffix = " · Autoplay" if (gs and gs.autoplay) else ""
        foot = ("-# " + (f"Track requested by {requester} · " if requester else "")
                + f"{kbps}kbps · EQ {eq_label}{loop_suffix}{autoplay_suffix}")
        c.add_item(discord.ui.TextDisplay(foot))

        # Flourish lines (small text): sky conditions, then upcoming events.
        # All use the guild's configured location/timezone, defaulting to Riga.
        prefs = (get_display_prefs(str(guild_id)) if guild_id
                 else {"location": dict(DEFAULT_WEATHER_LOCATION), "timezone": None})
        loc, tz = prefs["location"], prefs.get("timezone")

        w = _weather_text_for(loc)
        if w:
            c.add_item(discord.ui.TextDisplay("-# " + w))
        a = aurora.forecast_line(
            _kp_cache.get("list"),
            _sky_cache.get(_loc_key(loc["lat"], loc["lon"]), {}).get("hourly"),
            loc["lat"], loc["lon"], tz,
        )
        if a:
            c.add_item(discord.ui.TextDisplay("-# " + a))

        f1_txt = f1.format_race(_f1_cache.get("race"), tz)
        if f1_txt:
            c.add_item(discord.ui.TextDisplay("-# " + f1_txt))
        r_txt = rocket.format_launch(_rocket_cache.get("launch"), tz)
        if r_txt:
            c.add_item(discord.ui.TextDisplay("-# " + r_txt))

        self.add_item(c)

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
        passes, msg = _check_vote(self.bot, interaction.guild, interaction.user, action)
        if not passes:
            await interaction.response.send_message(msg, ephemeral=False)
        return passes


def build_player_view(bot, title: str, extra_desc: str = "",
                      thumbnail: str = "", url: str = "",
                      requester_name: str = "",
                      queue_tracks: list | None = None,
                      guild_id: int | None = None,
                      paused: bool = False,
                      finished: bool = False) -> PlayerView:
    """Build the Components V2 Now-Playing card (replaces create_np_embed)."""
    return PlayerView(
        bot, title=title, extra_desc=extra_desc, thumbnail=thumbnail, url=url,
        requester_name=requester_name, queue_tracks=queue_tracks,
        guild_id=guild_id, paused=paused, finished=finished,
    )


_MENTION_RE = re.compile(r"<@!?(\d+)>")


def _get_requester_name(bot, user_id, guild=None) -> str:
    """Resolve a user ID to a plain display name — never a mention.

    Rendering <@id> in the persistent Now-Playing card pinged the requester every
    time the card refreshed (and every queued user in the Up-Next list). Plain
    names show who requested a track without the notification spam.
    """
    if not user_id:
        return ""
    try:
        uid = int(user_id)
    except (TypeError, ValueError):
        return ""
    if guild is not None:
        member = guild.get_member(uid)
        if member:
            return member.display_name
    user = bot.get_user(uid)
    if user:
        return user.display_name
    return "someone"


def _plain_names(bot, guild, text: str) -> str:
    """Replace any <@id> mentions in a string with plain, non-pinging display names."""
    if not text or "<@" not in text:
        return text
    return _MENTION_RE.sub(lambda m: _get_requester_name(bot, m.group(1), guild) or "someone", text)


# --- Weather + next-F1-race card flourishes -----------------------------------
# Both are cached and refreshed off-loop; the card build only reads the caches,
# so it never blocks the event loop or hits the network on every render. Weather
# is keyed by rounded lat/lon (guilds sharing a location share the fetch); F1 is
# global (same for everyone) and formatted per-guild timezone at render time.
_weather_cache: dict = {}  # "lat,lon" -> {"text", "ts", "busy"}
_WEATHER_TTL = 600.0       # 10 minutes
_f1_cache = {"race": None, "ts": 0.0, "busy": False}
_F1_TTL = 3600.0           # 1 hour (the schedule changes rarely)
_rocket_cache = {"launch": None, "ts": 0.0, "busy": False}
_ROCKET_TTL = 7200.0       # 2 hours (Launch Library anon throttle is ~15/hr)
_kp_cache = {"list": None, "ts": 0.0, "busy": False}   # NOAA Kp forecast (global)
_KP_TTL = 3600.0           # 1 hour
_sky_cache: dict = {}      # "lat,lon" -> {"hourly", "ts", "busy"} (cloud + day/night)
_SKY_TTL = 1800.0          # 30 minutes


def _loc_key(lat, lon) -> str:
    return f"{round(float(lat), 2)},{round(float(lon), 2)}"


def _weather_text_for(location: dict) -> str:
    return _weather_cache.get(_loc_key(location["lat"], location["lon"]), {}).get("text", "")


async def _refresh_weather_for(location: dict):
    """Fetch weather for one location if its cache is stale (no-op otherwise)."""
    key = _loc_key(location["lat"], location["lon"])
    entry = _weather_cache.setdefault(key, {"text": "", "ts": 0.0, "busy": False})
    now = time.monotonic()
    if entry["busy"] or (entry["ts"] and now - entry["ts"] < _WEATHER_TTL):
        return
    entry["busy"] = True
    entry["ts"] = now  # throttle attempts (success or failure) to the TTL
    try:
        text = await asyncio.get_event_loop().run_in_executor(
            None, weather.get_weather, location["lat"], location["lon"], location["name"]
        )
        if text:
            entry["text"] = text
    except Exception:
        pass
    finally:
        entry["busy"] = False


async def _refresh_weather_if_stale(bot, guild_id):
    """Refresh the weather for a guild's configured location."""
    await _refresh_weather_for(get_display_prefs(str(guild_id))["location"])


async def _refresh_f1_if_stale():
    """Fetch the next F1 race in the background if the cache is stale."""
    now = time.monotonic()
    if _f1_cache["busy"] or (_f1_cache["ts"] and now - _f1_cache["ts"] < _F1_TTL):
        return
    _f1_cache["busy"] = True
    _f1_cache["ts"] = now
    try:
        _f1_cache["race"] = await asyncio.get_event_loop().run_in_executor(None, f1.get_next_race)
    except Exception:
        pass
    finally:
        _f1_cache["busy"] = False


async def _refresh_rocket_if_stale():
    """Fetch the next rocket launch in the background if the cache is stale."""
    now = time.monotonic()
    if _rocket_cache["busy"] or (_rocket_cache["ts"] and now - _rocket_cache["ts"] < _ROCKET_TTL):
        return
    _rocket_cache["busy"] = True
    _rocket_cache["ts"] = now
    try:
        _rocket_cache["launch"] = await asyncio.get_event_loop().run_in_executor(None, rocket.get_next_launch)
    except Exception:
        pass
    finally:
        _rocket_cache["busy"] = False


async def _refresh_kp_if_stale():
    """Fetch NOAA's Kp forecast in the background if the cache is stale (global)."""
    now = time.monotonic()
    if _kp_cache["busy"] or (_kp_cache["ts"] and now - _kp_cache["ts"] < _KP_TTL):
        return
    _kp_cache["busy"] = True
    _kp_cache["ts"] = now
    try:
        lst = await asyncio.get_event_loop().run_in_executor(None, aurora.get_kp_forecast)
        if lst:
            _kp_cache["list"] = lst
    except Exception:
        pass
    finally:
        _kp_cache["busy"] = False


async def _refresh_sky_for(location: dict):
    """Fetch hourly cloud + day/night for one location (for the aurora forecast)."""
    key = _loc_key(location["lat"], location["lon"])
    entry = _sky_cache.setdefault(key, {"hourly": None, "ts": 0.0, "busy": False})
    now = time.monotonic()
    if entry["busy"] or (entry["ts"] and now - entry["ts"] < _SKY_TTL):
        return
    entry["busy"] = True
    entry["ts"] = now
    try:
        hourly = await asyncio.get_event_loop().run_in_executor(
            None, weather.get_hourly_sky, location["lat"], location["lon"]
        )
        if hourly:
            entry["hourly"] = hourly
    except Exception:
        pass
    finally:
        entry["busy"] = False


async def _refresh_sky_if_stale(bot, guild_id):
    """Refresh the hourly sky for a guild's configured location."""
    await _refresh_sky_for(get_display_prefs(str(guild_id))["location"])


def _build_grab_embed(gs, track) -> discord.Embed:
    """Build the DM embed for !grab / the grab button (the current track's details)."""
    title = (gs.player.current_track_title or track.title) if gs else track.title
    embed = discord.Embed(title=title or "Now Playing", color=NP_EMBED_COLOR)
    desc = []
    artist = (getattr(gs.player, "current_artist", "") or "") if gs else ""
    if artist:
        desc.append(f"by {artist}")
    if getattr(track, "url", ""):
        desc.append(track.url)
    if desc:
        embed.description = "\n".join(desc)
    if getattr(track, "thumbnail", ""):
        embed.set_thumbnail(url=track.thumbnail)
    embed.set_footer(text="Grabbed from Now Playing")
    return embed


async def _safe_delete(msg):
    """Delete a message, ignoring errors (already gone / missing perms)."""
    try:
        await msg.delete()
    except Exception:
        pass


def _discard_future(fut):
    """Retrieve and swallow a future's result/exception so a discarded in-flight
    resolve (e.g. after a failed voice connect) doesn't log 'never retrieved'."""
    def _swallow(f):
        try:
            f.result()
        except Exception:
            pass
    try:
        fut.add_done_callback(_swallow)
    except Exception:
        pass


# Per-(guild, user, action) button cooldowns. Stops machine-gunning the card
# buttons — most importantly next/prev, which resolve a track via the YouTube API
# on a cache-miss (a burst of skips = a burst of /player calls). Shuffle/loop are
# in-memory + a Discord edit; grab sends a DM; the cooldown keeps all of them civil.
_button_cooldowns: dict = {}


def _on_cooldown(guild_id, user_id, action: str, seconds: float) -> bool:
    """True (and blocks) if this user triggered this action within `seconds`."""
    key = (guild_id, user_id, action)
    now = time.monotonic()
    if now - _button_cooldowns.get(key, 0.0) < seconds:
        return True
    _button_cooldowns[key] = now
    return False


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


async def _do_update_np_embed(bot, channel, msg_id, view):
    """Helper to update the NP card (Components V2 view) in a separate task.

    Uses a partial message (no fetch round-trip) — only the message ID is needed
    to edit, and the view is re-registered for button dispatch on edit.
    """
    try:
        await channel.get_partial_message(msg_id).edit(view=view)
    except Exception as e:
        print(f"[commands] Failed to update NP card: {e}")

async def update_np_embed(bot, channel_id: int, view: "PlayerView"):
    """Edit the existing NP message card in-place (no new message)."""
    channel = bot.get_channel(channel_id)
    if not channel or not hasattr(channel, 'guild') or not channel.guild:
        return
    guild_id = channel.guild.id
    gs = bot.get_guild_state(guild_id)
    msg_id = gs.np_message_id
    if not msg_id:
        return
    asyncio.create_task(_do_update_np_embed(bot, channel, msg_id, view))


async def update_np_stopped(bot, channel_id: int):
    """Update the NP card to a stopped/queue-finished state and remove all buttons."""
    channel = bot.get_channel(channel_id)
    if not channel or not hasattr(channel, 'guild') or not channel.guild:
        return
    guild_id = channel.guild.id
    gs = bot.get_guild_state(guild_id)
    msg_id = gs.np_message_id
    if not msg_id:
        return
    view = build_player_view(bot, title="", finished=True, guild_id=guild_id)
    try:
        await channel.get_partial_message(msg_id).edit(view=view)
    except Exception as e:
        print(f"[commands] Failed to update stopped NP card: {e}")


# --- Next-track prefetch (fair-play aware, anti-ban throttled) -----------------
#
# While the current track plays, resolve the CDN URL of the track fair-play will
# pick NEXT, so the transition skips the ~1.3s resolve. Anti-ban safeguards:
#   * only ONE track ahead is ever prefetched;
#   * only one prefetch runs at a time per guild (prefetch_task guard);
#   * a global minimum interval floors how often prefetch resolves may fire, so a
#     burst of skips/queue-adds can't hammer YouTube;
#   * a track that already holds a fresh cached resolve is skipped;
#   * the cached result REPLACES the play-time resolve (AudioPlayer.play reuses it),
#     so this does NOT add API calls per track — it just moves the one resolve
#     earlier in time.
_PREFETCH_MIN_INTERVAL = 8.0  # seconds; global floor between prefetch resolves
_last_prefetch_monotonic = 0.0


async def _prefetch_next_track(bot, guild_id):
    """Resolve the fair-play-predicted next track's CDN URL in the background."""
    global _last_prefetch_monotonic
    gs = bot.get_guild_state(guild_id)
    try:
        upcoming = gs.queue.preview_fair_order(1)
        if not upcoming:
            return
        track = upcoming[0]
        if track.is_radio:
            return
        # Already have a still-fresh resolve for this exact track — don't refetch.
        if track.resolved_info and is_stream_info_fresh(track.resolved_info, track.resolved_at):
            return
        # Global rate-limit: never let prefetch resolves cluster (protects the IP).
        now = time.monotonic()
        if now - _last_prefetch_monotonic < _PREFETCH_MIN_INTERVAL:
            return
        _last_prefetch_monotonic = now

        yt_client = bot.config.get("youtube", {}).get("client", "web")
        cookies_file = bot.config.get("youtube", {}).get("cookies_file") or None
        info = await asyncio.get_event_loop().run_in_executor(
            None, get_audio_url_with_retry, track.query, yt_client, False, cookies_file
        )
        track.resolved_info = info
        track.resolved_at = time.time()
    except Exception as e:
        print(f"[commands] Prefetch failed (will resolve at play time): {e}")


def _schedule_prefetch(bot, guild_id):
    """Kick off a single-in-flight background prefetch of the next track."""
    gs = bot.get_guild_state(guild_id)
    if gs.prefetch_task and not gs.prefetch_task.done():
        return  # one already running
    gs.prefetch_task = asyncio.create_task(_prefetch_next_track(bot, guild_id))


# --- Autoplay / endless mode --------------------------------------------------
#
# When autoplay is on and the queue would otherwise drain, keep the music going
# with tracks from the last song's YouTube Mix. Anti-ban: the Mix is fetched once
# (flat metadata) and cached in gs.autoplay_pool, so many autoplay tracks come
# from a single call; each track's stream URL is then resolved lazily by the same
# prefetch path. autoplay_history avoids repeats.
async def _autoplay_pick(bot, guild_id, seed):
    """Pick one related track for autoplay, refilling the Mix pool if needed.
    Returns a Track (attributed to the seed's requester) or None."""
    gs = bot.get_guild_state(guild_id)
    try:
        if not gs.autoplay_pool:
            yt_client = bot.config.get("youtube", {}).get("client", "web")
            seed_url = seed.url or seed.query
            related = await asyncio.get_event_loop().run_in_executor(
                None, get_related_tracks, seed_url, yt_client, 25
            )
            gs.autoplay_pool = [t for t in related if t.get("url") and t["url"] not in gs.autoplay_history]
        while gs.autoplay_pool:
            cand = gs.autoplay_pool.pop(0)
            if cand["url"] in gs.autoplay_history:
                continue
            gs.autoplay_history.add(cand["url"])
            if len(gs.autoplay_history) > 200:  # bound memory; keep the recent tail
                gs.autoplay_history = set(list(gs.autoplay_history)[-100:])
            return Track(query=cand["url"], title=cand.get("title", "Unknown"),
                         requested_by=seed.requested_by, url=cand["url"])
    except Exception as e:
        print(f"[commands] Autoplay pick failed: {e}")
    return None


async def _autoplay_topup(bot, guild_id):
    """If autoplay is on and the queue is empty, proactively queue one related
    track (while the current one still plays) so the transition stays seamless."""
    gs = bot.get_guild_state(guild_id)
    if not gs.autoplay or not gs.queue.is_empty():
        return
    seed = gs.queue.current
    if not seed or seed.is_radio:
        return
    track = await _autoplay_pick(bot, guild_id, seed)
    if track and gs.autoplay and gs.queue.is_empty():
        gs.queue.add(track)
        _schedule_prefetch(bot, guild_id)


def _schedule_autoplay_topup(bot, guild_id):
    """Single-in-flight proactive autoplay top-up (no-op when autoplay is off)."""
    gs = bot.get_guild_state(guild_id)
    if not gs.autoplay:
        return
    if gs.autoplay_task and not gs.autoplay_task.done():
        return
    gs.autoplay_task = asyncio.create_task(_autoplay_topup(bot, guild_id))


async def _resolve_track_info(bot, channel_id: int, track: Track):
    """Silently resolve missing track metadata via yt-dlp and update the NP embed.

    Also caches the full resolve (CDN URL + metadata) on the Track so the eventual
    play() reuses it instead of resolving again — one resolve, not two.
    """
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
        track.resolved_info = info
        track.resolved_at = time.time()

        _ch = bot.get_channel(channel_id)
        guild_id = _ch.guild.id if _ch and hasattr(_ch, 'guild') and _ch.guild else None
        if not guild_id:
            return
        gs = bot.get_guild_state(guild_id)
        current = gs.queue.current
        if current:
            requester_name = _get_requester_name(bot, current.requested_by)
            view = build_player_view(
                bot,
                current.title,
                thumbnail=current.thumbnail,
                url=current.url,
                requester_name=requester_name,
                queue_tracks=gs.queue.preview_fair_order(),
                guild_id=guild_id,
            )
            asyncio.create_task(update_np_embed(bot, channel_id, view))
    except Exception as e:
        print(f"[commands] Failed background resolve for {track.query}: {e}")

async def send_new_np(bot, channel_id: int, view: "PlayerView"):
    channel = bot.get_channel(channel_id)
    if not channel or not hasattr(channel, 'guild') or not channel.guild:
        return
    guild_id = channel.guild.id
    gs = bot.get_guild_state(guild_id)

    # Keep the card flourishes reasonably fresh (background, no-op if recent).
    asyncio.create_task(_refresh_weather_if_stale(bot, guild_id))
    asyncio.create_task(_refresh_sky_if_stale(bot, guild_id))
    asyncio.create_task(_refresh_f1_if_stale())
    asyncio.create_task(_refresh_rocket_if_stale())
    asyncio.create_task(_refresh_kp_if_stale())

    # Reset votes whenever a new NP message is sent / track changes
    gs.prev_votes.clear()
    gs.playpause_votes.clear()
    gs.stop_votes.clear()
    gs.next_votes.clear()

    # Delete the old card concurrently with sending the new one (no fetch round-trip).
    old_msg_id = gs.np_message_id
    if old_msg_id:
        asyncio.create_task(_safe_delete(channel.get_partial_message(old_msg_id)))

    try:
        new_msg = await channel.send(view=view)
        gs.np_message_id = new_msg.id
    except Exception as e:
        print(f"[commands] Failed to send NP message: {e}")

import math as _math

def _check_vote(bot, guild, user, action: str) -> tuple[bool, str]:
    """Shared fairness vote logic for both button interactions and prefix commands.

    Returns (passes, denial_message). denial_message is empty string when passes=True.
    """
    user_id = str(user.id)
    owner_id = str(bot.config.get("owner_id", ""))
    if user_id == owner_id:
        return True, ""

    bot_voice = guild.voice_client
    if not bot_voice or not bot_voice.channel:
        return True, ""

    vc_members = [m for m in bot_voice.channel.members if not m.bot]
    total = len(vc_members)
    if total <= 1:
        return True, ""

    gs = bot.get_guild_state(guild.id)
    current = gs.queue.current

    if action in ["next", "prev", "playpause"]:
        if current and current.requested_by == user_id:
            return True, ""
    elif action == "stop":
        all_reqs = set()
        if current:
            all_reqs.add(current.requested_by)
        for t in gs.queue.list():
            all_reqs.add(t.requested_by)
        if len(all_reqs) == 1 and user_id in all_reqs:
            return True, ""

    pct = gs.fairness_pct
    votes_set = getattr(gs, f"{action}_votes")
    votes_set.add(user_id)
    req = max(1, _math.ceil((pct / 100.0) * total))

    if len(votes_set) >= req:
        votes_set.clear()
        return True, ""

    msg = f"`{action}` vote from {user.display_name} recorded! ({len(votes_set)}/{req} votes needed, fairness: {pct}%)"
    return False, msg


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

    @commands.hybrid_command(name="search", description="Search YouTube and pick a result to play")
    @app_commands.describe(query="Song name or keywords to search for")
    async def search(self, ctx: commands.Context, *, query: str = None):
        if not await check_channel(ctx):
            return

        if not query:
            await ctx.send(f"Usage: `{self.bot.command_prefix}search <keywords>`")
            return

        if not ctx.author.voice or not ctx.author.voice.channel:
            await ctx.send("You need to be in a voice channel.")
            return

        # Block queueing from outside the bot's current voice channel (mirrors button controls)
        bot_voice = ctx.guild.voice_client
        if bot_voice and bot_voice.is_connected() and ctx.author.voice.channel != bot_voice.channel:
            await ctx.send("You must be in the same voice channel as the bot to queue songs.")
            return

        query = _strip_ytsearch_prefix(query)
        await ctx.defer()
        status_msg = await ctx.send("Searching…")
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

    @commands.hybrid_command(name="play", description="Play a song or playlist from a URL or search query")
    @app_commands.describe(query="URL or search keywords to play")
    async def play(self, ctx: commands.Context, *, query: str = None):
        if not await check_channel(ctx):
            return

        if not query:
            await ctx.send(f"Usage: `{self.bot.command_prefix}play <url or keywords>`")
            return

        if not ctx.author.voice or not ctx.author.voice.channel:
            await ctx.send("You need to be in a voice channel.")
            return

        voice_channel = ctx.author.voice.channel
        # Block queueing from outside the bot's current voice channel (mirrors button controls)
        bot_voice = ctx.guild.voice_client
        if bot_voice and bot_voice.is_connected() and voice_channel != bot_voice.channel:
            await ctx.send("You must be in the same voice channel as the bot to queue songs.")
            return
        guild_id = str(ctx.guild.id)
        gs = self.bot.get_guild_state(ctx.guild.id)

        query = _strip_ytsearch_prefix(query)

        # Acknowledge the slash interaction up front — voice-connect + resolve can
        # exceed the 3s deadline. Plain-text queries no longer run a separate
        # pre-search: get_audio_url resolves `ytsearch:<text>` in a single pass
        # (one YoutubeDL session instead of a flat ytsearch5 hop + a full resolve).
        if ctx.interaction:
            await ctx.defer()

        # ---------------------------------------------------------------
        # Spotify: expand the link into "artist - title" searches resolved on
        # YouTube (Spotify audio is DRM-protected and can't be streamed directly).
        # ---------------------------------------------------------------
        if is_spotify_url(query):
            sp = self.bot.config.get("spotify", {}) or {}
            try:
                resolved = await asyncio.get_event_loop().run_in_executor(
                    None, resolve_spotify, query, sp.get("client_id"), sp.get("client_secret")
                )
            except SpotifyError as e:
                await ctx.send(str(e))
                return
            except Exception as e:
                await ctx.send(f"Couldn't read that Spotify link: {e}")
                return
            sp_tracks = resolved.get("tracks") or []
            if not sp_tracks:
                await ctx.send("No tracks found on that Spotify link.")
                return
            if len(sp_tracks) == 1:
                query = sp_tracks[0]["query"]  # single track → fall through to the search flow
            else:
                await self._queue_spotify(ctx, gs, resolved)
                return

        # ---------------------------------------------------------------
        # Playlist detection: play first track, offer to add the rest
        # ---------------------------------------------------------------
        if is_playlist_url(query):
            loop = asyncio.get_event_loop()
            yt_client = self.bot.config.get("youtube", {}).get("client", "web")
            channel_id = ctx.channel.id
            user_id = str(ctx.author.id)
            voice_client = ctx.guild.voice_client
            voice_actually_playing = (
                gs.player.is_playing and voice_client and voice_client.is_playing()
            )

            def _pending_and_expire(remaining, playlist_title, current_title):
                """Store the pending-playlist offer and schedule its 120s expiry."""
                count = len(remaining)
                self.bot.pending_playlists[str(channel_id)] = {
                    "query": query, "user_id": user_id, "guild_id": guild_id,
                    "channel_id": channel_id, "tracks": remaining,
                    "playlist_title": playlist_title,
                }

                async def _expire_playlist(ch_id: int):
                    await asyncio.sleep(120)
                    if self.bot.pending_playlists.pop(str(ch_id), None):
                        try:
                            expired_extra = (
                                f"**{playlist_title}** had **{count}** more tracks.\n"
                                f"~~Click Load playlist~~ *(expired)*"
                            )
                            expired_view = build_player_view(self.bot, current_title, expired_extra, guild_id=ctx.guild.id)
                            await update_np_embed(self.bot, ch_id, expired_view)
                        except Exception:
                            pass

                asyncio.create_task(_expire_playlist(channel_id))

            if voice_actually_playing:
                # Already playing — no rush to start track 1; enumerate fully then queue.
                status_msg = await ctx.send("Fetching playlist info…")
                try:
                    playlist_info = await loop.run_in_executor(None, extract_playlist_info, query, yt_client)
                except Exception as e:
                    await status_msg.edit(content=f"Error fetching playlist: {e}")
                    return
                tracks = playlist_info["tracks"]
                if not tracks:
                    await status_msg.edit(content="No tracks found in this playlist.")
                    return
                playlist_title = playlist_info["title"]
                remaining_tracks = tracks[1:]

                track = Track(query=tracks[0]["url"], title=tracks[0]["title"], requested_by=user_id, url=tracks[0]["url"])
                gs.queue.add(track)
                _schedule_prefetch(self.bot, ctx.guild.id)
                asyncio.create_task(_safe_delete(status_msg))

                count = len(remaining_tracks)
                if count > 0:
                    extra = (f"**{playlist_title}** has **{count}** more tracks.\n"
                             f"Click 'Load playlist' to add them to the queue.")
                    _pending_and_expire(remaining_tracks, playlist_title, gs.queue.current.title if gs.queue.current else "")
                else:
                    extra = f"Added **{playlist_title}** (1 track) to the queue."

                current = gs.queue.current
                view = build_player_view(self.bot, current.title, extra,
                                         thumbnail=current.thumbnail, url=current.url,
                                         requester_name=f"<@{current.requested_by}>" if getattr(current, 'requested_by', None) else "",
                                         queue_tracks=gs.queue.preview_fair_order(), guild_id=ctx.guild.id)
                await update_np_embed(self.bot, channel_id, view)
                return

            # Not playing: EARLY START — enumerate the full playlist in the background
            # while a fast limit=1 fetch gets track 1, so audio starts ~1-2.5s sooner.
            status_msg = await ctx.send("Loading playlist…")
            full_fut = loop.run_in_executor(None, extract_playlist_info, query, yt_client)
            try:
                first_info = await loop.run_in_executor(None, extract_playlist_info, query, yt_client, 1)
            except Exception as e:
                _discard_future(full_fut)
                await status_msg.edit(content=f"Error fetching playlist: {e}")
                return
            first_tracks = first_info["tracks"]
            if not first_tracks:
                _discard_future(full_fut)
                await status_msg.edit(content="No tracks found in this playlist.")
                return
            playlist_title = first_info["title"]
            first_track_info = first_tracks[0]

            # Join voice if not already connected
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
                    _discard_future(full_fut)
                    await status_msg.edit(content=f"Failed to join voice channel: {e}")
                    return

            # Start track 1 immediately (do not wait for full enumeration).
            try:
                played = await gs.player.play(first_track_info["url"])
                title = played["title"]
                track_thumbnail = played.get("thumbnail", "")
                track_url = played.get("webpage_url", "")
                gs.queue.current = Track(query=first_track_info["url"], title=title,
                                         requested_by=user_id, thumbnail=track_thumbnail, url=track_url)
            except Exception as e:
                _discard_future(full_fut)
                await status_msg.edit(content=f"Error playing first track: {e}")
                return

            # Audio is now playing — show the card and arm auto-next before the (possibly
            # slower) full enumeration lands.
            view = build_player_view(self.bot, title, f"From playlist: **{playlist_title}**",
                                     thumbnail=track_thumbnail, url=track_url,
                                     requester_name=f"<@{user_id}>",
                                     queue_tracks=gs.queue.preview_fair_order(), guild_id=ctx.guild.id)
            asyncio.create_task(_safe_delete(status_msg))
            await send_new_np(self.bot, channel_id, view)
            _start_auto_next(self.bot, channel_id, ctx.guild.id)

            # Finish enumerating; when done, offer the rest via the Load button.
            try:
                full_info = await full_fut
            except Exception as e:
                print(f"[commands] Playlist enumeration failed after start: {e}")
                return
            remaining_tracks = full_info["tracks"][1:] if full_info.get("tracks") else []
            if not remaining_tracks:
                return
            _pending_and_expire(remaining_tracks, playlist_title, title)
            count = len(remaining_tracks)
            extra = (f"**{playlist_title}** has **{count}** more tracks.\n"
                     f"Click 'Load playlist' to add them to the queue.")
            current = gs.queue.current
            if current:
                view = build_player_view(self.bot, current.title, extra,
                                         thumbnail=current.thumbnail, url=current.url,
                                         requester_name=f"<@{current.requested_by}>" if getattr(current, 'requested_by', None) else "",
                                         queue_tracks=gs.queue.preview_fair_order(), guild_id=ctx.guild.id)
                await update_np_embed(self.bot, channel_id, view)
            return

        # ---------------------------------------------------------------
        # Single track flow
        # ---------------------------------------------------------------
        user_id = str(ctx.author.id)
        channel_id = ctx.channel.id
        loop = asyncio.get_event_loop()

        voice_client = ctx.guild.voice_client
        voice_actually_playing = (
            gs.player.is_playing
            and voice_client
            and voice_client.is_playing()
        )

        if voice_actually_playing:
            # Queue-add path: bot already playing something.
            track = Track(query=query, title=query, requested_by=user_id)
            gs.queue.add(track)

            # Resolve title/thumbnail in the background (also caches the CDN URL for play).
            asyncio.create_task(_resolve_track_info(self.bot, channel_id, track))

            # Ack — also closes a deferred slash interaction (keeps the user's command in chat).
            try:
                await ctx.send("Added to queue.", delete_after=5)
            except Exception:
                pass

            current = gs.queue.current
            if current:
                requester_name = f"<@{current.requested_by}>" if getattr(current, 'requested_by', None) else ""
                view = build_player_view(
                    self.bot,
                    current.title,
                    thumbnail=current.thumbnail,
                    url=current.url,
                    requester_name=requester_name,
                    queue_tracks=gs.queue.preview_fair_order(),
                    guild_id=ctx.guild.id,
                )
                await update_np_embed(self.bot, channel_id, view)
            return

        # Start-playback path: overlap the URL resolve with the voice connect
        # (they are independent — connect needs no resolved URL, resolve needs no voice).
        yt_cfg = self.bot.config.get("youtube", {})
        resolve_fut = loop.run_in_executor(
            None, get_audio_url_with_retry, query,
            yt_cfg.get("client", "web"), self.bot.config.get("debug", False),
            yt_cfg.get("cookies_file") or None,
        )

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
                _discard_future(resolve_fut)  # let the in-flight resolve finish and drop it
                await ctx.send(f"Failed to join voice channel: {e}")
                return

        status_msg = await ctx.send("Resolving…")
        try:
            info = await resolve_fut
        except Exception as e:
            await status_msg.edit(content=f"Error playing track: {_friendly_ytdlp_error(e)}")
            return

        resolved_at = time.time()
        try:
            played = await gs.player.play(query, info, resolved_at)
            title = played["title"]
            track_thumbnail = played.get("thumbnail", "")
            track_url = played.get("webpage_url", "")

            # Store as current track so queue-add updates can reference it
            gs.queue.current = Track(
                query=query, title=title, requested_by=user_id,
                thumbnail=track_thumbnail, url=track_url,
            )

            view = build_player_view(self.bot, title,
                                    thumbnail=track_thumbnail,
                                    url=track_url,
                                    requester_name=f"<@{user_id}>",
                                    queue_tracks=gs.queue.preview_fair_order(),
                                    guild_id=ctx.guild.id)
            asyncio.create_task(_safe_delete(status_msg))
            await send_new_np(self.bot, channel_id, view)
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

        # Block queueing from outside the bot's current voice channel (mirrors button controls)
        bot_voice = ctx.guild.voice_client
        if bot_voice and bot_voice.is_connected() and ctx.author.voice.channel != bot_voice.channel:
            await ctx.send("You must be in the same voice channel as the bot to start a station.")
            return

        await ctx.defer()

        if not query:
            # Discovery mode: let user narrow by country + genre before loading stations
            status_msg = await ctx.send("Discover radio")
            embed = discord.Embed(
                title="Discover radio",
                description="Pick a country and genre to browse stations, or leave either as \"Any\" to browse all.",
                color=0x3498db,
            )
            view = RadioDiscoveryView(self.bot, ctx, status_msg)
            await status_msg.edit(content=None, embed=embed, view=view)
            return

        # Search mode: go straight to byname results
        status_msg = await ctx.send("Searching stations…")
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
        passes, vote_msg = _check_vote(self.bot, ctx.guild, ctx.author, "playpause")
        if not passes:
            await ctx.send(vote_msg)
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
        passes, vote_msg = _check_vote(self.bot, ctx.guild, ctx.author, "playpause")
        if not passes:
            await ctx.send(vote_msg)
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

        passes, vote_msg = _check_vote(self.bot, ctx.guild, ctx.author, "stop")
        if not passes:
            await ctx.send(vote_msg)
            return

        gs = self.bot.get_guild_state(ctx.guild.id)
        channel_id = gs.current_text_channel_id or ctx.channel.id
        gs.player.stop_playback()
        gs.queue.clear()
        await gs.player.disconnect()
        await update_np_stopped(self.bot, channel_id)
        await ctx.send("Stopped playback and left voice.")

    @commands.command(name="skip")
    async def skip(self, ctx: commands.Context):
        if not await check_channel(ctx):
            return
        if not ctx.guild:
            return

        passes, vote_msg = _check_vote(self.bot, ctx.guild, ctx.author, "next")
        if not passes:
            await ctx.send(vote_msg)
            return

        gs = self.bot.get_guild_state(ctx.guild.id)
        channel_id = ctx.channel.id
        # Cancel the existing auto-next task and invalidate its generation
        if gs.auto_next_task and not gs.auto_next_task.done():
            gs.auto_next_task.cancel()
            gs.auto_next_task = None
        gs.auto_next_gen += 1
        gs.player.stop_playback()
        next_track = gs.queue.next(force=True)  # manual skip bypasses track-loop
        if next_track:
            try:
                info = await gs.player.play(next_track.query, next_track.resolved_info, next_track.resolved_at)
                title = info["title"]
                next_track.title = title
                next_track.thumbnail = info.get("thumbnail", "")
                next_track.url = info.get("webpage_url", "")
                view = build_player_view(self.bot, title,
                                        thumbnail=next_track.thumbnail,
                                        url=next_track.url,
                                        requester_name=_get_requester_name(self.bot, next_track.requested_by),
                                        queue_tracks=gs.queue.preview_fair_order(),
                                        guild_id=ctx.guild.id)
                await ctx.send("Skipped.", delete_after=3)
                await send_new_np(self.bot, channel_id, view)
                _start_auto_next(self.bot, channel_id, ctx.guild.id)
            except Exception as e:
                await ctx.send(f"Skipping track: {_friendly_ytdlp_error(e)}")
        else:
            await ctx.send("Skipped. Queue is empty.")
            channel_id = gs.current_text_channel_id or ctx.channel.id
            await update_np_stopped(self.bot, channel_id)

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
                    req_name = _get_requester_name(self.bot, t.requested_by, ctx.guild)
                    if req_name:
                        req_tag = f" — *{req_name}*"
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
        await self._refresh_np_card(ctx)

    async def _refresh_np_card(self, ctx):
        """Rebuild the Now-Playing card in place from current state (queue edits, loop)."""
        gs = self.bot.get_guild_state(ctx.guild.id)
        current = gs.queue.current
        if not current:
            return
        view = build_player_view(
            self.bot, current.title,
            thumbnail=current.thumbnail, url=current.url,
            requester_name=_get_requester_name(self.bot, current.requested_by, ctx.guild),
            queue_tracks=gs.queue.preview_fair_order(), guild_id=ctx.guild.id,
        )
        await update_np_embed(self.bot, ctx.channel.id, view)

    async def _queue_spotify(self, ctx, gs, resolved):
        """Play/queue a multi-track Spotify playlist or album as YouTube searches.

        Tracks are queued as plain-text "artist - title" queries (title known up
        front from Spotify), resolved to YouTube lazily on play/prefetch — so a
        200-track playlist adds instantly without a burst of YouTube calls.
        """
        title = resolved.get("title") or "Spotify"
        sp_tracks = resolved["tracks"]
        user_id = str(ctx.author.id)
        channel_id = ctx.channel.id
        guild_id = str(ctx.guild.id)
        voice_channel = ctx.author.voice.channel
        voice_client = ctx.guild.voice_client
        playing = gs.player.is_playing and voice_client and voice_client.is_playing()

        def mk(t):
            return Track(query=t["query"], title=t["title"], requested_by=user_id)

        if playing:
            for t in sp_tracks:
                gs.queue.add(mk(t))
            _schedule_prefetch(self.bot, ctx.guild.id)
            await ctx.send(f"Queued **{len(sp_tracks)}** tracks from **{title}**.")
            await self._refresh_np_card(ctx)
            return

        # Join voice if not already connected
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
                await ctx.send(f"Failed to join voice channel: {e}")
                return

        status = await ctx.send(f"Loading **{title}**…")
        first = sp_tracks[0]
        try:
            played = await gs.player.play(first["query"])
        except Exception as e:
            await status.edit(content=f"Error playing first track: {_friendly_ytdlp_error(e)}")
            return
        gs.queue.current = Track(query=first["query"], title=played["title"], requested_by=user_id,
                                 thumbnail=played.get("thumbnail", ""), url=played.get("webpage_url", ""))
        for t in sp_tracks[1:]:
            gs.queue.add(mk(t))
        view = build_player_view(self.bot, gs.queue.current.title,
                                 f"From Spotify: **{title}**",
                                 thumbnail=gs.queue.current.thumbnail, url=gs.queue.current.url,
                                 requester_name=_get_requester_name(self.bot, user_id, ctx.guild),
                                 queue_tracks=gs.queue.preview_fair_order(), guild_id=ctx.guild.id)
        asyncio.create_task(_safe_delete(status))
        await send_new_np(self.bot, channel_id, view)
        _start_auto_next(self.bot, channel_id, ctx.guild.id)

    @commands.hybrid_command(name="grab", description="DM yourself the currently playing track")
    async def grab(self, ctx: commands.Context):
        if not await check_channel(ctx):
            return
        if not ctx.guild:
            return
        gs = self.bot.get_guild_state(ctx.guild.id)
        current = gs.queue.current
        if not current or not gs.player.is_playing:
            await ctx.send("Nothing is playing right now.")
            return
        try:
            await ctx.author.send(embed=_build_grab_embed(gs, current))
            await ctx.send("Sent it to your DMs.", ephemeral=True)
        except discord.Forbidden:
            await ctx.send(
                "I couldn't DM you — enable **Allow direct messages from server members**.",
                ephemeral=True,
            )

    @commands.hybrid_command(name="setlocation", description="Set the city shown for weather on the player card")
    @app_commands.describe(city="City name, e.g. 'Riga' or 'London'")
    async def setlocation(self, ctx: commands.Context, *, city: str = None):
        if not await check_channel(ctx):
            return
        if not ctx.guild:
            return
        if not await self._check_admin(ctx):
            return
        gid = str(ctx.guild.id)
        if not city:
            cur = get_weather_location(gid)
            await ctx.send(f"Weather location: **{cur['name']}**. Usage: `{self.bot.command_prefix}setlocation <city>`")
            return
        geo = await asyncio.get_event_loop().run_in_executor(None, weather.geocode, city)
        if not geo:
            await ctx.send(f"Couldn't find '{city[:60]}'. Try another spelling or add a country, e.g. `Riga, LV`.")
            return
        name, lat, lon = geo
        set_weather_location(gid, name, lat, lon)
        new_loc = {"name": name, "lat": lat, "lon": lon}
        await _refresh_weather_for(new_loc)
        await _refresh_sky_for(new_loc)
        await ctx.send(f"Weather location set to **{name}**.")
        await self._refresh_np_card(ctx)

    @commands.hybrid_command(name="settimezone", description="Set the timezone for F1 race times (IANA name)")
    @app_commands.describe(tz="IANA timezone, e.g. 'Europe/Riga' or 'America/New_York'")
    async def settimezone(self, ctx: commands.Context, tz: str = None):
        if not await check_channel(ctx):
            return
        if not ctx.guild:
            return
        if not await self._check_admin(ctx):
            return
        gid = str(ctx.guild.id)
        if not tz:
            await ctx.send(f"Timezone: **{get_timezone(gid)}**. Usage: `{self.bot.command_prefix}settimezone <IANA tz>` (e.g. `Europe/Riga`)")
            return
        try:
            from zoneinfo import ZoneInfo
            ZoneInfo(tz)
        except Exception:
            await ctx.send(f"Unknown timezone '{tz[:60]}'. Use an IANA name like `Europe/Riga` or `America/New_York`.")
            return
        set_timezone(gid, tz)
        await ctx.send(f"Timezone set to **{tz}** — F1 race times will show in this zone.")
        await self._refresh_np_card(ctx)

    @commands.hybrid_command(name="loop", description="Set repeat mode: off, track, or queue")
    @app_commands.describe(mode="off, track (repeat current), or queue (repeat all) — omit to cycle")
    async def loop(self, ctx: commands.Context, mode: str = None):
        if not await check_channel(ctx):
            return
        if not ctx.guild:
            return
        gs = self.bot.get_guild_state(ctx.guild.id)
        if mode is None:
            gs.queue.cycle_loop()
        else:
            aliases = {
                "off": "off", "none": "off", "no": "off",
                "track": "track", "one": "track", "song": "track", "current": "track", "1": "track",
                "queue": "queue", "all": "queue", "q": "queue",
            }
            key = aliases.get(mode.strip().lower())
            if key is None:
                await ctx.send(f"Usage: `{self.bot.command_prefix}loop [off|track|queue]`")
                return
            gs.queue.loop_mode = key
        labels = {
            "off": "Repeat **off**.",
            "track": "Repeating the **current track**.",
            "queue": "Repeating the **whole queue**.",
        }
        await ctx.send(labels[gs.queue.loop_mode])
        await self._refresh_np_card(ctx)

    @commands.hybrid_command(name="remove", description="Remove a track from the queue by its position")
    @app_commands.describe(position="Queue position to remove (see the queue list)")
    async def remove(self, ctx: commands.Context, position: int = None):
        if not await check_channel(ctx):
            return
        if not ctx.guild:
            return
        if position is None:
            await ctx.send(f"Usage: `{self.bot.command_prefix}remove <position>`")
            return
        gs = self.bot.get_guild_state(ctx.guild.id)
        removed = gs.queue.remove(position)
        if not removed:
            await ctx.send(f"No track at position {position}.")
            return
        await ctx.send(f"Removed **{removed.title}** from the queue.")
        await self._refresh_np_card(ctx)

    @commands.hybrid_command(name="move", description="Move a queued track to a new position")
    @app_commands.describe(from_pos="Current position", to_pos="New position")
    async def move(self, ctx: commands.Context, from_pos: int = None, to_pos: int = None):
        if not await check_channel(ctx):
            return
        if not ctx.guild:
            return
        if from_pos is None or to_pos is None:
            await ctx.send(f"Usage: `{self.bot.command_prefix}move <from> <to>`")
            return
        gs = self.bot.get_guild_state(ctx.guild.id)
        moved = gs.queue.move(from_pos, to_pos)
        if not moved:
            await ctx.send("Invalid positions.")
            return
        await ctx.send(f"Moved **{moved.title}** to position {to_pos}.")
        await self._refresh_np_card(ctx)

    @commands.hybrid_command(name="skipto", description="Skip straight to a track in the queue")
    @app_commands.describe(position="Queue position to jump to")
    async def skipto(self, ctx: commands.Context, position: int = None):
        if not await check_channel(ctx):
            return
        if not ctx.guild:
            return
        if position is None:
            await ctx.send(f"Usage: `{self.bot.command_prefix}skipto <position>`")
            return
        gs = self.bot.get_guild_state(ctx.guild.id)
        if not gs.queue.skip_to(position):
            await ctx.send(f"No track at position {position}.")
            return

        channel_id = ctx.channel.id
        if gs.auto_next_task and not gs.auto_next_task.done():
            gs.auto_next_task.cancel()
            gs.auto_next_task = None
        gs.auto_next_gen += 1
        gs.player.stop_playback()
        next_track = gs.queue.next(force=True)
        if not next_track:
            await ctx.send("Nothing to play.")
            await update_np_stopped(self.bot, channel_id)
            return
        try:
            info = await gs.player.play(next_track.query, next_track.resolved_info, next_track.resolved_at)
            next_track.title = info["title"]
            next_track.thumbnail = info.get("thumbnail", "")
            next_track.url = info.get("webpage_url", "")
            view = build_player_view(self.bot, next_track.title,
                                     thumbnail=next_track.thumbnail, url=next_track.url,
                                     requester_name=_get_requester_name(self.bot, next_track.requested_by, ctx.guild),
                                     queue_tracks=gs.queue.preview_fair_order(), guild_id=ctx.guild.id)
            await ctx.send(f"Jumped to **{next_track.title}**.", delete_after=3)
            await send_new_np(self.bot, channel_id, view)
            _start_auto_next(self.bot, channel_id, ctx.guild.id)
        except Exception as e:
            await ctx.send(f"Skipping track: {_friendly_ytdlp_error(e)}")

    @commands.hybrid_command(name="clear", description="Clear the upcoming queue (keeps the current track)")
    async def clear(self, ctx: commands.Context):
        if not await check_channel(ctx):
            return
        if not ctx.guild:
            return
        gs = self.bot.get_guild_state(ctx.guild.id)
        n = gs.queue.clear_upcoming()
        await ctx.send(f"Cleared **{n}** track(s) from the queue." if n else "The queue is already empty.")
        await self._refresh_np_card(ctx)

    @commands.hybrid_command(name="dedupe", description="Remove duplicate tracks from the queue")
    async def dedupe(self, ctx: commands.Context):
        if not await check_channel(ctx):
            return
        if not ctx.guild:
            return
        gs = self.bot.get_guild_state(ctx.guild.id)
        n = gs.queue.dedupe()
        await ctx.send(f"Removed **{n}** duplicate(s) from the queue." if n else "No duplicates in the queue.")
        await self._refresh_np_card(ctx)

    @commands.hybrid_command(name="autoplay", description="Keep playing related tracks when the queue ends")
    @app_commands.describe(mode="on or off — omit to toggle")
    async def autoplay(self, ctx: commands.Context, mode: str = None):
        if not await check_channel(ctx):
            return
        if not ctx.guild:
            return
        gs = self.bot.get_guild_state(ctx.guild.id)
        if mode is None:
            gs.autoplay = not gs.autoplay
        else:
            m = mode.strip().lower()
            if m in ("on", "enable", "true", "yes", "1"):
                gs.autoplay = True
            elif m in ("off", "disable", "false", "no", "0"):
                gs.autoplay = False
            else:
                await ctx.send(f"Usage: `{self.bot.command_prefix}autoplay [on|off]`")
                return
        if gs.autoplay:
            await ctx.send("Autoplay **on** — I'll keep the music going with related tracks when the queue runs out.")
            _schedule_autoplay_topup(self.bot, ctx.guild.id)  # top up now if idle-queued
        else:
            gs.autoplay_pool = []  # drop the cached Mix so re-enabling reseeds fresh
            await ctx.send("Autoplay **off**.")
        await self._refresh_np_card(ctx)

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

        await ctx.send(f"Added **{len(tracks)}** tracks to the queue.")

        # Prefetch the next track's CDN URL (no-op if playback start below already did).
        _schedule_prefetch(self.bot, ctx.guild.id)

        # Start playback if nothing is currently playing
        if not gs.player.is_playing:
            next_track = gs.queue.next()
            if next_track:
                try:
                    info = await gs.player.play(next_track.query, next_track.resolved_info, next_track.resolved_at)
                    title = info["title"]
                    next_track.title = title
                    next_track.thumbnail = info.get("thumbnail", "")
                    next_track.url = info.get("webpage_url", "")
                    view = build_player_view(self.bot, title,
                                            thumbnail=next_track.thumbnail,
                                            url=next_track.url,
                                            requester_name=f"<@{next_track.requested_by}>",
                                            queue_tracks=gs.queue.preview_fair_order(),
                                            guild_id=ctx.guild.id)
                    await send_new_np(self.bot, channel_id, view)
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
            f"`{p}play <url or keywords>` — Play from YouTube, SoundCloud, or Spotify (or search text)\n"
            f"`{p}search <keywords>` — Search YouTube and pick a result from a dropdown\n"
            f"`{p}grab` — DM yourself the currently playing track\n"
            f"`{p}pause` — Pause playback\n"
            f"`{p}resume` — Resume paused playback\n"
            f"`{p}skip` — Skip the current track\n"
            f"`{p}stop` — Stop playback, clear queue, and leave voice\n"
            f"`{p}queue` — Show the current queue\n"
            f"`{p}shuffle` — Shuffle the current queue\n"
            f"`{p}loop [off|track|queue]` — Repeat the current track or the whole queue\n"
            f"`{p}remove <pos>` — Remove a track from the queue\n"
            f"`{p}move <from> <to>` — Reorder a queued track\n"
            f"`{p}skipto <pos>` — Jump straight to a queued track\n"
            f"`{p}clear` — Clear the upcoming queue (keeps the current track)\n"
            f"`{p}dedupe` — Remove duplicate tracks from the queue\n"
            f"`{p}autoplay [on|off]` — Keep playing related tracks when the queue ends\n"
            f"`{p}loadall` — Load all remaining tracks from the last pending playlist\n"
            f"`{p}radio` — Browse internet radio by region/country/genre\n"
            f"`{p}radio <name>` — Search 30k+ radio stations by name\n"
            f"`{p}bitrate [kbps]` — Show or set audio encoding bitrate\n"
            f"`{p}eq [bass|treble <N> | preset | reset]` — Per-guild equalizer, -10..+10 dB *(admin only)*\n"
            f"`{p}setlocation <city>` — Set the city for the weather line on the player *(admin only)*\n"
            f"`{p}settimezone <tz>` — Set the timezone for F1 race times, e.g. Europe/Riga *(admin only)*\n"
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
    # Current track just (re)started — prefetch the predicted next track's CDN URL,
    # and (if autoplay is on and nothing is queued) proactively queue a related one.
    _schedule_prefetch(bot, guild_id)
    _schedule_autoplay_topup(bot, guild_id)


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
            prev_current = gs.queue.current  # track that just finished (autoplay seed)
            next_track = gs.queue.next()
            if not next_track:
                # Autoplay: keep the music going with a related track before giving up
                # (fallback if the proactive top-up didn't fill the queue in time).
                if gs.autoplay and prev_current and not prev_current.is_radio:
                    fill = await _autoplay_pick(bot, guild_id, prev_current)
                    if fill:
                        gs.queue.add(fill)
                        next_track = gs.queue.next()
                if not next_track:
                    # Queue drained — update embed to stopped state and strip buttons
                    await update_np_stopped(bot, channel_id)
                    break
            try:
                info = await gs.player.play(
                    next_track.query, next_track.resolved_info, next_track.resolved_at
                )
                title = info["title"]
                next_track.title = title
                next_track.thumbnail = info.get("thumbnail", "")
                next_track.url = info.get("webpage_url", "")
                consecutive_errors = 0  # reset on success
                view = build_player_view(bot, title,
                                        thumbnail=next_track.thumbnail,
                                        url=next_track.url,
                                        requester_name=_get_requester_name(bot, next_track.requested_by),
                                        queue_tracks=gs.queue.preview_fair_order(),
                                        guild_id=guild_id)
                await send_new_np(bot, channel_id, view)
                # Prefetch the following track while this one plays; if autoplay is on
                # and nothing is queued, proactively queue a related track too.
                _schedule_prefetch(bot, guild_id)
                _schedule_autoplay_topup(bot, guild_id)
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
    # Warm the flourish caches so the first card shows them.
    asyncio.create_task(_refresh_weather_for(dict(DEFAULT_WEATHER_LOCATION)))
    asyncio.create_task(_refresh_sky_for(dict(DEFAULT_WEATHER_LOCATION)))
    asyncio.create_task(_refresh_f1_if_stale())
    asyncio.create_task(_refresh_rocket_if_stale())
    asyncio.create_task(_refresh_kp_if_stale())
