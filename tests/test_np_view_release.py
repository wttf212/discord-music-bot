"""Tests that a Now-Playing card's views are released when the card goes away.

PlayerView has no timeout, and discord.py's ViewStore keeps a view until the view
is stopped. Deleting its message does not drop it. Every track sends a new card
and deletes the old one, so without a stop() each card's view stayed in memory
for the life of the process.

The store here is discord.py's real ViewStore. The fake channel stores views the
way discord.py's send() and PartialMessage.edit() do.
"""
import asyncio
import os
import re
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import discord
from discord.ui.view import ViewStore
import commands

GUILD = 1
CHANNEL = 10
NEXT_KEY = (discord.ComponentType.button.value, "btn_next")


class _Message:
    def __init__(self, channel, msg_id):
        self.channel = channel
        self.id = msg_id
        self.deleted = False

    async def edit(self, *, view):
        if self.deleted:
            raise discord.NotFound(SimpleNamespace(status=404, reason="Not Found"),
                                   {"code": 10008, "message": "Unknown Message"})
        store = self.channel.store                        # as PartialMessage.edit does
        store.remove_message_tracking(self.id)
        if view and not view.is_finished() and view.is_dispatchable():
            store.add_view(view, self.id)

    async def delete(self):
        self.deleted = True       # discord.py's MESSAGE_DELETE leaves the store alone


class _Channel:
    def __init__(self, store):
        self.store = store
        self.guild = SimpleNamespace(id=GUILD)
        self.messages = {}

    async def send(self, *, view):
        msg = _Message(self, 1000 + len(self.messages))
        self.messages[msg.id] = msg
        if view and not view.is_finished() and view.is_dispatchable():   # as send() does
            self.store.add_view(view, msg.id)
        return msg

    def get_partial_message(self, msg_id):
        return self.messages[msg_id]


class _Bot:
    config = {"audio": {"bitrate": 128}}
    pending_playlists: dict = {}

    def __init__(self, gs, channel):
        self._gs = gs
        self._channel = channel

    def get_guild_state(self, guild_id):
        return self._gs

    def get_channel(self, channel_id):
        return self._channel

    def get_guild(self, guild_id):
        return None

    def get_user(self, uid):
        return None


class TestNowPlayingViewsAreReleased(unittest.TestCase):

    def setUp(self):
        for name in ("_refresh_weather_if_stale", "_refresh_sky_if_stale", "_refresh_f1_if_stale",
                     "_refresh_rocket_if_stale", "_refresh_kp_if_stale"):
            p = patch.object(commands, name, AsyncMock())    # no network
            p.start()
            self.addCleanup(p.stop)
        self.store = ViewStore(None)
        self.channel = _Channel(self.store)
        self.gs = SimpleNamespace(np_message_id=None, np_views=[], prev_votes=set(),
                                  playpause_votes=set(), stop_votes=set(), next_votes=set())
        self.bot = _Bot(self.gs, self.channel)

    def _card(self, title="t"):
        # guild_id=None: a live card with every control row, built from defaults.
        return commands.build_player_view(self.bot, title)

    def _run(self, scenario):
        """Run scenario() on an event loop, as the bot does. discord.py gives a view
        the future that stop() resolves only when the view is built inside a running
        loop. Afterwards, let the deletes and edits the scenario scheduled finish."""
        async def main():
            result = await scenario()
            for _ in range(5):
                await asyncio.sleep(0)
            return result
        return asyncio.run(main())

    async def _send(self, view):
        await commands.send_new_np(self.bot, CHANNEL, view)

    async def _edit(self, view):
        await commands.update_np_embed(self.bot, CHANNEL, view)

    def _views_held(self):
        """Every view discord.py's store still references."""
        held = {id(v): v for v in self.store._synced_message_views.values()}
        for items in self.store._views.values():
            for item in items.values():
                held[id(item.view)] = item.view
        return list(held.values())

    def test_a_replaced_card_releases_its_view(self):
        async def scenario():
            old, new = self._card("a"), self._card("b")
            await self._send(old)
            await self._send(new)
            return old, new
        old, new = self._run(scenario)
        self.assertTrue(old.is_finished(), "the replaced card's view must be stopped")
        self.assertFalse(new.is_finished())
        self.assertEqual(self._views_held(), [new])

    def test_a_hundred_tracks_leave_one_view(self):
        async def scenario():
            for i in range(100):
                await self._send(self._card(f"t{i}"))
        self._run(scenario)
        self.assertEqual(len(self._views_held()), 1)

    def test_an_in_place_edit_leaves_the_live_buttons_registered(self):
        async def scenario():
            edited = self._card("a")
            await self._send(self._card("a"))
            await self._edit(edited)
            return edited
        edited = self._run(scenario)
        self.assertFalse(edited.is_finished())
        self.assertIs(self.store._views[self.gs.np_message_id][NEXT_KEY].view, edited,
                      "the ► button must still reach the card's current view")

    def test_every_view_an_edited_card_held_is_released_with_it(self):
        async def scenario():
            first, edited, nxt = self._card("a"), self._card("a"), self._card("b")
            await self._send(first)
            await self._edit(edited)
            await asyncio.sleep(0)     # the edit lands
            await self._send(nxt)
            return first, edited, nxt
        first, edited, nxt = self._run(scenario)
        self.assertTrue(first.is_finished())
        self.assertTrue(edited.is_finished())
        self.assertEqual(self._views_held(), [nxt])

    def test_a_view_a_button_put_on_the_card_is_released_with_it(self):
        """_edit_card (play/pause, loop, shuffle) stores its view untracked. It has the
        card's custom_ids, so stopping the tracked view unregisters it as well."""
        async def scenario():
            clicked, nxt = self._card("a"), self._card("b")
            await self._send(self._card("a"))
            card = self.channel.messages[self.gs.np_message_id]
            interaction = SimpleNamespace(edit_original_response=card.edit, message=card)
            await commands._edit_card(interaction, clicked)
            await self._send(nxt)
            return nxt
        nxt = self._run(scenario)
        self.assertEqual(self._views_held(), [nxt])

    def test_a_deleted_card_releases_its_view(self):
        async def scenario():
            card = self._card("a")
            await self._send(card)
            await commands._clear_np(self.bot, CHANNEL)
            return card
        card = self._run(scenario)
        self.assertTrue(card.is_finished())
        self.assertEqual(self._views_held(), [])

    def test_an_edit_that_lands_after_the_card_was_replaced_is_released(self):
        """update_np_embed edits in a background task, so the card can be replaced
        between scheduling the edit and the edit landing on the old message."""
        async def scenario():
            late_edit, nxt = self._card("a"), self._card("b")
            await self._send(self._card("a"))
            await self._edit(late_edit)     # scheduled, not landed yet
            await self._send(nxt)           # the card is replaced first
            return late_edit, nxt
        late_edit, nxt = self._run(scenario)
        self.assertTrue(late_edit.is_finished())
        self.assertEqual(self._views_held(), [nxt])


class TestGuildStateHoldsTheCardViews(unittest.TestCase):

    def test_guild_state_declares_np_views(self):
        """Read main.py as text: importing it starts the bgutil server."""
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "main.py")
        with open(path, encoding="utf-8") as f:
            src = f.read()
        self.assertTrue(re.search(r"^\s+np_views: list = field\(default_factory=list\)", src, re.M),
                        "GuildState must declare np_views")


if __name__ == "__main__":
    unittest.main(verbosity=2)
