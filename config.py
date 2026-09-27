import json
import logging
import os
from pathlib import Path

from dotenv import load_dotenv

# Base project directory
BASE_DIR = Path(__file__).resolve().parent

# Load environment variables from .env file (if it exists)
load_dotenv(BASE_DIR / ".env")

log = logging.getLogger(__name__)


def _parse_channels(raw: str) -> list[str]:
    """Parse channel list from JSON string or comma-separated string."""
    raw = (raw or "").strip()
    if not raw:
        return ["naebnet", "kiev_levyy_bereg"]
    if raw.startswith("[") and raw.endswith("]"):
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                return [str(ch).strip().lstrip("@") for ch in parsed if str(ch).strip()]
        except Exception:
            pass
    return [ch.strip().lstrip("@") for ch in raw.split(",") if ch.strip()]


def _parse_schedule_times(raw: str) -> list[str]:
    """Parse comma-separated HH:MM times, e.g. '09:00, 20:00'."""
    raw = (raw or "").strip()
    if not raw:
        return ["09:00", "20:00"]
    times: list[str] = []
    for item in raw.split(","):
        cleaned = item.strip()
        if not cleaned:
            continue
        parts = cleaned.split(":")
        if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
            h, m = int(parts[0]), int(parts[1])
            if 0 <= h <= 23 and 0 <= m <= 59:
                times.append(f"{h:02d}:{m:02d}")
        else:
            log.warning("Некоректний формат часу в SCHEDULE_TIMES: '%s'", cleaned)
    return times or ["09:00", "20:00"]


# Telegram Bot
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
_raw_tg_id = os.getenv("MY_TELEGRAM_ID", "").strip()
try:
    MY_TELEGRAM_ID = int(_raw_tg_id) if _raw_tg_id else 0
except ValueError:
    MY_TELEGRAM_ID = 0

# Channels to scrape
TG_CHANNELS = _parse_channels(os.getenv("TG_CHANNELS", ""))

# Images configuration
SEND_IMAGES = os.getenv("SEND_IMAGES", "true").strip().lower() in ("true", "1", "yes")
try:
    MAX_TOTAL_IMAGES = int(os.getenv("MAX_TOTAL_IMAGES", "5"))
except ValueError:
    MAX_TOTAL_IMAGES = 5

# Primary LLM: Google Gemini
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.5-flash").strip()

# Fallback LLM: OpenRouter
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "").strip()
OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL", "qwen/qwen3.8-omni-flash").strip()
OPENROUTER_HTTP_REFERER = os.getenv(
    "OPENROUTER_HTTP_REFERER", "https://github.com/ervins324/news-parcer"
).strip()
OPENROUTER_TITLE = os.getenv("OPENROUTER_TITLE", "News Parcer").strip()

# Scheduler
TIMEZONE = os.getenv("TIMEZONE", "Europe/Kyiv").strip()
SCHEDULE_TIMES = _parse_schedule_times(os.getenv("SCHEDULE_TIMES", "09:00, 20:00"))
RUN_ON_STARTUP = os.getenv("RUN_ON_STARTUP", "false").strip().lower() in (
    "true",
    "1",
    "yes",
)

# Storage
_seen_path = os.getenv("SEEN_POSTS_FILE", "seen_posts.json").strip()
SEEN_POSTS_FILE = (
    Path(_seen_path) if Path(_seen_path).is_absolute() else (BASE_DIR / _seen_path)
)
