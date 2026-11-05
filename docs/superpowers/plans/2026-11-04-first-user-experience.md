# First User Experience Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Новый монтажёр самостоятельно проходит путь от установки и выбора сценария до импорта XML в Premiere.

**Architecture:** Улучшить существующий Flask/JS веб-интерфейс, переиспользуя inbox, normalization jobs и Stage 3 status. Сохранить папочную работу и редакции. Унифицировать выбор локального окружения без удаления действующих команд; CEP остаётся дополнительным способом.

**Tech Stack:** Python 3.11 / native macOS 3.12, Flask, browser ES modules, CSS, Bash, pytest, Node behavior tests, FFmpeg, MLX/faster-whisper.

**Spec:** [2026-11-04-first-user-experience-design.md](../specs/2026-11-04-first-user-experience-design.md).

Статус: утверждён пользователем 2026-11-04, начато выполнение. Основной путь «браузер →
папки ЗАГРУЗИТЬ → XML → Premiere» подтверждён пользователем 2026-11-04.
Оценки времени намеренно не назначены без проверки установки и реального пилота.

## Global Constraints

- Основной путь — браузер, папки `ЗАГРУЗИТЬ` и XML; развитие CEP вне scope.
- Python 3.11 — опорная версия; 3.12 — дополнительная для native Apple Silicon.
- Сохранить команды, имеющиеся `.venv`/`.venv-macos-arm64` и явный `ROUGH_CUT_VENV`.
- Существующие непустые настройки и токен не перезаписываются.
- Локальный основной путь: macOS Intel/Apple Silicon; Linux и Windows/WSL2 — Docker. Первый XML-пилот: текущий macOS Intel + Premiere Pro 2025, с записью точных версий.
- Не менять алгоритмы matching/retiming, формат редакций и UID, хранение озвучки, блокировки и бюджеты.
- Прямая загрузка DOCX через браузер — отдельное последующее расширение.
- Не выполнять массовые миграции проектов и не модернизировать CEP.
- Изменения API — добавочные; не переименовывать существующие поля и маршруты ради текстов UI.
- Демо и обязательные проверки не запускают платные услуги.
- Coverage для `src/` сохраняет текущий порог 85%; не добавлять тесты, проверяющие только косметический текст.

## Review Focus

1. Существующий `.env`, нестандартное окружение и Rosetta: настройки сохраняются, setup/run/doctor используют согласованный профиль. Проверки: задача 3.
2. Кириллица, NFC/NFD, пробелы и исчезнувший сценарий: список не путает файлы и не отправляет старый путь. Проверки: задача 4.
3. Несохранённые правки и новая редакция: выбор исходника не заменяет/не активирует её молча. Проверки: задача 4.
4. Reload после TTS/XML и устаревшая сборка: результат не маскируется состоянием задачи или пустым mapping. Проверки: задача 2.
5. Холодный запуск без модели, ключей и Premiere: демо объясняет загрузку модели; XML не считается проверенным импортом. Проверки: задачи 5–7.

## Порядок и приоритеты

| Очередь | Приоритет | Результат | Зависимости |
|---|---|---|---|
| 1 | P0 | Понятный вход: авторизация и бесплатный режим | — |
| 2 | P0 | Достоверные состояния UI | — |
| 3 | P1 | Согласованная установка и диагностика | — |
| 4 | P1 | Выбор сценария из папки в «Нормализации» | — |
| 5 | P1 | Понятный маршрут и готовность возможностей | 2, 4 |
| 6 | P1 | Короткая согласованная документация | 3, 4, 5 |
| 7 | P2 | Демо и проверенный путь до Premiere | 2–6 |

Задачи 2, 3 и 4 можно исследовать независимо. Изменения общих файлов
`project.html`, `state.js`, `core.js`, `stage1.js` интегрируются последовательно.
Каждую задачу сдавать отдельным проверяемым изменением. Коммиты и способ
исполнения определяются при переходе к реализации.

## Task 1: Снять первые блокировки в инструкции

**Files:** Modify `README.md`, `docs/STAGE1_GUIDE.md`.

**Interfaces:** Существующий Basic Auth: любое имя пользователя, пароль —
`ROUGH_CUT_API_TOKEN`. Токен нигде не выводится в примерах и логах.

- [x] Возле первого открытия `http://127.0.0.1:8000` объяснить логин/пароль и место токена в `.env`.
- [x] Явно разделить ключ доступа к приложению и необязательные платные ключи провайдеров.
- [x] Перед пробным прогоном показать создание проекта и все три пользовательские папки; назвать dry-run WAV-заглушками без речи.
- [x] Объяснить, что базовый текущий setup пока не устанавливает ASR; не обещать весь путь до синхронов до задачи 3.
- [x] Проверить команды и ссылки чтением и ручным проходом до первой страницы. Новые автоматические тесты для текста не нужны.

**Готово:** пользователь понимает запрос пароля и ограничения бесплатного
режима до первого запуска, не ища раздел API.

## Task 2: Исправить противоречивые состояния UI

**Files:** Modify `src/static/stage1.css`, `src/static/stage1/stage3.js`,
`src/static/stage1/tts.js`, `src/static/stage1/core.js`,
`src/static/stage1/state.js`, `src/static/stage1.js`, `src/templates/project.html`.
Tests: extend `tests/stage1_behavior_test.cjs`; create browser smoke
`tests/first_user_browser_smoke.cjs` and a dev-only runner configuration if needed.

**Interfaces:** Consume `GET /stage3/status?project=`:
`{processing, build, assembly}`; сохранить смысл трёх измерений.
Use existing job statuses and guards. Existing backend contract/spec:
`docs/superpowers/specs/2026-10-25-stage3-readiness-status.md`.

- [x] Зафиксировать воспроизведение в реальном браузере со стилями: stop скрыт в idle/completed/failed/cancelled, видим для соответствующей активной задачи; вкладки и диалоги корректно скрываются.
- [x] Исправить перекрытие `hidden` правилами CSS; Node DOM mock сам по себе не доказывает визуальную невидимость.
- [x] Связать пустое состояние mapping с действительным статусом: при существующем таймлайне без синхронов показать отсутствие требуемых соответствий, а не просьбу собрать таймлайн заново.
- [x] Обновлять три состояния сборки при открытии вкладки, завершении build/prepare и смене редакции; после reload не показывать устаревший успех. Существующие backend guards остаются источником запретов.
- [x] Проверить `build.is_current=false`, `assembly.no_timeline`, `assembly.error`, draft с пропусками, complete и legacy XML без build_id. Пустой список медиа не превращает успешный экспорт в ошибку.
- [x] Выполнить `npm run test:stage1` и `.venv/bin/python -m pytest tests/test_stage3_status.py tests/test_stage3_api.py -q`; браузерный smoke проверить на idle → running → completed и после reload.

**Готово:** задача, готовность монтажа и файл результата не противоречат друг
другу. Успешный XML с двумя ЗК не сопровождается «Сначала соберите timeline».

## Task 3: Унифицировать локальную установку

**Files:** Modify `scripts/setup-local`, `scripts/run-local`, `scripts/doctor`,
`scripts/setup-macos-apple-silicon`, `scripts/run-native-macos`,
`scripts/bootstrap-macos`, `scripts/bootstrap-docker`, `docs/DEPENDENCY_POLICY.md`;
create shared profile/env helpers under `scripts/lib/`.
Tests: create `tests/test_setup_profiles.py`; update
`tests/test_deployment_contract.py`, `tests/test_dependency_locks.py` only where contracts change.

**Interfaces:** Setup/run/doctor share profile selection `auto|cpu|mlx` through
`ROUGH_CUT_PROFILE`; `ROUGH_CUT_VENV` wins for the environment path.
Auto: native macOS ARM → MLX/`.venv-macos-arm64`; otherwise CPU/`.venv`.
Native wrapper explicitly chooses MLX; Docker remains explicit and separate.
Явный профиль задаётся в окружении терминала для всех трёх команд; одноразовый
override не сохраняется неявно. При отсутствии auto-окружения runner объясняет
выбор и явный запуск существующего CPU/native venv, не мигрирует его.

- [x] Сначала создать поведенческие shell-тесты с подставными uname/python/pip/doctor: ARM, Intel, Rosetta, неподдерживаемый Python, явный venv; никакой реальной установки пакетов в этих тестах.
- [x] Проверить одинаковый явный профиль для setup/run/doctor и случай двух существующих venv: auto выбирает по платформе; понятная ошибка сообщает, как запустить старый профиль, без удаления окружений.
- [x] Вынести общий выбор профиля и подготовку `.env`; обеспечить токен через `secrets.token_hex(32)` без вывода значения. Тесты: новый файл, пустой токен, существующий непустой токен, повторный запуск.
- [x] Согласовать установку runtime/ASR с профилем, не удаляя dev-инструменты существующего пути; новые native macOS настройки выбирают `TTS_PROVIDER=macos`. Не перезаписывать имеющийся provider/connector.
- [x] Сохранить guards ARM/Rosetta и текущую Python-политику; runner/doctor используют то же окружение. Не переносить и не удалять старые venv автоматически.
- [x] Специализированные команды оставить совместимыми обёртками; обычный setup не вызывает CEP/LaunchAgent. `bootstrap-macos` остаётся явным расширенным сценарием.
- [x] Doctor сообщает о возможностях выбранного профиля, не требует коннектор для native `say` и не предупреждает о неиспользуемом Gemini-ключе. Модель Whisper загружается отдельно, с пояснением места/объёма и сетевой необходимости.
- [x] Выполнить `.venv/bin/python -m pytest tests/test_setup_profiles.py tests/test_deployment_contract.py tests/test_dependency_locks.py -q` и `bash -n` всех изменённых shell-файлов. Затем реальные smoke на доступных платформах в отдельной копии; недоступные платформы отметить непроверенными.

**Готово:** две публичные команды setup/run выбирают согласованное окружение
с локальным ASR; повторный setup сохраняет пользовательские настройки.

Проверено 2026-11-04: чистая установка CPU на macOS Intel в отдельной копии, генерация токена, повторное сохранение настроек, запуск API, авторизация и наличие локального ASR. Apple Silicon, Rosetta на реальном ARM и Docker-host smoke остаются непроверенными; поведенческие ветки проверены изолированными тестами.

## Task 4: Выбирать сценарий без ручного ввода пути

**Files:** Modify `src/templates/project.html`, `src/static/stage1/state.js`,
`src/static/stage1/normalization.js`, `src/static/stage1/revisions.js`,
`src/static/stage1.js`; create `src/static/stage1/inbox.js`.
If required, additive path metadata in `src/api_core_routes.py` / `src/api_stage4_routes.py`.
Tests: `tests/stage1_behavior_test.cjs`, `tests/test_stage1_web_contract.py`,
`tests/test_inbox_service.py`, `tests/test_normalization_api.py`, `tests/test_api_stage4_routes.py`.

**Interfaces:** Existing inbox `scenario_files[]` has `name`, `relative_path`,
`fingerprint`. Shared discovery returns unfiltered files. Normalization resolves
project-relative path once; import keeps its existing project-relative convention
and fingerprint filter. Do not feed inbox-relative values directly into a route
expecting repository-relative values.

- [x] Добавить тесты выбора/обновления списка: пусто, один/несколько файлов, кириллица и пробелы, сохранение выбора, исчезновение файла, ошибка API, legacy `input/`.
- [x] Вынести общий запрос inbox; отдельные представления для нормализации и новых редакций. Не отфильтровывать уже импортированный активный исходник из нормализации.
- [x] Добавить select, «Обновить список», пояснение папки и выбранного файла. При пустом списке показать конкретное действие; путь убрать в расширенные настройки.
- [x] Привязать выбор к existing normalization job, а не к revision import. Не запускать обработку на change. Перечитать список при возврате в окно с debounce, не перетирать выбор и правки.
- [x] Проверить активную редакцию после reload и смену файла при черновике/несохранённых правках/активной задаче. Сохранить существующие подтверждения и блокировки.
- [x] Выполнить `npm run test:stage1` и `.venv/bin/python -m pytest tests/test_inbox_service.py tests/test_normalization_api.py tests/test_api_stage4_routes.py tests/test_stage4_revision_assets.py tests/test_stage1_web_contract.py -q`; пройти браузером путь от пустого проекта до подтверждения.

**Готово:** файл из `ЗАГРУЗИТЬ/Сценарии` выбирается на стартовой вкладке, без
правки пути и без изменения семантики редакций.

## Task 5: Сделать рабочий маршрут понятным

**Files:** Modify `src/templates/index.html`, `src/templates/project.html`,
`src/static/projects.js`, `src/static/projects.css`, `src/static/stage1.css`,
`src/static/stage1/readiness.js`, `src/static/stage1/core.js`,
`src/static/stage1/normalization.js`, `src/static/stage1/stage3.js`,
`src/api_core_routes.py`.
Tests: `tests/test_readiness_api.py`, `tests/stage1_behavior_test.cjs`, browser smoke from task 2.

**Interfaces:** `/system/readiness` сохраняет `checks`, `status`, `overall`;
добавляет `capabilities` с `local_tts`, `local_asr`, `cloud_llm`, `cloud_tts`,
`cloud_asr` (булевы доступности). Проверка существования ключа не означает
проверенную связь с внешним сервисом. Существующие action guards сохраняются.

- [ ] Сначала зафиксировать случаи: рабочий локальный режим без ключей; нет FFmpeg; нет ASR; выбранный недоступный облачный режим; ошибка ответа readiness. Не маскировать настоящие проблемы.
- [ ] Показать компактную готовность по возможностям. Только отсутствие необязательного ключа не раскрывает предупреждения автоматически; явно сказать, что локальная работа доступна. LLM без ключа выключен по умолчанию.
- [ ] На главной объяснить «сценарий + записи → черновой монтаж для Premiere». Убрать общий `Stage 1` из карточек/title, сохранив технические этапы в подробностях.
- [ ] Основные кнопки назвать «Создать озвучку», «Проверить аудиофайлы», «Собрать таймлайн», «Скачать XML для Premiere». Увести manifest/ID/backend-подробности и редко используемые поля под раскрытие; блоки с проблемами остаются заметными.
- [ ] Показать следующий шаг после подтверждения, озвучки и сборки; рекомендации отражают existing guards, не создают обхода подтверждений. Этап «Редакции» остаётся доступным параллельно, не обязательным первым шагом.
- [ ] После XML показать постоянную инструкцию импорта, актуальность/черновой режим и пропуски. Ошибку файла объяснять рядом с выбором по-русски, техническую причину сохранять в журнале.
- [ ] Выполнить `.venv/bin/python -m pytest tests/test_readiness_api.py tests/test_stage1_web_contract.py -q`, `npm run test:stage1` и браузерную проверку 1440×900/1280×800: основной следующий шаг видим и не заслонён диагностикой.

**Готово:** пользователь понимает назначение продукта, доступный режим и
следующее действие; облачные функции не выглядят обязательной настройкой.

## Task 6: Собрать единую пользовательскую документацию

**Files:** Modify `README.md`, `docs/README.md`, `docs/STAGE1_GUIDE.md`,
`docs/TECH_DEBT.md`; create `docs/USER_GUIDE.md`, `docs/CONFIGURATION.md`,
`docs/API.md`, `docs/DEPLOYMENT.md`, `docs/DEVELOPMENT.md`.
Adjust `scripts/gen-api-routes` and existing documentation contract tests if their targets move.

**Interfaces:** README — маршрут нового пользователя. USER_GUIDE — материалы,
сценарий, готовый/синтетический закадр, синхроны, редакции, XML/Premiere.
CONFIGURATION/API/DEPLOYMENT/DEVELOPMENT — соответствующие справочники.

- [x] Переписать основной README вокруг согласованного браузерного пути и единой пары setup/run; Docker дать явной альтернативой, CEP — дополнительной ссылкой с текущим статусом заморозки.
- [x] Описать три папки, поддерживаемые расширения и правильную разметку короткого сценария. Объяснить первичный разбор отдельно от загрузки новой редакции.
- [x] Перенести переменные среды и API-контракты без потери информации; направить генератор API-таблицы в `docs/API.md`. Обновить внутренние ссылки, индексы и тесты, проверяющие старое расположение текста.
- [x] Согласовать STAGE1_GUIDE с текущими маршрутами. Зафиксировать реальные ограничения демо/моделей и импорт XML. Не удалять открытые пункты TECH_DEBT до подтверждённого исправления.
- [x] Проверить Markdown-ссылки и команды; выполнить `.venv/bin/python -m pytest tests/test_deployment_contract.py tests/test_dependency_locks.py tests/test_stage1_web_contract.py -q`. Если другой тест зависит от перенесённых разделов, обновить его проверку на документ-владелец.

**Готово:** по README можно начать работу без чтения REST API и десятков
переменных среды; техническая информация доступна по отдельным ссылкам.

## Task 7: Демо-проект и реальный пользовательский пилот

**Files:** Create `examples/first-project/README.md`, `scenario.txt`,
`narrator.wav`, `review.wav`, `expected.json` in that directory;
create `scripts/create-demo`, `tests/test_demo_project.py`;
create `docs/FIRST_PROJECT_CHECKLIST.md` and report in `artifacts/first-user/`.
Extend browser smoke from task 2 with the complete first-project flow.

**Interfaces:** `scripts/create-demo` creates a unique `demo_<suffix>` under
`projects/`, uses the existing project layout, copies scenario/review/narrator
into the three inbox folders and prints the URL. `expected.json` records the
three expected block types, source speaker and reference quote; it is not a
ready-made normalization/sync result. Existing project names are never reused.

- [ ] Подготовить короткий собственный сценарий из двух ЗК и одного синхрона; два небольших WAV с подходящей речью, пригодные для бесплатного локального ASR. Демо использует «Готовая запись», native macOS TTS описать дополнительным вариантом.
- [ ] Написать тесты создания нового проекта и повторного запуска: уникальность, три папки, наличие/валидность WAV, сохранность уже существующего проекта. Затем реализовать команду без сети и платных запусков.
- [ ] Дать короткую инструкцию прямо с результатом команды; выбор модели и первое скачивание описать отдельно от работы с кэшем. Не обещать фиксированное время холодного запуска.
- [ ] Пройти установку в отдельной копии по финальному README: вход → создание демо → выбор сценария → подтверждение → готовая запись → локальный ASR/проверка синхрона → XML. Сначала без платных ключей; зафиксировать реальные результаты, версии, время и вмешательства.
- [ ] На текущем macOS Intel импортировать XML в установленный Premiere Pro 2025: записать точные версии, проверить число/порядок блоков, связность медиа, звук, реплику синхрона и длительность. Это XML-пилот, не CEP conform. Не записывать его как выполненный при недоступном Premiere; Apple Silicon/Docker требуют отдельных проверок.
- [ ] Проверить reload и повторное открытие демо, отсутствие ложного успеха у устаревшей сборки. Не проверять воспроизводимость ASR побайтовым сравнением XML; сравнивать ожидаемые реплики и слышимые границы.
- [ ] Выполнить `.venv/bin/python -m pytest tests/test_demo_project.py -q` и браузерный smoke. Перед сдачей всей серии выполнить CI-проверки из `.github/workflows/ci.yml`; известные прежние падения отделить от регрессий, не объявлять весь проект зелёным по частичному прогону.

**Готово:** сохранён отчёт, по которому другой человек повторяет путь без
автора. Непроверенные платформы/импорт помечены явно. Качество синхронов на
реальных больших проектах остаётся отдельным пилотом из TECH_DEBT.

## Последующие задачи, не блокирующие первую версию

- Прямая загрузка сценариев из браузера: отдельный endpoint с лимитом размера,
  проверкой расширения/содержимого, безопасным именем, запретом выхода из проекта
  и явной политикой совпадающих имён. WAV-upload не расширять вслепую.
- Дополнительные показатели на карточках проектов: активная редакция, время
  изменения, последнее полезное состояние — после согласования лёгкого read-only API.
- Пилот на недоступных платформах и отдельная оценка качества длинных записей.
- Возобновление CEP только отдельным решением пользователя.
