# Развёртывание

[Браузерный быстрый старт](../README.md) · [Настройки](CONFIGURATION.md)

## Docker

Контейнер слушает `0.0.0.0:8000` — у приложения нет TLS, только токен
(`ROUGH_CUT_API_TOKEN`). Не публикуй порт 8000 напрямую в интернет: для
локальной работы обращайся к нему только с `127.0.0.1` этой же машины, а для
доступа снаружи ставь перед контейнером reverse proxy с TLS (nginx/Caddy и
т.п.).

### Быстрое развёртывание на другой машине

Linux, Windows/WSL2, Intel Mac и универсальный CPU-вариант для Apple Silicon:

```bash
git clone git@github.com:zabreiki-blip/rough_cut_v3.git
cd rough_cut_v3
scripts/bootstrap-docker
```

Скрипт проверяет Docker Compose v2, создаёт `.env` с новым локальным API token,
создаёт каталоги данных, собирает образ и ждёт успешный `/health`. Затем заполните
в `.env` ключи используемых LLM/TTS-провайдеров. Каталоги `projects/` и `data/`
примонтированы с хоста и переживают пересборку контейнера; кеш Whisper хранится
в named volume. Образ собирается под нативную архитектуру Docker host без
жёсткой привязки к `amd64` или `arm64`.

```bash
docker compose ps
docker compose logs --tail=100 stage1-api
```

Для установки панели на другом Mac:

```bash
scripts/install-cep-macos
```

После этого в CEP-настройках укажите каталог `projects` этой машины, URL backend
и новый `ROUGH_CUT_API_TOKEN` из её `.env`.

### Ресурсы и права на `projects/`, `data/`

Контейнер ограничен `mem_limit: ${ROUGH_CUT_MEM_LIMIT:-8g}` и
`cpus: ${ROUGH_CUT_CPUS:-0}` (переопределяются в `.env`). Whisper medium int8
на CPU занимает ≈ 2–3 ГБ; остальное — запас под ffmpeg и WAV-буферы.
`ROUGH_CUT_CPUS=0` — без лимита CPU. **Предупреждение:** ненулевое значение не
может превышать число CPU, доступных Docker (Docker Desktop: Settings > Resources;
часто это 4). Иначе `docker compose up` падает с ошибкой «range of CPUs is from
0.01 to N». `SYNC_WHISPER_CPU_THREADS` (по умолчанию 8) — отдельная настройка
числа потоков Whisper, она не задаёт лимит контейнера. На Docker Desktop лимит
памяти работает внутри VM: значение выше памяти VM не вредит, но реально доступна
только память VM.

Процесс в образе работает под `roughcut` (uid/gid 10001). На macOS (Docker
Desktop) владелец bind-mount транслируется автоматически. На Linux каталоги
`projects/` и `data/` должны быть доступны на запись uid 10001, иначе API не
сможет сохранять проекты. `scripts/bootstrap-docker` проверяет это и печатает
подсказку (sudo сам не вызывает):

```bash
sudo chown -R 10001:10001 projects data
# или, сохранив владельца-хоста (нужен пакет acl):
sudo setfacl -R -m u:10001:rwX -d -m u:10001:rwX projects data
```

Не меняйте `user:` в `docker-compose.yml` на uid хоста: /app и named volume
`whisper_cache` принадлежат 10001, и запись кэша моделей сломается. При rootless
Docker или `userns-remap` uid смещается — подберите владельца по subuid-диапазону.

### Apple Silicon M1–M4: Metal/MLX

Docker Desktop работает внутри Linux VM и не передаёт Apple Metal контейнеру.
Для ускорения на процессорах M-серии запускайте API нативно:

```bash
brew install python@3.11 ffmpeg catdoc
PYTHON_BIN=python3.11 ./scripts/setup-local
./scripts/run-local
```

Единая пара setup/run автоматически выбирает MLX на нативном arm64
(`.venv-macos-arm64`); CPU и Rosetta используют `.venv`. MLX поддерживает
Python 3.11/3.12; CPU — 3.11. Старые `scripts/setup-macos-apple-silicon` и
`scripts/run-native-macos` оставлены для совместимости. Для явного CPU используйте
`ROUGH_CUT_PROFILE=cpu` перед обеими командами; `ROUGH_CUT_VENV` переопределяет
путь окружения. См. [профили](CONFIGURATION.md#профиль-локальной-установки).

Профиль использует закреплённый `mlx-whisper==0.4.3`. Модель `medium` преобразуется адаптером в `mlx-community/whisper-medium-mlx`
и скачивается при первом распознавании. MLX поддерживает word timestamps;
модель не входит в установку, для скачивания нужны сеть и место на диске.

Обычный Docker-режим на M‑серии тоже поддерживается, но использует CPU
`faster-whisper`; он переносимее, однако медленнее нативного MLX.

### Ручной Docker-запуск

```bash
docker compose build
docker compose up -d
curl http://localhost:8000/health
```

Пример запроса:

```bash
curl -X POST http://localhost:8000/stage1/analyze \
  -H "Authorization: Bearer $ROUGH_CUT_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"project":"my_project","input_path":"projects/my_project/input/scenario.docx","use_llm":true}'
```

`/stage1/analyze` оставлен только для legacy CLI/API и помечен deprecated.
Новый веб-workflow запускает фоновую задачу через
`/stage1/normalization-jobs/start`; синхронный `/stage1/normalization/generate`
сохранён для совместимости и тестовых интеграций.

## macOS Connector

Настройки сервиса и требования к токену: [CONFIGURATION.md](CONFIGURATION.md#macos-connector-для-docker-backend).

## Дополнительный CEP-путь (заморожен)

## Stage 2.1: автоматическое подключение синхронов

Готовые синхронизированные секвенции Premiere являются источником координат.
Видео остаётся в Premiere и не загружается в backend.

Для headless-монтажа по отрендеренным просмотровкам Docker автоматически читает
`projects/<project>/media/reviews`, создаёт заменяемые `RC_SRC_*` sequences и
готовый nested XML. Premiere нужен только для импорта результата.

1. Установить development CEP-панель по инструкции в [cep/README.md](../cep/README.md).
2. Открыть sync-секвенцию и нажать «Использовать активную».
3. Указать одного или нескольких спикеров через запятую и стабильный ASCII
   Source ID. Если секвенция общая, включить «все спикеры проекта».
4. Нажать «Экспортировать звук и запустить распознавание». Панель:
   - экспортирует полный WAV через Adobe Media Encoder;
   - регистрирует sequence ID/name;
   - ждёт готовности WAV;
   - запускает фоновое распознавание и сопоставление.
5. Во вкладке «Сборка» прослушать жёлтые совпадения, выбрать альтернативу или
   поправить `source in/out`, затем подтвердить.
6. Нажать Build Timeline и импортировать XML в Premiere.
7. Сделать Rough Cut активным и в CEP нажать
   «Применить подтверждённые СНХ».

Артефакты хранятся отдельно от подтверждённого сценария:

```text
projects/<project>/work/sync_sources.json
projects/<project>/work/sync_audio/<source_id>.wav
projects/<project>/work/transcripts/<source_id>.words.json
projects/<project>/work/sync_matches.json
projects/<project>/work/full_timeline.json
```

В автоматическую сборку идут только `auto_matched` и `confirmed`. Результаты
`needs_review` и `not_found` остаются плейсхолдерами. Последовательный экспорт
нескольких секвенций не перетирает предыдущие результаты: поиск выполняется по
общему пулу готовых word indexes.

Один и тот же участок исходного синхрона можно использовать в нескольких местах
монтажа — например, короткую часть ответа в анонсе, а полный ответ позднее.
Совпадения различаются по `block_uid`/`segment_id`, поэтому одинаковые и
пересекающиеся диапазоны `source in/out` намеренно не дедуплицируются.

Docker устанавливает `faster-whisper` и хранит модели в named volume
`whisper_cache`. При локальном запуске на нативном Apple Silicon setup устанавливает
`mlx-whisper`; адаптер выбирается автоматически.

Изолированный Whisper worker переиспользует загруженную модель между
последовательными заданиями и автоматически пересоздаётся после ошибки или
таймаута. Word index повторно используется только при совпадении сигнатуры WAV,
конфигурации, платформы и версий backend; новый индекс атомарно заменяет старый
только после успешного распознавания.

Одновременно выполняется не более `ROUGH_CUT_MAX_ASR_JOBS` (по умолчанию 1;
мусор или значение < 1 → 1) распознаваний на весь процесс — и для sync, и для
финальной проверки, локальных и облачных. Лишние задания ждут слот (статус
`Waiting for an ASR slot`, поле `asr_waiting`) и отменяются без запуска ASR;
слот держится только на время самого распознавания. Локальный Whisper при этом
всё равно сериализован одним worker-процессом.
