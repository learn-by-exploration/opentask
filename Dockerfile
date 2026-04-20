FROM python:3.10-slim

# Avoid interactive prompts during build
ENV DEBIAN_FRONTEND=noninteractive

# Install system deps + Node.js 20.x (for claude CLI)
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl ca-certificates git procps \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

# Install claude CLI globally (run postinstall to fetch native binary)
RUN npm install -g @anthropic-ai/claude-code \
    && node $(npm root -g)/@anthropic-ai/claude-code/install.cjs

WORKDIR /app

# Install Python dependencies first (layer caching)
COPY pyproject.toml .
RUN pip install --no-cache-dir .

# Copy application code
COPY app/ app/

# Data directory for SQLite
RUN mkdir -p /app/data

# Dashboard port
EXPOSE 8095

# Don't buffer Python output
ENV PYTHONUNBUFFERED=1

ENTRYPOINT ["python", "-m", "app"]
