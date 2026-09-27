import logging
import os
from google import genai
from openrouter import OpenRouter

import config

log = logging.getLogger(__name__)


def _generate_with_gemini(prompt: str) -> str:
    """Генерація дайджесту через Google Gemini."""
    if not config.GEMINI_API_KEY:
        raise ValueError("GEMINI_API_KEY не налаштовано у файлі .env")

    log.debug("Підключення до Google Gemini (модель: %s)...", config.GEMINI_MODEL)
    ai_client = genai.Client(api_key=config.GEMINI_API_KEY)
    response = ai_client.models.generate_content(
        model=config.GEMINI_MODEL,
        contents=prompt,
    )

    if response.usage_metadata:
        total_tokens = response.usage_metadata.total_token_count
        log.info("Gemini: Генерація успішна. Використано токенів: %s", total_tokens)

    if not response.text:
        raise ValueError("Google Gemini повернув порожню відповідь.")

    return response.text


def _generate_with_openrouter(prompt: str) -> str:
    """Генерація дайджесту через OpenRouter (резервне джерело)."""
    if not config.OPENROUTER_API_KEY:
        raise ValueError("OPENROUTER_API_KEY не налаштовано у файлі .env")

    log.info("Підключення до OpenRouter (модель: %s)...", config.OPENROUTER_MODEL)
    with OpenRouter(
        api_key=config.OPENROUTER_API_KEY,
        http_referer=config.OPENROUTER_HTTP_REFERER or None,
        x_open_router_title=config.OPENROUTER_TITLE or None,
    ) as open_router:
        res = open_router.chat.send(
            model=config.OPENROUTER_MODEL,
            messages=[
                {"content": prompt, "role": "user"},
            ],
            stream=False,
        )

        if not res or not res.choices:
            raise ValueError(f"OpenRouter повернув несподівану порожню відповідь: {res}")

        choice = res.choices[0]
        content = choice.message.content
        if not content:
            raise ValueError("OpenRouter повернув повідомлення без текстового контенту.")

        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            text = "".join(getattr(item, "text", str(item)) for item in content)
        else:
            text = str(content)

        used_model = getattr(res, "model", config.OPENROUTER_MODEL)
        log.info("OpenRouter: Генерація успішна (модель: %s)", used_model)
        return text


def list_openrouter_models(limit: int = 100) -> list[dict[str, str]]:
    """
    Допоміжна функція для отримання списку доступних моделей OpenRouter.
    Використовується для вибору моделі для OPENROUTER_MODEL в .env.
    """
    api_key = config.OPENROUTER_API_KEY or os.getenv("OPENROUTER_API_KEY", "")
    if not api_key:
        log.warning("OPENROUTER_API_KEY не задано. Неможливо отримати список моделей.")
        return []

    models: list[dict[str, str]] = []
    with OpenRouter(
        http_referer=config.OPENROUTER_HTTP_REFERER or None,
        x_open_router_title=config.OPENROUTER_TITLE or None,
        api_key=api_key,
    ) as open_router:
        res = open_router.models.list(offset=0, limit=limit)
        while res is not None:
            if hasattr(res, "result") and hasattr(res.result, "data") and res.result.data:
                for item in res.result.data:
                    models.append({
                        "id": getattr(item, "id", ""),
                        "name": getattr(item, "name", ""),
                    })
            if hasattr(res, "next") and callable(res.next):
                res = res.next()
            else:
                break

    return models


def generate_news_digest(tg_content: str) -> str:
    """
    Генерує новинний дайджест із постів Telegram.
    Спочатку робить спробу через Google Gemini.
    У разі помилки автоматично перемикається на OpenRouter.
    """
    prompt = f"""
    Твоє завдання — створити лаконічний новинний дайджест із наданих сирих постів.

    Ось дані:
    {tg_content}

    === ФІЛЬТРАЦІЯ ===
    Ігноруй: кримінал, ДТП, збори коштів, рекламу, будь-який контент про рф, погоду, напрямки бпла, ракет, оголошення повітряних тривог, побутові скарги (ями, смітники) та дрібні локальні події.
    Якщо після фільтрації новин не залишилось — поверни рівно цей рядок і нічого більше: "На сьогодні важливих новин немає."

    === СТРУКТУРА ВИВОДУ (ДОТРИМУЙСЯ СТРОГО) ===
    Вивід — це послідовність секцій, по одній на кожен канал, що є у вхідних даних.
    Кожна секція має такий вигляд:

    === НАЗВА КАНАЛУ ===
    - <ЕМОДЗІ> текст новини (Джерело: https://t.me/...)
    - <ЕМОДЗІ> текст новини (Джерело: https://t.me/...)

    Між секціями — порожній рядок. Всередині секції кожен пункт починається з дефіса "-", потім пробіл, потім рівно ОДИН емодзі зі списку нижче, потім пробіл, потім текст новини.
    Після тексту новини обов'язково додай "(Джерело: https://t.me/...)" з точним посиланням на оригінальний пост.

    === ФІКСОВАНА КАРТА ЕМОДЗІ ===
    Обирай емодзі ЛИШЕ з цього переліку, відповідно до теми новини:
    ⚡ — важлива подія
    🚨 — надзвичайна подія / НП
    💥 — атака / обстріл
    💧 — комуналка / вода
    🏗 — будівництво
    🚦 — транспорт / дороги
    🏥 — медицина
    💰 — економіка / бізнес
    ⚽ — спорт
    🌤 — погода

    Жодних інших емодзі не використовуй. Кожен пункт ОБОВ'ЯЗКОВО має рівно один емодзі з цього списку. Якщо жодна категорія не підходить — використовуй ⚡.

    === ПРИКЛАД ПРАВИЛЬНОГО ВИВОДУ ===
    === NN ===
    - ⚡ Новий законопроєкт про енергоринок внесено до Ради (Джерело: https://t.me/naebnet/102)
    - 🚦 На перехресті встановили новий світлофор (Джерело: https://t.me/naebnet/101)

    === Київ Лівий Берег ===
    - 🚨 Пожежа у Дніпровському районі локалізована (Джерело: https://t.me/kiev_levyy_bereg/75778)

    === НАЗВИ КАНАЛІВ ===
    Назви каналів підставляй так:
    - "naebnet" → "NN"
    - "kiev_levyy_bereg" → "Київ Лівий Берег"
    Для будь-якого іншого каналу використовуй його username без символу "@".

    === ЗАБОРОНЕНО ===
    - Жодних привітань ("Доброго ранку"), епілогів ("Бажаємо гарного дня"), пояснень чи передмов.
    - Жодного markdown: без зірочок **, без зворотних лапок `, без #.
    - Жодних довгих тире (—) — тільки звичайний дефіс (-) або пробіли.
    - Не пиши емодзі в заголовках секцій — емодзі тільки в пунктах.
    - Завжди пиши українською, навіть якщо джерело російською.
    - Не додавай пунктів від себе — лише те, що є у вхідних даних.
    - Не нумеруй пункти.
    """

    gemini_error: Exception | None = None

    # 1. Основний провайдер: Google Gemini
    try:
        log.info("Спроба генерації дайджесту через Google Gemini (%s)...", config.GEMINI_MODEL)
        return _generate_with_gemini(prompt)
    except Exception as exc:
        gemini_error = exc
        log.warning("Помилка при зверненні до Google Gemini: %s", exc)

    # 2. Резервний провайдер: OpenRouter
    log.info("Перемикання на резервний LLM провайдер: OpenRouter...")
    try:
        return _generate_with_openrouter(prompt)
    except Exception as openrouter_err:
        log.error("Помилка при зверненні до OpenRouter: %s", openrouter_err)
        raise RuntimeError(
            f"Не вдалося згенерувати дайджест новин. "
            f"Gemini: {gemini_error}; OpenRouter: {openrouter_err}"
        ) from openrouter_err


if __name__ == "__main__":
    import sys
    if sys.platform == "win32":
        try:
            sys.stdout.reconfigure(encoding="utf-8")
            sys.stderr.reconfigure(encoding="utf-8")
        except Exception:
            pass
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    if "--test-openrouter" in sys.argv:
        print("=== ТЕСТ OPENROUTER ===")
        if not config.OPENROUTER_API_KEY:
            print("❌ ПОМИЛКА: OPENROUTER_API_KEY не вказано у файлі .env!")
            print("Отримайте ключ на https://openrouter.ai/settings/keys та додайте його в .env")
            sys.exit(1)
        print(f"Модель: {config.OPENROUTER_MODEL}")
        print("Надсилаємо тестовий запит...")
        try:
            test_prompt = "Напиши українською: 'OpenRouter успішно підключено та працює!' та додай одне коротке речення про штучний інтелект."
            reply = _generate_with_openrouter(test_prompt)
            print("\n✅ ВІДПОВІДЬ ВІД OPENROUTER:")
            print("-" * 50)
            print(reply)
            print("-" * 50)
        except Exception as err:
            print(f"\n❌ ПОМИЛКА під час запиту до OpenRouter: {err}")

    elif "--test-fallback" in sys.argv:
        print("=== ТЕСТ ПЕРЕМИКАННЯ GEMINI -> OPENROUTER (FALLBACK) ===")
        if not config.OPENROUTER_API_KEY:
            print("❌ ПОМИЛКА: OPENROUTER_API_KEY не вказано у файлі .env!")
            sys.exit(1)
        # Симулюємо помилку Gemini
        original_gemini_key = config.GEMINI_API_KEY
        config.GEMINI_API_KEY = "invalid_dummy_key_to_force_fallback"
        sample_tg_text = (
            "*** Джерело: t.me/naebnet ***\n"
            "Пост: У Києві відкрито новий технологічний хаб для інженерів.\n"
            "Посилання на оригінал: https://t.me/naebnet/999\n"
        )
        print("Симулюємо збій Gemini та викликаємо generate_news_digest()...")
        try:
            digest = generate_news_digest(sample_tg_text)
            print("\n✅ УСПІШНИЙ FALLBACK! Результат, згенерований OpenRouter:")
            print("-" * 50)
            print(digest)
            print("-" * 50)
        except Exception as err:
            print(f"\n❌ ПОМИЛКА при fallback: {err}")
        finally:
            config.GEMINI_API_KEY = original_gemini_key

    elif "--list-models" in sys.argv:
        print("Запит списку моделей OpenRouter...")
        try:
            model_list = list_openrouter_models(limit=50)
            if model_list:
                print(f"Знайдено моделей: {len(model_list)}")
                for m in model_list:
                    print(f"• {m['id']:<40} | {m['name']}")
            else:
                print("Моделей не знайдено або не вказано OPENROUTER_API_KEY у .env.")
        except Exception as e:
            print(f"Помилка при запиті моделей: {e}")
    else:
        print("Доступні команди для тестування LLM:")
        print("  python ai_config.py --test-openrouter  -> Тестовий прямий запит до OpenRouter")
        print("  python ai_config.py --test-fallback    -> Перевірка автоматичного перемикання (Gemini помилка -> OpenRouter)")
        print("  python ai_config.py --list-models      -> Перегляд доступних моделей OpenRouter")
