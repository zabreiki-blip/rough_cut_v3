# Настройки приложения

[Начало работы](../README.md) · [API](API.md) · [Развёртывание](DEPLOYMENT.md)

## Переменные окружения

Обязательный ключ:
- `ROUGH_CUT_API_TOKEN` — пароль Basic Auth для веб-интерфейса и API; в браузере
  имя пользователя произвольное. Это доступ к локальному приложению, не ключ
  провайдера.

Платный ключ провайдера необязателен: `OPENROUTER_API_KEY` нужен только для
функций OpenRouter (например, LLM-анализа и облачного TTS). При
`TTS_PROVIDER=macos` локальный синтез не требует API-ключа. Установщик `scripts/setup-local` устанавливает локальный ASR: faster-whisper
для CPU или mlx-whisper на нативном Apple Silicon.

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
- `OPENROUTER_MODEL` (по умолчанию `openai/gpt-4o-mini`) — модель анализа и
  нормализации; `OPENROUTER_BASE_URL` (по умолчанию `https://openrouter.ai/api/v1`) —
  базовый URL OpenRouter, общий для чата, TTS и облачного ASR
- `FFMPEG_TIMEOUT_SECONDS=180` — таймаут подгонки темпа Gemini TTS через ffmpeg (30–3600 с)
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
  - `MACOS_TTS_VOICE` — голос по умолчанию (`Milena`), если не задан голос по полу;
    `MACOS_TTS_SAMPLE_RATE` — частота WAV в Гц (по умолчанию `24000`, диапазон `8000–192000`)
  - для коннектора macOS: обязательный `MACOS_CONNECTOR_TOKEN`, а также `MACOS_CONNECTOR_URL`, `MACOS_CONNECTOR_TIMEOUT`, `MACOS_CONNECTOR_BIND`, `MACOS_CONNECTOR_PORT`;
    LaunchAgent автоматически задаёт `MACOS_CONNECTOR_ENV_FILE` как путь к
    отдельному файлу настроек с правами `0600`, поэтому эту переменную не нужно
    добавлять в проектный `.env`
    - `MACOS_COMMAND_TIMEOUT` — таймаут одной команды `say`/конвертации на стороне
      коннектора в секундах (по умолчанию `900`);
      `MACOS_CONNECTOR_MAX_REQUEST_BODY_BYTES` — лимит JSON-запроса к коннектору
      (по умолчанию 256 КиБ)
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
- `SYNC_CLOUD_ASR_API_KEY` — отдельный ключ облачного ASR (`SYNC_WHISPER_BACKEND=cloud-audio`);
  если пуст, используется `OPENROUTER_API_KEY`
- `ROUGH_CUT_MAX_REQUEST_BODY_BYTES` — потолок тела запроса Flask; автоматически
  поднимается выше `SYNC_AUDIO_MAX_BYTES` на размер multipart-заголовков
- `ROUGH_CUT_BIND` (по умолчанию `127.0.0.1`) и `ROUGH_CUT_PORT` (`8000`) — адрес
  публикации порта Docker-контейнера на host
- `SYNC_WHISPER_DOCKER_DEVICE` (`cpu`) и `SYNC_WHISPER_DOCKER_COMPUTE_TYPE` (`int8`) —
  значения `SYNC_WHISPER_DEVICE`/`SYNC_WHISPER_COMPUTE_TYPE` внутри контейнера
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
- `SYNC_OFFSET_CLUSTER_ENABLED=1` включает автопринятие сильных глобальных
  совпадений (`fallback_full`), если не менее `SYNC_OFFSET_CLUSTER_MIN_SUPPORT`
  (по умолчанию `3`) других таких совпадений того же источника имеют то же смещение
  «найдено − таймкод сценария» (шаг связи `SYNC_OFFSET_CLUSTER_LINK_SEC`, по
  умолчанию `60` с): систематический сдвиг таймкодов участка файла. Подтверждённые
  и вручную правленые совпадения считаются опорами (по текущему `source_in`).
  `SYNC_OFFSET_CLUSTER_MAX_WIDTH_SEC` (по умолчанию `240`) — кластер шире этого
  значения не считается одним сдвигом; `SYNC_OFFSET_CLUSTER_MIN_WORDS` (по умолчанию
  `5`) — более короткие цитаты не повышаются и не служат опорой. `0` в
  `SYNC_OFFSET_CLUSTER_ENABLED` отключает проход и возвращает ранее повышенные
  совпадения в `needs_review`.
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

## Профиль локальной установки

`PYTHON_BIN=python3.11 ./scripts/setup-local` выбирает Python явно: системный
`python3` может оказаться неподдерживаемым (например, 3.9). CPU требует 3.11;
нативный macOS arm64 с MLX поддерживает 3.11 и 3.12. `ROUGH_CUT_PROFILE=auto`
выбирает MLX на нативном Apple Silicon (`.venv-macos-arm64`), CPU на остальных
системах и под Rosetta (`.venv`). `ROUGH_CUT_PROFILE=cpu|mlx` задаёт профиль явно.
`ROUGH_CUT_VENV` переопределяет путь окружения и сохраняется: используйте одинаковые
переопределения для setup, run и doctor. MLX под Rosetta не поддерживается.

`HF_HOME` задаёт кэш моделей (локально по умолчанию `.cache/huggingface`).
Whisper скачивает модель при первом распознавании: требуется сеть, для medium
примерно 1.5 ГБ и дополнительное свободное место. Время холодного старта зависит
от сети, модели и компьютера. `--dry-run` создаёт WAV-заглушки без речи.
