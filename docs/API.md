# API (Flask)

[Начало работы](../README.md) · [Настройки](CONFIGURATION.md)

## API (Flask)

Запуск (`.env` загружается автоматически через `python-dotenv`):

```bash
export PYTHONPATH=src
flask --app api run --host 0.0.0.0 --port 8000
```

Все маршруты, кроме минимального `GET /health`, требуют токен. Браузер покажет
стандартный Basic Auth prompt: имя пользователя произвольное, пароль — значение
`ROUGH_CUT_API_TOKEN`. Для CLI используйте `Authorization: Bearer`:

```bash
curl -H "Authorization: Bearer $ROUGH_CUT_API_TOKEN" http://localhost:8000/stage1/status?project=my_project
```

Эндпоинты (таблица генерируется из `api.app.url_map`; не редактируйте её
вручную, обновляйте командой `scripts/gen-api-routes --write`, а
`scripts/gen-api-routes --check` и тест `tests/test_api_route_contract.py`
ловят расхождение):

<!-- api-routes:begin -->
| Метод | Путь | Endpoint |
| --- | --- | --- |
| GET | `/` | `project_home` |
| GET | `/health` | `health` |
| GET | `/project/<project>` | `project_dashboard` |
| GET, POST | `/projects` | `create_project`, `list_projects` |
| DELETE | `/projects/<project>` | `delete_project` |
| GET | `/speaker/<project>/<path:filename>` | `speaker_file` |
| POST | `/stage1/analyze` | `stage1_analyze` |
| GET, PUT | `/stage1/normalization` | `stage1_normalization_get`, `stage1_normalization_save` |
| GET | `/stage1/normalization-jobs/<job_id>` | `stage1_normalization_job_get` |
| POST | `/stage1/normalization-jobs/start` | `stage1_normalization_job_start` |
| GET | `/stage1/normalization-jobs/status` | `stage1_normalization_job_status` |
| POST | `/stage1/normalization/confirm` | `stage1_normalization_confirm` |
| POST | `/stage1/normalization/confirm/preview` | `stage1_normalization_learning_preview` |
| POST | `/stage1/normalization/generate` | `stage1_normalization_generate` |
| GET | `/stage1/normalization/history` | `stage1_normalization_history` |
| GET | `/stage1/normalization/history/<name>/diff` | `stage1_normalization_history_diff` |
| POST | `/stage1/normalization/history/<name>/restore` | `stage1_normalization_history_restore` |
| GET, POST | `/stage1/normalization/memory` | `stage1_normalization_memory_create`, `stage1_normalization_memory_get` |
| DELETE, PATCH | `/stage1/normalization/memory/<int:rule_id>` | `stage1_normalization_memory_delete`, `stage1_normalization_memory_update` |
| POST | `/stage1/normalization/reset` | `stage1_normalization_reset` |
| GET | `/stage1/openrouter-usage` | `stage1_openrouter_usage` |
| GET | `/stage1/recorded-voiceover` | `recorded_voiceover_state` |
| POST | `/stage1/recorded-voiceover/apply` | `recorded_voiceover_apply` |
| GET | `/stage1/recorded-voiceover/audio` | `recorded_voiceover_audio` |
| PATCH | `/stage1/recorded-voiceover/blocks/<block_uid>/voice-source` | `recorded_voiceover_voice_source` |
| GET | `/stage1/recorded-voiceover/jobs/<job_id>` | `recorded_voiceover_job_status` |
| POST | `/stage1/recorded-voiceover/jobs/<job_id>/cancel` | `recorded_voiceover_job_cancel` |
| POST | `/stage1/recorded-voiceover/jobs/start` | `recorded_voiceover_job_start` |
| PATCH | `/stage1/recorded-voiceover/ranges/<block_uid>` | `recorded_voiceover_range_patch` |
| POST | `/stage1/recorded-voiceover/scan` | `recorded_voiceover_scan` |
| POST | `/stage1/recorded-voiceover/source` | `recorded_voiceover_source` |
| GET | `/stage1/status` | `stage1_status` |
| POST | `/stage1/tts` | `stage1_tts` |
| GET | `/stage1/tts-jobs/<job_id>` | `stage1_tts_job_status` |
| POST | `/stage1/tts-jobs/<job_id>/cancel` | `stage1_tts_job_cancel` |
| POST | `/stage1/tts-jobs/start` | `stage1_tts_job_start` |
| GET | `/stage1/tts-jobs/status` | `stage1_tts_job_status_alias` |
| POST | `/stage1/tts-phrase` | `stage1_tts_phrase` |
| POST | `/stage1/tts/clear` | `stage1_tts_clear` |
| POST | `/stage1/validate` | `stage1_validate` |
| POST | `/stage2/build` | `stage2_build` |
| GET | `/stage2/export/xml` | `stage2_export_xml` |
| POST | `/stage2/final-review/estimate` | `final_review_estimate` |
| GET | `/stage2/final-review/jobs/<job_id>` | `final_review_status` |
| POST | `/stage2/final-review/jobs/<job_id>/apply` | `final_review_apply` |
| POST | `/stage2/final-review/jobs/<job_id>/cancel` | `final_review_cancel` |
| GET | `/stage2/final-review/latest` | `final_review_latest` |
| POST | `/stage2/final-review/start` | `final_review_start` |
| GET | `/stage2/sync-jobs/<job_id>` | `stage2_sync_job_status` |
| POST | `/stage2/sync-jobs/<job_id>/cancel` | `stage2_sync_job_cancel` |
| POST | `/stage2/sync-jobs/start` | `stage2_sync_job_start` |
| GET | `/stage2/sync-jobs/status` | `stage2_sync_job_status_alias` |
| GET, PATCH | `/stage2/sync-matches` | `stage2_sync_matches` |
| PATCH | `/stage2/sync-matches/<block_uid>` | `stage2_sync_matches` |
| DELETE, GET, POST | `/stage2/sync-sources` | `stage2_sync_sources` |
| GET, PUT | `/stage2/sync-sources/<source_id>/audio` | `stage2_sync_source_audio` |
| PUT | `/stage2/sync-sources/<source_id>/words` | `stage2_sync_source_words` |
| GET | `/stage2/timeline` | `stage2_timeline` |
| GET | `/stage3/builds` | `stage3_builds_history` |
| GET | `/stage3/export/xml` | `stage3_export_xml` |
| GET | `/stage3/jobs/<job_id>` | `stage3_job_status` |
| POST | `/stage3/jobs/prepare/start` | `stage3_prepare_job_start` |
| POST | `/stage3/jobs/scan/start` | `stage3_scan_job_start` |
| GET | `/stage3/jobs/status` | `stage3_job_status_alias` |
| POST | `/stage3/media/scan` | `stage3_media_scan` |
| GET | `/stage3/plan` | `stage3_plan_check` |
| GET | `/stage3/status` | `stage3_status` |
| POST | `/stage3/timeline/prepare` | `stage3_timeline_prepare` |
| GET | `/stage4/diffs/<diff_id>` | `stage4_diff_get` |
| GET | `/stage4/inbox` | `stage4_inbox` |
| GET | `/stage4/revisions` | `stage4_revisions_list` |
| POST | `/stage4/revisions/<revision_id>/activate` | `stage4_revision_activate` |
| POST | `/stage4/revisions/<revision_id>/reconcile` | `stage4_revision_reconcile` |
| POST | `/stage4/revisions/import` | `stage4_revision_import` |
| GET | `/system/readiness` | `system_readiness` |
<!-- api-routes:end -->

Долгие операции (нормализация, TTS, sync, Stage 3, финальная проверка)
запускаются асинхронно через `.../start`; статус читается по
`.../<job_id>` или `.../status?project=<project>` (после рестарта показывается
последний checkpoint), остановка — `.../<job_id>/cancel` (после текущего
внешнего вызова; sync сохраняет готовые индексы и совпадения). Синхронные
`POST /stage3/media/scan` и `POST /stage3/timeline/prepare` — fallback.

Последний статус TTS и sync атомарно сохраняется в каталоге `work` проекта.
После рестарта незавершённая задача показывается как `interrupted`: повторный
запуск TTS пропускает WAV с тем же fingerprint, а sync повторно использует
готовые Whisper-индексы и сохранённые совпадения, если fingerprint всех входов
не изменился. Завершённые in-memory статусы удаляются через 24 часа.

Ограничение: публичный API `faster-whisper` не экспортирует состояние decoder/VAD,
необходимое для точного продолжения внутри одного WAV. При остановке сохраняются
полностью готовые источники; текущий незавершённый аудиофайл распознаётся заново.
Резюм через `clip_timestamps` намеренно не используется, поскольку он меняет VAD
и контекст декодирования и может дать пропуски либо дубли на границе.

LLM-нормализация сохраняет успешные batch-решения в
`work/normalization_llm_cache.json`. Повторный запуск после сбоя запрашивает
только недостающие batch; изменение текста, правил, модели или prompt инвалидирует
соответствующий кеш.

## Финальная проверка

Контракты оценки, отмены, восстановления и применения приведены в
[настройках финальной проверки](CONFIGURATION.md#финальная-проверка-стоимость-и-восстановление).
