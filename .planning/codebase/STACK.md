# Technology Stack

**Analysis Date:** 2026-04-14

## Language & Runtime

**Primary Language:**
- Python 3.12 (slim image in Docker; locally 3.11+)
- Used for: All bot logic, audio processing orchestration, configuration management

**Runtime Environment:**
- CPython 3.12-slim (Docker)
- Local development: Python 3.11.9+

## Package Manager

**Manager:** pip
- Lockfile: `requirements.txt` (pinned versions)
- Installation: `pip install -r requirements.txt` (Docker) or via `setup.sh` / `setup.bat`

## Core Framework & Libraries

**Discord Integration:**
- `discord.py[voice]` ≥2.7.0 - Discord API client with voice support
  - Location: `main.py`, `commands.py`, `audio_player.py`
  - Features: Voice state management, message commands, interactions/buttons

**Audio Processing:**
- `yt-dlp[default]` - Video/audio extraction from YouTube, SoundCloud, and other platforms
  - Location: `audio_player.py` (functions `get_audio_url()`, `extract_playlist_info()`, `_start_ytdlp_stream()`)
  - Features: Playlist extraction, format selection, HTTP header preservation

- `yt-dlp-ejs[all]` - JavaScript evaluation plugin for yt-dlp signature solving
  - Dependencies: Deno JavaScript runtime (installed in Docker at `/root/.deno/bin`)
  - Location: System integration via yt-dlp extractor args

- `imageio-ffmpeg` - FFmpeg binary fallback if system FFmpeg not found
  - Location: `audio_player.py`, function `_find_ffmpeg()`
  - Purpose: Automatic FFmpeg resolution when config path is "ffmpeg"

**Audio Codec/Transport:**
- FFmpeg (system or via imageio-ffmpeg) - Audio transcoding and piping
  - Input: yt-dlp stdout pipe (binary audio stream)
  - Output: PCM/s16 audio to Discord voice socket
  - Config: `ffmpeg_path` in `config.yaml` (default: "ffmpeg" in PATH)
  - Key args: `-f s16le -ar 48000 -ac 2` (stereo, 48kHz, 16-bit PCM)

**Configuration & Data:**
- `pyyaml` - YAML configuration parsing
  - Location: `main.py` loads `config.yaml`
  - Config file: `config.example.yaml` (template) / `config.yaml` (runtime)

## Production Dependencies

| Package | Version | Purpose |
|---------|---------|---------|
| discord.py | ≥2.7.0 | Discord API client with voice support |
| yt-dlp | latest | Audio/video extraction and metadata |
| yt-dlp-ejs | latest | JavaScript evaluation for signature solving |
| yt-dlp-bgutil | (plugin) | PO token generation for YouTube bypass |
| pyyaml | latest | YAML configuration parsing |
| imageio-ffmpeg | latest | Fallback FFmpeg binary provider |
| PyNaCl | (discord.py dep) | Voice encryption (sodium-based) |
| Deno | latest | JavaScript runtime for signature solving |

## Development & Deployment

**Build Tool:** None (direct execution)

**Packaging:** Docker
- Base image: `python:3.12-slim`
- System dependencies: `ffmpeg`, `curl`, `unzip`, `libsodium-dev`
- Image includes: Deno, bgutil-pot (Linux binary), Python deps

**Linting/Formatting:** Not configured (no eslintrc, prettier, or black config found)

**Type Checking:** Not configured (no pyright, mypy, or similar)

## Configuration Management

**Configuration Files:**
- `config.yaml` - Runtime configuration (bot token, Discord settings, YouTube client, audio params)
- `config.example.yaml` - Template with defaults and documentation
- `guild_settings.json` - Per-guild settings (allowed channel, bitrate, admins) stored as JSON

**Environment Variables:**
- `GUILD_SETTINGS_FILE` - Override path to `guild_settings.json` (default: project root)
  - Used in Docker: `/data/guild_settings.json` (persistent volume)

**Configuration Structure (config.yaml):**
```yaml
bot_token: "YOUR_BOT_TOKEN_HERE"        # Discord bot token
prefix: "!"                             # Command prefix
debug: false                            # Debug logging
owner_id: "YOUR_DISCORD_USER_ID"        # Bot owner ID

youtube:
  client: "web"                         # YouTube client (web, android_vr, web_mobile, etc.)

ffmpeg_path: "ffmpeg"                   # FFmpeg binary location

audio:
  sample_rate: 48000                    # PCM sample rate (Hz)
  channels: 2                           # Stereo
  frame_duration_ms: 20                 # Frame duration for voice packets
  bitrate: 128                          # Audio bitrate (kbps)
```

## Platform Requirements

**Development:**
- Python 3.11+
- FFmpeg (in PATH or specify in config)
- Deno (for YouTube signature solving)
  - Auto-installed by `setup.sh` / `setup.bat` or in Docker
- bgutil-pot binary (Windows: `bgutil-pot.exe`, Linux: `bgutil-pot`)
  - Auto-downloaded in Docker
  - On local dev: Must be in project root or PATH

**Production (Docker):**
- Base: python:3.12-slim
- System libs: ffmpeg, libsodium-dev (voice encryption), curl (downloads), unzip
- Deno: Installed at `/root/.deno/bin`
- bgutil-pot: Downloaded to project root (Linux x86_64 binary)
- Data: `/data/guild_settings.json` (volume mount required for persistence)

**Network Requirements:**
- Discord API connectivity (Discord CDN for large embeds/attachments)
- YouTube CDN (via yt-dlp → FFmpeg pipe)
- SoundCloud API (via yt-dlp)
- bgutil HTTP server (localhost:4416, internal)

## Special Components

**bgutil-pot (PO Token Provider):**
- Purpose: Generate YouTube PO (Proof of Origin) tokens to bypass BotGuard attestation
- Execution: Started as background subprocess in `main.py`
  - CLI mode: `bgutil-pot.exe` runs as subprocess for each yt-dlp extraction
  - HTTP mode: Optional HTTP server on port 4416 (legacy, often broken)
- Location: `bgutil-pot.exe` (Windows) at project root or PATH
- Status: Terminates gracefully on bot shutdown
- Version: v0.7.2+ (from memory)

**yt-dlp Plugin System:**
- Location: `yt-dlp-plugins/bgutil-ytdlp-pot-provider/`
- Extractors:
  - `getpot_bgutil_http.py` - HTTP server provider (preference 130)
  - `getpot_bgutil_cli.py` - CLI provider (preference 1, preferred)
  - `getpot_bgutil.py` - Base class and utilities
- Integration: Plugin dir added to `sys.path` before yt-dlp import
- Subprocess PYTHONPATH: Plugin dir prepended for subprocess yt-dlp calls

## Concurrency Model

**Async Framework:** asyncio (Python standard library)
- Location: `main.py`, `commands.py`, `audio_player.py`
- Bot event handlers: async/await
- Task scheduling: `asyncio.create_task()`, `asyncio.gather()`

**Parallel Execution:**
- Audio URL fetch and stream startup run concurrently via `asyncio.gather()` in `play()` command
- Voice client I/O: Driven by discord.py's internal async socket handling
- Audio piping: Handled by FFmpeg subprocess with threaded stdin/stdout

---

*Stack analysis: 2026-04-14*
