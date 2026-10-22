# Проверка правдоподобия СНХ-совпадений и резервный поиск по local index

Спецификация глобального исправления `SyncMatcher` по итогам
`docs/UIP_ATOM_AUDIT.md` (раздел «Обновление 22.10.2026»). Это правка матчера,
а не проекта: любой проект получает её при следующем sync.

## Исходные факты

- `SyncMatcher(sources, indexes, config)` (`src/stage2/sync_matcher.py`) — чистая
  функция от запроса и `WordIndex`.
- Основной индекс — `<source_id>.words.json` (облачный ASR, возможно
  `timestamp_precision: retimed`). Рядом `index_retiming` хранит
  `<source_id>.local_words.json` (локальный Whisper по всему файлу) в той же
  временной базе. Файл может отсутствовать.
- Матчер строится в `run_match_phase` (`src/sync_match_phase.py`);
  отпечаток — `_sync_matching_fingerprint` (`src/api.py`).
- Подтверждённые (`confirmed`) матчи матчер не трогает.

## A. Резервный поиск по local index

- Новый модуль `src/stage2/match_verification.py`:
  `load_local_word_index(index_path, source_id, *, primary_index=None) -> WordIndex | None`.
  Читает `local_words.json` через `local_words_path` / `_normalize_words`;
  `version == LOCAL_WORDS_VERSION`, слова не пусты. Проверку модели/сигнатуры
  аудио, как в `_load_local_words`, не делает (нет аудио под рукой), но если у
  основного индекса есть `metadata.transcription_cache.audio` и в файле есть
  `audio`, то `size` (и `sample_sha256`, когда он есть у обоих) обязаны совпасть,
  иначе файл игнорируется (чужое аудио = чужая шкала времени). Метаданные
  результата: `timestamp_precision="word"`, `index_kind="local_whisper"`.
- `SyncMatcher(..., local_indexes=None)` (keyword). Внутри лениво создаётся
  вложенный `SyncMatcher` по тем же источникам и local-индексам (без рекурсии).
- В `match()`: если основной результат `not_found` с причиной `no_candidate` или
  `below_review_threshold` и у подходящих источников есть local-индекс —
  запрос повторяется во вложенном матчере. При `confidence >= review_threshold`
  возвращается совпадение из local-индекса: `needs_review` (никогда не auto,
  кроме `MatcherConfig.local_fallback_auto=True`), `strategy="local_index_fallback"`,
  `metadata`: `review_reason="local_index_fallback"`, `index_kind="local_whisper"`,
  `primary_confidence`, `primary_unmatched_reason`. Альтернативы основного
  результата дописываются после local-кандидатов. Иначе — исходный `not_found` с
  `local_fallback_tried=True` и `local_best_confidence`.
  Если local-индекса у подходящих источников нет — результат не меняется.
  Фильтр по спикеру считается один раз по основным источникам; вложенному матчеру
  передаются `source_ids` = подходящие источники, у которых есть local-индекс
  (в исходном порядке), поэтому источник другого/неразмеченного спикера не выбирается.

## B. Проверки правдоподобия времени (каждый не-`not_found` результат)

Конфигурация `MatcherConfig`: `max_words_per_sec=6.0`, `collapsed_word_sec=0.03`,
`local_boundary_tolerance_sec=0.4`, `local_fallback_auto=False`.

1. `implausible_word_rate`: слов в диапазоне >= 5 и слов/сек > `max_words_per_sec`.
2. `collapsed_word_times`: слов >= 4 и >= 60% слов длятся <= `collapsed_word_sec`.
3. Сверка с local transcript (только если для `best.source_id` есть local-индекс
   и результат получен из основного индекса). Вложенный матчер ищет тот же
   запрос только рядом (near-timecode по `search_window_sec`, без глобального
   fallback) с `approximate_timecode = speech_in`, `source_ids=[best.source_id]`.
   Время кандидатов матчера уже включает `guide_offset_sec`, как и
   `approximate_timecode`, поэтому сдвиг не требуется. При local `confidence >=
   review_threshold`:
   - `tail_truncated_vs_local`: `local.end > speech_out + tolerance`;
   - `head_truncated_vs_local`: `local.start < speech_in - tolerance`;
   - `range_disagrees_with_local`: перекрытие речевых диапазонов < 50% local-диапазона.
   Если рядом local ничего не нашёл (`below_review_threshold`) при уверенном
   основном (`confidence >= auto_threshold`) — `local_text_mismatch`.
   Отсутствие слов local-индекса в окне (`no_candidate`) — нет данных, флаг не ставится.
   При любом local-флаге первой в `alternatives` добавляется local-диапазон
   (`_padded_range`), в `metadata` — `local_speech_in_sec`, `local_speech_out_sec`,
   `local_confidence`.
- Любой флаг B => `needs_review` (не `auto_matched`), `metadata.plausibility_flags`,
  `review_reason` = первый флаг, если ещё не задан; `timing_reliable=False` для
  флагов 1/2. Без флагов поведение и метаданные не меняются.
- Производительность: проверка 3 — один near-timecode поиск по local-индексу на
  не-`not_found` результат.
- Коррекция диапазона: если проверка 3 дала флаги диапазона (`range_disagrees_with_local`,
  `head/tail_truncated_vs_local`) и local-поиск нашёл весь запрос однозначно
  (`auto_matched` по обычным порогам: `auto_threshold`, `ambiguity_margin`, семантика/gap/rate),
  берётся локальный диапазон (speech_in/out из local-слов, те же head/tail padding).
  Метаданные: `range_source="local_transcript"`, `range_corrected_flags` (сработавшие флаги),
  `cloud_speech_in/out_sec`, `cloud_source_in/out_sec`, `cloud_confidence`, `cloud_timing_flags`;
  облачный диапазон — первой альтернативой; `plausibility_flags` не ставятся. Статус
  `auto_matched`, если облачный текст тоже проходит `auto_threshold` без семантических флагов
  (двойное подтверждение текста; облачная неоднозначность игнорируется — дубли типичны для
  сломанного диапазона), иначе `needs_review`. Слабое/неоднозначное local-свидетельство —
  прежнее поведение (облачный диапазон + флаги + `needs_review`). Подтверждённые не
  пересчитываются (фаза). Версия отпечатка матчинга 6.

## C. Подключение

- Фаза сопоставления грузит local-индексы каждого проиндексированного источника
  через `load_local_word_index(ctx.word_index_path(...), ...)` и передаёт их
  матчеру (kwarg `local_indexes` только если они есть).
- `_sync_matching_fingerprint`: версия 5 + стабильный дайджест набора local-индексов
  (`source_id`, число слов, sha256 слов), чтобы неподтверждённые матчи
  пересчитались при следующем sync.

## Тесты

`tests/test_sync_match_verification.py` — синтетические `WordIndex`: fallback
находит фразу только из local; без local — без изменений; 20 слов по 20 мс за
0.4 с; обрезанный хвост относительно local; чистое совпадение остаётся
`auto_matched`; отпечаток меняется при появлении local-индекса; загрузчик.
