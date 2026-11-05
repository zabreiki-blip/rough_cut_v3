# Разработка и CLI

[Начало работы](../README.md) · [API](API.md) · [Политика зависимостей](DEPENDENCY_POLICY.md)

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

## Локальный запуск (CLI)

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env   # настрой локальный токен и только нужные провайдеры
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

## Тесты

```bash
pip install -r requirements-dev.txt
pytest -q --cov=src --cov-report=term-missing
ruff check src tests
ruff format --check src tests
mypy src
node tests/cep_behavior_test.cjs
node tests/stage1_behavior_test.cjs
node tests/scroll_jump_test.cjs
python -m compileall -q src tests
docker compose config --quiet
```

Каждый тест выполняется с глобальным таймаутом 120 секунд для предотвращения зависаний.

Те же проверки автоматически выполняются в GitHub Actions. Корректностные
правила Ruff запускаются командой `ruff check src tests`, а форматирование
блокирующе проверяется командой `ruff format --check src tests`.

Зависимости зафиксированы воспроизводимыми lock-файлами с хэшами (uv, Python 3.11, `--universal`: linux x86_64/aarch64, macOS): `requirements-lock.txt` — runtime (его ставит Docker и CI `runtime-smoke`), `requirements-dev-lock.txt` — dev (CI `verify`). Источник истины для версий — `requirements*.txt`; после любого их изменения запустите `scripts/lock-deps` (или `scripts/lock-deps --upgrade`) и закоммитьте входные файлы вместе с lock-файлами. Подробности — в [DEPENDENCY_POLICY.md](DEPENDENCY_POLICY.md).

### pre-commit хуки

Установка один раз: `pip install pre-commit && pre-commit install`. Локально
хуки проверяют `ruff check`, `ruff format`, `gitleaks`, ESLint для CEP и
`tests/cep_behavior_test.cjs`, а также базовую гигиену файлов (trailing
whitespace, EOF, YAML/TOML/JSON).
`mypy` выполняется в CI, но не входит в локальные pre-commit хуки.

## Локальная автоматизация

```bash
PYTHON_BIN=python3.11 scripts/setup-local   # venv, зависимости, .env, диагностика
scripts/run-local                           # локальный API
scripts/doctor                              # зависимости, ключи, порты и connector
scripts/bootstrap-macos                     # setup + LaunchAgent + CEP на macOS
scripts/pipeline-stage1 --project Демо --input scenario.docx
```

`pipeline-stage1` сначала создаёт черновик нормализации и останавливается для
проверки. После проверки повторите команду с `--yes-confirm`; затем выполняются
confirm → TTS → validate. Для проверки без платного TTS добавьте `--dry-run`.

Для воспроизводимой обычной установки используйте setup/run из [README](../README.md).
Ручной CLI выше — отдельный путь для разработчика, он не устанавливает ASR
и не управляет историей редакций. Dry-run проверяет структуру, а не качество речи.

## Legacy DOC

Для старого `.doc` установите `catdoc`: macOS — `brew install catdoc`,
Debian/Ubuntu — `sudo apt-get install -y catdoc`. Можно использовать `antiword`;
для `.docx` и `.txt` эти конвертеры не нужны.
