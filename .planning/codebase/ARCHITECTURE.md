# Architecture

**Analysis Date:** 2026-04-14

## Pattern Overview

**Overall:** Layered monolith with event-driven Discord bot framework.

**Key Characteristics:**
- Discord.py bot with async/await concurrency model
- Strict separation between Discord API layer, audio streaming layer, and data management
- Subprocess-based audio streaming to bypass YouTube TLS fingerprinting blocks
- Plugin system for PO token generation (bgutil yt-dlp provider)

## Layers

### Discord Command Layer (MusicCog)
- **Purpose:** Handle Discord user commands and interactions, manage voice channel connections
- **Location:** `commands.py` (MusicCog class, ~1032 lines)
- **Contains:** Command handlers for play, pause, resume, stop, skip, queue, bitrate, admin management, fair-play settings
- **Depends on:** AudioPlayer, TrackQueue, guild_settings module, audio_player utilities
- **Used by:** MusicBot.setup_hook() which loads this cog on startup

### Audio Playback Layer (AudioPlayer)
- **Purpose:** Manage audio streaming to Discord voice channels using FFmpeg pipeline
- **Location:** `audio_player.py` (AudioPlayer class, ~500 lines)
- **Contains:** get_audio_url(), _start_ytdlp_stream(), is_playlist_url(), extract_playlist_info() functions; AudioPlayer class methods for play/pause/resume/stop
- **Depends on:** yt_dlp (YouTube metadata extraction), Discord VoiceClient (audio output)
- **Used by:** MusicCog commands and audio streaming orchestration

### Queue Management Layer (TrackQueue)
- **Purpose:** Manage track ordering with fair-play algorithm that alternates between users
- **Location:** `track_queue.py` (TrackQueue class, ~99 lines)
- **Contains:** deque-based queue with history tracking, fair-play ordering logic, Track dataclass
- **Depends on:** Nothing (pure data structure)
- **Used by:** MusicCog to manage queue state

### Guild Settings Persistence Layer
- **Purpose:** Per-guild configuration (allowed channels, bitrate, admin users)
- **Location:** `guild_settings.py` (~76 lines)
- **Contains:** JSON-based settings CRUD functions (load/save), per-guild accessors for channel, bitrate, admins
- **Depends on:** JSON, filesystem
- **Used by:** MusicCog for authorization and guild-level settings management

### YouTube Authentication Plugin Layer
- **Purpose:** Generate PO (Play Overlay) tokens for YouTube playback to evade blocks
- **Location:** `yt-dlp-plugins/bgutil-ytdlp-pot-provider/yt_dlp_plugins/extractor/`
  - `getpot_bgutil.py` (base class BgUtilPTPBase)
  - `getpot_bgutil_cli.py` (CLI-based token provider, preference=1)
  - `getpot_bgutil_http.py` (HTTP server-based token provider, preference=130)
- **Contains:** yt_dlp provider registration, CLI/HTTP token generation strategies
- **Depends on:** yt_dlp's provider API, bgutil-pot.exe binary
- **Used by:** yt_dlp extractor during YouTube URL resolution in get_audio_url()

### Bot Initialization Layer
- **Purpose:** Bootstrap Discord bot with config, manage lifecycle of bgutil-pot server
- **Location:** `main.py` (MusicBot class, ~174 lines)
- **Contains:** Config loading, bgutil-pot subprocess startup, bot event handlers (on_ready, on_voice_state_update), empty-channel auto-leave logic
- **Depends on:** discord.py, yaml config, AudioPlayer, TrackQueue
- **Used by:** Entry point for the entire bot

## Data Flow

### Audio Playback Flow (Play Command)

1. User calls `!play <URL or search query>`
2. **MusicCog.play()** validates user is in voice channel, detects playlist via is_playlist_url()
3. **For single tracks:**
   - Calls get_audio_url() (in-process yt_dlp metadata extraction with download=False)
   - Calls _start_ytdlp_stream() (spawns yt-dlp subprocess, pipes audio to stdout)
   - Both run concurrently via asyncio.gather() to minimize latency
4. **get_audio_url()** returns: {url, title, thumbnail, webpage_url, http_headers, is_audio_only}
5. **_start_ytdlp_stream()** returns: Popen object with stdout as audio pipe
6. **AudioPlayer.play()** creates discord.FFmpegPCMAudio reading from yt-dlp subprocess stdout
7. FFmpeg decodes audio and streams PCM frames to Discord VoiceClient
8. Discord bot transmits Opus-encoded frames to voice channel
9. After playback ends, on_playback_done callback triggers auto-next chain (_auto_next)

### Fair-Play Queue Algorithm

1. User adds track → TrackQueue.add(track)
2. Next() called by _auto_next:
   - If fair_play=True and queue has >1 track and last_played_user is set:
     - Search for first track by DIFFERENT user than last_played_user
     - Move that track to front of deque
   - Pop and return front track
   - Update last_played_user
3. preview_fair_order() simulates next 10 tracks without mutation (for display)

### Empty Channel Auto-Leave

1. on_voice_state_update() fires when member joins/leaves voice channel
2. If last non-bot member leaves channel while bot is playing:
   - Create 60-second timeout task (_leave_after_timeout)
3. If member rejoins within 60 seconds:
   - Cancel timeout, resume waiting
4. If timeout expires and channel still empty:
   - _do_empty_leave() stops playback, clears queue, disconnects

### YouTube PO Token Generation

1. get_audio_url() builds ydl_opts with extractor_args
2. If YouTube URL and bgutil-pot.exe exists:
   - Register CLI provider (preference=1) with cli_path → bgutil-pot.exe
   - Register HTTP provider (preference=130) with base_url → 127.0.0.1:1 (dead port to disable)
3. yt_dlp provider registry selects CLI (lower preference = higher priority)
4. BgUtilCliPTP spawns bgutil-pot CLI subprocess with query, receives JSON PO token
5. Token injected into format URL as pot= parameter
6. HTTP headers with Cookie/Referer passed to FFmpeg for format 18 fallback

## Key Abstractions

### Track
- **Purpose:** Represent a queued music track with metadata
- **Location:** `track_queue.py` (dataclass)
- **Pattern:** Immutable dataclass with query, title, requested_by, thumbnail, url fields

### AudioPlayer
- **Purpose:** Abstract away FFmpeg/Discord VoiceClient complexity
- **Location:** `audio_player.py`
- **Pattern:** Stateful class managing voice connection, subprocess lifecycle, playback state

### TrackQueue
- **Purpose:** Abstract queue operations with fair-play fairness logic
- **Location:** `track_queue.py`
- **Pattern:** Deque wrapper with history and last_user tracking for fairness

### Guild Settings
- **Purpose:** Abstract per-guild configuration persistence
- **Location:** `guild_settings.py`
- **Pattern:** Functional accessors over JSON file storage

## State Management

### Playback State (AudioPlayer)
- `is_playing`: Boolean flag for current playback status
- `is_paused`: Boolean flag for pause state
- `current_track_title`: Currently playing track title
- `_voice_client`: discord.py VoiceClient connection
- `_ytdlp_proc`: yt-dlp subprocess (Popen handle)

### Queue State (TrackQueue)
- `_queue`: deque[Track] of pending tracks
- `_history`: list[Track] of completed tracks (for previous)
- `current`: Track currently playing
- `fair_play`: Boolean flag for fairness algorithm
- `last_played_user`: User ID of last played track for fairness

### Bot State (MusicBot)
- `_current_guild_id`: Guild ID of active voice connection
- `current_text_channel_id`: Text channel for bot messages
- `_auto_next_task`: asyncio.Task for auto-play chain (prevented from duplicating)
- `_auto_next_gen`: Generation counter to cancel stale auto_next tasks
- `_empty_channel_task`: asyncio.Task for 60-second empty-channel timeout
- `pending_playlists`: dict[message_id -> playlist_info] for loadall button

## Entry Points

### main.py (Program Entry)
- **Location:** `main.py:main()` function
- **Triggers:** Script execution (`python main.py`)
- **Responsibilities:**
  - Load config from config.yaml
  - Start bgutil-pot HTTP server subprocess (for token generation)
  - Create MusicBot instance
  - Call bot.run(token) to start Discord connection
  - Cleanup bgutil-pot on exit

### MusicBot.setup_hook()
- **Location:** `main.py:MusicBot.setup_hook()`
- **Triggers:** Called by discord.py during bot startup
- **Responsibilities:** Load MusicCog via commands.setup()

### Discord Event Handlers (main.py)
- **on_ready:** Logs bot login success
- **on_voice_state_update:** Detects empty channels, triggers auto-leave timeout

### Command Handlers (commands.py)
- **MusicCog.play():** Play single track or playlist
- **MusicCog.pause():** Pause playback
- **MusicCog.resume():** Resume playback
- **MusicCog.stop():** Stop playback and clear queue
- **MusicCog.skip():** Skip to next track
- **MusicCog.queue():** Display queue with pagination
- **MusicCog.loadall():** Load remaining playlist tracks via button
- **MusicCog.bitrate():** Set audio bitrate for guild
- **MusicCog.shutdown():** Stop bot
- **MusicCog.addadmin():** Add admin user
- **MusicCog.removeadmin():** Remove admin user
- **MusicCog.fairplay():** Toggle fair-play mode
- **MusicCog.fairness():** Show fair-play statistics
- **MusicCog.help():** Display help information

## Error Handling

**Strategy:** Async exception handling with graceful degradation.

**Patterns:**
- Command handlers use try/catch for asyncio operations, respond with error messages
- AudioPlayer.play() uses asyncio.gather(..., return_exceptions=True) to isolate metadata/stream failures
- get_audio_url() catches yt_dlp extraction errors, re-raises to caller
- Subprocess pipes (yt-dlp, ffmpeg) logged asynchronously via background threads to prevent blocking
- Empty channel timeout uses `not voice_client.is_connected()` checks to avoid stale state
- Fair-play preview (preview_fair_order) caps to 10 tracks to prevent O(N^2) timeouts on button interactions

## Cross-Cutting Concerns

**Logging:** Console print() statements with prefixes ([main], [yt-dlp], [ffmpeg], [player], [debug]) for filtering.

**Validation:**
- Voice channel membership checked before play commands
- Owner-only checks on settc (admin command)
- Guild-level allowed-channel enforcement via check_channel()
- Query validation for playlist detection (check for list= or /sets/ in URL)

**Authentication:** Per-guild admin list (guild_settings.json) for admin-only commands (addadmin, removeadmin, bitrate, fairplay).

**Concurrency:** asyncio.gather() for parallel metadata extraction + subprocess startup; asyncio.Task for auto-next chain and empty-channel timeout with generation counter to prevent duplication.

---

*Architecture analysis: 2026-04-14*
