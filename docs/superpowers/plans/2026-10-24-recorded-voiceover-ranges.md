# Recorded Voiceover Ranges Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Собирать закадр из диапазонов одной чистовой записи (возможно из нескольких частей) в порядке сценария; блоки без записи идут по TTS или остаются «нет озвучки».

**Architecture:** Добавить отдельный источник записанного закадра, автоопределение файлов из `ЗАГРУЗИТЬ/Закадр/` и последовательную
привязку к `block_uid`. Общий WAV и диапазоны проходят через таймлайн, assembly plan,
версии и оба XML-экспортера; путь TTS сохраняется. Решение по каждому блоку хранится как `voice_source`.

**Tech Stack:** Существующие Python/Flask, ffmpeg, Whisper worker, JavaScript, Premiere XML.

**Spec:** `docs/superpowers/specs/2026-10-24-recorded-voiceover-ranges-design.md`

Статус: план для обсуждения. Пользователь запросил только план; код не менять.

## Global Constraints

- MVP: одна чистовая запись (основной случай — один сплошной файл; вторичный — части одной записи), один диктор, порядок сценария.
- Дубли не сдвигают последующие блоки. Дубль всего блока: все прочтения сохраняются как `takes`, по умолчанию выбирается последний полный дубль; блок помечается «дубли: N» и `needs_review` до подтверждения. Частичный дубль: один диапазон на блок, пометка «частичный дубль», решение за пользователем. Повтор фразы в разных блоках сценария дублем не считается.
- Никакой нарезки по блокам, изменения темпа и автоматического удаления дыхания.
- До применения готовой разметки текущая озвучка не меняется. Публикация идет по блокам:
  частичная запись допустима; блок без подтвержденного диапазона использует TTS или «нет озвучки».
- Автоопределение только запускает локальный ASR; автопереход на платный backend запрещен.
- Существующие TTS, СНХ и старые проекты сохраняют совместимость.
- Использовать текущие ограничения загрузки, аутентификацию, project locks,
  реестр активных задач, общий ASR semaphore, отмену и бюджеты облачного ASR.

## Review Focus

1. Повторяющиеся фразы и пропущенные слова не сдвигают молча остальные блоки — Task 2.
2. Общий исходник длиннее блока: XML не обрезает media duration до длины фразы — Task 5.
3. Правка границ без изменения сценария делает прежний build устаревшим — Task 4/5.
4. Смена редакции, отмена или сбой публикации не смешивают старую озвучку с новой — Task 3.
5. Проект с TTS и без новых полей работает без миграции пользовательских файлов — Task 1/4/5.
6. Смешанная сборка не теряет TTS-блоки; смена `voice_source` меняет build — Task 3/4/5.
7. Автоопределение не запускает платный ASR; части записи не пересекаются — Task 1/2.
8. Дубли не сдвигают последующие блоки; по умолчанию выбран последний полный дубль; частичный дубль помечен и не режется из середины без решения пользователя — Task 2/3/6.

## Точки интеграции, проверенные в репозитории

- `src/stage2/whisper_process.py:383`: `transcribe_audio_in_process(...)` уже
  поддерживает progress, cancel event, timeout, cloud options и budget callback.
- `src/stage2/sync_transcript.py:121`: `ingest_word_index(...)` формирует WordIndex;
  не требуется регистрировать запись закадра как источник СНХ.
- `src/stage2/timeline_parser.py:641`: сейчас ЗК ищет `<speaker_dir>/zkN.wav`.
- `src/stage2/models.py:30`: поля `source_in_sec/source_out_sec` уже существуют,
  но предназначены в основном для синхронов.
- `src/stage2/xml_export.py:158`, `src/stage3/nested_xml.py:501`: сейчас ЗК
  экспортируется с `in=0`; media duration равна длительности отдельного блока.
- `src/api.py:665`: готовность озвучки сейчас считается по наличию `zk*.wav`.
- `src/stage4/revision_assets.py`: snapshot/restore и integrity предполагают
  отдельные WAV-активы и позиционные алиасы.
- CEP conform в `cep/js/main.js:1511` предназначен для СНХ/ведущих. В MVP закадр
  попадает в Premiere через XML; добавлять вставку закадра в этот conform не требуется.

### Task 1: Модель общего источника и привязок

**Files:** Create `src/voiceover/models.py`, `src/voiceover/storage.py`,
`src/voiceover/__init__.py`; modify `src/project_service.py`, `src/inbox_service.py`;
create `tests/test_recorded_voiceover_storage.py`; extend `tests/test_inbox_service.py`.

**Interfaces:** `RecordingSource` хранит `id`, project-relative `asset`, `sha256`,
`duration_sec`, `sample_rate`, `channels`, `part_index`, `part_name` (части одной записи
упорядочены по имени файла). `VoiceoverRange` хранит `block_uid`,
`text_sha256`, `recording_id`, `source_in_sec`, `source_out_sec`,
`speech_in_sec`, `speech_out_sec`, `status`, `confidence`, `manual_range`,
`takes: list[{source_in_sec, source_out_sec, speech_in_sec, speech_out_sec, confidence, partial: bool}]`
(все прочтения блока; пустой список — блок без записи), `selected_take: int | None` (индекс выбранного дубля;
`None` — авто: последний полный дубль). Ручной выбор сохраняется отдельно от авто-решения (`origin: auto|manual`).
`load_recorded_voiceover(paths: dict[str, Path]) -> dict` читает рабочую карту.

- [ ] Написать тесты round-trip, отбраковки NaN/Infinity и диапазонов вне файла,
  безопасных путей и повторного импорта одного содержимого; проверить пустой результат для старого проекта;
  round-trip `takes`/`selected_take`; старая карта без этих полей читается с `takes=[]`, `selected_take=None`.
- [ ] Написать тесты сканирования `ЗАГРУЗИТЬ/Закадр/`: новый файл регистрируется один раз,
  повторный скан не дублирует, файл из inbox попадает сюда же, несколько файлов сортируются
  по имени как части одной записи, пустая папка означает отсутствие записи (остается TTS),
  пересекающиеся подтвержденные диапазоны частей отбраковываются.
- [ ] Запустить `.venv/bin/python -m pytest tests/test_recorded_voiceover_storage.py tests/test_inbox_service.py -q`: новые тесты сначала FAIL.
- [ ] Реализовать хранение: `ЗАГРУЗИТЬ/Закадр/`, неизменяемые источники
  `speaker/recordings/<sha256>/`, рабочая карта `work/recorded_voiceover.json`.
  WAV остается общим; MP3 получает одну общую PCM-копию. ASR-кеш хранить отдельно
  по хешу файла и фактической конфигурации ASR, без создания `zkN.wav`.
  Сканирование `ЗАГРУЗИТЬ/Закадр/` и inbox регистрирует новые файлы как части записи;
  ASR здесь не запускается (его ставит Task 2).
- [ ] Повторить тесты: PASS. Проверить diff и зафиксировать отдельным коммитом при реализации.

### Task 2: Распознавание и последовательное сопоставление

**Files:** Create `src/voiceover/alignment.py`, `src/api_voiceover_jobs.py`,
`tests/test_voiceover_alignment.py`, `tests/test_voiceover_jobs.py`;
modify `src/api.py`, `src/api_service_contracts.py`.

**Interfaces:** `align_voiceover(normalization: dict, index: WordIndex) -> list[VoiceoverRange]`;
job получает immutable source ID и снимок редакции, возвращает предложения
со статусами `needs_review`/`missing`. Публикации в активную озвучку здесь нет.
Алгоритм: последовательное выравнивание; распознавание соседних повторов текста блока
(полные дубли → `takes`, выбор по умолчанию — последний полный; частичные дубли → `partial=True`,
один предлагаемый диапазон: без вырезания из середины, иначе от начала первого до конца последнего прочтения);
дубли не продвигают курсор дальше последнего прочтения блока и не сдвигают следующие блоки;
блок с дублями получает пометку «дубли: N» и `needs_review`.
Автозапуск: при регистрации новой части job ставится в очередь автоматически, только локальный ASR;
части выравниваются по порядку имен, каждая следующая ищет опоры только среди оставшихся блоков.

- [ ] Написать тесты: три ЗК среди СНХ, поглощенные блоки, одинаковые фразы,
  пропуск слова/блока, пустое ASR, долгая запись, отмена в очереди ASR и при распознавании,
  автозапуск при обнаружении файла, выравнивание нескольких частей по порядку имен
  без пересечения диапазонов, отсутствие вызова платного backend при автоопределении;
  блок с 3 полными дублями подряд → 3 `takes`, выбран третий (`selected_take` по умолчанию = последний),
  следующие блоки не сдвинуты; частичный дубль одного слова → `partial=True`, пометка и предлагаемый диапазон
  без выреза из середины; одинаковая фраза в двух разных блоках сценария ≠ дубль (нет `takes`, порядок сохранен).
- [ ] Запустить `.venv/bin/python -m pytest tests/test_voiceover_alignment.py tests/test_voiceover_jobs.py -q`: сначала FAIL.
- [ ] Переиспользовать `transcribe_audio_in_process` и `ingest_word_index`.
  Последовательно искать текстовые опоры, уточнять границы блоков между ними;
  не выполнять независимый поиск каждой фразы по всей записи. Кеш ASR не зависит
  от текста сценария, результат выравнивания зависит от текста и редакции.
  Общий ASR slot и cancellation обязательны; автопереход на платный backend не добавлять.
- [ ] Повторить тесты: PASS; проверить diff и коммит при реализации.

### Task 3: API подтверждения, публикация и версии

**Files:** Create `src/api_voiceover_routes.py`, `tests/test_recorded_voiceover_api.py`;
modify `src/api.py`, `src/stage4/revision_assets.py`, `src/stage4/versioning.py`,
`src/api_stage1_routes.py`; extend `tests/test_stage4_revision_assets.py`,
`tests/test_api_route_contract.py`, `README.md`.

**Interfaces:** Project-scoped `/stage1/recorded-voiceover`: GET состояния,
POST `/source` с multipart `audio` либо выбранным inbox file ID,
POST `/jobs/start`, GET `/jobs/<job_id>`, POST `/jobs/<job_id>/cancel`,
PATCH `/ranges/<block_uid>` (границы и `selected_take`; выбор дубля меняет диапазон блока и инвалидирует таймлайн и build),
PATCH `/blocks/<block_uid>/voice-source` (`recorded|tts`, ручной override),
POST `/apply`, GET `/audio` для общего playback.
JSON-операции передают project и expected revision; PATCH/apply также передают
ожидаемый generation разметки для отклонения устаревшего запроса с 409.

- [ ] Тесты: неподтвержденный сценарий, чужой путь, oversized upload,
  устаревший PATCH, конкурентный TTS/смена редакции, отмена, рестарт,
  сбой публикации, восстановление редакции, перенос/изменение блока,
  частичная публикация (часть блоков recorded, часть tts, часть none),
  ручной TTS-override поверх найденного диапазона, блок без записи и без TTS получает `none`;
  PATCH `selected_take` выбирает другой дубль, сохраняет `origin=manual`, инвалидирует timeline/build;
  PATCH с устаревшим generation → 409; частичный дубль публикуется только после подтверждения.
- [ ] Запустить `.venv/bin/python -m pytest tests/test_recorded_voiceover_api.py tests/test_stage4_revision_assets.py tests/test_api_route_contract.py -q`: сначала FAIL новых случаев.
- [ ] Отделить draft от опубликованной карты; публиковать подтвержденные диапазоны по блокам
  (частичная публикация допустима). Для каждого блока хранить `voice_source = recorded | tts | none`
  и происхождение `auto|manual`; ручной TTS-override имеет приоритет над найденным диапазоном.
  Использовать журнал восстановления для публикации и snapshots, не подставлять общий файл
  в каждый `zkN`-алиас. Явно различать TTS manifest и recorded source/ranges; integrity и restore
  выбирают проверку по типу озвучки. Измененные тексты теряют привязку,
  неизменные UID сохраняют диапазоны. Гарантировать блокировки в обе стороны.
  Смена `voice_source` инвалидирует таймлайн и build.
- [ ] Повторить тесты: PASS; обновить route contract и документацию; review/коммит.

### Task 4: Таймлайн и единая проверка готовности

**Files:** Modify `src/stage2/models.py`, `src/stage2/timeline_parser.py`,
`src/stage2/assembly_plan.py`, `src/stage2/assembly_manifest.py`, `src/stage_common.py`,
`src/api.py`, `src/api_stage1_routes.py`, `src/api_stage2_routes.py`,
`src/api_stage3_routes.py`; extend их существующие тесты.

**Interfaces:** Дополнить `TimelineSegment` полями `voiceover_kind` (`recorded|tts`, default `tts`, на уровне блока),
`audio_source_duration_sec`, `audio_source_sha256`. Для recorded-ЗК использовать
`wav_path` как путь общего монтажного WAV, `source_id=recording_id`, существующие
`source_in_sec/source_out_sec`, `speech_in_sec/speech_out_sec`, `block_uid`.
`duration_sec=source_out_sec-source_in_sec`. Старые TTS остаются с in=0.
Рабочая карта получает монотонный `generation`; таймлайн сохраняет хеш опубликованной карты.

- [ ] Тесты: один файл обслуживает несколько ЗК без `zkN.wav`, СНХ между ними,
  legacy TTS, нет файла/подтверждения, выход за duration, одинаковая нормализация
  с новой разметкой делает timeline устаревшим. Assembly manifest содержит диапазоны ЗК.
  Смешанный таймлайн (блоки recorded и tts рядом) собирается без потери TTS-блоков;
  блок без recorded и без tts дает причину `missing_voiceover`.
- [ ] Запустить `.venv/bin/python -m pytest tests/test_stage2_timeline_parser.py tests/test_stage2_assembly_plan.py tests/test_stage2_assembly_manifest.py tests/test_tts_api.py -q`: новые случаи FAIL.
- [ ] Сделать общую проверку готовности озвучки для status, validate, build и export:
  готовность по блоку: recorded ИЛИ tts. TTS проверяет WAV-актив, recorded проверяет источник,
  revision/text hash и диапазон. Assembly plan пишет `missing_voiceover` только если нет ни recorded, ни tts.
  Не направлять recorded source ID в поиск секвенций СНХ/медиа mapping.
  При изменении карты или `voice_source` отклонять старый timeline/export с требованием пересборки.
- [ ] Повторить тесты: PASS; review/коммит.

### Task 5: Оба XML-экспортера и идентичность сборки

**Files:** Modify `src/stage2/xml_export.py`, `src/stage3/nested_xml.py`,
`src/stage3/builds.py`; extend `tests/test_stage2_xml_export.py`,
`tests/test_stage3_nested_xml.py`, `tests/test_stage3_builds.py`,
`tests/test_stage2_assembly_manifest.py`.

**Interfaces:** `clipitem.start/end` описывают таймлайн; `clipitem.in/out` — отрезок
общей записи; `file.duration` — полный исходник. ID клипа уникален на блок,
file ID уникален в XML; одинаковый path допустим для нескольких клипов.
TTS-клип (`in=0`) и диапазоны общей записи могут идти на одной дорожке ЗК вперемешку.

- [ ] Тестовый пример: WAV 120 с; ЗК1=10–15 с, ЗК2=40–47 с; монтажная длительность
  5+7 с, оба pathurl одинаковые, file.duration=120 с. Проверить оба экспортера,
  уникальные XML ID, dedicated VO track, округление по fps без накопления дрейфа,
  старые TTS и корректный source range в assembly manifest.
  Добавить смешанный случай: ЗК1 recorded, ЗК2 tts (`in=0`), ЗК3 recorded на одной дорожке.
- [ ] Запустить `.venv/bin/python -m pytest tests/test_stage2_xml_export.py tests/test_stage3_nested_xml.py tests/test_stage3_builds.py tests/test_stage2_assembly_manifest.py -q`: сначала FAIL новых случаев.
- [ ] Исправить обе ЗК-ветки; включить source hash, карту диапазонов и `voice_source` каждого блока
  в build identity. Смена диапазона или `voice_source` меняет build; замена общего файла по тому же пути
  выявляется проверкой источника. Секвенции СНХ не затрагивать.
- [ ] Повторить тесты: PASS; review/коммит.

### Task 6: Интерфейс и сквозная приемка

**Files:** Create `src/static/stage1/recorded_voiceover.js`;
modify `src/templates/project.html`, `src/static/stage1.js`,
`src/static/stage1/state.js`, `src/static/stage1/core.js`,
`src/static/stage1/tts.js`, `src/static/stage1.css`;
extend `tests/stage1_behavior_test.cjs`, `docs/STAGE1_GUIDE.md`.

**Interfaces:** «Озвучка» → «Синтез / Готовая запись» → выбрать файл →
«Сопоставить со сценарием» → список блоков с текстом, in/out и статусом →
прослушать/поправить/подтвердить → «Применить запись к редакции» → сборка.
Колонка «Источник: Диктор / TTS / нет» по блокам; переключатель «использовать TTS» для блока.
Один проигрыватель ищет позиции в общем WAV и останавливается на конце диапазона.
Полноценный waveform-редактор не требуется для MVP.

- [ ] Тесты поведения: восстановление job после reload, отображение числа готовых
  блоков вместо требования `zkN.wav`, колонка источника по блокам, переключатель блока на TTS,
  список дублей блока с прослушиванием и выбором (`selected_take`), пометки «дубли: N» и «частичный дубль»,
  уведомление «найдена запись диктора — идет сопоставление» при автоопределении,
  предупреждение о возможной разнице громкости TTS и диктора, подтверждение и PATCH границ,
  ошибка stale revision, переключение редакции, применение при частичной разметке
  (блоки без записи показывают TTS или «нет озвучки»).
- [ ] Запустить `npm run test:stage1`: новые случаи сначала FAIL; реализовать UI;
  повторить до PASS.
- [ ] Запустить `.venv/bin/python -m pytest`, `.venv/bin/python -m ruff check src tests`,
  `.venv/bin/python -m ruff format --check src tests`, `.venv/bin/python -m mypy src`,
  `npm run test:stage1`, `npm run lint:cep`; исправить ошибки, провести diff review.
- [ ] Ручная приемка при реализации: чистовая запись с тремя ЗК и СНХ между ними;
  запись из двух частей; блок с TTS рядом с блоком записи; импорт обоих XML в Premiere;
  запись одним сплошным файлом с дублями (проверить список дублей блока, выбор последнего по умолчанию и смену выбора);
  прослушать начала/концы, проверить диапазоны, один общий источник, длительность,
  финальную проверку и возврат к другой редакции.
  Если Premiere недоступен, явно отметить эту часть как непроверенную.
- [ ] Обновить руководство, review/коммит. До отдельного запроса реализации
  ни один пункт этого плана не выполнять.
