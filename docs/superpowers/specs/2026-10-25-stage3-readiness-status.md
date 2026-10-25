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
   `stale_reasons` (только изменившиеся входы: `normalization_revision`, `scenario_revision_id`,
   `sync_matches_fingerprint`, `timeline_sha256`; `build_missing`, если build.json пропал) и
   `live_timeline_unavailable_reason` (почему входы не с чем сравнить: `timeline_missing`,
   `timeline_rebuild_required`, `timeline_invalid`, ...; иначе `null`). При недоступном живом таймлайне
   `is_current=false`, а `stale_reasons` пуст: причина не маскируется под изменившийся вход.
3. `assembly` — текущая готовность по живому плану (без записи): `level`:
   - `no_timeline` — нет актуального таймлайна; `reason` + `message` (`timeline_missing`,
     `timeline_rebuild_required`, `normalization_confirmation_required`, `timeline_invalid` — файл не
     читается/не JSON-объект);
   - `error` — таймлайн актуален, но построение плана упало: `reason="plan_failed"`, `message`.
     `processing` и `build` при этом возвращаются как обычно (HTTP 200);
   - `draft` — есть блокеры; `blocker_count`, `blockers_by_reason`, `blockers[]` (segment_id, reason, detail);
   - `complete_ready` — блокеров нет.
   `placeholders` (segments, duration_sec, by_reason) и `open_tasks` из `plan.summary`.
   Каталог медиа берётся только из кэша ffprobe (`work/stage3_ffprobe_cache.json`): статус не запускает
   ffprobe и ничего не пишет. Файлы без записи в кэше не попадают в каталог; это отражают
   `catalog_incomplete` и `uncached_media_files` (план по неполному каталогу может показывать лишние блокеры).

Правило: «Сборка без пропусков» показывается только при `build.mode == complete && build.is_current`.
Черновая сборка всегда подписана «Черновик: N пропусков». Результат задачи, чей `build_id != latest` или
чья редакция не совпадает с активной, помечается `superseded`/`stale`.

## API

- Новый `GET /stage3/status?project=` → `{project, active_revision_id, processing, build, assembly}`.
  Строго только чтение и дёшево: без `mkdir` (несуществующий проект → 404), без ffprobe, без записи кэша,
  без восстановления прерванной активации редакции (`_confirmed_normalization_read_only`);
  `assembly` считается тем же `_compute_plan`, что и `_perform_plan_check`.
- Job: `scenario_revision_id` и `timeline_sha256` пишутся в запись при СТАРТЕ prepare-задачи (до результата,
  поэтому есть и у failed/interrupted); `build_id` — из result при завершении. При чтении `superseded: true`,
  если задача относится не к latest build, либо её редакция (`stale_revision`) или таймлайн
  (`stale_timeline`) не совпадают с текущими. Задача без сборки при тех же редакции и таймлайне не superseded.
  Для `interrupted` время сравнения с latest build — `started_at` (одинаково на всех путях чтения).
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
