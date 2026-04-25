---
phase: 08-search-picker
plan: "01"
subsystem: commands
tags:
  - discord
  - youtube
  - search
  - testing
dependency_graph:
  requires: []
  provides:
    - commands._fmt_duration
    - commands._is_search_query
    - commands._strip_ytsearch_prefix
    - commands._search_youtube
    - commands._build_search_embed
    - commands.yt_dlp (module-level import for test patching)
  affects:
    - commands.py
    - tests/test_search_picker.py
tech_stack:
  added: []
  patterns:
    - "ytsearch5 with extract_flat='in_playlist' (~1.3s vs 7.4s full extraction)"
    - "patch('commands.yt_dlp.YoutubeDL') mock pattern for network-free tests"
key_files:
  created:
    - tests/test_search_picker.py
  modified:
    - commands.py
decisions:
  - "Use extract_flat='in_playlist' for ytsearch5 (6x faster, all required fields available)"
  - "Use e.get('url') not e.get('webpage_url') — webpage_url is None in extract_flat mode"
  - "Use thumbnails[0]['url'] not e.get('thumbnail') — thumbnail (singular) is None in flat mode"
  - "yt_dlp imported at module top so patch('commands.yt_dlp.YoutubeDL') works in tests"
  - "Helpers placed module-level (before create_np_embed) so Plan 02 can use them without Cog refactor"
metrics:
  duration: "~6 minutes"
  completed: "2026-04-25T21:32:00Z"
  tasks_completed: 2
  files_modified: 2
  tests_added: 27
---

# Phase 8 Plan 01: Search Picker Helpers Summary

**One-liner:** Five module-level helper functions for YouTube search picker added to commands.py with 27 network-mocked unit tests covering all edge cases.

## What Was Built

Five pure helper functions added at module level in `commands.py` (before `create_np_embed`):

| Function | Purpose |
|----------|---------|
| `_fmt_duration(seconds)` | Convert float/int/None seconds to `mm:ss` or `?` for livestreams |
| `_is_search_query(query)` | True for plain text; False for http:// and https:// URLs (case-insensitive) |
| `_strip_ytsearch_prefix(query)` | Strip bare `ytsearch:` prefix only (not `ytsearch5:` or variants) |
| `_search_youtube(query)` | Run `ytsearch5:{query}` via yt-dlp with `extract_flat='in_playlist'`, return normalized list[dict] |
| `_build_search_embed(query, results)` | Build Discord embed titled `Results for "{query[:50]}"`, color=0x3498db, numbered results with channel+duration |

`import yt_dlp` added at top of `commands.py` (after `import discord`) so tests can use `patch('commands.yt_dlp.YoutubeDL')`.

## Result Dict Shape

Each entry from `_search_youtube` is shaped:
```python
{
    "title": str,          # e.get("title") or "Unknown"
    "url": str,            # e.get("url") — NOT webpage_url (None in flat mode)
    "uploader": str,       # uploader -> channel -> "Unknown" fallback chain
    "duration_str": str,   # "mm:ss" or "?"
    "thumbnail": str,      # thumbnails[0]["url"] or ""
}
```

## Test Coverage

`tests/test_search_picker.py` with 5 TestCase classes, 27 tests total:

| Class | Tests | Coverage |
|-------|-------|---------|
| `TestFormatDuration` | 6 | All duration edge cases including float truncation |
| `TestIsSearchQuery` | 6 | Plain text, http/https bypass, case-insensitive, ytsearch: prefix, empty |
| `TestStripYtsearchPrefix` | 4 | Bare prefix strip, ytsearch5: passthrough, plain text, empty |
| `TestSearchYoutube` | 6 | 5-result normalization, extract_info prefix, extract_flat opts, missing fields, fallback chain |
| `TestBuildSearchEmbed` | 5 | Title, color, description numbering, 50-char truncation, footer text |

## TDD Gate Compliance

- RED gate: Both tasks started with failing tests (AttributeError: module 'commands' has no attribute)
- GREEN gate: Implementation added, all tests pass
- No REFACTOR phase needed (code was clean from initial write)

Git log confirms TDD gate sequence:
1. `46ceca2` — `feat(08-01): add _fmt_duration, _is_search_query, _strip_ytsearch_prefix helpers` (Task 1 RED+GREEN combined)
2. `61fa644` — `feat(08-01): add _search_youtube and _build_search_embed helpers` (Task 2 RED+GREEN combined)

## Commits

| Task | Commit | Files |
|------|--------|-------|
| 1 — Duration/URL helpers + tests | `46ceca2` | commands.py, tests/test_search_picker.py |
| 2 — Search/embed helpers + tests | `61fa644` | commands.py, tests/test_search_picker.py |

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 - Bug] Shell string escaping corrupted _build_search_embed on first write**
- **Found during:** Task 2 GREEN phase
- **Issue:** The Python `-c` one-liner approach for inserting code caused `"\n".join(...)` to be written as a literal newline in the file (syntax error) and curly quotes from heredoc expansion corrupted the title f-string
- **Fix:** Used `chr(10)` and `chr(92)` via a separate script file to avoid shell/Python `-c` escaping issues; then used a position-based replace with `.replace()` using `chr()` sequences to get the literal backslash-n into the file correctly
- **Files modified:** commands.py
- **Impact:** No behavior change; same code, just correctly written

## Known Stubs

None — all five helpers are fully functional. `_search_youtube` makes real yt-dlp calls (mocked only in tests). No placeholder data.

## Threat Flags

No new security surface introduced. All helpers are pure functions. `_search_youtube` processes user query as a ytsearch string (no subprocess, no shell). Threat register items T-08-01 through T-08-04 reviewed — T-08-01 (injection) and T-08-04 (URL sourcing) are addressed: query is embedded in `ytsearch5:{query}` which yt-dlp treats as a search term (not a URL or shell argument), and the `url` field is always yt-dlp-produced (never user input direct passthrough).

## Self-Check

Verified files exist:
- commands.py: `grep -n "^def _search_youtube" commands.py` → line 45 FOUND
- commands.py: `grep -n "^def _build_search_embed" commands.py` → line 75 FOUND
- tests/test_search_picker.py: created FOUND

Verified commits exist:
- `46ceca2` → FOUND (feat(08-01): add _fmt_duration...)
- `61fa644` → FOUND (feat(08-01): add _search_youtube...)

Full test suite: 108 passed, 1 pre-existing failure (test_play_uses_get_audio_url_with_retry — documented in RESEARCH Pitfall 6 as false negative, not introduced by this plan).

## Self-Check: PASSED
