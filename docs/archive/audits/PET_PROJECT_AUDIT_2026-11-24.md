# Технический аудит пет-проекта `rough_cut_v3` (2026-11-24)

- **Базовый коммит:** `c2a5726` + незакоммиченные изменения рабочей ветки (`translated_sync`, правки TTS и нормализации).
- **Объём кодовой базы:** ~48.4k строк Python (`src/`), ~9.2k строк JS/CSS (`src/static/`), >100 тестовых модулей (`tests/`).
- **Фокус аудита:** архитектура, надёжность, алгоритмика, производительность, фронтенд и поддерживаемость (DX).
- **Вне скоупа:** информационная безопасность (проект однопользовательский и работает локально).

---

## 1. Сильные стороны проекта

Для локального пет-проекта кодовая база демонстрирует очень высокую инженерную дисциплину:

1. **Надёжное файловое хранение (File-based ACID без СУБД для артефактов):**
   - Повсеместная атомарная запись через `src/atomic_io.py` (`tempfile.mkstemp` → `flush` → `os.fsync` → `os.replace`) гарантирует отсутствие битых JSON при внезапной остановке процесса.
   - В `src/stage4/versioning.py` реализован контроль целостности проекций (`_asset_projection_mismatches`) между активной ревизией сценария, `prepared.json`, манифестом озвучки и WAV-алиасами `zkN.wav`, а также защита подтверждённых классификаций при перегенерации (`_restore_exact_auto_classifications`).
   - Глобальная память нормализации (`src/stage1/normalization_memory.py`) на SQLite работает с `PRAGMA journal_mode = WAL`, `busy_timeout = 5000`, внешними ключами и идемпотентными двухфазными миграциями.

2. **Изоляция тяжёлого ML/ASR (`src/stage2/whisper_process.py`):**
   - Локальный ASR вынесен в отдельный `spawn`-процесс с двусторонним IPC-каналом (прогресс, бюджет, ошибки), жёстким таймаутом и кооперативной отменой.
   - Реализован фоновый `watchdog`-поток, автоматически выгружающий воркер из RAM/VRAM после простоя (`SYNC_WHISPER_IDLE_TIMEOUT_SECONDS`).
   - Учтены специфичные проблемы macOS: `OBJC_DISABLE_INITIALIZE_FORK_SAFETY`, отключение `_scproxy` в `src/api.py`, сброс состояния после `fork` (`os.register_at_fork`) и защита дескриптора `fd 0` (`_ensure_spawn_stdin`).

3. **Доменная алгоритмика и контроль расходов:**
   - Выравнивание таймкодов облачного ASR по локальному индексу (`src/stage2/word_retiming.py`) с фильтрацией дрейфа через скользящие медианы, Needleman-Wunsch на блоках замен и строгим quality gate (`quality_rejection`).
   - Последовательное выравнивание живой записи диктора (`src/voiceover/alignment.py`) с распознаванием дублей (`takes`), фальстартов и защитой от склейки одинаковых соседних реплик.
   - Учёт расходов OpenRouter (`src/cost_budget.py`, `src/openrouter_usage.py`) с предварительным резервированием бюджета перед каждой HTTP-попыткой и фоновой сверкой (`reconcile_pending`) по `X-Generation-Id`.

4. **Контрактное тестирование и воспроизводимость:**
   - Hashes-lock файлы (`requirements-lock.txt`, `requirements-dev-lock.txt`), порог покрытия `fail_under = 85.0`.
   - Быстрые UI-тесты на Node.js (`tests/stage1_behavior_test.cjs`, `tests/translated_sync_behavior_test.cjs`, `tests/scroll_jump_test.cjs`), проверяющие реальные ES-модули из `src/static/`, плюс полноценный браузерный smoke-стенд (`tests/first_user_browser_smoke.cjs`).

---

## 2. Поломки в текущем рабочем дереве (требуют починки перед коммитом)

При проверке текущего состояния репозитория (`ruff`, `mypy`, `pytest`, `node`) обнаружены 4 проблемы в незакоммиченных файлах:

| № | Проблема | Где обнаружено | Причина и решение |
|---|----------|----------------|-------------------|
| 1 | **Ошибка типизации `mypy`** | `src/stage1/gemini_tts.py:402` | `error: Argument 1 to "int" has incompatible type "Any \| None" [arg-type]`. На строке стоит `# type: ignore[attr-defined]`, который не покрывает `arg-type`. **Решение:** заменить `int(retry_after)` на `int(str(retry_after).strip())`. |
| 2 | **Падение `pytest` при сборе тестов** | `tests/test_translated_sync_matching.py:1-2`, `tests/test_translated_sync_transcript.py:4` | Используется импорт `from src.translated_sync...`, тогда как в `pyproject.toml` задан `pythonpath = ["src"]`. Без `PYTHONPATH=.` `pytest` падает с `ModuleNotFoundError: No module named 'src'`. **Решение:** заменить на `from translated_sync...`. |
| 3 | **Нарушение форматирования `ruff format`** | 30 файлов в `src/` и `tests/` | `ruff format --check src tests` возвращает код `1` (в новых файлах `translated_sync` и тестах используются однострочные конструкции через `;`). **Решение:** выполнить `.venv/bin/ruff format src tests`. |
| 4 | **Предупреждение Node.js `MODULE_TYPELESS_PACKAGE_JSON`** | `package.json` при запуске `node tests/*_test.cjs` | Node 24 выводит предупреждение о повторном парсинге ES-модулей `src/static/**/*.js`. Так как CJS-тесты уже имеют расширение `.cjs`, добавление `"type": "module"` в `package.json` уберёт предупреждение и накладные расходы. |

---

## 3. Архитектурные узкие места и технический долг

### 3.1. Конфликт таймаутов и чужой лок в `POST /stage2/translated-sync/match`
- **Файл:** `src/api_translated_sync_routes.py:65-200`, `src/static/stage1/core.js:225`.
- **Проблема 1 (Таймаут HTTP vs LLM):** При `semantic: true` эндпоинт синхронно в HTTP-потоке выполняет до 8 запросов к LLM (`review_candidates_with_llm`) с таймаутом `45.0` секунд на каждый (суммарно до 360 секунд), удерживая один из 4 потоков Gunicorn. При этом на фронтенде в `core.js` установлен `FETCH_TIMEOUT_MS = 30000` (30 секунд), который оборвёт запрос по `AbortController` раньше, чем завершится даже один медленный вызов LLM.
- **Проблема 2 (Чужой реестр задач):** На строке 86 для блокировки проекта во время семантического матчинга вызывается `services._reserve_tts_project(project)` (и `_release_tts_project` в `finally`). Из-за этого система считает, что в проекте запущена задача **«озвучки»**, а не сопоставления СНХ.
- **Рекомендация:** Передать увеличенный `timeoutMs` во фронтенд-вызове (или вынести семантический матчинг в фоновую задачу) и использовать лок реестра синхронов (`_SYNC_RUNTIME`), а не TTS.

### 3.2. Сверхкрупные модули (God-files)
Несколько ключевых модулей совмещают слишком много уровней ответственности:
- **`src/stage1/normalizer.py` (5 098 строк)** — парсинг форматов, регулярные выражения маркеров, эвристики склеек/таймкодов, промпты и вызовы LLM, управление ревизиями и историей правок. Кроме того, списки маркеров (`_BUILTIN_MARKERS`, `_TECH_MARKERS`) дублируются вручную с `src/stage1/normalization_memory.py:24-78`.
- **`src/stage2/cloud_asr.py` (2 735 строк)** — работа с `ffmpeg`/аудиочанками, HTTP-клиент, промптинг, парсинг ответов и постобработка.
- **`src/stage2/sync_matcher.py` (2 358 строк)** — токенизация, числительные, индексы и скоринг кандидатов.
- **`src/api.py` (2 179 строк)** — работает одновременно как точка входа Flask, реестр глобального состояния и фасад совместимости (`services=api`), через который роут-модули обращаются к приватным хелперам (`services._ensure_project`, `services._sync_stores`). В новых роутах (`src/api_translated_sync_routes.py`) объект `services` уже не аннотирован протоколом из `src/api_service_contracts.py`, а импорты спрятаны внутрь функций.

### 3.3. Дублирование фреймворка фоновых задач (5 реализаций)
Несмотря на выделение `src/job_runtime.py` (`JobRegistry`, `trim_terminal_jobs`), обвязка создания, сохранения на диск (`*_job.json`), восстановления статуса `interrupted` и отмены задач написана отдельно для:
1. TTS (`src/api_stage1_jobs.py`)
2. Нормализации (`src/api_stage1_jobs.py`)
3. Sync (`src/api.py` + `src/api_sync_worker.py`)
4. Final Review (`src/api_final_review_jobs.py`)
5. Voiceover (`src/api_voiceover_jobs.py`)
6. Stage 3 scan/prepare (`src/api_stage3_routes.py`)

Из-за этого отличается политика обработки ошибок записи на диск (best-effort против исключения) и поддержка отмены (у нормализации и Stage 3 нет `cancel`).

### 3.4. Три независимые реализации нормализации русских числительных
Логика сопоставления чисел цифрами и словами (`1945` ↔ `тысяча девятьсот сорок пятый`, `27-го` ↔ `двадцать седьмого`) написана трижды с разными словарями и правилами:
1. `src/stage2/sync_matcher.py` (`_NUMBER_UNITS`, `_NUMBER_TEENS`, `_NUMBER_TENS`, `_NUMBER_HUNDREDS`, `_NUMBER_SCALES`)
2. `src/voiceover/alignment.py` (`_merge_numbers` — переиспользует словари из `sync_matcher`, но содержит собственный автомат разрядов и `_ORDINAL_SUFFIXES`)
3. `src/stage2/word_retiming.py` (`_NUM_EXACT`, `_NUM_STEMS`, `_ORD_ENDINGS`, `_DIGIT_SUFFIXES` — отдельная реализация, сворачивающая числительные в токен `#num`)

Выделение единого модуля `src/text_ru.py` упростит поддержку и даст одинаковое качество матчинга во всех стадиях пайплайна.

### 3.5. Фронтенд и статический анализ JS (`src/static/`)
- Крупные модули `src/static/stage1/normalization.js` (1 245 строк), `sync.js` (1 158 строк) и `revisions.js` (1 097 строк) активно строят интерфейс через конкатенацию `innerHTML`.
- В `src/static/stage1/state.js` ~220 DOM-узлов кэшируются один раз при старте (`document.getElementById`). Это требует осторожности: любая перерисовка родительского контейнера через `innerHTML` сделает закэшированную ссылку в `ids.*` неактуальной.
- В `package.json` (`npm run lint:cep`) ESLint проверяет только замороженный каталог `cep/`, а рабочий фронтенд `src/static/` линтером не проверяется (пункт `D29` в `docs/TECH_DEBT.md`).

---

## 4. Актуализация реестра `docs/TECH_DEBT.md`

- **Пункт `D32` (медленный `test_large_input_is_fast` в `tests/test_stage2_word_retiming.py` — 10–12 с вместо 2 с):**
  При локальном прогоне на текущем окружении тест стабильно проходит за **0.44 с** (при пороге `2.0` с). Пункт можно перепроверить на полном прогоне и закрыть.

---

## 5. План рекомендуемых действий (по приоритету)

### Шаг 1. Быстрые исправления текущей ветки (5–10 минут)
1. Исправить `int(str(retry_after).strip())` в `src/stage1/gemini_tts.py:402`.
2. Убрать префикс `src.` из импортов в `tests/test_translated_sync_matching.py` и `tests/test_translated_sync_transcript.py`.
3. Добавить `"type": "module"` в `package.json`.
4. Отформатировать код: `.venv/bin/ruff format src tests`.

### Шаг 2. Точечные улучшения надёжности
1. В `src/api_translated_sync_routes.py`: заменить `_reserve_tts_project` / `_release_tts_project` на резервирование в реестре синхронов и согласовать фронтенд-таймаут для `semantic: true`.
2. Добавить протокол `TranslatedSyncRoutesServices` в `src/api_service_contracts.py` и вынести внутрифункционные импорты в начало `src/api_translated_sync_routes.py`.
3. Включить `src/static/` в конфигурацию ESLint (`sourceType: "module"`, browser globals).

### Шаг 3. Среднесрочный рефакторинг (по мере доработки модулей)
1. Вынести общие маркеры сценария (`_BUILTIN_MARKERS`, `_TECH_MARKERS`) из `normalizer.py` и `normalization_memory.py` в единый модуль `src/stage1/markers.py`.
2. Объединить нормализацию русских числительных из `sync_matcher.py`, `voiceover/alignment.py` и `word_retiming.py` в один общий модуль.
3. Унифицировать персистентность и жизненный цикл фоновых задач поверх `JobRegistry` в `src/job_runtime.py`.
