"""A stop while discord.py spawns FFmpeg must not leave the start waiting for ever.

The in-process stream reader's terminate() closes its read end on a daemon thread. Done before
discord.py 2.7.1's popen-stdin-writer thread had read from it, that first read raised
ValueError outside any try: the thread died without closing FFmpeg's stdin, FFmpeg never saw
EOF, and the start's first-frame read blocked a stream-pool worker (of 32) for good. The
command awaiting play() never returned, so a slash /play stayed on "is thinking...", and
enough of them stopped stream opens in every guild.

This drives discord.py's real FFmpegPCMAudio, pipe writer thread included, with a Python
stand-in for the FFmpeg executable that echoes its input at EOF and so, like FFmpeg, only
finishes once its stdin is closed.
"""
import asyncio
import contextlib
import os
import subprocess
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import discord
from test_overtaken_start import INFO_A, _Base, _outcome, _wait_parked
from test_play_abort import _Harness
from test_stream_reader import GV, FakeResponse, _reader, _wait_until

_PIPE_STUB = "import sys; sys.stdout.buffer.write(sys.stdin.buffer.read())"


class _SpawningHarness(_Harness):
    """The in-process reader (over a fake CDN session) as the stream, and the stand-in for
    FFmpeg spawned through discord.py. The spawn is the stage that parks."""

    def __init__(self, stop_at):
        super().__init__(stop_at=stop_at)
        self.spawned = []

    def open_stream(self, *args):
        body = b"\x00" * (1 << 20)    # far more than the pipe holds: the pump parks in os.write
        reader, _ = _reader([FakeResponse(200, body)], url=GV.format(len(body)))
        self.procs.append(reader)
        return reader

    @contextlib.contextmanager
    def patched(self):
        harness = self

        class _Spawning(discord.FFmpegPCMAudio):
            def _spawn_process(self, args, **kwargs):
                harness.park("ffmpeg")
                proc = subprocess.Popen([sys.executable, "-c", _PIPE_STUB],
                                        creationflags=discord.player.CREATE_NO_WINDOW,
                                        **kwargs)
                harness.spawned.append(proc)
                return proc

        with super().patched():
            with patch.object(discord, "FFmpegPCMAudio", lambda *a, **k: _Spawning(*a, **k)):
                yield


class TestStopDuringTheFFmpegSpawn(_Base):

    def test_a_stop_while_discord_py_spawns_ffmpeg_does_not_hang_the_start(self):
        harness = _SpawningHarness(stop_at="ffmpeg")
        self.addCleanup(lambda: [p.kill() for p in harness.spawned])   # unparks a hung read

        async def scenario(player):
            start = asyncio.ensure_future(player.play("a", prefetched_info=dict(INFO_A)))
            await _wait_parked(harness)
            reader = harness.procs[0]
            player.stop_playback()
            # The close runs on a daemon thread: wait for it, so it lands before the
            # writer thread's first read, as it did in the failing run.
            await asyncio.get_running_loop().run_in_executor(
                None, _wait_until, lambda: reader.stdout.closed)
            harness.release.set()
            return await _outcome(start)

        player, outcome = self.run_scenario(harness, scenario)
        self.assertNotIsInstance(outcome, asyncio.TimeoutError, "play() hung after the stop")
        self.assertIsInstance(outcome, asyncio.CancelledError, outcome)
        self.assertIsNone(player._ytdlp_proc)


if __name__ == "__main__":
    unittest.main(verbosity=2)
