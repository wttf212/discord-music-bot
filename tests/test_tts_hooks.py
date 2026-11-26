"""Speech hooks: a skip in the gap only cuts the speech; a drained queue still speaks; leaving
drops the speech still waiting."""
import asyncio, logging, os, sys, unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, mock_open, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import commands
from test_manual_dead_track import CHANNEL_ID, _Bot, _Harness, _Player, _VC, _guild_state
from test_player_speech import CLIP, _player
from track_queue import TrackQueue


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


class _VoiceInAnEmptyChannel(_VC):
    """Everyone has left the bot's voice channel."""

    def __init__(self, guild=None):
        self.guild, self.channel, self.connected = guild, SimpleNamespace(members=[]), True

    def is_connected(self):
        return self.connected

    async def disconnect(self, *, force=False):
        self.connected = False


class TestLeavingDropsWaitingSpeech(_Harness):
    """The spec: leaving the voice channel drops the clips still waiting and the update's start
    time. Left behind, five waiting !ttsafter clips made every later !tts on the server answer
    "Too many messages waiting" until someone used !play or !radio."""

    def test_the_drained_chain_leaving_an_empty_channel(self):
        player = _Player()
        player._voice_client = _VoiceInAnEmptyChannel()
        player.speech = _FakeSpeech()
        player.speech.reset = MagicMock()
        gs = _guild_state(player, "off", current="p")
        gs.fairness_pct = 50
        bot = _Bot(gs)
        asyncio.run(commands._auto_next(bot, CHANNEL_ID, 1, gs.auto_next_gen))
        self.assertTrue(any("Leaving" in (m or "") for m in bot.sent), bot.sent)
        player.speech.reset.assert_called_once_with()

    def test_the_empty_channel_leave(self):
        import main
        handlers = {}
        guild = SimpleNamespace(id=1)
        guild.voice_client = voice = _VoiceInAnEmptyChannel(guild)
        player = _player(voice)
        gs = SimpleNamespace(player=player, queue=TrackQueue(), auto_next_task=None, auto_next_gen=1,
                             empty_channel_task=None, current_text_channel_id=None)

        class _StandInBot:                  # just enough for main() to register its handlers
            tts_status = "Text-to-speech is off."

            def __init__(self, config):
                pass

            def event(self, fn):
                handlers[fn.__name__] = fn
                return fn

            def run(self, token):
                pass

            def get_guild_state(self, guild_id):
                return gs

            def get_guild(self, guild_id):
                return guild

            def get_channel(self, channel_id):
                return None

        levels = {n: logging.getLogger(n).level for n in ("discord.voice_state", "discord.player")}
        self.addCleanup(lambda: [logging.getLogger(n).setLevel(lv) for n, lv in levels.items()])
        with patch.object(main, "MusicBot", _StandInBot), \
                patch.object(main, "configure_fast_client"), \
                patch.object(main.yaml, "safe_load", return_value={"bot_token": "t"}), \
                patch("builtins.open", mock_open(read_data="")), \
                patch.dict(os.environ, {"DISCORD_BOT_TOKEN": ""}):
            main.main()

        async def everyone_leaves():
            player.speech.queue_after(CLIP)          # a !ttsafter waiting for the song change
            player.speech.track_started_at = 100.0
            await handlers["on_voice_state_update"](
                SimpleNamespace(guild=guild, bot=False),
                SimpleNamespace(channel=voice.channel), SimpleNamespace(channel=None))
            for _ in range(100):                     # the leave runs in its own task
                if not voice.connected:
                    break
                await asyncio.sleep(0.01)

        asyncio.run(everyone_leaves())
        self.assertFalse(voice.connected, "precondition: the bot left")
        self.assertEqual(player.speech.waiting_clips(), 0, "a waiting clip outlived the leave")
        self.assertIsNone(player.speech.track_started_at)


if __name__ == "__main__":
    unittest.main(verbosity=2)
