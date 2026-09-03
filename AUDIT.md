# Единый аудит rough_cut_v3

Дата консолидации: 2026-08-05. Последняя актуализация: 2026-09-03 (закрыты
P1-7 и P0-4, см. пометки ниже).
Источники: `PERF_AUDIT.md` (2026-08-05, свежий — основа раздела производительности),
`AUDIT_REPORT.md` (2026-06-09), `REVIEW_REPORT.md` (2026-06-09).
Проверка проведена по текущему рабочему дереву (`git log --oneline -30`, grep
по `src/`, `tests/`). Исходные файлы удалены 2026-08-05 (tracked-версии — в
git-истории; содержимое PERF_AUDIT.md полностью перенесено сюда).

## Как читать этот файл

- Раздел «Производительность» — из `PERF_AUDIT.md`, актуален как есть (дата
  совпадает с датой консолидации), с одной проверенной поправкой (см. ниже).
- Раздел «Закрыто» — пункты `AUDIT_REPORT.md`/`REVIEW_REPORT.md`, для которых
  найдено прямое подтверждение в коде/тестах.
- Раздел «Остаётся открытым» — пункты июньских отчётов, актуальность которых
  подтверждена (или не опровергнута) при проверке кода.

## Версии сценария: аудит и исправления 2026-08-11

Проверен полный путь редакции между Stage 1–4, API, фоновой обработкой,
рабочими JSON-проекциями и браузерным UI. Исправления применяются через общие
project paths и per-project locks, без специальных условий для BRIK.

### Закрыто

- **Родитель новой редакции.** Импорт строит diff от активной валидной
  редакции, а не всегда от последней по номеру; новый `revision_id` остаётся
  уникальным и монотонно растёт относительно всей истории.
- **Целостность active/work.** `inspect_revision_state` проверяет соответствие
  `work/active_revision.json` и `scenario_revision_id` рабочей нормализации.
  При расхождении импорт и переключение возвращают конфликт вместо тихого
  смешивания двух редакций.
- **Безопасное восстановление.** `POST /stage4/revisions/<id>/reconcile`
  сохраняет рабочие JSON и `speaker/zk*.wav(.meta)` в
  `versions/recovery/<timestamp>/`, затем восстанавливает выбранную известную
  редакцию.
- **Активация.** До изменения active marker валидируются имена и наличие
  revision-scoped snapshots, source hash, normalized/prepared/voiceover
  metadata, diff, timeline и sync matches. Запись рабочих JSON выполняется
  атомарно; исходящая редакция получает актуальный snapshot.
- **Снимок исходника.** `normalized.source` указывает на канонический
  `versions/scenarios/rev_NNNN.source.<ext>`, поэтому удаление или замена файла
  во входящей папке не меняет уже импортированную редакцию.
- **Идентичность редакции.** `scenario_revision_id`/`parent_revision_id`
  сохраняются при повторной нормализации и подтверждении, проходят в prepared,
  voiceover manifest, sync metadata и timeline. Артефакты другой редакции не
  принимаются как свежие только из-за совпадения остальных полей.
- **Конкурентные переходы.** Импорт, активация и reconcile сериализованы
  per-project `RLock`; переход отклоняется, пока для того же проекта активна
  TTS, normalization, sync или Stage 3 job.
- **UI.** Перед активацией сохраняется dirty-сценарий; блокирующее нарушение
  целостности показывается пользователю с действием восстановления. Ответ
  старого async diff-запроса больше не может перерисовать сравнение уже выбранной
  редакции. Импорт передаёт выбранные `use_llm` и `hint`, а snapshot-файлы
  истории не предлагаются как новые входящие сценарии.

### Проверенная миграция данных

- BRIK восстановлен до согласованной активной `rev_0002`; прежняя расходившаяся
  рабочая проекция сохранена в `versions/recovery/20260811T155814.012459Z/`.
- Для RVK_ULINOVSK восстановлена revision metadata; активна согласованная
  `rev_0002`.
- Остальные обнаруженные проекты не имели истории редакций и корректно
  возвращают `no_history`, а не ошибку. Это штатный режим до первого baseline.

### Открытые ограничения Stage 4

- Diff не классифицирует split/merge как отдельные операции и не имеет
  отдельного ambiguous-resolution workflow.
- UI/API сравнивает редакцию только с сохранённым родителем; выбора произвольной
  пары нет.
- Нет пользовательских операций delete/archive/rename для истории редакций.
- Активация не является общей транзакцией для всех файлов: каждый JSON
  заменяется атомарно после предварительной валидации, но автоматического
  rollback всей группы нет. `RLock` является внутрипроцессным; конфигурация с
  несколькими worker-процессами потребует общей блокировки.
- Отдельные build/jobs/XML-export endpoints из раннего Stage 4 плана и Stage
  4.5 media library пока не реализованы. Подробный roadmap — в
  `DEVELOPMENT_PLAN.md`.

### Автоматическая проверка

На 2026-08-11 полный набор проекта прошёл: **473 passed**. Дополнительно после
актуализации этого раздела запущены профильные тесты
`test_stage4_versioning.py`, `test_stage4_revision_assets.py` и
`test_api_route_contract.py`: **31 passed**.

## Производительность (основа: PERF_AUDIT.md, 2026-08-05)

Метод исходного аудита: статический анализ (4 прохода), замер тестов;
профилирование на реальном проекте не проводилось.

### Что уже оптимизировано (без изменений с PERF_AUDIT)

Параллельные LLM-чанки (`stage1/analyzer.py:701-714`), параллельная
LLM-классификация с кэшем по sha256 (`stage1/normalizer.py:206-251`),
TTS-пул + fingerprint-кэш (`stage1/gemini_tts.py:889-944`), долгоживущий
Whisper-воркер (`stage2/whisper_process.py:181-296`), фоновые daemon-джобы
Stage 2 (`api_stage2_routes.py:251-373`), кэш word-index по сигнатуре аудио
(`api.py:744-769`).

### P0 — главные узкие места

1. **~~Stage 3 синхронно в HTTP-обработчиках~~ — ЗАКРЫТО коммитом `3636cc4`
   (2026-08-05, «Speed up media scans and background exports»), уже после
   написания PERF_AUDIT.md.**
   Проверено: добавлены `POST /stage3/jobs/scan/start`,
   `POST /stage3/jobs/prepare/start`, `GET /stage3/jobs/<job_id>`,
   `GET /stage3/jobs/status` с progress-callback (`src/api_stage3_routes.py:320-651`);
   фронтенд переключён на polling через `pollStage3Job`
   (`src/static/stage1.js:1502-1574`). Старые синхронные `/stage3/media/scan`
   и `/stage3/timeline/prepare` (`src/api_stage3_routes.py:653-687`) оставлены
   как fallback-эндпоинты, не как единственный путь.

2. **~~ffprobe без кэша, полный пересканинг~~ — ЗАКРЫТО тем же коммитом.**
   Проверено: `_cached_probe_metadata`/`_metadata_cache_path`
   (`src/stage3/media_catalog.py:174-318`) кэширует метаданные по
   `fingerprint(size, mtime_ns)`; неизменные файлы не переиспускают `ffprobe`.

3. **CEP: полный COM-скан секвенции на каждый inspect — ЧАСТИЧНО ЗАКРЫТО тем
   же коммитом.**
   Проверено: добавлен `roughCutSignalCache` с ключом
   `(sequenceID, structuralSignature)` для `sequenceRoughCutSignals`
   (`cep/host.jsx:8-9,277-294,198-231`), `clipCount`/`trackCount` вынесены в
   локальные переменные внутри циклов. Кэш **не** применяется к секвенциям с
   маркерами (`markerCount !== 0` → кэш пропускается, `cep/host.jsx:139-145`,
   комментарий: «имена маркеров могут меняться без изменения numMarkers»).
   Для рабочих (уже размеченных) секвенций горячий путь остаётся некэшированным.

4. **CEP conform: O(items × (маркеры + клипы)) — ЗАКРЫТО (частично) коммитом
   `13c9e9a` (2026-09-03, «Index sequence markers by name during conform»).
   Проверено 2026-09-03.**
   `exactMarkersNamed` (`cep/host.jsx:2512-2521`) теперь ищет по индексу
   `markerIndexCache` (`cep/host.jsx:25`, объявление; `buildMarkerIndex` строит
   его один раз за скан) вместо полного COM-обхода маркеров на каждый вызов;
   поиск по имени стал O(1) после построения индекса. Индекс инвалидируется во
   всех точках мутации маркеров и на входе каждой публичной операции. Индекс
   `nodeId → clip` для `findTrackItemByNodeId`/`matchingSourceClips` не
   реализован — линейные сканы клипов остаются открытой частью рекомендации.

### P1 — существенно (без изменений, проверено против текущего кода)

5. **Перечитывание JSON-хранилищ на каждый запрос** — подтверждено,
   `_ensure_project`/`_sync_stores`/`_confirmed_normalization`
   (`src/api.py:234,292,692`) читают с диска при каждом вызове, in-memory
   кэша с инвалидацией по mtime не найдено.
6. **~~Whisper по умолчанию не MLX на Apple Silicon~~ — ЗАКРЫТО** на Apple Silicon
   auto-выбор ставит `mlx-whisper` первым (`src/stage2/whisper_runner.py:100-102`,
   fallback-порядок только для не-ARM платформ).
7. **Кэш LLM-анализа в `analyzer.py` — ЗАКРЫТО. Проверено 2026-09-03.**
   Реализован по тому же принципу, что и в `normalizer.py`:
   `_llm_cache_path`/`_load_llm_cache`/`_llm_cache_fingerprint`/
   `_cached_llm_blocks`/`_cache_llm_blocks` (`src/stage1/analyzer.py:655-769`)
   кэшируют успешные batch-результаты по sha256 от текста чанка и
   идентичности LLM-конфигурации (`_llm_identity`) в
   `work/analysis_llm_cache.json`. Покрыто тестом
   `tests/test_stage1_analyzer_llm_cache.py`.
8. ffmpeg atempo отдельным subprocess на блок (`stage1/gemini_tts.py:35-64`) —
   не менялось.
9. Блокирующие backoff-sleep в ретраях (`gemini_tts.py:758,1051`,
   `normalizer.py:1209,1213`) — не менялось.
10. CEP polling (whisper-status + export fileInfo, `cep/js/main.js:450-510,881-940`) —
    без единого scheduler/in-flight guard, без изменений.

### P2 — точечно (без изменений)

11. `_deduplicate` O(n²) в `stage2/sync_matcher.py:481-509` — не менялось.
12. `map_catalog_to_timeline` O(S×M) в `stage3/source_mapping.py:257-267,170-206` —
    не менялось.
13. `sequenceDurationSeconds` полный скан треков (`cep/host.jsx:146-177`) —
    не менялось.
14. `_audio_cache_signature` читается 3–4 раза на источник
    (`api_sync_worker.py:173,237,250,278`, `api.py:744-769`) — не менялось.
15. Whisper-воркер ретайрится при cancel/timeout → холодный старт
    (`whisper_process.py:287-294,333-345`) — не менялось.
16. 4 потока gunicorn ограничивают параллельность (`Dockerfile:32`) — без
    изменений; частично снято для Stage 3 благодаря п.1/2 (фон вместо
    удержания потока на минуты).

### Обновлённый план внедрения (эффект/труд)

1. ~~Stage 3 фоновые джобы + кэш ffprobe~~ — сделано (`3636cc4`).
2. ~~`SYNC_WHISPER_BACKEND=mlx` на Apple Silicon~~ — уже не требуется
   (auto-выбор предпочитает `mlx-whisper` на Apple Silicon; переменная нужна лишь
   если mlx-whisper не установлен — тогда установить пакет).
3. ~~Один скан маркеров на conform-проход (P0-4)~~ — сделано (`13c9e9a`,
   2026-09-03); индекс nodeId→clip для клипов пока не сделан.
4. In-memory кэш JSON-хранилищ по mtime (api.py) — не сделано.
5. ~~Кэш LLM-анализа в analyzer.py~~ — сделано (`src/stage1/analyzer.py:655-769`,
   проверено 2026-09-03).
6. Единый poller с in-flight guard в cep/js/main.js — не сделано.
7. Timing-харнесс/бенчмарки этапов pipeline — не сделано, инструментов в
   репозитории по-прежнему нет.

## Закрыто (подтверждено кодом/тестами) — из AUDIT_REPORT.md / REVIEW_REPORT.md

| # | Находка (июнь 2026) | Файл(ы) | Доказательство закрытия |
|---|---|---|---|
| 1 | `AUDIT_REPORT` P0: dry-run перезаписывал существующий WAV тишиной (batch) | `stage1/gemini_tts.py` | `_is_valid_wav` guard перед silence-записью в batch dry-run; тест `tests/test_p0_p1_regressions.py::DryRunPreservesExistingWavTest` |
| 2 | `REVIEW_REPORT` P2: dry-run **`synthesize_phrase`** по-прежнему перезаписывал WAV | `stage1/gemini_tts.py:1013-1024` | Та же `_is_valid_wav` проверка теперь применена и в phrase-ветке (не только batch) |
| 3 | `AUDIT_REPORT` P0: анализ без ручных маркеров падал (`block.text` не существует) | `stage1/analyzer.py:647-650` (было) | Поле исправлено на `text_original`/`text_for_tts`; regression-тесты LLM/fallback веток |
| 4 | `AUDIT_REPORT` P1: ID-контракт Stage1↔Stage2 (`ЗК10`→`zk1`→`zk10`, суффиксы `ЗК4.1`/`ЗК38А`, дубли `zk1`) | `stage1/analyzer.py`, `stage2/timeline_parser.py` | `timeline_parser.py:15-19` — regex получил lookahead `(?=\s|[:\-–]|$)`, суффиксы больше не «утекают» в текст; `tests/test_stage2_timeline_parser.py` (в т.ч. тест на `ЗК4.1`) |
| 5 | `REVIEW_REPORT` P1: Stage 2 терял блоки для `[[VO]]`/`VO:`/`NARRATOR:` и после merge (`blocks=1, timeline_zk=0`), строковая замена ненадёжна | `stage1/analyzer.py:540-575` | Заменено на `_canonical_raw_projection`: раздача канонических `ЗКN:` меток по `source_spans` вместо `str.replace`; ненайденные блоки дописываются с корректным `ЗК{num}:` в конец, а не молча теряются |
| 6 | `REVIEW_REPORT` P1: новый ZK-regex ложно матчил текст (`ЗК4.1 Блок` → `.1 Блок`, `ЗАКАДРОВЫЙ` ошибочно триггерил ZK-сегмент) | `stage2/timeline_parser.py:12-19` | `\b` после `ЗАКАДР` и lookahead после суффикса — оба сценария из отчёта прямо описаны в комментариях regex и покрыты тестом на суффикс |
| 7 | `AUDIT_REPORT` P1: повреждённый WAV проходил валидацию (`duration_ms==0` — warning, не error) | `stage1/validator.py:143` | `errors.append` вместо `warnings.append`; тест `CorruptedWavFailsValidationTest` |
| 8 | `AUDIT_REPORT` P1: устаревший WAV (текст/настройки изменились) не перегенерировался | `stage1/gemini_tts.py` | Fingerprint sidecar `.meta` (`SHA-256` от text+provider+voice+gender+speed+**model+sample_rate**); тест `StaleWavRegenerationTest` |
| 9 | `REVIEW_REPORT` P2: fingerprint не учитывал model/sample_rate/`MACOS_TTS_RATE` | `stage1/gemini_tts.py:211-213,625-634` | `_fingerprint_parameters` теперь возвращает `(model, sample_rate, extra_rate)` для каждого provider и подмешивает их в payload fingerprint |
| 10 | `AUDIT_REPORT` P2: нет изоляции путей между проектами | `api.py:132-141` (было) | `_abs_path(..., root=paths["root"])` — пути вне каталога проекта отклоняются 400 |
| 11 | `REVIEW_REPORT` P2: `/stage2/build` возвращал HTML 500 при плохом пути (`_abs_path` вне try/except) | `api_stage2_routes.py:599-602` | Вызов `_abs_path` обёрнут в собственный `try/except ValueError` → `_json_error(...,400)` |
| 12 | `REVIEW_REPORT` P2: семафор не мешал двум job одного проекта писать в один `zkN.wav` одновременно | `api.py`, `job_runtime.py` | `reserve_project`/`release_project` + `active_projects`-сеты на TTS/normalization/sync (`api.py:108-124,438,605,1295`) — не более одного активного job на проект |
| 13 | `AUDIT_REPORT` P2: неограниченные фоновые TTS-задачи | `api.py` | `threading.Semaphore(_TTS_CONCURRENT_MAX=3)` + per-project reservation (см. п.12) |
| 14 | `AUDIT_REPORT` P2: XML описывал аудио не так, как оно сгенерировано | `stage2/xml_export.py` | `_wav_audio_params()` читает реальные `sample_rate/bit_depth/channels` из WAV |
| 15 | `AUDIT_REPORT` P2: ошибки placeholder подавлялись | `api.py:697-715` (было) | Ошибка попадает в `warnings`, `placeholder_path` обнуляется |
| 16 | `AUDIT_REPORT` P2: локальные defaults расходились с документацией (`MAX_ZK_BLOCKS`, `python-dotenv`, `.env`) | `config.py`, `requirements.txt`, `api.py`, `README.md` | Дефолт `40`, `load_dotenv()`, `README.md` инструкция `cp .env.example .env` |
| 17 | `REVIEW_REPORT` P3: README пример API не работал после изоляции путей (`input/scenario.docx`) | `README.md` | Текущий README везде использует `projects/<project>/input/scenario.docx` (проверено grep) |

## Остаётся открытым (проверено против кода на 2026-08-05)

Из `AUDIT_REPORT.md`/`REVIEW_REPORT.md` не нашлось прямого опровержения или
подтверждения исправления для следующих пунктов (не были предметом этой
сессии проверки — возможны изменения, не отражённые в коде явным образом):

- **Runtime-проверка импорта XML в Premiere Pro** — оба отчёта прямо
  указывают, что этот шаг оставался ручным и не проверялся автоматически;
  подтверждений импорта в тестах нет и в этом проходе не искалось за
  пределами существующих unit/contract-тестов.
- **Live-тесты платных TTS/LLM провайдеров** — согласно `AUDIT_REPORT.md`, не
  входят в автоматический набор; не проверялось, изменилось ли это.
- Общий вывод `REVIEW_REPORT.md` о внутренней противоречивости
  `AUDIT_REPORT.md` (пункты одновременно «исправлено» и в плане «немедленно
  исправить») — актуален только как замечание к старому документу; в этом
  файле статусы сведены заново и такого противоречия нет.

## Что не удалось проверить в этой сессии

- Фактическое поведение Premiere при batch conform (Stage 2) и при импорте
  Stage 3 XML — требует запуска Premiere, не проверялось.
- Данные production-профилирования `/stage2/build` и `/stage3/timeline/prepare`
  на реальном проекте — PERF_AUDIT прямо указывает, что профилирование не
  проводилось; эта сессия тоже не запускала профилировщик.
- Полный охват `git log` за пределами последних 30 коммитов — более старые
  изменения, которые могли закрыть другие пункты июньских отчётов, не
  просматривались построчно (проверялись только конкретные grep-паттерны по
  ключевым находкам).
