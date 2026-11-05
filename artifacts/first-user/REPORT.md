# Результат плана первого пользовательского пути — 5 ноября 2026

Реализованы установка, выбор сценария из папки, достоверные состояния задач,
подсказки следующего шага, справочники и демо. Основной путь: браузер → папки
ЗАГРУЗИТЬ → XML → Premiere. CEP остаётся дополнительным замороженным путём.

## Что проверено

- Чистая отдельная установка CPU/macOS Intel: setup-local, генерация токена,
  сохранение настройки при повторе, run-local, авторизация, health, ASR/TTS readiness.
  Использован Python 3.11; системный Python 3.9 не подходит. Итоговая копия заново установлена по финальному
  README, а полный демопрогон повторён новым установленным интерпретатором.
  Порт 8000 занят: только в .env отдельной копии задан порт 0; пользовательские
  процессы не останавливались.
- Реальный Chrome: пустая папка → кириллический сценарий с пробелами → разбор
  без LLM → подтверждение → reload → исчезновение файла без потери разметки.
  Следующий шаг видим при 1440×900 и 1280×800; скрытые панели/кнопки действительно скрыты.
- Реальный бесплатный демопрогон: два ЗК, СНХ, запись диктора, локальный ASR small CPU
  int8, подтверждение обоих диапазонов, публикация, сопоставление СНХ, таймлайн
  и XML в режиме complete. Нет подставленных результатов распознавания или ключей.
  Синхрон получил точное auto_matched (confidence 1); по существующему контракту
  повторное ручное подтверждение для него не требуется и не было имитировано.
- Последний повтор: **13.696 с с тёплым кэшем**, без времени установки
  и скачивания модели. Это замер короткого демо, не обещание скорости первого запуска.
- XML: три аудиоблока ЗК → СНХ → ЗК, 337 кадров / 25 fps = **13,48 с**;
  все ссылки на медиа существуют. Для WAV-просмотровки видео представлено
  штатной карточкой «НУЖНО ВИДЕО»; звуковой синхрон использует реальный WAV.
- Reload сохранил актуальную сборку; сохранение правки сценария и повторный reload
  обозначили прежний XML устаревшим. Два повторных прогона дали тот же смысловой результат.

Версии: {'Chrome': '150.0.7871.125', 'Premiere': '25.6.6', 'Python': '3.11.16', 'OS': '12.7.6', 'arch': 'x86_64'}. Демо использует вымышленного спикера и синтетический Milena.

## Итог проверок CI

- Python: **3164 passed, 35 subtests passed, 3 failed**, coverage **89,68%**
  при пороге 85%. Все три падения воспроизведены до изменений:
  test_normalizer_snh_dash_credit::test_file_header_merges_with_following_quote;
  ::test_file_header_merges_quote_with_prose_before_first_timecode;
  test_speaker_not_a_person::test_editorial_date_is_not_a_timecode_or_sync_credit.
- Ruff correctness, compileall, ESLint, CEP behavior, UI behavior (62), scroll-jump,
  runtime ASR imports, Docker build, Compose config — прошли. Последующие
  изменения тестовых файлов проверены отдельным прогоном: 64 passed + 4 subtests.
- Полный Ruff format: 17 прежних файлов требуют форматирования; mypy: 23 прежние
  ошибки в 5 файлах. Сравнение с b47d3c5 теми же версиями инструментов подтвердило
  тот же набор ошибок. Изменённые/новые Python-файлы проходят Ruff.
- Gitleaks с редактированием вывода — без утечек в проверенных коммитах.
- Аудит зависимостей не выполнен: индекс не предоставил pip-audit==2.10.1,
  указанный CI. Версия инструмента самовольно не подменялась.

## Что ещё не подтверждено

**Импорт в Premiere и прослушивание человеком не проверены.** Premiere 25.6.6
установлен, но доступного безопасного управления созданием отдельного проекта
и XML-импортом нет. Текущий пользовательский проект не изменялся, CEP не устанавливался.
Проверка XML и успешный backend не считаются доказательством импорта в Premiere.
Следующий ручной шаг — импортировать demo-timeline.xml в отдельный одноразовый
проект по docs/FIRST_PROJECT_CHECKLIST.md и проверить звук/границы.

Apple Silicon, Rosetta на реальном оборудовании, Docker-host onboarding, холодное
скачивание модели и качество длинных реальных записей требуют отдельных пилотов.
Полный проект не объявляется зелёным по частичным проверкам.

## Как повторить

Пользовательский путь: README.md и docs/FIRST_PROJECT_CHECKLIST.md.
Автоматический путь: node tests/first_user_browser_smoke.cjs --demo --keep-pilot
(нужны Chrome, CPU ASR и кэш small; настройки — docs/DEVELOPMENT.md).
Обычный --demo удаляет временные XML/отчёты, не перезаписывая сохранённый
пилот; сохранение выполняется только с --keep-pilot.
Машиночитаемые результаты: demo-browser-pilot.json, demo-xml-check.json.
XML demo-timeline.xml ссылается на сохранённую локальную копию стенда; после
переноса репозитория/удаления стенда потребуется связать медиа заново.

## Неблокирующие замечания ревью

- Два пустых соседних заголовка в справочниках — косметика.
- После переключения недоступной облачной озвучки на macOS рамка диагностики
  остаётся предупреждающей до обновления readiness; текст предупреждения исчезает.
- Список блокеров остаётся плоским; группировка по причине отложена.
- Зависимость теста демо от внешнего ROUGH_CUT_PORT исправлена: падение при 9222
  воспроизведено, затем 2/2 прошли с явным портом тестового процесса.

## Решения при исполнении

Решения и цена ошибки (рабочая запись контроллера):

Ruling: work in current non-main feature checkout — user requested commit/push and continuing here; clean baseline, no conflicting user changes; avoids moving user's active workspace. Cost if wrong: feature branch contains reviewed incremental commits, reversible.

Ruling: primary controls commits/push; workers may edit only assigned files and may not push, reset, delete user files or use external paid APIs. Controller can authorize individual commits explicitly.

Ruling: full CI verification at series end, focused checks per task — avoid repeating the 1900+ test suite for each cosmetic step. Baseline collected once, known pre-existing failures reported separately.

Ruling: proceed with onboarding plan despite pre-existing parser failures — plan explicitly excludes matching/parsing algorithm changes; preserve failure list and compare final suite. Cost if wrong: pre-existing defects remain openly reported, no false green claim.

Task7 feasibility research: Premiere Pro 2025 25.6.6 installed on macOS x86_64. Official local scripting dictionary lacks XML import/new-project commands; existing CEP host acts on currently open project, not isolated project. No safe programmatic pilot route identified without new integration. Ruling: do not mutate user's current Premiere project or install CEP; implement demo + executable checks, prepare exact manual isolated-project XML checklist and only mark import verified after actual evidence. Cost if wrong: manual final pilot remains explicitly outstanding.

Ruling: run Task3 installer work in parallel with Task2's scoped UI fix/re-review — user's permanent delegation policy requests useful bounded execution when slots are available; installer scripts/tests and UI files have disjoint ownership, neither worker may commit/push. Controller serializes integration/commits and dependent Tasks4/5. Cost if wrong: isolated uncommitted paths can be reviewed independently; no shared index mutation.

Ruling: Task7's independent bundled-demo assets/create-demo/checklist can proceed while Task4/5 UI work continues — files/interfaces disjoint and user delegation policy favors useful concurrency. Final Task7 browser flow/import acceptance remains gated on Tasks4–6; worker must not edit shared UI/browser harness yet. Primary owns final integration and honest Premiere evidence.

Ruling: exact auto_matched sync is accepted through existing ready contract; test checks real quote/word bounds and never fabricates a manual review state. Cost if wrong: manual listening/import remains unverified and clearly required.
