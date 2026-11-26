# Discord Music Bot

A self-hosted music bot for Discord, written in Python with discord.py. It plays YouTube, SoundCloud and Spotify links in voice channels, and it can play internet radio.

Use it at your own risk: YouTube bans IP addresses that it thinks belong to bots.

## What it does

- Plays a YouTube, SoundCloud or Spotify link, or the top YouTube result for whatever you type. Spotify audio can't be streamed, so the bot plays the same song from YouTube.
- `!search` shows YouTube results to pick from.
- A YouTube or SoundCloud playlist starts with its first track. To queue the rest, click Load playlist on the player card or type `!loadall`. Spotify playlists and albums go straight into the queue. Either way, the limit is 2000 tracks.
- The queue can be shuffled, looped, reordered and cleaned up. Fair play, on by default, alternates songs between the people who added them.
- Autoplay adds related tracks when the queue runs out.
- Internet radio: browse by region, country and genre, or search over 30,000 stations by name.
- The player card has buttons for pause, previous, next, stop, the queue, loop, shuffle and grab. It also shows the local weather, the next F1 race and the next rocket launch, plus an aurora forecast when there's a chance to see one.
- `!grab` sends you the current track in a DM.
- Each server has its own bitrate and equalizer settings.
- The first song usually starts in about 2 seconds.
- Optional text-to-speech: the bot can read out messages sent with `!tts`, and every half hour it can say the time and how many songs are left. It stays off unless you turn it on.
- Works with Discord's end-to-end encrypted voice (DAVE), which needs discord.py 2.7 or newer.
- The owner can limit the bot to one text channel with `!settc`.
- It leaves the voice channel when everyone else has left, after a minute if music is still playing.

## Requirements

To run the bot from source you need:

- Python 3.11 or newer.
- FFmpeg. If it isn't on your PATH, the bot uses the copy that comes with the imageio-ffmpeg package.
- Deno, which yt-dlp uses to solve YouTube's JavaScript challenges. The setup scripts install it if it's missing.

The Docker image comes with all three.

You also need a Discord bot. Create an application in the [Discord Developer Portal](https://discord.com/developers/applications), copy its bot token, turn on the Message Content intent and invite the bot to your server. The details are under [Discord permissions and intents](#discord-permissions-and-intents).

## Setup

The bot reads its token from `bot_token` in `config.yaml` or from the `DISCORD_BOT_TOKEN` environment variable. If both are set, the environment variable wins. The Docker setup uses the environment variable.

### Windows

Run `setup.bat`. It creates a virtual environment, installs the Python packages, installs Deno if it's missing and copies `config.example.yaml` to `config.yaml`. Put your bot token and your Discord user ID (`owner_id`) in `config.yaml`, then start the bot from Command Prompt:

```bat
venv\Scripts\activate & python main.py
```

### Linux / macOS

```bash
chmod +x setup.sh && ./setup.sh
```

The script does the same as `setup.bat`. Put your bot token and `owner_id` in `config.yaml`, then start the bot:

```bash
source venv/bin/activate && python3 main.py
```

### Docker

1. Copy the example config and set `owner_id` in it:

   ```bash
   cp config.example.yaml config.yaml
   ```

2. Create a file named `.env` next to `docker-compose.yml`, with your bot token in it:

   ```
   DISCORD_BOT_TOKEN=paste-your-token-here
   ```

3. Start the bot:

   ```bash
   docker compose up -d
   ```

Compose reads `.env` and passes the token to the bot, so `bot_token` in `config.yaml` can stay as it is.

## Configuration

The settings are in `config.yaml`. `config.example.yaml` has comments on the bot token and on the YouTube, Spotify and text-to-speech settings. These are the ones you're most likely to change:

| Key | What it does |
|-----|--------------|
| `bot_token` | Your bot token. The `DISCORD_BOT_TOKEN` environment variable wins over it |
| `prefix` | The command prefix. Default `!` |
| `owner_id` | Your Discord user ID. The owner can use every command and never needs a vote |
| `debug` | `true` makes the log much more detailed |
| `youtube.fast_client` | Gets the first song playing in about 2 seconds instead of about 5. On by default; set it to `""` to turn it off. Not used when `cookies_file` is set |
| `youtube.cookies_file` | Optional. See [YouTube cookies](#youtube-cookies-optional) |
| `spotify.client_id`, `spotify.client_secret` | Optional. Only Spotify playlist and album links need them. Get them from a free app on the [Spotify developer dashboard](https://developer.spotify.com/dashboard) |
| `tts.enabled` | `true` turns on text-to-speech, once edge-tts is installed. Default `false`. See [Text-to-speech](#text-to-speech-optional) |
| `audio.bitrate` | The default audio bitrate in kbps (128). `!bitrate` changes it for one server |
| `ffmpeg_path` | Where FFmpeg is. The default, `ffmpeg`, means the one on your PATH |

## YouTube cookies (optional)

Cookies from your browser can make YouTube less likely to flag the bot, mostly on home internet connections.

1. Export them on the host, not inside Docker:

   ```bash
   yt-dlp --cookies-from-browser chrome --cookies cookies.txt -o /dev/null -- "https://www.youtube.com"
   ```

2. Put the `cookies.txt` it writes in the project folder.
3. In `config.yaml`, under `youtube:`, set `cookies_file`:
   - From source: `cookies_file: "./cookies.txt"`
   - Docker: `cookies_file: "/data/cookies.txt"`, and uncomment the `- ./cookies.txt:/data/cookies.txt` line in `docker-compose.yml`.

Export them again every 4 months or so. The bot warns in its log once the file is more than 150 days old. While `cookies_file` is set, `youtube.fast_client` is off, so the first song starts more slowly.

## Commands

Commands start with the prefix, `!` by default. `!help` shows a short version of this list in Discord.

These also work as slash commands: `/play`, `/search`, `/grab`, `/shuffle`, `/loop`, `/autoplay`, `/remove`, `/move`, `/skipto`, `/clear`, `/dedupe`, `/radio`, `/setlocation`, `/settimezone`, and the text-to-speech commands when it's on.

Commands marked *(admin only)* are for the bot owner and the bot admins the owner adds with `!addadmin`. Discord roles don't make someone a bot admin.

### Playback

| Command | What it does |
|---------|--------------|
| `!play <url or keywords>` | Play a YouTube, SoundCloud or Spotify link, or the top YouTube result for your words. Join a voice channel first |
| `!search <keywords>` | Search YouTube and pick a result from a dropdown |
| `!grab` | Send yourself the current track in a DM |
| `!pause` | Pause playback |
| `!resume` | Resume paused playback |
| `!skip` | Skip the current track |
| `!stop` | Stop playback, clear the queue and leave the voice channel |

When other people are listening too, `!skip`, `!pause` and `!resume` take a vote unless the current song is yours, and `!stop` takes one unless you added every song that is playing or queued. The pause, previous, next and stop buttons on the player card work the same way. `!fairness` sets how many votes it takes.

### Queue

| Command | What it does |
|---------|--------------|
| `!queue` | Show the queue |
| `!shuffle` | Shuffle the queue |
| `!loop [off\|track\|queue]` | Repeat the current track or the whole queue. With no argument, it cycles through the modes |
| `!autoplay [on\|off]` | Keep playing related tracks when the queue runs out. With no argument, it toggles |
| `!remove <pos>` | Remove the track at that position |
| `!move <from> <to>` | Move a queued track to another position |
| `!skipto <pos>` | Jump straight to a queued track |
| `!clear` | Clear the queue but keep the current track |
| `!dedupe` | Remove duplicate tracks from the queue |
| `!loadall` | Queue the rest of the playlist that was just added. The offer lasts two minutes |

### Radio

| Command | What it does |
|---------|--------------|
| `!radio` | Browse internet radio by region, country and genre |
| `!radio <name>` | Search over 30,000 stations by name |

### Sound and the player card

| Command | What it does |
|---------|--------------|
| `!bitrate [kbps]` | Show or set the audio bitrate for this server, from 1 to 512 kbps |
| `!eq` | Show the equalizer settings and how to change them *(admin only)* |
| `!eq bass <-10..+10>` | Boost or cut the bass, in dB *(admin only)* |
| `!eq treble <-10..+10>` | Boost or cut the treble, in dB *(admin only)* |
| `!eq <preset>` | Use a preset: `flat`, `bass-boost`, `treble-boost` or `vocal` *(admin only)* |
| `!eq reset` | Set the equalizer back to flat *(admin only)* |
| `!setlocation <city>` | Set the city for the weather on the player card *(admin only)* |
| `!settimezone <tz>` | Set the timezone for the times on the player card, e.g. `Europe/Riga` *(admin only)* |

Equalizer changes apply from the next track. Until an admin sets them, the city is Riga and the timezone is `Europe/Riga`.

### Text-to-speech (optional)

Off by default. To turn it on:

1. Set `tts.enabled: true` in `config.yaml`.
2. Install the edge-tts package and restart the bot:
   - From source: run `pip install -r requirements-tts.txt` with the virtual environment active.
   - Docker: add `WITH_TTS=true` to `.env`, then rebuild and restart with `docker compose up -d --build`.

It needs no account or key. The voices are Microsoft Edge's online ones, so the text to be spoken goes to Microsoft. If `tts.enabled` is `true` but edge-tts isn't installed, the bot logs a warning and leaves text-to-speech off.

| Command | What it does |
|---------|--------------|
| `!tts <text>` | Say something in the voice channel right now (the music dips) |
| `!ttsafter <text>` | Say something after the current song |
| `!ttslang lv\|en` | Speech language for this server *(admin only)* |
| `!announce on\|off` | Say the time and songs left every half hour, between songs *(admin only)* |
| `!ttsallow on\|off` | Allow or block `!tts` and `!ttsafter` on this server *(admin only)* |

- Speech is in Latvian until an admin switches the server to English with `!ttslang en`.
- `!tts` and `!ttsafter` work on every server until an admin blocks them there with `!ttsallow off`. The half-hourly update has its own switch, `!announce`.
- The half-hourly update is off until an admin runs `!announce on`. It uses the server's timezone from `!settimezone`.
- A message can be up to 300 characters. Each person can send one every 10 seconds, and up to 5 can wait their turn on a server.

### Fair play and votes

| Command | What it does |
|---------|--------------|
| `!fairplay on\|off` | Alternate queued songs between the people who added them. On by default *(admin only)* |
| `!fairness <0-100>` | Set the percentage of listeners who must vote when a command takes a vote. Default 50 *(admin only)* |

Neither setting is saved: fair play turns back on after `!stop` or when the bot leaves, and fairness goes back to 50 when the bot restarts.

### Owner only

These work only for the user in `owner_id`.

| Command | What it does |
|---------|--------------|
| `!addadmin @user` | Make someone a bot admin on this server |
| `!removeadmin @user` | Take bot admin away from someone on this server |
| `!settc` | Only take commands in this channel. Commands sent elsewhere are deleted, and the sender is pointed here |
| `!shutdown` | Shut the bot down. Under the included `docker-compose.yml` the container starts straight back up (`restart: unless-stopped`), so stop it there with `docker compose stop` |

## Discord permissions and intents

### Permissions

When you invite the bot, give it these permissions, or use the permissions integer `36793408`.

In text channels:

| Permission | Why |
|------------|-----|
| View Channel | See the channel and read commands |
| Send Messages | Post the player card and replies |
| Manage Messages | Delete commands sent outside the channel set with `!settc` |
| Embed Links | Show search results and the radio browser |
| Read Message History | Discord requires it before the bot can add a reaction (the `!tts` confirmation) |
| Add Reactions | Confirm a `!tts` message with a speaker reaction. Without it, the bot posts a short message instead. It only matters when text-to-speech is on |

In voice channels:

| Permission | Why |
|------------|-----|
| Connect | Join your voice channel |
| Speak | Play audio |
| Use Voice Activity | Send audio without push-to-talk |

Text-to-speech needs no extra voice permission. It plays through the same voice connection as the music.

### Invite scopes

Pick both scopes when you make the invite link:

- `bot`, for the permissions above
- `applications.commands`, for the slash commands

### Privileged intents

In the [Discord Developer Portal](https://discord.com/developers/applications), open your application, go to the Bot page and turn on Message Content under Privileged Gateway Intents. The bot reads commands like `!play` from message text, and Discord won't let it log in without this intent.

The bot doesn't ask for the Server Members or Presence intents, so it doesn't need them.
