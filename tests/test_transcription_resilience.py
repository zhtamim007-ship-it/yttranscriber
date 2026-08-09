import asyncio
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import httpx

from app.services import (
    Job,
    JobManager,
    ServiceError,
    _backoff_delay,
    _fallback_encoding_for_duration,
    _is_transient_status,
    _transcription_failure_message,
)

# --- Test doubles -----------------------------------------------------------


class FakeResponse:
    def __init__(self, status_code, payload=None, text="", headers=None):
        self.status_code = status_code
        self._payload = payload
        self.text = text
        self.headers = headers or {}

    def json(self):
        if self._payload is not None:
            return self._payload
        raise ValueError("not json")


class FakeClient:
    """Records every POST and plays back a scripted list of responses."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0
        self.kwargs_list = []

    async def post(self, *args, **kwargs):
        self.calls += 1
        self.kwargs_list.append(kwargs)
        next_item = self.responses.pop(0)
        if isinstance(next_item, BaseException):
            raise next_item
        return next_item


def make_job(directory: Path) -> Job:
    return Job(
        id="testjob",
        url="https://youtube.com/watch?v=abc123",
        video={"title": "Test video", "duration": 60},
        start_seconds=0,
        end_seconds=60,
        directory=directory,
    )


class _FakeSettings:
    transcription_base_url = "https://api.groq.com/openai/v1"
    transcription_api_key = "gsk_test"
    transcription_model = "whisper-large-v3"
    transcription_model_fallbacks = ("whisper-large-v3-turbo",)
    transcription_max_attempts = 5
    transcription_retry_after_cap = 45


def ok_payload():
    return {"text": "hello", "language": "en", "segments": [{"start": 0, "end": 1.2, "text": "hello"}]}


# --- Pure helper tests ------------------------------------------------------


class TransientStatusTests(unittest.TestCase):
    def test_transient_codes_are_retryable(self):
        for code in (408, 429, 500, 502, 503, 504):
            with self.subTest(code=code):
                self.assertTrue(_is_transient_status(code))
        self.assertTrue(_is_transient_status(None))  # network failure

    def test_permanent_codes_are_not_transient(self):
        for code in (200, 400, 401, 403, 404, 413, 415, 422):
            with self.subTest(code=code):
                self.assertFalse(_is_transient_status(code))


class BackoffTests(unittest.TestCase):
    def test_exponential_backoff_within_bounds(self):
        for attempt in range(6):
            delay = _backoff_delay(attempt)
            self.assertGreaterEqual(delay, 0.5 * 0.9)
            self.assertLessEqual(delay, 45.0 * 1.1)

    def test_retry_after_header_is_honored(self):
        for attempt in (0, 3):
            delay = _backoff_delay(attempt, "12")
            self.assertGreaterEqual(delay, 12 * 0.9)
            self.assertLessEqual(delay, 12 * 1.1)

    def test_backoff_is_capped(self):
        delay = _backoff_delay(99)
        self.assertLessEqual(delay, 45.0 * 1.1)

    def test_invalid_retry_after_falls_back_to_exponential(self):
        delay = _backoff_delay(1, "not-a-number")
        self.assertLessEqual(delay, 2.0 * 1.1)


class FallbackEncodingChoiceTests(unittest.TestCase):
    def test_short_chunks_use_wav(self):
        self.assertEqual(_fallback_encoding_for_duration(600), "wav")
        self.assertEqual(_fallback_encoding_for_duration(750), "wav")

    def test_long_chunks_use_mp3_to_stay_under_25mb(self):
        self.assertEqual(_fallback_encoding_for_duration(800), "mp3")
        self.assertEqual(_fallback_encoding_for_duration(3600), "mp3")


class FailureMessageTests(unittest.TestCase):
    def test_auth_message(self):
        msg = _transcription_failure_message(401, "invalid api key", ["whisper-large-v3"], saw_404=False)
        self.assertIn("API key", msg)

    def test_model_not_found_message(self):
        msg = _transcription_failure_message(404, "model not found", ["whisper-large-v3"], saw_404=True)
        self.assertIn("not found", msg)

    def test_transient_500_message_is_actionable(self):
        msg = _transcription_failure_message(500, "Internal server error", ["whisper-large-v3"], saw_404=False)
        self.assertIn("500", msg)
        self.assertIn("provider", msg.lower())
        self.assertIn("Retry", msg)

    def test_network_error_message(self):
        msg = _transcription_failure_message(None, "Read timed out", ["whisper-large-v3"], saw_404=False)
        self.assertIn("network", msg.lower())

    def test_permanent_rejection_keeps_original_shape(self):
        msg = _transcription_failure_message(400, "bad file", ["whisper-large-v3"], saw_404=False)
        self.assertIn("The speech model could not process this audio.", msg)


class JobPublicTests(unittest.TestCase):
    def test_public_exposes_retryable_flag(self):
        with tempfile.TemporaryDirectory() as td:
            job = make_job(Path(td))
            payload = job.public()
            self.assertIn("retryable", payload)
            self.assertFalse(payload["retryable"])
            job.retryable = True
            self.assertTrue(job.public()["retryable"])


# --- _request_transcription behaviour tests ---------------------------------


class RequestTranscriptionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.manager = JobManager()
        self._tmp = tempfile.TemporaryDirectory()
        self.job = make_job(Path(self._tmp.name))
        self.chunk = Path(self._tmp.name) / "speech-0000.mp3"
        self.chunk.write_bytes(b"fake audio bytes")
        self.settings_patcher = mock.patch("app.services.settings", _FakeSettings())
        self.settings_patcher.start()
        self.backoff_patcher = mock.patch("app.services._backoff_delay", return_value=0.0)
        self.backoff_patcher.start()

    async def asyncTearDown(self):
        self.backoff_patcher.stop()
        self.settings_patcher.stop()
        self._tmp.cleanup()

    async def test_success_on_first_try(self):
        client = FakeClient([FakeResponse(200, ok_payload())])
        result = await self.manager._request_transcription(client, self.chunk, self.job)
        self.assertEqual(result["text"], "hello")
        self.assertEqual(client.calls, 1)
        self.assertFalse(self.job.retryable)
        sent = client.kwargs_list[0]
        self.assertEqual(sent["data"]["model"], "whisper-large-v3")

    async def test_transient_500_then_success(self):
        client = FakeClient([FakeResponse(500, {"error": {"message": "Internal server error"}}), FakeResponse(200, ok_payload())])
        result = await self.manager._request_transcription(client, self.chunk, self.job)
        self.assertEqual(result["text"], "hello")
        self.assertEqual(client.calls, 2)
        self.assertFalse(self.job.retryable)

    async def test_network_error_then_success(self):
        client = FakeClient([httpx.ReadTimeout("read timed out"), FakeResponse(200, ok_payload())])
        result = await self.manager._request_transcription(client, self.chunk, self.job)
        self.assertEqual(result["text"], "hello")
        self.assertEqual(client.calls, 2)

    async def test_persistent_500_marks_job_retryable(self):
        error = FakeResponse(500, {"error": {"message": "Internal server error"}})
        # Primary model: 5 attempts on the prepared mp3, then 2 on the fallback
        # encoding (none available -> skipped), then the fallback model: 2
        # attempts on the prepared mp3. All fail -> transient error.
        client = FakeClient([error] * 9)
        async def no_fallback(*args, **kwargs):
            return None
        with mock.patch.object(self.manager, "_make_fallback_encoding", new=no_fallback):
            with self.assertRaises(ServiceError) as ctx:
                await self.manager._request_transcription(client, self.chunk, self.job)
        self.assertIn("500", str(ctx.exception))
        self.assertIn("provider", str(ctx.exception).lower())
        self.assertTrue(self.job.retryable)
        self.assertEqual(client.calls, 7)  # 5 primary + 2 fallback model

    async def test_auth_failure_fails_fast(self):
        client = FakeClient([FakeResponse(401, {"error": {"message": "invalid api key"}})])
        with self.assertRaises(ServiceError) as ctx:
            await self.manager._request_transcription(client, self.chunk, self.job)
        self.assertIn("API key", str(ctx.exception))
        self.assertEqual(client.calls, 1)
        self.assertFalse(self.job.retryable)

    async def test_permanent_400_rejection_does_not_retry_same_file(self):
        # The primary model rejects the file once (no point retrying the same
        # file); the fallback encoding is unavailable (ffmpeg-less test env),
        # so the fallback model also tries the same file exactly once.
        client = FakeClient([FakeResponse(400, {"error": {"message": "bad audio"}})] * 3)
        with self.assertRaises(ServiceError) as ctx:
            await self.manager._request_transcription(client, self.chunk, self.job)
        self.assertIn("could not process this audio", str(ctx.exception))
        self.assertEqual(client.calls, 2)  # 1 primary + 1 fallback-model try
        self.assertFalse(self.job.retryable)

    async def test_wav_fallback_used_after_transient_500s(self):
        error = FakeResponse(500, {"error": {"message": "Internal server error"}})
        fallback = Path(self._tmp.name) / "speech-0000.wav"
        fallback.write_bytes(b"RIFF fake wav")
        client = FakeClient([error] * 5 + [FakeResponse(200, ok_payload())])
        async def fake_fallback(*args, **kwargs):
            return fallback
        with mock.patch.object(self.manager, "_make_fallback_encoding", new=fake_fallback):
            result = await self.manager._request_transcription(client, self.chunk, self.job)
        self.assertEqual(result["text"], "hello")
        self.assertEqual(client.calls, 6)
        wav_call = client.kwargs_list[5]
        self.assertEqual(wav_call["files"]["file"][2], "audio/wav")
        self.assertFalse(self.job.retryable)

    async def test_model_fallback_used_after_primary_exhausted(self):
        error = FakeResponse(503, {"error": {"message": "overloaded"}})
        client = FakeClient([error] * 5 + [FakeResponse(200, ok_payload())])
        async def no_fallback(*args, **kwargs):
            return None
        with mock.patch.object(self.manager, "_make_fallback_encoding", new=no_fallback):
            result = await self.manager._request_transcription(client, self.chunk, self.job)
        self.assertEqual(result["text"], "hello")
        self.assertEqual(client.calls, 6)
        first_model = client.kwargs_list[0]["data"]["model"]
        fallback_model = client.kwargs_list[5]["data"]["model"]
        self.assertEqual(first_model, "whisper-large-v3")
        self.assertEqual(fallback_model, "whisper-large-v3-turbo")
        self.assertFalse(self.job.retryable)

    async def test_invalid_json_on_2xx_is_retried(self):
        bad = FakeResponse(200, text="<html>gateway</html>")
        client = FakeClient([bad, FakeResponse(200, ok_payload())])
        result = await self.manager._request_transcription(client, self.chunk, self.job)
        self.assertEqual(result["text"], "hello")
        self.assertEqual(client.calls, 2)


# --- Retry manager tests ----------------------------------------------------


class RetryManagerTests(unittest.IsolatedAsyncioTestCase):
    async def test_retry_resets_job_state(self):
        manager = JobManager()
        with tempfile.TemporaryDirectory() as td:
            job = make_job(Path(td))
            job.status = "failed"
            job.stage = "Transcription stopped"
            job.error = "boom"
            job.retryable = True
            job.segments = [{"start": 0, "end": 1, "text": "x"}]
            job.transcript = "x"
            job.languages = [{"code": "en", "name": "English", "parts": 1}]
            job.refined_transcript = "x"
            job.refinement_status = "failed"
            manager.retry(job)
            self.assertEqual(job.status, "queued")
            self.assertIsNone(job.error)
            self.assertFalse(job.retryable)
            self.assertEqual(job.segments, [])
            self.assertEqual(job.transcript, "")
            self.assertEqual(job.languages, [])
            self.assertIsNone(job.refined_transcript)
            self.assertEqual(job.refinement_status, "idle")
            self.assertIsNotNone(job.task)
            job.task.cancel()
            try:
                await job.task
            except asyncio.CancelledError:
                pass


# --- Real ffmpeg fallback re-encode test ------------------------------------


class FallbackEncodeIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_wav_fallback_is_valid_pcm_16k_mono(self):
        ffmpeg = shutil.which("ffmpeg") or os.getenv("FFMPEG_BIN")
        if not ffmpeg:
            self.skipTest("ffmpeg binary not available")
        manager = JobManager()
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            job = make_job(td)
            source = td / "speech-0000.mp3"
            subprocess.run(
                [ffmpeg, "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
                 "-ac", "1", "-ar", "16000", "-c:a", "libmp3lame", "-b:a", "64k", str(source)],
                capture_output=True, check=True,
            )
            duration = mock.AsyncMock(return_value=2.0)
            with mock.patch.object(manager, "_audio_duration", new=duration):
                output = await manager._make_fallback_encoding(job, source)
            self.assertIsNotNone(output)
            self.assertEqual(output.suffix, ".wav")
            data = output.read_bytes()
            self.assertEqual(data[:4], b"RIFF")
            self.assertEqual(data[8:12], b"WAVE")
            self.assertEqual(data[20:22], b"\x01\x00")  # PCM
            self.assertEqual(int.from_bytes(data[22:24], "little"), 1)  # mono
            self.assertEqual(int.from_bytes(data[24:28], "little"), 16000)  # 16 kHz
            output.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
