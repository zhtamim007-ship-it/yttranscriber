FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=10000

# A JavaScript runtime for yt-dlp's YouTube challenge solver comes from the
# pip "deno" extra in requirements.txt (yt-dlp[default,deno]). Do NOT install
# Debian's nodejs package for this: yt-dlp requires Node.js >= 22 to solve
# YouTube signature challenges, while Debian ships Node.js 18/20, which
# yt-dlp then marks as unsupported and ignores.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

RUN mkdir -p /tmp/sonicscript-jobs
EXPOSE 10000
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-10000}"]
