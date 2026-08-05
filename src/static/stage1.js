(function () {
  const project = window.__PROJECT_NAME__ || "default";
  const defaults = window.__STAGE1_DEFAULTS__ || {};
  const initialStatus = window.__STAGE1_STATUS__ || {};

  const el = (id) => document.getElementById(id);
  const ids = {
    input_path: el("input_path"),
    prepared_path: el("prepared_path"),
    raw_output_path: el("raw_output_path"),
    speaker_dir: el("speaker_dir"),
    manifest_path: el("manifest_path"),
    provider: el("provider"),
    gender: el("gender"),
    voice_name: el("voice_name"),
    phrase_text: el("phrase_text"),
    phrase_file_name: el("phrase_file_name"),
    phrase_player: el("phrase_player"),
    use_llm: el("use_llm"),
    dry_run: el("dry_run"),
    btn_analyze: el("btn_analyze"),
    btn_tts: el("btn_tts"),
    btn_tts_cancel: el("btn_tts_cancel"),
    btn_phrase_tts: el("btn_phrase_tts"),
    btn_validate: el("btn_validate"),
    btn_run_all: el("btn_run_all"),
    btn_refresh: el("btn_refresh"),
    btn_clear_log: el("btn_clear_log"),
    m_prepared: el("m_prepared"),
    m_wav: el("m_wav"),
    m_missing: el("m_missing"),
    m_progress: el("m_progress"),
    f_llm: el("f_llm"),
    f_manual: el("f_manual"),
    f_manifest: el("f_manifest"),
    progress_bar: el("progress_bar"),
    live_job_status: el("live_job_status"),
    live_event: el("live_event"),
    live_current: el("live_current"),
    live_retries: el("live_retries"),
    live_done: el("live_done"),
    live_error: el("live_error"),
    speaker_items: el("speaker_items"),
    speaker_hint: el("speaker_hint"),
    log_box: el("log_box"),
    step_analyze: el("step_analyze"),
    step_tts: el("step_tts"),
    step_validate: el("step_validate"),
    speed: el("speed"),
    speed_val: el("speed_val"),
    btn_build_timeline: el("btn_build_timeline"),
    btn_export_xml: el("btn_export_xml"),
    timeline_stats: el("timeline_stats"),
    ts_zk: el("ts_zk"),
    ts_snh: el("ts_snh"),
    ts_dur: el("ts_dur"),
    btn_sync_align: el("btn_sync_align"),
    btn_sync_cancel: el("btn_sync_cancel"),
    sync_sources_count: el("sync_sources_count"),
    sync_sources_names: el("sync_sources_names"),
    sync_job_status: el("sync_job_status"),
    sync_job_progress: el("sync_job_progress"),
    sync_job_message: el("sync_job_message"),
    sync_job_numbers: el("sync_job_numbers"),
    sync_progress_bar: el("sync_progress_bar"),
    sync_job_current: el("sync_job_current"),
    sync_job_video_progress: el("sync_job_video_progress"),
    sync_job_elapsed: el("sync_job_elapsed"),
    sync_job_eta: el("sync_job_eta"),
    sync_count_auto: el("sync_count_auto"),
    sync_count_confirmed: el("sync_count_confirmed"),
    sync_count_review: el("sync_count_review"),
    sync_count_unmatched: el("sync_count_unmatched"),
    sync_review_count: el("sync_review_count"),
    sync_review_list: el("sync_review_list"),
    sync_timecode_mode: el("sync_timecode_mode"),
    sync_whisper_model: el("sync_whisper_model"),
    normalization_input_path: el("normalization_input_path"),
    normalization_hint: el("normalization_hint"),
    normalization_use_llm: el("normalization_use_llm"),
    normalization_status: el("normalization_status"),
    normalization_warnings: el("normalization_warnings"),
    normalization_empty: el("normalization_empty"),
    normalization_blocks: el("normalization_blocks"),
    btn_normalize: el("btn_normalize"),
    btn_reset_normalization: el("btn_reset_normalization"),
    btn_save_normalization: el("btn_save_normalization"),
    btn_confirm_normalization: el("btn_confirm_normalization"),
    btn_add_normalization_block: el("btn_add_normalization_block"),
    norm_count_total: el("norm_count_total"),
    norm_count_voiceover: el("norm_count_voiceover"),
    norm_count_sync: el("norm_count_sync"),
    norm_count_unknown: el("norm_count_unknown"),
    memory_alias: el("memory_alias"),
    memory_type: el("memory_type"),
    memory_search: el("memory_search"),
    memory_filter_type: el("memory_filter_type"),
    memory_filter_status: el("memory_filter_status"),
    memory_rules: el("memory_rules"),
    btn_add_memory: el("btn_add_memory"),
    learning_dialog: el("normalization_learning_dialog"),
    learning_rule_list: el("learning_rule_list"),
    btn_cancel_learning: el("btn_cancel_learning"),
    btn_confirm_with_learning: el("btn_confirm_with_learning"),
    voiceover_lock: el("voiceover_lock"),
    assembly_lock: el("assembly_lock"),
    assembly_gate_message: el("assembly_gate_message"),
    stage3_media_directory: el("stage3_media_directory"),
    btn_stage3_media_scan: el("btn_stage3_media_scan"),
    btn_stage3_prepare: el("btn_stage3_prepare"),
    stage3_media_status: el("stage3_media_status"),
    stage3_media_count: el("stage3_media_count"),
    stage3_media_message: el("stage3_media_message"),
    stage3_media_list: el("stage3_media_list"),
    stage3_source_mapping: el("stage3_source_mapping"),
  };

  const toastContainer = document.getElementById("toast-container");

  let busy = false;
  let normalization = null;
  let memoryRules = [];
  let normalizationDirty = false;
  let normalizationEligible = initialStatus.normalization_confirmed !== false;
  let syncFeatureAvailable = null;
  let syncSources = [];
  let syncMatches = [];
  let syncMatchesRevision = 0;
  let syncJobRunning = false;
  let currentSyncJobId = "";
  let currentSyncJobStatus = "";
  let syncPollTimer = null;
  let currentTtsJobId = "";
  let currentTtsJobStatus = "";
  let ttsPollTimer = null;
  let ttsPollFailures = 0;
  let syncPollFailures = 0;
  let stage3MediaScanAttempted = false;
  let stage3MediaScanning = false;
  let stage3MediaCount = 0;
  let stage3SourceMapping = [];
  const MAX_POLL_BACKOFF_MS = 10000;
  const live = {
    status: "idle",
    event: "\u2014",
    current: "\u2014",
    retries: 0,
    done: 0,
    total: 0,
    error: "",
  };

  function now() {
    return new Date().toLocaleTimeString("ru-RU");
  }

  function toast(message, type) {
    if (!toastContainer) return;
    const t = document.createElement("div");
    t.className = "toast " + (type || "");
    t.textContent = message;
    toastContainer.appendChild(t);
    setTimeout(() => {
      t.style.opacity = "0";
      t.style.transform = "translateX(20px)";
      t.style.transition = "all 200ms";
      setTimeout(() => t.remove(), 200);
    }, 3500);
  }

  function appendLog(message, level) {
    const resolvedLevel = level === true ? "error" : level;
    const tag = resolvedLevel === "error" ? "ERR" : resolvedLevel === "warn" ? "WARN" : "INFO";
    const line = `[${now()}] [${tag}] ${message}`;
    ids.log_box.textContent += `${line}\n`;
    ids.log_box.scrollTop = ids.log_box.scrollHeight;
    if (resolvedLevel === "error") {
      ids.log_box.classList.add("log-error");
      setTimeout(() => ids.log_box.classList.remove("log-error"), 500);
    }
  }

  function setStepState(stepEl, state) {
    if (stepEl) stepEl.dataset.state = state;
  }

  function eventTitle(event) {
    const map = {
      queued: "в очереди",
      started: "старт",
      item_started: "синтез блока",
      item_generated: "блок готов",
      item_skipped: "пропущен",
      item_retrying: "повтор",
      cancelling: "останавливается",
      cancelled: "прервано",
      interrupted: "прервано",
      completed: "завершено",
      failed: "ошибка",
    };
    return map[event] || event || "\u2014";
  }

  function renderLive() {
    if (!ids.live_job_status) return;
    ids.live_job_status.dataset.state = live.status || "idle";
    ids.live_job_status.textContent = live.status || "idle";
    ids.live_event.textContent = eventTitle(live.event);
    ids.live_current.textContent = live.current || "\u2014";
    ids.live_retries.textContent = String(live.retries || 0);
    ids.live_done.textContent = `${live.done || 0}/${live.total || 0}`;
    ids.live_error.textContent = live.error ? `${live.error}` : "\u2014";
    ids.live_error.classList.toggle("has-error", Boolean(live.error));
  }

  function setLive(patch) {
    Object.assign(live, patch || {});
    renderLive();
  }

  function setBusy(state) {
    busy = state;
    [ids.btn_analyze, ids.btn_tts, ids.btn_phrase_tts, ids.btn_validate, ids.btn_run_all, ids.btn_build_timeline,
      ids.btn_sync_align,
      ids.btn_normalize, ids.btn_save_normalization, ids.btn_confirm_normalization, ids.btn_add_normalization_block,
      ids.btn_reset_normalization,
      ids.btn_add_memory, ids.btn_stage3_media_scan].forEach((b) => {
      if (b) b.disabled = state;
    });
    document.querySelectorAll("[data-action='confirm-sync-match']").forEach((button) => {
      button.disabled = state;
    });
    if (!state) updateActionAvailability();
  }

  function isNormalizationConfirmed() {
    return Boolean(normalizationEligible && normalization && String(normalization.status || "").toLowerCase() === "confirmed");
  }

  function updateActionAvailability() {
    if (busy) return;
    const hasDraft = Boolean(
      normalization && Array.isArray(normalization.blocks) && normalization.blocks.length > 0
    );
    const confirmed = isNormalizationConfirmed();
    const blocked = !confirmed;
    if (ids.btn_save_normalization) ids.btn_save_normalization.disabled = !hasDraft || !normalizationDirty;
    if (ids.btn_reset_normalization) ids.btn_reset_normalization.disabled = !hasDraft;
    if (ids.btn_confirm_normalization) {
      ids.btn_confirm_normalization.disabled = !hasDraft || (confirmed && !normalizationDirty);
    }
    if (ids.btn_add_normalization_block) ids.btn_add_normalization_block.disabled = !hasDraft;
    [ids.btn_tts, ids.btn_validate, ids.btn_run_all, ids.btn_build_timeline].forEach((button) => {
      if (button) button.disabled = blocked;
    });
    if (ids.btn_tts && ["queued", "running", "cancelling"].includes(currentTtsJobStatus)) {
      ids.btn_tts.disabled = true;
    }
    if (ids.btn_sync_align) {
      ids.btn_sync_align.disabled = blocked || syncJobRunning
        || syncFeatureAvailable !== true || syncSources.length === 0;
      ids.btn_sync_align.textContent = ["interrupted", "cancelled", "failed"].includes(currentSyncJobStatus)
        ? "Продолжить сопоставление"
        : "Найти СНХ автоматически";
    }
    if (ids.btn_sync_cancel) ids.btn_sync_cancel.hidden = !syncJobRunning;
    if (ids.sync_whisper_model) ids.sync_whisper_model.disabled = syncJobRunning;
    if (ids.btn_tts_cancel) {
      ids.btn_tts_cancel.hidden = !["queued", "running", "cancelling"].includes(currentTtsJobStatus);
    }
    if (ids.btn_tts) {
      ids.btn_tts.textContent = ["interrupted", "cancelled", "failed"].includes(currentTtsJobStatus)
        ? "Продолжить озвучку"
        : "TTS";
    }
    document.querySelectorAll("[data-action='confirm-sync-match']").forEach((button) => {
      button.disabled = blocked || syncJobRunning;
    });
    if (ids.voiceover_lock) ids.voiceover_lock.hidden = !blocked;
    if (ids.assembly_lock) ids.assembly_lock.hidden = !blocked;
    if (ids.assembly_gate_message) ids.assembly_gate_message.hidden = !blocked;
    if (blocked && ids.btn_export_xml) {
      ids.btn_export_xml.style.pointerEvents = "none";
      ids.btn_export_xml.style.opacity = ".4";
    }
  }

  function formData() {
    return {
      input_path: ids.input_path.value.trim(),
      prepared_path: ids.prepared_path.value.trim(),
      raw_output_path: ids.raw_output_path.value.trim(),
      speaker_dir: ids.speaker_dir.value.trim(),
      manifest_path: ids.manifest_path.value.trim(),
      provider: ids.provider.value.trim(),
      gender: ids.gender.value.trim(),
      voice_name: ids.voice_name.value.trim(),
      phrase_text: ids.phrase_text.value,
      phrase_file_name: ids.phrase_file_name.value.trim(),
      use_llm: ids.use_llm ? ids.use_llm.checked : false,
      dry_run: ids.dry_run.checked,
      speed: parseFloat(ids.speed.value),
    };
  }

  function isLikelySafePath(pathValue) {
    if (!pathValue) return false;
    if (pathValue.includes("\0")) return false;
    if (pathValue.includes("..")) return false;
    return true;
  }

  function assertRequiredPath(pathValue, label) {
    const value = (pathValue || "").trim();
    if (!value) throw new Error(`${label} is required`);
    if (!isLikelySafePath(value)) throw new Error(`${label} looks invalid: ${value}`);
  }

  async function postJSON(url, payload) {
    const response = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok || data.ok === false) {
      throw new Error(data.error || JSON.stringify(data));
    }
    return data.result || {};
  }

  async function getJSON(url) {
    const response = await fetch(url);
    const data = await response.json().catch(() => ({}));
    if (!response.ok || data.ok === false) {
      const error = new Error(data.error || JSON.stringify(data));
      error.status = response.status;
      throw error;
    }
    return data.result || {};
  }

  async function requestJSON(url, method, payload) {
    const options = { method, headers: { "Content-Type": "application/json" } };
    if (payload !== undefined) options.body = JSON.stringify(payload);
    const response = await fetch(url, options);
    const raw = await response.text();
    let data = {};
    try {
      data = raw ? JSON.parse(raw) : {};
    } catch (_err) {
      data = {};
    }
    if (!response.ok || data.ok === false) {
      const plainText = raw && !raw.trimStart().startsWith("<") ? raw.trim().slice(0, 500) : "";
      const detail = data.message || data.error || plainText;
      throw new Error(detail || `HTTP ${response.status}: сервер прервал запрос без JSON-ответа`);
    }
    if (raw && Object.keys(data).length === 0) {
      throw new Error(`HTTP ${response.status}: сервер вернул некорректный JSON`);
    }
    return data.result !== undefined ? data.result : data;
  }

  async function optionalRequestJSON(url, method, payload) {
    const options = { method: method || "GET", headers: { "Content-Type": "application/json" } };
    if (payload !== undefined) options.body = JSON.stringify(payload);
    const response = await fetch(url, options);
    if (response.status === 404) return { available: false, result: null };
    const raw = await response.text();
    let data = {};
    try {
      data = raw ? JSON.parse(raw) : {};
    } catch (_err) {
      data = {};
    }
    if (!response.ok || data.ok === false) {
      const plainText = raw && !raw.trimStart().startsWith("<") ? raw.trim().slice(0, 500) : "";
      const error = new Error(data.message || data.error || plainText || `HTTP ${response.status}`);
      error.status = response.status;
      throw error;
    }
    return {
      available: true,
      result: data.result !== undefined ? data.result : data,
    };
  }

  function isTransientPollingError(error) {
    const status = Number((error && error.status) || 0);
    return status === 0 || status === 408 || status === 429 || status >= 500;
  }

  function pollingBackoffMs(failures, baseDelay) {
    const exponent = Math.min(4, Math.max(0, Number(failures || 1) - 1));
    return Math.min(MAX_POLL_BACKOFF_MS, baseDelay * (2 ** exponent));
  }

  const blockTypeLabels = {
    voiceover: "Закадр", sync: "Синхрон", host: "Ведущий",
    technical: "Технический", other: "Прочее", unknown: "Не определён",
  };
  const sourceLabels = {
    builtin_rule: "правило", rule: "правило", memory: "память",
    llm: "LLM", manual: "исправлено вручную", correction: "ручное обучение",
    unknown: "не определено",
  };

  function escaped(value) {
    return String(value == null ? "" : value)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#039;");
  }

  function canonicalDocument(value) {
    if (!value) return null;
    const doc = value.normalization || value.document || value;
    if (!Array.isArray(doc.blocks)) doc.blocks = [];
    if (!doc.status) doc.status = "draft";
    if (doc.revision == null) doc.revision = 0;
    doc.warnings = Array.isArray(doc.warnings) ? doc.warnings : [];
    return doc;
  }

  function markNormalizationDirty() {
    if (!normalization) return;
    normalizationDirty = true;
    normalizationEligible = false;
    if (normalization.status === "confirmed") normalization.status = "draft";
    renderNormalizationStatus();
    updateActionAvailability();
  }

  function blockValue(block, ...names) {
    for (const name of names) if (block[name] != null) return block[name];
    return "";
  }

  function renderNormalizationStatus() {
    let state = normalization ? (normalization.status || "draft") : "empty";
    if (state === "confirmed" && !normalizationEligible) state = "stale";
    const labels = { empty: "не создан", draft: "черновик", confirmed: "подтверждён", stale: "нужно подтвердить заново" };
    ids.normalization_status.dataset.state = state;
    ids.normalization_status.textContent = labels[state] || state;
  }

  function renderNormalization() {
    renderNormalizationStatus();
    const blocks = normalization && Array.isArray(normalization.blocks) ? normalization.blocks : [];
    const counts = blocks.reduce((acc, block) => {
      const type = block.type || block.block_type || "unknown";
      acc[type] = (acc[type] || 0) + 1;
      return acc;
    }, {});
    ids.norm_count_total.textContent = String(blocks.length);
    ids.norm_count_voiceover.textContent = String(counts.voiceover || 0);
    ids.norm_count_sync.textContent = String(counts.sync || 0);
    ids.norm_count_unknown.textContent = String(counts.unknown || 0);

    const warnings = (normalization && normalization.warnings ? normalization.warnings : []).concat(
      blocks.flatMap((block) => Array.isArray(block.warnings) ? block.warnings : [])
    ).filter(Boolean);
    const warningGroups = new Map();
    warnings.forEach((warning) => {
      const raw = String(warning || "").trim();
      let message = raw;
      if (/^LLM classification failed:\s*JSONDecodeError/i.test(raw)) {
        message = "LLM вернул некорректный JSON — автоклассификация не применена";
      } else {
        const confidence = raw.match(/^LLM confidence\s+([^\s]+)\s+is below/i);
        if (confidence) {
          message = `Низкая уверенность LLM (${confidence[1]}) — требуется проверка`;
        }
      }
      warningGroups.set(message, (warningGroups.get(message) || 0) + 1);
    });
    const groupedWarnings = Array.from(warningGroups.entries());
    const visibleWarnings = groupedWarnings.slice(0, 8);
    ids.normalization_warnings.hidden = groupedWarnings.length === 0;
    ids.normalization_warnings.innerHTML = groupedWarnings.length
      ? `<strong>Требует внимания:</strong><br>${visibleWarnings.map(([message, count]) =>
          `${escaped(message)}${count > 1 ? ` <span class="warning-count">· ${count} блоков</span>` : ""}`
        ).join("<br>")}${groupedWarnings.length > visibleWarnings.length
          ? `<br>Ещё ${groupedWarnings.length - visibleWarnings.length} типов предупреждений`
          : ""}`
      : "";
    ids.normalization_empty.hidden = blocks.length > 0;
    ids.normalization_blocks.innerHTML = "";

    blocks.forEach((block, index) => {
      const type = block.type || block.block_type || "unknown";
      const source = block.source || block.decision_source || "unknown";
      const blockWarnings = Array.isArray(block.warnings) ? block.warnings : [];
      const syncFragments = Array.isArray(block.sync_fragments)
        ? block.sync_fragments.filter((fragment) => fragment && typeof fragment === "object")
        : [];
      const hasMultipleFragments = type === "sync" && syncFragments.length > 1;
      const card = document.createElement("article");
      card.className = `normalization-block${type === "unknown" || blockWarnings.length ? " has-warning" : ""}`;
      card.dataset.index = String(index);
      card.innerHTML = `
        <div class="block-head">
          <span class="block-order">#${index + 1}</span>
          <select data-field="type" aria-label="Тип блока">
            ${Object.entries(blockTypeLabels).map(([value, label]) => `<option value="${value}"${value === type ? " selected" : ""}>${label}</option>`).join("")}
          </select>
          <span class="decision-badge" data-source="${escaped(source)}">${escaped(sourceLabels[source] || source)}</span>
          <button class="btn btn-sm" data-action="remember-block" type="button" title="Добавить обозначение в память">Запомнить</button>
          <button class="btn btn-sm" data-action="move-up" type="button" title="Переместить выше"${index === 0 ? " disabled" : ""}>↑</button>
          <button class="btn btn-sm" data-action="move-down" type="button" title="Переместить ниже"${index === blocks.length - 1 ? " disabled" : ""}>↓</button>
          <button class="btn block-delete" data-action="delete-block" type="button" title="Удалить блок">×</button>
        </div>
        <div class="block-fields">
          <label class="field block-original"><span>Исходный текст</span><textarea data-field="original_text"${blockValue(block, "original_text", "text_original", "source_text") ? " readonly" : ""}>${escaped(blockValue(block, "original_text", "text_original", "source_text"))}</textarea></label>
          <label class="field"><span>Нормализованный текст</span><textarea data-field="normalized_text"${hasMultipleFragments ? " readonly title=\"Редактируйте отдельные фрагменты ниже\"" : ""}>${escaped(blockValue(block, "normalized_text", "text"))}</textarea></label>
          <label class="field"><span>Текст для TTS</span><textarea data-field="text_for_tts">${escaped(blockValue(block, "text_for_tts", "tts_text"))}</textarea></label>
        </div>
        <div class="block-meta">
          <label class="field"><span>Исходное обозначение</span><input data-field="original_label" value="${escaped(blockValue(block, "original_label", "original_marker", "marker"))}"></label>
          <label class="field"><span>Спикер</span><input data-field="speaker" value="${escaped(blockValue(block, "speaker", "speaker_name"))}"></label>
          <label class="field"><span>Роль / должность</span><input data-field="role" value="${escaped(blockValue(block, "role"))}"></label>
          <label class="field"><span>Таймкод начала</span><input data-field="timecode_start" value="${escaped(blockValue(block, "timecode_start", "timecode", "start_timecode"))}"${hasMultipleFragments ? " readonly title=\"Редактируйте таймкоды отдельных фрагментов ниже\"" : ""}></label>
          <label class="field"><span>Таймкод конца</span><input data-field="timecode_end" value="${escaped(blockValue(block, "timecode_end", "end_timecode"))}"${hasMultipleFragments ? " readonly title=\"Редактируйте таймкоды отдельных фрагментов ниже\"" : ""}></label>
          <label class="field"><span>Заметка</span><input data-field="notes" value="${escaped(blockValue(block, "notes"))}"></label>
        </div>
        ${hasMultipleFragments ? `<div class="sync-fragment-editor">
          <div class="sync-fragment-heading">Фрагменты спикера · ${syncFragments.length}</div>
          ${syncFragments.map((fragment, fragmentIndex) => `
            <div class="sync-fragment-row" data-fragment-index="${fragmentIndex}">
              <span class="sync-fragment-number">${fragmentIndex + 1}</span>
              <label class="field"><span>Начало</span><input data-fragment-field="timecode_start" value="${escaped(fragment.timecode_start || "")}"></label>
              <label class="field"><span>Конец</span><input data-fragment-field="timecode_end" value="${escaped(fragment.timecode_end || "")}"></label>
              <label class="field sync-fragment-text"><span>Текст фрагмента</span><textarea data-fragment-field="text">${escaped(fragment.text || "")}</textarea></label>
            </div>`).join("")}
        </div>` : ""}
        ${blockWarnings.length ? `<div class="block-warning">${blockWarnings.map(escaped).join(" · ")}</div>` : ""}`;
      ids.normalization_blocks.appendChild(card);
    });
    updateActionAvailability();
  }

  function updateBlockFromControl(control) {
    const card = control.closest(".normalization-block");
    if (!card || !normalization) return;
    const block = normalization.blocks[Number(card.dataset.index)];
    if (!block) return;
    if (control.dataset.fragmentField) {
      const fragmentIndex = Number(control.closest("[data-fragment-index]")?.dataset.fragmentIndex);
      const fragments = Array.isArray(block.sync_fragments) ? block.sync_fragments : [];
      const fragment = fragments[fragmentIndex];
      if (!fragment) return;
      fragment[control.dataset.fragmentField] = control.value;
      block.normalized_text = fragments.map((item) => String(item.text || "").trim())
        .filter(Boolean).join(" ");
      block.timecode_start = String(fragments[0]?.timecode_start || "");
      block.timecode_end = String(fragments[0]?.timecode_end || "");
      const normalizedControl = card.querySelector('[data-field="normalized_text"]');
      const startControl = card.querySelector('[data-field="timecode_start"]');
      const endControl = card.querySelector('[data-field="timecode_end"]');
      if (normalizedControl) normalizedControl.value = block.normalized_text;
      if (startControl) startControl.value = block.timecode_start;
      if (endControl) endControl.value = block.timecode_end;
      markNormalizationDirty();
      return;
    }
    block[control.dataset.field] = control.value;
    if (Array.isArray(block.sync_fragments) && block.sync_fragments.length === 1) {
      if (control.dataset.field === "normalized_text") {
        block.sync_fragments[0].text = control.value;
      } else if (control.dataset.field === "timecode_start") {
        block.sync_fragments[0].timecode_start = control.value;
      } else if (control.dataset.field === "timecode_end") {
        block.sync_fragments[0].timecode_end = control.value;
      }
    }
    if (control.dataset.field === "type") block.block_type = control.value;
    if (["type", "original_label"].includes(control.dataset.field)) {
      block.source = "manual";
      block.decision_source = "manual";
    }
    if (control.dataset.field === "type") {
      block.warnings = [];
      if (control.value === "voiceover" && !block.text_for_tts) {
        block.text_for_tts = block.normalized_text || "";
      }
    }
    markNormalizationDirty();
    if (control.dataset.field === "type") renderNormalization();
  }

  async function loadNormalization(quiet) {
    try {
      const result = await requestJSON(`/stage1/normalization?project=${encodeURIComponent(project)}`, "GET");
      normalization = canonicalDocument(result);
      normalizationDirty = false;
      renderNormalization();
    } catch (err) {
      if (!quiet && !String(err.message).includes("404")) throw err;
      normalization = null;
      renderNormalization();
    }
  }

  async function generateNormalization() {
    const inputPath = ids.normalization_input_path.value.trim();
    assertRequiredPath(inputPath, "input_path");
    appendLog(`Нормализация: ${inputPath}`);
    const started = await requestJSON("/stage1/normalization-jobs/start", "POST", {
      project, input_path: inputPath, hint: ids.normalization_hint.value.trim(),
      use_llm: ids.normalization_use_llm.checked,
    });
    const jobId = String(started.id || "").trim();
    if (!jobId) throw new Error("Сервер не вернул ID задачи нормализации");
    appendLog(`Нормализация поставлена в очередь: ${jobId}`);
    let state = started;
    while (["queued", "running"].includes(String(state.status || "").toLowerCase())) {
      await new Promise((resolve) => setTimeout(resolve, 1000));
      state = await requestJSON(
        `/stage1/normalization-jobs/${encodeURIComponent(jobId)}`,
        "GET",
      );
    }
    if (state.status !== "completed") {
      throw new Error(state.error || state.message || "Нормализация завершилась с ошибкой");
    }
    normalization = canonicalDocument(state.result);
    normalizationEligible = false;
    normalizationDirty = false;
    renderNormalization();
    toast(`Черновик создан: ${normalization.blocks.length} блоков`, "success");
  }

  function normalizationPayload() {
    normalization.blocks.forEach((block, index) => { block.order = index + 1; });
    return { project, revision: normalization.revision, status: "draft", blocks: normalization.blocks };
  }

  async function saveNormalization() {
    const result = await requestJSON("/stage1/normalization", "PUT", normalizationPayload());
    normalization = canonicalDocument(result);
    normalizationDirty = false;
    renderNormalization();
    toast("Черновик сохранён", "success");
    return normalization;
  }

  function chooseLearningDecisions(candidates) {
    ids.learning_rule_list.innerHTML = candidates.map((candidate) => {
      const fromLabel = blockTypeLabels[candidate.from_type] || candidate.from_type;
      const toLabel = blockTypeLabels[candidate.target_type] || candidate.target_type;
      const snippet = (candidate.snippets || [])[0] || "";
      const progress = Number(candidate.confirmations || 0);
      return `<div class="learning-rule" data-candidate-id="${escaped(candidate.id)}">
        <div><strong>«${escaped(candidate.marker)}» · ${escaped(fromLabel)} → ${escaped(toLabel)}</strong>
        <small>${escaped(snippet)}${progress ? `<br>Подтверждений: ${progress}/2` : ""}</small></div>
        <select data-learning-action aria-label="Действие с правилом ${escaped(candidate.marker)}">
          <option value="this_time" selected>Только в этом сценарии</option>
          <option value="remember">Запомнить</option>
        </select>
      </div>`;
    }).join("");
    return new Promise((resolve) => {
      const cancel = () => {
        ids.learning_dialog.close();
        resolve(null);
      };
      ids.btn_cancel_learning.onclick = cancel;
      ids.learning_dialog.oncancel = (event) => { event.preventDefault(); cancel(); };
      ids.btn_confirm_with_learning.onclick = () => {
        const decisions = Array.from(ids.learning_rule_list.querySelectorAll(".learning-rule")).map((row) => ({
          candidate_id: row.dataset.candidateId,
          action: row.querySelector("[data-learning-action]").value,
        }));
        ids.learning_dialog.close();
        resolve(decisions);
      };
      ids.learning_dialog.showModal();
    });
  }

  async function resetNormalization() {
    if (!normalization) return;
    const accepted = window.confirm(
      "Очистить всю нормализацию? Все блоки и ручные правки будут удалены. " +
      "Исходник, WAV и память обозначений не изменятся."
    );
    if (!accepted) return;
    const result = await requestJSON("/stage1/normalization/reset", "POST", {
      project, revision: normalization.revision,
    });
    normalization = canonicalDocument(result);
    normalizationEligible = false;
    normalizationDirty = false;
    ids.normalization_hint.value = "";
    renderNormalization();
    toast("Нормализация очищена. Можно запустить её заново.", "success");
    appendLog("Нормализация очищена: блоки удалены");
    await refreshStatus();
  }

  async function confirmNormalization() {
    const unknown = normalization.blocks.filter((block) => (block.type || block.block_type) === "unknown").length;
    if (unknown) throw new Error(`Исправьте неопределённые блоки: ${unknown}`);
    if (normalizationDirty) await saveNormalization();
    const preview = await requestJSON("/stage1/normalization/confirm/preview", "POST", {
      project, revision: normalization.revision,
    });
    const candidates = preview.candidates || [];
    const learningDecisions = candidates.length ? await chooseLearningDecisions(candidates) : [];
    if (learningDecisions === null) return;
    const result = await requestJSON("/stage1/normalization/confirm", "POST", {
      project, revision: normalization.revision, learning_decisions: learningDecisions,
    });
    normalization = canonicalDocument(result);
    normalization.status = "confirmed";
    normalizationEligible = true;
    normalizationDirty = false;
    renderNormalization();
    toast("Сценарий подтверждён. Озвучка и сборка доступны.", "success");
    await loadMemory(true);
    await refreshStatus();
  }

  function renderSpeaker(files) {
    ids.speaker_items.innerHTML = "";
    if (!files.length) {
      ids.speaker_items.innerHTML = '<p style="font-size:12px;color:var(--muted);padding:8px;">Нет файлов</p>';
      return;
    }
    files.forEach((file) => {
      const wrap = document.createElement("div");
      wrap.className = "audio-item";

      const name = document.createElement("span");
      name.className = "audio-name";
      name.textContent = file.replace(".wav", "");

      const audio = document.createElement("audio");
      audio.controls = true;
      audio.preload = "none";
      audio.src = `/speaker/${encodeURIComponent(project)}/${encodeURIComponent(file)}`;

      wrap.appendChild(name);
      wrap.appendChild(audio);
      ids.speaker_items.appendChild(wrap);
    });
  }

  function renderMemory() {
    const query = (ids.memory_search.value || "").trim().toLocaleLowerCase("ru");
    const typeFilter = ids.memory_filter_type.value;
    const statusFilter = ids.memory_filter_status.value;
    const rules = memoryRules.filter((rule) => {
      const alias = String(rule.alias || rule.marker || rule.normalized_marker || "");
      const type = rule.block_type || rule.target_type || rule.type || "unknown";
      const status = rule.status || "active";
      return (!query || alias.toLocaleLowerCase("ru").includes(query))
        && (!typeFilter || type === typeFilter)
        && (!statusFilter || status === statusFilter);
    });
    ids.memory_rules.innerHTML = "";
    if (!rules.length) {
      ids.memory_rules.innerHTML = '<div class="empty-state compact">Нет подходящих обозначений.</div>';
      return;
    }
    rules.forEach((rule) => {
      const id = rule.id || rule.rule_id;
      const alias = rule.alias || rule.marker || rule.normalized_marker || "";
      const type = rule.block_type || rule.target_type || rule.type || "unknown";
      const status = rule.status || "active";
      const source = rule.source || "manual";
      const confirmations = Number(rule.confirmations || rule.confirmation_count || 0);
      const corrections = Number(rule.corrections || rule.correction_count || 0);
      const row = document.createElement("div");
      row.className = "memory-rule";
      row.dataset.ruleId = String(id || "");
      row.innerHTML = `
        <div class="memory-rule-head">
          <input class="memory-rule-name" data-memory-field="alias" value="${escaped(alias)}" aria-label="Обозначение">
          <select data-memory-field="block_type" aria-label="Тип обозначения">
            ${Object.entries(blockTypeLabels).filter(([value]) => value !== "unknown").map(([value, label]) => `<option value="${value}"${value === type ? " selected" : ""}>${label}</option>`).join("")}
          </select>
        </div>
        <div class="memory-rule-meta">${escaped(sourceLabels[source] || source)} · ${escaped(status)} · подтверждений ${confirmations} · исправлений ${corrections}</div>
        <div class="memory-rule-actions">
          ${status !== "active" ? '<button class="btn btn-sm" data-action="activate-memory" type="button">Активировать</button>' : '<button class="btn btn-sm" data-action="disable-memory" type="button">Отключить</button>'}
          <button class="btn btn-sm" data-action="delete-memory" type="button">Удалить</button>
        </div>`;
      ids.memory_rules.appendChild(row);
    });
  }

  async function loadMemory(quiet) {
    try {
      const result = await requestJSON(`/stage1/normalization/memory?project=${encodeURIComponent(project)}`, "GET");
      memoryRules = Array.isArray(result) ? result : (result.rules || result.items || []);
      renderMemory();
    } catch (err) {
      if (!quiet) throw err;
      memoryRules = [];
      renderMemory();
    }
  }

  async function addMemoryRule() {
    const alias = ids.memory_alias.value.trim();
    if (!alias) {
      ids.memory_alias.focus();
      throw new Error("Введите обозначение");
    }
    await requestJSON("/stage1/normalization/memory", "POST", {
      project, alias, marker: alias, block_type: ids.memory_type.value, status: "active", source: "manual",
    });
    ids.memory_alias.value = "";
    await loadMemory(false);
    toast("Обозначение добавлено в память", "success");
  }

  async function updateMemoryRule(ruleId, patch) {
    await requestJSON(`/stage1/normalization/memory/${encodeURIComponent(ruleId)}`, "PATCH", patch);
    await loadMemory(false);
  }

  async function deleteMemoryRule(ruleId) {
    if (!window.confirm("Удалить это правило из общей памяти обозначений?")) return;
    await requestJSON(`/stage1/normalization/memory/${encodeURIComponent(ruleId)}?project=${encodeURIComponent(project)}`, "DELETE");
    await loadMemory(false);
    toast("Правило памяти удалено", "success");
  }

  function applyStatus(status) {
    const prepared = Number(status.prepared_items_count || 0);
    const wav = Number(status.speaker_files_count || 0);
    const missing = Array.isArray(status.missing_ids) ? status.missing_ids.length : 0;
    const pct = prepared > 0 ? Math.round((wav / prepared) * 100) : 0;

    ids.m_prepared.textContent = String(prepared);
    ids.m_wav.textContent = String(wav);
    ids.m_missing.textContent = String(missing);
    ids.m_progress.textContent = `${pct}%`;
    ids.f_llm.textContent = status.used_llm ? "yes" : "no";
    ids.f_manual.textContent = status.manual_markers_used ? "yes" : "no";
    ids.f_manifest.textContent = status.manifest_exists ? "ready" : "missing";
    ids.progress_bar.style.width = `${Math.max(0, Math.min(100, pct))}%`;
    ids.speaker_hint.textContent = `${wav} файлов`;

    // Update pipeline steps
    if (prepared > 0) {
      setStepState(ids.step_analyze, "completed");
    }
    if (wav > 0 && missing === 0 && prepared > 0) {
      setStepState(ids.step_tts, "completed");
    } else if (wav > 0) {
      setStepState(ids.step_tts, "active");
    }
    if (status.manifest_exists) {
      setStepState(ids.step_validate, "completed");
    }

    renderSpeaker(Array.isArray(status.speaker_files) ? status.speaker_files : []);
    const normalizationStatus = status.normalization_status || (status.normalization && status.normalization.status);
    if (typeof status.normalization_confirmed === "boolean") normalizationEligible = status.normalization_confirmed;
    if (normalizationStatus && normalization && !normalizationDirty) {
      normalization.status = normalizationStatus;
      renderNormalizationStatus();
    }
    updateActionAvailability();
  }

  async function refreshStatus() {
    const response = await fetch(`/stage1/status?project=${encodeURIComponent(project)}`);
    const data = await response.json();
    applyStatus(data.result || {});
  }

  async function doAnalyze() {
    const f = formData();
    assertRequiredPath(f.input_path, "input_path");
    assertRequiredPath(f.prepared_path, "prepared_path");
    assertRequiredPath(f.raw_output_path, "raw_output_path");
    setStepState(ids.step_analyze, "active");
    appendLog(`Analyze: ${f.input_path}`);
    const result = await postJSON("/stage1/analyze", {
      project,
      input_path: f.input_path,
      output_path: f.prepared_path,
      raw_output_path: f.raw_output_path,
      use_llm: f.use_llm,
    });
    setStepState(ids.step_analyze, "completed");
    toast(`Анализ завершён: ${result.blocks_count} блоков`, "success");
    appendLog(`Analyze done: blocks=${result.blocks_count}, llm=${result.used_llm}`);
    await refreshStatus();
  }

  async function doTTS() {
    const f = formData();
    assertRequiredPath(f.prepared_path, "prepared_path");
    assertRequiredPath(f.speaker_dir, "speaker_dir");
    setStepState(ids.step_tts, "active");
    if (ids.btn_tts_cancel) {
      ids.btn_tts_cancel.disabled = false;
      ids.btn_tts_cancel.textContent = "Остановить после текущего блока";
    }
    setLive({
      status: "queued", event: "queued",
      current: "\u2014", retries: 0, done: 0, total: 0, error: "",
    });
    appendLog(`TTS job: provider=${f.provider}, gender=${f.gender}, speed=${f.speed}x, dry=${f.dry_run}`);
    const start = await postJSON("/stage1/tts-jobs/start", {
      project,
      prepared_path: f.prepared_path,
      speaker_dir: f.speaker_dir,
      provider: f.provider,
      gender: f.gender,
      voice_name: f.voice_name || null,
      dry_run: f.dry_run,
      speed: f.speed,
    });

    const jobId = String(start.job_id || "").trim();
    if (!jobId) throw new Error("TTS job start failed: empty job_id");
    currentTtsJobId = jobId;
    currentTtsJobStatus = String(start.status || "queued").toLowerCase();
    if (ids.btn_tts_cancel) ids.btn_tts_cancel.hidden = false;

    appendLog(`TTS job queued: ${jobId}`);
    let lastMarker = "";
    let retryCount = 0;
    let pollFailures = 0;
    while (true) {
      let state;
      try {
        state = await getJSON(`/stage1/tts-jobs/${encodeURIComponent(jobId)}`);
        pollFailures = 0;
      } catch (err) {
        if (!isTransientPollingError(err)) throw err;
        pollFailures += 1;
        const delay = pollingBackoffMs(pollFailures, 1200);
        appendLog(
          `TTS status временно недоступен; повтор через ${Math.ceil(delay / 1000)} с.`,
          "warn",
        );
        await new Promise((resolve) => setTimeout(resolve, delay));
        continue;
      }
      const total = Number(state.total || 0);
      const done = Number(state.done || 0);
      const event = String(state.event || "");
      currentTtsJobStatus = String(state.status || "running").toLowerCase();
      const currentId = String(state.current_id || "").trim();
      const attempts = Number(state.attempts || 0);
      const marker = `${event}|${done}|${total}|${currentId}|${attempts}`;

      if (total > 0) {
        const pct = Math.max(0, Math.min(100, Math.round((done / total) * 100)));
        ids.progress_bar.style.width = `${pct}%`;
        ids.m_progress.textContent = `${pct}%`;
        ids.speaker_hint.textContent = `TTS: ${done}/${total}`;
      }

      if (marker !== lastMarker) {
        if (event === "item_retrying") retryCount += 1;
        setLive({
          status: String(state.status || "running"),
          event: event || "running",
          current: currentId || "\u2014",
          retries: retryCount,
          done, total,
          error: String(state.last_error || state.error || ""),
        });
        const level = event === "item_retrying" ? "warn" : event === "failed" ? "error" : "info";
        appendLog(`TTS ${event}: ${done}/${total}` + (currentId ? ` (${currentId})` : ""), level);
        lastMarker = marker;
      }

      if (state.status === "completed") {
        const result = state.result || {};
        setStepState(ids.step_tts, "completed");
        setLive({ status: "completed", event: "completed", current: "\u2014", done: total, total, error: "" });
        const gen = result.files_count || state.generated || 0;
        const skip = result.skipped_count || state.skipped || 0;
        toast(`TTS завершён: ${gen} создано, ${skip} пропущено`, "success");
        appendLog(`TTS done: generated=${gen}, skipped=${skip}, voice=${result.voice || state.voice || ""}`);
        currentTtsJobId = "";
        if (ids.btn_tts_cancel) ids.btn_tts_cancel.hidden = true;
        break;
      }
      if (state.status === "cancelled" || state.status === "interrupted") {
        setStepState(ids.step_tts, "active");
        setLive({
          status: state.status, event: state.event || state.status,
          current: "\u2014", done, total, error: "",
        });
        if (ids.btn_tts_cancel) ids.btn_tts_cancel.hidden = true;
        toast("Озвучка остановлена. Готовые WAV сохранены — можно продолжить позже.", "warn");
        appendLog("TTS остановлен; повторный запуск пропустит уже готовые WAV.", "warn");
        break;
      }
      if (state.status === "failed") {
        setStepState(ids.step_tts, "error");
        setLive({
          status: "failed", event: "failed",
          error: String(state.last_error || state.error || "TTS job failed"),
          done, total,
        });
        throw new Error(state.error || "TTS job failed");
      }

      await new Promise((resolve) => setTimeout(resolve, 1200));
    }

    await refreshStatus();
  }

  async function cancelTTSJob() {
    if (!currentTtsJobId || !["queued", "running", "cancelling"].includes(currentTtsJobStatus)) return;
    try {
      const state = await postJSON(`/stage1/tts-jobs/${encodeURIComponent(currentTtsJobId)}/cancel`, {});
      currentTtsJobStatus = String(state.status || "cancelling").toLowerCase();
      setLive({status: currentTtsJobStatus, event: "cancelling"});
      ids.btn_tts_cancel.disabled = true;
      ids.btn_tts_cancel.textContent = "Останавливается…";
      appendLog("Запрошена остановка TTS. Текущий внешний вызов будет завершён безопасно.", "warn");
    } catch (err) {
      toast(err.message || String(err), "error");
      appendLog(`TTS cancel: ${err.message || err}`, "error");
    }
  }

  function restoreTtsControls(state) {
    if (!state || typeof state !== "object") return;
    if (state.prepared_path) ids.prepared_path.value = String(state.prepared_path);
    if (state.speaker_dir) ids.speaker_dir.value = String(state.speaker_dir);
    if (state.provider) ids.provider.value = String(state.provider);
    if (state.gender) ids.gender.value = String(state.gender);
    if (Object.prototype.hasOwnProperty.call(state, "voice")) {
      ids.voice_name.value = String(state.voice || "");
    }
    if (Object.prototype.hasOwnProperty.call(state, "dry_run")) {
      ids.dry_run.checked = Boolean(state.dry_run);
    }
    const speed = Number(state.speed);
    if (Number.isFinite(speed) && speed > 0) {
      ids.speed.value = String(speed);
      ids.speed_val.textContent = speed.toFixed(2);
    }
  }

  function renderRestoredTtsJob(state) {
    const total = Number(state.total || 0);
    const done = Number(state.done || 0);
    currentTtsJobId = String(state.id || state.job_id || currentTtsJobId || "");
    currentTtsJobStatus = String(state.status || "").toLowerCase();
    setLive({
      status: currentTtsJobStatus || "idle",
      event: String(state.event || currentTtsJobStatus || "\u2014"),
      current: String(state.current_id || "\u2014"),
      retries: Number(state.retry_count || 0),
      done,
      total,
      error: String(state.last_error || state.error || ""),
    });
    if (total > 0) {
      const pct = Math.max(0, Math.min(100, Math.round((done / total) * 100)));
      ids.progress_bar.style.width = `${pct}%`;
      ids.m_progress.textContent = `${pct}%`;
    }
    const active = ["queued", "running", "cancelling"].includes(currentTtsJobStatus);
    if (ids.btn_tts_cancel) ids.btn_tts_cancel.hidden = !active;
    updateActionAvailability();
    return active;
  }

  async function pollRestoredTtsJob() {
    if (!currentTtsJobId) return;
    try {
      const state = await getJSON(`/stage1/tts-jobs/${encodeURIComponent(currentTtsJobId)}`);
      ttsPollFailures = 0;
      if (renderRestoredTtsJob(state)) {
        ttsPollTimer = window.setTimeout(pollRestoredTtsJob, 1200);
      } else {
        ttsPollTimer = null;
        if (["cancelled", "interrupted"].includes(currentTtsJobStatus)) {
          appendLog("TTS остановлен; готовые WAV можно продолжить кнопкой «Продолжить озвучку».", "warn");
        }
        await refreshStatus();
      }
    } catch (err) {
      if (!isTransientPollingError(err)) {
        ttsPollTimer = null;
        appendLog(`TTS status: ${err.message || err}`, "warn");
        return;
      }
      ttsPollFailures += 1;
      const delay = pollingBackoffMs(ttsPollFailures, 1200);
      appendLog(
        `TTS status временно недоступен; повтор через ${Math.ceil(delay / 1000)} с.`,
        "warn",
      );
      ttsPollTimer = window.setTimeout(pollRestoredTtsJob, delay);
    }
  }

  async function restoreTtsJob() {
    const response = await optionalRequestJSON(
      `/stage1/tts-jobs/status?project=${encodeURIComponent(project)}`,
    );
    if (!response.available || !response.result) return;
    restoreTtsControls(response.result);
    if (renderRestoredTtsJob(response.result) && !ttsPollTimer) {
      ttsPollTimer = window.setTimeout(pollRestoredTtsJob, 500);
    }
  }

  async function doPhraseTTS() {
    const f = formData();
    assertRequiredPath(f.speaker_dir, "speaker_dir");
    const phrase = (f.phrase_text || "").trim();
    if (!phrase) {
      toast("Введи текст фразы", "error");
      ids.phrase_text.focus();
      return;
    }
    appendLog(`Phrase TTS: provider=${f.provider}, gender=${f.gender}, speed=${f.speed}x, dry=${f.dry_run}`);
    const result = await postJSON("/stage1/tts-phrase", {
      project,
      text: phrase,
      speaker_dir: f.speaker_dir,
      provider: f.provider,
      gender: f.gender,
      voice_name: f.voice_name || null,
      file_name: f.phrase_file_name || null,
      dry_run: f.dry_run,
      speed: f.speed,
    });
    toast("Фраза озвучена", "success");
    appendLog(`Phrase done: file=${result.file}, voice=${result.voice}`);
    if (result.playback_url && ids.phrase_player) {
      ids.phrase_player.src = `${result.playback_url}?t=${Date.now()}`;
      ids.phrase_player.load();
    }
    await refreshStatus();
  }

  async function doValidate() {
    const f = formData();
    assertRequiredPath(f.prepared_path, "prepared_path");
    assertRequiredPath(f.speaker_dir, "speaker_dir");
    assertRequiredPath(f.manifest_path, "manifest_path");
    setStepState(ids.step_validate, "active");
    appendLog("Validate...");
    const response = await fetch("/stage1/validate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        project,
        prepared_path: f.prepared_path,
        speaker_dir: f.speaker_dir,
        manifest_path: f.manifest_path,
      }),
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok && response.status !== 422) {
      throw new Error(data.message || data.error || `HTTP ${response.status}`);
    }
    const result = data.result || {};
    if (result.ok) {
      setStepState(ids.step_validate, "completed");
      toast(`Валидация пройдена: ${result.items_count} элементов`, "success");
    } else {
      setStepState(ids.step_validate, "error");
      const errList = (result.errors || []).join("; ");
      toast(`Валидация: ${errList || "есть ошибки"}`, "warn");
    }
    appendLog(`Validate done: ok=${result.ok}, items=${result.items_count}, errors=${(result.errors||[]).length}`);
    await refreshStatus();
  }

  async function safely(fn) {
    if (busy) return;
    setBusy(true);
    try {
      await fn();
    } catch (err) {
      const msg = err.message || String(err);
      toast(msg, "error");
      appendLog(`ERROR: ${msg}`, true);
      if (msg.includes("macOS TTS unavailable") || msg.includes("command 'say'")) {
        appendLog("Подсказка: выбери provider=openrouter/gemini или настрой macOS-коннектор.", true);
      }
    } finally {
      setBusy(false);
    }
  }

  function openTab(tabName) {
    document.querySelectorAll(".workspace-tab").forEach((tab) => {
      tab.classList.toggle("active", tab.dataset.tab === tabName);
    });
    document.querySelectorAll("[data-tab-panel]").forEach((panel) => {
      const active = panel.dataset.tabPanel === tabName;
      panel.classList.toggle("active", active);
      panel.hidden = !active;
    });
  }

  // ── Stage 3: headless media scan ─────────────────────────────────────────
  function stage3MediaItems(result) {
    if (Array.isArray(result)) return result;
    if (!result || typeof result !== "object") return [];
    for (const key of ["sources", "items", "files", "media"]) {
      if (Array.isArray(result[key])) return result[key];
    }
    return [];
  }

  function formatMediaDuration(value) {
    const seconds = Number(value);
    if (!Number.isFinite(seconds) || seconds < 0) return "—";
    const hours = Math.floor(seconds / 3600);
    const minutes = Math.floor((seconds % 3600) / 60);
    const rest = Math.floor(seconds % 60);
    return hours > 0
      ? `${hours}:${String(minutes).padStart(2, "0")}:${String(rest).padStart(2, "0")}`
      : `${minutes}:${String(rest).padStart(2, "0")}`;
  }

  function formatMediaSize(value) {
    const bytes = Number(value);
    if (!Number.isFinite(bytes) || bytes < 0) return "—";
    if (bytes < 1024) return `${bytes} Б`;
    const units = ["КБ", "МБ", "ГБ", "ТБ"];
    let size = bytes / 1024;
    let unit = units[0];
    for (let index = 1; index < units.length && size >= 1024; index += 1) {
      size /= 1024;
      unit = units[index];
    }
    return `${size >= 10 ? size.toFixed(1) : size.toFixed(2)} ${unit}`;
  }

  function mediaItemMetadata(item) {
    const probe = item.probe && typeof item.probe === "object" ? item.probe : {};
    const video = item.video && typeof item.video === "object" ? item.video : {};
    const duration = item.duration_sec ?? item.duration_seconds ?? item.duration
      ?? probe.duration_sec ?? probe.duration_seconds ?? probe.duration;
    const size = item.size_bytes ?? item.size ?? probe.size_bytes ?? probe.size;
    const width = item.width ?? video.width ?? probe.width;
    const height = item.height ?? video.height ?? probe.height;
    const fps = item.fps ?? video.fps ?? probe.fps;
    const resolution = width && height ? `${width}×${height}` : "—";
    const fpsText = Number.isFinite(Number(fps)) ? `${Number(fps).toFixed(2)} fps` : "—";
    return { duration, size, resolution, fpsText };
  }

  function renderStage3Media(result) {
    if (!ids.stage3_media_list) return;
    const items = stage3MediaItems(result);
    stage3MediaCount = items.length;
    if (ids.stage3_media_directory) {
      ids.stage3_media_directory.textContent = result.directory || "Папка проекта не настроена";
    }
    ids.stage3_media_count.textContent = String(items.length);
    if (ids.btn_stage3_prepare) {
      ids.btn_stage3_prepare.disabled = busy || items.length === 0;
    }
    ids.stage3_media_message.textContent = items.length > 0
      ? `Папка просканирована. ${items.length} файл(ов) готовы как временные источники.`
      : "В указанной папке поддерживаемые видео-, WAV- или MP3-файлы не найдены.";
    ids.stage3_media_list.innerHTML = items.length === 0
      ? '<div class="empty-state compact">Проверьте путь и наличие видео-, WAV- или MP3-файлов.</div>'
      : items.map((item) => {
        const path = String(item.container_path || item.relative_path || item.path || item.file || item.name || "Без имени");
        const name = String(item.name || path.split(/[\\/]/).pop() || path);
        const metadata = mediaItemMetadata(item);
        return `<article class="stage3-media-item">
          <div class="stage3-media-file">
            <strong title="${escaped(path)}">${escaped(name)}</strong>
            <span title="${escaped(path)}">${escaped(path)}</span>
          </div>
          <dl class="stage3-media-meta">
            <div><dt>Длительность</dt><dd>${formatMediaDuration(metadata.duration)}</dd></div>
            <div><dt>Размер</dt><dd>${formatMediaSize(metadata.size)}</dd></div>
            <div><dt>Кадр</dt><dd>${escaped(metadata.resolution)}</dd></div>
            <div><dt>Частота</dt><dd>${escaped(metadata.fpsText)}</dd></div>
          </dl>
        </article>`;
      }).join("");
    renderStage3SourceMapping(result.mapping || []);
  }

  function stage3MappingLabel(item) {
    const aliases = (item.sequence_aliases || []).concat(item.speaker_aliases || []);
    return aliases[0] || item.source_id || "Источник";
  }

  function renderStage3SourceMapping(mapping) {
    if (!ids.stage3_source_mapping) return;
    const items = Array.isArray(mapping) ? mapping : [];
    stage3SourceMapping = items;
    if (items.length === 0) {
      ids.stage3_source_mapping.innerHTML =
        '<div class="empty-state compact">Сначала соберите Stage 2 timeline.</div>';
      return;
    }
    ids.stage3_source_mapping.innerHTML = items.map((item) => {
      const mapped = item.status === "mapped";
      const reason = mapped
        ? (item.media_kind === "audio"
          ? `карточка + звук · до ${formatMediaDuration(item.max_source_out_sec)}`
          : `до ${formatMediaDuration(item.max_source_out_sec)} на исходнике`)
        : (item.reason === "ambiguous_name_match" ? "неоднозначное имя" :
          (item.reason === "media_too_short" ? "просмотровка короче нужного диапазона" :
            "просмотровка не найдена"));
      return `<article class="stage3-map-row" data-state="${mapped ? "mapped" : "unmapped"}">
        <div><strong>${escaped(stage3MappingLabel(item))}</strong><small>${escaped(item.source_id || "")}</small></div>
        <span class="stage3-map-arrow">→</span>
        <div><strong>${escaped(item.media_name || "Нет файла")}</strong><small>${escaped(reason)}</small></div>
      </article>`;
    }).join("");
  }

  async function scanStage3Media() {
    if (stage3MediaScanning) return;
    stage3MediaScanAttempted = true;
    stage3MediaScanning = true;
    ids.btn_stage3_media_scan.disabled = true;
    ids.stage3_media_status.dataset.state = "running";
    ids.stage3_media_status.textContent = "сканирование…";
    ids.stage3_media_message.textContent = "Читаем файлы и метаданные…";
    try {
      const result = await postJSON("/stage3/media/scan", { project });
      renderStage3Media(result);
      await loadSyncWorkspace(true);
      ids.stage3_media_status.dataset.state = "completed";
      ids.stage3_media_status.textContent = "готово";
      toast(`Просмотровки найдены: ${stage3MediaItems(result).length}`, "success");
      appendLog(
        `Stage 3 media: найдено ${stage3MediaItems(result).length}; `
        + `Whisper-источников=${Number(result.registered_sources || 0)}; `
        + `directory=${result.directory || "—"}`
      );
    } catch (err) {
      ids.stage3_media_status.dataset.state = "failed";
      ids.stage3_media_status.textContent = "ошибка";
      ids.stage3_media_message.textContent = err.message || String(err);
      throw err;
    } finally {
      stage3MediaScanning = false;
      ids.btn_stage3_media_scan.disabled = busy;
    }
  }

  function scanStage3MediaOnce() {
    if (stage3MediaScanAttempted) return;
    scanStage3Media().catch((err) => {
      appendLog(`Stage 3 media init: ${err.message || err}`, "warn");
    });
  }

  async function prepareStage3Timeline() {
    if (stage3MediaCount <= 0) {
      throw new Error("Сначала добавьте просмотровки в папку проекта.");
    }
    ids.btn_stage3_prepare.disabled = true;
    ids.btn_stage3_prepare.textContent = "Подготовка XML…";
    ids.stage3_media_status.dataset.state = "running";
    ids.stage3_media_status.textContent = "сборка…";
    try {
      const result = await postJSON("/stage3/timeline/prepare", { project });
      renderStage3SourceMapping(result.source_mapping || []);
      ids.stage3_media_status.dataset.state = "completed";
      ids.stage3_media_status.textContent = "XML готов";
      ids.stage3_media_message.textContent =
        `Сопоставлено источников: ${(result.mapped_source_ids || []).length}; `
        + `плейсхолдеров: ${Number(result.placeholder_segments || 0)}.`;
      appendLog(
        `Stage 3 XML: mapped=${(result.mapped_source_ids || []).join(", ") || "—"}; `
        + `unmapped=${(result.unmapped_source_ids || []).join(", ") || "—"}`
      );
      toast("Nested XML готов. Начинаю загрузку.", "success");
      window.location.href = result.download_url;
    } catch (err) {
      ids.stage3_media_status.dataset.state = "failed";
      ids.stage3_media_status.textContent = "ошибка";
      ids.stage3_media_message.textContent = err.message || String(err);
      throw err;
    } finally {
      ids.btn_stage3_prepare.textContent = "Подготовить таймлайн";
      ids.btn_stage3_prepare.disabled = busy || stage3MediaCount <= 0;
    }
  }

  document.querySelectorAll(".workspace-tab").forEach((tab) => {
    tab.addEventListener("click", () => {
      openTab(tab.dataset.tab);
      if (tab.dataset.tab === "assembly") {
        scanStage3MediaOnce();
        loadSyncWorkspace(true).catch((err) => {
          appendLog(`Stage 2 sync: ${err.message || err}`, "warn");
        });
      }
    });
  });

  ids.normalization_blocks.addEventListener("input", (event) => {
    if (event.target.dataset.field || event.target.dataset.fragmentField) updateBlockFromControl(event.target);
  });
  ids.normalization_blocks.addEventListener("change", (event) => {
    if (event.target.dataset.field || event.target.dataset.fragmentField) updateBlockFromControl(event.target);
  });
  ids.normalization_blocks.addEventListener("click", (event) => {
    const button = event.target.closest("[data-action]");
    if (!button || !normalization) return;
    const card = button.closest(".normalization-block");
    const block = normalization.blocks[Number(card.dataset.index)];
    if (button.dataset.action === "remember-block") {
      ids.memory_alias.value = block.original_label || "";
      ids.memory_type.value = block.type || "voiceover";
      ids.memory_alias.focus();
      return;
    }
    if (button.dataset.action === "move-up" || button.dataset.action === "move-down") {
      const index = Number(card.dataset.index);
      const target = index + (button.dataset.action === "move-up" ? -1 : 1);
      if (target >= 0 && target < normalization.blocks.length) {
        [normalization.blocks[index], normalization.blocks[target]] =
          [normalization.blocks[target], normalization.blocks[index]];
        markNormalizationDirty();
        renderNormalization();
      }
      return;
    }
    if (button.dataset.action !== "delete-block") return;
    normalization.blocks.splice(Number(card.dataset.index), 1);
    markNormalizationDirty();
    renderNormalization();
  });
  ids.btn_add_normalization_block.addEventListener("click", () => {
    if (!normalization) return;
    normalization.blocks.push({
      uid: (self.crypto && crypto.randomUUID) ? crypto.randomUUID() : `manual-${Date.now()}`,
      order: normalization.blocks.length + 1,
      type: "unknown", original_text: "", normalized_text: "", text_for_tts: "",
      original_label: "", source: "manual", confidence: 1,
      warnings: ["Укажите тип и текст блока"],
    });
    markNormalizationDirty();
    renderNormalization();
  });
  ids.btn_normalize.addEventListener("click", () => safely(generateNormalization));
  ids.btn_reset_normalization.addEventListener("click", () => safely(resetNormalization));
  ids.btn_save_normalization.addEventListener("click", () => safely(saveNormalization));
  ids.btn_confirm_normalization.addEventListener("click", () => safely(confirmNormalization));
  ids.btn_add_memory.addEventListener("click", () => safely(addMemoryRule));
  ids.memory_search.addEventListener("input", renderMemory);
  ids.memory_filter_type.addEventListener("change", renderMemory);
  ids.memory_filter_status.addEventListener("change", renderMemory);
  ids.memory_rules.addEventListener("click", (event) => {
    const button = event.target.closest("[data-action]");
    if (!button) return;
    const ruleId = button.closest(".memory-rule").dataset.ruleId;
    if (!ruleId) return;
    if (button.dataset.action === "delete-memory") safely(() => deleteMemoryRule(ruleId));
    if (button.dataset.action === "activate-memory") safely(() => updateMemoryRule(ruleId, { status: "active" }));
    if (button.dataset.action === "disable-memory") safely(() => updateMemoryRule(ruleId, { status: "disabled" }));
  });
  ids.memory_rules.addEventListener("change", (event) => {
    const control = event.target.closest("[data-memory-field]");
    if (!control) return;
    const ruleId = control.closest(".memory-rule").dataset.ruleId;
    if (!ruleId) return;
    safely(() => updateMemoryRule(ruleId, { [control.dataset.memoryField]: control.value.trim() }));
  });

  // ── Stage 2 ──────────────────────────────────────────────────────────────
  function syncCollection(document, keys) {
    if (Array.isArray(document)) return document;
    if (!document || typeof document !== "object") return [];
    for (const key of keys) {
      if (Array.isArray(document[key])) return document[key];
    }
    return [];
  }

  function normalizedMatchStatus(value) {
    const status = String(value || "unmatched").toLowerCase();
    if (status === "matched" || status === "auto" || status === "ready") return "auto_matched";
    if (status === "review" || status === "candidate" || status === "probable") return "needs_review";
    if (status === "not_found" || status === "missing") return "unmatched";
    return status;
  }

  function matchCandidate(match) {
    const alternatives = Array.isArray(match.alternatives) ? match.alternatives : [];
    const nested = match.candidate || match.best_candidate || alternatives[0] || {};
    const value = { ...nested };
    [
      "source_id", "sequence_name", "source_in_sec", "source_out_sec",
      "speech_in_sec", "speech_out_sec", "matched_text", "confidence",
    ].forEach((key) => {
      if (match[key] !== undefined && match[key] !== null && match[key] !== "") {
        value[key] = match[key];
      }
    });
    return value;
  }

  function candidateChoices(match) {
    const primary = matchCandidate(match);
    const values = [primary, ...(Array.isArray(match.alternatives) ? match.alternatives : [])];
    const seen = new Set();
    return values.filter((candidate) => {
      const key = [
        candidate.source_id,
        Number(candidate.source_in_sec).toFixed(3),
        Number(candidate.source_out_sec).toFixed(3),
      ].join(":");
      if (!candidate.source_id || seen.has(key)) return false;
      seen.add(key);
      return true;
    });
  }

  function candidateSource(candidate) {
    return syncSources.find((source) =>
      String(source.id || source.source_id || "") === String(candidate.source_id || "")
    ) || {};
  }

  function syncAudioUrl(sourceId) {
    if (!sourceId) return "";
    return `/stage2/sync-sources/${encodeURIComponent(sourceId)}/audio?project=${encodeURIComponent(project)}`;
  }

  function matchBlock(match) {
    if (!normalization || !Array.isArray(normalization.blocks)) return {};
    const uid = String(match.block_uid || "");
    if (uid) {
      return normalization.blocks.find((block) => String(block.uid || "") === uid) || {};
    }
    const legacyId = String(match.segment_id || "");
    const legacyIndex = Number(legacyId.match(/^snh(\d+)$/)?.[1] || 0) - 1;
    if (legacyIndex < 0) return {};
    return normalization.blocks.filter((block) => String(block.type || "") === "sync")[legacyIndex] || {};
  }

  function formatSourceSecond(value) {
    const seconds = Number(value);
    if (!Number.isFinite(seconds) || seconds < 0) return "\u2014";
    const hours = Math.floor(seconds / 3600);
    const minutes = Math.floor((seconds % 3600) / 60);
    const rest = seconds % 60;
    const prefix = hours > 0 ? `${hours}:${String(minutes).padStart(2, "0")}` : String(minutes);
    return `${prefix}:${rest.toFixed(1).padStart(4, "0")}`;
  }

  function renderSyncSources() {
    if (!ids.sync_sources_count) return;
    ids.sync_sources_count.textContent = String(syncSources.length);
    if (syncFeatureAvailable === false) {
      ids.sync_sources_names.textContent = "Функция недоступна в текущей версии backend";
      ids.sync_job_status.dataset.state = "idle";
      ids.sync_job_status.textContent = "недоступно";
    } else if (syncSources.length === 0) {
      ids.sync_sources_names.textContent = "Нет зарегистрированных секвенций";
    } else {
      const names = syncSources.map((source) => {
        const sequence = source.premiere_sequence || source.sequence_name
          || source.name || source.id || "без имени";
        const speakers = [source.speaker].concat(
          Array.isArray(source.speakers) ? source.speakers : []
        ).filter(Boolean);
        const scope = source.all_speakers
          ? "все спикеры"
          : (speakers.length ? speakers.join(", ") : (source.speaker || "спикер не указан"));
        return `${sequence} · ${scope}`;
      });
      const visible = names.slice(0, 3).join(", ");
      ids.sync_sources_names.textContent = names.length > 3
        ? `${visible} и ещё ${names.length - 3}`
        : visible;
    }
    updateActionAvailability();
  }

  function syncCounts(document) {
    const summary = document && typeof document.summary === "object" ? document.summary : {};
    const explicit = {
      auto: summary.auto_matched ?? summary.auto ?? document?.auto_matched,
      confirmed: summary.confirmed ?? document?.confirmed,
      review: summary.needs_review ?? summary.review ?? document?.needs_review,
      unmatched: summary.unmatched ?? document?.unmatched,
    };
    if (Object.values(explicit).some((value) => value !== undefined)) {
      return {
        auto: Number(explicit.auto || 0),
        confirmed: Number(explicit.confirmed || 0),
        review: Number(explicit.review || 0),
        unmatched: Number(explicit.unmatched || 0),
      };
    }
    return syncMatches.reduce((counts, match) => {
      const status = normalizedMatchStatus(match.status);
      if (status === "auto_matched") counts.auto += 1;
      else if (status === "confirmed") counts.confirmed += 1;
      else if (status === "needs_review") counts.review += 1;
      else counts.unmatched += 1;
      return counts;
    }, { auto: 0, confirmed: 0, review: 0, unmatched: 0 });
  }

  function renderSyncMatches(document) {
    if (!ids.sync_review_list) return;
    const counts = syncCounts(document || {});
    ids.sync_count_auto.textContent = String(counts.auto);
    ids.sync_count_confirmed.textContent = String(counts.confirmed);
    ids.sync_count_review.textContent = String(counts.review);
    ids.sync_count_unmatched.textContent = String(counts.unmatched);

    const reviewItems = syncMatches.filter((match) => {
      const status = normalizedMatchStatus(match.status);
      return status === "needs_review" || status === "unmatched";
    });
    ids.sync_review_count.textContent = `${reviewItems.length} фрагментов`;

    if (syncFeatureAvailable === false) {
      ids.sync_review_list.innerHTML =
        '<div class="empty-state compact">Сопоставление появится после обновления backend.</div>';
      return;
    }
    if (reviewItems.length === 0) {
      ids.sync_review_list.innerHTML = syncMatches.length > 0
        ? '<div class="empty-state compact">Все найденные фрагменты готовы к сборке.</div>'
        : '<div class="empty-state compact">Результатов сопоставления пока нет.</div>';
      return;
    }

    ids.sync_review_list.innerHTML = reviewItems.map((match) => {
      const status = normalizedMatchStatus(match.status);
      const candidate = matchCandidate(match);
      const candidates = candidateChoices(match);
      const block = matchBlock(match);
      const blockUid = String(match.block_uid || match.segment_id || "");
      const matchId = String(match.segment_id || blockUid);
      const speaker = match.speaker || block.speaker || match.segment_id || "СНХ";
      const timecode = match.approximate_timecode || match.approx_timecode || block.timecode_start || "";
      const scenarioText = match.scenario_text || match.query_text
        || block.normalized_text || block.original_text || "";
      const matchedText = candidate.matched_text || match.matched_text || "";
      const sourceRecord = candidateSource(candidate);
      const source = candidate.sequence_name || sourceRecord.sequence_name
        || sourceRecord.premiere_sequence || candidate.source_id || "источник не выбран";
      const confidence = Number(candidate.confidence);
      const confidenceText = Number.isFinite(confidence)
        ? `${Math.round(confidence * (confidence <= 1 ? 100 : 1))}%`
        : "\u2014";
      const llmReview = match.metadata?.llm_semantic_review;
      const llmConfidence = Number(llmReview?.confidence);
      const llmHint = llmReview
        ? `<div class="sync-match-llm">LLM выбрал этот вариант по смыслу${
            Number.isFinite(llmConfidence)
              ? ` · уверенность ${Math.round(llmConfidence * 100)}%`
              : ""
          }. Проверьте аудио перед подтверждением.</div>`
        : "";
      const sourceIn = Number(candidate.source_in_sec);
      const sourceOut = Number(candidate.source_out_sec);
      const hasCandidate = Boolean(candidate.source_id)
        && Number.isFinite(sourceIn) && Number.isFinite(sourceOut) && sourceOut > sourceIn;
      const label = llmReview
        ? "LLM · проверить"
        : (status === "unmatched" ? "не найдено" : "нужно проверить");
      const action = hasCandidate && matchId
        ? `<button class="btn btn-sm btn-accent" type="button"
             data-action="confirm-sync-match" data-match-id="${escaped(matchId)}">
             ${status === "unmatched" ? "Подтвердить лучший вариант" : "Подтвердить"}
           </button>`
        : "";
      const candidateSelect = candidates.length > 1
        ? `<label class="sync-match-field sync-match-candidate">
             <span>Вариант</span>
             <select data-sync-candidate>
               ${candidates.map((item, index) => {
                 const itemSource = candidateSource(item);
                 const itemName = item.sequence_name || itemSource.sequence_name
                   || itemSource.premiere_sequence || item.source_id;
                 const itemConfidence = Number(item.confidence);
                 const itemPct = Number.isFinite(itemConfidence)
                   ? Math.round(itemConfidence * (itemConfidence <= 1 ? 100 : 1))
                   : 0;
                 return `<option value="${index}">${escaped(itemName)} · ${formatSourceSecond(item.source_in_sec)} · ${itemPct}%</option>`;
               }).join("")}
             </select>
           </label>`
        : "";
      const reviewControls = hasCandidate
        ? `<div class="sync-review-controls">
             ${candidateSelect}
             <label class="sync-match-field">
               <span>Source in, сек</span>
               <input data-sync-in type="number" min="0" step="0.04" value="${escaped(sourceIn.toFixed(3))}">
             </label>
             <label class="sync-match-field">
               <span>Source out, сек</span>
               <input data-sync-out type="number" min="0" step="0.04" value="${escaped(sourceOut.toFixed(3))}">
             </label>
           </div>
           <audio data-sync-audio controls preload="none"
             src="${escaped(syncAudioUrl(candidate.source_id))}"
             data-range-in="${escaped(sourceIn)}" data-range-out="${escaped(sourceOut)}"></audio>`
        : "";
      return `
        <article class="sync-match-card" data-status="${escaped(status)}"
          data-block-uid="${escaped(blockUid)}" data-match-id="${escaped(matchId)}"
          data-source-id="${escaped(candidate.source_id || "")}">
          <div class="sync-match-head">
            <div class="sync-match-title">${escaped(speaker)}${timecode ? ` · ${escaped(timecode)}` : ""}</div>
            <span class="sync-match-status">${label}</span>
          </div>
          <p class="sync-match-text">${escaped(scenarioText || "Текст сценария отсутствует")}</p>
          <div class="sync-match-found" data-sync-found>${matchedText
            ? escaped(matchedText)
            : "Whisper-фрагмент не найден"}</div>
          ${llmHint}
          <div class="sync-match-meta" data-sync-meta>
            ${escaped(source)} · ${formatSourceSecond(sourceIn)}–${formatSourceSecond(sourceOut)}
            · уверенность ${confidenceText}
          </div>
          ${reviewControls}
          <div class="sync-match-actions">${action}</div>
        </article>`;
    }).join("");
    updateActionAvailability();
  }

  async function loadSyncWorkspace(quiet) {
    const [sourcesResponse, matchesResponse, jobResponse] = await Promise.all([
      optionalRequestJSON(`/stage2/sync-sources?project=${encodeURIComponent(project)}`),
      optionalRequestJSON(`/stage2/sync-matches?project=${encodeURIComponent(project)}`),
      optionalRequestJSON(`/stage2/sync-jobs/status?project=${encodeURIComponent(project)}`),
    ]);
    syncFeatureAvailable = sourcesResponse.available || matchesResponse.available;

    const sourcesDocument = sourcesResponse.result || {};
    const matchesDocument = matchesResponse.result || {};
    syncSources = syncCollection(sourcesDocument, ["items", "sources"]);
    syncMatches = syncCollection(matchesDocument, ["items", "matches"]);
    syncMatchesRevision = Number(matchesDocument.revision || 0);
    renderSyncSources();
    renderSyncMatches(matchesDocument);
    if (jobResponse.available && jobResponse.result) {
      const job = syncJobPayload(jobResponse.result);
      renderSyncJob(job);
      const status = String(job.status || "").toLowerCase();
      if (["queued", "running", "cancelling"].includes(status)) {
        syncJobRunning = true;
        updateActionAvailability();
        if (!syncPollTimer && currentSyncJobId) {
          syncPollTimer = window.setTimeout(
            () => pollSyncJob(`/stage2/sync-jobs/${encodeURIComponent(currentSyncJobId)}`),
            500,
          );
        }
      }
      updateActionAvailability();
    }
    if (!quiet && syncFeatureAvailable === false) {
      appendLog("Stage 2 sync API пока недоступен; обычная сборка продолжает работать.", "warn");
    }
  }

  function syncJobPayload(value) {
    if (!value || typeof value !== "object") return {};
    return value.job && typeof value.job === "object" ? value.job : value;
  }

  function renderSyncJob(value) {
    const job = syncJobPayload(value);
    const status = String(job.status || "running").toLowerCase();
    currentSyncJobId = String(job.id || job.job_id || currentSyncJobId || "");
    currentSyncJobStatus = status;
    const total = Number(job.total || job.items_total || 0);
    const done = Number(job.done ?? job.completed ?? job.items_done ?? 0);
    const current = job.current_source_id || job.current_source || job.current_id || "";
    const message = job.message || job.event || (current ? `Обрабатывается ${current}` : "Сопоставление СНХ");
    const totalSources = Number(job.total_sources || 0);
    const completedSources = Math.min(
      totalSources || Number.POSITIVE_INFINITY,
      Number(job.transcribed || 0)
        + Number(job.reused_indexes || 0)
        + (Array.isArray(job.source_failures) ? job.source_failures.length : 0),
    );
    const processedVideo = Number(job.transcription_processed_sec || 0);
    const currentVideoDuration = Number(job.transcription_duration_sec || 0);
    const currentFraction = currentVideoDuration > 0
      ? Math.max(0, Math.min(1, processedVideo / currentVideoDuration))
      : 0;
    const sourcePct = totalSources > 0
      ? Math.round(((completedSources + currentFraction) / totalSources) * 100)
      : 0;
    const pct = status === "completed" ? 100
      : (currentVideoDuration > 0 || totalSources > 0 ? sourcePct
        : (total > 0 ? Math.round((done / total) * 100) : 0));
    const mapping = stage3SourceMapping.find((item) => item.source_id === current) || {};
    const jobStartedAt = Date.parse(job.started_at || "");
    const sourceStartedAt = Date.parse(job.transcription_source_started_at || "");
    const elapsedSec = Number.isFinite(jobStartedAt)
      ? Math.max(0, (Date.now() - jobStartedAt) / 1000)
      : 0;
    const sourceElapsedSec = Number.isFinite(sourceStartedAt)
      ? Math.max(0, (Date.now() - sourceStartedAt) / 1000)
      : 0;
    const currentEtaSec = sourceElapsedSec > 0
      && processedVideo > 0
      && currentVideoDuration > processedVideo
      ? sourceElapsedSec * (currentVideoDuration - processedVideo) / processedVideo
      : 0;
    const overallFraction = Math.max(0, Math.min(1, pct / 100));
    const overallEtaSec = elapsedSec > 0 && overallFraction > 0 && overallFraction < 1
      ? elapsedSec * (1 - overallFraction) / overallFraction
      : 0;

    ids.sync_job_status.dataset.state = status;
    ids.sync_job_status.textContent = status === "completed" ? "готово"
      : status === "failed" ? "ошибка"
      : status === "interrupted" ? "прервано"
      : status === "cancelled" ? "прервано"
      : status === "cancelling" ? "останавливается"
      : status === "queued" ? "в очереди"
      : "в работе";
    ids.sync_job_progress.hidden = false;
    ids.sync_job_message.textContent = message;
    ids.sync_job_numbers.textContent = totalSources > 0
      ? `${completedSources}/${totalSources} файлов · ${pct}%`
      : (total > 0 ? `${done}/${total}` : `${pct}%`);
    ids.sync_progress_bar.style.width = `${Math.max(0, Math.min(100, pct))}%`;
    if (ids.sync_job_current) {
      ids.sync_job_current.textContent = mapping.media_name || current || "—";
    }
    if (ids.sync_job_video_progress) {
      ids.sync_job_video_progress.textContent = currentVideoDuration > 0
        ? `${formatMediaDuration(processedVideo)} / ${formatMediaDuration(currentVideoDuration)}`
        : "—";
    }
    if (ids.sync_job_elapsed) {
      ids.sync_job_elapsed.textContent = elapsedSec > 0 ? formatMediaDuration(elapsedSec) : "—";
    }
    if (ids.sync_job_eta) {
      ids.sync_job_eta.textContent = status === "completed" ? "готово"
        : (overallEtaSec > 0
          ? `≈ ${formatMediaDuration(overallEtaSec)} всего`
          : (currentEtaSec > 0
            ? `≈ ${formatMediaDuration(currentEtaSec)} текущая`
            : "рассчитывается"));
    }
  }

  function finishSyncJob() {
    syncJobRunning = false;
    syncPollFailures = 0;
    if (syncPollTimer) window.clearTimeout(syncPollTimer);
    syncPollTimer = null;
    updateActionAvailability();
  }

  async function pollSyncJob(statusUrl) {
    try {
      const response = await optionalRequestJSON(statusUrl);
      syncPollFailures = 0;
      if (!response.available) {
        finishSyncJob();
        appendLog("Статус задачи сопоставления больше недоступен.", "warn");
        return;
      }
      const job = syncJobPayload(response.result);
      renderSyncJob(job);
      const status = String(job.status || "").toLowerCase();
      if (status === "completed") {
        finishSyncJob();
        await loadSyncWorkspace(true);
        toast("Автоматическое сопоставление СНХ завершено", "success");
        appendLog("Stage 2 sync: сопоставление завершено.");
        return;
      }
      if (status === "failed" || status === "cancelled") {
        finishSyncJob();
        if (status === "cancelled") {
          toast("Сопоставление остановлено. Индексы и найденные диапазоны сохранены.", "warn");
          appendLog("Stage 2 sync: остановлено; можно продолжить повторным запуском.", "warn");
        } else {
          const message = job.error || job.last_error || "Сопоставление завершилось с ошибкой";
          toast(message, "error");
          appendLog(`Stage 2 sync: ${message}`, "error");
        }
        return;
      }
      if (status === "interrupted") {
        finishSyncJob();
        await loadSyncWorkspace(true);
        toast("Работа была прервана. Подтверждённые СНХ сохранены — запустите сопоставление снова.", "warn");
        appendLog("Stage 2 sync: задача прервана; сохранённые результаты можно продолжить.", "warn");
        return;
      }
      syncPollTimer = window.setTimeout(() => pollSyncJob(statusUrl), 1500);
    } catch (err) {
      if (!isTransientPollingError(err)) {
        finishSyncJob();
        toast(err.message || String(err), "error");
        appendLog(`Stage 2 sync status: ${err.message || err}`, "error");
        return;
      }
      syncPollFailures += 1;
      const delay = pollingBackoffMs(syncPollFailures, 1500);
      appendLog(
        `Stage 2 sync status временно недоступен; повтор через ${Math.ceil(delay / 1000)} с.`,
        "warn",
      );
      syncPollTimer = window.setTimeout(() => pollSyncJob(statusUrl), delay);
    }
  }

  async function startSyncAlignment() {
    appendLog("Stage 2 sync: запуск распознавания и сопоставления...");
    if (ids.btn_sync_cancel) {
      ids.btn_sync_cancel.disabled = false;
      ids.btn_sync_cancel.textContent = "Остановить после текущего шага";
    }
    const response = await optionalRequestJSON(
      "/stage2/sync-jobs/start",
      "POST",
      {
        project,
        timecode_mode: ids.sync_timecode_mode?.value || "relative",
        model: ids.sync_whisper_model?.value || "small",
        source_scope: stage3MediaCount > 0 ? "review_media" : "all",
      },
    );
    if (!response.available) {
      syncFeatureAvailable = false;
      renderSyncSources();
      renderSyncMatches({});
      appendLog("Stage 2 sync API пока недоступен; обычная сборка продолжает работать.", "warn");
      toast("Автосопоставление пока не установлено", "warn");
      return;
    }

    const job = syncJobPayload(response.result);
    const jobId = String(job.job_id || job.id || "");
    if (!jobId && String(job.status || "").toLowerCase() !== "completed") {
      throw new Error("Backend не вернул идентификатор задачи сопоставления");
    }
    syncFeatureAvailable = true;
    syncJobRunning = true;
    currentSyncJobId = jobId;
    currentSyncJobStatus = String(job.status || "queued").toLowerCase();
    renderSyncJob(job);
    updateActionAvailability();

    if (String(job.status || "").toLowerCase() === "completed") {
      finishSyncJob();
      await loadSyncWorkspace(true);
      return;
    }
    const suppliedUrl = String(job.status_url || "");
    const statusUrl = suppliedUrl.startsWith("/stage2/sync-jobs/")
      ? suppliedUrl
      : `/stage2/sync-jobs/${encodeURIComponent(jobId)}`;
    syncPollTimer = window.setTimeout(() => pollSyncJob(statusUrl), 500);
  }

  async function cancelSyncJob() {
    if (!currentSyncJobId || !syncJobRunning) return;
    try {
      const response = await optionalRequestJSON(
        `/stage2/sync-jobs/${encodeURIComponent(currentSyncJobId)}/cancel`,
        "POST",
        {},
      );
      if (!response.available) throw new Error("Backend не поддерживает остановку задачи");
      renderSyncJob(response.result);
      ids.btn_sync_cancel.disabled = true;
      ids.btn_sync_cancel.textContent = "Останавливается…";
      appendLog("Запрошена остановка сопоставления; уже сохранённые результаты останутся.", "warn");
    } catch (err) {
      toast(err.message || String(err), "error");
      appendLog(`Stage 2 sync cancel: ${err.message || err}`, "error");
    }
  }

  async function confirmSyncMatch(matchId, card) {
    const match = syncMatches.find((item) =>
      String(item.segment_id || item.block_uid || "") === matchId
    );
    if (!match) throw new Error("Совпадение больше не найдено; обновите список");
    const candidates = candidateChoices(match);
    const selectedIndex = Number(card?.querySelector("[data-sync-candidate]")?.value || 0);
    const candidate = candidates[selectedIndex] || matchCandidate(match);
    const sourceIn = Number(card?.querySelector("[data-sync-in]")?.value ?? candidate.source_in_sec);
    const sourceOut = Number(card?.querySelector("[data-sync-out]")?.value ?? candidate.source_out_sec);
    const sourceId = String(card?.dataset.sourceId || candidate.source_id || "");
    if (!sourceId || !Number.isFinite(sourceIn) || !Number.isFinite(sourceOut)
      || sourceIn < 0 || sourceOut <= sourceIn) {
      throw new Error("Проверьте источник и границы Source in/out");
    }
    const payload = {
      project,
      expected_revision: syncMatchesRevision,
      status: "confirmed",
      source_id: sourceId,
      source_in_sec: sourceIn,
      source_out_sec: sourceOut,
    };
    ["speech_in_sec", "speech_out_sec", "matched_text", "confidence"].forEach((key) => {
      if (candidate[key] !== undefined) payload[key] = candidate[key];
    });
    const response = await optionalRequestJSON(
      `/stage2/sync-matches/${encodeURIComponent(matchId)}`,
      "PATCH",
      payload,
    );
    if (!response.available) {
      appendLog("Подтверждение СНХ недоступно в текущей версии backend.", "warn");
      return;
    }
    toast("Диапазон СНХ подтверждён", "success");
    appendLog(`Stage 2 sync: подтверждён ${matchId}.`);
    await loadSyncWorkspace(true);
  }

  async function doBuildTimeline() {
    appendLog("Stage 2: Building timeline...");
    const f = formData();
    const result = await postJSON("/stage2/build", {
      project,
      raw_path: f.raw_output_path,
      speaker_dir: f.speaker_dir,
      fps: 25,
    });
    const { zk, snh, total_duration_sec } = result;
    appendLog(`Timeline built: ЗК=${zk}, Спикеры=${snh}, длит.=${Math.round(total_duration_sec)}с`);

    ids.ts_zk.textContent = zk;
    ids.ts_snh.textContent = snh;
    const m = Math.floor(total_duration_sec / 60);
    const s = Math.round(total_duration_sec % 60);
    ids.ts_dur.textContent = `${m}:${String(s).padStart(2, "0")}`;
    ids.timeline_stats.style.display = "flex";

    ids.btn_export_xml.href = `/stage2/export/xml?project=${encodeURIComponent(project)}`;
    ids.btn_export_xml.style.pointerEvents = "";
    ids.btn_export_xml.style.opacity = "";
  }

  ids.btn_sync_align.addEventListener("click", () => safely(startSyncAlignment));
  if (ids.btn_sync_cancel) ids.btn_sync_cancel.addEventListener("click", cancelSyncJob);
  ids.sync_review_list.addEventListener("click", (event) => {
    const button = event.target.closest("[data-action='confirm-sync-match']");
    if (!button) return;
    const matchId = String(button.dataset.matchId || "");
    if (matchId) safely(() => confirmSyncMatch(matchId, button.closest(".sync-match-card")));
  });
  ids.sync_review_list.addEventListener("change", (event) => {
    const select = event.target.closest("[data-sync-candidate]");
    const card = event.target.closest(".sync-match-card");
    if (select && card) {
      const match = syncMatches.find((item) =>
        String(item.segment_id || item.block_uid || "") === String(card.dataset.matchId || "")
      );
      const candidate = match ? candidateChoices(match)[Number(select.value)] : null;
      if (!candidate) return;
      const source = candidateSource(candidate);
      const sourceName = candidate.sequence_name || source.sequence_name
        || source.premiere_sequence || candidate.source_id;
      const confidence = Number(candidate.confidence);
      card.dataset.sourceId = candidate.source_id;
      card.querySelector("[data-sync-in]").value = Number(candidate.source_in_sec).toFixed(3);
      card.querySelector("[data-sync-out]").value = Number(candidate.source_out_sec).toFixed(3);
      card.querySelector("[data-sync-found]").textContent =
        candidate.matched_text || "Whisper-фрагмент не найден";
      card.querySelector("[data-sync-meta]").textContent =
        `${sourceName} · ${formatSourceSecond(candidate.source_in_sec)}–${formatSourceSecond(candidate.source_out_sec)}`
        + ` · уверенность ${Number.isFinite(confidence) ? Math.round(confidence * (confidence <= 1 ? 100 : 1)) + "%" : "—"}`;
      const audio = card.querySelector("[data-sync-audio]");
      audio.src = syncAudioUrl(candidate.source_id);
      audio.dataset.rangeIn = candidate.source_in_sec;
      audio.dataset.rangeOut = candidate.source_out_sec;
      audio.load();
    }
    if (card && event.target.matches("[data-sync-in], [data-sync-out]")) {
      const audio = card.querySelector("[data-sync-audio]");
      audio.dataset.rangeIn = card.querySelector("[data-sync-in]").value;
      audio.dataset.rangeOut = card.querySelector("[data-sync-out]").value;
    }
  });
  ids.sync_review_list.addEventListener("play", (event) => {
    const audio = event.target.closest("[data-sync-audio]");
    if (!audio) return;
    const start = Number(audio.dataset.rangeIn);
    const end = Number(audio.dataset.rangeOut);
    if (Number.isFinite(start) && (!Number.isFinite(audio.currentTime)
      || audio.currentTime < start || (Number.isFinite(end) && audio.currentTime >= end))) {
      audio.currentTime = start;
    }
  }, true);
  ids.sync_review_list.addEventListener("timeupdate", (event) => {
    const audio = event.target.closest("[data-sync-audio]");
    if (!audio) return;
    const end = Number(audio.dataset.rangeOut);
    if (Number.isFinite(end) && audio.currentTime >= end) audio.pause();
  }, true);
  ids.btn_build_timeline.addEventListener("click", () => safely(doBuildTimeline));
  if (ids.btn_stage3_media_scan) ids.btn_stage3_media_scan.addEventListener("click", () => safely(scanStage3Media));
  if (ids.btn_stage3_prepare) ids.btn_stage3_prepare.addEventListener("click", () => safely(prepareStage3Timeline));

  if (ids.btn_analyze) ids.btn_analyze.addEventListener("click", () => safely(doAnalyze));
  ids.btn_tts.addEventListener("click", () => safely(doTTS));
  if (ids.btn_tts_cancel) ids.btn_tts_cancel.addEventListener("click", cancelTTSJob);
  ids.btn_phrase_tts.addEventListener("click", () => safely(doPhraseTTS));
  ids.btn_validate.addEventListener("click", () => safely(doValidate));
  ids.btn_refresh.addEventListener("click", () => safely(async () => {
    await refreshStatus();
    await loadSyncWorkspace(true);
  }));
  ids.btn_run_all.addEventListener("click", () =>
    safely(async () => {
      await doTTS();
      await doValidate();
    })
  );
  ids.btn_clear_log.addEventListener("click", () => {
    ids.log_box.textContent = "";
  });

  ids.speed.addEventListener("input", () => {
    ids.speed_val.textContent = parseFloat(ids.speed.value).toFixed(2);
  });

  if (ids.use_llm) ids.use_llm.checked = false;
  ids.dry_run.checked = false;
  ids.provider.value = defaults.provider || "openrouter";
  ids.gender.value = defaults.gender || "female";
  applyStatus(initialStatus);
  renderLive();
  appendLog(`UI ready. project=${project}`);
  Promise.all([loadNormalization(true), loadMemory(true)]).catch((err) => appendLog(`Normalization init: ${err.message}`, "warn"));
  restoreTtsJob().catch((err) => appendLog(`TTS status init: ${err.message || err}`, "warn"));
  loadSyncWorkspace(true).catch((err) => appendLog(`Stage 2 sync init: ${err.message || err}`, "warn"));
})();
