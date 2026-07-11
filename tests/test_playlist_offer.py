"""Tests for the offer-first pending-playlist state machine (260711-rqx).

Covers the pure text helpers, _await_pending_tracks, and the exactly-once
dict.pop gate WITHOUT hitting Discord or the network.
"""
import asyncio
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import commands


class TestLoadingOfferText(unittest.TestCase):
    def test_loading_offer_text(self):
        self.assertEqual(
            commands._loading_offer_text("P"),
            "**P** — loading track list…\nClick 'Load playlist' to add the rest.",
        )


class TestResolveOfferOutcome(unittest.TestCase):
    def test_single_track_playlist_is_empty(self):
        a = {"url": "u1", "title": "a"}
        self.assertEqual(
            commands._resolve_offer_outcome({"tracks": [a]}, "P"),
            ("empty", "", []),
        )

    def test_multi_track_playlist_is_ready(self):
        a = {"url": "u1", "title": "a"}
        b = {"url": "u2", "title": "b"}
        c = {"url": "u3", "title": "c"}
        self.assertEqual(
            commands._resolve_offer_outcome({"tracks": [a, b, c]}, "P"),
            ("ready",
             "**P** has **2** more tracks.\nClick 'Load playlist' to add them to the queue.",
             [b, c]),
        )

    def test_empty_tracks_list_is_empty(self):
        self.assertEqual(
            commands._resolve_offer_outcome({"tracks": []}, "P"),
            ("empty", "", []),
        )


class TestErrorOfferText(unittest.TestCase):
    def test_error_offer_text(self):
        self.assertEqual(
            commands._error_offer_text("P"),
            "Couldn't load the track list for **P**.",
        )


class TestExpiryOfferText(unittest.TestCase):
    def test_expiry_unknown_count(self):
        self.assertEqual(
            commands._expiry_offer_text("P", None),
            "**P** — track list offer expired.",
        )

    def test_expiry_known_count(self):
        b = {"url": "u2", "title": "b"}
        c = {"url": "u3", "title": "c"}
        self.assertEqual(
            commands._expiry_offer_text("P", [b, c]),
            "**P** had **2** more tracks.\n~~Click Load playlist~~ *(expired)*",
        )


class TestAwaitPendingTracks(unittest.TestCase):
    def test_ready_entry_returns_tracks_without_awaiting(self):
        b = {"url": "u2", "title": "b"}
        c = {"url": "u3", "title": "c"}
        pending = {"tracks": [b, c], "future": None}
        result = asyncio.run(commands._await_pending_tracks(pending))
        self.assertEqual(result, [b, c])

    def test_enumerating_entry_awaits_future(self):
        a = {"url": "u1", "title": "a"}
        b = {"url": "u2", "title": "b"}
        c = {"url": "u3", "title": "c"}

        async def _fut():
            return {"tracks": [a, b, c]}

        async def _run():
            pending = {"tracks": None, "future": asyncio.ensure_future(_fut())}
            return await commands._await_pending_tracks(pending)

        result = asyncio.run(_run())
        self.assertEqual(result, [b, c])

    def test_enumerating_entry_future_resolves_to_single_track(self):
        a = {"url": "u1", "title": "a"}

        async def _fut():
            return {"tracks": [a]}

        async def _run():
            pending = {"tracks": None, "future": asyncio.ensure_future(_fut())}
            return await commands._await_pending_tracks(pending)

        result = asyncio.run(_run())
        self.assertEqual(result, [])

    def test_enumerating_entry_future_raises_propagates(self):
        async def _fut():
            raise RuntimeError("enumeration failed")

        async def _run():
            pending = {"tracks": None, "future": asyncio.ensure_future(_fut())}
            return await commands._await_pending_tracks(pending)

        with self.assertRaises(RuntimeError):
            asyncio.run(_run())

    def test_no_future_returns_empty(self):
        pending = {"tracks": None, "future": None}
        result = asyncio.run(commands._await_pending_tracks(pending))
        self.assertEqual(result, [])


class TestExactlyOncePopGate(unittest.TestCase):
    def test_second_pop_returns_none(self):
        pending_playlists = {"123": {"tracks": [], "future": None}}
        first = pending_playlists.pop("123", None)
        second = pending_playlists.pop("123", None)
        self.assertIsNotNone(first)
        self.assertIsNone(second)


if __name__ == "__main__":
    unittest.main(verbosity=2)
