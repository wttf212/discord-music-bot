"""Tests for interaction acknowledgement under load.

Discord invalidates an interaction token 3s after the click. A real session produced
`NotFound 404 (10062): Unknown interaction` from the skip button while the bot was
mid-resolve — the skip worked, but the clicker saw "This interaction failed".

Two rules: acknowledge before doing any other work, and never let a dead token raise.
"""
import asyncio
import inspect
import os
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import discord
import commands as cmds
from commands import _ack, _respond, _edit_card


def _http_error(status=404, code=10062):
    resp = MagicMock()
    resp.status = status
    resp.reason = "Not Found"
    return discord.NotFound(resp, {"code": code, "message": "Unknown interaction"})


def _interaction(*, defer_raises=None, already_done=False):
    it = MagicMock()
    it.response.is_done.return_value = already_done
    it.response.defer = AsyncMock(side_effect=defer_raises)
    it.response.send_message = AsyncMock()
    it.followup.send = AsyncMock()
    it.edit_original_response = AsyncMock()
    it.message.edit = AsyncMock()
    return it


class TestAck(unittest.TestCase):

    def test_normal_defer_succeeds(self):
        it = _interaction()
        self.assertTrue(asyncio.run(_ack(it)))
        it.response.defer.assert_awaited_once()

    def test_expired_token_returns_false_and_does_not_raise(self):
        """10062 must not propagate — the action it triggered is still valid."""
        it = _interaction(defer_raises=_http_error())
        self.assertFalse(asyncio.run(_ack(it)))

    def test_already_responded_counts_as_acknowledged(self):
        it = _interaction(defer_raises=discord.InteractionResponded(MagicMock()))
        self.assertTrue(asyncio.run(_ack(it)))


class TestRespond(unittest.TestCase):

    def test_uses_followup_after_defer(self):
        it = _interaction(already_done=True)
        asyncio.run(_respond(it, "nope"))
        it.followup.send.assert_awaited_once()
        it.response.send_message.assert_not_awaited()

    def test_uses_response_when_not_yet_deferred(self):
        it = _interaction(already_done=False)
        asyncio.run(_respond(it, "nope"))
        it.response.send_message.assert_awaited_once()

    def test_dead_token_is_swallowed(self):
        it = _interaction(already_done=True)
        it.followup.send = AsyncMock(side_effect=_http_error())
        asyncio.run(_respond(it, "nope"))   # must not raise


class TestEditCard(unittest.TestCase):

    def test_edits_via_interaction_when_alive(self):
        it = _interaction()
        asyncio.run(_edit_card(it, MagicMock()))
        it.edit_original_response.assert_awaited_once()
        it.message.edit.assert_not_awaited()

    def test_falls_back_to_message_edit_when_token_dead(self):
        """The bot token never expires, so the card still updates."""
        it = _interaction()
        it.edit_original_response = AsyncMock(side_effect=_http_error())
        asyncio.run(_edit_card(it, MagicMock()))
        it.message.edit.assert_awaited_once()

    def test_never_raises_when_both_fail(self):
        it = _interaction()
        it.edit_original_response = AsyncMock(side_effect=_http_error())
        it.message.edit = AsyncMock(side_effect=RuntimeError("gone"))
        asyncio.run(_edit_card(it, MagicMock()))


class TestCallbacksAcknowledgeFirst(unittest.TestCase):
    """The 3s budget must not be spent on votes or cooldown checks."""

    CALLBACKS = ("playpause_callback", "prev_callback", "next_callback",
                 "stop_callback", "queue_callback")
    SECONDARY = ("loop_callback", "shuffle_callback", "grab_callback")

    def _body(self, name):
        row = cmds._ControlsRow if hasattr(cmds._ControlsRow, name) else cmds._SecondaryRow
        fn = getattr(row, name)
        return inspect.getsource(getattr(fn, "callback", fn))

    def test_ack_is_the_first_await(self):
        for name in self.CALLBACKS + self.SECONDARY:
            src = self._body(name)
            awaits = [ln.strip() for ln in src.splitlines() if "await " in ln]
            self.assertTrue(awaits, f"{name}: no awaits found")
            self.assertIn("_ack(interaction)", awaits[0],
                          f"{name} must acknowledge before anything else, got: {awaits[0]}")

    def test_no_raw_defer_left_in_transport_buttons(self):
        for name in self.CALLBACKS + self.SECONDARY:
            self.assertNotIn("interaction.response.defer()", self._body(name),
                             f"{name} should go through _ack")

    def test_no_raw_send_message_after_defer(self):
        """response.send_message after a defer raises InteractionResponded."""
        for name in self.CALLBACKS + self.SECONDARY:
            self.assertNotIn("interaction.response.send_message", self._body(name),
                             f"{name} must use _respond, which is followup-aware")

    def test_card_editing_callbacks_do_not_use_response_edit_message(self):
        """edit_message is unavailable once deferred — must go through _edit_card,
        which also survives an expired token by editing the message directly."""
        for name in ("playpause_callback", "loop_callback", "shuffle_callback"):
            src = self._body(name)
            self.assertNotIn("interaction.response.edit_message", src, name)
            self.assertIn("_edit_card(interaction", src, name)

    def test_grab_acknowledges_before_dming(self):
        """The DM is a network round-trip; doing it first risks the 3s token."""
        src = self._body("grab_callback")
        ack_at = src.index("_ack(interaction)")
        dm_at = src.index("interaction.user.send")
        self.assertLess(ack_at, dm_at, "grab must acknowledge before sending the DM")


class TestPrefetchFloor(unittest.TestCase):

    def test_floor_is_not_slower_than_the_skip_cooldown_allows(self):
        """A floor far above the 2s skip cooldown guarantees prefetch falls behind."""
        self.assertLessEqual(cmds._PREFETCH_MIN_INTERVAL, 4.0)

    def test_floor_still_bounds_bursts(self):
        self.assertGreaterEqual(cmds._PREFETCH_MIN_INTERVAL, 2.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
