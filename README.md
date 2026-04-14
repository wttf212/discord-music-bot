# Discord Music Bot

A Discord music bot with YouTube and SoundCloud. (Use it at your own risk, youtube loves to ban IPs)

## Features

- 🎵 Play music from YouTube, SoundCloud, and other supported sites
- 📋 Playlist support with reaction-based loading
- 🔧 Configurable audio bitrate
- 🔒 DAVE E2EE voice encryption support (discord.py v2.7.0+)
- 🛡️ Automatic PO token generation via bgutil-pot (prevents YouTube bans)
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

1. Export on the host: `yt-dlp --cookies-from-browser chrome -o /dev/null -- "https://www.youtube.com"`
2. Place the resulting `cookies.txt` in the project root.
3. Set `cookies_file: "/data/cookies.txt"` in `config.yaml` under the `youtube:` key.
4. Docker: uncomment the `- ./cookies.txt:/data/cookies.txt` line in `docker-compose.yml`.

Re-export every ~4 months. See `config.example.yaml` for full details.

## Commands

| Command | Description |
|---------|-------------|
| `!play <url or search>` | Play a track or playlist |
| `!pause` | Pause playback |
| `!resume` | Resume playback |
| `!skip` | Skip current track |
| `!stop` | Stop and leave voice |
| `!queue` | Show queue |
| `!loadall` | Load remaining playlist tracks |
| `!bitrate [kbps]` | Show or set audio bitrate |
| `!fairplay on\|off` | Toggle user interleaving mode for queues *(admin)* |
| `!fairness <0-100>` | Set the percentage of users strictly needed to skip/stop songs *(admin)* |
| `!addadmin @user` | Add a user as a bot admin for this server *(owner)* |
| `!removeadmin @user`| Remove a user as a bot admin for this server *(owner)* |
| `!settc` | Restrict commands to this channel *(owner)* |
| `!shutdown` | Shut down the bot *(owner)* |

## Requirements

- Python 3.11+
- ffmpeg
- Deno (for YouTube signature solving)
- Discord bot with these privileged intents enabled:
  - Message Content
  - Server Members (recommended)