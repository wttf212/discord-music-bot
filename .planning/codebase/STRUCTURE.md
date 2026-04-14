# Directory Structure

## Layout
```
discord-music-bot/
├── main.py                         # Bot entry point, initialization, event listeners
├── commands.py                     # All Discord slash/prefix commands (MusicCog)
├── audio_player.py                 # Audio streaming engine (yt-dlp + FFmpeg)
├── track_queue.py                  # Queue data structure with fair-play algorithm
├── guild_settings.py               # Per-guild settings persistence (JSON)
├── guild_settings.json             # Runtime guild settings storage
├── config.example.yaml             # Config template with all options documented
├── config.yaml                     # Live bot config (gitignored)
├── requirements.txt                # Python dependencies
├── bgutil-pot.exe                  # bgutil PO token CLI binary (v0.7.2)
├── Dockerfile                      # Container image definition
├── docker-compose.yml              # Docker Compose deployment config
├── setup.sh / setup.bat            # Platform setup scripts
├── .gitignore
├── .dockerignore
├── README.md
└── yt-dlp-plugins/
    └── bgutil-ytdlp-pot-provider/  # yt-dlp plugin for YouTube PO tokens
        └── yt_dlp_plugins/
            └── extractor/
                ├── getpot_bgutil.py       # Base plugin class
                ├── getpot_bgutil_cli.py   # CLI provider (uses bgutil-pot.exe)
                └── getpot_bgutil_http.py  # HTTP provider (uses bgutil server)
```

## Key Locations
| Path | Purpose |
|------|---------|
| `main.py` | Bot startup, config loading, `MusicBot` class, `on_voice_state_update` listener |
| `commands.py` | `MusicCog` — all user-facing commands, `PlayerControls` UI view |
| `audio_player.py` | `AudioPlayer` — yt-dlp subprocess, FFmpeg piping, `get_audio_url()` |
| `track_queue.py` | `TrackQueue`, `Track` dataclass, fair-play ordering, playlist logic |
| `guild_settings.py` | `GuildSettings` — per-guild DJ role, volume, autoplay settings |
| `guild_settings.json` | Runtime JSON file storing all guild settings |
| `config.example.yaml` | Canonical config reference with comments |
| `yt-dlp-plugins/` | bgutil PO token plugin (loaded via `PYTHONPATH`) |
| `bgutil-pot.exe` | Pre-built binary for YouTube attestation tokens |

## Module Responsibilities
| Module | Lines | Role |
|--------|-------|------|
| `commands.py` | ~1032 | Discord interface layer |
| `audio_player.py` | ~500+ | Audio streaming & subprocess management |
| `track_queue.py` | ~300+ | Queue logic & data structures |
| `guild_settings.py` | ~100+ | Settings persistence |
| `main.py` | ~100+ | Bot init & event routing |

## Naming Conventions
- Files: `snake_case.py`
- Classes: `PascalCase` (e.g., `AudioPlayer`, `TrackQueue`, `MusicCog`)
- Methods: `snake_case`, private methods prefixed with `_` (e.g., `_start_ytdlp_stream`)
- Constants: `UPPER_CASE` (e.g., `PLAYLIST_EMOJI`)
- Config keys: `snake_case` in YAML (e.g., `bot_token`, `command_prefix`)

## Config File Structure
```yaml
# config.yaml / config.example.yaml
bot_token: "..."
command_prefix: "!"
debug: false
youtube:
  cookies_file: "..."
audio:
  ffmpeg_path: "..."
  volume: 100
```
