# Coding Conventions

## Code Style
- Formatting: No enforced linter/formatter config (no `.flake8`, `.pylintrc`, or `pyproject.toml`)
- Indentation: 4 spaces
- Line length: Flexible, typically <120 chars
- Language level: Modern Python 3.10+ (union syntax `str | None`, not `Optional[str]`)

## Naming Conventions
- Files: `snake_case` (`audio_player.py`, `track_queue.py`)
- Functions/methods: `snake_case` with underscore prefix for private (`_is_youtube()`, `_find_ffmpeg()`)
- Classes: `PascalCase` (`AudioPlayer`, `TrackQueue`, `MusicBot`, `MusicCog`)
- Variables: `snake_case` (`is_playing`, `current_track_title`)
- Constants: `UPPER_CASE` (`PLAYLIST_EMOJI`)

## Patterns Used

### Cog-based Command Organization
- Where used: `commands.py`
- All Discord commands grouped under `MusicCog(commands.Cog)`

### Dataclass for Data Structures
- Where used: `track_queue.py`
- `Track` is a `@dataclass` holding track metadata

### Discord UI Components
- Where used: `commands.py`
- `discord.ui.View` and `discord.ui.Button` with `@discord.ui.button()` decorators

### Background Tasks with Generation Counters
- Where used: `audio_player.py`
- Generation counters used to invalidate stale background tasks on track skip/stop

### Parallel Async Operations
- Where used: `audio_player.py`
- `asyncio.gather(..., return_exceptions=True)` for concurrent operations (e.g., URL fetch + stream start)

### Subprocess Management
- Where used: `audio_player.py`
- Proper cleanup: terminate → timeout → kill pattern for yt-dlp and FFmpeg subprocesses
- Daemon threads for non-blocking stderr logging

### Config Access Pattern
- Dict-based YAML config accessed via `bot.config["key"]["subkey"]`

## Error Handling
- Strategy: Silent exception capture (`except Exception: pass`) for non-critical operations
- Critical paths (in `audio_player.py`) re-raise exceptions
- No custom exception hierarchy

## Logging / Debug Output
- Method: `print()` statements with module-name prefixes, gated by `if self._debug:`
- Prefixes: `[main]`, `[yt-dlp]`, `[ffmpeg]`, `[player]`, `[commands]`, `[debug][player]`
- No structured logging framework (e.g., `logging` module not used)

## Comments & Documentation
- Docstrings: One-line `"""..."""` for simple functions
- Inline comments: Sparse, explain "why" not "what"

## Import Organization
- Standard library → third-party (`discord`, `yt_dlp`) → local modules
- No enforced grouping tool (no `isort` config)
