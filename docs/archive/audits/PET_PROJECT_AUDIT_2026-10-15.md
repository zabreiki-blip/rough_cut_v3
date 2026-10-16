# Аудит pet-проекта rough_cut_v3 — 15 октября 2026

> Архивное снимение состояния на дату проверки.

Дата: 2026-10-15. Проверенное состояние: `HEAD=001a684`, рабочее дерево чистое
на начало аудита. Пилот в Premiere и платный smoke-тест не выполнялись: для них
нужен пользователь за Adobe и явное ручное действие.

## Итог

Проект в хорошем инженерном состоянии: 855 тестов проходят за ~16 с, `mypy`
чист на 58 модулях, `ruff check` чист, ESLint чист, поведенческие тесты CEP и
Stage 1 проходят, coverage 83.1% при пороге 79%. Найденные проблемы точечные.
Единственный блокер CI (падающий `ruff format --check`) исправлен в рамках
аудита; остальное — перенесённый backlog из аудита 2026-09-08 и гигиена.

## Таблица прогонов

| Проверка | Результат |
| --- | --- |
| `pytest -q --timeout=120` | 855 passed, 16.1 с |
| `pytest --cov=src` | TOTAL 83.1% (порог `fail_under=79`) |
| `ruff check src tests` | All checks passed |
| `ruff format --check src tests` | **Не проходило**: `tests/test_stage1_revision_speaker_dir.py` → исправлено в аудите, теперь чисто |
| `mypy src` | Success: no issues found in 58 source files |
| `npx eslint cep tests/cep_behavior_test.cjs` | Без ошибок |
| `node tests/cep_behavior_test.cjs` | 9 тестов passed |
| `node tests/stage1_behavior_test.cjs` | Без ошибок |
| `docker compose config --quiet` | Ок (`--env-file` из `.env.example`) |
| Сверка README ↔ код (49 эндпоинтов) | Расхождений в путях нет; см. находку 8 |
| Сверка `requirements*.txt` ↔ `requirements-lock.txt` | Дрейфа нет |
| Сверка env-переменных код ↔ `.env.example` ↔ README | Дрейфа нет |

## Находки

### Блокер CI (исправлен)

1. **`ruff format --check` падал на закоммиченном дереве** —
   `tests/test_stage1_revision_speaker_dir.py` не отформатирован под
   `ruff==0.16.6`, который же пропинен в `requirements-dev.txt` и pre-commit.
   Любой push этого HEAD уходил в красный CI. Исправлено одним
   `ruff format`; `pytest`/`mypy` после правки перезапущены и чисты.

### Высокий приоритет

2. **Уязвимая зависимость `requests==2.32.3`** (`requirements.txt`).
   В 2.32.4 исправлена CVE-2024-47081 (утечка учётных данных `.netrc` при
   редиректе на другой хост). Библиотека используется для всех платных
   исходящих вызовов (`src/stage1/openrouter_client.py`,
   `src/stage1/gemini_tts.py`, `src/stage2/sync_llm_review.py`,
   `src/api_core_routes.py`). Исправление: поднять пин до `>=2.32.4`,<3`,
   обновить `requirements-lock.txt`, прогнать тесты.
   Смежно: в проекте два HTTP-стека — `requests` и `httpx`
   (`src/stage2/cloud_asr.py`). Для pet-проекта это терпимо, но единый
   клиент сократил бы поверхность зависимостей.

3. **Финальная проверка не имеет отмены** (перенос P2 с 2026-09-08).
   `src/api_final_review_routes.py:195-206` регистрирует только
   `start`/`jobs/<id>`/`apply`; в `src/api_final_review_jobs.py` нет
   ни `cancel`-события, ни кооперативной остановки. Задача держит
   `active_projects` до завершения или рестарта процесса, а удаление
   проекта в этот период возвращает 409 (`src/api_core_routes.py:326-329`).
   Исправление: добавить `POST /stage2/final-review/jobs/<id>/cancel` по
   модели TTS/sync (`threading.Event`, статус `cancelled`, `release` в
   `finally`) и route/lifecycle-тест.

4. **Poll финальной проверки останавливается на сетевой ошибке** —
   `cep/js/main.js:1601-1621`. На `error` таймер не перевзводится и UI не
   переводится в retry-состояние; временный сбой сети оставляет панель в
   подвешенном виде. Рядом `pollExport` (`cep/js/main.js:1198-1208`)
   перевзводится с bounded attempts — привести к тому же паттерну с
   backoff и явной кнопкой «Повторить».

5. **`applyFinalReview` молча не работает после перезагрузки панели** —
   `cep/js/main.js:1782-1783`: ранний `return` при пустом
   `currentFinalReviewJobId` (память панели). Job может быть жив на
   backend, но кнопка не объясняет причину. Исправление: при отсутствии
   контекста запрашивать последний checkpoint job по проекту либо явно
   сообщать «контекст потерян, повторите финальную проверку».

### Средний приоритет

6. **Нет жёсткого лимита трат на платные вызовы.** Есть только учёт
   (`src/openrouter_usage.py`) и `SYNC_LLM_REVIEW_MAX_BLOCKS`
   (`src/api_sync_worker.py:434`, дефолт 30). Облачная финальная проверка
   уходит в `google/gemini-3.7-flash` (`cep/js/main.js:1628`) по WAV всей
   сборки, а облачный ASР синхрона тарифицируется по длине аудио —
   обе точки билляются без предварительного «бюджетного» гейта. Рекомендация:
   необязательный `ROUGH_CUT_MAX_USD_PER_JOB` (проверка ledger перед стартом
   и отказ 409 при превышении) плюс предупреждение в UI с оценкой стоимости.

7. **Мёртвый no-op `checkpoint_matches()`** — `src/api_sync_worker.py:129-131`
   определяет заглушку `return None`, которая ниже в той же функции
   затеняется реальной реализацией (`src/api_sync_worker.py:444`). Первое
   определение не исполняется никогда и вводит в заблуждение при чтении
   «сохраняются готовые совпадения» из README. Удалить.

8. **README не описывает эндпоинты финальной проверки.** В списке
   `docs/STAGE1_GUIDE.md:233-235` они есть, в `README.md` (раздел «API
   (Flask)») — нет. Добавить три строки: `/stage2/final-review/start`,
   `GET /stage2/final-review/jobs/<job_id>`,
   `POST /stage2/final-review/jobs/<job_id>/apply`.

9. **Дубль LLM-кэша в легаси-пути.** `_load_llm_cache` и соседние хелперы
   продублированы в `src/stage1/analyzer.py:687` и
   `src/stage1/normalizer.py:1983`. `stage1/analyzer.py` — помеченный
   deprecated путь (`src/api_stage1_routes.py:114`), но его всё ещё
   дёргают `scripts/analyze-script` и quick start README. Либо вынести общий
   модуль кэша, либо зафиксировать дату удаления легаси-пути.

10. **Одноразовые скрипты проекта в `scripts/`.** `finish_brik_normalization.py`,
    `finish_brik_tts.py`, `repair_brik_revision.py`, `repair_brik_louella_sync.py`
    — 868 строк зашитых под BRIK правок, вне CI и вне тестов, с `set -e`-
    несоблюдением в одном из них. Перенести в `docs/archive/` или удалить,
    чтобы `scripts/` остался эксплуатационным.

11. **Горячие точки сложности.** `run_sync_job()` — 668 строк и ~40 ветвлений
    (`src/api_sync_worker.py:46`), без прямого юнит-теста (покрыт только через
    API-тесты). Крупные файлы: `stage1/normalizer.py` 2598, `api.py` 1766,
    `stage2/cloud_asr.py` 1533, `stage1/gemini_tts.py` 1224,
    `stage4/versioning.py` 1178, `stage2/sync_matcher.py` 1168 строк.
    Рекомендация: дробить `run_sync_job` на шаги (prepare indexes → transcribe
    → match → review → persist) с юнит-тестами на каждый.

### Низкий приоритет / гигиена

12. **Порог coverage занижен.** `fail_under = 79.0` при фактических 83.1%.
    Поднять до 80 и следить за слабыми модулями:
    `stage1/cli.py` 67.8%, `stage1/openrouter_client.py` 62.4%,
    `api_stage3_routes.py` 73.2%, `api_stage1_routes.py` 74.2%,
    `stage1/gemini_tts.py` 76.1%, `stage1/macos_tts_backend.py` 72.7%,
    `api_stage2_routes.py` 78.1%.

13. **Нет автоматического обновления зависимостей.** В CI нет Dependabot и
    `pip-audit`; находка 2 появилась именно из-за ручного пина. Добавить
    `.github/dependabot.yml` (pip + github-actions) и шаг `pip-audit`.

14. **Кэш-мусор в рабочем дереве.** В корне и в `tests/__pycache__` лежат
    артефакты pytest/mypy/ruff и `.pyc` от Python 3.11/3.12/3.13. Всё
    покрыто `.gitignore`, но при локальных прогонах разных версий Python
    возможны ложные попадания старых `.pyc`. Достаточно периодической
    чистки; CI и целевая версия (3.11) согласованы, это не дефект.

## Что сделано хорошо (не ломать при правках)

- 855 тестов за 16 с с глобальным таймаутом 120 с на тест — быстрая обратная
  связь, редкий для такого объёма кода.
- `atomic_io.atomic_write_text`: tmp-файл + `fsync` + `os.replace` — состояние
  `projects/` не разрушается при падении (32 ГБ пользовательских данных).
- Аутентификация: fail-closed, `hmac.compare_digest`, Basic-челлендж только
  для прямой навигации из браузера (`src/api.py:306-332`).
- `RedactingFormatter` вычищает токены из логов и трейсбеков
  (`src/logging_config.py`).
- Пути: `resolve()` + `is_relative_to(project root)` для sync-audio и
  final-review (`src/api_final_review_routes.py:18-28`), валидация имени
  проекта регуляркой.
- Изолированный Whisper-воркер: idle-watchdog с адаптивным интервалом,
  пересоздание после ошибки, fork-защита (`src/stage2/whisper_process.py`).
- Один gunicorn worker — осознанно и задокументировано в `Dockerfile` и
  `docker-compose.yml` (in-process локи ревизий).
- Checkpoint/recovery: persisted TTS/sync job status переживает рестарт,
  word index переиспользуется по сигнатуре, revision recovery сохраняет
  копию перед восстановлением.
- Документация среды: `.env` никогда не коммитился, `.dockerignore` и
  `.gitignore` исключают `.env`, `projects/`, `data/`; lock-файл и
  `.env.example` согласованы с кодом.

## Практический порядок работ

1. (сделано) `ruff format` для падающего файла — CI снова зелёный.
2. Поднять `requests` до `>=2.32.4,<3`, обновить lock, прогнать тесты.
3. Добавить cancel для финальной проверки + route/lifecycle-тест (находка 3).
4. Починить poll/apply в CEP для финальной проверки (находки 4-5).
5. Удалить мёртвый `checkpoint_matches` (находка 7), дополнить README
   эндпоинтами (находка 8).
6. Ввести необязательный бюджетный гейт на платные вызовы (находка 6).
7. Начать дробление `run_sync_job` с юнит-тестами шагов (находка 11).
8. Поднять `fail_under` до 80, включить Dependabot/`pip-audit` (находки
   12-13), вычистить одноразовые BRIK-скрипты из `scripts/` (находка 10).
