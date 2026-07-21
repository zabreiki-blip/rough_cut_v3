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
    normalization_input_path: el("normalization_input_path"),
    normalization_hint: el("normalization_hint"),
    normalization_use_llm: el("normalization_use_llm"),
    normalization_status: el("normalization_status"),
    normalization_warnings: el("normalization_warnings"),
    normalization_empty: el("normalization_empty"),
    normalization_blocks: el("normalization_blocks"),
    btn_normalize: el("btn_normalize"),
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
    voiceover_lock: el("voiceover_lock"),
    assembly_lock: el("assembly_lock"),
    assembly_gate_message: el("assembly_gate_message"),
  };

  const toastContainer = document.getElementById("toast-container");

  let busy = false;
  let normalization = null;
  let memoryRules = [];
  let normalizationDirty = false;
  let normalizationEligible = initialStatus.normalization_confirmed !== false;
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
      ids.btn_normalize, ids.btn_save_normalization, ids.btn_confirm_normalization, ids.btn_add_normalization_block,
      ids.btn_add_memory].forEach((b) => {
      if (b) b.disabled = state;
    });
    if (!state) updateActionAvailability();
  }

  function isNormalizationConfirmed() {
    return Boolean(normalizationEligible && normalization && String(normalization.status || "").toLowerCase() === "confirmed");
  }

  function updateActionAvailability() {
    if (busy) return;
    const hasDraft = Boolean(normalization && Array.isArray(normalization.blocks));
    const confirmed = isNormalizationConfirmed();
    const blocked = !confirmed;
    if (ids.btn_save_normalization) ids.btn_save_normalization.disabled = !hasDraft || !normalizationDirty;
    if (ids.btn_confirm_normalization) {
      ids.btn_confirm_normalization.disabled = !hasDraft || (confirmed && !normalizationDirty);
    }
    if (ids.btn_add_normalization_block) ids.btn_add_normalization_block.disabled = !hasDraft;
    [ids.btn_tts, ids.btn_validate, ids.btn_run_all, ids.btn_build_timeline].forEach((button) => {
      if (button) button.disabled = blocked;
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
      throw new Error(data.error || JSON.stringify(data));
    }
    return data.result || {};
  }

  async function requestJSON(url, method, payload) {
    const options = { method, headers: { "Content-Type": "application/json" } };
    if (payload !== undefined) options.body = JSON.stringify(payload);
    const response = await fetch(url, options);
    const data = await response.json().catch(() => ({}));
    if (!response.ok || data.ok === false) {
      const detail = data.message || data.error || JSON.stringify(data);
      throw new Error(detail || `HTTP ${response.status}`);
    }
    return data.result !== undefined ? data.result : data;
  }

  const blockTypeLabels = {
    voiceover: "Закадр", sync: "Синхрон", host: "Ведущий",
    technical: "Технический", other: "Прочее", unknown: "Не определён",
  };
  const sourceLabels = {
    builtin_rule: "правило", rule: "правило", memory: "память",
    llm: "LLM", manual: "исправлено вручную", unknown: "не определено",
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
    ids.normalization_warnings.hidden = warnings.length === 0;
    ids.normalization_warnings.innerHTML = warnings.length
      ? `<strong>Требует внимания:</strong><br>${warnings.map(escaped).join("<br>")}` : "";
    ids.normalization_empty.hidden = blocks.length > 0;
    ids.normalization_blocks.innerHTML = "";

    blocks.forEach((block, index) => {
      const type = block.type || block.block_type || "unknown";
      const source = block.source || block.decision_source || "unknown";
      const blockWarnings = Array.isArray(block.warnings) ? block.warnings : [];
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
          <label class="field"><span>Нормализованный текст</span><textarea data-field="normalized_text">${escaped(blockValue(block, "normalized_text", "text"))}</textarea></label>
          <label class="field"><span>Текст для TTS</span><textarea data-field="text_for_tts">${escaped(blockValue(block, "text_for_tts", "tts_text"))}</textarea></label>
        </div>
        <div class="block-meta">
          <label class="field"><span>Исходное обозначение</span><input data-field="original_label" value="${escaped(blockValue(block, "original_label", "original_marker", "marker"))}"></label>
          <label class="field"><span>Спикер</span><input data-field="speaker" value="${escaped(blockValue(block, "speaker", "speaker_name"))}"></label>
          <label class="field"><span>Роль / должность</span><input data-field="role" value="${escaped(blockValue(block, "role"))}"></label>
          <label class="field"><span>Таймкод начала</span><input data-field="timecode_start" value="${escaped(blockValue(block, "timecode_start", "timecode", "start_timecode"))}"></label>
          <label class="field"><span>Таймкод конца</span><input data-field="timecode_end" value="${escaped(blockValue(block, "timecode_end", "end_timecode"))}"></label>
          <label class="field"><span>Заметка</span><input data-field="notes" value="${escaped(blockValue(block, "notes"))}"></label>
        </div>
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
    block[control.dataset.field] = control.value;
    if (control.dataset.field === "type") block.block_type = control.value;
    block.source = "manual";
    block.decision_source = "manual";
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
    const result = await requestJSON("/stage1/normalization/generate", "POST", {
      project, input_path: inputPath, hint: ids.normalization_hint.value.trim(),
      use_llm: ids.normalization_use_llm.checked,
    });
    normalization = canonicalDocument(result);
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
  }

  async function confirmNormalization() {
    const unknown = normalization.blocks.filter((block) => (block.type || block.block_type) === "unknown").length;
    if (unknown) throw new Error(`Исправьте неопределённые блоки: ${unknown}`);
    const result = await requestJSON("/stage1/normalization/confirm", "POST", {
      ...normalizationPayload(),
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
        <div class="memory-rule-meta">${escaped(status)} · подтверждений ${confirmations} · исправлений ${corrections}</div>
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

    appendLog(`TTS job queued: ${jobId}`);
    let lastMarker = "";
    let retryCount = 0;
    while (true) {
      const state = await getJSON(`/stage1/tts-jobs/${encodeURIComponent(jobId)}`);
      const total = Number(state.total || 0);
      const done = Number(state.done || 0);
      const event = String(state.event || "");
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

  document.querySelectorAll(".workspace-tab").forEach((tab) => {
    tab.addEventListener("click", () => openTab(tab.dataset.tab));
  });

  ids.normalization_blocks.addEventListener("input", (event) => {
    if (event.target.dataset.field) updateBlockFromControl(event.target);
  });
  ids.normalization_blocks.addEventListener("change", (event) => {
    if (event.target.dataset.field) updateBlockFromControl(event.target);
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

  ids.btn_build_timeline.addEventListener("click", () => safely(doBuildTimeline));

  if (ids.btn_analyze) ids.btn_analyze.addEventListener("click", () => safely(doAnalyze));
  ids.btn_tts.addEventListener("click", () => safely(doTTS));
  ids.btn_phrase_tts.addEventListener("click", () => safely(doPhraseTTS));
  ids.btn_validate.addEventListener("click", () => safely(doValidate));
  ids.btn_refresh.addEventListener("click", () => safely(refreshStatus));
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
})();
