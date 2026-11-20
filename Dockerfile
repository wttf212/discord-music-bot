FROM python:3.12-slim

# Install system dependencies
# - ffmpeg: audio transcoding
# - curl + unzip: download/extract Deno
# - libsodium: required by PyNaCl for discord.py voice encryption
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    unzip \
    ffmpeg \
    libsodium-dev \
    && rm -rf /var/lib/apt/lists/*

# Install Deno (JavaScript runtime required for YouTube signature solving)
ENV DENO_INSTALL="/root/.deno"
ENV PATH="${DENO_INSTALL}/bin:${PATH}"
RUN curl -fsSL https://deno.land/install.sh | sh

# Run every Deno that yt-dlp spawns at nice 10. Each resolve spends ~2s of CPU in Deno
# solving YouTube's JS challenge; at normal priority, concurrent solves on a 2-vCPU host
# starve discord.py's audio sender thread (measured, 2 pinned cores, 50 streams: p99
# packet gap 21 ms -> 125 ms with 4 concurrent resolves, 48 ms with Deno and bgutil
# below normal priority). yt-dlp finds deno through PATH, so a wrapper in front of the
# real binary is enough. main.py starts the bgutil server niced the same way.
RUN mkdir -p /opt/nice-bin \
    && printf '#!/bin/sh\nexec nice -n 10 %s/bin/deno "$@"\n' "$DENO_INSTALL" > /opt/nice-bin/deno \
    && chmod +x /opt/nice-bin/deno
ENV PATH="/opt/nice-bin:${PATH}"

WORKDIR /app

# Copy source code
COPY . .

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Download bgutil-pot binary (YouTube PO token generator, Linux x86_64)
RUN curl -fsSL \
    "https://github.com/jim60105/bgutil-ytdlp-pot-provider-rs/releases/latest/download/bgutil-pot-linux-x86_64" \
    -o bgutil-pot && chmod +x bgutil-pot

# Persistent data directory for guild settings
RUN mkdir -p /data
ENV GUILD_SETTINGS_FILE=/data/guild_settings.json
ENV PYTHONUNBUFFERED=1

CMD ["python", "main.py"]
