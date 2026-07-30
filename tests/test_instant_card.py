"""Tests for the instant "Loading…" card shown before a resolve completes.

A cold play spends ~2s resolving plus up to ~3s waiting out the CDN settle window.
The video id is known from the URL immediately, and YouTube serves thumbnails off
i.ytimg.com from the id alone — Discord's CDN fetches the image, so artwork can be on
screen in ~200ms at zero request cost to our IP.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import discord
import commands as cmds
from commands import _youtube_thumbnail, build_player_view, _ControlsRow


class FakeBot:
    config = {"audio": {"bitrate": 128}}
    pending_playlists: dict = {}

    def get_guild_state(self, guild_id):   # never called with guild_id=None
        raise AssertionError("guild state should not be needed for a guild-less card")

    def get_guild(self, guild_id):
        return None

    def get_user(self, uid):
        return None


def _container(view):
    return view.children[0]


def _kinds(view):
    return [type(i).__name__ for i in _container(view).children]


def _texts(view):
    """All TextDisplay content, descending into Sections (the title lives in one
    whenever the card has artwork)."""
    out = []
    for item in _container(view).children:
        if isinstance(item, discord.ui.TextDisplay):
            out.append(item.content)
        for sub in getattr(item, "children", None) or []:
            if isinstance(sub, discord.ui.TextDisplay):
                out.append(sub.content)
    return out


def _thumbnails(view):
    out = []
    for item in _container(view).children:
        accessory = getattr(item, "accessory", None)
        if isinstance(accessory, discord.ui.Thumbnail):
            out.append(accessory.media.url)
    return out


class TestYoutubeThumbnail(unittest.TestCase):

    def test_watch_url(self):
        self.assertEqual(_youtube_thumbnail("https://www.youtube.com/watch?v=dQw4w9WgXcQ"),
                         "https://i.ytimg.com/vi/dQw4w9WgXcQ/hqdefault.jpg")

    def test_short_url(self):
        self.assertEqual(_youtube_thumbnail("https://youtu.be/dQw4w9WgXcQ"),
                         "https://i.ytimg.com/vi/dQw4w9WgXcQ/hqdefault.jpg")

    def test_music_youtube_with_extra_params(self):
        self.assertEqual(
            _youtube_thumbnail("https://music.youtube.com/watch?v=sZKiMu4QUfY&si=wBRe"),
            "https://i.ytimg.com/vi/sZKiMu4QUfY/hqdefault.jpg")

    def test_text_query_has_no_thumbnail(self):
        self.assertEqual(_youtube_thumbnail("rick astley never gonna give you up"), "")

    def test_non_youtube_url_has_no_thumbnail(self):
        self.assertEqual(_youtube_thumbnail("https://soundcloud.com/artist/track"), "")

    def test_empty_and_none_are_safe(self):
        self.assertEqual(_youtube_thumbnail(""), "")
        self.assertEqual(_youtube_thumbnail(None), "")

    def test_uses_hqdefault_which_always_exists(self):
        """maxresdefault 404s for plenty of videos; hqdefault is always present."""
        self.assertIn("hqdefault", _youtube_thumbnail("https://youtu.be/abc12345678"))


class TestLoadingCard(unittest.TestCase):

    def _loading(self, **kw):
        params = dict(thumbnail=_youtube_thumbnail("https://youtu.be/dQw4w9WgXcQ"),
                      url="https://youtu.be/dQw4w9WgXcQ", loading=True, guild_id=None)
        params.update(kw)
        return build_player_view(FakeBot(), "Loading…", **params)

    def test_header_says_loading(self):
        self.assertEqual(_texts(self._loading())[0], "**Loading…**")

    def test_shows_artwork_before_the_resolve(self):
        self.assertEqual(_thumbnails(self._loading()),
                         ["https://i.ytimg.com/vi/dQw4w9WgXcQ/hqdefault.jpg"])

    def test_has_no_controls(self):
        """Nothing is playing yet — skip/pause/stop would hit the previous track."""
        self.assertNotIn("_ControlsRow", _kinds(self._loading()))
        self.assertNotIn("_SecondaryRow", _kinds(self._loading()))

    def test_title_links_to_the_requested_url(self):
        body = " ".join(_texts(self._loading()))
        self.assertIn("[Loading…](https://youtu.be/dQw4w9WgXcQ)", body)

    def test_text_query_still_renders_without_artwork(self):
        view = self._loading(thumbnail="", url="")
        self.assertEqual(_thumbnails(view), [])
        self.assertEqual(_texts(view)[0], "**Loading…**")


class TestNormalCardUnchanged(unittest.TestCase):
    """Regression guard: adding `loading` must not alter the live card."""

    def _live(self):
        return build_player_view(FakeBot(), "Song", thumbnail="https://x/y.jpg",
                                 url="https://youtu.be/abc12345678", guild_id=None)

    def test_header_is_now_playing(self):
        self.assertEqual(_texts(self._live())[0], "**Now Playing**")

    def test_controls_present(self):
        self.assertIn("_ControlsRow", _kinds(self._live()))

    def test_paused_and_finished_still_win_over_default(self):
        paused = build_player_view(FakeBot(), "S", guild_id=None, paused=True)
        finished = build_player_view(FakeBot(), "", guild_id=None, finished=True)
        self.assertEqual(_texts(paused)[0], "**Paused**")
        self.assertEqual(_texts(finished)[0], "**Queue finished**")

    def test_finished_card_has_no_controls(self):
        finished = build_player_view(FakeBot(), "", guild_id=None, finished=True)
        self.assertNotIn("_ControlsRow", _kinds(finished))


class TestPlayPathWiring(unittest.TestCase):

    def test_play_sends_loading_card_then_edits_in_place(self):
        import inspect
        src = inspect.getsource(cmds.MusicCog.play.callback)
        self.assertIn("loading=True", src)
        self.assertIn("await send_new_np(self.bot, channel_id, loading_view)", src)
        self.assertIn("await update_np_embed(self.bot, channel_id, view)", src,
                      "the live card must replace the loading card by editing it")
        self.assertNotIn('await ctx.send("Resolving…")', src)

    def test_failure_clears_the_loading_card(self):
        """A stuck 'Loading…' card is worse than no card."""
        import inspect
        src = inspect.getsource(cmds.MusicCog.play.callback)
        self.assertIn("_clear_np", src)
        clear_src = inspect.getsource(cmds._clear_np)
        self.assertIn("gs.np_message_id = None", clear_src)


if __name__ == "__main__":
    unittest.main(verbosity=2)
