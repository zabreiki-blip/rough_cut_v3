# rough_cut_v3 — Stage 1

Этап 1: анализ сценария (`.doc/.docx`) -> подготовка блоков `zkN` -> синтез TTS -> манифест для сборки в Premiere.

## Структура

- `projects/<project>/input/` — исходный сценарий проекта.
- `projects/<project>/work/` — промежуточные артефакты (`scenario_raw.txt`, `scenario_prepared.json`).
- `projects/<project>/speaker/` — файлы озвучки `zk1.wav`, `zk2.wav`, ...
- `src/stage1/` — core-логика и CLI.
- `src/api.py` — Flask API.
- `scripts/` — shell-обертки для CLI.

## Локальные зависимости

Для обработки legacy `.doc` нужен `catdoc`.

- macOS (Homebrew): `brew install catdoc`
- Debian/Ubuntu: `sudo apt-get install -y catdoc`

Если `catdoc` не нужен, используй `.docx` или `.txt`.

## Переменные окружения

Обязательные ключи:
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
  - для коннектора macOS из Docker: `MACOS_CONNECTOR_URL`, `MACOS_CONNECTOR_TOKEN`, `MACOS_CONNECTOR_TIMEOUT`

### macOS Connector (для Docker backend)

Если API крутится в Docker, можно оставить `TTS_PROVIDER=macos` и вынести `say` в локальный сервис на Mac:

1. В `.env` для backend:

```bash
TTS_PROVIDER=macos
MACOS_CONNECTOR_URL=http://host.docker.internal:8001/synthesize
MACOS_CONNECTOR_TOKEN=change_me
```

2. На Mac (вне Docker) запусти коннектор:

```bash
scripts/run-macos-connector
```

3. Проверка:

```bash
curl -s http://localhost:8001/health
```

Примечание по безопасности: `http://host.docker.internal` нормален для локальной машины.
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
pip install -r requirements.txt
cp .env.example .env   # заполни API-ключи
export PYTHONPATH=src
```

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

Эндпоинты:
- `GET /` — стартовая страница: создать проект / выбрать проект.
- `GET /project/<project>` — UI первого этапа для конкретного проекта.
- `GET /health`
- `GET /stage1/status?project=<project>`
- `POST /stage1/analyze`
- `POST /stage1/tts`
- `POST /stage1/tts-jobs/start` (асинхронный запуск TTS с live-прогрессом)
- `GET /stage1/tts-jobs/<job_id>` (получение статуса job)
- `POST /stage1/tts-phrase` (озвучка одной фразы)
- `POST /stage1/validate`

Примечание: статусы `tts-jobs` хранятся в памяти процесса API.
После рестарта backend активные/исторические job-статусы сбрасываются.

## Docker

```bash
docker compose build
docker compose up -d
curl http://localhost:8000/health
```

Пример запроса:

```bash
curl -X POST http://localhost:8000/stage1/analyze \
  -H "Content-Type: application/json" \
  -d '{"project":"my_project","input_path":"projects/my_project/input/scenario.docx","use_llm":true}'
```
