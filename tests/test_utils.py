import unittest

from app.utils import (
    parse_clock,
    segments_to_srt,
    segments_to_vtt,
    validate_youtube_url,
)


class UrlValidationTests(unittest.TestCase):
    def test_accepts_standard_youtube_urls(self):
        self.assertEqual(
            validate_youtube_url("https://www.youtube.com/watch?v=abc123"),
            "https://www.youtube.com/watch?v=abc123",
        )
        self.assertEqual(validate_youtube_url("youtu.be/abc123"), "https://youtu.be/abc123")

    def test_rejects_non_youtube_hosts(self):
        with self.assertRaises(ValueError):
            validate_youtube_url("https://example.com/watch?v=abc123")
        with self.assertRaises(ValueError):
            validate_youtube_url("https://youtube.com.evil.example/watch?v=abc123")


class TimelineTests(unittest.TestCase):
    def test_parses_supported_timestamps(self):
        self.assertEqual(parse_clock("42"), 42)
        self.assertEqual(parse_clock("02:03"), 123)
        self.assertEqual(parse_clock("1:02:03.5"), 3723.5)

    def test_rejects_malformed_timestamps(self):
        for value in ("", "1:99", "hello", "1:2:3:4"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_clock(value)

    def test_subtitle_exports_can_use_original_video_offset(self):
        segments = [{"start": 0.25, "end": 2.5, "text": "বাংলা and English"}]
        srt = segments_to_srt(segments, offset=60)
        vtt = segments_to_vtt(segments, offset=60)
        self.assertIn("00:01:00,250 --> 00:01:02,500", srt)
        self.assertIn("00:01:00.250 --> 00:01:02.500", vtt)
        self.assertIn("বাংলা and English", srt)


if __name__ == "__main__":
    unittest.main()
