# Freelans Bot (RU-first, Full-Auto PoC)

Автоматический пайплайн для фриланс-площадок:

1. Парсинг новых проектов с площадок.
2. Скоринг релевантности под ваш стек.
3. Генерация отклика через LLM.
4. Полуавтомат: генерация отклика по кнопке из Telegram (автоотправка отключена по умолчанию).
5. Уведомление в Telegram со ссылками на объявление/отклик/чат.

## Поддерживаемые площадки (в конфиге)

- `flru`
- `freelance_ru`
- `kwork`
- `workzilla`
- `youdo`
- `yandex_uslugi`
- `freelancejob`

## Архитектура

- `src/freelans_bot/adapters/` — адаптеры площадок (универсальный Playwright-адаптер + конфиг)
- `src/freelans_bot/core/orchestrator.py` — пайплайн `collect -> score -> draft -> apply -> notify`
- `src/freelans_bot/services/` — скоринг и генератор откликов
- `src/freelans_bot/integrations/telegram.py` — уведомления в Telegram
- `src/freelans_bot/storage/db.py` — SQLite-хранилище лидов/событий/откликов
- `src/freelans_bot/app.py` — FastAPI + фоновый воркер (удобно для Railway)

## Быстрый старт

```bash
cp .env.example .env
# заполните TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID и OPENROUTER_API_KEY
python3 -m venv .venv
.venv/bin/python -m pip install -e .
.venv/bin/playwright install chromium
```

Сохранить сессии логина (по площадкам):

```bash
.venv/bin/python scripts/save_session.py --list
.venv/bin/python scripts/save_session.py --platform flru
.venv/bin/python scripts/save_session.py --platform freelance_ru
.venv/bin/python scripts/save_session.py --platform kwork
.venv/bin/python scripts/save_session.py --platform workzilla
.venv/bin/python scripts/save_session.py --platform youdo
.venv/bin/python scripts/save_session.py --platform yandex_uslugi
.venv/bin/python scripts/save_session.py --platform freelancejob
```

Как проходит логин:

1. Запускаешь команду `save_session.py` для нужной платформы.
2. Открывается браузер Chromium.
3. Логинишься как обычно (пароль, код, капча).
4. Убедись, что вход выполнен (виден аккаунт/аватар).
5. Возвращаешься в терминал и жмешь `Enter`.
6. Скрипт сохраняет сессию в папку `state/`.

Если у площадки сменился URL входа, передай его вручную:

```bash
.venv/bin/python scripts/save_session.py --platform youdo --login-url "https://youdo.com/нужный-url"
```

Запуск локально:

```bash
.venv/bin/python -m uvicorn freelans_bot.app:app --host 0.0.0.0 --port 8000
```

Проверка:

- `GET /health`
- `GET /stats`
- `GET /events`

## Управление через Telegram

Единая точка входа:

- `/start`

Дальше все действия только через inline-кнопки:

1. `Status` — состояние бота.
2. `Accounts` — подключенные аккаунты, включение/выключение платформ, logout.
3. `Profile` — редактирование имени/резюме/аватарки/портфолио.
4. `Settings` — pause/resume и auto-apply on/off.
5. `Run Cycle Now` — ручной запуск цикла парсинга.

В карточке нового объявления есть кнопка `Generate proposal`.
Она генерирует отклик вручную (полуавтомат), без автоотправки при `AUTO_APPLY=false`.

## Обучение качества откликов (без fine-tune)

Схема:

1. Бот отправляет отклик.
2. Ты оцениваешь результат через inline-кнопки `Mark Good` / `Mark Bad`.
3. `good`-отклики попадают в базу успешных примеров.
4. При новых откликах бот подмешивает эти примеры в промпт и пишет лучше в твоем стиле.

Пример отправки фидбека:

```bash
.venv/bin/python scripts/mark_feedback.py \
  --url "https://example.com/job/123" \
  --verdict good \
  --note "ответил в чате и попросил созвон"
```

Посмотреть накопленные успешные примеры:

```bash
curl "http://127.0.0.1:8000/learning/examples?language=ru&limit=5"
```

## Что нужно докрутить перед боевым запуском

- Актуализировать CSS-селекторы в `src/freelans_bot/config/platforms.yaml`.
- Добавить анти-дубликаты по `external_id`, если площадка его отдает явно.
- Подключить прокси/антикапчу при необходимости.

## Важные переменные окружения

- `AUTO_APPLY=false` — полуавтомат (рекомендуется для старта).
- `MIN_SCORE_TO_APPLY=0.45` — минимальный скор для автоотклика.
- `KEYWORDS` / `NEGATIVE_KEYWORDS` — основной фильтр качества.
- `FREELANCER_PROFILE`, `PORTFOLIO_URLS` — контекст для генерации отклика.
- `LLM_PROVIDER=openrouter` — использовать OpenRouter.
- `OPENROUTER_API_KEY` — ключ OpenRouter.
- `OPENROUTER_MODEL` — модель в формате OpenRouter (`openai/gpt-4.1-mini`, `anthropic/claude-3.7-sonnet`, и т.д.).
