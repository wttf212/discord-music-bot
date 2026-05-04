# Discord Music Bot

A Discord music bot with YouTube and SoundCloud. (Use it at your own risk, YouTube loves to ban IPs)

## Features

- 🎵 Play music from YouTube, SoundCloud, and other supported sites
- 🔍 YouTube search with a dropdown picker (`!search`)
- 📻 Internet radio browser — region/country/genre discovery or name search across 30k+ stations
- 📋 Playlist support — plays the first track immediately, load the rest with `!loadall`
- 🔀 Queue with fair-play interleaving and shuffle
- 🎚️ Per-guild equalizer (bass, treble, presets)
- 🔧 Configurable audio bitrate per server
- 🔒 DAVE E2EE voice encryption support (discord.py v2.7.0+)
- 🛡️ Automatic PO token generation via bgutil-pot (prevents YouTube blocks)
- 📌 Channel restriction (`!settc`)
- 🚪 Auto-leave when voice channel empties

## Quick Start

### Windows
```bash
setup.bat
# Edit config.yaml with your bot token
venv\Scripts\activate & python main.py
```

### Linux / macOS
```bash
chmod +x setup.sh && ./setup.sh
# Edit config.yaml with your bot token
source venv/bin/activate && python3 main.py
```

### Docker
```bash
cp config.example.yaml config.yaml
# Edit config.yaml with your bot token
docker compose up -d
```

## Configuration

Copy `config.example.yaml` to `config.yaml` and fill in:

| Key | Description |
|-----|-------------|
| `bot_token` | Your Discord bot token |
| `prefix` | Command prefix (default: `!`) |
| `owner_id` | Your Discord user ID (for `!settc` and `!shutdown`) |
| `debug` | Enable verbose logging |
| `audio.bitrate` | Opus encoding bitrate in kbps (default: 128) |

## Cookie Auth (Optional)

Passing your YouTube visitor cookies reduces bot-detection on residential IPs.

1. Export on the host: `yt-dlp --cookies-from-browser chrome --cookies cookies.txt -o /dev/null -- "https://www.youtube.com"`
2. Place the resulting `cookies.txt` in the project root.
3. Set `cookies_file` in `config.yaml` under the `youtube:` key:
   - **Native** (Windows/Linux/macOS): use the path to `cookies.txt` on your filesystem, e.g. `cookies_file: "./cookies.txt"`
   - **Docker**: use `cookies_file: "/data/cookies.txt"` and uncomment the bind-mount in step 4.
4. Docker only: uncomment the `- ./cookies.txt:/data/cookies.txt` line in `docker-compose.yml`.

Re-export every ~4 months. See `config.example.yaml` for full details.

## Commands

All commands work with the prefix (default `!`) and as slash commands (e.g. `/play`, `/search`).

### Playback

| Command | Description |
|---------|-------------|
| `!play <url>` | Play a track or playlist from a YouTube/SoundCloud URL (join voice first) |
| `!search <keywords>` | Search YouTube and pick a result from a dropdown |
| `!pause` | Pause playback |
| `!resume` | Resume paused playback |
| `!skip` | Skip the current track |
| `!stop` | Stop playback, clear the queue, and leave voice |

### Queue

| Command | Description |
|---------|-------------|
| `!queue` | Show the current queue |
| `!shuffle` | Shuffle the queued tracks |
| `!loadall` | Load all remaining tracks from the last pending playlist |

### Radio

| Command | Description |
|---------|-------------|
| `!radio` | Open the region → country → genre discovery picker |
| `!radio <name>` | Search 30k+ stations by name and pick from results |

### Audio

| Command | Description |
|---------|-------------|
| `!bitrate [kbps]` | Show or set the Opus encoding bitrate for this server |
| `!eq` | Show equalizer usage |
| `!eq bass <-10..10>` | Boost or cut bass (dB) |
| `!eq treble <-10..10>` | Boost or cut treble (dB) |
| `!eq preset <name>` | Apply a named preset (e.g. `bass`, `vocal`, `flat`) |
| `!eq reset` | Reset equalizer to flat |

### Admin *(bot admin required)*

| Command | Description |
|---------|-------------|
| `!fairplay on\|off` | Toggle fair-play interleaving (alternates tracks between users) |
| `!fairness <0-100>` | Percentage of voice-channel members required to vote-skip or vote-stop |

### Owner *(owner_id only)*

| Command | Description |
|---------|-------------|
| `!addadmin @user` | Grant a user bot-admin privileges for this server |
| `!removeadmin @user` | Revoke bot-admin privileges for a user |
| `!settc` | Restrict all bot commands to the current channel |
| `!shutdown` | Shut down the bot |

### Other

| Command | Description |
|---------|-------------|
| `!help` | Show command list |

## Requirements

- Python 3.11+
- ffmpeg
- Deno (for YouTube signature solving)
- Discord bot with these privileged intents enabled:
  - Message Content
  - Server Members (recommended)
