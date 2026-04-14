# Codebase Concerns

## Technical Debt

### Monolithic Commands File
- Issue: `commands.py` is ~1032 lines with mixed responsibilities (command parsing, UI views, embed building, vote tracking)
- Impact: Hard to navigate, test, and extend
- Location: `commands.py`
- Suggested fix: Split into sub-modules (embeds, views, vote logic)

### Debug Logging via print()
- Issue: All debug output uses `print()` with manual `[module]` prefixes instead of Python `logging`
- Impact: No log levels, no structured output, can't be filtered or redirected
- Location: `audio_player.py`, `commands.py`, `main.py`
- Suggested fix: Replace with `logging.getLogger(__name__)`

### No Config Validation
- Issue: Config loaded as raw dict with no schema validation — missing keys cause `KeyError` at runtime
- Location: `main.py`
- Suggested fix: Use `pydantic` or `cerberus` for config schema

## Known Bugs / TODOs
- [ ] `track_queue.py:34-46` — Fair-play reordering fails silently when all remaining tracks are from the same user
- [ ] Dynamic vote sets via `setattr()` — potential memory leak if guild objects accumulate
- [ ] `nul` file in repo root — appears to be a Windows artifact from a mistyped command

## Security Concerns

### Plaintext Bot Token in Config File
- Risk: `bot_token` stored in plaintext `config.yaml`; if accidentally committed, token is exposed
- Location: `config.example.yaml`, `config.yaml` (gitignored)
- Mitigation: Load from env var (`DISCORD_BOT_TOKEN`) with `config.yaml` as fallback only

### Inconsistent Permission Checking
- Risk: Not all commands consistently verify DJ role or voice channel membership
- Location: `commands.py`
- Mitigation: Centralize permission check into a reusable decorator or check helper

### No Rate Limiting on Playlist Fetches
- Risk: A user can queue large playlists repeatedly, causing high yt-dlp CPU/memory usage (DoS)
- Location: `audio_player.py`, `commands.py`
- Mitigation: Cap playlist size and add per-user cooldowns

### Subprocess Input Not Validated
- Risk: Track URLs passed to yt-dlp subprocess without sanitization
- Location: `audio_player.py`
- Mitigation: Validate URLs against allowed patterns before passing to subprocess

## Performance Concerns

### Guild Settings: Full JSON Load on Every Read
- Issue: `guild_settings.py` loads and parses the entire JSON file on each `get()` call
- Location: `guild_settings.py`
- Impact: Disk I/O on every settings access, degrades at scale

### Fair-Play Preview is O(N²)
- Issue: Fair-play queue preview is recalculated on every Now Playing embed update
- Location: `track_queue.py`, `commands.py`
- Impact: Noticeable lag in large queues

### Guild Settings Race Condition
- Issue: Concurrent writes to `guild_settings.json` (multiple guilds active) can corrupt the file — no file locking
- Location: `guild_settings.py`
- Risk level: Medium

## Fragile / High-Risk Areas

### bgutil-pot.exe Binary Dependency
- Why fragile: Pre-built Windows binary checked into repo root; must be manually updated when YouTube changes attestation; not cross-platform
- Location: `bgutil-pot.exe`, `audio_player.py`
- Risk level: High — YouTube auth changes can silently break all playback

### yt-dlp Plugin Implicit Path Setup
- Why fragile: Plugin loaded by injecting `yt-dlp-plugins/bgutil-ytdlp-pot-provider` into `sys.path` and `PYTHONPATH`; path errors cause silent fallback to weak PO tokens
- Location: `audio_player.py` (`_plugin_dir`, `_base_dir` setup)
- Risk level: High

### Unbounded Daemon Threads
- Why fragile: FFmpeg and yt-dlp stderr are drained by daemon threads with no lifecycle management — threads leak if player is reset rapidly
- Location: `audio_player.py:~392-407`
- Risk level: Medium

### Auto-Next Task Generation Counter
- Why fragile: Generation counter used to cancel stale auto-next tasks is not protected by a lock; concurrent increments under load could cause races
- Location: `audio_player.py`
- Risk level: Medium

## Missing Features / Gaps
- No persistent queue (queue lost on bot restart)
- No playlist management (save/load named playlists)
- No web dashboard
- No Spotify/SoundCloud support
- No seek/rewind commands

## Dependencies at Risk
| Dependency | Risk | Reason |
|------------|------|--------|
| `yt-dlp` | High | YouTube API changes break it frequently; pinning is risky, unpinning is risky |
| `bgutil-pot.exe` | High | Hardcoded binary; YouTube attestation requirements evolve |
| `discord.py` | Low | Stable, well-maintained |
| `PyNaCl` | Low | Stable voice encryption library |
