---
phase: "07"
plan: "02"
subsystem: audio_player, commands
tags: [eq, ffmpeg, commands, tdd, filter, discord]
status: checkpoint
dependency_graph:
  requires: [guild_settings.get_eq_bass, guild_settings.set_eq_bass, guild_settings.get_eq_treble, guild_settings.set_eq_treble, guild_settings.EQ_PRESETS, guild_settings.get_eq_preset_name]
  provides: [audio_player._build_ffmpeg_af_options, audio_player.AudioPlayer.get_eq_for_guild, audio_player.AudioPlayer.set_eq, commands.MusicCog.eq]
  affects: [audio_player.py, commands.py, tests/test_eq_filter.py]
tech_stack:
  added: []
  patterns: [FFmpeg -af filter chain injection, per-guild EQ dict mirroring _guild_bitrates, TDD RED/GREEN for filter builder]
key_files:
  created: [tests/test_eq_filter.py]
  modified: [audio_player.py, commands.py]
decisions:
  - _build_ffmpeg_af_options returns empty string for (0,0) so flat EQ emits no -af flag and is byte-identical to pre-phase behavior
  - ffmpeg_options built as "-vn" + optional af_flag so -vn always appears first (required by FFmpegPCMAudio stream format expectations)
  - set_eq is async to match set_bitrate pattern; next-track-only semantics per D-08 (no live interruption)
  - EQ restore on voice-join mirrors bitrate restore in both play-command join paths (playlist + single-track)
metrics:
  duration_minutes: 9
  completed_date: "2026-04-25"
  tasks_completed: 2
  tasks_total: 3
  files_created: 1
  files_modified: 2
---

# Phase 07 Plan 02: EQ Pipeline Wiring Summary

**One-liner:** FFmpeg EQ filter chain wired into AudioPlayer via `_build_ffmpeg_af_options`, `!eq` command with bass/treble/preset/reset subcommands, EQ footer in now-playing embed, and EQ restore on voice join.

**Status: CHECKPOINT — awaiting human audio + command verification (Task 3)**

## What Was Built

### audio_player.py

- **`_build_ffmpeg_af_options(bass_db, treble_db) -> str`** (module-level): Returns `'-af "bass=g=N,treble=g=M"'` for non-flat EQ, or `""` for (0,0). Only non-zero bands appear in the filter string. Values are interpolated from validated integers so no shell injection is possible (T-07-06).
- **`AudioPlayer._guild_eq: dict[int, tuple[int, int]]`**: Per-guild EQ state initialized to `{}` in `__init__`, mirroring `_guild_bitrates`.
- **`AudioPlayer.get_eq_for_guild(guild_id)`**: Returns `(0, 0)` default when guild absent.
- **`AudioPlayer.set_eq(guild_id, bass_db, treble_db)`**: Stores `(bass_db, treble_db)` for next-track-only semantics (D-08). No live interruption.
- **`play()` updated**: `FFmpegPCMAudio(options=ffmpeg_options)` where `ffmpeg_options = "-vn"` when flat or `"-vn -af \"...\"` when non-flat. Flat EQ is byte-identical to pre-phase behavior.

### commands.py

- **Import extended** with `get_eq_bass`, `set_eq_bass`, `get_eq_treble`, `set_eq_treble`, `EQ_PRESETS`, `get_eq_preset_name` from `guild_settings`.
- **`create_np_embed` footer updated**: Now shows `Audio: {kbps} kbps • EQ: {preset_label} • !bitrate | !eq to change` (D-10).
- **EQ restore on voice join**: Both voice-join paths (playlist flow + single-track flow) now restore saved EQ from `guild_settings.json` immediately after the bitrate restore, mirroring the bitrate pattern (D-09).
- **`!eq` command** (admin-only, D-02):
  - No args: show current `(bass, treble)` + usage hint
  - `!eq bass <N>` / `!eq treble <N>`: parse int, validate via `set_eq_bass`/`set_eq_treble`, persist, update `AudioPlayer.set_eq`, confirm "applies starting next track"
  - `!eq <preset>` (`flat`, `bass-boost`, `treble-boost`, `vocal`): look up from `EQ_PRESETS`, persist both bands, update player, confirm
  - `!eq reset` / `!eq flat`: same as flat preset
  - Unknown subcommand: list valid options
- **`!help` updated** with `!eq` entry.

### tests/test_eq_filter.py (new)

5 unit tests for `_build_ffmpeg_af_options`:
- `test_flat_returns_empty` — (0,0) → ""
- `test_bass_only` — (5,0) → `-af "bass=g=5"`
- `test_treble_only_negative` — (0,-3) → `-af "treble=g=-3"`
- `test_both` — (5,-2) → `-af "bass=g=5,treble=g=-2"`
- `test_boundaries` — (-10,10) → `-af "bass=g=-10,treble=g=10"`

## Task Commits

| Task | Description | Commit | Files |
|------|-------------|--------|-------|
| TDD RED | Failing tests for _build_ffmpeg_af_options | 8da7301 | tests/test_eq_filter.py |
| Task 1 GREEN | EQ filter builder, _guild_eq state, dynamic FFmpegPCMAudio options | bd8e7c8 | audio_player.py |
| Task 2 | !eq command, EQ footer, EQ restore on voice join | 7c30877 | commands.py |

## Test Results

- `tests/test_eq_filter.py`: 5 tests, all pass
- `tests/test_eq_settings.py`: 34 tests, all pass (no regression)
- `tests/test_retry.py`: 43 tests, all pass (no regression)
- Total: 82 tests, all pass

## Verification Commands Run

```
grep -n "_build_ffmpeg_af_options" audio_player.py  # 3 occurrences (def + comment + call)
grep -n "self._guild_eq" audio_player.py             # 3 occurrences (init, getter, setter)
grep -n "def get_eq_for_guild" audio_player.py       # matches
grep -n "async def set_eq" audio_player.py           # matches
grep -n "options=ffmpeg_options" audio_player.py     # matches
grep -n "async def eq" commands.py                   # matches exactly once
grep -c "saved_bass = get_eq_bass" commands.py       # 2 (both voice-join sites)
grep -c "await self._check_admin(ctx)" commands.py   # 3 (fairplay + fairness + eq)
grep -n "EQ: " commands.py                           # footer line present
```

## Checkpoint: Awaiting Task 3

Task 3 is a `checkpoint:human-verify` requiring manual audio verification in a live Discord guild. The 8 scenarios to verify are:

1. **Flat baseline**: Fresh guild plays with footer `EQ: flat`; FFmpeg log shows `-vn` only (no -af)
2. **Bass-boost next-track-only**: `!eq bass-boost` while track A plays; track A unaffected; track B has boosted bass; footer shows `EQ: bass-boost`
3. **Custom preset**: `!eq bass 3` then `!eq treble -2`; `!eq` shows `custom (bass=+3 dB, treble=-2 dB)`
4. **Range validation**: `!eq bass 99`, `!eq bass -50`, `!eq bass abc`, `!eq nonsense` all rejected with error messages
5. **Admin gate**: Non-admin `!eq bass 5` rejected with permission message
6. **Persistence across restart**: `!eq vocal`, stop bot, start bot, `!play` — footer shows `EQ: vocal` from start
7. **Reset**: `!eq reset` → flat audio; `guild_settings.json` shows `eq_bass: 0, eq_treble: 0`
8. **No corruption**: flat EQ → logs show `-vn` only; active EQ → logs show `-vn -af "bass=g=...,treble=g=..."`

## Deviations from Plan

None — plan executed exactly as written. TDD RED/GREEN flow followed for Task 1; filter builder implementation matches spec exactly.

## Known Stubs

None — all EQ accessors read/write real `guild_settings.json`; `AudioPlayer._guild_eq` is populated from real persistence on voice join.

## Threat Flags

None — no new network endpoints, auth paths, or trust boundaries beyond what the plan's threat model covers. EQ values flow through `int()` parse + `set_eq_bass/treble` range validation before reaching FFmpeg argv (T-07-06 mitigated). `!eq` is admin-only (T-07-07 mitigated).

## Self-Check: PASSED

- `tests/test_eq_filter.py` exists with 5 passing tests
- `audio_player.py` contains `_build_ffmpeg_af_options` (definition at line 33, call in play())
- `audio_player.py` contains `_guild_eq`, `get_eq_for_guild`, `set_eq`
- `commands.py` contains `async def eq` and `@commands.command(name="eq")`
- Commits 8da7301, bd8e7c8, 7c30877 verified in git log
