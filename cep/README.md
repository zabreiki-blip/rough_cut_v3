# Rough Cut — панель синхронизации для Premiere Pro

Панель является Premiere-частью следующего этапа текущего Rough Cut v3. Она
регистрирует уже синхронизированные секвенции и даёт проверяемый механизм
вставки найденного диапазона в монтажную секвенцию. Видео не отправляется в
backend.

## Что уже реализовано

- чтение метаданных активной секвенции;
- безопасное разрешение секвенции: сохранённый `sequenceID`/`projectItem.nodeId`
  обязаны совпасть; fallback по имени используется только для legacy-записи без ID;
- `POST /stage2/sync-sources` с метаданными активной секвенции;
- автоматический export полного guide WAV через Adobe Media Encoder;
- polling размера/даты WAV до завершения записи, регистрация guide и запуск
  настраиваемого Whisper job;
- batch conform подтверждённых СНХ из `GET /stage2/timeline` поверх
  плейсхолдеров активной Rough Cut секвенции;
- повторная проверка project path и ID активной target sequence перед batch;
- sequence-маркеры `RC_SYNC` с текстом, source range и confidence;
- настраиваемые backend URL, проект и Bearer token;
- журнал ответов Premiere/backend.

API token намеренно не сохраняется в `localStorage`. URL и имя проекта
сохраняются локально. Также можно сохранить общий каталог `projects`: он нужен,
чтобы автоматически построить путь
`<projects>/<project>/work/sync_audio/<source_id>.wav`.

При первом запуске нужно указать проект, ключ доступа и общий каталог
`projects`. Адрес локального backend по умолчанию — `http://127.0.0.1:8000`.

`source_id` обязан соответствовать backend-контракту
`^[A-Za-z0-9_-]{1,96}$`. Если имя секвенции полностью кириллическое, панель
автоматически использует стабильный `sequenceID` вместо кириллического slug.

## Структура

```text
cep/
  CSXS/manifest.xml
  css/panel.css
  js/CSInterface.js
  js/main.js
  host.jsx
  index.html
```

`js/CSInterface.js` — минимальный совместимый мост, адаптированный из локальной
панели `PR_TOOLBOX`. Полная библиотека Adobe не нужна: используются только
`evalScript` и `getSystemPath`.

## Локальная установка для проверки

1. Сделать symlink каталога `cep` в пользовательский каталог расширений:

   macOS:

   ```sh
   ln -s /absolute/path/to/rough_cut_v3/cep \
     "$HOME/Library/Application Support/Adobe/CEP/extensions/rough-cut-v3-sync"
   ```

   Windows:

   ```text
   %AppData%\Adobe\CEP\extensions\rough-cut-v3-sync
   ```

2. Для unsigned development build включить `PlayerDebugMode` для CSXS runtime,
   который использует установленный Premiere, и перезапустить Premiere.
   Например для CSXS 11 на macOS:

   ```sh
   defaults write com.adobe.CSXS.11 PlayerDebugMode 1
   ```

3. Открыть `Window → Extensions → Rough Cut — Синхронизация`.

В текущей development-среде symlink уже установлен, а `PlayerDebugMode` включён
для CSXS 11/12. После изменения панели достаточно перезапустить Premiere.
Перед распространением расширение нужно подписать ZXP и поднять bundle version.

## Backend contract

Панель использует существующую конвенцию проекта без префикса `/api`:

```text
POST /stage2/sync-sources
GET  /stage2/sync-sources?project=<name>
GET  /stage2/sync-matches?project=<name>
```

UI использует `GET` для восстановления существующей регистрации и `POST` для
создания/обновления source.
После готовности WAV панель вызывает настраиваемый маршрут
`POST /stage2/sync-jobs/start` с
`{project, source_ids: [source_id]}`. Его можно заменить в настройках панели без
изменения кода. Backend повторно использует индекс, только если не изменились
аудиофайл и конфигурация распознавания; matcher использует все уже готовые
индексы проекта.

Пример payload регистрации:

```json
{
  "project": "BRIK",
  "id": "vasilieva_01",
  "source_id": "vasilieva_01",
  "speaker": "Татьяна Васильева",
  "speakers": ["Ирина Котова"],
  "all_speakers": false,
  "premiere_sequence": "SYNC_ВАСИЛЬЕВА_01",
  "sequence_id": "01234567-...",
  "sequence_name": "SYNC_ВАСИЛЬЕВА_01",
  "sequence_start_tc": "01:00:00:00",
  "project_path": "/path/to/project.prproj",
  "fps": 25,
  "duration_sec": 1842.7,
  "guide_offset_sec": 0,
  "metadata": {
    "project_item_node_id": "123",
    "zero_point_sec": 0,
    "video_track_count": 2,
    "audio_track_count": 3
  }
}
```

Ответ backend может быть непосредственным JSON или обёрткой `{ "ok": true,
"result": ... }`: панель журналирует его без зависимости от формы.

## Автоматический guide WAV

Кнопка «Экспортировать звук и запустить распознавание»:

1. проверяет, что активна именно считанная sync-секвенция;
2. создаёт недостающий `work/sync_audio`;
3. запускает `app.encoder.encodeSequence(..., 0, 1)`, затем `startBatch()`;
4. каждые две секунды проверяет WAV через host `fileInfo`;
5. после трёх стабильных проверок повторно регистрирует source с
   `guide_audio`, затем запускает Whisper route.

Если поле preset пустое, host ищет
`Wave48mono24.epr` относительно `app.path`, в первую очередь по macOS-пути:

```text
Contents/Settings/EncoderPresets/Wave48mono24.epr
```

Если установленная версия Premiere хранит пресет иначе, укажите полный путь к
любому подходящему audio `.epr` вручную. Существующий WAV не перезаписывается,
пока явно не включён флажок перезаписи.

Backend принимает запросы из CEP/file origin при
`ROUGH_CUT_CEP_CORS=1`; все реальные запросы по-прежнему требуют Bearer token.
Docker-сервис публикуется только на `127.0.0.1`, поэтому этот режим не следует
переносить на публичный сетевой интерфейс.

## Внутренний API вставки диапазона

Ручные контролы одиночной тестовой вставки не входят в продуктовый интерфейс.
Низкоуровневая команда `rcStage2_insertSequenceRange` остаётся в `host.jsx`
для автоматических регрессионных тестов и диагностики разработки.

`sourceInSeconds` и `sourceOutSeconds` считаются от начала media sync-секвенции,
а не от отображаемого sequence timecode (`zeroPoint`). Это совпадает с полным
guide WAV, экспортированным от начала секвенции без удаления пауз. Значение
`zeroPointSeconds` регистрируется отдельно, чтобы последующая автоматизация
могла явно учесть иной экспортный offset.

Host-код временно задаёт in/out у `source.projectItem`, выполняет вставку и
всегда восстанавливает исходные in/out в `finally`. Содержимое исходной
sync-секвенции не изменяется.

### Технические ограничения

- Вставка меняет активную **целевую** секвенцию и не имеет автоматического undo.
  Продуктовый сценарий запрашивает подтверждение и рекомендует работать с копией.
- Premiere API отличается между версиями. Сначала используется
  `Sequence.overwriteClip/insertClip`, затем ограниченный track fallback.
  Ответ содержит `usedApi` и наблюдавшееся изменение количества video/audio
  items.
- Проверка через количество items является внутренней диагностикой, а не
  доказательством корректного A/V placement: overwrite может заменить item без
  изменения количества.
- ProjectItem sync-секвенции обычно вставляется как nested sequence. Если
  источником является настоящая Multi-Camera Source Sequence, Premiere обычно
  сохраняет multicam-поведение nested clip, но это зависит от типа исходника и
  настроек Premiere. Обычная sync-секвенция с двумя камерами автоматически не
  становится переключаемым multicam clip.
- Public ExtendScript API не даёт надёжного кросс-версионного способа включить
  multicam для уже вставленного обычного nested clip; QE DOM намеренно не
  используется.
- Fallback-вставка не атомарна: при редкой ошибке может появиться только video
  или audio. Панель возвращает warning, после которого автоматическую пакетную
  сборку запускать нельзя.
- Encoder job ID означает принятие задания, а не успешное завершение. CEP API
  не даёт одинаково надёжного callback во всех целевых версиях, поэтому панель
  проверяет стабильность файла. Финальную проверку WAV/длительности должен
  выполнить backend перед Whisper.

## Batch conform

Кнопка «Применить подтверждённые СНХ» загружает full timeline, выбирает
сегменты `snh`/`host` со статусом `auto_matched` или `confirmed`, валидными
sequence ID/name и source range, сортирует их по `start_sec` и вызывает одну
host-команду. Каждый диапазон вставляется на V1/A1; точки source project item
восстанавливаются в `finally`.

Host останавливается при первой ошибке или если Premiere не показывает обе
части A/V после placement, и возвращает отчёт `requested/completed/errors`.
Исходные sync-секвенции не редактируются. Целевая Rough Cut секвенция должна
быть активной до нажатия кнопки. Панель инспектирует её непосредственно перед
запросом, а host повторно проверяет target ID и путь открытого Premiere-проекта.
После каждой вставки создаётся или обновляется маркер `RC_SYNC`.

## Проверка batch conform

1. Batch conform заменяет плейсхолдеры только для `auto_matched`/`confirmed`.
2. Жёлтые и ненайденные СНХ остаются плейсхолдерами для ручной проверки.
3. Host проверяет обе части A/V, останавливается на первой ошибке и возвращает
   полный отчёт.
4. Исходные sync-секвенции остаются без изменений.

Batch conform следует запускать на копии Rough Cut:
публичный ExtendScript API не предоставляет надёжной транзакции или общего undo
для серии вставок.
