# news-parcer — Telegram news digest bot

## Entrypoint
- `main.py` — async aiogram 3.x bot, polling-based.

## Commands
- `python main.py` — starts bot (auto-runs `/gazette` once on startup if `config.py:MY_TELEGRAM_ID` is set)
- No test/lint/formatter config exists. No CI.

## Key structure
| File | Role |
|---|---|
| `main.py` | Bot logic: web scraping (`t.me/s/{channel}`), message dispatch, post dedup |
| `config.py` | Secrets (`BOT_TOKEN`, `GEMINI_API_KEY`), channel list, target user ID |
| `ai_config.py` | Google Gemini client (`google-genai` v2.x, not `google-generativeai`) |
| `requirements.txt` | `aiogram==3.29.1`, `beautifulsoup4==4.15.0`, `google-genai==2.11.0`, `google-api-python-client`, `requests==2.34.2` |
| `seen_posts.json` | Auto-created; stores up to 200 post IDs to avoid resending the same news |

## Before committing
- **Never commit `config.py`** — it contains live API tokens (`BOT_TOKEN`, `GEMINI_API_KEY`).
- **Never commit `seen_posts.json`** — local cache of already-seen post IDs.
- The project uses `.gitignore` to exclude `.venv/`, `__pycache__/`, `config.py`, `seen_posts.json`.

## Known quirks
- Scrapes `t.me/s/{channel}` with a desktop Chrome User-Agent.
- Messages >4000 chars are split into 4000-char chunks with 0.5s delay.
- Bot auto-runs `make_and_send_gazette()` on startup, then stops polling (`dp.stop_polling()`). After auto-run the bot exits.
- Link previews disabled globally via `DefaultBotProperties(link_preview_is_disabled=True)`.
- Post dedup uses `seen_posts.json` — post IDs are saved only after the digest is sent successfully. Oldest entries are evicted at 200.
