# SonicScript AI

A production-ready YouTube transcription web app with precise timeline selection, automatic multilingual speech recognition, noise-aware audio preprocessing, timestamped playback, AI refinement, and TXT/SRT/VTT/JSON exports.

![Python](https://img.shields.io/badge/Python-3.12-17324d)
![FastAPI](https://img.shields.io/badge/FastAPI-0.116-0c9c72)
![Render](https://img.shields.io/badge/Deploy-Render-4de6b1)

## What it does

- Inspects a public YouTube URL and fetches its title, thumbnail, and runtime.
- Lets the user transcribe the entire video or choose an exact start and end timestamp.
- Downloads only the selected timeline when possible instead of always fetching the complete video.
- Uses FFmpeg to create a seekable audio overview and a separate speech-optimized, noise-reduced audio stream.
- Splits long selections into overlapping 10-minute chunks so requests stay within provider limits without losing words at chunk boundaries.
- Uses Whisper Large v3 with automatic language detection.
- Preserves Bangla, English, other languages, and code-switched speech without translating them.
- Shows timestamped transcript segments that seek the selected audio when clicked.
- Keeps the original transcript and creates AI refinement as a separate version.
- Exports TXT, SRT, VTT, and structured JSON.
- Maps subtitle timestamps back to the original video's timeline.
- Automatically removes temporary job files after the configured expiration time.

> Accuracy depends on the source audio and speech-recognition model. The preprocessing pipeline can improve noisy recordings, but no automatic speech recognizer can guarantee perfect results for every recording. AI refinement is provided as a review aid, and the original transcript is always preserved.

## Architecture

- **Web and API:** FastAPI with a responsive static frontend
- **Media processing:** `yt-dlp`, FFmpeg, and FFprobe
- **Speech recognition:** OpenAI-compatible transcription API using Groq `whisper-large-v3` by default
- **AI refinement:** OpenAI-compatible chat API using Groq `llama-3.3-70b-versatile` by default
- **Storage:** Temporary local job directories under `/tmp`

YouTube captions are not required or used.

## Deploy to Render Free

The repository includes a `render.yaml` file and a Dockerfile.

Docker is used so that FFmpeg, FFprobe, Node.js, Python, and the required application dependencies are installed automatically.

The Render Blueprint explicitly contains:

```yaml
plan: free
```

Therefore, the Blueprint does not intentionally create a paid Render instance.

It also does not create:

- A paid persistent disk
- A background worker service
- A cron job
- A managed database
- A Redis instance
- Any other paid Render service

### Deployment steps

1. Push this repository to GitHub.
2. Log in to Render.
3. Select **New**.
4. Select **Blueprint**.
5. Connect the GitHub repository.
6. Render should automatically detect `render.yaml`.
7. Confirm that the service shows the **Free** instance type.
8. Add the requested `GROQ_API_KEY` environment variable.
9. Deploy the Blueprint.

The health-check endpoint is:

```text
/health
```

Do not continue if Render shows:

- Starter
- Standard
- Pro
- A monthly price
- A paid persistent disk
- A paid database
- Any other paid resource

Render account and verification policies can change independently of this repository. Always confirm that the Render deployment screen says **Free** before creating the service.

## Render Free limitations

The application is configured to run one transcription job at a time on Render Free:

```yaml
- key: MAX_CONCURRENT_JOBS
  value: "1"
```

This reduces the chance of exceeding the Free instance's limited memory and CPU.

When using Render Free:

- The service may start slowly after a period of inactivity.
- Long videos may take considerably longer to process.
- Only one transcription should be processed at a time.
- Temporary files disappear after a restart or new deployment.
- Existing jobs may be lost when the service restarts.
- The service should run with only one Uvicorn process.
- Very long videos may exceed the practical resources of a Free instance.

The application stores temporary files under:

```text
/tmp/sonicscript-jobs
```

No persistent disk is required.

## Required API key

The application requires a Groq API key for transcription and AI refinement.

Create the following environment variable in Render:

```env
GROQ_API_KEY=gsk_your_key_here
```

Do not add the actual API key to GitHub, `render.yaml`, or the source code.

Add it through the Render dashboard under:

```text
Environment → Environment Variables
```

## YouTube verification on cloud hosts

YouTube may sometimes ask datacenter IP addresses to verify that a request is not automated.

If this happens for videos you are authorized to process, you may configure a Netscape-format cookie file and set:

```env
YTDLP_COOKIES_FILE=/path/to/secret/youtube-cookies.txt
```

Never commit YouTube cookies to GitHub.

Cookies can expire and must be maintained by the person operating the deployment.

Only download or transcribe videos you are authorized to process, and follow YouTube's terms and applicable laws.

## Handling "Requested format is not available"

Some YouTube videos only expose their playable audio through certain player
clients. If a single client is pinned (for example only `android`/`web`),
yt-dlp can fail with:

```text
[youtube] <id>: Requested format is not available. Use --list-formats for a list of available formats
```

The app now automatically retries with a chain of fallback strategies instead of
giving up:

- Multiple YouTube player clients are tried in order (`android, web`, `web, tv`,
  `tv`, `android_vr, web_embedded`, `ios, mweb`, and finally yt-dlp's built-in
  default set). The `tv` client in particular exposes formats other clients omit
  and is also less likely to trigger YouTube's bot-check.
- For each client, the app tries efficient audio-only selection
  (`bestaudio/best`) and then the default format.
- Video inspection (`/api/videos/inspect`) performs the same client rotation and
  treats an empty format list as a signal to try the next client, instead of
  failing immediately.

If all strategies fail, the app reports a clearer message that the video is
likely private, region/age-restricted, DRM-protected, members-only, or removed.

## Local development

### Requirements

- Python 3.12 or later
- FFmpeg
- FFprobe
- Node.js
- Groq API key

### Install

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

On Windows, activate the virtual environment with:

```powershell
.venv\Scripts\activate
```

Copy the example environment configuration:

```bash
cp .env.example .env
```

Set your Groq API key:

```env
GROQ_API_KEY=gsk_your_key_here
```

Start the development server:

```bash
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

Open:

```text
http://localhost:8000
```

Interactive API documentation is available at:

```text
http://localhost:8000/api/docs
```

## Run with Docker

Build the Docker image:

```bash
docker build -t sonicscript-ai .
```

Run it:

```bash
docker run --rm \
  -p 10000:10000 \
  -e GROQ_API_KEY="gsk_your_key_here" \
  sonicscript-ai
```

Open:

```text
http://localhost:10000
```

## Configuration

| Variable | Default | Purpose |
|---|---:|---|
| `GROQ_API_KEY` | — | Default API key for transcription and refinement |
| `TRANSCRIPTION_API_KEY` | `GROQ_API_KEY` | Optional dedicated speech-recognition API key |
| `TRANSCRIPTION_BASE_URL` | `https://api.groq.com/openai/v1` | OpenAI-compatible transcription API root |
| `TRANSCRIPTION_MODEL` | `whisper-large-v3` | Speech-to-text model |
| `REFINEMENT_API_KEY` | `GROQ_API_KEY` | Optional dedicated AI-refinement API key |
| `REFINEMENT_BASE_URL` | Transcription URL | OpenAI-compatible chat API root |
| `REFINEMENT_MODEL` | `llama-3.3-70b-versatile` | Transcript-refinement model |
| `MAX_VIDEO_DURATION_SECONDS` | `21600` | Maximum accepted video runtime in seconds |
| `TRANSCRIPTION_CHUNK_SECONDS` | `600` | Audio chunk size sent to the speech model |
| `MAX_CONCURRENT_JOBS` | `2` in app, `1` on Render Free | Simultaneously processed transcription jobs |
| `JOB_TTL_SECONDS` | `14400` | Temporary job and audio retention |
| `ENABLE_DENOISE` | `true` | Enables FFmpeg speech denoising |
| `YTDLP_COOKIES_FILE` | — | Optional YouTube cookie-file location |

If you use an API provider other than Groq, configure the base URLs, API keys, and model names explicitly.

## API overview

- `POST /api/videos/inspect` — Validate a YouTube URL and retrieve video metadata
- `POST /api/jobs` — Start a timeline transcription job
- `GET /api/jobs/{id}` — Retrieve the transcription and refinement status
- `DELETE /api/jobs/{id}` — Cancel and remove a job
- `GET /api/jobs/{id}/audio` — Stream the selected audio overview
- `POST /api/jobs/{id}/refine` — Start non-destructive AI refinement
- `GET /api/jobs/{id}/download?format=txt` — Export plain text
- `GET /api/jobs/{id}/download?format=srt` — Export SRT captions
- `GET /api/jobs/{id}/download?format=vtt` — Export WebVTT captions
- `GET /api/jobs/{id}/download?format=json` — Export structured JSON
- `GET /health` — Render deployment health check

## Tests

Run the unit tests:

```bash
python -m unittest discover -s tests -v
```

Check Python syntax:

```bash
python -m compileall -q app
```

Check JavaScript syntax:

```bash
node --check app/static/app.js
```

## Privacy and operational notes

- URLs, selected audio, and transcripts are processed by the server and the configured AI provider.
- Job information is stored temporarily in memory.
- Temporary files are stored under `/tmp/sonicscript-jobs`.
- Temporary data may disappear after a restart or deployment.
- Do not run multiple Uvicorn workers unless job state and files are moved to shared storage.
- API usage limits and costs are determined by the configured AI provider.
- The Render web service can remain free, but the AI provider may have separate limits or costs.
