FROM python:3.13-slim

# Prevent Python from writing .pyc files, keep stdout/stderr unbuffered, and optimize memory
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONOPTIMIZE=1 \
    MALLOC_ARENA_MAX=2 \
    TZ=Europe/Kyiv

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    tzdata \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy dependency specifications first to leverage Docker layer caching
COPY requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy application source code
COPY . .

# Ensure directory for persistent data exists
RUN mkdir -p /app/data

# Run the bot
CMD ["python", "main.py"]
