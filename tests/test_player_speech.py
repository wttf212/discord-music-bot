"""AudioPlayer + Speech: gap speech plays before the song, music is always wrapped."""
import asyncio, contextlib, os, sys, threading, time, unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import discord

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import audio_player, commands, speech
from track_queue import Track, TrackQueue
from test_play_abort import FRAME, INFO, _FakeVoice, _Harness

CLIP = FRAME * 3


class _OneAtATimeVoice(_FakeVoice):
    """Refuses a second play() like discord.py; finish() is the current source running out."""

    def __init__(self):
        super().__init__()
        self.plays, self.source, self.after, self.stops = [], None, None, 0

    def is_playing(self):
        return self.source is not None

    def play(self, source, after=None, **encoder_settings):
        if self.source is not None:
            raise discord.ClientException("Already playing audio.")
        self.played = self.source = source
        self.after = after
        self.plays.append(source)

    def stop(self):
        self.stops += 1
        self.finish()

    def finish(self):
        source, after = self.source, self.after
        self.source = self.after = None
        if source is not None and after:
            after(None)


class _LeavingVoice(_OneAtATimeVoice):
    """discord.py 2.7.1's disconnect(): the connection reads as closed before its first await,
    and the guild drops the client only once Discord confirms the leave."""

    def __init__(self, guild, channel):
        super().__init__()
        self.guild, self.channel, self.connected = guild, channel, True

    def is_connected(self):
        return self.connected

    def play(self, source, after=None, **encoder_settings):
        if not self.connected:
            raise discord.ClientException("Not connected to voice.")
        super().play(source, after, **encoder_settings)

    async def disconnect(self, *, force=False):
        self.stop()
        self.connected = False
        await asyncio.sleep(0.05)
        self.guild.voice_client = None


def _player(voice):
    with patch.object(audio_player, "_find_ffmpeg", return_value="ffmpeg"):
        player = audio_player.AudioPlayer({"audio": {"sample_rate": 48000, "channels": 2},
                                           "youtube": {"client": "web"}})
    player._voice_client = voice
    return player


class TestPlayerSpeech(unittest.TestCase):
    def test_music_is_wrapped_in_a_mixer(self):
        harness, voice = _Harness(), _OneAtATimeVoice()
        player = _player(voice)
        self.addCleanup(lambda: [b.cleanup() for b in harness.buffers])
        with harness.patched():
            asyncio.run(player.play("q", prefetched_info=dict(INFO)))
        self.assertIsInstance(voice.played, speech.SpeechMixer)
        self.assertIs(voice.played.music, harness.buffers[0])
        self.assertIsNotNone(player.speech.track_started_at)

    def test_after_song_speech_plays_before_the_next_song(self):
        harness, voice = _Harness(), _OneAtATimeVoice()
        player = _player(voice)
        self.addCleanup(lambda: [b.cleanup() for b in harness.buffers])

        async def go():
            player.speech.queue_after(CLIP)
            task = asyncio.ensure_future(player.play("q", prefetched_info=dict(INFO)))
            for _ in range(200):
                await asyncio.sleep(0.01)
                if voice.plays:
                    break
            self.assertIsNone(voice.plays[0].music)
            await asyncio.sleep(0.2)
            self.assertFalse(task.done(), "the song must wait for the gap speech")
            voice.finish()
            await asyncio.wait_for(task, 5)
            self.assertEqual(len(voice.plays), 2)
            self.assertIs(voice.plays[1].music, harness.buffers[0])
            self.assertEqual(voice.stops, 0, "play()'s own stop must not cut the gap speech")

        with harness.patched():
            asyncio.run(go())

    def test_explicit_stop_cuts_speech(self):
        voice = _OneAtATimeVoice()
        player = _player(voice)

        async def go():
            await player.speech.say(CLIP)
            player.stop_playback()
            for _ in range(5):
                await asyncio.sleep(0)
            self.assertEqual(voice.stops, 1)
            self.assertFalse(player.speech.solo_active)

        asyncio.run(go())

    def test_failed_start_plays_held_clips_alone(self):
        voice = _OneAtATimeVoice()
        player = _player(voice)
        gate = threading.Event()

        def failing_open(*args):
            gate.wait(5)
            raise audio_player.StreamFailedError("dead track")

        async def go():
            task = asyncio.ensure_future(player.play("q", prefetched_info=dict(INFO)))
            await asyncio.sleep(0.05)
            self.assertEqual(await player.speech.say(CLIP), "held")
            gate.set()
            with self.assertRaises(audio_player.StreamFailedError):
                await task
            self.assertEqual(len(voice.plays), 1)
            self.assertIsNone(voice.plays[0].music)

        with patch.object(audio_player, "_open_audio_stream", failing_open):
            asyncio.run(go())

    def test_new_voice_client_resets_speech(self):
        player = _player(_OneAtATimeVoice())
        player.speech.queue_after(CLIP)
        player.set_voice_client(_OneAtATimeVoice())
        self.assertEqual(player.speech.waiting_clips(), 0)

    def test_resyncing_the_same_voice_client_keeps_waiting_speech(self):
        # Voice recovery hands the player the client it already has: nobody left the channel.
        voice = _OneAtATimeVoice()
        player = _player(voice)
        player.speech.queue_after(CLIP)
        player.speech.track_started_at = 123.0
        player.set_voice_client(voice)
        self.assertEqual(player.speech.waiting_clips(), 1, "an accepted !ttsafter clip was dropped")
        self.assertEqual(player.speech.track_started_at, 123.0, "the update's start time was dropped")

    def test_radio_is_wrapped_too(self):
        class _Radio(discord.AudioSource):
            _process = SimpleNamespace(stderr=[])

            def read(self):
                return FRAME

            def is_opus(self):
                return False

        voice = _OneAtATimeVoice()
        player = _player(voice)
        track = Track(query="r", title="R", requested_by="u", url="http://radio", is_radio=True)
        with patch.object(discord, "FFmpegPCMAudio", lambda *a, **k: _Radio()):
            asyncio.run(player.play_radio(track))
        self.assertIsInstance(voice.played, speech.SpeechMixer)


class TestRadioStartTeardown(unittest.TestCase):
    """A radio start that never plays stops its FFmpeg and leaves the player idle, as a song
    start does. Left flagged as playing, wait_for_playback() never returns and auto-play stalls."""

    def _radio_start(self, voice, stop_in_the_gap=False):
        harness, outcome = _Harness(), {}
        player = _player(voice)
        track = Track(query="r", title="R", requested_by="u", url="http://radio", is_radio=True)

        async def go():
            parked = asyncio.Event()
            if stop_in_the_gap:
                real_wait_for_gap = player.speech.wait_for_gap

                async def wait_for_gap(gen):
                    parked.set()
                    await real_wait_for_gap(gen)

                player.speech.wait_for_gap = wait_for_gap
                player.speech.queue_after(CLIP)     # the fake voice never finishes speaking it
            task = asyncio.ensure_future(player.play_radio(track))
            if stop_in_the_gap:
                await asyncio.wait_for(parked.wait(), 5)
                player.stop_playback()
            try:
                outcome["result"] = await task
            except BaseException as e:
                outcome["error"] = e
            for _ in range(300):                    # the teardown runs on a daemon thread
                if all(f.cleaned.is_set() for f in harness.ffmpegs):
                    break
                await asyncio.sleep(0.01)

        with harness.patched():
            asyncio.run(go())
        self.assertEqual(len(harness.ffmpegs), 1, "precondition: the station's FFmpeg was started")
        self.assertTrue(harness.ffmpegs[0].cleaned.is_set(), "the station's FFmpeg was left running")
        self.assertFalse(player.is_playing, "a radio start that never began still flags it as playing")
        self.assertIsNone(player.current_track_title)
        self.assertTrue(player._playback_done.is_set(),
                        "wait_for_playback() would wait forever for a station that never started")
        return outcome

    def test_voice_client_play_raising(self):
        voice = _FakeVoice(play_error=discord.ClientException("Not connected to voice."))
        outcome = self._radio_start(voice)
        self.assertIsInstance(outcome.get("error"), discord.ClientException, outcome)

    def test_a_stop_during_the_gap(self):
        voice = _FakeVoice()
        outcome = self._radio_start(voice, stop_in_the_gap=True)
        self.assertIsInstance(outcome.get("error"), asyncio.CancelledError, outcome)
        self.assertIsNone(voice.played.music, "the station reached the voice client after the stop")


def _patched_commands():
    """The commands-layer patches every end-to-end test below shares."""
    stack = contextlib.ExitStack()
    for target, value in (("_VOICE_RECOVERY_POLL", 0.01),
                          ("check_channel", AsyncMock(return_value=True)),
                          ("_check_vote", MagicMock(return_value=(True, ""))),
                          ("_on_cooldown", MagicMock(return_value=False)),
                          ("update_np_stopped", AsyncMock()),
                          ("send_new_np", AsyncMock()),
                          ("build_player_view", lambda *a, **k: None),
                          ("_get_requester_name", lambda *a, **k: "u"),
                          ("_schedule_prefetch", lambda *a, **k: None),
                          ("_schedule_autoplay_topup", lambda *a, **k: None)):
        stack.enter_context(patch.object(commands, target, value))
    return stack


class _Guild:
    """One guild with the real AudioPlayer, Speech and handlers: the bot is in voice with a
    listener, and connect() records a rejoin."""

    def __init__(self, test, tracks=("b",), history=(), current=None):
        self.harness, self.rejoins, self.sent, self.edits = _Harness(), [], [], []
        test.addCleanup(lambda: [b.cleanup() for b in self.harness.buffers])
        self.guild = SimpleNamespace(id=1, voice_client=None)
        self.channel = SimpleNamespace(members=[SimpleNamespace(bot=False)], connect=self._connect)
        self.guild.voice_client = self.voice = _LeavingVoice(self.guild, self.channel)
        self.player = _player(self.voice)
        self.gs = SimpleNamespace(queue=TrackQueue(), player=self.player, auto_next_task=None,
                                  auto_next_gen=1, autoplay=False, current_text_channel_id=None)
        self.gs.queue._history.extend(self._track(q) for q in history)
        self.gs.queue.current = self._track(current) if current else None
        for q in tracks:
            self.gs.queue.add(self._track(q))
        self.text = SimpleNamespace(id=10, send=self._send)
        self.bot = SimpleNamespace(get_guild_state=lambda gid: self.gs,
                                   get_guild=lambda gid: self.guild,
                                   get_channel=lambda cid: self.text, command_prefix="!")
        self.ctx = SimpleNamespace(bot=self.bot, guild=self.guild, channel=self.text,
                                   send=self._send, interaction=None,
                                   author=SimpleNamespace(id=42, voice=SimpleNamespace(channel=self.channel)))
        self.cog = SimpleNamespace(bot=self.bot)
        self.row = SimpleNamespace(view=SimpleNamespace(
            bot=self.bot, evaluate_vote=AsyncMock(return_value=True)))
        self.parked = None

    @staticmethod
    def _track(q):
        return Track(query=q, title=q, requested_by="u", resolved_info=dict(INFO, title=q),
                     resolved_at=time.time())

    async def _connect(self):
        self.rejoins.append(_LeavingVoice(self.guild, self.channel))
        self.guild.voice_client = self.rejoins[-1]
        return self.rejoins[-1]

    async def _send(self, content=None, **kwargs):
        self.sent.append(content)

    def _click(self):
        return SimpleNamespace(guild_id=1, user=SimpleNamespace(id=42), channel=self.text,
                               response=SimpleNamespace(defer=AsyncMock(), is_done=lambda: True),
                               followup=SimpleNamespace(send=AsyncMock(side_effect=self._send)))

    def watch_the_gap(self):
        """Set self.parked once a start waits out the gap speech."""
        self.parked = asyncio.Event()
        real_wait_for_gap = self.player.speech.wait_for_gap

        async def wait_for_gap(gen):
            self.parked.set()
            await real_wait_for_gap(gen)

        self.player.speech.wait_for_gap = wait_for_gap

    def advance(self, how):
        """Start the next song the way `how` does; returns the task that runs its play()."""
        if how == "chain":
            commands._start_auto_next(self.bot, 10, 1)
            return self.gs.auto_next_task
        if how == "!skip":
            return asyncio.ensure_future(commands.MusicCog.skip.callback(self.cog, self.ctx))
        if how == "!skipto 1":
            return asyncio.ensure_future(commands.MusicCog.skipto.callback(self.cog, self.ctx, 1))
        if how == "►":
            return asyncio.ensure_future(commands._ControlsRow.next_callback(self.row, self._click(), None))
        if how == "◄":
            return asyncio.ensure_future(commands._ControlsRow.prev_callback(self.row, self._click(), None))
        if how == "radio":
            async def edit(content=None, **kwargs):
                self.edits.append(content)
            picker = SimpleNamespace(edit=edit, delete=AsyncMock())
            return asyncio.ensure_future(commands._play_radio_selected(
                self.bot, self.ctx, {"url_resolved": "http://radio", "name": "R"}, picker))
        raise ValueError(how)

    async def stop(self, how):
        if how == "!stop":
            await commands.MusicCog.stop.callback(self.cog, self.ctx)
        else:  # the ■ button
            await commands._ControlsRow.stop_callback(self.row, self._click(), None)

    def songs_played(self):
        """Which pipelines reached the voice client, in order: 0 is the first one built."""
        index = {id(b): i for i, b in enumerate(self.harness.buffers)}
        return [index.get(id(p.music), "radio") for p in self.voice.plays if p.music is not None]


class TestStopDuringGapSpeech(unittest.TestCase):
    """!stop or ■ while the next song waits out the gap speech. The stop ends the speech, which
    releases the start onto a connection that is closing. Whoever holds that start, the
    auto-next chain or a !skip or ► handler, must not take that for a voice drop: put the
    track back, rejoin the channel and play it."""

    def _stop_during_the_gap(self, holder, stopper):
        g, outcome = _Guild(self), {}

        async def scenario():
            g.watch_the_gap()
            g.player.speech.queue_after(CLIP)       # a waiting !ttsafter clip
            start = g.advance(holder)               # pops "b"; its start waits out the clip
            outcome["gen"] = g.gs.auto_next_gen
            await asyncio.wait_for(g.parked.wait(), 5)
            await g.stop(stopper)
            await asyncio.wait({start}, timeout=1)
            outcome["start ended"] = start.done()
            await asyncio.sleep(0.3)                # room to wait out a "voice drop" and rejoin
            outcome["chain left"] = g.gs.auto_next_task is not None and not g.gs.auto_next_task.done()

        with g.harness.patched(), _patched_commands():
            asyncio.run(scenario())
        with self.subTest(holder=holder, stopper=stopper):
            self.assertEqual(g.rejoins, [], "the bot rejoined the voice channel after the stop")
            self.assertFalse(any("lost the voice connection" in (m or "").lower() for m in g.sent), g.sent)
            self.assertTrue(outcome["start ended"], "the start outlived the stop")
            self.assertFalse(outcome["chain left"], "an auto-next chain outlived the stop")
            self.assertEqual(g.songs_played(), [], "a song reached the voice client after the stop")
            self.assertEqual(g.gs.queue.list(), [])
            self.assertFalse(g.player.is_playing)
        return g, outcome

    def test_stop_ends_auto_play_for_good(self):
        for stopper in ("!stop", "■"):
            g, outcome = self._stop_during_the_gap("chain", stopper)
            with self.subTest(stopper=stopper):
                self.assertGreater(g.gs.auto_next_gen, outcome["gen"],
                                   "the stop must retire the chain's generation")

    def test_stop_ends_a_manual_advance_for_good(self):
        for holder in ("!skip", "►"):
            for stopper in ("!stop", "■"):
                self._stop_during_the_gap(holder, stopper)


class TestNewerStartDuringGapSpeech(unittest.TestCase):
    """!skip waits out the gap speech for b, and a newer start comes meanwhile: ◄, !skipto or a
    radio station. The newer start's stop ends the speech and releases b's start, which must
    then give up: it is not the track anyone asked for any more, and the voice client is the
    newer start's. Auto-play then carries on from the newer track."""

    def _newer_start_during_the_gap(self, newer, **queue):
        g = _Guild(self, **queue)

        async def scenario():
            g.watch_the_gap()
            g.player.speech.queue_after(CLIP)
            skip = g.advance("!skip")                # b's start waits out the clip
            await asyncio.wait_for(g.parked.wait(), 5)
            newer_start = g.advance(newer)
            await asyncio.wait({skip, newer_start}, timeout=2)
            for _ in range(100):                     # the newer track reaches the voice client
                if g.voice.source is not None and g.voice.source.music is not None:
                    break
                await asyncio.sleep(0.01)
            g.on_air = g.songs_played()
            g.chain = g.gs.auto_next_task
            g.voice.finish()                         # the newer track ends by itself
            for _ in range(200):                     # auto-next starts the one after it
                if len(g.songs_played()) > len(g.on_air):
                    break
                await asyncio.sleep(0.01)

        with g.harness.patched(), _patched_commands():
            asyncio.run(scenario())
        messages = g.sent + g.edits
        self.assertFalse(any("already playing" in (m or "").lower() for m in messages), messages)
        self.assertIsNotNone(g.chain, "the newer start left auto-play off")
        return g

    def test_previous_button(self):
        g = self._newer_start_during_the_gap("◄", tracks=("b", "c"), history=("h",), current="p")
        # Built: b's pipeline (0), the previous track's (1), then auto-next's next one (2).
        self.assertEqual(g.on_air, [1], "b's superseded start took the voice client")
        self.assertEqual(g.songs_played(), [1, 2])
        self.assertEqual([t.query for t in g.gs.queue.list()], ["c"])

    def test_skipto(self):
        g = self._newer_start_during_the_gap("!skipto 1", tracks=("b", "c", "d"))
        self.assertEqual(g.on_air, [1], "b's superseded start took the voice client")
        self.assertEqual(g.songs_played(), [1, 2])
        self.assertEqual([t.query for t in g.gs.queue.list()], [])

    def test_radio_pick(self):
        g = self._newer_start_during_the_gap("radio", tracks=("b", "c"))
        self.assertEqual(g.on_air, ["radio"], "b's superseded start took the voice client")
        self.assertEqual(g.songs_played(), ["radio", 1], "auto-play stalled after the station")
