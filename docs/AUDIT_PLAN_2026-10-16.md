# Сводный план аудита rough_cut_v3 — 2026-10-16

> Исторический план на базе `c626cbb`. Состояние текущего кода и открытые
> задачи см. в [`GENERAL_AUDIT_2026-10-21.md`](GENERAL_AUDIT_2026-10-21.md)
> и [`TECH_DEBT.md`](TECH_DEBT.md); таблицы ниже не являются текущим backlog.

Источники: `.kilo/plans/1792150519294-pet-project-audit-plan.md` (статический аудит, находки B1–B18),
`AUDIT.md` (фактический прогон гейтов), ревью оркестратора (поправки ниже).
База: `HEAD=c626cbb`, ветка `fix/audit-2026-10-16`.

## Базовая линия (из AUDIT.md, прогон 2026-10-16)

pytest 1056 passed (~21 s); покрытие 83.7% (порог 79.0); ruff check/format, mypy (59 файлов),
eslint — чисто; `cep_behavior_test.cjs`, `stage1_behavior_test.cjs` (9) — зелёные; `pip check` OK.
Модули <80%: `stage1/openrouter_client.py` 62.4, `stage1/cli.py` 67.8, `stage1/macos_tts_backend.py` 72.7,
`macos_connector.py` 74.8, `stage1/analyzer.py` 74.9, `stage1/gemini_tts.py` 76.1,
`stage2/whisper_process.py` 76.1, `stage1/validator.py` 77.3.

## Отклонённые выводы AUDIT.md

- «gunicorn 22.0.0 ✅», «зависимости актуальны» — неверно (CVE-2024-1135 и др.), см. B3.
- «98 дублирующихся функций» (`to_dict`, `__init__`, …) — не дублирование; миксин не нужен.
- Разбиение normalizer/api/cloud_asr/host.jsx как «критичное» — отложено (см. Отложено).
- Кэш линтеров в CI, pre-commit на размер файла — низкая ценность, не берём.

## Поправки к плану .kilo

- **B4**: `ast_serialize`, `librt` (← mypy 2.x) и `python-discovery` (← virtualenv) — законные
  транзитивные dev-зависимости, не мусор. Реальная проблема: один lock смешивает runtime и dev,
  Docker ставит диапазоны. Решение — runtime-lock отдельно от dev-lock (pip-tools/uv compile).
- **B2**: без эвристики «мс на символ» (ложные отказы на коротких фразах/темпе).
- **B3**: сначала минимально патченная 23.x, затем отдельным шагом последняя мажорная.
- **B5**: итерация 1 — `max_tokens` + резка oversized-блоков; бюджетный резерв — итерация 2.
- **B10**: вместо теста «маршрут упомянут в README» — генерируемый список маршрутов.
- **B11/B12/B17**: только после пилота F1–F4 и при реальной необходимости.

## Порядок работ

Каждый пункт — TDD (падающий тест → правка), отдельный коммит, полный набор гейтов.

| # | ID | Pri | Задача | Готово, когда |
|---|----|-----|--------|---------------|
| 1 | B1 | P1 | При `retry_without_style` публиковать WAV под fingerprint с `style=""`, помечать элемент `degraded: true`; то же в `synthesize_phrase` | Тесты: деградированный WAV не лежит под styled-fp; повторный styled-запуск не кэш-хит; аналог для `synthesize_phrase` |
| 1 | B2 | P1 | `_is_valid_wav`: `nframes>0`, `framerate>0`, объявленный data-chunk не больше файла | Тесты: header-only, усечённый, валидный тихий; `_write_silence_wav` проходит; битое не попадает в кэш |
| 1 | — | P1 | Инструкция/скрипт инвалидации существующих деградированных/битых ассетов | Описано в docs, dry-run по умолчанию |
| 2 | B3 | P1 | gunicorn → 23.x, затем → актуальная; тест пина ≥23.0.0 в requirements и lock | Гейты, `docker build`, `gunicorn --version` |
| 3 | B5.1 | P1 | `max_tokens` в OpenRouter-payload; резка oversized-блока в `_llm_batches` | Тесты: блок > max_chars → ≥2 батча; payload содержит max_tokens |
| 4 | B6 | P2 | Удалить заглушку `checkpoint_matches` (`api_sync_worker.py:182`) | Гейты |
| 4 | B7 | P2 | TTL 24ч + cap 200 для job-реестра финальной проверки | Тест вытеснения terminal и сохранения активной |
| 5 | B15 | P3 | Dependabot (pip, actions), `pip-audit` в CI, `concurrency`, `setup-node` | CI-конфиг валиден |
| 6 | B4′ | P1 | Раздельные runtime/dev lock, Docker из runtime-lock; тест согласованности пинов | Лок воспроизводим, тест зелёный |
| 7 | B5.2 | P2 | Бюджетный резерв для LLM/TTS через `cost_budget.py`, дефолт `limit=0` | Тест: при превышении `requests.post` не вызван |
| 8 | B8 | P2 | `ROUGH_CUT_MAX_ASR_JOBS` семафор | Тест ожидания/отмены |
| 8 | B9 | P2 | Лимиты ресурсов и uid в compose | Тест контракта |
| 9 | B10 | P2 | README: маршруты (генерация) + `MACOS_TTS_*`; архив `NORMALIZATION_GLOBAL_CHECK` | — |
| 10 | B14 | P3 | Тесты слабых модулей, затем `fail_under` → 82 | Покрытие ≥82 |
| 10 | B16, B18 | P3 | BRIK-скрипты в архив, явный COPY в Dockerfile; clean-pyc и политика версий Python | — |

## Статус (2026-10-17)

Выполнено на ветке `fix/audit-2026-10-16`: B1, B2, B3, B4′, B5.1, B6 (заглушка оказалась нужной —
оставлена и задокументирована), B7, B8, B9, B10, B14, B15, B16, B18; дополнительно закрыты CVE
Flask/requests/Werkzeug/urllib3/pytest. Не выполнено: B5.2 (бюджетный резерв LLM/TTS) и отложенное
ниже. Остаток и непроверенное — в [`TECH_DEBT.md`](TECH_DEBT.md).

## Отложено (после пилота F1–F4)

B11 (декомпозиция `run_sync_job`), B12 (JobService), B13 (бенчмарк финальной проверки),
B17 и host.jsx (модули CEP), разбиение normalizer/api/cloud_asr.

## Гейты

```bash
pytest -q --timeout=120
pytest -q --cov=src --cov-report=term-missing --timeout=120
ruff check src tests && ruff format --check src tests
mypy src
node tests/cep_behavior_test.cjs && node tests/stage1_behavior_test.cjs
npm run lint:cep
docker compose config --quiet
```

Ручные (не автоматизируются): платный TTS-smoke, пилот F1–F4 в Premiere.
