import asyncio
import json
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from itertools import islice

import aiohttp
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import Command
from aiogram.types import BufferedInputFile, InputMediaPhoto, MediaUnion, Message
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from bs4 import BeautifulSoup

# Import config and generation function
import config
from ai_config import generate_news_digest

# ---------------------------------------------------------------------------
#  Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("news-parcer")

# ---------------------------------------------------------------------------
#  Constants
# ---------------------------------------------------------------------------
MAX_SEEN_POSTS = 200
MSG_CHAR_LIMIT = 4000
HTTP_TIMEOUT = aiohttp.ClientTimeout(total=15)
MAX_RETRIES = 3
RETRY_BASE_DELAY = 1.0  # seconds, doubled each retry
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)


# ---------------------------------------------------------------------------
#  Seen-posts persistence
# ---------------------------------------------------------------------------
def load_seen_posts() -> set[str]:
    """Завантажує ідентифікатори раніше оброблених постів."""
    if not config.SEEN_POSTS_FILE.exists():
        return set()
    try:
        data = json.loads(config.SEEN_POSTS_FILE.read_text(encoding="utf-8"))
        return set(data.get("seen", []))
    except (json.JSONDecodeError, KeyError, OSError) as exc:
        log.warning(
            "Не вдалося завантажити кеш постів (%s): %s", config.SEEN_POSTS_FILE, exc
        )
        return set()


def save_seen_posts(seen: set[str]):
    """Зберігає у файл, залишаючи не більше MAX_SEEN_POSTS найновіших."""
    try:
        config.SEEN_POSTS_FILE.parent.mkdir(parents=True, exist_ok=True)
        as_list = list(seen)[-MAX_SEEN_POSTS:]
        config.SEEN_POSTS_FILE.write_text(
            json.dumps({"seen": as_list}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except OSError as exc:
        log.error(
            "Помилка збереження кешу постів у %s: %s", config.SEEN_POSTS_FILE, exc
        )


# ---------------------------------------------------------------------------
#  Telegram bot & Scheduler init
# ---------------------------------------------------------------------------
log.debug("Ініціалізація Telegram бота...")
bot = Bot(
    token=config.BOT_TOKEN or "dummy_token_to_allow_import",
    default=DefaultBotProperties(link_preview_is_disabled=True),
)
dp = Dispatcher()
scheduler = AsyncIOScheduler(timezone=config.TIMEZONE)
gazette_lock = asyncio.Lock()


# ---------------------------------------------------------------------------
#  HTTP helpers
# ---------------------------------------------------------------------------
async def _fetch(
    session: aiohttp.ClientSession,
    url: str,
    *,
    retries: int = MAX_RETRIES,
) -> bytes | None:
    """GET *url* with exponential-backoff retries. Returns body bytes or None."""
    delay = RETRY_BASE_DELAY
    for attempt in range(1, retries + 1):
        try:
            async with session.get(url) as resp:
                if resp.status == 200:
                    return await resp.read()
                log.warning(
                    "HTTP %s для %s (спроба %d/%d)", resp.status, url, attempt, retries
                )
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            log.warning(
                "Помилка запиту %s (спроба %d/%d): %s", url, attempt, retries, exc
            )
        if attempt < retries:
            await asyncio.sleep(delay)
            delay *= 2
    return None


# ---------------------------------------------------------------------------
#  Smart message splitting
# ---------------------------------------------------------------------------
async def send_long_message_to_user(
    bot_instance: Bot, chat_id: int, text: str
) -> list[int]:
    """
    Розбиває довгий текст на частини ≤ MSG_CHAR_LIMIT, намагаючись
    різати по останньому переносу рядка або пробілу.
    """
    message_ids: list[int] = []
    while text:
        if len(text) <= MSG_CHAR_LIMIT:
            chunk = text
            text = ""
        else:
            cut = text.rfind("\n", 0, MSG_CHAR_LIMIT)
            if cut == -1:
                cut = text.rfind(" ", 0, MSG_CHAR_LIMIT)
            if cut == -1:
                cut = MSG_CHAR_LIMIT
            chunk = text[:cut]
            text = text[cut:].lstrip("\n")
        sent = await bot_instance.send_message(chat_id, chunk)
        message_ids.append(sent.message_id)
        if text:
            await asyncio.sleep(0.5)
    return message_ids


# ---------------------------------------------------------------------------
#  Channel scraping (async, concurrent)
# ---------------------------------------------------------------------------
async def _scrape_channel(
    session: aiohttp.ClientSession,
    channel: str,
    seen: set[str],
    time_threshold: datetime,
) -> tuple[str, set[str], dict]:
    """Scrape a single channel. Returns (text_data, new_ids, images_by_post)."""
    log.debug("Парсимо канал: t.me/%s", channel)
    text_data = f"\n*** Джерело: t.me/{channel} ***\n"
    new_ids: set[str] = set()
    images_by_post: dict[str, list[dict[str, str]]] = {}

    body = await _fetch(session, f"https://t.me/s/{channel}")
    if body is None:
        text_data += "(не вдалося отримати доступ до сторінки каналу)\n"
        return text_data, new_ids, images_by_post

    soup = BeautifulSoup(body, "lxml")
    messages = soup.find_all("div", class_="tgme_widget_message")
    log.debug("Знайдено всього повідомлень на сторінці %s: %d", channel, len(messages))

    posts_added = 0
    posts_skipped = 0
    for msg in messages:
        post_id_attr = msg.get("data-post")
        if not post_id_attr or not isinstance(post_id_attr, str):
            continue

        if post_id_attr in seen:
            posts_skipped += 1
            continue

        time_tag = msg.find("time", class_="time")
        if not time_tag:
            continue

        post_time_attr = time_tag.get("datetime")
        if not post_time_attr or not isinstance(post_time_attr, str):
            continue

        post_time_str: str = post_time_attr
        if post_time_str.endswith("Z"):
            post_time_str = post_time_str[:-1] + "+00:00"

        try:
            post_time = datetime.fromisoformat(post_time_str)
        except ValueError:
            continue

        if post_time < time_threshold:
            continue

        post_link = f"https://t.me/{post_id_attr}"
        text_block = msg.find("div", class_="tgme_widget_message_text")
        if text_block:
            clean_text = text_block.get_text(separator="\n").strip()
            if clean_text:
                text_data += (
                    f"Пост: {clean_text}\nПосилання на оригінал: {post_link}\n\n"
                )
                new_ids.add(post_id_attr)
                posts_added += 1
                photo_urls: list[dict[str, str]] = []
                for photo_a in msg.select("a.tgme_widget_message_photo_wrap"):
                    style_attr = str(photo_a.get("style") or "")
                    href_attr = str(photo_a.get("href") or "")
                    match = re.search(r"background-image:url\('(.+?)'\)", style_attr)
                    if match and href_attr:
                        photo_urls.append(
                            {
                                "url": match.group(1),
                                "link": href_attr.split("?")[0],
                            }
                        )
                if photo_urls:
                    images_by_post[post_id_attr] = photo_urls

    log.debug(
        "З каналу %s: додано %d, пропущено (раніше бачені) %d",
        channel,
        posts_added,
        posts_skipped,
    )
    return text_data, new_ids, images_by_post


async def get_telegram_posts_via_web(
    session: aiohttp.ClientSession,
    seen: set[str],
) -> tuple[str, set[str], dict]:
    """Scrape all configured channels concurrently."""
    t0 = time.perf_counter()
    log.info(
        "Початок збору даних з Telegram-каналів (%d каналів)...",
        len(config.TG_CHANNELS),
    )
    time_threshold = datetime.now(timezone.utc) - timedelta(days=1)

    tasks = [
        _scrape_channel(session, ch, seen, time_threshold) for ch in config.TG_CHANNELS
    ]
    results = await asyncio.gather(*tasks)

    text_data = ""
    new_ids: set[str] = set()
    images_by_post: dict[str, list[dict[str, str]]] = {}
    for ch_text, ch_ids, ch_images in results:
        text_data += ch_text
        new_ids |= ch_ids
        images_by_post.update(ch_images)

    log.info(
        "Збір даних завершено за %.2f сек. Знайдено нових постів: %d",
        time.perf_counter() - t0,
        len(new_ids),
    )
    return text_data, new_ids, images_by_post


# ---------------------------------------------------------------------------
#  Image sending (async downloads)
# ---------------------------------------------------------------------------
def extract_post_ids_from_digest(text: str) -> set[str]:
    return set(re.findall(r"https://t\.me/([a-zA-Z0-9_]+/\d+)", text))


async def _download_images(
    session: aiohttp.ClientSession,
    images_by_post: dict,
    limit: int,
) -> list[BufferedInputFile]:
    """Download up to *limit* images concurrently."""
    flat_photos = list(
        islice(
            (photo for photos in images_by_post.values() for photo in photos),
            limit,
        )
    )
    if not flat_photos:
        return []

    async def _dl(photo: dict) -> BufferedInputFile | None:
        data = await _fetch(session, photo["url"], retries=2)
        if data is None:
            return None
        filename = photo["url"].split("/")[-1]
        return BufferedInputFile(data, filename=filename)

    results = await asyncio.gather(*[_dl(p) for p in flat_photos])
    return [r for r in results if r is not None]


async def send_post_images(
    bot_instance: Bot,
    chat_id: int,
    response_text: str,
    images_by_post: dict,
    session: aiohttp.ClientSession,
) -> str:
    """
    Надсилає альбом зображень (sendMediaGroup), у якого текст дайджесту
    є підписом (caption) першого фото.
    Повертає решту тексту, якщо він не вмістився у ліміт caption (1024 симв.).
    """
    loaded = await _download_images(
        session,
        images_by_post,
        getattr(config, "MAX_TOTAL_IMAGES", 5),
    )
    if not loaded:
        return response_text

    caption_limit = 1024
    caption = response_text[:caption_limit]
    leftover = response_text[caption_limit:]

    log.debug(
        "Надсилаємо галерею зображень (%d шт.) з текстом у підписі...", len(loaded)
    )
    for i in range(0, len(loaded), 10):
        chunk = loaded[i : i + 10]
        cap = caption if i == 0 else None
        try:
            if len(chunk) == 1:
                await bot_instance.send_photo(chat_id, chunk[0], caption=cap)
            else:
                media: list[MediaUnion] = [
                    InputMediaPhoto(media=img, caption=(cap if idx == 0 else None))
                    for idx, img in enumerate(chunk)
                ]
                await bot_instance.send_media_group(chat_id, media)
        except Exception as exc:
            log.warning("Помилка надсилання галереї зображень: %s", exc)
        await asyncio.sleep(0.5)

    return leftover


# ---------------------------------------------------------------------------
#  Core gazette logic
# ---------------------------------------------------------------------------
async def make_and_send_gazette(bot_instance: Bot, chat_id: int):
    """Генерує дайджест та надсилає його вказаному користувачу."""
    if gazette_lock.locked():
        log.warning(
            "Генерація газети вже виконується. Запит для %s відхилено.", chat_id
        )
        try:
            await bot_instance.send_message(
                chat_id, "⏳ Газета вже готується. Будь ласка, зачекайте."
            )
        except Exception:
            pass
        return

    async with gazette_lock:
        t0 = time.perf_counter()
        waiting_msg = await bot_instance.send_message(
            chat_id, "🗞 Друкую газету... Зачекайте хвилинку."
        )

        try:
            seen = load_seen_posts()

            async with aiohttp.ClientSession(
                timeout=HTTP_TIMEOUT,
                headers={"User-Agent": USER_AGENT},
            ) as session:
                (
                    tg_content,
                    new_post_ids,
                    images_by_post,
                ) = await get_telegram_posts_via_web(session, seen)

                if not new_post_ids:
                    await waiting_msg.edit_text(
                        "Свіжих новин за останні 24 години не знайдено."
                    )
                    return

                log.info(
                    "Сирий текст зібрано. Запит до LLM (Gemini / OpenRouter fallback)..."
                )
                # Виконуємо синхронний запит до LLM у окремому потоці, щоб не блокувати asyncio loop
                response_text = await asyncio.to_thread(
                    generate_news_digest, tg_content
                )

                log.info("Надсилаємо дайджест користувачу %s...", chat_id)

                if getattr(config, "SEND_IMAGES", True):
                    digest_post_ids = extract_post_ids_from_digest(response_text)
                    filtered_images = {
                        pid: photos
                        for pid, photos in images_by_post.items()
                        if pid in digest_post_ids
                    }
                    leftover_text = await send_post_images(
                        bot_instance,
                        chat_id,
                        response_text,
                        filtered_images,
                        session,
                    )
                    if leftover_text:
                        await send_long_message_to_user(
                            bot_instance, chat_id, leftover_text
                        )
                else:
                    await send_long_message_to_user(
                        bot_instance, chat_id, response_text
                    )

            await waiting_msg.delete()

            # Зберігаємо нові ID разом з попередніми
            seen |= new_post_ids
            save_seen_posts(seen)
            log.info("Збережено %d нових ID постів у кеш", len(new_post_ids))
            log.info(
                "Газету успішно надіслано! Загальний час: %.2f сек.",
                time.perf_counter() - t0,
            )

        except Exception as exc:
            log.error("Помилка під час генерації газети: %s", exc, exc_info=True)
            try:
                await waiting_msg.edit_text(f"Сталася помилка при генерації: {exc}")
            except Exception as edit_err:
                log.error("Не вдалося відредагувати статус-повідомлення: %s", edit_err)


# ---------------------------------------------------------------------------
#  Scheduled & Auto-run jobs
# ---------------------------------------------------------------------------
async def scheduled_gazette_job():
    """Задача автоматичної генерації газети за розкладом."""
    log.info("--- [РОЗКЛАД] Запуск випуску газети за розкладом ---")
    if not config.MY_TELEGRAM_ID or config.MY_TELEGRAM_ID == 123456789:
        log.warning("[РОЗКЛАД] Скасовано: не налаштовано MY_TELEGRAM_ID у файлі .env!")
        return

    await make_and_send_gazette(bot, config.MY_TELEGRAM_ID)


def setup_scheduler():
    """Налаштовує cron тригери для кожного часу в config.SCHEDULE_TIMES."""
    for time_str in config.SCHEDULE_TIMES:
        try:
            hour, minute = map(int, time_str.split(":"))
            trigger = CronTrigger(hour=hour, minute=minute, timezone=config.TIMEZONE)
            job = scheduler.add_job(
                scheduled_gazette_job,
                trigger=trigger,
                id=f"gazette_{hour:02d}_{minute:02d}",
                name=f"Gazette {time_str} ({config.TIMEZONE})",
                replace_existing=True,
            )
            log.info("Додано розклад випуску: о %s (%s)", time_str, config.TIMEZONE)
        except Exception as exc:
            log.error("Не вдалося додати час розкладу '%s': %s", time_str, exc)


async def auto_run_gazette_if_enabled():
    """Запускає дайджест відразу після старту, якщо увімкнено RUN_ON_STARTUP."""
    if not config.RUN_ON_STARTUP:
        log.info(
            "RUN_ON_STARTUP=false — початковий випуск пропущено. Бот готовий до роботи за розкладом."
        )
        return

    await asyncio.sleep(2)
    log.info("--- [АВТОЗАПУСК] Починаємо підготовку газети при старті бота ---")

    if not config.MY_TELEGRAM_ID or config.MY_TELEGRAM_ID == 123456789:
        log.warning(
            "[АВТОЗАПУСК] Скасовано: не вказано дійсний MY_TELEGRAM_ID у файлі .env!"
        )
        return

    await make_and_send_gazette(bot, config.MY_TELEGRAM_ID)


# ---------------------------------------------------------------------------
#  Aiogram handlers
# ---------------------------------------------------------------------------
@dp.message(Command("start"))
async def cmd_start(message: Message):
    times_str = (
        ", ".join(config.SCHEDULE_TIMES) if config.SCHEDULE_TIMES else "не налаштовано"
    )
    await message.answer(
        "👋 Привіт! Я твій персональний редактор газетного дайджесту.\n\n"
        "📌 <b>Команди:</b>\n"
        "• /gazette — підготувати та надіслати свіжий випуск новин прямо зараз\n"
        "• /schedule — переглянути активний розклад випусків\n"
        "• /status — перевірити статус підключення LLM та джерел\n\n"
        f"⏰ <b>Автоматична розсилка:</b> о <code>{times_str}</code> (часовий пояс: <code>{config.TIMEZONE}</code>)",
        parse_mode="HTML",
    )


@dp.message(Command("gazette"))
async def cmd_gazette(message: Message):
    if not message.from_user:
        log.warning("Отримано команду /gazette без даних користувача.")
        return

    user_id = message.from_user.id
    log.info("Користувач %d запросив випуск газети вручну (/gazette)", user_id)
    await make_and_send_gazette(bot, user_id)


@dp.message(Command("schedule"))
async def cmd_schedule(message: Message):
    lines = [
        "📅 <b>Розклад випуску новин:</b>",
        f"• Часовий пояс: <code>{config.TIMEZONE}</code>",
        f"• Час розсилки: <code>{', '.join(config.SCHEDULE_TIMES)}</code>",
        f"• Цільовий Telegram ID: <code>{config.MY_TELEGRAM_ID or 'не вказано'}</code>",
        f"• Випуск при старті: <code>{'Увімкнено' if config.RUN_ON_STARTUP else 'Вимкнено'}</code>",
        "",
        "⏳ <b>Найближчі заплановані випуски:</b>",
    ]
    jobs = scheduler.get_jobs()
    if jobs:
        for job in jobs:
            next_time = job.next_run_time
            if next_time:
                lines.append(
                    f"• {job.name}: <code>{next_time.strftime('%Y-%m-%d %H:%M:%S')}</code>"
                )
            else:
                lines.append(f"• {job.name}")
    else:
        lines.append("<i>Немає активних задач у розкладі.</i>")

    await message.answer("\n".join(lines), parse_mode="HTML")


@dp.message(Command("status"))
async def cmd_status(message: Message):
    gemini_status = "✅ Налаштовано" if config.GEMINI_API_KEY else "❌ Не вказано"
    openrouter_status = (
        "✅ Налаштовано" if config.OPENROUTER_API_KEY else "❌ Не вказано"
    )
    seen_count = len(load_seen_posts())
    channels = "\n".join(f"• @{ch}" for ch in config.TG_CHANNELS)

    text = (
        "⚙️ <b>Стан системи та конфігурації:</b>\n\n"
        "<b>LLM провайдери:</b>\n"
        f"• Основний (Gemini - <code>{config.GEMINI_MODEL}</code>): {gemini_status}\n"
        f"• Резервний (OpenRouter - <code>{config.OPENROUTER_MODEL}</code>): {openrouter_status}\n\n"
        f"<b>Канали для моніторингу ({len(config.TG_CHANNELS)}):</b>\n{channels}\n\n"
        f"<b>Зображення:</b> {'Увімкнено (до ' + str(config.MAX_TOTAL_IMAGES) + ')' if config.SEND_IMAGES else 'Вимкнено'}\n"
        f"<b>Кеш прочитаних постів:</b> {seen_count} постів\n"
        f"<b>Файл кешу:</b> <code>{config.SEEN_POSTS_FILE}</code>"
    )
    await message.answer(text, parse_mode="HTML")


# ---------------------------------------------------------------------------
#  Entry point
# ---------------------------------------------------------------------------
async def main():
    if not config.BOT_TOKEN or config.BOT_TOKEN.startswith("dummy"):
        log.critical(
            "Помилка: BOT_TOKEN не вказано! Будь ласка, вкажіть його у файлі .env (скопіюйте з .env.example)."
        )
        return

    # Ініціалізація та старт планувальника
    setup_scheduler()
    scheduler.start()
    log.info(
        "Планувальник успішно запущено. Активних задач: %d", len(scheduler.get_jobs())
    )

    # Запуск авто-випуску при старті (якщо увімкнено)
    asyncio.create_task(auto_run_gazette_if_enabled())

    log.info("Запуск Telegram bot polling...")
    try:
        await dp.start_polling(bot)
    finally:
        scheduler.shutdown(wait=False)
        await bot.session.close()
        log.info("Роботу бота завершено.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Роботу бота зупинено користувачем.")
