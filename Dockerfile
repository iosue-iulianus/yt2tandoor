FROM python:3.10-slim

# System deps: ffmpeg (for whisper/yt-dlp), git (for whisper install), nodejs+npm (for claude CLI)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    git \
    curl \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

# Install Claude Code CLI
RUN npm install -g @anthropic-ai/claude-code

# Create non-root user
RUN useradd -m -s /bin/bash appuser

WORKDIR /app

# Install Python deps
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy source (owned by appuser so non-root can read)
COPY --chown=appuser:appuser pipeline.py bot.py recipe_from_video.py ./

# Ensure data dir exists
RUN mkdir -p /app/data && chown appuser:appuser /app/data

USER appuser

# Create config dirs (will be overridden by volume mounts)
RUN mkdir -p /home/appuser/.config/yt2tandoor /home/appuser/.claude

CMD ["python", "bot.py"]
