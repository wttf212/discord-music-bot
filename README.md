# Discord Music Bot

A Discord music bot with YouTube, SoundCloud, and Spotify. (Use it at your own risk, YouTube loves to ban IPs)

## Features

- 🎵 Play from YouTube, SoundCloud, and Spotify (Spotify links are resolved to YouTube)
- 🔍 YouTube search with a dropdown picker (`!search`)
- ♾️ Autoplay / endless mode — keeps playing related tracks when the queue ends (`!autoplay`)
- 🔁 Loop the current track or the whole queue (`!loop`)
- 🧰 Queue tools — remove, move, skip-to, clear, and dedupe
- 📩 Grab — DM yourself the currently playing track (`!grab`)
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
| `spotify.client_id` / `spotify.client_secret` | *(optional)* Enable Spotify **playlist/album** links (single track links work without them) |

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
| `!play <url or keywords>` | Play from YouTube, SoundCloud, or Spotify — links play directly, plain text auto-plays the top YouTube result (join voice first) |
| `!search <keywords>` | Search YouTube and pick a result from a dropdown |
| `!grab` | DM yourself the currently playing track |
| `!pause` | Pause playback |
| `!resume` | Resume paused playback |
| `!skip` | Skip the current track |
| `!stop` | Stop playback, clear the queue, and leave voice |

### Queue

| Command | Description |
|---------|-------------|
| `!queue` | Show the current queue |
| `!shuffle` | Shuffle the queued tracks |
| `!loop [off\|track\|queue]` | Repeat the current track or the whole queue |
| `!autoplay [on\|off]` | Keep playing related tracks when the queue ends |
| `!remove <pos>` | Remove a track from the queue by position |
| `!move <from> <to>` | Move a queued track to a new position |
| `!skipto <pos>` | Jump straight to a queued track |
| `!clear` | Clear the upcoming queue (keeps the current track) |
| `!dedupe` | Remove duplicate tracks from the queue |
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

## Bot Permissions

### Required Discord Permissions

When inviting the bot, grant it the following permissions (or use the permissions integer `2184276992`):

**Text Channel**

| Permission | Why |
|---|---|
| View Channel | Read commands and post responses |
| Send Messages | Post now-playing embeds and status replies |
| Manage Messages | Delete user command messages and stale status messages to keep chat clean |
| Embed Links | Send rich now-playing, queue, search, and radio embeds |
| Read Message History | Fetch and edit previous now-playing messages |

**Voice Channel**

| Permission | Why |
|---|---|
| Connect | Join the voice channel the user is in |
| Speak | Stream audio to the channel |
| Use Voice Activity | Required for audio transmission |

### OAuth2 Invite Scopes

Select both scopes when generating your invite URL:

- `bot` — grants the permissions above
- `applications.commands` — registers slash commands (`/play`, `/search`, etc.)

### Privileged Gateway Intents

Enable these in the [Discord Developer Portal](https://discord.com/developers/applications) under your application → **Bot → Privileged Gateway Intents**:

| Intent | Why |
|---|---|
| **Message Content** | Required — lets the bot read prefix commands (`!play`, `!skip`, etc.) |
| **Server Members** | Recommended — needed for accurate voice-channel member counts (fair-play vote thresholds and auto-leave) |

> **Note:** Without Server Members intent, the bot cannot reliably count how many users are in a voice channel, which breaks `!fairness` vote thresholds and may cause premature auto-leave.

## Requirements

- Python 3.11+
- ffmpeg
- Deno (for YouTube signature solving)
- Discord bot with privileged intents enabled (see [Bot Permissions](#bot-permissions) above)
