# SonicScript AI

A production-ready YouTube transcription web app with precise timeline selection, automatic multilingual speech recognition, noise-aware audio preprocessing, timestamped playback, AI refinement, and TXT/SRT/VTT/JSON exports.

![Python](https://img.shields.io/badge/Python-3.12-17324d) ![FastAPI](https://img.shields.io/badge/FastAPI-0.116-0c9c72) ![Render](https://img.shields.io/badge/Deploy-Render-4de6b1)

## What it does

- Inspects a public YouTube URL and fetches its true title, thumbnail, and runtime.
- Lets the user transcribe the entire video or choose an exact start/end timestamp.
- Downloads only the selected timeline when possible, rather than always fetching the full video.
- Uses FFmpeg to create a seekable audio overview and a separate speech-optimized, noise-reduced stream.
- Splits long selections into overlapping 10-minute chunks so requests stay within provider limits without losing words at chunk boundaries.
- Uses Whisper Large v3 with **automatic language detection**. It does not request translation, so Bangla, English, and code-switched speech stay in their spoken languages.
- Shows timestamped transcript segments that seek the selected audio when clicked.
- Keeps an immutable original transcript and creates AI refinement as a separate version.
- Exports TXT, SRT, VTT, and structured JSON. Subtitle timestamps map back to the original video's timeline.
- Automatically removes temporary job files after the configured TTL.

> Accuracy depends on the source audio and speech model. The preprocessing pipeline improves many noisy recordings, but no automatic speech recognizer can guarantee perfect text for every recording. The app deliberately presents refinement as a review aid and always preserves the original.

## Architecture

- **Web/API:** FastAPI + static responsive frontend
- **Media:** `yt-dlp`, FFmpeg, and FFprobe
- **Speech recognition:** OpenAI-compatible audio transcription endpoint; defaults to Groq `whisper-large-v3`
- **Refinement:** OpenAI-compatible chat endpoint; defaults to Groq `llama-3.3-70b-versatile`
- **Storage:** temporary local job directories (appropriate for Render's ephemeral disk)

No YouTube captions are read or required.

## Deploy to Render

The repository includes both `render.yaml` and a Dockerfile. Docker is used so FFmpeg is always installed.

1. Fork or push this repository to GitHub.
2. In Render, choose **New → Blueprint** and connect the repository.
3. Render detects `render.yaml` and creates the web service.
4. Set the requested secret environment variable:
   - `GROQ_API_KEY`: a Groq API key with access to the configured Whisper and chat models.
5. Deploy. The health endpoint is `/health`.

The Starter plan or larger is recommended for enough memory, temporary disk, and request concurrency. The web server must use one process because job state is kept in memory; the included command does that.

### YouTube verification on cloud hosts

YouTube occasionally asks datacenter IPs to verify that a request is not automated. If this occurs for videos you are authorized to process, mount a Netscape-format cookie file as a Render secret file and set:

```env
YTDLP_COOKIES_FILE=/path/to/secret/youtube-cookies.txt
```

Do not commit browser cookies to Git. Cookies expire and must be maintained by the deployer. Follow YouTube's terms and transcribe only content you are allowed to process.

## Local development

Requirements: Python 3.12+, FFmpeg/FFprobe, and a Groq API key.

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# Export values from .env using your preferred environment loader:
export GROQ_API_KEY="gsk_..."
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

Open <http://localhost:8000>. Interactive API docs are at <http://localhost:8000/api/docs>.

You can also run the production container:

```bash
docker build -t sonicscript-ai .
docker run --rm -p 10000:10000 -e GROQ_API_KEY="gsk_..." sonicscript-ai
```

## Configuration

| Variable | Default | Purpose |
|---|---:|---|
| `GROQ_API_KEY` | — | Default key for transcription and refinement |
| `TRANSCRIPTION_API_KEY` | `GROQ_API_KEY` | Optional dedicated speech API key |
| `TRANSCRIPTION_BASE_URL` | `https://api.groq.com/openai/v1` | OpenAI-compatible API root |
| `TRANSCRIPTION_MODEL` | `whisper-large-v3` | Speech-to-text model |
| `REFINEMENT_API_KEY` | `GROQ_API_KEY` | Optional dedicated refinement key |
| `REFINEMENT_BASE_URL` | transcription URL | OpenAI-compatible chat API root |
| `REFINEMENT_MODEL` | `llama-3.3-70b-versatile` | Transcript editor model |
| `MAX_VIDEO_DURATION_SECONDS` | `21600` | Maximum accepted video runtime (6 hours) |
| `TRANSCRIPTION_CHUNK_SECONDS` | `600` | Chunk size sent to speech model |
| `MAX_CONCURRENT_JOBS` | `2` | Simultaneously processed jobs per instance |
| `JOB_TTL_SECONDS` | `14400` | Idle job/audio retention (4 hours) |
| `ENABLE_DENOISE` | `true` | FFmpeg speech denoising stage |
| `YTDLP_COOKIES_FILE` | — | Optional server-side YouTube cookies path |

If using OpenAI instead of Groq, set the transcription/refinement base URLs, model names, and API keys explicitly. The selected audio chunks are mono 16 kHz MP3 files.

## API overview

- `POST /api/videos/inspect` — validate URL and retrieve metadata
- `POST /api/jobs` — start a timeline transcription
- `GET /api/jobs/{id}` — poll job/transcript/refinement state
- `DELETE /api/jobs/{id}` — cancel and remove a job
- `GET /api/jobs/{id}/audio` — stream the selected audio overview
- `POST /api/jobs/{id}/refine` — start non-destructive AI refinement
- `GET /api/jobs/{id}/download?format=srt` — export transcript
- `GET /health` — deployment health check

## Tests

```bash
python -m unittest discover -s tests -v
python -m compileall -q app
node --check app/static/app.js
```

## Privacy and operational notes

- URLs, selected audio, and transcripts are processed by the server and the configured AI provider.
- Job data is not stored in a database. It lives in memory and under `/tmp/sonicscript-jobs`, then expires after the TTL or disappears on a restart/deploy.
- Do not run multiple Uvicorn workers without moving job state and files to shared storage/queues.
- Rate limits and usage costs are determined by the configured provider.
