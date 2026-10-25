# Спецификация: достоверные статусы готовности Stage 3

Связано: план `docs/superpowers/plans/2026-10-24-common-project-remediation.md`, этап 3.

## Проблема (факты)

- `work/stage3_{kind}_job_status.json` хранит только последний запуск kind и не содержит `build_id`/редакции
  (`src/api_stage3_routes.py:711-720`, поля `:780-792`). Пример BRIK: файл показывал `failed / 350 блокеров`
  от устаревшего запуска, хотя реальных блокеров 11.
- `build_is_current` (`src/stage3/builds.py:288-310`) не попадает в JSON: только заголовок `X-RC-Build-Stale`
  в `/stage3/export/xml` (`:1015`) и 409 (`:1245`).
- `GET /stage3/builds` отдаёт `complete`, но не актуальность; `GET /stage3/plan` — `complete_ready`, `blockers`,
  но не связь с latest build.
- CEP-панель не читает Stage 3 вообще; draft/complete не различаются; списка блокеров нет.
- Stage 2 `/stage2/export/xml` выдаёт XML без проверки плана.

## Модель статуса

Три независимых измерения, никогда не сводятся в одно «готово»:

1. `processing` — состояние последней задачи: `idle|queued|running|completed|failed|interrupted`
   + `job_id`, `finished_at`, `build_id` (если задача создала сборку), `scenario_revision_id`.
2. `build` — последняя сборка: `build_id`, `mode` (`draft|complete`), `created_at`, `is_current` (bool),
   `stale_reasons` (список изменившихся входов: `normalization_revision`, `scenario_revision_id`,
   `sync_matches_fingerprint`, `timeline_sha256`).
3. `assembly` — текущая готовность по живому плану (без записи): `level`:
   - `no_timeline` — нет актуального таймлайна (с причиной, напр. `timeline_rebuild_required`);
   - `draft` — есть блокеры; `blocker_count`, `blockers_by_reason`, `blockers[]` (segment_id, reason, detail);
   - `complete_ready` — блокеров нет.
   `placeholders` (segments, duration_sec, by_reason) и `open_tasks` из `plan.summary`.

Правило: «Сборка без пропусков» показывается только при `build.mode == complete && build.is_current`.
Черновая сборка всегда подписана «Черновик: N пропусков». Результат задачи, чей `build_id != latest` или
чья редакция не совпадает с активной, помечается `superseded`/`stale`.

## API

- Новый `GET /stage3/status?project=` → `{project, active_revision_id, processing, build, assembly}`.
  Только чтение; `assembly` считается тем же кодом, что `_perform_plan_check`.
- Job: добавить в запись `build_id` и `scenario_revision_id` (из result при завершении); при чтении job,
  относящегося не к latest build, отдавать `superseded: true`.
- `GET /stage3/builds`: добавить `is_current` и `stale_reasons` для latest.
- `/stage2/export/xml`: вне scope этапа; зафиксировать в отчёте как известный обход (отдельная задача).

Обратная совместимость: существующие поля не удаляются и не меняют смысл.

## UI (CEP)

Блок «Сборка Stage 3»: три строки — обработка / последняя сборка (draft|complete, актуальна|устарела + причины) /
готовность (список блокеров по сегментам, сгруппированный по reason, с открытыми задачами).

## Тесты (TDD)

- status: нет таймлайна; таймлайн устарел; draft с блокерами; complete_ready; complete build актуален;
  build устарел после смены sync fingerprint / редакции; job failed старше latest → `superseded`.
- builds: `is_current`/`stale_reasons` для latest.
- регресс: существующие тесты `tests/test_stage3_api.py`, `tests/test_stage3_builds.py` зелёные.

## Вне scope

Изменение логики блокеров, Stage 2 export, редизайн панели.
