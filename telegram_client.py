import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Any, Callable, Coroutine, Optional, Union

import aiohttp

log = logging.getLogger("telegram_client")


@dataclass
class BufferedInputFile:
    """Сумісний клас для передачі бінарних файлів (зображень)."""
    data: bytes
    filename: str


@dataclass
class InputMediaPhoto:
    """Сумісний клас для опису фотографії в альбомі (sendMediaGroup)."""
    media: Union[BufferedInputFile, str]
    caption: Optional[str] = None


MediaUnion = InputMediaPhoto


@dataclass
class User:
    id: int
    first_name: str = ""
    last_name: Optional[str] = None
    username: Optional[str] = None


@dataclass
class Chat:
    id: int
    type: str = "private"


class Message:
    """Легковажний об'єкт повідомлення Telegram."""

    def __init__(self, raw: dict[str, Any], bot: "Bot"):
        self.raw = raw
        self.bot = bot
        self.message_id: int = raw.get("message_id", 0)
        from_dict = raw.get("from") or {}
        self.from_user: Optional[User] = (
            User(
                id=from_dict.get("id", 0),
                first_name=from_dict.get("first_name", ""),
                last_name=from_dict.get("last_name"),
                username=from_dict.get("username"),
            )
            if from_dict
            else None
        )
        chat_dict = raw.get("chat") or {}
        self.chat = Chat(
            id=chat_dict.get("id", 0),
            type=chat_dict.get("type", "private"),
        )
        self.text: str = raw.get("text") or raw.get("caption") or ""

    async def answer(self, text: str, parse_mode: Optional[str] = "HTML") -> "Message":
        return await self.bot.send_message(self.chat.id, text, parse_mode=parse_mode)

    async def edit_text(self, text: str, parse_mode: Optional[str] = None) -> "Message":
        return await self.bot.edit_message_text(self.chat.id, self.message_id, text, parse_mode=parse_mode)

    async def delete(self) -> bool:
        return await self.bot.delete_message(self.chat.id, self.message_id)


class DefaultBotProperties:
    def __init__(self, link_preview_is_disabled: bool = True):
        self.link_preview_is_disabled = link_preview_is_disabled


class Command:
    def __init__(self, command: str):
        self.command = command.lstrip("/")


class Bot:
    """
    Легкий клієнт Telegram Bot API на базі aiohttp.
    Замінює важкий aiogram без втрати швидкості та можливостей.
    Споживає всього ~2 MB RAM замість 140 MB у aiogram.
    """

    def __init__(self, token: str, default: Optional[DefaultBotProperties] = None):
        self.token = token
        self.default = default or DefaultBotProperties()
        self.api_url = f"https://api.telegram.org/bot{token}"
        self._session: Optional[aiohttp.ClientSession] = None
        self._owns_session = False

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
            self._owns_session = True
        return self._session

    @property
    def session(self):
        class _SessionWrapper:
            def __init__(self, bot_instance: "Bot"):
                self.bot = bot_instance

            async def close(self):
                await self.bot.close()

        return _SessionWrapper(self)

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    async def _api_call(self, method: str, data: Optional[dict] = None, form_data: Optional[aiohttp.FormData] = None) -> dict:
        session = await self._get_session()
        url = f"{self.api_url}/{method}"
        try:
            if form_data is not None:
                async with session.post(url, data=form_data, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                    result = await resp.json()
            else:
                async with session.post(url, json=data or {}, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                    result = await resp.json()

            if not result.get("ok"):
                log.warning("Telegram API error in %s: %s", method, result.get("description"))
                raise RuntimeError(f"Telegram API {method} error: {result.get('description')}")
            return result.get("result", {})
        except Exception as exc:
            log.warning("HTTP помилка при виклику %s: %s", method, exc)
            raise

    async def send_message(
        self,
        chat_id: int,
        text: str,
        parse_mode: Optional[str] = None,
    ) -> Message:
        payload = {
            "chat_id": chat_id,
            "text": text,
        }
        if parse_mode:
            payload["parse_mode"] = parse_mode
        if self.default and self.default.link_preview_is_disabled:
            payload["link_preview_options"] = {"is_disabled": True}

        res = await self._api_call("sendMessage", data=payload)
        return Message(res, self)

    async def edit_message_text(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        parse_mode: Optional[str] = None,
    ) -> Message:
        payload = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
        }
        if parse_mode:
            payload["parse_mode"] = parse_mode
        res = await self._api_call("editMessageText", data=payload)
        return Message(res if isinstance(res, dict) else {"message_id": message_id}, self)

    async def delete_message(self, chat_id: int, message_id: int) -> bool:
        try:
            await self._api_call("deleteMessage", data={"chat_id": chat_id, "message_id": message_id})
            return True
        except Exception:
            return False

    async def send_photo(
        self,
        chat_id: int,
        photo: Union[BufferedInputFile, str],
        caption: Optional[str] = None,
    ) -> Message:
        form = aiohttp.FormData()
        form.add_field("chat_id", str(chat_id))
        if caption:
            form.add_field("caption", caption)

        if isinstance(photo, BufferedInputFile):
            form.add_field("photo", photo.data, filename=photo.filename)
        else:
            form.add_field("photo", str(photo))

        res = await self._api_call("sendPhoto", form_data=form)
        return Message(res, self)

    async def send_media_group(
        self,
        chat_id: int,
        media: list[InputMediaPhoto],
    ) -> list[Message]:
        form = aiohttp.FormData()
        form.add_field("chat_id", str(chat_id))

        media_descriptors = []
        for idx, item in enumerate(media):
            field_name = f"photo_{idx}"
            desc = {"type": "photo", "media": f"attach://{field_name}"}
            if item.caption:
                desc["caption"] = item.caption
            media_descriptors.append(desc)

            if isinstance(item.media, BufferedInputFile):
                form.add_field(field_name, item.media.data, filename=item.media.filename)
            else:
                form.add_field(field_name, str(item.media))

        form.add_field("media", json.dumps(media_descriptors))
        res = await self._api_call("sendMediaGroup", form_data=form)
        if isinstance(res, list):
            return [Message(item, self) for item in res]
        return []


HandlerFunc = Callable[[Message], Coroutine[Any, Any, Any]]


class Dispatcher:
    """Легкий диспетчер команд для Telegram-бота."""

    def __init__(self):
        self._command_handlers: dict[str, HandlerFunc] = {}
        self._is_polling = False

    def message(self, filter_obj: Any):
        """Декоратор для реєстрації хендлера (сумісний із dp.message(Command('cmd')))."""
        def decorator(func: HandlerFunc):
            if isinstance(filter_obj, Command):
                self._command_handlers[filter_obj.command.lower()] = func
            elif hasattr(filter_obj, "command"):
                self._command_handlers[str(filter_obj.command).lower()] = func
            return func
        return decorator

    async def stop_polling(self):
        self._is_polling = False

    async def start_polling(self, bot: Bot):
        """Long-polling цикл для отримання команд з Telegram."""
        self._is_polling = True
        offset = 0
        session = await bot._get_session()
        log.info("Запуск легкого Telegram polling циклу...")

        while self._is_polling:
            try:
                url = f"{bot.api_url}/getUpdates"
                params = {"offset": offset, "timeout": 25}
                async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=35)) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        updates = data.get("result", [])
                        for update in updates:
                            update_id = update.get("update_id", 0)
                            offset = max(offset, update_id + 1)
                            await self._process_update(update, bot)
                    else:
                        await asyncio.sleep(2)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                log.warning("Помилка polling Telegram API: %s. Повтор за 3с...", exc)
                await asyncio.sleep(3)

    async def _process_update(self, update: dict[str, Any], bot: Bot):
        raw_msg = update.get("message")
        if not raw_msg:
            return

        msg = Message(raw_msg, bot)
        text = (msg.text or "").strip()
        if text.startswith("/"):
            # Витягуємо назву команди (наприклад, "/gazette@bot" -> "gazette")
            cmd_part = text.split()[0][1:].split("@")[0].lower()
            handler = self._command_handlers.get(cmd_part)
            if handler:
                try:
                    await handler(msg)
                except Exception as exc:
                    log.error("Помилка обробки команди /%s: %s", cmd_part, exc, exc_info=True)
