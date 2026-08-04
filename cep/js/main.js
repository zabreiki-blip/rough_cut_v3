(function () {
  "use strict";

  var cs = new window.CSInterface();
  var activeSource = null;
  var guideAudioPath = "";
  var exportPollTimer = null;
  var exportInProgress = false;
  var whisperPollTimer = null;
  var currentWhisperJobId = "";
  var currentWhisperReviewCount = 0;
  var currentWhisperJobStatus = "";
  var CONFORM_VIDEO_TRACK_INDEX = 0;
  var CONFORM_AUDIO_TRACK_INDEX = 0;
  var hostFunctions = {
    ping: "rcStage2_ping",
    projects: "rcStage2_listProjects",
    inspect: "rcStage2_inspectActiveSequence",
    resolve: "rcStage2_resolveSequence",
    conform: "rcStage2_conformSequenceRanges",
    exportAudio: "rcStage2_exportActiveSequenceAudio",
    fileInfo: "rcStage2_fileInfo"
  };

  function byId(id) {
    return document.getElementById(id);
  }

  function now() {
    var date = new Date();
    return date.toLocaleTimeString ? date.toLocaleTimeString() : String(date);
  }

  function log(message, data) {
    var target = byId("log");
    var notice = byId("panelNotice");
    var line = "[" + now() + "] " + message;
    if (data !== undefined) {
      try {
        line += "\n" + JSON.stringify(data, null, 2);
      } catch (error) {
        line += "\n" + String(data);
      }
    }
    target.textContent = target.textContent ? line + "\n\n" + target.textContent : line;
    if (notice) {
      notice.className = "panel-notice";
      if (/(ошиб|недоступ|не удалось|не выполн|не запущ|не загруж|не найден|не разреш|нельзя|отклон|остановлен|проверьте|укажите|введите|отмен)/i.test(message)) {
        notice.className += " panel-notice-error";
      } else if (/(готов|заверш|зарегистрирован|сохран|обновл|получен|выбран|поставлен)/i.test(message)) {
        notice.className += " panel-notice-success";
      }
      notice.textContent = message;
    }
  }

  function setHostBadge(state, text) {
    var badge = byId("hostBadge");
    badge.className = "badge badge-" + state;
    badge.textContent = text;
  }

  function parseHostResult(raw) {
    if (!raw || raw === "undefined") {
      throw new Error("Premiere вернул пустой результат.");
    }
    if (/EvalScript|undefined is not an object|ReferenceError/.test(raw)) {
      throw new Error(raw);
    }
    try {
      return JSON.parse(raw);
    } catch (error) {
      throw new Error("Некорректный ответ host.jsx: " + raw);
    }
  }

  function callHost(method, payload, callback) {
    var functionName = hostFunctions[method];
    if (!functionName) {
      callback(new Error("Неизвестная host-команда: " + method));
      return;
    }

    var script = functionName + "()";
    if (payload !== undefined) {
      // URI encoding keeps Cyrillic and quotes out of the ExtendScript source.
      var encoded = encodeURIComponent(JSON.stringify(payload));
      script = functionName + "(" + JSON.stringify(encoded) + ")";
    }

    cs.evalScript(script, function (raw) {
      var result;
      try {
        result = parseHostResult(raw);
      } catch (error) {
        callback(error);
        return;
      }
      if (!result.ok) {
        callback(new Error(result.error || "Host-команда завершилась ошибкой."), result);
        return;
      }
      callback(null, result);
    });
  }

  function normalizeBaseUrl(value) {
    return String(value || "").replace(/\/+$/, "");
  }

  function config() {
    return {
      project: byId("projectName").value.trim(),
      apiBaseUrl: normalizeBaseUrl(byId("apiBaseUrl").value),
      token: byId("apiToken").value,
      projectsRoot: String(byId("projectsRoot").value || "").replace(/[\/\\]+$/, ""),
      transcribePath: byId("transcribePath").value.trim()
    };
  }

  function replaceProjectOptions(projects, selectedProject) {
    var select = byId("projectName");
    var items = Array.isArray(projects) ? projects : [];
    var selected = String(selectedProject || "");
    var i;
    var option;

    while (select.firstChild) {
      select.removeChild(select.firstChild);
    }
    if (!items.length) {
      option = document.createElement("option");
      option.value = "";
      option.textContent = "Проекты не найдены";
      select.appendChild(option);
      return;
    }
    for (i = 0; i < items.length; i++) {
      option = document.createElement("option");
      option.value = String(items[i]);
      option.textContent = String(items[i]);
      select.appendChild(option);
    }
    select.value = items.indexOf(selected) >= 0 ? selected : String(items[0]);
    window.localStorage.setItem("roughCutStage2.project", select.value);
  }

  function loadProjects() {
    var refresh = byId("refreshProjects");
    var selected = byId("projectName").value ||
      window.localStorage.getItem("roughCutStage2.project") || "";
    var current = config();
    refresh.disabled = true;

    function loadFromBackend(hostError) {
      if (!current.token) {
        refresh.disabled = false;
        log("Список проектов не загружен: " + hostError.message);
        return;
      }
      request("GET", "/projects", null, function (error, response) {
        refresh.disabled = false;
        if (error) {
          log("Список проектов не загружен: " + error.message);
          return;
        }
        replaceProjectOptions(response.projects || [], selected);
        log("Список проектов получен из backend: " + (response.projects || []).length + ".");
      });
    }

    if (!current.projectsRoot) {
      loadFromBackend(new Error("Укажите общий каталог projects."));
      return;
    }
    callHost("projects", { projectsRoot: current.projectsRoot }, function (error, response) {
      if (error) {
        loadFromBackend(error);
        return;
      }
      refresh.disabled = false;
      replaceProjectOptions(response.projects || [], selected);
      log("Список проектов обновлён: " + (response.projects || []).length + ".");
    });
  }

  function saveConfig() {
    var current = config();
    window.localStorage.setItem("roughCutStage2.project", current.project);
    window.localStorage.setItem("roughCutStage2.apiBaseUrl", current.apiBaseUrl);
    window.localStorage.setItem("roughCutStage2.projectsRoot", current.projectsRoot);
    window.localStorage.setItem("roughCutStage2.transcribePath", current.transcribePath);
    // The token intentionally remains memory-only.
    byId("apiBaseUrl").value = current.apiBaseUrl;
    log("Настройки сохранены. API token не записан на диск.");
    loadProjects();
    refreshWhisperStatus();
  }

  function loadConfig() {
    var savedProject = window.localStorage.getItem("roughCutStage2.project") || "";
    replaceProjectOptions(
      savedProject ? [savedProject] : [],
      savedProject
    );
    byId("apiBaseUrl").value = window.localStorage.getItem("roughCutStage2.apiBaseUrl") || "http://127.0.0.1:8000";
    byId("projectsRoot").value = window.localStorage.getItem("roughCutStage2.projectsRoot") || "";
    byId("transcribePath").value =
      window.localStorage.getItem("roughCutStage2.transcribePath") ||
      "/stage2/sync-jobs/start";
  }

  function request(method, path, body, callback) {
    var current = config();
    var finished = false;
    function finish(error, payload) {
      if (finished) {
        return;
      }
      finished = true;
      callback(error, payload);
    }
    if (!current.apiBaseUrl) {
      finish(new Error("Укажите Backend URL."));
      return;
    }
    if (!current.token) {
      finish(new Error("Введите API token."));
      return;
    }

    var xhr = new XMLHttpRequest();
    xhr.open(method, current.apiBaseUrl + path, true);
    xhr.timeout = 30000;
    xhr.setRequestHeader("Accept", "application/json");
    xhr.setRequestHeader("Authorization", "Bearer " + current.token);
    if (body !== null) {
      xhr.setRequestHeader("Content-Type", "application/json");
    }
    xhr.onreadystatechange = function () {
      var parsed;
      if (xhr.readyState !== 4) {
        return;
      }
      try {
        parsed = xhr.responseText ? JSON.parse(xhr.responseText) : {};
      } catch (error) {
        finish(new Error("Backend вернул не JSON (HTTP " + xhr.status + ")."));
        return;
      }
      if (xhr.status < 200 || xhr.status >= 300) {
        finish(new Error(parsed.error || parsed.message || ("HTTP " + xhr.status)), parsed);
        return;
      }
      if (parsed.ok === false) {
        finish(new Error(parsed.error || parsed.message || "Backend отклонил запрос."), parsed);
        return;
      }
      finish(null, parsed);
    };
    xhr.onerror = function () {
      finish(new Error("Backend недоступен или соединение было прервано."));
    };
    xhr.ontimeout = function () {
      finish(new Error("Backend не ответил за 30 секунд."));
    };
    xhr.send(body === null ? null : JSON.stringify(body));
  }

  function whisperStatusLabel(status) {
    var labels = {
      queued: "в очереди",
      running: "в работе",
      completed: "готово",
      interrupted: "прервано",
      cancelled: "прервано",
      cancelling: "останавливается",
      failed: "ошибка"
    };
    return labels[status] || status || "ожидание";
  }

  function whisperMessage(job) {
    var message = String(job.message || "");
    if (message === "Preparing word indexes" || message === "Transcribing audio") {
      return "Распознавание аудио";
    }
    if (message === "Matching scenario quotes") {
      return "Сопоставление реплик со сценарием";
    }
    if (message === "Queued") {
      return "Задача ожидает запуска";
    }
    if (job.status === "completed") {
      return "Распознавание и сопоставление завершены";
    }
    if (job.status === "failed") {
      return job.error || "Распознавание завершилось с ошибкой";
    }
    if (job.status === "interrupted" || job.status === "cancelled") {
      return "Работа была прервана. Сохранённые подтверждения не потеряны; запустите распознавание снова, чтобы продолжить.";
    }
    return message || "Ожидание данных распознавания";
  }

  function formatDuration(seconds) {
    var value = Math.max(0, Math.round(Number(seconds) || 0));
    var hours = Math.floor(value / 3600);
    var minutes = Math.floor((value % 3600) / 60);
    var secs = value % 60;
    function pad(number) {
      return number < 10 ? "0" + number : String(number);
    }
    return hours > 0
      ? hours + ":" + pad(minutes) + ":" + pad(secs)
      : minutes + ":" + pad(secs);
  }

  function elapsedSince(isoDate) {
    var started = Date.parse(String(isoDate || ""));
    return isFinite(started) ? Math.max(0, (Date.now() - started) / 1000) : 0;
  }

  function etaFromProgress(elapsed, done, total) {
    if (elapsed <= 0 || done <= 0 || total <= done) {
      return 0;
    }
    return Math.max(0, elapsed * (total - done) / done);
  }

  function renderWhisperIdle(message) {
    var fill = byId("whisperProgressFill");
    currentWhisperReviewCount = 0;
    currentWhisperJobStatus = "";
    byId("resumeWhisper").hidden = true;
    byId("cancelWhisper").hidden = true;
    byId("whisperStatus").className = "badge badge-muted";
    byId("whisperStatus").textContent = "ожидание";
    byId("whisperMessage").textContent = message || "Задача ещё не запущена.";
    byId("whisperCurrent").textContent = "Источник: —";
    byId("whisperProgressText").textContent = "—";
    byId("whisperEta").textContent = "Осталось: —";
    fill.className = "whisper-progress-fill";
    fill.style.width = "0";
    byId("whisperProgress").removeAttribute("aria-valuenow");
    byId("whisperReviewNotice").hidden = true;
    byId("whisperReviewNoticeCount").textContent = "0";
  }

  function whisperJobIsActive() {
    return currentWhisperJobStatus === "queued" ||
      currentWhisperJobStatus === "running" ||
      currentWhisperJobStatus === "cancelling";
  }

  function renderWhisperJob(job) {
    var status = String(job.status || "").toLowerCase();
    var total = Number(job.total || job.total_segments || 0);
    var done = Number(job.done || 0);
    var sourceFailures = Array.isArray(job.source_failures) ? job.source_failures.length : 0;
    var sourcesDone = Number(job.transcribed || 0) + Number(job.reused_indexes || 0) + sourceFailures;
    var totalSources = Number(job.total_sources || 0);
    var percent = total > 0 ? Math.max(0, Math.min(100, Math.round(done * 100 / total))) : 0;
    var audioDone = Number(job.transcription_processed_sec || 0);
    var audioTotal = Number(job.transcription_duration_sec || 0);
    var wordCount = Number(job.transcription_words || 0);
    var transcriptionElapsed = elapsedSince(
      job.transcription_source_started_at || job.started_at
    );
    var matchingElapsed = elapsedSince(job.matching_started_at);
    var etaSeconds = 0;
    var fill = byId("whisperProgressFill");
    var badge = byId("whisperStatus");
    var reviewCount = Number(
      job.needs_review !== undefined ? job.needs_review : (job.review || 0)
    );
    var reviewNotice = byId("whisperReviewNotice");
    var current = job.current_source_id
      ? "Источник: " + job.current_source_id
      : (job.current_segment_id ? "Реплика: " + job.current_segment_id : "Источник: —");

    badge.textContent = whisperStatusLabel(status);
    badge.className = "badge " + (status === "failed" ? "badge-error" :
      (status === "completed" ? "badge-ok" : "badge-muted"));
    byId("whisperMessage").textContent = whisperMessage(job);
    byId("whisperCurrent").textContent = current;
    byId("whisperSources").textContent = sourcesDone + " / " + totalSources;
    byId("whisperSegments").textContent = done + " / " + Number(job.total_segments || total || 0);
    byId("whisperMatched").textContent = Number(job.matched || 0);
    currentWhisperJobStatus = status;
    byId("resumeWhisper").hidden = !(
      status === "interrupted" || status === "cancelled" || status === "failed"
    );
    byId("cancelWhisper").hidden = !(
      status === "queued" || status === "running" || status === "cancelling"
    );
    byId("exportAndTranscribe").disabled = exportInProgress || whisperJobIsActive();
    currentWhisperReviewCount = reviewCount;
    byId("whisperReview").textContent = String(reviewCount);
    byId("whisperUnmatched").textContent = Number(job.unmatched || 0);
    byId("whisperReviewNoticeCount").textContent = String(reviewCount);
    reviewNotice.hidden = reviewCount <= 0;
    fill.className = "whisper-progress-fill";

    if (status === "failed") {
      fill.className += " is-error";
      fill.style.width = "100%";
      byId("whisperProgressText").textContent = "ошибка";
      byId("whisperEta").textContent = "Осталось: —";
      byId("whisperProgress").removeAttribute("aria-valuenow");
    } else if ((status === "queued" || status === "running") && total <= 0 && audioTotal <= 0) {
      fill.className += " is-indeterminate";
      fill.style.width = "";
      byId("whisperProgressText").textContent = transcriptionElapsed > 0
        ? "Прошло " + formatDuration(transcriptionElapsed)
        : "Распознавание идёт";
      byId("whisperEta").textContent = "Осталось: рассчитывается";
      byId("whisperProgress").removeAttribute("aria-valuenow");
    } else if ((status === "queued" || status === "running") && audioTotal > 0 && total <= 0) {
      percent = Math.max(0, Math.min(100, Math.round(audioDone * 100 / audioTotal)));
      etaSeconds = etaFromProgress(transcriptionElapsed, audioDone, audioTotal);
      fill.style.width = percent + "%";
      byId("whisperProgressText").textContent =
        formatDuration(audioDone) + " / " + formatDuration(audioTotal) +
        (wordCount > 0 ? " · " + wordCount + " слов" : "");
      byId("whisperEta").textContent = etaSeconds > 0
        ? "Осталось: ≈ " + formatDuration(etaSeconds)
        : "Осталось: рассчитывается";
      byId("whisperProgress").setAttribute("aria-valuenow", String(percent));
      byId("whisperProgress").setAttribute("aria-valuemin", "0");
      byId("whisperProgress").setAttribute("aria-valuemax", "100");
    } else {
      if (status === "completed") percent = 100;
      etaSeconds = etaFromProgress(matchingElapsed, done, total);
      fill.style.width = percent + "%";
      byId("whisperProgressText").textContent = status === "completed"
        ? done + " / " + total + " реплик · 100%"
        : done + " / " + total + " реплик · " + percent + "%";
      byId("whisperEta").textContent = status === "completed"
        ? "Готово"
        : (etaSeconds > 0
          ? "Осталось: ≈ " + formatDuration(etaSeconds)
          : "Осталось: рассчитывается");
      byId("whisperProgress").setAttribute("aria-valuenow", String(percent));
      byId("whisperProgress").setAttribute("aria-valuemin", "0");
      byId("whisperProgress").setAttribute("aria-valuemax", "100");
    }
  }

  function scheduleWhisperPoll(delay) {
    if (whisperPollTimer) {
      window.clearTimeout(whisperPollTimer);
    }
    whisperPollTimer = window.setTimeout(refreshWhisperStatus, delay);
  }

  function refreshWhisperStatus(options) {
    options = options || {};
    var current = config();
    var path;
    var requestedJobId = currentWhisperJobId;
    var manual = Boolean(options.manual);
    var allowProjectFallback = options.allowProjectFallback !== false;
    if (whisperPollTimer) {
      window.clearTimeout(whisperPollTimer);
      whisperPollTimer = null;
    }
    if (!current.project || !current.apiBaseUrl || !current.token) {
      renderWhisperIdle("Введите ключ доступа, чтобы видеть прогресс.");
      if (manual) {
        log("Статус распознавания не обновлён: проверьте проект, адрес сервера и ключ доступа.");
      }
      return;
    }
    path = requestedJobId
      ? "/stage2/sync-jobs/" + encodeURIComponent(requestedJobId)
      : "/stage2/sync-jobs/status?project=" + encodeURIComponent(current.project);
    request("GET", path, null, function (error, response) {
      var job;
      if (error) {
        if (requestedJobId && error.message === "job not found" && allowProjectFallback) {
          currentWhisperJobId = "";
          refreshWhisperStatus({
            manual: manual,
            allowProjectFallback: false
          });
          return;
        }
        if (error.message === "job not found") {
          renderWhisperIdle("Для проекта пока нет задачи распознавания.");
        } else {
          renderWhisperIdle("Статус распознавания недоступен: " + error.message);
        }
        if (manual) {
          log("Статус распознавания не обновлён: " + error.message);
        }
        scheduleWhisperPoll(10000);
        return;
      }
      job = response.result || response;
      currentWhisperJobId = job.id || currentWhisperJobId;
      renderWhisperJob(job);
      if (manual) {
        log("Статус распознавания обновлён.", job);
      }
      scheduleWhisperPoll(
        job.status === "queued" || job.status === "running" || job.status === "cancelling" ? 2500 : 10000
      );
    });
  }

  function refreshWhisperStatusManually() {
    currentWhisperJobId = "";
    log("Обновляю статус распознавания…");
    refreshWhisperStatus({
      manual: true,
      allowProjectFallback: false
    });
  }

  function renderSource(sequence) {
    var container = byId("activeSequence");
    var generatedId;
    var sourceName;
    var changed = !activeSource || activeSource.sequenceId !== sequence.sequenceId;
    if (changed) {
      guideAudioPath = "";
      byId("guideOutputPath").value = "";
      byId("sourceId").value = "";
      byId("speakerName").value = "";
      byId("allSpeakers").checked = false;
    }
    activeSource = sequence;
    container.className = "sequence-summary";
    container.innerHTML =
      '<div class="sequence-title"></div>' +
      '<div class="sequence-meta"></div>';
    container.firstChild.textContent = sequence.name;
    container.lastChild.textContent =
      "ID " + sequence.sequenceId +
      " · " + sequence.durationSeconds.toFixed(2) + " сек" +
      " · V" + sequence.videoTrackCount + "/A" + sequence.audioTrackCount;
    byId("registerSequence").disabled = Boolean(sourceRegistrationBlockReason(sequence));
    byId("resolveSequence").disabled = false;
    byId("exportAndTranscribe").disabled =
      Boolean(sourceRegistrationBlockReason(sequence)) || whisperJobIsActive();
    if (!byId("sourceId").value.trim()) {
      sourceName = String(sequence.name || "");
      generatedId = /[^\x00-\x7F]/.test(sourceName) ? "" : sourceName
          .toLowerCase()
          .replace(/\s+/g, "_")
          .replace(/[^\w-]+/g, "")
          .replace(/^_+|_+$/g, "");
      byId("sourceId").value = generatedId || (
        "sequence_" + String(sequence.sequenceId).replace(/[^A-Za-z0-9_-]/g, "").slice(0, 32)
      );
    }
    if (changed) {
      hydrateSourceForm(sequence);
    }
  }

  function setSourceActionsDisabled(disabled) {
    var roleBlocked = Boolean(sourceRegistrationBlockReason(activeSource));
    byId("registerSequence").disabled = disabled || roleBlocked;
    byId("resolveSequence").disabled = disabled;
    byId("exportAndTranscribe").disabled =
      disabled || roleBlocked || exportInProgress || whisperJobIsActive();
  }

  function sourceRegistrationBlockReason(sequence) {
    var guard = sequence && sequence.roughCutGuard;
    if (!sequence) {
      return "Сначала считайте активную секвенцию.";
    }
    if (!guard || !guard.inspectionComplete) {
      return "Не удалось безопасно определить роль секвенции. Регистрация и расшифровка запрещены.";
    }
    if (guard.isRoughCutTarget) {
      return "Монтажную секвенцию с RC_BLOCK/RC_SYNC нельзя регистрировать или расшифровывать как sync-источник.";
    }
    return "";
  }

  function normalizedTreePath(value) {
    return String(value || "").replace(/\\/g, "/");
  }

  function sourceIdentityMatchesSequence(source, sequence) {
    var metadata = (source && source.metadata) || {};
    return Boolean(
      (source.sequence_id && sequence.sequenceId &&
        String(source.sequence_id) === String(sequence.sequenceId)) ||
      (metadata.project_item_node_id && sequence.projectItemNodeId &&
        String(metadata.project_item_node_id) === String(sequence.projectItemNodeId) &&
        source.project_path && sequence.projectPath &&
        normalizedTreePath(source.project_path) === normalizedTreePath(sequence.projectPath)) ||
      (metadata.project_item_tree_path && sequence.projectItemTreePath &&
        normalizedTreePath(metadata.project_item_tree_path) ===
          normalizedTreePath(sequence.projectItemTreePath))
    );
  }

  function registeredSourceGuard(source) {
    var metadata = source.metadata || {};
    return {
      id: source.id || source.source_id || "",
      sequenceId: source.sequence_id || "",
      projectPath: source.project_path || "",
      projectItemNodeId: metadata.project_item_node_id || "",
      projectItemTreePath: metadata.project_item_tree_path || ""
    };
  }

  function absoluteGuidePath(source, current) {
    var metadata = source.metadata || {};
    var configured = String(metadata.guide_audio_absolute || source.guide_audio || "");
    if (!configured) {
      return "";
    }
    if (/^(?:[A-Za-z]:[\\/]|\/)/.test(configured)) {
      return configured;
    }
    if (!current.projectsRoot || !current.project) {
      return "";
    }
    return current.projectsRoot + "/" + current.project + "/" + configured.replace(/^\/+/, "");
  }

  function hydrateSourceForm(sequence) {
    var current = config();
    if (!current.project || !current.apiBaseUrl || !current.token) {
      return;
    }
    setSourceActionsDisabled(true);
    request(
      "GET",
      "/stage2/sync-sources?project=" + encodeURIComponent(current.project),
      null,
      function (error, response) {
        var items;
        var existing = null;
        var i;
        if (!activeSource || activeSource.sequenceId !== sequence.sequenceId) {
          return;
        }
        setSourceActionsDisabled(false);
        if (error) {
          log("Не удалось проверить регистрацию sync-источника: " + error.message);
          return;
        }
        items = (response.result && response.result.sources) || response.sources || [];
        for (i = 0; i < items.length; i++) {
          if (String(items[i].sequence_id || "") === String(sequence.sequenceId || "")) {
            existing = items[i];
            break;
          }
          if (items[i].metadata &&
              String(items[i].metadata.project_item_node_id || "") ===
                String(sequence.projectItemNodeId || "")) {
            existing = items[i];
            break;
          }
        }
        if (!existing) {
          return;
        }
        byId("sourceId").value = existing.id || existing.source_id || "";
        var registeredSpeakers = [];
        if (existing.speaker) registeredSpeakers.push(existing.speaker);
        if (Array.isArray(existing.speakers)) {
          registeredSpeakers = registeredSpeakers.concat(existing.speakers);
        }
        byId("speakerName").value = registeredSpeakers.join(", ");
        byId("allSpeakers").checked = Boolean(existing.all_speakers);
        guideAudioPath = absoluteGuidePath(existing, current);
        byId("guideOutputPath").value = guideAudioPath || defaultGuideOutput();
        log("Найдена существующая регистрация sync-источника.", existing);
      }
    );
  }

  function secondsToTimecode(seconds, fps) {
    var safeFps = Math.max(1, Math.round(Number(fps) || 25));
    var totalFrames = Math.max(0, Math.round(Number(seconds || 0) * safeFps));
    var frames = totalFrames % safeFps;
    var totalSeconds = Math.floor(totalFrames / safeFps);
    var secs = totalSeconds % 60;
    var minutes = Math.floor(totalSeconds / 60) % 60;
    var hours = Math.floor(totalSeconds / 3600);
    function pad(value) {
      return value < 10 ? "0" + value : String(value);
    }
    return pad(hours) + ":" + pad(minutes) + ":" + pad(secs) + ":" + pad(frames);
  }

  function inspectActive() {
    log("Считываю активную секвенцию…");
    callHost("inspect", undefined, function (error, response) {
      if (error) {
        log("Не удалось считать секвенцию: " + error.message);
        return;
      }
      renderSource(response.sequence);
      var blocked = sourceRegistrationBlockReason(response.sequence);
      if (blocked) {
        log(blocked, response.sequence);
      } else {
        log("Активная секвенция сохранена как источник.", response.sequence);
      }
    });
  }

  function registerSequence() {
    registerSequenceWithCallback();
  }

  function guideReference(outputPath, current) {
    var root = String(current.projectsRoot || "").replace(/\\/g, "/");
    var absolute = String(outputPath || "").replace(/\\/g, "/");
    var projectPrefix = root + "/" + current.project + "/";
    if (root && absolute.indexOf(projectPrefix) === 0) {
      return absolute.slice(projectPrefix.length);
    }
    return absolute;
  }

  function registerSequenceWithCallback(callback) {
    var current = config();
    var sourceId = byId("sourceId").value.trim();
    var timebase = Number(activeSource && activeSource.timebaseTicks);
    var fps = isFinite(timebase) && timebase > 0 ? 254016000000 / timebase : 25;
    var done = callback || function () {};
    if (!activeSource) {
      log("Сначала считайте активную секвенцию.");
      done(new Error("No active source."));
      return;
    }
    var roleBlock = sourceRegistrationBlockReason(activeSource);
    if (roleBlock) {
      log("Регистрация не выполнена: " + roleBlock);
      done(new Error(roleBlock));
      return;
    }
    if (!current.project) {
      log("Укажите имя проекта.");
      done(new Error("Project is required."));
      return;
    }
    if (!sourceId) {
      log("Укажите стабильный Source ID.");
      done(new Error("Source ID is required."));
      return;
    }
    if (!/^[A-Za-z0-9_-]{1,96}$/.test(sourceId)) {
      log("Source ID: только латинские буквы, цифры, _ и - (до 96 символов).");
      done(new Error("Invalid Source ID."));
      return;
    }

    var speakerNames = byId("speakerName").value.split(/[,;\n]+/).map(function (value) {
      return value.trim();
    }).filter(function (value) {
      return Boolean(value);
    });
    var payload = {
      project: current.project,
      id: sourceId,
      source_id: sourceId,
      speaker: speakerNames[0] || "",
      speakers: speakerNames.slice(1),
      all_speakers: byId("allSpeakers").checked,
      premiere_sequence: activeSource.name,
      sequence_id: activeSource.sequenceId,
      sequence_name: activeSource.name,
      sequence_start_tc: secondsToTimecode(activeSource.zeroPointSeconds, fps),
      project_path: activeSource.projectPath,
      fps: fps,
      duration_sec: activeSource.durationSeconds,
      guide_audio: guideAudioPath ? guideReference(guideAudioPath, current) : "",
      guide_offset_sec: 0,
      metadata: {
        sequence_role: activeSource.roughCutGuard.isRoughCutTarget
          ? "rough_cut_target"
          : "sync_source",
        rough_cut_target: Boolean(activeSource.roughCutGuard.isRoughCutTarget),
        guide_audio_absolute: guideAudioPath,
        project_item_node_id: activeSource.projectItemNodeId,
        project_item_tree_path: activeSource.projectItemTreePath,
        zero_point_sec: activeSource.zeroPointSeconds,
        zero_point_ticks: activeSource.zeroPointTicks,
        timebase_ticks: activeSource.timebaseTicks,
        video_display_format: activeSource.videoDisplayFormat,
        video_track_count: activeSource.videoTrackCount,
        audio_track_count: activeSource.audioTrackCount
      }
    };

    log("Регистрирую sync-источник…");
    request("POST", "/stage2/sync-sources", payload, function (error, response) {
      if (error) {
        log("Регистрация не выполнена: " + error.message, response);
        done(error);
        return;
      }
      log("Sync-источник зарегистрирован.", response);
      done(null, response);
    });
  }

  function defaultGuideOutput() {
    var current = config();
    var sourceId = byId("sourceId").value.trim();
    if (!current.projectsRoot || !current.project || !sourceId) {
      return "";
    }
    return current.projectsRoot + "/" + current.project + "/work/sync_audio/" + sourceId + ".wav";
  }

  function startTranscriptionJob() {
    var current = config();
    var sourceId = byId("sourceId").value.trim();
    var path = current.transcribePath || "/stage2/sync-jobs/start";
    request("POST", path, {
      project: current.project,
      source_ids: [sourceId]
    }, function (error, response) {
      if (error) {
        log("WAV готов и зарегистрирован, но Whisper job не запущен: " + error.message, response);
        return;
      }
      log("Whisper job запущен.", response);
      var job = response.result || response;
      currentWhisperJobId = job.job_id || job.id || "";
      renderWhisperJob({
        id: currentWhisperJobId,
        status: job.status || "queued",
        message: "Queued",
        current_source_id: sourceId,
        total_sources: 1
      });
      scheduleWhisperPoll(1000);
    });
  }

  function resumeTranscriptionJob() {
    if (!byId("sourceId").value.trim()) {
      log("Продолжение не запущено: сначала выберите или считайте sync-источник.");
      return;
    }
    log("Продолжаю распознавание без повторного экспорта WAV…");
    startTranscriptionJob();
  }

  function cancelTranscriptionJob() {
    if (!currentWhisperJobId || !(
      currentWhisperJobStatus === "queued" ||
      currentWhisperJobStatus === "running" ||
      currentWhisperJobStatus === "cancelling"
    )) {
      return;
    }
    byId("cancelWhisper").disabled = true;
    request(
      "POST",
      "/stage2/sync-jobs/" + encodeURIComponent(currentWhisperJobId) + "/cancel",
      {},
      function (error, response) {
        byId("cancelWhisper").disabled = false;
        if (error) {
          log("Остановка не выполнена: " + error.message, response);
          return;
        }
        log("Остановка запрошена. Индексы и найденные диапазоны сохраняются.", response);
        renderWhisperJob(response.result || response);
        scheduleWhisperPoll(500);
      }
    );
  }

  function pollExport(outputPath, jobId) {
    var previousSize = -1;
    var previousModified = -1;
    var stableChecks = 0;
    var attempts = 0;
    var maxAttempts = 900;

    function check() {
      attempts += 1;
      callHost("fileInfo", { path: outputPath }, function (error, response) {
        var file;
        if (error) {
          log("Не удалось проверить guide WAV: " + error.message);
          if (attempts < maxAttempts) {
            exportPollTimer = window.setTimeout(check, 2000);
          } else {
            exportPollTimer = null;
            exportInProgress = false;
            byId("exportAndTranscribe").disabled = whisperJobIsActive();
          }
          return;
        }
        file = response.file;
        if (file.exists && file.size > 0 &&
            file.size === previousSize && file.modifiedMs === previousModified) {
          stableChecks += 1;
        } else {
          stableChecks = 0;
        }
        previousSize = file.size;
        previousModified = file.modifiedMs;

        if (stableChecks >= 3) {
          exportPollTimer = null;
          exportInProgress = false;
          guideAudioPath = file.path || outputPath;
          log("Guide WAV готов: " + file.size + " байт.", file);
          registerSequenceWithCallback(function (registerError) {
            if (!registerError) {
              startTranscriptionJob();
            }
          });
          return;
        }
        if (attempts >= maxAttempts) {
          exportPollTimer = null;
          exportInProgress = false;
          byId("exportAndTranscribe").disabled = whisperJobIsActive();
          log("Ожидание экспорта остановлено через 30 минут. Job ID: " + jobId);
          return;
        }
        exportPollTimer = window.setTimeout(check, 2000);
      });
    }

    if (exportPollTimer) {
      window.clearTimeout(exportPollTimer);
    }
    exportPollTimer = window.setTimeout(check, 1000);
  }

  function exportAndTranscribe() {
    var outputPath = byId("guideOutputPath").value.trim() || defaultGuideOutput();
    var presetPath = byId("presetPath").value.trim();
    if (!activeSource) {
      log("Сначала считайте активную sync-секвенцию.");
      return;
    }
    var roleBlock = sourceRegistrationBlockReason(activeSource);
    if (roleBlock) {
      log("Экспорт и расшифровка не запущены: " + roleBlock);
      return;
    }
    if (!outputPath) {
      log("Укажите Guide WAV или настройте общий каталог projects, проект и Source ID.");
      return;
    }
    if (exportInProgress) {
      log("Экспорт уже выполняется.");
      return;
    }
    exportInProgress = true;
    byId("exportAndTranscribe").disabled = true;
    byId("guideOutputPath").value = outputPath;
    log("Передаю экспорт звука в Adobe Media Encoder…");
    callHost("exportAudio", {
      outputPath: outputPath,
      presetPath: presetPath,
      expectedSequenceId: activeSource.sequenceId,
      overwrite: byId("overwriteGuide").checked
    }, function (error, response) {
      if (error) {
        exportInProgress = false;
        byId("exportAndTranscribe").disabled = whisperJobIsActive();
        log("Экспорт не запущен: " + error.message);
        return;
      }
      guideAudioPath = response.job.outputPath;
      log("Экспорт поставлен в очередь. Ожидаю стабильный WAV…", response.job);
      pollExport(response.job.outputPath, response.job.jobId);
    });
  }

  function resolveSequence() {
    if (!activeSource) {
      return;
    }
    callHost("resolve", {
      sequenceId: activeSource.sequenceId,
      projectItemNodeId: activeSource.projectItemNodeId,
      name: activeSource.name
    }, function (error, response) {
      if (error) {
        log("Ссылка не разрешена: " + error.message);
        return;
      }
      log("Ссылка на sequence ID действительна.", response.sequence);
    });
  }

  function pathFilename(value) {
    var parts = String(value || "").split(/[\\/]/);
    return parts.length ? parts[parts.length - 1] : "";
  }

  function setConformStatus(state, message) {
    var status = byId("conformStatus");
    status.className = "operation-status operation-status-" + state;
    status.textContent = message;
  }

  function setConformBusy(busy) {
    byId("conformAll").disabled = busy;
    byId("conformAll").textContent = busy
      ? "Применение…"
      : "Применить найденные СНХ";
  }

  function loadFreshTimeline(project, callback) {
    request("POST", "/stage2/build", {
      project: project,
      fps: 25
    }, function (buildError, buildResponse) {
      if (buildError) {
        callback(buildError, buildResponse);
        return;
      }
      request(
        "GET",
        "/stage2/timeline?project=" + encodeURIComponent(project),
        null,
        callback
      );
    });
  }

  function conformAll() {
    var current = config();
    if (!current.project) {
      log("Укажите проект.");
      setConformStatus("error", "Сначала выберите проект в настройках.");
      return;
    }
    if (!current.token) {
      log("Введите ключ доступа.");
      setConformStatus("error", "Введите ключ доступа в настройках проекта.");
      return;
    }
    if (currentWhisperJobStatus === "queued" || currentWhisperJobStatus === "running" ||
        currentWhisperJobStatus === "cancelling") {
      log("Conform не запущен: дождитесь завершения сопоставления СНХ.");
      setConformStatus("error", "Сопоставление ещё выполняется. После завершения обновите статус.");
      return;
    }
    if (currentWhisperJobStatus === "interrupted" || currentWhisperJobStatus === "cancelled") {
      log("Conform не запущен: сопоставление было прервано. Запустите его снова — сохранённые подтверждения останутся.");
      setConformStatus("error", "Сначала возобновите сопоставление СНХ в браузере.");
      return;
    }
    setConformBusy(true);
    setConformStatus("working", "Проверяю активную монтажную секвенцию…");
    log("Проверяю активную целевую секвенцию…");
    callHost("inspect", undefined, function (inspectError, inspected) {
      var target;
      if (inspectError) {
        log("Целевая секвенция не считана: " + inspectError.message);
        setConformBusy(false);
        setConformStatus("error", "Не удалось считать активную секвенцию.");
        return;
      }
      target = inspected.sequence;
      request(
        "GET",
        "/stage2/sync-sources?project=" + encodeURIComponent(current.project),
        null,
        function (sourcesError, sourcesResponse) {
      var registeredSources;
      var sourceGuards;
      var conflictingSource;
      if (sourcesError) {
        log("Conform не запущен: не удалось проверить зарегистрированные sync-источники: " + sourcesError.message);
        setConformBusy(false);
        setConformStatus("error", "Не удалось безопасно проверить целевую секвенцию.");
        return;
      }
      registeredSources = (sourcesResponse.result && sourcesResponse.result.sources) ||
        sourcesResponse.sources;
      if (!Array.isArray(registeredSources)) {
        log("Conform не запущен: сервер не вернул список зарегистрированных sync-источников.");
        setConformBusy(false);
        setConformStatus("error", "Не удалось безопасно проверить целевую секвенцию.");
        return;
      }
      conflictingSource = registeredSources.filter(function (source) {
        return sourceIdentityMatchesSequence(source, target);
      })[0] || null;
      if (conflictingSource) {
        log(
          "Conform не запущен: активная секвенция зарегистрирована как sync-источник [" +
          String(conflictingSource.id || conflictingSource.source_id ||
            conflictingSource.sequence_id || "без ID") +
          "]. Откройте отдельную монтажную секвенцию."
        );
        setConformBusy(false);
        setConformStatus("error", "Нельзя применять СНХ в зарегистрированную секвенцию-источник.");
        return;
      }
      sourceGuards = registeredSources.map(registeredSourceGuard);
      if (!window.confirm(
        "Применить найденные СНХ к секвенции «" + target.name + "»?\n\n" +
        (currentWhisperReviewCount > 0
          ? currentWhisperReviewCount + " СНХ требуют подтверждения в браузере: " +
            "проект → «Сборка» → Sync → «Требуют проверки». " +
            "Сейчас они будут пропущены.\n\n"
          : "") +
        "Перед продолжением рекомендуется создать её копию."
      )) {
        setConformBusy(false);
        setConformStatus("idle", "Операция отменена. Секвенция не изменена.");
        return;
      }
      setConformStatus("working", "Собираю актуальный timeline…");
      log("Пересобираю timeline и загружаю найденные диапазоны…");
      loadFreshTimeline(current.project, function (error, response) {
      var timeline;
      var segments;
      var items;
      if (error) {
        log("Timeline не загружен: " + error.message, response);
        setConformBusy(false);
        setConformStatus("error", "Не удалось загрузить монтажный таймлайн.");
        return;
      }
      timeline = response.result || response;
      segments = timeline.segments || [];
      var reviewCount = segments.filter(function (segment) {
        return segment.match_status === "needs_review";
      }).length;
      if (reviewCount > 0) {
        log(
          reviewCount + " СНХ требуют подтверждения в браузере: " +
          "проект → «Сборка» → Sync → «Требуют проверки». " +
          "До подтверждения они не будут применены в Premiere."
        );
      }
      items = segments.filter(function (segment) {
        var typeOk = segment.type === "snh" || segment.type === "host";
        var statusOk = segment.match_status === "auto_matched" || segment.match_status === "confirmed";
        var sourceOk = segment.sequence_id || segment.sequence_name;
        var rangeOk = Number(segment.source_out_sec) > Number(segment.source_in_sec);
        return typeOk && statusOk && sourceOk && rangeOk && isFinite(Number(segment.start_sec));
      }).map(function (segment) {
        var placeholderIdentity = segment.block_uid
          ? "RC_BLOCK " + String(segment.block_uid)
          : "";
        return {
          segmentId: segment.id || segment.block_uid,
          blockUid: segment.block_uid || "",
          sourceSequenceId: segment.sequence_id || "",
          sourceSequenceName: segment.sequence_name || "",
          sourceInSeconds: Number(segment.source_in_sec),
          sourceOutSeconds: Number(segment.source_out_sec),
          timelineSeconds: Number(segment.start_sec),
          placeholderIdentity: placeholderIdentity,
          placeholderIdentityRequired: true,
          placeholderName: pathFilename(segment.placeholder_path)
            || String(segment.id || "") + "_placeholder.png",
          placeholderNames: [
            pathFilename(segment.placeholder_path)
              || String(segment.id || "") + "_placeholder.png",
            segment.speaker && segment.timecode
              ? String(segment.speaker) + " [" + String(segment.timecode) + "]"
              : ""
          ],
          text: segment.text || "",
          speaker: segment.speaker || "",
          confidence: Number(segment.match_confidence || 0)
        };
      }).sort(function (left, right) {
        return left.timelineSeconds - right.timelineSeconds;
      });
      if (!items.length) {
        log("В timeline нет найденных СНХ с валидными sequence/range.");
        setConformBusy(false);
        setConformStatus("idle", "Найденные СНХ для применения отсутствуют.");
        return;
      }
      setConformStatus("working", "Применяю СНХ: " + items.length + "…");
      log("Начинаю применение " + items.length + " СНХ. Активная секвенция будет изменена…");
      var conformPayload = {
        items: items,
        videoTrackIndex: CONFORM_VIDEO_TRACK_INDEX,
        audioTrackIndex: CONFORM_AUDIO_TRACK_INDEX,
        expectedTargetSequenceId: target.sequenceId,
        expectedTargetSequenceName: target.name,
        expectedProjectPath: target.projectPath,
        expectedTimelineDuration: Number(timeline.total_duration_sec),
        registeredSourceGuards: sourceGuards,
        enableExplicitTrackShiftBatch: true,
        repairStaleInsertions: false
      };
      function submitConform() {
        callHost("conform", conformPayload, function (hostError, hostResponse) {
        var report;
        var insertedCount;
        var existingCount;
        if (hostError) {
          if (hostError.message.indexOf("RC_STALE_INSERTION_CONFIRM_REQUIRED: ") === 0 &&
              !conformPayload.repairStaleInsertions) {
            var repairDetails = hostError.message.replace(
              "RC_STALE_INSERTION_CONFIRM_REQUIRED: ",
              ""
            );
            if (window.confirm(
              "В монтажной секвенции найдена старая вставка без актуального RC_SYNC-маркера:\n\n" +
              repairDetails +
              "\n\nЗаменить старую V/A-пару новым диапазоном и сдвинуть последующие клипы на разницу длительности?"
            )) {
              conformPayload.repairStaleInsertions = true;
              setConformStatus("working", "Заменяю устаревшие вставки и применяю СНХ…");
              log("Подтверждена замена устаревшей V/A-вставки: " + repairDetails);
              submitConform();
            } else {
              setConformBusy(false);
              setConformStatus("idle", "Замена устаревшей вставки отменена. Секвенция не изменена.");
            }
            return;
          }
          var changedTimeline = /^Inserted\s+/i.test(hostError.message);
          log(
            (changedTimeline
              ? "Conform прерван после частичного изменения: "
              : "Conform не запущен: ") + hostError.message,
            hostResponse
          );
          setConformBusy(false);
          setConformStatus(
            "error",
            changedTimeline
              ? "Клип уже вставлен, но операция прервалась. Не удаляйте его: повторный запуск проверит V/A и восстановит маркер."
              : "Не удалось применить СНХ: " + hostError.message
          );
          return;
        }
        report = hostResponse.report;
        insertedCount = (report.inserted || []).length;
        existingCount = (report.alreadyConformed || []).length;
        setConformBusy(false);
        setConformStatus(
          report.stopped ? "error" : "success",
          report.stopped
            ? "Операция остановлена: добавлено " + insertedCount +
              ", уже было " + existingCount + " из " + report.requested + "."
            : "Готово: добавлено " + insertedCount +
              "; уже было " + existingCount + "."
        );
        log(
          "Conform: добавлено " + insertedCount +
          ", уже было " + existingCount +
          " из " + report.requested +
          (report.stopped ? ", остановлен на ошибке." : ", завершён."),
          report
        );
      });
      }
      submitConform();
    });
    });
    });
  }

  function bind() {
    byId("saveConfig").addEventListener("click", saveConfig);
    byId("refreshProjects").addEventListener("click", loadProjects);
    byId("refreshWhisper").addEventListener("click", refreshWhisperStatusManually);
    byId("resumeWhisper").addEventListener("click", resumeTranscriptionJob);
    byId("cancelWhisper").addEventListener("click", cancelTranscriptionJob);
    byId("apiToken").addEventListener("change", function () {
      loadProjects();
      refreshWhisperStatus();
    });
    byId("projectsRoot").addEventListener("change", loadProjects);
    byId("projectName").addEventListener("change", function () {
      window.localStorage.setItem("roughCutStage2.project", byId("projectName").value);
      currentWhisperJobId = "";
      guideAudioPath = "";
      byId("guideOutputPath").value = defaultGuideOutput();
      refreshWhisperStatus();
      if (activeSource) {
        hydrateSourceForm(activeSource);
      }
    });
    byId("inspectActive").addEventListener("click", inspectActive);
    byId("registerSequence").addEventListener("click", registerSequence);
    byId("resolveSequence").addEventListener("click", resolveSequence);
    byId("exportAndTranscribe").addEventListener("click", exportAndTranscribe);
    byId("conformAll").addEventListener("click", conformAll);
    byId("clearLog").addEventListener("click", function () {
      byId("log").textContent = "";
    });
  }

  function init() {
    loadConfig();
    bind();
    renderWhisperIdle("Введите ключ доступа, чтобы видеть прогресс.");
    loadProjects();
    callHost("ping", undefined, function (error, response) {
      if (error) {
        setHostBadge("error", "Premiere: ошибка");
        log("Модуль Premiere недоступен: " + error.message);
        return;
      }
      setHostBadge("ok", response.host || "Premiere");
      byId("log").textContent = "";
      log("Панель готова.", response);
    });
  }

  if (window.__ROUGH_CUT_TEST_MODE__) {
    window.__ROUGH_CUT_TEST_HOOKS__ = {
      etaFromProgress: etaFromProgress,
      formatDuration: formatDuration,
      refreshWhisperStatus: refreshWhisperStatus,
      refreshWhisperStatusManually: refreshWhisperStatusManually,
      loadFreshTimeline: loadFreshTimeline,
      renderWhisperJob: renderWhisperJob,
      sourceRegistrationBlockReason: sourceRegistrationBlockReason,
      sourceIdentityMatchesSequence: sourceIdentityMatchesSequence,
      registeredSourceGuard: registeredSourceGuard
    };
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
