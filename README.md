# Innopolis Helper Bot

Telegram-ассистент по внутренней документации. Отвечает только по загруженным
документам, указывает источники, собирает обратную связь, создаёт заявки в
поддержку, управляет доступом в Telegram и показывает аналитику обращений.

Документы разбираются через Docling и удалённую мультимодальную модель. Ответы
строятся по результатам BM25-поиска по версионированным фрагментам и через
OpenAI-совместимую чат-модель.

## Быстрый старт с Docker

Требуется Docker с Compose. Для локального сервера моделей без GPU используйте
необязательный профиль `llm`, описанный ниже.

**1. Создайте файл окружения.**

```bash
cp .env.example .env
```

Задайте как минимум:

```env
TELEGRAM_BOT_TOKEN=...
TELEGRAM_ALLOWED_USERNAMES=@alice,bob
LLM_BASE_URL=https://your-provider.example.com/v1
LLM_MODEL=your-multimodal-model-id
LLM_API_KEY=your-api-key
POSTGRES_PASSWORD=replace-with-a-strong-password
GRAFANA_ADMIN_PASSWORD=replace-with-another-password
GRAFANA_DB_PASSWORD=replace-with-a-reader-password
```

`DOCUMENTS_BASE_URL` и `DOCUMENT_LINK_SECRET` включают подписанные ссылки на
источники и должны быть заданы вместе. На Linux укажите `HOST_UID` и `HOST_GID`
своего пользователя, чтобы загруженные и сгенерированные файлы получали
правильного владельца.

**2. Соберите образы.**

```bash
./scripts/docker_build.sh
```

Скрипт собирает основные образы, образ админ-панели и образ ingestion, а затем
удаляет устаревшие Docker-данные. В ingestion-образ устанавливаются PyTorch
с поддержкой только CPU, Docling и LibreOffice, поэтому первая сборка большая.
Зависимости кэшируются в отдельном слое: изменения в `app/` больше не
переустанавливают их.

Эквивалентная команда вручную:

```bash
docker compose --profile ingest build
```

Модели Docling и Hugging Face хранятся в томе `ingest_cache` и скачиваются
только при первом извлечении.

**3. Запустите стек.**

```bash
docker compose up -d
docker compose ps
```

Бот применяет миграции базы данных при старте. PostgreSQL должен быть здоров до
того, как бот, обработчики и админ-панель начнут с ним работать. Контейнер
админ-панели завершается, пока не подготовлен `storage/admin.env`; остальные
сервисы продолжают работать.

**4. Откройте сервисы.**

| Сервис | Адрес |
| --- | --- |
| Админ-панель | `http://127.0.0.1:8001/admin/` |
| Просмотр документов | `http://127.0.0.1:8090` |
| Grafana | `http://127.0.0.1:3000/d/bot-analytics` |

Все опубликованные порты привязаны к `127.0.0.1`. Для удалённого доступа
используйте обратный прокси.

## Сервисы Docker

| Сервис | Назначение | Профиль |
| --- | --- | --- |
| `bot` | Опрос Telegram и ответы | по умолчанию |
| `postgres` | PostgreSQL | по умолчанию |
| `viewer` | Просмотр подписанных PDF/Office-источников | по умолчанию |
| `admin` | Админ-панель | по умолчанию |
| `document-worker` | Обработка загрузок из админ-панели | по умолчанию |
| `ticket-worker` | Отправка заявок в поддержку; простаивает без провайдера | по умолчанию |
| `grafana` | Дашборды | по умолчанию |
| `ingest` | Однократная массовая обработка PDF | `ingest` |
| `analytics-labeler` | Классификация вопросов для аналитики Grafana | `analytics` |
| `llama` | Необязательный локальный сервер llama.cpp | `llm` |

Сервисы с профилем не запускаются обычной командой `docker compose up -d`.
Включайте их явно, например:

```bash
docker compose --profile analytics up -d analytics-labeler
docker compose --profile ingest run --rm ingest
```

### Необязательный локальный llama.cpp

Положите GGUF-модель и мультимодальный проектор в `.models/`, задайте
`LLM_BASE_URL=http://llama:8080/v1` и запустите профиль `llm`:

```bash
docker compose --profile llm up -d llama
```

Если имена файлов отличаются от значений по умолчанию, задайте
`LLAMA_MODEL_FILE` и `LLAMA_MMPROJ_FILE`. Для параллельного извлечения
увеличивайте `LLAMA_PARALLEL` и `LLAMA_CTX` вместе; каждый слот получает
`LLAMA_CTX / LLAMA_PARALLEL`. Для сервера с одним слотом установите
`EXTRACTION_VISION_CONCURRENCY=1`.

### Настройка админ-панели

Админ-панели нужны две ограниченные роли PostgreSQL и файл `storage/admin.env`.
Обычные представления используют роль только для чтения `bot_admin_reader`.
Управление списком доступа, приглашениями, загрузками и удалением документов
использует роль `bot_admin_access`.

Создайте `storage/admin.env` (файл игнорируется git, права `chmod 600`):

```env
ADMIN_DATABASE_URL=postgresql+asyncpg://bot_admin_reader:READER_PASSWORD@127.0.0.1:5438/bot
ADMIN_ACCESS_DATABASE_URL=postgresql+asyncpg://bot_admin_access:ACCESS_PASSWORD@127.0.0.1:5438/bot
ADMIN_USERNAME=admin
ADMIN_PASSWORD=CHOOSE_A_STRONG_PASSWORD
```

Создайте роли через образ бота после первого запуска стека, когда бот применил
миграции:

```bash
docker compose run --rm --no-deps \
  -e ADMIN_READER_PASSWORD='READER_PASSWORD' \
  bot python scripts/setup_admin_reader.py

docker compose run --rm --no-deps \
  -e ADMIN_ACCESS_DATABASE_URL='postgresql+asyncpg://bot_admin_access:ACCESS_PASSWORD@127.0.0.1:5438/bot' \
  bot python scripts/setup_admin_access.py
```

Замените примеры паролей и закодируйте специальные символы в URL подключения
(percent-encoding).

Затем запустите панель:

```bash
docker compose up -d admin
docker compose logs -f admin
```

После обновления существующей установки повторно запустите
`setup_admin_access.py`: функции удаления документов нужны добавленные права
на таблицы.

Если `storage/admin.env` отсутствует, завершается только контейнер админ-панели.
Остальной стек продолжает работать.

### Документы

**Загрузка через админ-панель (рекомендуется).** Откройте **Document upload
queue → Upload document** и выберите PDF, DOCX, XLSX или PPTX. Файл попадает в
`data/inbox/`, ставится в очередь PostgreSQL и обрабатывается
`document-worker` в версионированные фрагменты под `data/processed/`. Следующий
вопрос боту уже использует новый документ без перезапуска.

Обработчик берёт один документ за раз. В очереди отображается 10 строк на
страницу; для старых загрузок используйте пагинацию. Чтобы обновить прогресс,
перезагрузите страницу очереди. Стадии: `ingest`, `render`, `image analysis`,
`chunking` и `completed`.

Если такая контрольная сумма уже сохранена или активно обрабатывается,
загрузка записывается как `file already exists` и не обрабатывается.

**Удаление документа.** На вкладке **Documents** есть действие **Delete**. Оно
удаляет документ, его фрагменты и сохранённые файлы версий из базы знаний, но
оставляет историю в очереди загрузок. Тот же файл можно загрузить снова.
Для этого контейнер админ-панели монтирует `data/processed` в режиме чтения и
записи.

**Массовая загрузка из `data/knowledge` (необязательно).** Массовый сканер
обрабатывает только PDF:

```bash
cp new-document.pdf data/knowledge/
docker compose --profile ingest run --rm ingest
```

Неизменённые файлы используют кэш извлечения. Посмотреть вычисленные ID
документов без обработки:

```bash
docker compose --profile ingest run --rm ingest --dry-run
```

### Операции

```bash
docker compose ps
docker compose logs -f bot
docker compose logs -f document-worker
docker compose stop
docker compose start
```

Остановить контейнеры, сохранив тома PostgreSQL, Grafana и кэша моделей:

```bash
docker compose --profile "*" down
```

Удалить контейнеры **и все сохранённые данные**, включая базу, дашборды и кэш
моделей. Это необратимо:

```bash
docker compose --profile "*" down -v
```

После изменения `.env` пересоздайте нужный сервис; `restart` не перечитывает
переменные окружения:

```bash
docker compose up -d --force-recreate bot
docker compose up -d --force-recreate admin
docker compose up -d --force-recreate document-worker
```

После изменения кода приложения или Dockerfile пересоберите образы:

```bash
docker compose --profile ingest build
docker compose up -d
```

Миграции можно применить вручную. Бот также делает это автоматически при
старте:

```bash
docker compose run --rm --no-deps bot alembic upgrade head
```

### Обслуживание диска Docker

```bash
./scripts/docker_cleanup.sh
```

Скрипт удаляет остановленные одноразовые контейнеры, висячие образы,
неиспользуемые сети и кэш сборки старше 7 дней. Именованные тома никогда не
удаляются. Настройте срок через `BUILD_CACHE_TTL` или удалите неиспользуемые
образы с `--images`:

```bash
BUILD_CACHE_TTL=720h ./scripts/docker_cleanup.sh
./scripts/docker_cleanup.sh --images
./scripts/docker_cleanup.sh --all-cache
```

Для автоматической сборки с очисткой используйте `./scripts/docker_build.sh`.
Логи контейнеров ограничены 3 × 10 МБ на сервис.

## Запуск локально без Docker

Обычный запуск через Python занимает несколько команд. Docker необязателен;
бот поддерживает SQLite (по умолчанию) или PostgreSQL.

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements/base.txt
cp .env.example .env
# укажите TELEGRAM_BOT_TOKEN, LLM_BASE_URL, LLM_MODEL и LLM_API_KEY
.venv/bin/python -m app
```

Бот применяет миграции при старте и создаёт `data/bot.sqlite3`, если
`DATABASE_URL` не задан. Он может запуститься без документов и будет отвечать,
что подходящие фрагменты не найдены.

Для обработки документов установите зависимости извлечения и LibreOffice для
Office-файлов:

```bash
.venv/bin/python -m pip install -r requirements/ingestion.txt
```

Необязательный локальный просмотрщик:

```bash
.venv/bin/python -m uvicorn app.anchors.viewer:app --host 127.0.0.1 --port 8090
```

Ручной конвейер для одного документа, если вы не используете обработчик из
админ-панели:

```bash
.venv/bin/python -m app.rag.ingest "data/knowledge/travel.pdf" --document-id travel
.venv/bin/python -m app.rag.render --all
.venv/bin/python -m app.rag.extract --all
```

`app.rag.extract --pages 4,7` создаёт частичное отладочное извлечение, которое
бот игнорирует. Команды `--all` обрабатывают все сохранённые версии.

### Локальный запуск админ-панели

У админ-панели другие требования к FastAPI, чем у бота, поэтому используйте
отдельное окружение:

```bash
python3 -m venv storage/admin-venv
storage/admin-venv/bin/pip install -r requirements/admin.txt
```

Запустите панель с теми же значениями `storage/admin.env`, что и в Docker:

```bash
ADMIN_DATABASE_URL='postgresql+asyncpg://bot_admin_reader:READER_PASSWORD@127.0.0.1:5438/bot' \
ADMIN_ACCESS_DATABASE_URL='postgresql+asyncpg://bot_admin_access:ACCESS_PASSWORD@127.0.0.1:5438/bot' \
ADMIN_USERNAME=admin \
ADMIN_PASSWORD=CHOOSE_A_STRONG_PASSWORD \
  storage/admin-venv/bin/uvicorn app.admin.web:create_app \
  --factory --host 127.0.0.1 --port 8001
```

Для локальных загрузок также запустите `document-worker` из основного окружения
с заданными `DATABASE_URL` и `DOCUMENTS_DIR`. Держите админ-панель привязанной к
localhost или размещайте её за HTTPS с контролем доступа. AMIS по умолчанию
загружает ресурсы интерфейса с `unpkg.com`.

## Конфигурация

Основные переменные:

| Переменная | По умолчанию | Назначение |
| --- | --- | --- |
| `TELEGRAM_BOT_TOKEN` | — | Токен бота от @BotFather |
| `TELEGRAM_BOT_USERNAME` | — | Имя бота без `@`; включает ссылки-приглашения |
| `TELEGRAM_ALLOWED_USERNAMES` | пусто: доступ запрещён всем | Однократная инициализация списка доступа |
| `ACCESS_INVITE_NOTIFY_CHAT_ID` | — | Необязательный Telegram-чат для уведомлений об активации приглашений |
| `SUPPORT_URL` | пусто | Адрес кнопки поддержки |
| `LLM_BASE_URL` | локальное значение по умолчанию | Базовый URL OpenAI-совместимого провайдера |
| `LLM_MODEL` | `qwen3.8-flash` | ID модели провайдера |
| `LLM_API_KEY` | `local` | API-ключ провайдера |
| `LLM_SESSION_ID` | `innopolis-helper-bot` | Необязательный заголовок маршрутизации/сессии |
| `LLM_USER_AGENT` | `innopolis-helper-bot/1.0` | Идентификация клиента |
| `LLM_ANSWER_REASONING_EFFORT` | `minimal` | Усилие рассуждения для ответа |
| `LLM_EXTRACTION_REASONING_EFFORT` | `low` | Усилие рассуждения при извлечении |
| `EXTRACTION_VISION_CONCURRENCY` | `2` | Параллельные vision-запросы по картинкам/страницам |
| `EXTRACTION_TEXT_CONCURRENCY` | `4` | Параллельные запросы группировки фрагментов |
| `EXTRACTION_REQUEST_TIMEOUT` | `180` | Таймаут запроса извлечения, секунды |
| `EXTRACTION_MIN_IMAGE_PIXELS` | `1024` | Пропускать мелкие декоративные картинки; `0` отключает |
| `EXTRACTION_CACHE_ENABLED` | `true` | Кэшировать сырые ответы извлечения |
| `EXTRACTION_CACHE_DIR` | `~/.cache/innopolis-helper-bot/vision` | Каталог кэша сырых ответов |
| `LLM_ENABLE_THINKING` | `auto` | Режим thinking для проверенной пары OpenCode/qwen |
| `LLM_THINKING_BUDGET` | по умолчанию провайдера (`512`) | Бюджет thinking-токенов |
| `ANSWER_MAX_COMPLETION_TOKENS` | `1400` | Лимит thinking + ответа |
| `ANSWER_MAX_TOKENS` | `600` | Лимит длины ответа |
| `TEMPERATURE` | `0.1` | Температура ответа |
| `TOP_K` | `2` | Начальное число результатов поиска |
| `MIN_SCORE` | `0` | Минимальный балл BM25 |
| `KNOWLEDGE_DIR` | `data/knowledge` | Каталог PDF для массовой загрузки |
| `DOCUMENTS_DIR` | `data/processed/documents` | Хранилище версионированных документов |
| `DATABASE_PATH` | `data/bot.sqlite3` | Путь SQLite |
| `DATABASE_URL` | — | Необязательный URL PostgreSQL |
| `DOCUMENTS_BASE_URL` | — | Публичный URL просмотрщика для подписанных ссылок |
| `DOCUMENT_LINK_SECRET` | — | HMAC-секрет для подписанных ссылок |
| `TICKET_PROVIDER` | пусто: отключено | `demo`, `jsm`, `zendesk` или `usedesk` |
| `TICKET_BASE_URL` | — | URL API тикет-провайдера |
| `TICKET_API_TOKEN` | — | Секрет тикет-провайдера |
| `TICKET_EMAIL` | — | Необязательный email агента |
| `JIRA_SERVICE_DESK_ID` | — | ID service desk в Jira Service Management |
| `JIRA_REQUEST_TYPE_ID` | — | ID типа запроса в Jira Service Management |
| `USEDESK_CHANNEL_ID` | — | ID канала Usedesk |
| `TICKET_WEB_BASE_URL` | — | Префикс URL для ссылок на тикеты в браузере |
| `ANALYTICS_AREAS_FILE` | — | Необязательное сопоставление продуктовых областей для разметчика |

Переменные только для Docker находятся в `.env` или `storage/admin.env`:

| Переменная | Назначение |
| --- | --- |
| `POSTGRES_PASSWORD` | Пароль PostgreSQL; обязателен для Compose |
| `POSTGRES_PORT` | Порт PostgreSQL на хосте, по умолчанию `5438` |
| `GRAFANA_ADMIN_PASSWORD` | Пароль администратора Grafana |
| `GRAFANA_DB_PASSWORD` | Пароль роли Grafana только для чтения |
| `GRAFANA_PORT` | Порт Grafana на хосте, по умолчанию `3000` |
| `VIEWER_PORT` | Порт просмотрщика на хосте, по умолчанию `8090` |
| `ADMIN_PORT` | Порт админ-панели на хосте, по умолчанию `8001` |
| `HOST_UID`, `HOST_GID` | Владелец файлов извлечения на хосте |
| `ADMIN_DATABASE_URL` | URL PostgreSQL только для чтения для админ-панели |
| `ADMIN_ACCESS_DATABASE_URL` | Ограниченный URL для операций записи в админ-панели |
| `LLAMA_MODEL_FILE` | Имя GGUF-модели для необязательного профиля `llm` |
| `LLAMA_MMPROJ_FILE` | Имя мультимодального проектора для профиля `llm` |
| `LLAMA_CTX` | Общий размер контекста llama.cpp |
| `LLAMA_PARALLEL` | Слоты сервера llama.cpp; согласуйте с параллелизмом извлечения |

`LLM_SESSION_ID` и `LLM_USER_AGENT` необязательны для обычных провайдеров и
нужны некоторым маршрутизирующим провайдерам. Значения reasoning — параметры
модели провайдера (`none`, `minimal`, `low`, `medium`, `high`).

## Извлечение и поиск

Каждый документ хранится как неизменяемая версия, при необходимости
рендерится и разбирается на версионированные фрагменты. Бот перечитывает
изменённые манифесты фрагментов перед каждым вопросом, поэтому загрузки и
удаления не требуют перезапуска.

- Docling извлекает текст, таблицы и области изображений.
- Области изображений и страницы с малым количеством текста анализируются
  мультимодальной моделью.
- Независимые vision-запросы выполняются параллельно; сырые ответы кэшируются
  для каждого запроса.
- Чёткие визуальные элементы принимаются по отдельности; неопределённая
  транскрипция или направление стрелок на схеме блокируют только этот элемент.
  Ограничения, конфликты и предложения на проверку сохраняются в метаданных
  проверки.
- Страницы с небольшими областями изображений и слабым текстовым слоем
  дополнительно анализируются целиком, а очень высокие страницы делятся на
  перекрывающиеся фрагменты, чтобы текст расписаний и постеров не терялся при
  уменьшении изображения моделью.
- BM25 передаёт доказательства модели ответа; ответы должны содержать ссылки
  на ID источников.
- Расширение контекста поиска ограничено и не обрезает фрагменты.
- Узлы и связи схем, строки и столбцы таблиц из принятых визуальных фрагментов
  доступны в промпте ответа.

Поддерживаются PDF, DOCX, XLSX, PPTX, изображения, текст, Markdown, HTML,
структурированные данные и исходный код. Админ-панель принимает PDF, DOCX,
XLSX и PPTX; для остальных форматов используйте CLI. Для извлечения из Office
нужен LibreOffice, установленный локально или доступный в ingestion-образе.
Если `soffice` отсутствует в `PATH`, задайте `SOFFICE_PATH`.

## Доступ, тикеты и аналитика

`TELEGRAM_ALLOWED_USERNAMES` однократно инициализирует список доступа. Дальше
управляйте доступом на вкладке **Allowed Telegram users** в админ-панели.
Ссылки-приглашения привязывают Telegram ID к списку доступа и могут иметь срок
действия и лимит использований. Изменения доступа применяются без перезапуска
бота.

`ticket-worker` запускается вместе с основным стеком и простаивает, пока не
задан `TICKET_PROVIDER`. Он сразу сохраняет запрос в надёжную очередь и
записывает внешний ID тикета после подтверждения создания провайдером.
Демо-режим:

```env
TICKET_PROVIDER=demo
```

```bash
docker compose up -d ticket-worker
docker compose logs -f ticket-worker
```

Аналитика поддержки необязательна. Фоновый разметчик классифицирует новые
вопросы для дашбордов Grafana и не задерживает ответы в Telegram. Запускайте
один разметчик на базу данных:

```bash
docker compose --profile analytics up -d analytics-labeler
docker compose --profile analytics logs -f analytics-labeler
```

Задайте `ANALYTICS_AREAS_FILE` для необязательного JSON-сопоставления
продуктовых областей. Подробности о тикет-провайдерах, процессе разметки и
приватности см. в [docs/analytics.md](docs/analytics.md).

## Тесты

```bash
.venv/bin/python -m pip install -r requirements/dev.txt
.venv/bin/python -m pytest
```

Офлайн-тесты не требуют учётных данных модели. Перед полным набором тестов
установите зависимости извлечения.

## Диагностика

**У бота нет фрагментов.** Он запускается, но не может отвечать по документам,
пока не завершится загрузка или массовая обработка. Проверьте
`docker compose logs -f document-worker` и `docker compose ps`.

**Загрузка через админ-панель остаётся в очереди.** Обработчик берёт один
документ за раз. Большой PDF блокирует следующие загрузки. Не удаляйте
подготовленный файл из inbox, пока задача активна.

**Повторная загрузка отклонена.** Сохранённая версия или загрузка в очереди
имеет такую же контрольную сумму. Если нужно заменить документ, удалите
существующий документ на вкладке **Documents**; завершённая история очереди не
блокирует повторную загрузку.

**`Extraction is locked`.** Предыдущий запуск был прерван. Убедитесь, что
обработчик не работает, и удалите указанный файл `.extract.lock`,
`.ingest.lock` или `.render.lock`.

**Модель возвращает не JSON или пустые фрагменты.** Убедитесь, что модель
принимает изображения, проверьте `LLM_BASE_URL`, `LLM_MODEL` и `LLM_API_KEY` и
повторите попытку. Сырые ответы кэшируются; смена модели или промптов
сбрасывает кэш.

**Не работает рендеринг Office.** Установите LibreOffice и убедитесь, что
`soffice` доступен в `PATH`, либо задайте `SOFFICE_PATH` / передайте
`--soffice`.

**Доступ запрещён.** Проверьте Telegram-username отправителя и активную запись
в **Allowed Telegram users**. Список из переменной окружения используется
только для однократной инициализации.

**Сеть всё ещё используется после `down`.** Остановите необязательные профили:

```bash
docker compose --profile "*" down
```

Не коммитьте `.env`, `storage/admin.env`, внутренние документы и
сгенерированные артефакты.

## Благодарности

Админ-панель основана на
[FastAPI-Amis-Admin](https://github.com/amisadmin/fastapi-amis-admin) и
UI-фреймворке [AMIS](https://github.com/baidu/amis).

Дополнительные компоненты: [aiogram](https://github.com/aiogram/aiogram) для
Telegram, [Docling](https://github.com/docling-project/docling) для разбора
документов, [PyMuPDF](https://github.com/pymupdf/PyMuPDF) для рендеринга PDF,
[FastAPI](https://fastapi.tiangolo.com/) и
[SQLAlchemy](https://www.sqlalchemy.org/), а также
[Grafana](https://grafana.com/) для дашбордов.
