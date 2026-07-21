import asyncio
import json
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
import requests
from bs4 import BeautifulSoup
import sys

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import Command
from aiogram.types import Message

# Імпортуємо конфіг та функцію генерації
import config
from ai_config import generate_news_digest
SEEN_POSTS_FILE = Path("seen_posts.json")
MAX_SEEN_POSTS = 200


def load_seen_posts() -> list:
    if not SEEN_POSTS_FILE.exists():
        return []
    try:
        data = json.loads(SEEN_POSTS_FILE.read_text(encoding="utf-8"))
        return data.get("seen", [])
    except (json.JSONDecodeError, KeyError):
        return []


def save_seen_posts(seen: list):
    # Зберігаємо порядок (останні додані залишаються в кінці)
    sorted_list = seen[-MAX_SEEN_POSTS:]
    SEEN_POSTS_FILE.write_text(
        json.dumps({"seen": sorted_list}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


print("[DEBUG] Ініціалізація Telegram бота (з автоматичним запуском)...")
bot = Bot(
    token=config.BOT_TOKEN,
    default=DefaultBotProperties(link_preview_is_disabled=True)
)
dp = Dispatcher()

# --- ФУНКЦІЯ БЕЗПЕЧНОГО НАДСИЛАННЯ ДОВГОГО ТЕКСТУ ---
async def send_long_message_to_user(bot_instance: Bot, chat_id: int, text: str):
    """
    Розбиває довгий текст на частини по 4000 символів та надсилає їх користувачу за його ID.
    """
    limit = 4000
    if len(text) <= limit:
        await bot_instance.send_message(chat_id, text)
    else:
        print(f"[DEBUG] Повідомлення занадто довге ({len(text)} симв.). Розбиваємо на частини...")
        for i in range(0, len(text), limit):
            part = text[i:i+limit]
            await bot_instance.send_message(chat_id, part)
            await asyncio.sleep(0.5)

# --- ФУНКЦІЯ ЗБОРУ ДАНИХ ЧЕРЕЗ WEB-PARSING ---
def get_telegram_posts_via_web() -> tuple[str, set]:
    start_time = time.time()
    seen_list = load_seen_posts()
    seen = set(seen_list)
    print(f"[DEBUG] Початок збору даних з Telegram-каналів... (всього збережено {len(seen)} ID)")
    text_data = ""
    new_ids: set = set()
    time_threshold = datetime.now(timezone.utc) - timedelta(days=1)
    print(f"[DEBUG] Шукаємо пости, опубліковані після: {time_threshold}")
    
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }
    
    for channel in config.TG_CHANNELS:
        print(f"[DEBUG] Парсимо канал: t.me/{channel}")
        text_data += f"\n*** Джерело: t.me/{channel} ***\n"
        url = f"https://t.me/s/{channel}"
        
        try:
            response = requests.get(url, headers=headers, timeout=10)
            if response.status_code != 200:
                text_data += "(не вдалося отримати доступ до сторінки каналу)\n"
                continue
                
            soup = BeautifulSoup(response.text, 'html.parser')
            messages = soup.find_all('div', class_='tgme_widget_message')
            print(f"[DEBUG] Знайдено всього повідомлень на сторінці: {len(messages)}")
            
            posts_added = 0
            posts_skipped = 0
            for msg in messages:
                post_id_attr = msg.get('data-post')
                if not post_id_attr or not isinstance(post_id_attr, str):
                    continue

                if post_id_attr in seen:
                    posts_skipped += 1
                    continue

                post_link = f"https://t.me/{post_id_attr}"

                time_tag = msg.find('time', class_='time')
                if not time_tag:
                    continue
                
                post_time_attr = time_tag.get('datetime')
                if not post_time_attr or not isinstance(post_time_attr, str):
                    continue
                
                post_time_str: str = post_time_attr
                if post_time_str.endswith('Z'):
                    post_time_str = post_time_str[:-1] + '+00:00'
                
                try:
                    post_time = datetime.fromisoformat(post_time_str)
                except ValueError:
                    continue
                
                if post_time < time_threshold:
                    continue
                
                text_block = msg.find('div', class_='tgme_widget_message_text')
                if text_block:
                    clean_text = text_block.get_text(separator="\n").strip()
                    if clean_text:
                        text_data += f"Пост: {clean_text}\nПосилання на оригінал: {post_link}\n\n"
                        new_ids.add(post_id_attr)
                        posts_added += 1
            
            print(f"[DEBUG] З каналу {channel}: додано {posts_added}, пропущено (раніше бачені) {posts_skipped}")
                        
        except Exception as e:
            print(f"[ERROR] Помилка при обробці каналу {channel}: {e}")
            text_data += f"(Помилка зчитування каналу: {e})\n"
            
    end_time = time.time()
    parsing_duration = end_time - start_time
    print(f"[DEBUG] Збір даних завершено за {parsing_duration:.2f} сек.\n")
    return text_data, new_ids

# --- СПІЛЬНА ЛОГІКА СТВОРЕННЯ ТА НАДСИЛАННЯ ГАЗЕТИ ---
async def make_and_send_gazette(bot_instance: Bot, chat_id: int):
    """
    Генерує дайджест та надсилає його вказаному користувачу (потрібно як для команди, так і для автозапуску).
    """
    total_start = time.time()
    waiting_msg = await bot_instance.send_message(chat_id, "🗞 Друкую газету... Зачекайте хвилинку.")
    
    try:
        tg_content, new_post_ids = get_telegram_posts_via_web()

        if not new_post_ids:
            await waiting_msg.edit_text("Свіжих новин за останні 24 години не знайдено.")
            return

        print("[DEBUG] Сирий текст зібрано. Запит до Gemini через ai_config...")
        response_text = generate_news_digest(tg_content)
        
        print("[DEBUG] Надсилаємо газету...")
        await send_long_message_to_user(bot_instance, chat_id, response_text)
        
        await waiting_msg.delete()

        if new_post_ids:
            seen_list = load_seen_posts()
            for post_id in new_post_ids:
                if post_id not in seen_list:
                    seen_list.append(post_id)
            save_seen_posts(seen_list)
            print(f"[DEBUG] Збережено {len(new_post_ids)} нових ID постів у seen_posts.json")
        
        total_end = time.time()
        total_duration = total_end - total_start
        print(f"[DEBUG] Газету успішно надіслано! Загальний час: {total_duration:.2f} сек.\n")

        
    except Exception as e:
        print(f"[ERROR] Помилка під час генерації газети: {e}")
        try:
            await waiting_msg.edit_text(f"Сталася помилка при генерації: {e}")
        except Exception as edit_err:
            print(f"[ERROR] Не вдалося відредагувати статус-повідомлення: {edit_err}")

# --- ФУНКЦІЯ АВТОЗАПУСКУ ПРИ СТАРТІ ---
async def auto_run_gazette(bot_instance: Bot):
    """
    Запускає генерацію газети автоматично відразу після старту main.py.
    """
    # Даємо боту 1-2 секунди, щоб повністю підключитися до серверів Telegram
    await asyncio.sleep(2)
    print("\n[DEBUG] --- АВТОЗАПУСК: Починаємо підготовку газети при старті скрипта ---")
    
    if not hasattr(config, 'MY_TELEGRAM_ID') or config.MY_TELEGRAM_ID == 123456789:
        print("[WARNING] Автозапуск скасовано: Не вказано ваш реальний MY_TELEGRAM_ID у файлі config.py!")
        return
        
    await make_and_send_gazette(bot_instance, config.MY_TELEGRAM_ID)
    # await bot.session.close()
    await dp.stop_polling()

# --- ХЕНДЛЕРИ AIOGRAM ---
@dp.message(Command("start"))
async def cmd_start(message: Message):
    await message.answer(
        "Привіт! Я твій персональний редактор газети.\n"
        "Надішли /gazette, і я підготую свіжий випуск новин."
    )

@dp.message(Command("gazette"))
async def cmd_gazette(message: Message):
    if not message.from_user:
        print("[WARNING] Отримано команду /gazette без даних користувача.")
        return
    
    # Тепер Pylance точно знає, що тут буде тільки число (int)
    user_id = message.from_user.id
    print(f"[DEBUG] Користувач {user_id} запросив випуск газети вручну (/gazette)")
    await make_and_send_gazette(bot, user_id)

# --- ЗАПУСК БОТА ---
async def main():
    # Запускаємо таску автозапуску
    asyncio.create_task(auto_run_gazette(bot))
    
    print("[DEBUG] Запуск bot polling...")
    try:
        await dp.start_polling(bot)
    finally:
        # Це гарантує закриття сесії навіть при помилках
        await bot.session.close()
        print("[DEBUG] Роботу завершено.")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("[DEBUG] Роботу бота зупинено.")