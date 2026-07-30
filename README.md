# rough_cut_v3 — Stage 1

Пайплайн: нормализация сценария (`.doc/.docx`) → подтверждение блоков →
синтез TTS → Whisper-сопоставление СНХ → сборка в Premiere.

## Структура

- `projects/<project>/input/` — исходный сценарий проекта.
- `projects/<project>/work/` — canonical `scenario_normalized.json` и производные `scenario_raw.txt`/`scenario_prepared.json`.
- `projects/<project>/speaker/` — файлы озвучки `zk1.wav`, `zk2.wav`, ...
- `src/stage1/` — core-логика и CLI.
- `src/stage2/` — таймлайн, sync-источники, Whisper word index и fuzzy matching.
- `cep/` — панель Premiere для регистрации, экспорта аудиогида и conform.
- `src/api.py` — Flask API.
- `scripts/` — shell-обертки для CLI.

## Локальные зависимости

Для обработки legacy `.doc` нужен `catdoc`.

- macOS (Homebrew): `brew install catdoc`
- Debian/Ubuntu: `sudo apt-get install -y catdoc`

Если `catdoc` не нужен, используй `.docx` или `.txt`.

## Переменные окружения

Обязательные ключи:
- `ROUGH_CUT_API_TOKEN` — обязательный токен веб-интерфейса и API
- `OPENROUTER_API_KEY`
- `GEMINI_API_KEY`

Если используешь только `TTS_PROVIDER=macos`, ключи для TTS не нужны.

Важно для `macos`: backend должен работать локально на macOS, либо через коннектор (ниже).  
В Docker/Linux команда `say` недоступна напрямую.

Опционально:
- `MAX_ZK_BLOCKS=40` (верхняя граница количества блоков `zkN` после анализа; по умолчанию `40`)
- `VALIDATE_MIN_WAV_DURATION_MS=350` (warning, если WAV короче порога; `0` отключает)
- `VALIDATE_SILENCE_RMS_THRESHOLD=120` (warning, если RMS слишком низкий; `0` отключает)
- `TTS_PROVIDER=gemini|openrouter|macos` (какой движок синтеза использовать)
- `TTS_GENDER=female|male` (пол голоса по умолчанию)
- для `openrouter`: `OPENROUTER_TTS_MODEL`, `OPENROUTER_TTS_VOICE`, `OPENROUTER_TTS_SAMPLE_RATE`
  - можно задать пресеты: `OPENROUTER_TTS_VOICE_FEMALE`, `OPENROUTER_TTS_VOICE_MALE`
  - и для Gemini: `GEMINI_TTS_VOICE_FEMALE`, `GEMINI_TTS_VOICE_MALE`
- для macOS (бесплатно, локально): `MACOS_TTS_VOICE_FEMALE`, `MACOS_TTS_VOICE_MALE`, `MACOS_TTS_RATE` (по умолчанию `Milena/Yuri`)
  - для коннектора macOS: обязательный `MACOS_CONNECTOR_TOKEN`, а также `MACOS_CONNECTOR_URL`, `MACOS_CONNECTOR_TIMEOUT`, `MACOS_CONNECTOR_BIND`
- для Stage 2 sync: `SYNC_WHISPER_BACKEND=auto|mlx-whisper|faster-whisper|openai-whisper`,
  `SYNC_WHISPER_MODEL`, `SYNC_WHISPER_DEVICE`, `SYNC_WHISPER_COMPUTE_TYPE`;
  одна транскрипция выполняется в отдельном процессе с hard timeout
  `SYNC_WHISPER_TIMEOUT_SECONDS` (по умолчанию 14400 секунд)
- `SYNC_AUDIO_MAX_BYTES` — потоковый лимит фактически прочитанных байт WAV
  (по умолчанию 4 ГБ), работающий и без `Content-Length`
- `ROUGH_CUT_CEP_CORS=1` — разрешить аутентифицированные запросы только из
  локального Premiere CEP file origin

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
python -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env   # заполни API-ключи
export PYTHONPATH=src
```

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
- `GET /project/<project>` — UI первого этапа для конкретного проекта.
- `GET /health`
- `GET /stage1/status?project=<project>`
- `POST /stage1/normalization/generate`
- `GET|PUT /stage1/normalization`
- `POST /stage1/normalization/confirm/preview` — показать варианты обучения по ручным исправлениям
- `POST /stage1/normalization/confirm`
- `GET|POST /stage1/normalization/memory`
- `PATCH|DELETE /stage1/normalization/memory/<rule_id>`
- `POST /stage1/analyze`
- `POST /stage1/tts`
- `POST /stage1/tts-jobs/start` (асинхронный запуск TTS с live-прогрессом)
- `GET /stage1/tts-jobs/<job_id>` (получение статуса job)
- `POST /stage1/tts-phrase` (озвучка одной фразы)
- `POST /stage1/validate`
- `POST /stage2/build`
- `GET /stage2/export/xml?project=<project>`
- `GET|POST|DELETE /stage2/sync-sources`
- `GET|PUT /stage2/sync-sources/<source_id>/audio`
- `PUT /stage2/sync-sources/<source_id>/words`
- `POST /stage2/sync-jobs/start`
- `GET /stage2/sync-jobs/<job_id>`
- `GET|PATCH /stage2/sync-matches`
- `PATCH /stage2/sync-matches/<block_uid>`
- `GET /stage2/timeline?project=<project>`

Примечание: статусы `tts-jobs` хранятся в памяти одного процесса API; Docker
явно запускает один worker. Завершённые статусы удаляются через 24 часа.
После рестарта backend активные/исторические job-статусы сбрасываются.

## Stage 2.1: автоматическое подключение синхронов

Готовые синхронизированные секвенции Premiere являются источником координат.
Видео остаётся в Premiere и не загружается в backend.

1. Установить development CEP-панель по инструкции в `cep/README.md`.
2. Открыть sync-секвенцию и нажать «Считать активную».
3. Указать одного или нескольких спикеров через запятую и стабильный ASCII
   Source ID. Если секвенция общая, включить «все спикеры проекта».
4. Нажать «Экспортировать звук → Whisper». Панель:
   - экспортирует полный WAV через Adobe Media Encoder;
   - регистрирует sequence ID/name;
   - ждёт готовности WAV;
   - запускает фоновое распознавание и сопоставление.
5. Во вкладке «Сборка» прослушать жёлтые совпадения, выбрать альтернативу или
   поправить `source in/out`, затем подтвердить.
6. Нажать Build Timeline и импортировать XML в Premiere.
7. Сделать Rough Cut активным и в CEP нажать
   «Подтянуть все подтверждённые СНХ».

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

Docker устанавливает `faster-whisper` и хранит модели в named volume
`whisper_cache`. При локальном запуске на Apple Silicon можно установить
`mlx-whisper`; адаптер выбирается автоматически.

## Тесты

```bash
pip install -r requirements-dev.txt
PYTHONPATH=src pytest -q
```

## Docker

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
Новый workflow начинается с `/stage1/normalization/generate`.
