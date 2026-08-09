import unittest

from app.services import (
    _YD_DOWNLOAD_STRATEGIES,
    _YD_PLAYER_CLIENT_SETS,
    _base_ydl_options,
    _is_bot_error,
    _is_format_unavailable,
)


class FormatFallbackTests(unittest.TestCase):
    def test_client_sets_include_default_and_alternates(self):
        # The default pinned set stays first, and yt-dlp's default set is available as a fallback.
        self.assertEqual(_YD_PLAYER_CLIENT_SETS[0], ["android", "web"])
        self.assertIn(None, _YD_PLAYER_CLIENT_SETS)

    def test_download_strategies_try_multiple_clients_and_formats(self):
        # Must try efficient audio-only first, then fall back.
        self.assertEqual(_YD_DOWNLOAD_STRATEGIES[0], (["android", "web"], "bestaudio/best"))
        client_sets = [c for c, _ in _YD_DOWNLOAD_STRATEGIES]
        self.assertIn(None, client_sets)  # includes yt-dlp default client set
        # More than one distinct client set is tried.
        self.assertGreater(len(set(map(tuple, (c or [] for c, _ in _YD_DOWNLOAD_STRATEGIES)))), 1)

    def test_format_unavailable_detection(self):
        self.assertTrue(_is_format_unavailable("ERROR: Requested format is not available. Use --list-formats"))
        self.assertTrue(_is_format_unavailable("No formats are available"))
        self.assertFalse(_is_format_unavailable("The video is private"))

    def test_bot_error_detection(self):
        self.assertTrue(_is_bot_error("Sign in to confirm you're not a bot"))
        self.assertTrue(_is_bot_error("use --cookies to authenticate"))
        self.assertFalse(_is_bot_error("The video is unavailable"))

    def test_base_options_keep_default_client_when_none(self):
        opts = _base_ydl_options(player_client=None)
        self.assertNotIn("extractor_args", opts)

    def test_base_options_pin_client_when_provided(self):
        opts = _base_ydl_options(player_client=["tv"])
        self.assertEqual(opts["extractor_args"], {"youtube": {"player_client": ["tv"]}})


if __name__ == "__main__":
    unittest.main()
