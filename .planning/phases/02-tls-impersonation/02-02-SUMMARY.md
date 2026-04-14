---
phase: 02-tls-impersonation
plan: "02"
subsystem: audio
tags: [yt-dlp, curl_cffi, tls-impersonation, anti-detection, chrome-fingerprint]

# Dependency graph
requires:
  - phase: 02-01
    provides: curl_cffi added to requirements.txt and Dockerfile; startup guard warns if unavailable
provides:
  - _IMPERSONATE_AVAILABLE module-level flag in audio_player.py (set once at import via try/except)
  - Chrome TLS impersonation for in-process yt-dlp path (get_audio_url — TLS-01a)
  - Chrome TLS impersonation for subprocess yt-dlp path (_start_ytdlp_stream — TLS-01b)
  - android_vr/android/ios client contradiction warning (both paths)
affects: [03-cookie-auth, 04-resilience, audio_player]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "_IMPERSONATE_AVAILABLE try/except flag at module load — checked once, never mutated"
    - "Both yt-dlp code paths impersonate atomically — never partial-deploy one without the other"
    - "Subprocess --impersonate outside if is_yt: block — TLS fingerprint applies to all extractors"

key-files:
  created: []
  modified:
    - audio_player.py

key-decisions:
  - "ImpersonateTarget imported from yt_dlp.networking.impersonate (not yt_dlp.utils — wrong path raises ImportError)"
  - "Both TLS-01a and TLS-01b committed in one atomic commit — partial impersonation is a stronger bot signal than none"
  - "_IMPERSONATE_AVAILABLE checks curl_cffi import at module load; runtime curl_cffi failure propagates through asyncio.gather return_exceptions=True"
  - "--impersonate block placed outside if is_yt: so SoundCloud, Bandcamp, etc. also get Chrome TLS fingerprint"

patterns-established:
  - "Module-level availability flags: try-import at top of file, checked in functions — no runtime ImportError"
  - "Atomicity for dual code paths: both paths in one commit to prevent contradictory fingerprints from same IP"

requirements-completed:
  - TLS-01a
  - TLS-01b

# Metrics
duration: 8min
completed: 2026-04-14
---

# Phase 2 Plan 02: audio_player.py — Chrome TLS Impersonation (TLS-01a + TLS-01b) Summary

**Chrome JA3/TLS fingerprint applied to both yt-dlp code paths via curl_cffi ImpersonateTarget, with android_vr/ios client contradiction warnings**

## Performance

- **Duration:** ~8 min
- **Started:** 2026-04-14T18:20:00Z
- **Completed:** 2026-04-14T18:28:13Z
- **Tasks:** 2
- **Files modified:** 1

## Accomplishments

- Added `_IMPERSONATE_AVAILABLE` module-level flag using try/except on `yt_dlp.networking.impersonate.ImpersonateTarget` and `curl_cffi` — set once at import, never mutated
- TLS-01a: `get_audio_url()` sets `ydl_opts['impersonate'] = ImpersonateTarget('chrome')` when flag is True, replacing Python urllib's distinctive JA3 fingerprint with Chrome's
- TLS-01b: `_start_ytdlp_stream()` appends `["--impersonate", "chrome"]` to cmd (outside `if is_yt:` — applies to all extractors), with `cmd.append(actual_query)` remaining the final positional element
- Both paths emit a WARNING when android_vr/android/android_music/ios client config is combined with Chrome TLS fingerprint — detectable contradiction logged to console without blocking playback

## Task Commits

1. **Task 1: Add _IMPERSONATE_AVAILABLE module-level flag** - `da10675` (feat)
2. **Task 2: Apply Chrome TLS impersonation to both code paths (TLS-01a + TLS-01b)** - `9d099ea` (feat)

## Files Created/Modified

- `audio_player.py` — Added `_IMPERSONATE_AVAILABLE` flag (lines 16–24), impersonation block in `get_audio_url()` (after ytsearch rewrite, before `with YoutubeDL`), impersonation block in `_start_ytdlp_stream()` (after `if is_yt:` block, before `cmd.append(actual_query)`)

## Decisions Made

- `yt_dlp.networking.impersonate` is the correct import path — `yt_dlp.utils` does not contain `ImpersonateTarget` and raises `ImportError` at startup
- Both TLS-01a and TLS-01b ship in one commit — contradictory fingerprints from the same IP (one path impersonating, one not) is a stronger bot detection signal than no impersonation at all
- `--impersonate chrome` block placed outside `if is_yt:` — TLS fingerprinting applies to SoundCloud, Bandcamp, and other extractors, not only YouTube
- `_IMPERSONATE_AVAILABLE` is checked at module load via `import curl_cffi`; if curl_cffi is corrupted after import, `YoutubeDLError` propagates through `asyncio.gather(return_exceptions=True)` to the caller's Discord error handler

## Deviations from Plan

None — plan executed exactly as written. The verification script in the plan used single-quote string matching `"'--impersonate', 'chrome'"` which does not match the double-quote source form `["--impersonate", "chrome"]`, but this is a cosmetic mismatch in the test script only — the actual requirement (--impersonate chrome in cmd) is met and verified via `grep -- "--impersonate" audio_player.py`.

## Issues Encountered

None — all edits applied cleanly. Syntax check passes on every step.

## Known Stubs

None — no stub values, placeholder text, or unwired data sources introduced.

## Threat Flags

No new threat surface introduced beyond what is documented in the plan's threat model.

## User Setup Required

None — no external service configuration required. curl_cffi availability is handled by Plan 02-01.

## Next Phase Readiness

- Both yt-dlp code paths now present Chrome JA3/TLS fingerprint to YouTube CDN — prerequisite for Phase 3 (Cookie Auth) which adds visitor cookies to further authenticate requests
- Phase 3 can safely add `cookies_from_browser` or `cookiefile` to `ydl_opts` in `get_audio_url()` — the impersonation opt is already set before `with YoutubeDL(...)`
- No regressions to existing commands or playback behavior — when `_IMPERSONATE_AVAILABLE` is False (curl_cffi absent), both paths continue exactly as before

---
*Phase: 02-tls-impersonation*
*Completed: 2026-04-14*
