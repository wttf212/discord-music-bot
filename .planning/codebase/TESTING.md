# Testing

## Status
**No tests exist in this codebase.** All code is currently untested.

## Evidence
- No `pytest.ini`, `setup.cfg [tool:pytest]`, or `pyproject.toml` test config
- No `tests/` directory
- No files matching `test_*.py` or `*_test.py` patterns
- `requirements.txt` lists no testing dependencies (pytest, unittest, coverage, etc.)
- `.gitignore` includes `_test_*.py` stub pattern — suggests future intent, not current practice
- No CI/CD pipeline configuration detected

## Framework & Tools
- Test framework: None
- Assertion library: None
- Mocking: None
- Coverage: None

## What Needs Tests (Gap Analysis)

### High Priority
- `audio_player.py` — Core audio streaming, yt-dlp subprocess management, FFmpeg piping
- `track_queue.py` — Queue add/remove/shuffle/move logic
- `commands.py` — Command parsing, permission checks, Discord interaction handling

### Medium Priority
- Config loading and validation (`main.py`)
- bgutil PO token extraction path
- Error recovery paths (subprocess crash, 403 handling)

## Running Tests
```bash
# No test suite configured — manual testing only
# Future: pytest
pytest
```

## Recommended Testing Approach
Given the async/subprocess-heavy nature of the bot:
- Use `pytest-asyncio` for async test support
- Mock `discord.ext.commands` context objects
- Mock yt-dlp with fixture data for audio URL tests
- Integration tests against a real Discord guild (staging bot token)
