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
    f_llm: el("f_llm"),
    f_manual: el("f_manual"),
    f_manifest: el("f_manifest"),
    progress_bar: el("progress_bar"),
    progress_label: el("progress_label"),
    live_job_status: el("live_job_status"),
    live_event: el("live_event"),
    live_current: el("live_current"),
    live_retries: el("live_retries"),
    live_done: el("live_done"),
    live_error: el("live_error"),
    speaker_items: el("speaker_items"),
    speaker_hint: el("speaker_hint"),
    log_box: el("log_box"),
  };

  let busy = false;
  const live = {
    status: "idle",
    event: "—",
    current: "—",
    retries: 0,
    done: 0,
    total: 0,
    error: "",
  };

  function now() {
    return new Date().toLocaleTimeString("ru-RU");
  }

  function appendLog(message, level = "info") {
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
    return map[event] || event || "—";
  }

  function renderLive() {
    if (!ids.live_job_status) return;
    ids.live_job_status.dataset.state = live.status || "idle";
    ids.live_job_status.textContent = live.status || "idle";
    ids.live_event.textContent = eventTitle(live.event);
    ids.live_current.textContent = live.current || "—";
    ids.live_retries.textContent = String(live.retries || 0);
    ids.live_done.textContent = `${live.done || 0}/${live.total || 0}`;
    ids.live_error.textContent = live.error ? `Ошибка: ${live.error}` : "—";
    ids.live_error.classList.toggle("has-error", Boolean(live.error));
  }

  function setLive(patch) {
    Object.assign(live, patch || {});
    renderLive();
  }

  function setBusy(state) {
    busy = state;
    [ids.btn_analyze, ids.btn_tts, ids.btn_phrase_tts, ids.btn_validate, ids.btn_run_all].forEach((b) => {
      if (b) b.disabled = state;
    });
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
      use_llm: ids.use_llm.checked,
      dry_run: ids.dry_run.checked,
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
    if (!value) {
      throw new Error(`${label} is required`);
    }
    if (!isLikelySafePath(value)) {
      throw new Error(`${label} looks invalid: ${value}`);
    }
  }

  async function postJSON(url, payload) {
    const response = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok || data.ok === false) {
      const err = data.error || JSON.stringify(data);
      throw new Error(err);
    }
    return data.result || {};
  }

  async function getJSON(url) {
    const response = await fetch(url);
    const data = await response.json().catch(() => ({}));
    if (!response.ok || data.ok === false) {
      const err = data.error || JSON.stringify(data);
      throw new Error(err);
    }
    return data.result || {};
  }

  function renderSpeaker(files) {
    ids.speaker_items.innerHTML = "";
    files.forEach((file) => {
      const wrap = document.createElement("div");
      wrap.className = "audio-item";
      const title = document.createElement("p");
      title.textContent = file;
      const audio = document.createElement("audio");
      audio.controls = true;
      audio.preload = "none";
      audio.src = `/speaker/${encodeURIComponent(project)}/${encodeURIComponent(file)}`;
      wrap.appendChild(title);
      wrap.appendChild(audio);
      ids.speaker_items.appendChild(wrap);
    });
  }

  function applyStatus(status) {
    const prepared = Number(status.prepared_items_count || 0);
    const wav = Number(status.speaker_files_count || 0);
    const missing = Array.isArray(status.missing_ids) ? status.missing_ids.length : 0;
    const pct = prepared > 0 ? Math.round((wav / prepared) * 100) : 0;

    ids.m_prepared.textContent = String(prepared);
    ids.m_wav.textContent = String(wav);
    ids.m_missing.textContent = String(missing);
    ids.f_llm.textContent = status.used_llm ? "yes" : "no";
    ids.f_manual.textContent = status.manual_markers_used ? "yes" : "no";
    ids.f_manifest.textContent = status.manifest_exists ? "ready" : "missing";
    ids.progress_bar.style.width = `${Math.max(0, Math.min(100, pct))}%`;
    ids.progress_label.textContent = `${pct}%`;
    ids.speaker_hint.textContent = `Файлы: ${wav}, не хватает: ${missing}`;

    renderSpeaker(Array.isArray(status.speaker_files) ? status.speaker_files : []);
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
    appendLog(`Analyze: ${f.input_path}`);
    const result = await postJSON("/stage1/analyze", {
      project,
      input_path: f.input_path,
      output_path: f.prepared_path,
      raw_output_path: f.raw_output_path,
      use_llm: f.use_llm,
    });
    appendLog(`Analyze done: blocks=${result.blocks_count}, llm=${result.used_llm}`);
    await refreshStatus();
  }

  async function doTTS() {
    const f = formData();
    assertRequiredPath(f.prepared_path, "prepared_path");
    assertRequiredPath(f.speaker_dir, "speaker_dir");
    setLive({
      status: "queued",
      event: "queued",
      current: "—",
      retries: 0,
      done: 0,
      total: 0,
      error: "",
    });
    appendLog(`TTS job: provider=${f.provider}, gender=${f.gender}, dry=${f.dry_run}`);
    const start = await postJSON("/stage1/tts-jobs/start", {
      project,
      prepared_path: f.prepared_path,
      speaker_dir: f.speaker_dir,
      provider: f.provider,
      gender: f.gender,
      voice_name: f.voice_name || null,
      dry_run: f.dry_run,
    });

    const jobId = String(start.job_id || "").trim();
    if (!jobId) {
      throw new Error("TTS job start failed: empty job_id");
    }

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
        ids.progress_label.textContent = `${pct}%`;
        ids.speaker_hint.textContent = `TTS: ${done}/${total}` + (currentId ? ` · ${currentId}` : "");
      }

      if (marker !== lastMarker) {
        if (event === "item_retrying") retryCount += 1;
        setLive({
          status: String(state.status || "running"),
          event: event || "running",
          current: currentId || "—",
          retries: retryCount,
          done,
          total,
          error: String(state.last_error || state.error || ""),
        });
        const level = event === "item_retrying" ? "warn" : event === "failed" ? "error" : "info";
        appendLog(`TTS ${event}: ${done}/${total}` + (currentId ? ` (${currentId})` : ""), level);
        lastMarker = marker;
      }

      if (state.status === "completed") {
        const result = state.result || {};
        setLive({
          status: "completed",
          event: "completed",
          current: "—",
          done: total,
          total,
          error: "",
        });
        appendLog(
          `TTS done: generated=${result.files_count || state.generated || 0}, skipped=${result.skipped_count || state.skipped || 0}, voice=${result.voice || state.voice || ""}`
        );
        break;
      }
      if (state.status === "failed") {
        setLive({
          status: "failed",
          event: "failed",
          error: String(state.last_error || state.error || "TTS job failed"),
          done,
          total,
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
      appendLog("ERROR: Введи текст фразы для озвучки.", true);
      ids.phrase_text.focus();
      return;
    }
    appendLog(`Phrase TTS: provider=${f.provider}, gender=${f.gender}, dry=${f.dry_run}`);
    const result = await postJSON("/stage1/tts-phrase", {
      project,
      text: phrase,
      speaker_dir: f.speaker_dir,
      provider: f.provider,
      gender: f.gender,
      voice_name: f.voice_name || null,
      file_name: f.phrase_file_name || null,
      dry_run: f.dry_run,
    });
    appendLog(`Phrase done: file=${result.file}, voice=${result.voice}`);
    if (result.playback_url && ids.phrase_player) {
      const audioUrl = `${result.playback_url}?t=${Date.now()}`;
      ids.phrase_player.src = audioUrl;
      ids.phrase_player.load();
    }
    await refreshStatus();
  }

  async function doValidate() {
    const f = formData();
    assertRequiredPath(f.prepared_path, "prepared_path");
    assertRequiredPath(f.speaker_dir, "speaker_dir");
    assertRequiredPath(f.manifest_path, "manifest_path");
    appendLog("Validate...");
    const result = await postJSON("/stage1/validate", {
      project,
      prepared_path: f.prepared_path,
      speaker_dir: f.speaker_dir,
      manifest_path: f.manifest_path,
    });
    appendLog(`Validate done: ok=${result.ok}, items=${result.items_count}`);
    await refreshStatus();
  }

  async function safely(fn) {
    if (busy) return;
    setBusy(true);
    try {
      await fn();
    } catch (err) {
      const msg = err.message || String(err);
      appendLog(`ERROR: ${msg}`, true);
      if (msg.includes("macOS TTS unavailable") || msg.includes("command 'say'")) {
        appendLog(
          "Подсказка: для macOS-озвучки запусти API локально на Mac или подними macOS-коннектор (MACOS_CONNECTOR_URL), либо выбери provider=openrouter/gemini.",
          true
        );
      }
    } finally {
      setBusy(false);
    }
  }

  ids.btn_analyze.addEventListener("click", () => safely(doAnalyze));
  ids.btn_tts.addEventListener("click", () => safely(doTTS));
  ids.btn_phrase_tts.addEventListener("click", () => safely(doPhraseTTS));
  ids.btn_validate.addEventListener("click", () => safely(doValidate));
  ids.btn_refresh.addEventListener("click", () => safely(refreshStatus));
  ids.btn_run_all.addEventListener("click", () =>
    safely(async () => {
      await doAnalyze();
      await doTTS();
      await doValidate();
    })
  );
  ids.btn_clear_log.addEventListener("click", () => {
    ids.log_box.textContent = "";
  });

  ids.use_llm.checked = false;
  ids.dry_run.checked = false;
  ids.provider.value = defaults.provider || "openrouter";
  ids.gender.value = defaults.gender || "female";
  applyStatus(initialStatus);
  renderLive();
  appendLog(`UI ready. project=${project}`);
})();
