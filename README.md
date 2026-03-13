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

Команд не нужно. Просто отправь боту любое сообщение, и откроется меню.

Дальше все действия через inline-кнопки:

1. `Status` — состояние бота.
2. `Вакансии` — последние найденные вакансии (ссылки, скор, статус, бюджет).
3. `Accounts` — список бирж, карточка каждой биржи, включение/выключение мониторинга, удаление сессии.
4. `Profile` — общий профиль (имя/резюме/аватар/портфолио).
5. `Settings` — пауза и auto-apply.
6. `Run cycle` — ручной запуск цикла парсинга.

Навигация по меню работает в одном сообщении: при кликах бот редактирует текущий экран, а не спамит новыми сообщениями.
Автоматический цикл работает по кругу: каждые ~5 секунд бот проверяет следующую биржу.
Таким образом биржи сканируются по очереди без простоев и с постоянным потоком.
Периодические отчеты по таймеру отключены, отправляются именно карточки вакансий.
Скипнутые заявки в `Вакансии` не показываются.

Внутри `Accounts` можно заполнить анкету для каждой площадки:

- имя на бирже
- заголовок профиля
- описание
- портфолио
- ставки (фикс/почасовая)
- URL страницы профиля

Эти данные подмешиваются в генерацию отклика для соответствующей платформы.
После редактирования анкеты бот автоматически запускает синхронизацию профиля на сайт.
Также есть ручная кнопка `Синхронизировать на сайт` в карточке площадки.

В карточке объявления есть кнопки:

- `Сгенерировать отклик` — быстрый черновик
- `Свой запрос к ИИ` — сначала вводишь свой текст-пожелание, затем бот генерирует отклик с учетом этого запроса

В экране `Вакансии` эти же действия доступны по каждому `ID`.

## Обучение качества откликов (без fine-tune)

Схема:

1. Бот отправляет отклик.
2. Ты оцениваешь результат через inline-кнопки `Хорошо` и `Плохо`.
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
- `POLL_INTERVAL_SECONDS=5` — интервал между площадками (round-robin).
- `TELEGRAM_NOTIFY_BATCH_SIZE=8` — сколько лидов отправлять в Telegram за один проход.
- `TELEGRAM_NOTIFY_RETRY_AFTER_SECONDS=45` — пауза перед повторной отправкой неудачных уведомлений.
- `TELEGRAM_NOTIFY_MAX_ATTEMPTS=200` — максимум попыток доставки для одного лида.
- `/stats` показывает runtime по площадкам: `ok/error`, `last_success_at`, `last_found`, `last_new`.
- `MIN_SCORE_TO_APPLY=0.45` — минимальный скор для автоотклика.
- `MAX_PAGES_PER_PLATFORM_SCAN=8` — глубина обхода страниц на площадке.
- `PLAYWRIGHT_FEED_TIMEOUT_MS=15000` — таймаут загрузки страницы ленты (не дает зависать на одной бирже).
- `PLAYWRIGHT_CARDS_WAIT_TIMEOUT_MS=5000` — сколько ждать карточки заказов перед переходом к следующей площадке.
- `KEYWORDS` / `NEGATIVE_KEYWORDS` — базовый фильтр качества.
- `FOCUS_KEYWORDS` — фокус на тематике (Python, Telegram-боты, сайты, автоматизации).
- `STRICT_TOPIC_FILTER=true` — строгий режим: лид без совпадений по `FOCUS_KEYWORDS` получает `score=0` и не отправляется в Telegram.
- `FREELANCER_PROFILE`, `PORTFOLIO_URLS` — контекст для генерации отклика.
- `LLM_PROVIDER=openrouter` — использовать OpenRouter.
- `OPENROUTER_API_KEY` — ключ OpenRouter.
- `OPENROUTER_MODEL` — модель в формате OpenRouter (`openai/gpt-4.1-mini`, `anthropic/claude-3.7-sonnet`, и т.д.).
