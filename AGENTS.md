# news-parcer — Telegram news digest bot

## Entrypoint
- `main.py` — async aiogram 3.x bot, polling-based with background APScheduler and continuous execution.

## Commands
- `python main.py` — starts bot and scheduler (runs on schedule, listens to `/gazette`, `/schedule`, `/status`, `/start`)
- `python -m unittest discover tests -v` — run test suite
- `python ai_config.py --test-openrouter` — test direct request to OpenRouter
- `python ai_config.py --test-fallback` — test automatic fallback from Gemini to OpenRouter
- `python ai_config.py --list-models` — lists models from OpenRouter
- `docker compose up -d --build` — run as a daemon service on a server

## CI / CD
- `.github/workflows/ci.yml` — automated GitHub Actions CI pipeline running unit tests on Python 3.12 & 3.13 and validating Docker build.

## Key structure
| File | Role |
|---|---|
| `main.py` | Bot logic: scraping (`t.me/s/{channel}`), message dispatch, APScheduler cron scheduling, deduplication |
| `config.py` | Configuration loader via `python-dotenv` with fallbacks and environment parsing |
| `.env` | Local secrets and environment variables (`BOT_TOKEN`, `GEMINI_API_KEY`, `OPENROUTER_API_KEY`, etc.) |
| `.env.example` | Template file for configuring environment variables |
| `ai_config.py` | LLM client with primary Google Gemini and automated OpenRouter fallback (`openrouter==1.2.32`) |
| `requirements.txt` | Core bot dependencies + `openrouter==1.2.32`, `python-dotenv`, `apscheduler` |
| `Dockerfile` | Container configuration for server deployment |
| `docker-compose.yml` | Multi-container/service spec with volume persistence for `seen_posts.json` |
| `seen_posts.json` | Auto-created cache storing up to 200 post IDs |

## Before committing
- **Never commit `.env`** — contains live API tokens (`BOT_TOKEN`, `GEMINI_API_KEY`, `OPENROUTER_API_KEY`).
- **Never commit `seen_posts.json`** — local cache of already-seen post IDs.
- The project uses `.gitignore` to exclude `.venv/`, `__pycache__/`, `config.py`, `seen_posts.json`, `.env`, `.env.*` (except `.env.example`).

## Known features & quirks
- Scrapes `t.me/s/{channel}` concurrently using a desktop Chrome User-Agent.
- HTTP requests use exponential-backoff retries.
- Scheduled delivery uses `APScheduler` based on `SCHEDULE_TIMES` and `TIMEZONE` in `.env`.
- LLM generation first tries Google Gemini; if any error occurs (quota, downtime, API error), it seamlessly falls back to OpenRouter.
- Post dedup uses `seen_posts.json` (or path set by `SEEN_POSTS_FILE`).
