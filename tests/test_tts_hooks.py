"""Speech hooks: a skip in the gap only cuts the speech; a drained queue still speaks."""
import asyncio, os, sys, unittest
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import commands
from test_manual_dead_track import CHANNEL_ID, _Bot, _Harness, _Player, _advance, _guild_state


class _FakeSpeech:
    def __init__(self, in_gap=False):
        self.in_gap = in_gap
        self.cut = MagicMock()
        self.skip_gap = MagicMock()
        self.end_of_queue = AsyncMock()


def _setup(in_gap, queued):
    player = _Player()
    player.speech = _FakeSpeech(in_gap)
    gs = _guild_state(player, "off", current="p", queued=queued)
    return player, gs


class TestSpeechHooks(_Harness):
    def test_skip_or_next_in_the_gap_only_cuts_the_speech(self):
        for handler in ("skip", "next"):
            with self.subTest(handler=handler):
                player, gs = _setup(in_gap=True, queued=("g1",))
                sent = self._run(handler, gs)
                player.speech.skip_gap.assert_called_once_with()
                player.speech.cut.assert_not_called()   # that would drop the clips held for the song
                self.assertEqual(player.calls, [])
                self.assertEqual([t.query for t in gs.queue], ["g1"])
                self.assertTrue(any("announcement" in (m or "") for m in sent))

    def test_skip_outside_the_gap_advances_as_before(self):
        player, gs = _setup(in_gap=False, queued=("g1",))
        self._run("skip", gs)
        self.assertEqual(player.calls, ["g1"])
        player.speech.cut.assert_not_called()
        player.speech.skip_gap.assert_not_called()

    def test_skip_or_next_with_an_empty_queue_speaks_waiting_speech(self):
        for handler in ("skip", "next"):
            with self.subTest(handler=handler):
                player, gs = _setup(in_gap=False, queued=())
                self._run(handler, gs)
                player.speech.end_of_queue.assert_awaited_once()

    def test_drained_chain_speaks_waiting_speech(self):
        player, gs = _setup(in_gap=False, queued=())
        asyncio.run(commands._auto_next(_Bot(gs), CHANNEL_ID, 1, gs.auto_next_gen))
        player.speech.end_of_queue.assert_awaited_once()
        commands.update_np_stopped.assert_awaited()

    def test_players_without_speech_are_untouched(self):
        player = _Player()
        gs = _guild_state(player, "off", current="p")
        self._run("skip", gs)                      # no AttributeError
        self.assertEqual(player.calls, [])
        asyncio.run(commands._auto_next(_Bot(gs), CHANNEL_ID, 1, gs.auto_next_gen))
        self.assertEqual(commands.update_np_stopped.await_count, 2)
