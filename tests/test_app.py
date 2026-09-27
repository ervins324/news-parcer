import os
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import config
import ai_config
import main


class TestConfigLoading(unittest.TestCase):
    """Тести завантаження та парсингу конфігурації."""

    def test_parse_channels_comma_separated(self):
        result = config._parse_channels("chan1, @chan2, chan3")
        self.assertEqual(result, ["chan1", "chan2", "chan3"])

    def test_parse_channels_json_array(self):
        result = config._parse_channels('["@channel_a", "channel_b"]')
        self.assertEqual(result, ["channel_a", "channel_b"])

    def test_parse_channels_empty_fallback(self):
        result = config._parse_channels("")
        self.assertEqual(result, ["naebnet", "kiev_levyy_bereg"])

    def test_parse_schedule_times(self):
        result = config._parse_schedule_times("08:30, 20:15, invalid, 25:00")
        self.assertEqual(result, ["08:30", "20:15"])

    def test_parse_schedule_times_empty_fallback(self):
        result = config._parse_schedule_times("")
        self.assertEqual(result, ["09:00", "20:00"])

    def test_config_defaults_loaded(self):
        self.assertTrue(isinstance(config.TG_CHANNELS, list))
        self.assertTrue(isinstance(config.SCHEDULE_TIMES, list))
        self.assertTrue(isinstance(config.TIMEZONE, str))
        self.assertIn("gemini", config.GEMINI_MODEL)
        self.assertTrue(bool(config.OPENROUTER_MODEL))


class TestSeenPosts(unittest.TestCase):
    """Тести кешування та персистентності прочитаних постів."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.temp_file = Path(self.temp_dir.name) / "test_seen.json"
        self.original_seen_file = config.SEEN_POSTS_FILE
        config.SEEN_POSTS_FILE = self.temp_file

    def tearDown(self):
        config.SEEN_POSTS_FILE = self.original_seen_file
        self.temp_dir.cleanup()

    def test_load_nonexistent_file(self):
        seen = main.load_seen_posts()
        self.assertEqual(seen, set())

    def test_save_and_load_seen_posts(self):
        posts = {"post_1", "post_2", "post_3"}
        main.save_seen_posts(posts)
        loaded = main.load_seen_posts()
        self.assertEqual(loaded, posts)

    def test_max_seen_posts_limit(self):
        # Generate 250 posts, save, and ensure max 200 are kept
        posts = {f"post_{i}" for i in range(250)}
        main.save_seen_posts(posts)
        loaded = main.load_seen_posts()
        self.assertLessEqual(len(loaded), main.MAX_SEEN_POSTS)


class TestSchedulerSetup(unittest.TestCase):
    """Тести конфігурації планувальника задач APScheduler."""

    def test_setup_scheduler_registers_jobs(self):
        main.scheduler.remove_all_jobs()
        with patch.object(config, "SCHEDULE_TIMES", ["08:00", "18:30"]):
            main.setup_scheduler()
            jobs = main.scheduler.get_jobs()
            self.assertEqual(len(jobs), 2)
            job_names = [j.name for j in jobs]
            self.assertTrue(any("08:00" in name for name in job_names))
            self.assertTrue(any("18:30" in name for name in job_names))


class TestLLMGenerationAndFallback(unittest.TestCase):
    """Тести генерації новин через Gemini та резервний OpenRouter."""

    @patch("google.genai.Client")
    def test_gemini_success_primary(self, mock_genai_client):
        # Gemini returns content successfully
        mock_instance = MagicMock()
        mock_response = MagicMock()
        mock_response.text = "=== NN ===\n- ⚡ Важлива новина (Джерело: https://t.me/naebnet/1)"
        mock_response.usage_metadata.total_token_count = 150
        mock_instance.models.generate_content.return_value = mock_response
        mock_genai_client.return_value = mock_instance

        with patch.object(config, "GEMINI_API_KEY", "dummy_key"):
            result = ai_config.generate_news_digest("Пост 1")
            self.assertIn("=== NN ===", result)
            self.assertIn("Важлива новина", result)

    @patch("ai_config._generate_with_openrouter")
    @patch("ai_config._generate_with_gemini")
    def test_fallback_to_openrouter_on_gemini_error(self, mock_gemini, mock_openrouter):
        # Gemini raises an error
        mock_gemini.side_effect = Exception("Gemini quota exceeded / 429")
        mock_openrouter.return_value = "=== OpenRouter Digest ===\n- ⚡ Резервна новина"

        result = ai_config.generate_news_digest("Сирий текст")
        mock_gemini.assert_called_once()
        mock_openrouter.assert_called_once()
        self.assertIn("=== OpenRouter Digest ===", result)

    @patch("ai_config._generate_with_openrouter")
    @patch("ai_config._generate_with_gemini")
    def test_both_providers_fail_raises_runtime_error(self, mock_gemini, mock_openrouter):
        mock_gemini.side_effect = Exception("Gemini down")
        mock_openrouter.side_effect = Exception("OpenRouter down")

        with self.assertRaises(RuntimeError) as ctx:
            ai_config.generate_news_digest("Пост")
        self.assertIn("Gemini down", str(ctx.exception))
        self.assertIn("OpenRouter down", str(ctx.exception))

    @patch("openrouter.OpenRouter")
    def test_openrouter_direct_call(self, mock_openrouter_class):
        mock_client = MagicMock()
        mock_openrouter_class.return_value.__enter__.return_value = mock_client

        mock_choice = MagicMock()
        mock_choice.message.content = "OpenRouter response text"
        mock_res = MagicMock()
        mock_res.choices = [mock_choice]
        mock_res.model = "google/gemini-2.0-flash-001"
        mock_client.chat.send.return_value = mock_res

        with patch.object(config, "OPENROUTER_API_KEY", "test_key"):
            result = ai_config._generate_with_openrouter("Test prompt")
            self.assertEqual(result, "OpenRouter response text")


class TestHelpers(unittest.TestCase):
    """Тести допоміжних функцій."""

    def test_extract_post_ids_from_digest(self):
        text = (
            "=== NN ===\n"
            "- ⚡ Новина 1 (Джерело: https://t.me/naebnet/101)\n"
            "- 🚦 Новина 2 (Джерело: https://t.me/kiev_levyy_bereg/202)\n"
        )
        ids = main.extract_post_ids_from_digest(text)
        self.assertEqual(ids, {"naebnet/101", "kiev_levyy_bereg/202"})

    @patch.dict(os.environ, {"OPENROUTER_API_KEY": ""}, clear=False)
    def test_list_openrouter_models_without_key(self):
        with patch.object(config, "OPENROUTER_API_KEY", ""):
            models = ai_config.list_openrouter_models()
            self.assertEqual(models, [])


if __name__ == "__main__":
    unittest.main()
