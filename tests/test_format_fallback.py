import unittest

from app.services import (
    _JS_RUNTIME_MESSAGE,
    _YD_DOWNLOAD_STRATEGIES,
    _YD_PLAYER_CLIENT_SETS,
    _base_ydl_options,
    _is_bot_error,
    _is_format_unavailable,
    _is_js_runtime_missing,
)


class FormatFallbackTests(unittest.TestCase):
    def test_inspect_starts_with_ytdlp_default_client_set(self):
        # yt-dlp's built-in default set adapts to the available JS runtime and
        # auth state, so it must be tried before any pinned client set.
        self.assertIsNone(_YD_PLAYER_CLIENT_SETS[0])
        self.assertGreater(len([s for s in _YD_PLAYER_CLIENT_SETS if s]), 1)

    def test_inspect_tries_runtime_free_client_before_dependent_ones(self):
        # android_vr works without a JS runtime or PO token; it must be tried
        # before clients whose formats are dropped without a runtime (web/tv)
        # or require a GVS PO token (android/ios/mweb).
        def first_index(member):
            for index, client_set in enumerate(_YD_PLAYER_CLIENT_SETS):
                if client_set and member in client_set:
                    return index
            return None

        vr_index = first_index("android_vr")
        self.assertIsNotNone(vr_index)
        for dependent in ("web", "tv", "android", "ios", "mweb"):
            dep_index = first_index(dependent)
            if dep_index is not None:
                self.assertLessEqual(vr_index, dep_index, f"android_vr should be tried before {dependent}")

    def test_download_strategies_try_default_client_set_first(self):
        # Must try yt-dlp's adaptive default set with audio-only first.
        self.assertEqual(_YD_DOWNLOAD_STRATEGIES[0], (None, "bestaudio/best"))
        self.assertEqual(_YD_DOWNLOAD_STRATEGIES[1], (None, None))

    def test_download_strategies_cover_multiple_sets_and_formats(self):
        client_sets = [c for c, _ in _YD_DOWNLOAD_STRATEGIES]
        self.assertIn(None, client_sets)  # includes yt-dlp default client set
        # More than one distinct pinned client set is tried.
        self.assertGreater(len(set(map(tuple, (c or [] for c, _ in _YD_DOWNLOAD_STRATEGIES)))), 1)
        # Audio-only selection is attempted for the runtime-free client set too.
        pinned = {tuple(c) for c, f in _YD_DOWNLOAD_STRATEGIES if c and f == "bestaudio/best"}
        self.assertIn(("android_vr", "web_embedded"), pinned)

    def test_format_unavailable_detection(self):
        self.assertTrue(_is_format_unavailable("ERROR: Requested format is not available. Use --list-formats"))
        self.assertTrue(_is_format_unavailable("No formats are available"))
        self.assertFalse(_is_format_unavailable("The video is private"))

    def test_bot_error_detection(self):
        self.assertTrue(_is_bot_error("Sign in to confirm you're not a bot"))
        self.assertTrue(_is_bot_error("use --cookies to authenticate"))
        self.assertFalse(_is_bot_error("The video is unavailable"))

    def test_js_runtime_missing_detection(self):
        self.assertTrue(_is_js_runtime_missing(
            "No supported JavaScript runtime could be found. Only deno is enabled by default"
        ))
        self.assertTrue(_is_js_runtime_missing("Signature solving failed: Some formats may be missing"))
        self.assertTrue(_is_js_runtime_missing("n challenge solving failed: Some formats may be missing"))
        self.assertFalse(_is_js_runtime_missing("Requested format is not available"))
        self.assertIn("Deno", _JS_RUNTIME_MESSAGE)

    def test_base_options_enable_deno_and_node_runtimes(self):
        # Both runtimes must be enabled, deno first (yt-dlp's preference order).
        # Enabling only node disabled deno and left servers without a usable
        # YouTube challenge solver.
        opts = _base_ydl_options()
        self.assertEqual(list(opts["js_runtimes"].keys()), ["deno", "node"])

    def test_base_options_keep_default_client_when_none(self):
        opts = _base_ydl_options(player_client=None)
        self.assertNotIn("extractor_args", opts)

    def test_base_options_pin_client_when_provided(self):
        opts = _base_ydl_options(player_client=["tv"])
        self.assertEqual(opts["extractor_args"], {"youtube": {"player_client": ["tv"]}})


if __name__ == "__main__":
    unittest.main()
