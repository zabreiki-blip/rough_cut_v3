# Дельта-аудит rough_cut_v3

> Архивный снимок состояния на дату проверки.

Дата: 2026-09-08. Проверенное состояние: `HEAD=4fedd902fbce3742ca8aecd7fdffeff728169190` плюс незафиксированный рабочий дифф. Рабочее дерево было грязным до начала аудита; изменения не откатывались и не коммитились.

Аудит подтвердил исправления вчерашних пунктов 1–9 по тестам, статическому анализу и изолированным сценариям. Одновременно новый код «Финальная проверка» имеет несколько незакрытых переходов жизненного цикла. Пилот в Premiere и платный smoke-тест не выполнялись: для них требуется пользователь за Adobe и явное ручное действие.

## Состояние дерева

По `git status --short` до работы: 27 изменённых tracked-файлов и 4 untracked-файла. Untracked: `.kilo/`, `docs/PET_PROJECT_AUDIT_2026-09-07.md`, `src/api_final_review_routes.py`, `src/stage2/final_review.py`, `tests/test_stage2_final_review.py`.

До добавления этого отчёта исходный `git diff --stat` показывал 27 файлов, `700 insertions(+), 133 deletions(-)`. В рабочем дереве также находится новый чек-лист и этот отчёт.

Новый непроверявшийся ранее код: `src/stage2/final_review.py`, `src/api_final_review_routes.py`, обвязка final-review в `src/api.py`, UI final review в `cep/js/main.js` и host-команды в `cep/host.jsx`, а также миграция OpenRouter TTS и изменения диапазонов синхронизации.

## Таблица прогонов

| Проверка | Результат |
| --- | --- |
| `pytest -q --cov=src --cov-report=term --durations=10` | Не запустилась: установленный pytest не знает `--cov` (нет `pytest-cov`) |
| `python3 -m pytest -q --durations=10` | 730 passed, 1 failed; нестабильный `tests/test_tts_progress_contract.py::test_cancellation_progress_schema_and_order` |
| Повтор целевого failed-теста | Успешно, 1 passed; полный результат требует повторной проверки в полноценном окружении |
| `ruff check src tests` | Не выполнено: команда и модуль `ruff` отсутствуют |
| `mypy src` | Не выполнено: команда и модуль `mypy` отсутствуют |
| `node tests/cep_behavior_test.cjs` | Успешно, 7 тестов |
| `node tests/stage1_behavior_test.cjs` | Успешно, без вывода об ошибках |
| `npm run lint:cep` | Успешно |
| `python -m compileall -q src tests` | Команда `python` отсутствует; `python3 -m compileall -q src tests` успешно |
| `docker compose config --quiet` | Успешно |
| `git check-ignore --no-index` для `projects/<p>/ЗАГРУЗИТЬ/...` | Успешно: `.gitignore:28` исключает загрузки, включая `Просмотровки` |
| Тесты маршрутов/final review/stage3 API | 47 passed |
| Sync regression tests | 104 passed |
| Тест dry-run и кэша ffprobe | 2 passed |

В Python-прогоне присутствовали предупреждения о недоступных опциях `timeout`/`timeout_method` в текущем pytest-окружении. Coverage и его порог 79% измерить нельзя.

## Вердикты по пунктам 1–9

| Пункт | Вердикт | Основание |
| --- | --- | --- |
| 1. TTS dry-run | Закрыто по выполненному сценарию | `test_dry_run_never_populates_production_cache` прошёл; dry-run использует `speaker/dry-run/assets/` и отдельный fingerprint (`src/stage1/gemini_tts.py:673–680`). Ошибочные non-200/пустой `/audio/speech` отдельно не покрыты тестом. |
| 2. Гонка сохранения | Частично подтверждено кодом | В текущем JS-пути используется поколение изменений; обычный CEP harness прошёл, но новый сценарий «сохранение → правка → задержанный ответ» отдельным поведенческим тестом не закреплён и вручную не выполнялся. |
| 3. Контекст CEP | Частично подтверждено кодом | `captureSourceOperation` замораживает контекст, а `exportPollResumeFn = check` сохраняет operation через hide/resume (`cep/js/main.js:1250`). Существующий Node CEP harness прошёл, но сценарий «экспорт A → считать B → завершение» для нового кода отдельно не повторён; Premiere не запускался. Поля, читаемые только при старте, остаются ограничением интерфейса. |
| 4. Удаление проекта | Закрыто для всех реестров статически | `_project_has_active_mutation` обходит `iter_job_registries()` (`src/api.py:204–209`), final review зарегистрирован (`src/api.py:186`), reserve-пути используют transition lock перед registry lock. Полной параметризованной матрицы реестров нет. |
| 5. Смешанные папки | Закрыто тестами/изменённым сканированием | Каталог объединяет legacy и `ЗАГРУЗИТЬ/Просмотровки`; приоритет inbox и дедупликация выполняются по `relative_path`. Тесты Stage 3 API прошли. |
| 6. mypy | Частично подтверждено | Assert-фикс присутствует, но обязательный `mypy src` не удалось выполнить: пакет отсутствует в окружении. |
| 7. Git-ignore | Закрыто | `git check-ignore --no-index` подтвердил исключение `projects/*/ЗАГРУЗИТЬ/` и `projects/*/media/`. |
| 8. Docker context | Закрыто статически | `.dockerignore` содержит `.venv`, `node_modules`, `projects`, `data`, `artifacts`, `.env.bak.*`; образ не собирался по скоупу. |
| 9. ffprobe cache | Закрыто выполненными тестами | `test_scanning_another_directory_keeps_unchanged_ffprobe_entries` и смежные тесты прошли; кэш сохраняет записи другой области, двойной скан не дублирует probe. |

## Новые находки

### P1 — apply может применить отчёт другого job

Код: `src/api_final_review_routes.py:81–97`.

`_apply` получает job по `job_id`, но затем читает общий `final_review_report.json` из проекта и не проверяет `report["job_id"] == job_id`. Если в одном проекте последовательно создать два final-review job, apply первого после завершения второго может использовать corrections второго отчёта. Это риск неверного монтажа, хотя клон исходной секвенции не изменяет.

Репродукция: создать два завершённых job в одном временном проекте, записать на диск отчёт второго, вызвать `POST /stage2/final-review/jobs/<первый>/apply`; текущий код вернёт corrections из файла второго job. Исправление: хранить report path/снимок в job и сверять `job_id` перед apply.

### P2 — старт без `full_timeline.json` тратит транскрипцию

Код: `src/api_final_review_routes.py:39–71`, worker в `src/api.py:1574+`.

`_start` проверяет WAV и стабильность, но не наличие/валидность `work/full_timeline.json`. Worker доходит до `FullTimeline.from_dict({})` и завершается ошибкой «timeline segments must be a list». При облачном ASR платная транскрипция уже будет запущена впустую.

Исправление: до резервации и запуска worker проверить наличие файла и корректность минимальной структуры, вернуть понятный `409`/`404`.

### P2 — у final-review нет cancel endpoint

Код: `src/api_final_review_routes.py:100–111`, реестр `src/api.py:168–186`.

Для final-review зарегистрирован active-project lock, но нет cancel-маршрута и cancel event. Зависшая задача удерживает проект до завершения или рестарта процесса; удаление проекта в этот период получает конфликт. Нужно добавить кооперативную отмену, статус `cancelled`, release в `finally` и ограничение/trim реестра по принятой модели остальных job.

### P2 — apply после перезагрузки панели молча не работает

Код: `cep/js/main.js:1689–1691`.

`applyFinalReview` сразу возвращается, если в памяти нет `currentFinalReviewJobId` или `currentFinalReviewSequence`. После перезагрузки CEP-панели job может остаться на backend, но кнопка не объясняет причину и не восстанавливает контекст. Это подтверждено статическим сценарием; в ручном Premiere не проверялось.

### P2 — poll final review останавливается на сетевой ошибке

Код: `cep/js/main.js:1600–1620`.

При `error` callback не планирует следующий poll и не переводит UI в retry/timeout состояние. Временный сетевой сбой оставляет панель в промежуточном состоянии без автоматического восстановления. Нужны bounded retry/backoff и явная кнопка повторить.

## Аудит нового алгоритма

- Пустой transcript не падает: `_best_span` возвращает пустой диапазон, finding получает `missing`. Несколько missing подряд не двигают `cursor`, поэтому последующие реальные совпадения могут получить `out_of_order`/лишние warnings. Это консервативный, но шумный результат, не исправленный кодом.
- Prefix/suffix для коротких диапазонов ограничены минимумом в один токен; автоматические corrections создаются только для `snh`/`host`, а для `zk` остаётся finding.
- Окно поиска ограничено примерно 600 словами вперёд (`src/stage2/final_review.py:36–51`), что ограничивает квадратичный поиск, но стоимость на длинных render остаётся риском производительности и не замерялась.
- `_project_audio_path` проверяет `resolve()`, принадлежность project root и суффикс `.wav` (`src/api_final_review_routes.py:17–25`); пустое значение направляется к штатному `final_review_audio`.
- Worker освобождает reservation через `finally` при ошибке транскрипции/анализа; исключение на старте thread также освобождает проект (`src/api_final_review_routes.py:56–70`).
- OpenRouter payload использует `/audio/speech`, `response_format=pcm`, модель `microsoft/mai-voice-2` и sample rate 24000 (`src/stage1/gemini_tts.py:250–292`). Non-200 и пустой ответ обрабатываются, но платный smoke-тест не выполнялся. В `.env` наличие `OPENROUTER_API_KEY` и `TTS_PROVIDER` подтверждено без вывода значений.
- Дефолт `TTS_PROVIDER=openrouter` (`src/stage1/config.py:132–144`) требует внимания пользователей со старым `.env`, где есть только Gemini key; README quick start явно предлагает macOS, но старый пользовательский `.env` следует проверить перед переключением.
- Паддинги `2.0/1.5` и metadata `speech_in/out_sec` согласованы с parser/model; fallback на `source_in/out` сохранён для старых матчей. Тесты sync-пути прошли.
- `host.jsx` lint-путь прошёл через `npm run lint:cep`; поведение Premiere/AME и доступность `Sequence.clone` на конкретной версии Adobe не подтверждены без ручного пилота.

## Пилот Premiere

Статус: **отложен**. Пользовательский прогон за Premiere/Media Encoder не выполнялся, поэтому F1–F4 и платный B′ не имеют фактических результатов. `docs/E2E_PILOT_CHECKLIST.md` дополнен:

- этапом B′ для одного платного smoke-теста OpenRouter MAI-Voice-2 с проверкой PCM, mono и примерно 24 кГц;
- этапом F1–F4 для export, polling, clone/apply и особых проверок;
- строками B′/F1–F4 в шаблоне отчёта;
- двумя вариантами запуска backend и уточнением модели Mac.

Перед пилотом рекомендуется закоммитить текущие исправления для воспроизводимости. Сам аудит этого не делал.

## Практический порядок работ

1. Исправить P1-сверку `report.job_id` и добавить route/lifecycle тесты final-review.
2. Добавить pre-check `full_timeline.json`, cancel/timeout и bounded retry для CEP polling.
3. Установить dev-инструменты из project requirements и повторить обязательный pytest с coverage, Ruff и mypy в том же окружении, где запускается CI.
4. Закоммитить проверенное дерево, затем пройти A–B–B′–C–D–E–F по обновлённому чек-листу и приложить артефакты при сбое.
