import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from telegram_client import (
    Bot,
    Dispatcher,
    Command,
    Message,
    BufferedInputFile,
    InputMediaPhoto,
)


class TestTelegramClient(unittest.IsolatedAsyncioTestCase):
    """Тести для легкого Telegram-клієнта (заміна aiogram)."""

    async def test_message_attributes(self):
        raw = {
            "message_id": 42,
            "from": {"id": 112233, "first_name": "TestUser"},
            "chat": {"id": 998877, "type": "private"},
            "text": "/gazette",
        }
        bot = Bot(token="123456:dummy_token")
        msg = Message(raw, bot)

        self.assertEqual(msg.message_id, 42)
        self.assertEqual(msg.from_user.id, 112233)
        self.assertEqual(msg.from_user.first_name, "TestUser")
        self.assertEqual(msg.chat.id, 998877)
        self.assertEqual(msg.text, "/gazette")
        await bot.close()

    async def test_command_dispatching(self):
        dp = Dispatcher()
        called_cmd = []

        @dp.message(Command("test"))
        async def handle_test(msg: Message):
            called_cmd.append(msg.text)

        bot = Bot(token="123456:dummy_token")
        fake_update = {
            "update_id": 1,
            "message": {
                "message_id": 10,
                "chat": {"id": 123},
                "text": "/test@my_bot arg1",
            },
        }

        await dp._process_update(fake_update, bot)
        self.assertEqual(called_cmd, ["/test@my_bot arg1"])
        await bot.close()

    @patch("telegram_client.Bot._api_call")
    async def test_send_message(self, mock_api):
        mock_api.return_value = {"message_id": 55, "text": "hello"}
        bot = Bot(token="123456:dummy")

        msg = await bot.send_message(12345, "hello", parse_mode="HTML")
        self.assertEqual(msg.message_id, 55)
        mock_api.assert_called_once()
        await bot.close()

    @patch("telegram_client.Bot._api_call")
    async def test_send_photo(self, mock_api):
        mock_api.return_value = {"message_id": 77}
        bot = Bot(token="123456:dummy")

        photo = BufferedInputFile(b"fake_image_bytes", "test.jpg")
        msg = await bot.send_photo(12345, photo, caption="caption test")
        self.assertEqual(msg.message_id, 77)
        mock_api.assert_called_once()
        await bot.close()

    @patch("telegram_client.Bot._api_call")
    async def test_send_media_group(self, mock_api):
        mock_api.return_value = [{"message_id": 101}, {"message_id": 102}]
        bot = Bot(token="123456:dummy")

        media = [
            InputMediaPhoto(media=BufferedInputFile(b"img1", "1.jpg"), caption="cap1"),
            InputMediaPhoto(media=BufferedInputFile(b"img2", "2.jpg")),
        ]
        msgs = await bot.send_media_group(12345, media)
        self.assertEqual(len(msgs), 2)
        self.assertEqual(msgs[0].message_id, 101)
        self.assertEqual(msgs[1].message_id, 102)
        mock_api.assert_called_once()
        await bot.close()


if __name__ == "__main__":
    unittest.main()
