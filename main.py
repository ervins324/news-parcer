import asyncio
import json
import logging
import re
import time
from datetime import datetime, timezone, timedelta
from itertools import islice
from pathlib import Path

import aiohttp
from bs4 import BeautifulSoup

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import Command
from aiogram.types import Message, BufferedInputFile, InputMediaPhoto, MediaUnion

# Імпортуємо конфіг та функцію генерації
import config
from ai_config import generate_news_digest

# ---------------------------------------------------------------------------
#  Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("news-parcer")

# ---------------------------------------------------------------------------
#  Constants
# ---------------------------------------------------------------------------
SEEN_POSTS_FILE = Path("seen_posts.json")
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
    if not SEEN_POSTS_FILE.exists():
        return set()
    try:
        data = json.loads(SEEN_POSTS_FILE.read_text(encoding="utf-8"))
        return set(data.get("seen", []))
    except (json.JSONDecodeError, KeyError):
        return set()


def save_seen_posts(seen: set[str]):
    """Зберігає в файл, обрізаючи до MAX_SEEN_POSTS найновіших."""
    as_list = list(seen)[-MAX_SEEN_POSTS:]
    SEEN_POSTS_FILE.write_text(
        json.dumps({"seen": as_list}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
#  Telegram bot init
# ---------------------------------------------------------------------------
log.debug("Ініціалізація Telegram бота (з автоматичним запуском)...")
bot = Bot(
    token=config.BOT_TOKEN,
    default=DefaultBotProperties(link_preview_is_disabled=True),
)
dp = Dispatcher()


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
                log.warning("HTTP %s для %s (спроба %d/%d)", resp.status, url, attempt, retries)
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            log.warning("Помилка запиту %s (спроба %d/%d): %s", url, attempt, retries, exc)
        if attempt < retries:
            await asyncio.sleep(delay)
            delay *= 2
    return None


# ---------------------------------------------------------------------------
#  Smart message splitting
# ---------------------------------------------------------------------------
async def send_long_message_to_user(bot_instance: Bot, chat_id: int, text: str) -> list[int]:
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
            # Шукаємо останній перенос рядка, потім пробіл
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
                text_data += f"Пост: {clean_text}\nПосилання на оригінал: {post_link}\n\n"
                new_ids.add(post_id_attr)
                posts_added += 1
                photo_urls: list[dict[str, str]] = []
                for photo_a in msg.select("a.tgme_widget_message_photo_wrap"):
                    style_attr = str(photo_a.get("style") or "")
                    href_attr = str(photo_a.get("href") or "")
                    match = re.search(r"background-image:url\('(.+?)'\)", style_attr)
                    if match and href_attr:
                        photo_urls.append({
                            "url": match.group(1),
                            "link": href_attr.split("?")[0],
                        })
                if photo_urls:
                    images_by_post[post_id_attr] = photo_urls

    log.debug(
        "З каналу %s: додано %d, пропущено (раніше бачені) %d",
        channel, posts_added, posts_skipped,
    )
    return text_data, new_ids, images_by_post


async def get_telegram_posts_via_web(
    session: aiohttp.ClientSession,
    seen: set[str],
) -> tuple[str, set[str], dict]:
    """Scrape all configured channels concurrently."""
    t0 = time.perf_counter()
    log.debug("Початок збору даних з Telegram-каналів... (всього збережено %d ID)", len(seen))
    time_threshold = datetime.now(timezone.utc) - timedelta(days=1)
    log.debug("Шукаємо пости, опубліковані після: %s", time_threshold)

    tasks = [
        _scrape_channel(session, ch, seen, time_threshold)
        for ch in config.TG_CHANNELS
    ]
    results = await asyncio.gather(*tasks)

    # Merge results
    text_data = ""
    new_ids: set[str] = set()
    images_by_post: dict[str, list[dict[str, str]]] = {}
    for ch_text, ch_ids, ch_images in results:
        text_data += ch_text
        new_ids |= ch_ids
        images_by_post.update(ch_images)

    log.debug("Збір даних завершено за %.2f сек.", time.perf_counter() - t0)
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
    # Flatten to a list of photo dicts, capped at limit
    flat_photos = list(islice(
        (photo for photos in images_by_post.values() for photo in photos),
        limit,
    ))
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
        session, images_by_post, getattr(config, "MAX_TOTAL_IMAGES", 10),
    )
    if not loaded:
        return response_text

    caption_limit = 1024
    caption = response_text[:caption_limit]
    leftover = response_text[caption_limit:]

    log.debug("Надсилаємо галерею зображень (%d шт.) з текстом у підписі...", len(loaded))
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
    t0 = time.perf_counter()
    waiting_msg = await bot_instance.send_message(chat_id, "🗞 Друкую газету... Зачекайте хвилинку.")

    try:
        seen = load_seen_posts()

        async with aiohttp.ClientSession(
            timeout=HTTP_TIMEOUT,
            headers={"User-Agent": USER_AGENT},
        ) as session:
            tg_content, new_post_ids, images_by_post = await get_telegram_posts_via_web(session, seen)

            if not new_post_ids:
                await waiting_msg.edit_text("Свіжих новин за останні 24 години не знайдено.")
                return

            log.debug("Сирий текст зібрано. Запит до Gemini через ai_config...")
            response_text = generate_news_digest(tg_content)

            log.debug("Надсилаємо газету...")

            if getattr(config, "SEND_IMAGES", True):
                digest_post_ids = extract_post_ids_from_digest(response_text)
                filtered_images = {
                    pid: photos
                    for pid, photos in images_by_post.items()
                    if pid in digest_post_ids
                }
                leftover_text = await send_post_images(
                    bot_instance, chat_id, response_text, filtered_images, session,
                )
                if leftover_text:
                    await send_long_message_to_user(bot_instance, chat_id, leftover_text)
            else:
                await send_long_message_to_user(bot_instance, chat_id, response_text)

        await waiting_msg.delete()

        # Зберігаємо нові ID разом з попередніми
        seen |= new_post_ids
        save_seen_posts(seen)
        log.debug("Збережено %d нових ID постів у seen_posts.json", len(new_post_ids))

        log.debug("Газету успішно надіслано! Загальний час: %.2f сек.", time.perf_counter() - t0)

    except Exception as exc:
        log.error("Помилка під час генерації газети: %s", exc)
        try:
            await waiting_msg.edit_text(f"Сталася помилка при генерації: {exc}")
        except Exception as edit_err:
            log.error("Не вдалося відредагувати статус-повідомлення: %s", edit_err)


# ---------------------------------------------------------------------------
#  Auto-run on startup
# ---------------------------------------------------------------------------
async def auto_run_gazette(bot_instance: Bot):
    """Запускає генерацію газети автоматично відразу після старту main.py."""
    # Даємо боту 1-2 секунди, щоб повністю підключитися до серверів Telegram
    await asyncio.sleep(2)
    log.debug("--- АВТОЗАПУСК: Починаємо підготовку газети при старті скрипта ---")

    if not hasattr(config, "MY_TELEGRAM_ID") or config.MY_TELEGRAM_ID == 123456789:
        log.warning("Автозапуск скасовано: Не вказано ваш реальний MY_TELEGRAM_ID у файлі config.py!")
        return

    await make_and_send_gazette(bot_instance, config.MY_TELEGRAM_ID)
    await dp.stop_polling()


# ---------------------------------------------------------------------------
#  Aiogram handlers
# ---------------------------------------------------------------------------
@dp.message(Command("start"))
async def cmd_start(message: Message):
    await message.answer(
        "Привіт! Я твій персональний редактор газети.\n"
        "Надішли /gazette, і я підготую свіжий випуск новин."
    )


@dp.message(Command("gazette"))
async def cmd_gazette(message: Message):
    if not message.from_user:
        log.warning("Отримано команду /gazette без даних користувача.")
        return

    user_id = message.from_user.id
    log.debug("Користувач %d запросив випуск газети вручну (/gazette)", user_id)
    await make_and_send_gazette(bot, user_id)


# ---------------------------------------------------------------------------
#  Entry point
# ---------------------------------------------------------------------------
async def main():
    asyncio.create_task(auto_run_gazette(bot))
    log.debug("Запуск bot polling...")
    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()
        log.debug("Роботу завершено.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.debug("Роботу бота зупинено.")