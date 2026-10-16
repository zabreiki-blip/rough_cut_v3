# rough_cut_v3

Пайплайн: нормализация сценария (`.doc/.docx`) → подтверждение блоков →
синтез TTS → Whisper-сопоставление СНХ → сборка в Premiere.

## Быстрый старт

Минимальный путь до первого результата на macOS, без платных API-ключей
(используется системный голос `say`).

1. Установи зависимости и создай `.env`:

   ```bash
   scripts/setup-local
   ```

   Скрипт создаёт `.venv`, ставит зависимости из `requirements-dev.txt`,
   копирует `.env.example` в `.env` (с сгенерированным `ROUGH_CUT_API_TOKEN`),
   создаёт папки `projects/` и `data/`, затем запускает быструю диагностику.
   В конце выведет `Готово. Запуск API: scripts/run-local`.

2. Открой `.env` и укажи путь без ключей LLM/TTS-провайдеров:

   ```bash
   TTS_PROVIDER=macos
   ```

   Ключ `OPENROUTER_API_KEY` в этом режиме не нужен.

3. (Опционально) Проверь окружение подробнее:

   ```bash
   scripts/doctor
   ```

   Покажет ✅/⚠️/❌ по Python, `ffmpeg`, `.env`, токену, backend на `:8000` и,
   на macOS, по `say`/`afconvert`.

4. Запусти локальный API:

   ```bash
   scripts/run-local
   ```

   Ожидаемый результат: процесс висит в терминале, backend слушает
   `http://127.0.0.1:8000`. Открой этот адрес в браузере — должна появиться
   стартовая страница создания/выбора проекта.

5. Пробный прогон без реального синтеза и без LLM (создай проект и положи
   сценарий в `projects/my_project/input/scenario.docx`, затем):

   ```bash
   scripts/analyze-script --in projects/my_project/input/scenario.docx --no-llm
   scripts/synthesize-tts --in projects/my_project/work/scenario_prepared.json --out projects/my_project/speaker --dry-run
   scripts/validate-stage1 --in projects/my_project/work/scenario_prepared.json --speaker projects/my_project/speaker
   ```

   Ожидаемый результат: в `projects/my_project/work/` появится
   `scenario_prepared.json`, в `projects/my_project/speaker/` — WAV-заглушки
   без реального голоса, а `validate-stage1` выведет отчёт без ошибок.

Дальше — обычный веб-workflow: открой проект в браузере, вкладка
«Нормализация» → проверка блоков → подтверждение → TTS (уже с
`TTS_PROVIDER=macos` или платным провайдером) → Stage 2 (Whisper-синхрон) →
сборка в Premiere. Подробности — в разделах ниже и в
[`docs/STAGE1_GUIDE.md`](docs/STAGE1_GUIDE.md).

## Оглавление

- [Структура](#структура)
- [Простая загрузка файлов](#простая-загрузка-файлов)
  - [Версии сценария](#версии-сценария)
- [Локальные зависимости](#локальные-зависимости)
- [Переменные окружения](#переменные-окружения)
  - [macOS Connector (для Docker backend)](#macos-connector-для-docker-backend)
- [Локальный запуск (CLI)](#локальный-запуск-cli)
- [Ручная разметка диктора](#ручная-разметка-диктора)
- [Dry run (без API-ключей)](#dry-run-без-api-ключей)
- [API (Flask)](#api-flask)
- [Stage 2.1: автоматическое подключение синхронов](#stage-21-автоматическое-подключение-синхронов)
- [Тесты](#тесты)
  - [pre-commit хуки](#pre-commit-хуки)
- [Локальная автоматизация](#локальная-автоматизация)
- [Docker](#docker)
  - [Быстрое развёртывание на другой машине](#быстрое-развёртывание-на-другой-машине)
  - [Apple Silicon M1–M4: Metal/MLX](#apple-silicon-m1m4-metalmlx)
  - [Ручной Docker-запуск](#ручной-docker-запуск)
- [Документация](#документация)

## Структура

- `projects/<project>/input/` — исходный сценарий проекта.
- `projects/<project>/work/` — canonical `scenario_normalized.json` и производные `scenario_raw.txt`/`scenario_prepared.json`.
- `projects/<project>/speaker/assets/` — неизменяемые WAV по UID блока;
  `speaker/revisions/rev_NNNN/` — позиционные `zkN.wav` конкретной редакции.
- `projects/<project>/versions/` — история редакций: сценарии, diff, снимки таймлайна и recovery-копии.
- `src/stage1/` — core-логика и CLI.
- `src/stage2/` — таймлайн, sync-источники, Whisper word index и fuzzy matching.
- `cep/` — панель Premiere для регистрации, экспорта аудиогида и conform.
- `src/api.py` — Flask API.
- `scripts/` — shell-обертки для CLI.

## Простая загрузка файлов

После создания проекта приложение автоматически создаёт две пользовательские
папки:

```text
projects/<project>/ЗАГРУЗИТЬ/Сценарии/
projects/<project>/ЗАГРУЗИТЬ/Просмотровки/
```

- в `Сценарии` кладутся `.doc`, `.docx`, `.txt` или `.md`;
- в `Просмотровки` кладутся видео и отдельные `.wav`/`.mp3`;
- видео и отдельный звук с одинаковым именем связываются автоматически;
- старые `input/` и `media/reviews/` продолжают поддерживаться.

### Версии сценария

История работает одинаково для любого проекта из `projects/`; имя проекта не
зашито в механизм версий. Рабочий цикл во вкладке «Версии»:

1. Создай, проверь и сохрани исходную нормализацию. При первом импорте она
   автоматически фиксируется как baseline `rev_0001`.
2. Положи новый `.doc`, `.docx`, `.txt` или `.md` в
   `ЗАГРУЗИТЬ/Сценарии`, выбери его и нажми «Загрузить новую редакцию».
3. Приложение создаст следующий уникальный `rev_NNNN`, сравнит его с активной
   редакцией и сохранит как отдельный черновик. Импорт сам по себе не меняет
   активную редакцию.
4. Проверь diff и нажми «Открыть» у нужной редакции. Несохранённые правки
   нормализации интерфейс сначала предложит сохранить. После переключения
   редакцию нужно проверить и подтвердить перед TTS, Stage 2 и Stage 3.

Каждая редакция хранит собственные снимки исходного файла, нормализации,
подготовленного сценария и карты озвучки; при наличии также сохраняются sync
matches и таймлайн. Озвучка привязана к стабильному UID блока: `zk1`, `zk2`, …
остаются позиционными экспортными именами, поэтому WAV неизменившихся или
перемещённых блоков можно переиспользовать между редакциями.

Перед импортом и переключением API сверяет `work/active_revision.json` с
`scenario_revision_id` рабочей нормализации. Если они расходятся, операции
блокируются, а вкладка предлагает «Восстановить». Восстановление сначала
сохраняет текущее состояние в `versions/recovery/<timestamp>/`, затем разворачивает
выбранную известную редакцию. Не редактируй файлы в `versions/` вручную.

Импорт, переключение и восстановление также блокируются, пока в этом же проекте
активна нормализация, TTS, sync или подготовка Stage 3. Задачи других проектов
могут выполняться независимо.

## Локальные зависимости

Для обработки legacy `.doc` нужен `catdoc`.

- macOS (Homebrew): `brew install catdoc`
- Debian/Ubuntu: `sudo apt-get install -y catdoc`

Если `catdoc` не нужен, используй `.docx` или `.txt`.

## Переменные окружения

Обязательные ключи:
- `ROUGH_CUT_API_TOKEN` — обязательный токен веб-интерфейса и API
- `OPENROUTER_API_KEY`

Если используешь только `TTS_PROVIDER=macos`, ключи для TTS не нужны.

Важно для `macos`: backend должен работать локально на macOS, либо через коннектор (ниже).
В Docker/Linux команда `say` недоступна напрямую.

Опционально:
- `MAX_ZK_BLOCKS=40` (верхняя граница количества блоков `zkN` после анализа; по умолчанию `40`)
- `VALIDATE_MIN_WAV_DURATION_MS=350` (warning, если WAV короче порога; `0` отключает)
- `VALIDATE_SILENCE_RMS_THRESHOLD=120` (warning, если RMS слишком низкий; `0` отключает)
- `OPENROUTER_MAX_TOKENS` — жёсткий лимит токенов ответа для каждого платного
  чат-запроса к OpenRouter (анализ сценария, классификатор нормализации,
  смысловая проверка синхронов). Не задан — лимит считается по размеру входа
  (не выше 16384). Если ответ обрезан (`finish_reason=length`), запрос не
  повторяется (повтор оплатил бы тот же результат) и завершается ошибкой —
  увеличьте значение. Блок нормализации длиннее 8000 символов отправляется в
  классификатор только головой и хвостом (полный текст остаётся локально)
- `TTS_PROVIDER=openrouter|macos` (в веб-интерфейсе OpenRouter-модель выбирается отдельно)
- `TTS_GENDER=female|male` (пол голоса по умолчанию)
- `TTS_MAX_WORKERS=3` ограничивает параллельные запросы синтеза (`1–4`)
- `ANALYZER_LLM_WORKERS=4` ограничивает параллельный анализ независимых LLM-чанков (`1–4`)
- для `openrouter` доступны `microsoft/mai-voice-2` и
  `google/gemini-3.1-flash-tts-preview` через отдельный TTS endpoint.
  Русские MAI preset-голоса: `ru-RU-Masha:MAI-Voice-2` и
  `ru-RU-Lev:MAI-Voice-2`. Их можно сменить через `OPENROUTER_TTS_MODEL`,
  `OPENROUTER_TTS_VOICE`, `OPENROUTER_TTS_SAMPLE_RATE`
  - можно задать пресеты: `OPENROUTER_TTS_VOICE_FEMALE`, `OPENROUTER_TTS_VOICE_MALE`
  - для Gemini через OpenRouter: `OPENROUTER_GEMINI_TTS_VOICE_FEMALE`,
    `OPENROUTER_GEMINI_TTS_VOICE_MALE`
  - поле «Интонация / стиль / формат ролика» передаёт Gemini режиссёрский prompt,
    а для MAI сопоставляет описание с Azure style; изменение поля инвалидирует TTS-кэш
- для macOS (бесплатно, локально): `MACOS_TTS_VOICE_FEMALE`, `MACOS_TTS_VOICE_MALE`, `MACOS_TTS_RATE` (по умолчанию `Milena/Yuri`)
  - для коннектора macOS: обязательный `MACOS_CONNECTOR_TOKEN`, а также `MACOS_CONNECTOR_URL`, `MACOS_CONNECTOR_TIMEOUT`, `MACOS_CONNECTOR_BIND`, `MACOS_CONNECTOR_PORT`;
    LaunchAgent автоматически задаёт `MACOS_CONNECTOR_ENV_FILE` как путь к
    отдельному файлу настроек с правами `0600`, поэтому эту переменную не нужно
    добавлять в проектный `.env`
- для Stage 2 sync: `SYNC_WHISPER_BACKEND=auto|mlx-whisper|faster-whisper|openai-whisper|cloud-audio`,
  `SYNC_WHISPER_MODEL`, `SYNC_WHISPER_DEVICE`, `SYNC_WHISPER_COMPUTE_TYPE`;
- параметры faster-whisper: `SYNC_WHISPER_BEAM_SIZE` и
  `SYNC_WHISPER_BEST_OF` (по умолчанию `5`), `SYNC_WHISPER_BATCH_SIZE`
  (`0` отключает batching; для CPU начинать с `4` или `8`) и
  `SYNC_WHISPER_CPU_THREADS` (`0` оставляет выбор числа потоков backend-у);
  одна транскрипция выполняется в отдельном процессе с hard timeout
  `SYNC_WHISPER_TIMEOUT_SECONDS` (по умолчанию 14400 секунд); задайте
  `SYNC_WHISPER_UNLOAD_AFTER_JOB=1`, чтобы выгружать процесс и модель после
  каждой успешной транскрипции. Это освобождает RAM, но следующая задача снова
  загрузит модель. По умолчанию воркер вместо этого выгружается сам после
  простоя `SYNC_WHISPER_IDLE_TIMEOUT_SECONDS` секунд (по умолчанию 900 = 15
  минут; `0` отключает авто-выгрузку и держит воркер в памяти бессрочно, как
  раньше) — это не мешает тёплому старту при частых запросах, но не даёт
  модели бесконечно занимать RAM во время долгой открытой сессии Premiere.
- `SYNC_AUDIO_MAX_BYTES` — потоковый лимит фактически прочитанных байт WAV
  (по умолчанию 4 ГБ), работающий и без `Content-Length`
- `SYNC_CLOUD_ASR_OVERLAP_SEC` — перекрытие соседних фрагментов облачного ASR
  в секундах (по умолчанию `60`)
- `SYNC_CLOUD_ASR_MAX_TOKENS` — лимит токенов ответа одного запроса облачного
  ASR (по умолчанию `32768`); при обрезке (`finish_reason=length`) запуск
  завершается ошибкой без повтора
- `SYNC_LLM_REVIEW_ENABLED=1` включает смысловую проверку блоков с низким
  совпадением; `SYNC_LLM_REVIEW_THRESHOLD` задаёт порог (по умолчанию `0.80`),
  а `SYNC_LLM_REVIEW_MAX_BLOCKS` ограничивает число LLM-проверок за запуск.
  Модель выбирает только из реальных диапазонов Whisper, и результат всегда
  требует ручного подтверждения в браузере
- `ROUGH_CUT_CEP_CORS=1` — разрешить аутентифицированные запросы только из
  локального Premiere CEP file origin
- `ROUGH_CUT_HOST_PROJECTS_ROOT` — необязательный путь к корню проектов на
  host-машине для преобразования контейнерных путей, возвращаемых в CEP

### Финальная проверка: стоимость и восстановление

После экспорта WAV CEP вызывает `POST /stage2/final-review/estimate` с
`project` и `audio_path` (WAV внутри проекта). Без обращения к провайдеру
сервер возвращает `duration`/`duration_sec`, `planned_chunks` с учётом
перекрытия, точную десятичную строку `estimated_cost_usd`, `cost_limit_usd`
и `enforced`. Панель показывает оценку до подтверждения. Затем
`POST /stage2/final-review/start` передаёт её строку без округления в
`confirmed_estimate_usd`; HTTP 409 при изменившейся оценке требует нового
подтверждения. Успешный Start возвращает HTTP 202, `job_id` и `status_url`.

`FINAL_REVIEW_TIMEOUT_SECONDS=7200` задаёт срок всей задачи, включая ожидание
ASR. Каждый HTTP-запрос ограничен 900 секундами и оставшимся временем задачи.
Истечение общего срока даёт `failed` с ошибкой timeout. Настройки облачного
ASR: `ROUGH_CUT_MAX_USD_PER_JOB=0` отключает ограничение стоимости;
`ROUGH_CUT_CLOUD_ASR_USD_PER_MINUTE_ESTIMATE=0` означает неизвестную оценку.
Положительный лимит требует положительной цены минуты. Превышение оценки
отклоняет запуск с HTTP 409. Лимит относится отдельно к final review и к
каждой sync-задаче целиком по всем её исходникам, включая платные повторы.
Локальный ASR имеет нулевую облачную оценку. Без включённого лимита показанная
стоимость служит учёту расходов и не ограничивает оплату.

`GET /stage2/final-review/jobs/<job_id>?project=<project>` возвращает статус,
`estimated_cost_usd`, `cost_limit_usd`, известную стоимость `cost_usd`,
неурегулированную оценку `reserved_cost_usd` и флаг `cost_pending`.
Если провайдер не сообщил фактическую цену, оценка остаётся в резерве;
`cost_pending=true` сохраняет неизвестность даже при нулевой оценке.
При отмене или timeout уже принятый провайдером запрос может быть платным:
панель показывает это предупреждение и известную стоимость/резерв. Журнал
хранит числовую оценку `reserved_cost_usd` в pending-событии отдельно от
подтверждённой `cost_usd` (обе суммы сериализованы десятичными строками).
Резерв, включая явный ноль, сохраняется после запуска следующей задачи и
рестарта; он не входит в подтверждённый итог расходов.

При загрузке панели и смене проекта
`GET /stage2/final-review/latest?project=<project>` восстанавливает последнюю
задачу (HTTP 404, если её нет). Для `queued`/`running`/`cancelling` продолжается
опрос, а завершённый отчёт восстанавливает Apply только после проверки
совпадения `sequenceId` активной секвенции. После рестарта сервера незавершённая
задача становится `interrupted`. Ошибки опроса дают задержки 3, 6, 12, 24, 30
секунд, затем кнопку Retry; повтор опроса сохраняет job ID. При скрытой панели
опрос приостанавливается и возобновляется при её открытии.

Cancel вызывает `POST /stage2/final-review/jobs/<job_id>/cancel`: активная
задача отвечает HTTP 202 со статусом `cancelling`, затем `cancelled`;
завершённая отвечает HTTP 200 со своим прежним статусом.
`POST /stage2/final-review/jobs/<job_id>/apply` принимает `project` и
`request_id` (строка до 128 символов). Сетевой повтор одного действия использует
тот же ID и получает ту же итерацию. Новый явный Apply создаёт новый ID и новую
итерацию после возвращения к исходной секвенции и обновления статуса панели;
ID другой задачи вызывает HTTP 409. Старый клиент без ID каждый раз
создаёт итерацию. Панель клонирует подтверждённую секвенцию и применяет правки
в копии; API сам по себе подготавливает запись итерации и corrections.

### macOS Connector (для Docker backend)

Если API крутится в Docker, можно оставить `TTS_PROVIDER=macos` и вынести `say` в локальный сервис на Mac:

1. В `.env` для backend:

```bash
TTS_PROVIDER=macos
MACOS_CONNECTOR_URL=http://host.docker.internal:8001/synthesize
MACOS_CONNECTOR_TOKEN=change_me
MACOS_CONNECTOR_BIND=0.0.0.0
```

2. На Mac (вне Docker) запусти коннектор:

```bash
scripts/run-macos-connector
```

3. Проверка:

```bash
curl -s http://localhost:8001/health
```

По умолчанию коннектор слушает только `127.0.0.1`. Доступ из Docker требует явного
`MACOS_CONNECTOR_BIND=0.0.0.0`; используйте его только с сильным токеном и локальным firewall.
Примечание по безопасности: `http://host.docker.internal` предназначен для локальной машины.
Для удаленного контура поднимай коннектор за HTTPS (TLS, reverse proxy) и не отправляй токен в открытой сети.

Если backend в Docker не видит `host.docker.internal`, проверь что в `docker-compose.yml` есть:

```yaml
extra_hosts:
  - "host.docker.internal:host-gateway"
```

## Локальный запуск (CLI)

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env   # заполни API-ключи
export PYTHONPATH=src
```

Используй именно Python 3.11 — на этой версии проверяются CI, Docker-образ и `ruff target-version`; другая локальная версия может незаметно разойтись в поведении с CI.

В основном веб-потоке сначала открой вкладку «Нормализация», проверь типы всех блоков и подтверди сценарий. TTS и Stage 2 недоступны для черновика. Перед подтверждением интерфейс показывает найденные ручные исправления типа блока: каждое можно применить только сейчас или запомнить. Запомненное исправление становится активным правилом после подтверждения в двух сценариях с разными исходниками. Точные совпадения из общей SQLite-памяти применяются автоматически; подтверждённые решения LLM сохраняются отдельными кандидатами и также требуют двух разных исходников для активации.

Перенос строки внутри абзаца `.docx` сохраняется внутри одного блока. Новый абзац без собственной метки после распознанного закадра автоматически считается продолжением закадрового текста до следующей явной метки синхрона, ведущего или технического блока.

1. Анализ сценария:

```bash
scripts/analyze-script --in projects/my_project/input/scenario.docx --out projects/my_project/work/scenario_prepared.json
```

2. Синтез TTS:

```bash
scripts/synthesize-tts --in projects/my_project/work/scenario_prepared.json --out projects/my_project/speaker --gender female
```

Быстрая озвучка одной фразы (без прогона всего сценария):

```bash
scripts/synthesize-phrase --text "Тест дикторского голоса" --out projects/my_project/speaker --file phrase_preview --provider macos --gender female
```

## Ручная разметка диктора

Если в сценарии есть реплики спикеров, можно вручную пометить только закадровый текст.
При наличии меток анализатор берет только эти блоки.

Для `.docx` поддержан вариант "заливкой":
- выделяешь текст закадра цветом (Highlight/заливка текста в Word),
- или ставишь заливку на весь абзац,
- при запуске `analyze-script` будут взяты только эти помеченные блоки (LLM не обязателен).

Формат 1 (блочный):

```text
[[VO]]
Текст для диктора...
[[/VO]]
```

Формат 2 (префикс абзаца):

```text
ЗК: Текст для диктора...
```

Также поддержаны `VO:`, `NARRATOR:`, `ЗАКАДР:`.

Спикерские метки `СНХ:`, `ВЕДУЩИЙ:` и `СПИКЕР:` автоматически исключаются из дикторского текста (fallback-режим и LLM-инструкция).
Технические метки `Вот ссылка`, `ВИДЕО`, `ЛАЙФ`, `ВНИМАНИЕ` тоже автоматически исключаются.

3. Валидация и манифест:

```bash
scripts/validate-stage1 --in projects/my_project/work/scenario_prepared.json --speaker projects/my_project/speaker --manifest projects/my_project/speaker/manifest.json
```

## Dry run (без API-ключей)

```bash
scripts/analyze-script --in projects/my_project/input/scenario.docx --no-llm
scripts/synthesize-tts --in projects/my_project/work/scenario_prepared.json --out projects/my_project/speaker --dry-run
scripts/validate-stage1 --in projects/my_project/work/scenario_prepared.json --speaker projects/my_project/speaker
```

## API (Flask)

Запуск (`.env` загружается автоматически через `python-dotenv`):

```bash
export PYTHONPATH=src
flask --app api run --host 0.0.0.0 --port 8000
```

Все маршруты, кроме минимального `GET /health`, требуют токен. Браузер покажет
стандартный Basic Auth prompt: имя пользователя произвольное, пароль — значение
`ROUGH_CUT_API_TOKEN`. Для CLI используйте `Authorization: Bearer`:

```bash
curl -H "Authorization: Bearer $ROUGH_CUT_API_TOKEN" http://localhost:8000/stage1/status?project=my_project
```

Эндпоинты:
- `GET /` — стартовая страница: создать проект / выбрать проект.
- `GET|POST /projects` — список и создание проектов.
- `DELETE /projects/<project>` — удалить проект.
- `GET /project/<project>` — UI первого этапа для конкретного проекта.
- `GET /health`
- `GET /stage1/status?project=<project>`
- `POST /stage1/normalization/generate`
- `POST /stage1/normalization/reset`
- `GET|PUT /stage1/normalization`
- `POST /stage1/normalization-jobs/start`
- `GET /stage1/normalization-jobs/<job_id>`
- `GET /stage1/normalization-jobs/status?project=<project>`
- `POST /stage1/normalization/confirm/preview` — показать варианты обучения по ручным исправлениям
- `POST /stage1/normalization/confirm`
- `GET|POST /stage1/normalization/memory`
- `PATCH|DELETE /stage1/normalization/memory/<rule_id>`
- `POST /stage1/analyze`
- `POST /stage1/tts`
- `POST /stage1/tts-jobs/start` (асинхронный запуск TTS с live-прогрессом)
- `GET /stage1/tts-jobs/<job_id>` (получение статуса job)
- `GET /stage1/tts-jobs/status?project=<project>` (последняя задача, включая checkpoint после рестарта)
- `POST /stage1/tts-jobs/<job_id>/cancel` (остановка после текущего внешнего вызова)
- `POST /stage1/tts-phrase` (озвучка одной фразы)
- `POST /stage1/validate`
- `POST /stage2/build`
- `GET /stage2/export/xml?project=<project>`
- `GET|POST|DELETE /stage2/sync-sources`
- `GET|PUT /stage2/sync-sources/<source_id>/audio`
- `PUT /stage2/sync-sources/<source_id>/words`
- `POST /stage2/sync-jobs/start`
- `GET /stage2/sync-jobs/<job_id>`
- `GET /stage2/sync-jobs/status?project=<project>`
- `POST /stage2/sync-jobs/<job_id>/cancel` (сохраняет готовые индексы и совпадения)
- `GET|PATCH /stage2/sync-matches`
- `PATCH /stage2/sync-matches/<block_uid>`
- `GET /stage2/timeline?project=<project>`
- `POST /stage3/jobs/scan/start`
- `POST /stage3/jobs/prepare/start`
- `GET /stage3/jobs/<job_id>`
- `GET /stage3/jobs/status?project=<project>`
- `POST /stage3/media/scan` — синхронный fallback сканирования.
- `POST /stage3/timeline/prepare` — синхронный fallback подготовки.
- `GET /stage3/export/xml?project=<project>`
- `GET /stage4/inbox?project=<project>` — найденные сценарии и просмотровки
- `GET /stage4/revisions?project=<project>` — история и состояние целостности
- `POST /stage4/revisions/import` — создать редакцию из нового сценария
- `GET /stage4/diffs/<diff_id>?project=<project>` — получить сохранённый diff
- `POST /stage4/revisions/<revision_id>/activate` — открыть редакцию
- `POST /stage4/revisions/<revision_id>/reconcile` — восстановить рассогласованный проект

Последний статус TTS и sync атомарно сохраняется в каталоге `work` проекта.
После рестарта незавершённая задача показывается как `interrupted`: повторный
запуск TTS пропускает WAV с тем же fingerprint, а sync повторно использует
готовые Whisper-индексы и сохранённые совпадения, если fingerprint всех входов
не изменился. Завершённые in-memory статусы удаляются через 24 часа.

Ограничение: публичный API `faster-whisper` не экспортирует состояние decoder/VAD,
необходимое для точного продолжения внутри одного WAV. При остановке сохраняются
полностью готовые источники; текущий незавершённый аудиофайл распознаётся заново.
Резюм через `clip_timestamps` намеренно не используется, поскольку он меняет VAD
и контекст декодирования и может дать пропуски либо дубли на границе.

LLM-нормализация сохраняет успешные batch-решения в
`work/normalization_llm_cache.json`. Повторный запуск после сбоя запрашивает
только недостающие batch; изменение текста, правил, модели или prompt инвалидирует
соответствующий кеш.

## Stage 2.1: автоматическое подключение синхронов

Готовые синхронизированные секвенции Premiere являются источником координат.
Видео остаётся в Premiere и не загружается в backend.

Для headless-монтажа по отрендеренным просмотровкам Docker автоматически читает
`projects/<project>/media/reviews`, создаёт заменяемые `RC_SRC_*` sequences и
готовый nested XML. Premiere нужен только для импорта результата.

1. Установить development CEP-панель по инструкции в `cep/README.md`.
2. Открыть sync-секвенцию и нажать «Использовать активную».
3. Указать одного или нескольких спикеров через запятую и стабильный ASCII
   Source ID. Если секвенция общая, включить «все спикеры проекта».
4. Нажать «Экспортировать звук и запустить распознавание». Панель:
   - экспортирует полный WAV через Adobe Media Encoder;
   - регистрирует sequence ID/name;
   - ждёт готовности WAV;
   - запускает фоновое распознавание и сопоставление.
5. Во вкладке «Сборка» прослушать жёлтые совпадения, выбрать альтернативу или
   поправить `source in/out`, затем подтвердить.
6. Нажать Build Timeline и импортировать XML в Premiere.
7. Сделать Rough Cut активным и в CEP нажать
   «Применить подтверждённые СНХ».

Артефакты хранятся отдельно от подтверждённого сценария:

```text
projects/<project>/work/sync_sources.json
projects/<project>/work/sync_audio/<source_id>.wav
projects/<project>/work/transcripts/<source_id>.words.json
projects/<project>/work/sync_matches.json
projects/<project>/work/full_timeline.json
```

В автоматическую сборку идут только `auto_matched` и `confirmed`. Результаты
`needs_review` и `not_found` остаются плейсхолдерами. Последовательный экспорт
нескольких секвенций не перетирает предыдущие результаты: поиск выполняется по
общему пулу готовых word indexes.

Один и тот же участок исходного синхрона можно использовать в нескольких местах
монтажа — например, короткую часть ответа в анонсе, а полный ответ позднее.
Совпадения различаются по `block_uid`/`segment_id`, поэтому одинаковые и
пересекающиеся диапазоны `source in/out` намеренно не дедуплицируются.

Docker устанавливает `faster-whisper` и хранит модели в named volume
`whisper_cache`. При локальном запуске на Apple Silicon можно установить
`mlx-whisper`; адаптер выбирается автоматически.

Изолированный Whisper worker переиспользует загруженную модель между
последовательными заданиями и автоматически пересоздаётся после ошибки или
таймаута. Word index повторно используется только при совпадении сигнатуры WAV,
конфигурации, платформы и версий backend; новый индекс атомарно заменяет старый
только после успешного распознавания.

## Тесты

```bash
pip install -r requirements-dev.txt
pytest -q
node tests/cep_behavior_test.cjs
python -m compileall -q src tests
docker compose config --quiet
```

Каждый тест выполняется с глобальным таймаутом 120 секунд для предотвращения зависаний.

Те же проверки автоматически выполняются в GitHub Actions. Корректностные
правила Ruff запускаются командой `ruff check src tests`, а форматирование
блокирующе проверяется командой `ruff format --check src tests`.

`requirements-lock.txt` — полная фиксация версий прямых и транзитивных зависимостей (сгенерирован на Python 3.11 через `pip freeze` после установки `requirements-dev.txt` и `requirements-whisper.txt`). Источник истины для версий — `requirements*.txt`; lock-файл используется для воспроизводимости и диагностики, обновляйте его после любых изменений `requirements-*.txt`.

### pre-commit хуки

Установка один раз: `pip install pre-commit && pre-commit install`. Локально
хуки проверяют `ruff check`, `ruff format`, `gitleaks`, ESLint для CEP и
`tests/cep_behavior_test.cjs`, а также базовую гигиену файлов (trailing
whitespace, EOF, YAML/TOML/JSON).
`mypy` выполняется в CI, но не входит в локальные pre-commit хуки.

## Локальная автоматизация

```bash
scripts/setup-local                         # venv, зависимости, .env, диагностика
scripts/run-local                           # локальный API
scripts/doctor                              # зависимости, ключи, порты и connector
scripts/bootstrap-macos                     # setup + LaunchAgent + CEP на macOS
scripts/pipeline-stage1 --project Демо --input scenario.docx
```

`pipeline-stage1` сначала создаёт черновик нормализации и останавливается для
проверки. После проверки повторите команду с `--yes-confirm`; затем выполняются
confirm → TTS → validate. Для проверки без платного TTS добавьте `--dry-run`.

## Docker

Контейнер слушает `0.0.0.0:8000` — у приложения нет TLS, только токен
(`ROUGH_CUT_API_TOKEN`). Не публикуй порт 8000 напрямую в интернет: для
локальной работы обращайся к нему только с `127.0.0.1` этой же машины, а для
доступа снаружи ставь перед контейнером reverse proxy с TLS (nginx/Caddy и
т.п.).

### Быстрое развёртывание на другой машине

Linux, Windows/WSL2, Intel Mac и универсальный CPU-вариант для Apple Silicon:

```bash
git clone git@github.com:zabreiki-blip/rough_cut_v3.git
cd rough_cut_v3
scripts/bootstrap-docker
```

Скрипт проверяет Docker Compose v2, создаёт `.env` с новым локальным API token,
создаёт каталоги данных, собирает образ и ждёт успешный `/health`. Затем заполните
в `.env` ключи используемых LLM/TTS-провайдеров. Каталоги `projects/` и `data/`
примонтированы с хоста и переживают пересборку контейнера; кеш Whisper хранится
в named volume. Образ собирается под нативную архитектуру Docker host без
жёсткой привязки к `amd64` или `arm64`.

```bash
docker compose ps
docker compose logs --tail=100 stage1-api
```

Для установки панели на другом Mac:

```bash
scripts/install-cep-macos
```

После этого в CEP-настройках укажите каталог `projects` этой машины, URL backend
и новый `ROUGH_CUT_API_TOKEN` из её `.env`.

### Apple Silicon M1–M4: Metal/MLX

Docker Desktop работает внутри Linux VM и не передаёт Apple Metal контейнеру.
Для ускорения на процессорах M‑серии запускайте API нативно:

```bash
brew install python@3.12 ffmpeg catdoc
scripts/setup-macos-apple-silicon
# заполнить .env
scripts/run-native-macos
```

Установщик требует нативный `arm64` Python (не Rosetta), создаёт отдельное
окружение `.venv-macos-arm64`, ставит `mlx-whisper==0.4.3` и проверяет, что
`SYNC_WHISPER_BACKEND=auto` выбрал `mlx-whisper`. Модель `medium` автоматически
преобразуется адаптером в `mlx-community/whisper-medium-mlx` и загружается при
первом распознавании. Официальный MLX Whisper требует `ffmpeg`, поддерживает
word timestamps и установку через `pip install mlx-whisper`.

Обычный Docker-режим на M‑серии тоже поддерживается, но использует CPU
`faster-whisper`; он переносимее, однако медленнее нативного MLX.

### Ручной Docker-запуск

```bash
docker compose build
docker compose up -d
curl http://localhost:8000/health
```

Пример запроса:

```bash
curl -X POST http://localhost:8000/stage1/analyze \
  -H "Authorization: Bearer $ROUGH_CUT_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"project":"my_project","input_path":"projects/my_project/input/scenario.docx","use_llm":true}'
```

`/stage1/analyze` оставлен только для legacy CLI/API и помечен deprecated.
Новый веб-workflow запускает фоновую задачу через
`/stage1/normalization-jobs/start`; синхронный `/stage1/normalization/generate`
сохранён для совместимости и тестовых интеграций.

## Документация

- [`docs/README.md`](docs/README.md) — полный индекс актуальной документации и
  архивных отчётов.
- [`docs/STAGE1_GUIDE.md`](docs/STAGE1_GUIDE.md) — подробное руководство по
  работе со сценарием на Stage 1: разметка диктора, нормализация, TTS.
- [`docs/E2E_PILOT_CHECKLIST.md`](docs/E2E_PILOT_CHECKLIST.md) — пошаговый
  чек-лист для сквозного пилота в реальном Premiere Pro (сценарий → озвучка →
  синхронизация → conform → XML).
- [`docs/DEPENDENCY_POLICY.md`](docs/DEPENDENCY_POLICY.md) — политика
  зависимостей и поддерживаемых платформ (Docker, macOS Apple Silicon, CI).
- [`docs/archive/README.md`](docs/archive/README.md) — исторические аудиты,
  планы и исследования. Их выводы не являются текущим backlog.
