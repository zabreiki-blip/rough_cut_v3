(function () {
  "use strict";

  var cs = new window.CSInterface();
  var activeSource = null;
  var guideAudioPath = "";
  var exportPollTimer = null;
  var exportInProgress = false;
  var exportPollResumeFn = null;
  var whisperPollWasActive = false;
  var exportPollWasActive = false;
  var whisperPollTimer = null;
  // Bumped every time the panel is hidden so in-flight requests started
  // before the hide can recognise themselves as stale once their callback
  // finally arrives (see pauseBackgroundPolling/resumeBackgroundPolling).
  var pollGeneration = 0;
  // Counts how many times pollExport's terminal (stableChecks >= 3) branch
  // has actually run for the current panel session. Only ever read by
  // tests, to prove the visibilitychange race cannot double-fire
  // registerSequenceWithCallback/startTranscriptionJob.
  var exportPollTerminalCallCount = 0;
  var currentWhisperJobId = "";
  var currentWhisperReviewCount = 0;
  var currentWhisperJobStatus = "";
  var finalReview = {
    timer: null,
    retryIndex: 0,
    generation: 0,
    visible: !document.hidden,
    resume: false,
    job: null,
    sourceSequence: null,
    project: "",
    applyRequestId: "",
    applyBusy: false,
    inFlight: false,
    retryAction: "status",
    exportResume: null,
    layoutSequence: null,
    startUncertain: false,
    recoveredProject: "",
    lastKnownJobId: "",
    startBaselineJobId: ""
  };
  var FINAL_REVIEW_RETRY_DELAYS_MS = [3000, 6000, 12000, 24000, 30000];
  var CONFORM_VIDEO_TRACK_INDEX = 0;
  var CONFORM_AUDIO_TRACK_INDEX = 0;
  // CEP panels can remain open for an entire editing session. Keep their
  // diagnostic DOM bounded even when a backend is unavailable or a job polls
  // for a long time.
  var MAX_LOG_ENTRY_CHARACTERS = 8000;
  var MAX_LOG_CHARACTERS = 100000;
  var hostFunctions = {
    ping: "rcStage2_ping",
    projects: "rcStage2_listProjects",
    inspect: "rcStage2_inspectActiveSequence",
    inspectLayout: "rcStage2_captureAssemblyLayout",
    resolve: "rcStage2_resolveSequence",
    conform: "rcStage2_conformSequenceRanges",
    exportAudio: "rcStage2_exportActiveSequenceAudio",
    exportFinalReview: "rcStage2_exportFinalReviewAudio",
    fileInfo: "rcStage2_fileInfo",
    clone: "rcStage2_cloneActiveSequence"
  };

  function byId(id) {
    return document.getElementById(id);
  }

  function now() {
    var date = new Date();
    return date.toLocaleTimeString ? date.toLocaleTimeString() : String(date);
  }

  // `level` ("error" | "success" | "info") is the source of truth for the
  // notice styling when the caller knows it (e.g. derived from an HTTP
  // status or an explicit success/error branch). The Russian-word regex
  // below is only a last-resort fallback for messages logged without a
  // known level.
  function log(message, data, level) {
    var target = byId("log");
    var notice = byId("panelNotice");
    var line = "[" + now() + "] " + message;
    var retained;
    var cutoff;
    if (data !== undefined) {
      try {
        line += "\n" + JSON.stringify(data, null, 2);
      } catch (error) {
        line += "\n" + String(data);
      }
    }
    if (line.length > MAX_LOG_ENTRY_CHARACTERS) {
      line = line.slice(0, MAX_LOG_ENTRY_CHARACTERS) + "\n… запись журнала сокращена";
    }
    retained = target.textContent ? line + "\n\n" + target.textContent : line;
    if (retained.length > MAX_LOG_CHARACTERS) {
      // Do not leave a partial older record: retain complete, newest entries.
      cutoff = retained.lastIndexOf("\n\n", MAX_LOG_CHARACTERS);
      retained = cutoff > 0 ? retained.slice(0, cutoff) : retained.slice(0, MAX_LOG_CHARACTERS);
    }
    target.textContent = retained;
    if (notice) {
      notice.className = "panel-notice";
      if (level === "error") {
        notice.className += " panel-notice-error";
      } else if (level === "success" || level === "ok") {
        notice.className += " panel-notice-success";
      } else if (!level && /(ошиб|недоступ|не удалось|не выполн|не запущ|не загруж|не найден|не разреш|нельзя|отклон|остановлен|проверьте|укажите|введите|отмен)/i.test(message)) {
        notice.className += " panel-notice-error";
      } else if (!level && /(готов|заверш|зарегистрирован|сохран|обновл|получен|выбран|поставлен)/i.test(message)) {
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
      projectsRoot: String(byId("projectsRoot").value || "").replace(/[/\\]+$/, ""),
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
        log("Список проектов не загружен: " + hostError.message, undefined, "error");
        return;
      }
      request("GET", "/projects", null, function (error, response) {
        refresh.disabled = false;
        if (error) {
          log("Список проектов не загружен: " + error.message, undefined, "error");
          return;
        }
        replaceProjectOptions(response.projects || [], selected);
        restoreFinalReviewAfterProjectLoad();
        log(
          "Список проектов получен из backend: " + (response.projects || []).length + ".",
          undefined,
          "success"
        );
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
      restoreFinalReviewAfterProjectLoad();
      log(
        "Список проектов обновлён: " + (response.projects || []).length + ".",
        undefined,
        "success"
      );
    });
  }

  function configComplete() {
    var current = config();
    return Boolean(current.project && current.apiBaseUrl && current.token && current.projectsRoot);
  }

  // The token is memory-only (never persisted), so this re-opens the
  // settings card at the start of every session until the user fills it
  // in again, and folds it away once the required fields are complete.
  function syncConfigCardOpenState() {
    var card = byId("configCard");
    if (!card) {
      return;
    }
    if (configComplete()) {
      card.removeAttribute("open");
    } else {
      card.setAttribute("open", "");
    }
  }

  function checkConnection() {
    log("Проверяю соединение с backend…", undefined, "info");
    loadProjects();
    refreshWhisperStatus();
  }

  function saveConfig() {
    var current = config();
    window.localStorage.setItem("roughCutStage2.project", current.project);
    window.localStorage.setItem("roughCutStage2.apiBaseUrl", current.apiBaseUrl);
    window.localStorage.setItem("roughCutStage2.projectsRoot", current.projectsRoot);
    window.localStorage.setItem("roughCutStage2.transcribePath", current.transcribePath);
    // The token intentionally remains memory-only.
    byId("apiBaseUrl").value = current.apiBaseUrl;
    log("Настройки сохранены. API token не записан на диск.", undefined, "success");
    syncConfigCardOpenState();
    hideManualRetry();
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

  // Auto-retry delays for idempotent (GET) requests: ~1s, then ~3s — 3
  // attempts in total. State-changing requests (POST) are never retried
  // automatically; see offerManualRetry below.
  var RETRY_DELAYS_MS = [1000, 3000];
  var pendingManualRetry = null;

  function authHint() {
    return " Проверьте ключ доступа: он должен совпадать с переменной окружения " +
      "ROUGH_CUT_API_TOKEN на backend.";
  }

  function serverErrorHint() {
    return " Подробности смотрите в «Техническом журнале» ниже и в логах backend.";
  }

  function offlineHint() {
    return " Запустите scripts/run-local и проверьте адрес сервера в «Дополнительных настройках».";
  }

  function showOfflineNotice(show) {
    var el = byId("offlineNotice");
    if (el) {
      el.hidden = !show;
    }
  }

  function hideManualRetry() {
    pendingManualRetry = null;
    var button = byId("requestRetry");
    if (button) {
      button.hidden = true;
      button.textContent = "Повторить";
    }
  }

  // Non-idempotent (POST/PUT/DELETE) requests that failed due to a network
  // drop or timeout are ambiguous: the backend may have already applied the
  // change before the response was lost. Warn the user instead of
  // presenting a plain "Повторить" as if nothing happened.
  function manualRetryButtonText(pending) {
    if (pending.method === "GET") {
      return "Повторить";
    }
    if (pending.networkError) {
      return "Повторить (операция могла уже выполниться — проверьте таймлайн)";
    }
    return "Повторить";
  }

  function offerManualRetry(method, path, body, callback, error) {
    pendingManualRetry = {
      method: method,
      path: path,
      body: body,
      callback: callback,
      networkError: Boolean(error && error.network)
    };
    var button = byId("requestRetry");
    if (button) {
      button.hidden = false;
      button.textContent = manualRetryButtonText(pendingManualRetry);
    }
  }

  function retryPendingRequest() {
    if (!pendingManualRetry) {
      return;
    }
    var pending = pendingManualRetry;
    hideManualRetry();
    log("Повторяю запрос…", undefined, "info");
    request(pending.method, pending.path, pending.body, pending.callback);
  }

  function request(method, path, body, callback, retryState) {
    // retryState is shared (by reference) across every recursive retry of
    // the same logical operation, so the "finished" flag it carries
    // guarantees the external callback fires exactly once no matter how
    // many attempts are made or in what order the XHR events land.
    retryState = retryState || {attempt: 0, finished: false};
    if (retryState.isCurrent && !retryState.isCurrent()) return;
    var current = config();
    function finish(error, payload) {
      if (retryState.isCurrent && !retryState.isCurrent()) return;
      if (retryState.finished) {
        return;
      }
      retryState.finished = true;
      if (!error) {
        showOfflineNotice(false);
        hideManualRetry();
      }
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

    var isIdempotent = method === "GET";

    // Only network errors and timeouts are ever retried, and only for
    // idempotent (GET) requests — retrying a POST (conform/build/insert)
    // automatically could apply the same state change twice.
    function retryOrFail(error) {
      if (retryState.finished || (retryState.isCurrent && !retryState.isCurrent())) return;
      error.network = true;
      if (!retryState.managed && isIdempotent && retryState.attempt < RETRY_DELAYS_MS.length) {
        var delayMs = RETRY_DELAYS_MS[retryState.attempt];
        log(
          "Повтор через " + Math.round(delayMs / 1000) + " с… (" + error.message + ")",
          undefined,
          "info"
        );
        retryState.attempt += 1;
        window.setTimeout(function () {
          request(method, path, body, callback, retryState);
        }, delayMs);
        return;
      }
      showOfflineNotice(true);
      if (!retryState.managed && !isIdempotent) {
        offerManualRetry(method, path, body, callback, error);
      }
      finish(error);
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
      var message;
      if (retryState.isCurrent && !retryState.isCurrent()) return;
      if (xhr.readyState !== 4) {
        return;
      }
      if (xhr.status === 0) {
        // The connection was dropped/refused before any HTTP response was
        // received (backend down, DNS failure, aborted request, etc.).
        // readyState can reach 4 with status 0 *before* onerror/ontimeout
        // fires; treating that as a successful empty response would report
        // a false "success" for the outer callback. Ignore it here and let
        // onerror/ontimeout drive the retry/failure path exactly once.
        return;
      }
      try {
        parsed = xhr.responseText ? JSON.parse(xhr.responseText) : {};
      } catch (error) {
        message = "Backend вернул не JSON (HTTP " + xhr.status + ").";
        if (xhr.status >= 500) {
          message += serverErrorHint();
        }
        var parseError = new Error(message);
        parseError.status = xhr.status;
        parseError.invalidJson = true;
        finish(parseError);
        return;
      }
      // The HTTP status is the source of truth for success/error, not any
      // wording in the payload. Only the 2xx range counts as success; the
      // branches below only add actionable hints for specific status codes.
      if (xhr.status < 200 || xhr.status >= 300) {
        message = parsed.error || parsed.message || ("HTTP " + xhr.status);
        if (xhr.status === 401 || xhr.status === 403) {
          message += authHint();
        } else if (xhr.status >= 500) {
          message += serverErrorHint();
        }
        var httpError = new Error(message);
        httpError.status = xhr.status;
        finish(httpError, parsed);
        return;
      }
      if (parsed.ok === false) {
        finish(new Error(parsed.error || parsed.message || "Backend отклонил запрос."), parsed);
        return;
      }
      finish(null, parsed);
    };
    xhr.onerror = function () {
      retryOrFail(new Error("Backend недоступен или соединение было прервано." + offlineHint()));
    };
    xhr.ontimeout = function () {
      retryOrFail(new Error("Backend не ответил за 30 секунд." + offlineHint()));
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
    byId("whisperUnmatched").textContent = Number(job.unmatched || 0) + Number(job.no_source || 0);
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
        log(
          "Статус распознавания не обновлён: проверьте проект, адрес сервера и ключ доступа.",
          undefined,
          "error"
        );
      }
      return;
    }
    path = requestedJobId
      ? "/stage2/sync-jobs/" + encodeURIComponent(requestedJobId)
      : "/stage2/sync-jobs/status?project=" + encodeURIComponent(current.project);
    var requestGeneration = pollGeneration;
    request("GET", path, null, function (error, response) {
      var job;
      // The panel was hidden (or hidden/shown again) while this request was
      // in flight. Drop the response instead of re-arming a timer: resume
      // already scheduled (or will schedule) a fresh poll from the current
      // state, so acting on stale data here would risk a duplicate cycle.
      if (requestGeneration !== pollGeneration || document.hidden) {
        whisperPollWasActive = true;
        return;
      }
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
          log("Статус распознавания не обновлён: " + error.message, undefined, "error");
        }
        scheduleWhisperPoll(10000);
        return;
      }
      job = response.result || response;
      currentWhisperJobId = job.id || currentWhisperJobId;
      renderWhisperJob(job);
      if (manual) {
        log("Статус распознавания обновлён.", job, "success");
      }
      scheduleWhisperPoll(
        job.status === "queued" || job.status === "running" || job.status === "cancelling" ? 2500 : 10000
      );
    });
  }

  // CEP panels stay open for an entire editing shift. Polling the backend
  // while the panel is hidden/minimized just burns requests for nobody to
  // see, so pause client-side polling on visibilitychange and pick it back
  // up (with an immediate refresh) once the panel is visible again. This
  // never cancels the underlying server-side job — only the local timers
  // that ask about its progress.
  function pauseBackgroundPolling() {
    // Requests already in flight (callHost/XHR round-trips) cannot be
    // cancelled, so bump the generation counter to let their callbacks
    // detect the pause once they finally resolve.
    pollGeneration += 1;
    finalReview.resume = Boolean(finalReview.job || finalReview.inFlight || finalReview.exportResume ||
      finalReview.layoutSequence || finalReview.startUncertain);
    invalidateFinalReview();
    finalReview.visible = false;
    if (whisperPollTimer) {
      window.clearTimeout(whisperPollTimer);
      whisperPollTimer = null;
      whisperPollWasActive = true;
    }
    if (exportPollTimer) {
      window.clearTimeout(exportPollTimer);
      exportPollTimer = null;
      exportPollWasActive = true;
    }
  }

  function resumeBackgroundPolling() {
    finalReview.visible = true;
    if (finalReview.resume) {
      finalReview.resume = false;
      if (finalReview.layoutSequence) {
        captureFinalReviewLayout(finalReview.layoutSequence);
      } else if (finalReview.exportResume) {
        finalReview.exportResume();
      } else if (finalReview.job) {
        pollFinalReviewJob();
      } else {
        restoreFinalReview();
      }
    }
    if (whisperPollWasActive) {
      whisperPollWasActive = false;
      refreshWhisperStatus();
    }
    if (exportPollWasActive) {
      exportPollWasActive = false;
      if (exportPollResumeFn) {
        exportPollResumeFn();
      }
    }
  }

  function handleVisibilityChange() {
    if (document.hidden) {
      pauseBackgroundPolling();
    } else {
      resumeBackgroundPolling();
    }
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
      // eslint-disable-next-line no-control-regex -- \x00-\x7F range intentionally detects non-ASCII source names
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
          log("Не удалось проверить регистрацию sync-источника: " + error.message, undefined, "error");
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
        log("Найдена существующая регистрация sync-источника.", existing, "success");
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
        log("Не удалось считать секвенцию: " + error.message, undefined, "error");
        return;
      }
      renderSource(response.sequence);
      var blocked = sourceRegistrationBlockReason(response.sequence);
      if (blocked) {
        log(blocked, response.sequence, "error");
      } else {
        log("Активная секвенция сохранена как источник.", response.sequence, "success");
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

  function captureSourceOperation(guidePath) {
    return {
      current: config(),
      source: activeSource,
      sourceId: byId("sourceId").value.trim(),
      speakerNames: byId("speakerName").value.split(/[,;\n]+/).map(function (value) {
        return value.trim();
      }).filter(function (value) {
        return Boolean(value);
      }),
      allSpeakers: byId("allSpeakers").checked,
      guideAudioPath: guidePath == null ? guideAudioPath : guidePath
    };
  }

  function registerSequenceWithCallback(callback, operation) {
    var operationContext = operation || captureSourceOperation();
    var current = operationContext.current;
    var source = operationContext.source;
    var sourceId = operationContext.sourceId;
    var timebase = Number(source && source.timebaseTicks);
    var fps = isFinite(timebase) && timebase > 0 ? 254016000000 / timebase : 25;
    var done = callback || function () {};
    if (!source) {
      log("Сначала считайте активную секвенцию.");
      done(new Error("No active source."));
      return;
    }
    var roleBlock = sourceRegistrationBlockReason(source);
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

    var speakerNames = operationContext.speakerNames;
    var payload = {
      project: current.project,
      id: sourceId,
      source_id: sourceId,
      speaker: speakerNames[0] || "",
      speakers: speakerNames.slice(1),
      all_speakers: operationContext.allSpeakers,
      premiere_sequence: source.name,
      sequence_id: source.sequenceId,
      sequence_name: source.name,
      sequence_start_tc: secondsToTimecode(source.zeroPointSeconds, fps),
      project_path: source.projectPath,
      fps: fps,
      duration_sec: source.durationSeconds,
      guide_audio: operationContext.guideAudioPath ? guideReference(operationContext.guideAudioPath, current) : "",
      guide_offset_sec: 0,
      metadata: {
        sequence_role: source.roughCutGuard.isRoughCutTarget
          ? "rough_cut_target"
          : "sync_source",
        rough_cut_target: Boolean(source.roughCutGuard.isRoughCutTarget),
        guide_audio_absolute: operationContext.guideAudioPath,
        project_item_node_id: source.projectItemNodeId,
        project_item_tree_path: source.projectItemTreePath,
        zero_point_sec: source.zeroPointSeconds,
        zero_point_ticks: source.zeroPointTicks,
        timebase_ticks: source.timebaseTicks,
        video_display_format: source.videoDisplayFormat,
        video_track_count: source.videoTrackCount,
        audio_track_count: source.audioTrackCount
      }
    };

    log("Регистрирую sync-источник…");
    request("POST", "/stage2/sync-sources", payload, function (error, response) {
      if (error) {
        log("Регистрация не выполнена: " + error.message, response, "error");
        done(error);
        return;
      }
      log("Sync-источник зарегистрирован.", response, "success");
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

  function startTranscriptionJob(operation) {
    var operationContext = operation || captureSourceOperation();
    var current = operationContext.current;
    var sourceId = operationContext.sourceId;
    var path = current.transcribePath || "/stage2/sync-jobs/start";
    request("POST", path, {
      project: current.project,
      source_ids: [sourceId]
    }, function (error, response) {
      if (error) {
        log(
          "WAV готов и зарегистрирован, но Whisper job не запущен: " + error.message,
          response,
          "error"
        );
        return;
      }
      log("Whisper job запущен.", response, "success");
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
          log("Остановка не выполнена: " + error.message, response, "error");
          return;
        }
        log(
          "Остановка запрошена. Индексы и найденные диапазоны сохраняются.",
          response,
          "success"
        );
        renderWhisperJob(response.result || response);
        scheduleWhisperPoll(500);
      }
    );
  }

  function pollExport(outputPath, jobId, operation) {
    operation = operation || captureSourceOperation(outputPath);
    var previousSize = -1;
    var previousModified = -1;
    var stableChecks = 0;
    var attempts = 0;
    var maxAttempts = 900;
    // Guards the terminal branch below so a stale in-flight response can
    // never trigger registerSequenceWithCallback/startTranscriptionJob a
    // second time for this export cycle.
    var exportFinished = false;

    function check() {
      attempts += 1;
      var requestGeneration = pollGeneration;
      callHost("fileInfo", { path: outputPath }, function (error, response) {
        var file;
        // Same discard-stale-response rule as refreshWhisperStatus: never
        // re-arm a timer (or run the terminal branch) for a request that
        // started before the panel was hidden.
        if (requestGeneration !== pollGeneration || document.hidden) {
          exportPollWasActive = true;
          return;
        }
        if (exportFinished) {
          return;
        }
        if (error) {
          log("Не удалось проверить guide WAV: " + error.message);
          if (attempts < maxAttempts) {
            exportPollTimer = window.setTimeout(check, 2000);
          } else {
            exportPollTimer = null;
            exportPollResumeFn = null;
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
          exportFinished = true;
          exportPollTimer = null;
          exportPollResumeFn = null;
          exportInProgress = false;
          operation.guideAudioPath = file.path || outputPath;
          guideAudioPath = operation.guideAudioPath;
          log("Guide WAV готов: " + file.size + " байт.", file);
          exportPollTerminalCallCount += 1;
          registerSequenceWithCallback(function (registerError) {
            if (!registerError) {
              startTranscriptionJob(operation);
            }
          }, operation);
          return;
        }
        if (attempts >= maxAttempts) {
          exportPollTimer = null;
          exportPollResumeFn = null;
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
    exportPollResumeFn = check;
    exportPollWasActive = false;
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
    // Export may take minutes. Freeze source and project fields before the
    // asynchronous host request; another inspected sequence must not inherit
    // this WAV when the export completes.
    var operation = captureSourceOperation(outputPath);
    exportInProgress = true;
    byId("exportAndTranscribe").disabled = true;
    byId("guideOutputPath").value = outputPath;
    log("Передаю экспорт звука в Adobe Media Encoder…");
    callHost("exportAudio", {
      outputPath: outputPath,
      presetPath: presetPath,
      expectedSequenceId: operation.source.sequenceId,
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
      pollExport(response.job.outputPath, response.job.jobId, operation);
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

  function finalReviewOutputPath() {
    var current = config();
    return current.projectsRoot && current.project
      ? current.projectsRoot + "/" + current.project + "/work/final_review.wav"
      : "";
  }

  function setFinalReviewStatus(message) {
    byId("finalReviewStatus").textContent = message;
  }

  function finalReviewCurrent(generation) {
    return generation === finalReview.generation && finalReview.visible &&
      !document.hidden && finalReview.project === config().project;
  }

  function clearFinalReviewTimer() {
    if (finalReview.timer !== null) window.clearTimeout(finalReview.timer);
    finalReview.timer = null;
  }

  function invalidateFinalReview() {
    clearFinalReviewTimer();
    finalReview.generation += 1;
    finalReview.inFlight = false;
    finalReview.applyBusy = false;
    byId("applyFinalReview").disabled = true;
  }

  // Managed requests use this controller's retry policy. In particular, a
  // generic request retry must not escape the generation/visibility guard.
  function finalReviewRequest(method, path, body, generation, callback) {
    request(method, path, body, callback, {
      attempt: 0,
      finished: false,
      managed: true,
      isCurrent: function () { return finalReviewCurrent(generation); }
    });
  }

  function finalReviewStartButtons(disabled) {
    disabled = disabled || !config().project || finalReview.recoveredProject !== config().project;
    byId("startFinalReview").disabled = disabled;
    byId("startFinalReviewFromWav").disabled = disabled;
  }

  function finalReviewTerminal(status) {
    return status === "completed" || status === "failed" ||
      status === "interrupted" || status === "cancelled" || status === "timed_out";
  }

  function finalReviewStatusLabel(status) {
    var labels = {
      queued: "в очереди",
      running: "проверка выполняется",
      completed: "проверка завершена",
      failed: "ошибка проверки",
      interrupted: "проверка прервана",
      cancelling: "отмена проверки…",
      cancelled: "проверка отменена",
      timed_out: "время проверки истекло"
    };
    return labels[status] || status || "проверка выполняется";
  }

  function scheduleFinalReview(delay) {
    clearFinalReviewTimer();
    var generation = finalReview.generation;
    if (!finalReviewCurrent(generation)) return;
    finalReview.timer = window.setTimeout(function () {
      if (!finalReviewCurrent(generation)) return;
      finalReview.timer = null;
      if (finalReview.job) pollFinalReviewJob();
      else restoreFinalReview();
    }, delay);
  }

  function finalReviewFailure(error) {
    finalReview.inFlight = false;
    byId("applyFinalReview").disabled = true;
    setFinalReviewStatus("Не удалось получить статус проверки: " + error.message);
    // Authentication, validation, and missing-job errors need user action.
    var transient = error.network || error.status >= 500 || error.status === 429;
    if (transient && finalReview.retryIndex < FINAL_REVIEW_RETRY_DELAYS_MS.length) {
      var delay = FINAL_REVIEW_RETRY_DELAYS_MS[finalReview.retryIndex++];
      setFinalReviewStatus("Связь с проверкой потеряна. Повтор через " + (delay / 1000) + " с.");
      scheduleFinalReview(delay);
      return;
    }
    byId("retryFinalReview").hidden = false;
    finalReview.retryAction = "status";
  }

  function inspectFinalReviewSource(generation, callback) {
    byId("applyFinalReview").disabled = true;
    callHost("inspect", {}, function (error, response) {
      if (!finalReviewCurrent(generation)) return;
      var sequence = response && (response.sequence || response);
      var expected = finalReview.sourceSequence && finalReview.sourceSequence.sequenceId;
      if (error || !expected || !sequence || String(sequence.sequenceId) !== String(expected)) {
        setFinalReviewStatus(error ? "Не удалось проверить активную sequence: " + error.message :
          "Активная sequence не совпадает с сохранённой sequence «" + (expected || "неизвестна") + "». Откройте её и повторите запрос.");
        byId("retryFinalReview").hidden = false;
        finalReview.retryAction = "status";
        finalReview.applyBusy = false;
        return;
      }
      callback(sequence);
    });
  }

  function renderFinalReviewCost(job) {
    var confirmed = job.cost_usd;
    var reserved = job.reserved_cost_usd;
    var message = "Подтверждённая стоимость: " +
      (confirmed === null || confirmed === undefined ? "неизвестна" : "$" + confirmed) + ".";
    if (job.cost_pending || Number(reserved) > 0) {
      message += " Ожидает подтверждения провайдера: резерв " +
        (reserved === null || reserved === undefined ? "неизвестен" : "$" + reserved) +
        ". Итоговая стоимость пока неизвестна.";
    }
    var timedOut = job.status === "timed_out" || (job.status === "failed" &&
      /timeout|timed[\s_-]*out|deadline.*exceeded|exceeded.*deadline|transcription exceeded .* seconds/i.test(String(job.error || "")));
    if (job.status === "cancelled" || timedOut) {
      message += " Уже отправленные провайдеру запросы всё ещё могут быть оплачены.";
    }
    byId("finalReviewCost").textContent = message;
  }

  function renderFinalReviewJob(job) {
    if (!job || !job.id) {
      finalReviewFailure(new Error("Backend не вернул ID проверки."));
      return;
    }
    finalReview.recoveredProject = finalReview.project;
    if (finalReview.startUncertain && String(job.id) === finalReview.startBaselineJobId) {
      // latest may still refer to the old job while Start is accepting work.
      // Keep querying latest until a different ID proves the new job exists.
      clearFinalReviewTimer();
      finalReview.inFlight = false;
      finalReview.retryAction = "status";
      finalReviewStartButtons(true);
      byId("retryFinalReview").hidden = false;
      byId("cancelFinalReview").hidden = true;
      byId("applyFinalReview").disabled = true;
      setFinalReviewStatus("Новая проверка пока не подтверждена. Найдена предыдущая проверка; повторите запрос статуса.");
      return;
    }
    if (!finalReview.job || finalReview.job.id !== job.id) {
      finalReview.applyRequestId = "";
    }
    finalReview.job = job;
    finalReview.startUncertain = false;
    finalReview.startBaselineJobId = "";
    finalReview.lastKnownJobId = String(job.id);
    finalReview.sourceSequence = job.source_sequence || null;
    finalReview.retryIndex = 0;
    finalReview.inFlight = false;
    finalReview.retryAction = "status";
    finalReviewStartButtons(false);
    byId("retryFinalReview").hidden = true;
    byId("cancelFinalReview").hidden = finalReviewTerminal(job.status);
    byId("cancelFinalReview").disabled = job.status === "cancelling";
    byId("applyFinalReview").disabled = true;
    renderFinalReviewCost(job);
    setFinalReviewStatus(finalReviewStatusLabel(job.status) + (job.message ? ": " + job.message : "") +
      (job.error ? " — " + job.error : ""));
    if (job.status === "completed") {
      var generation = finalReview.generation;
      inspectFinalReviewSource(generation, function () {
        byId("applyFinalReview").disabled = false;
      });
    } else if (!finalReviewTerminal(job.status)) {
      scheduleFinalReview(3000);
    }
  }

  function restoreFinalReview() {
    var current = config();
    if (!current.project || !current.apiBaseUrl || !current.token) return;
    if (!finalReview.visible || document.hidden) {
      finalReview.resume = true;
      return;
    }
    if (finalReview.inFlight || finalReview.applyBusy) return;
    finalReview.project = current.project;
    if (finalReview.recoveredProject !== current.project) finalReviewStartButtons(true);
    clearFinalReviewTimer();
    finalReview.inFlight = true;
    var generation = finalReview.generation;
    finalReviewRequest("GET", "/stage2/final-review/latest?project=" + encodeURIComponent(current.project), null, generation, function (error, response) {
      if (!finalReviewCurrent(generation)) return;
      finalReview.inFlight = false;
      if (error && error.status === 404) {
        finalReview.recoveredProject = current.project;
        finalReviewStartButtons(finalReview.startUncertain);
        byId("retryFinalReview").hidden = !finalReview.startUncertain;
        byId("cancelFinalReview").hidden = true;
        setFinalReviewStatus(finalReview.startUncertain ?
          "Запуск пока не подтверждён. Проверка могла начаться; повторите запрос статуса." :
          "Для проекта пока нет финальной проверки.");
        return;
      }
      if (error) { finalReviewFailure(error); return; }
      renderFinalReviewJob(response.result || response);
    });
  }

  function restoreFinalReviewAfterProjectLoad() {
    if (finalReview.project !== config().project) {
      finalReviewProjectChanged();
    } else if (!finalReview.job && !finalReview.inFlight &&
               !finalReview.applyBusy && !finalReview.exportResume && !finalReview.layoutSequence) {
      restoreFinalReview();
    }
  }

  function finalReviewProjectChanged() {
    invalidateFinalReview();
    finalReview.job = null;
    finalReview.sourceSequence = null;
    finalReview.project = config().project;
    finalReview.recoveredProject = "";
    finalReview.applyRequestId = "";
    finalReview.startUncertain = false;
    finalReview.startBaselineJobId = "";
    finalReview.lastKnownJobId = "";
    finalReview.retryIndex = 0;
    finalReview.retryAction = "status";
    finalReview.exportResume = null;
    finalReview.layoutSequence = null;
    finalReview.resume = false;
    finalReviewStartButtons(false);
    byId("retryFinalReview").hidden = true;
    byId("cancelFinalReview").hidden = true;
    byId("finalReviewCost").textContent = "Перед запуском подтвердите оценку стоимости. Неизвестная стоимость — не лимит расходов.";
    setFinalReviewStatus("Восстанавливаю финальную проверку проекта…");
    restoreFinalReview();
  }

  function pollFinalReviewJob() {
    if (!finalReview.job || finalReview.inFlight || finalReview.applyBusy ||
        !finalReviewCurrent(finalReview.generation)) return;
    clearFinalReviewTimer();
    finalReview.inFlight = true;
    var generation = finalReview.generation;
    finalReviewRequest("GET", "/stage2/final-review/jobs/" + encodeURIComponent(finalReview.job.id) + "?project=" + encodeURIComponent(finalReview.project), null, generation, function (error, response) {
      if (!finalReviewCurrent(generation)) return;
      finalReview.inFlight = false;
      if (error) { finalReviewFailure(error); return; }
      renderFinalReviewJob(response.result || response);
    });
  }

  function retryFinalReview() {
    if (finalReview.inFlight || finalReview.applyBusy || !finalReviewCurrent(finalReview.generation)) return;
    byId("retryFinalReview").hidden = true;
    finalReview.retryIndex = 0;
    if (finalReview.retryAction === "apply") {
      beginFinalReviewApply(true);
    } else if (finalReview.job) {
      pollFinalReviewJob();
    } else {
      restoreFinalReview();
    }
  }

  function cancelFinalReview() {
    if (!finalReview.job || finalReviewTerminal(finalReview.job.status) ||
        !finalReviewCurrent(finalReview.generation)) return;
    invalidateFinalReview();
    finalReview.exportResume = null;
    finalReview.layoutSequence = null;
    finalReview.retryIndex = 0;
    finalReview.retryAction = "status";
    byId("retryFinalReview").hidden = true;
    byId("cancelFinalReview").disabled = true;
    setFinalReviewStatus(finalReviewStatusLabel("cancelling"));
    finalReview.inFlight = true;
    var generation = finalReview.generation;
    finalReviewRequest("POST", "/stage2/final-review/jobs/" + encodeURIComponent(finalReview.job.id) + "/cancel?project=" + encodeURIComponent(finalReview.project), {project: finalReview.project}, generation, function (error, response) {
      if (!finalReviewCurrent(generation)) return;
      finalReview.inFlight = false;
      if (error) {
        setFinalReviewStatus("Не удалось подтвердить отмену: " + error.message + ". Повторите запрос статуса.");
        byId("retryFinalReview").hidden = false;
        return;
      }
      renderFinalReviewJob(response.result || response);
    });
  }

  function submitFinalReview(sequence, layout, generation) {
    if (!finalReviewCurrent(generation)) return;
    var payload = {
      project: finalReview.project,
      audio_path: "work/final_review.wav",
      backend: "cloud-audio",
      model: "google/gemini-3.7-flash",
      source_sequence: sequence || {},
      assembly_layout: layout || {}
    };
    setFinalReviewStatus("Получаю оценку стоимости проверки…");
    finalReview.inFlight = true;
    finalReviewRequest("POST", "/stage2/final-review/estimate", payload, generation, function (error, response) {
      if (!finalReviewCurrent(generation)) return;
      finalReview.inFlight = false;
      if (error) {
        finalReviewStartButtons(false);
        setFinalReviewStatus("Не удалось оценить стоимость: " + error.message);
        return;
      }
      var estimate = response.result || response;
      // Never round through Number: backend confirmation compares exact decimals.
      var amount = estimate.estimated_cost_usd;
      var known = typeof amount === "string" && Number(amount) > 0;
      var message = "Оценка стоимости: " + (known ? "$" + amount : "неизвестна ($" + (amount || "0") + ")") + ". ";
      message += !estimate.enforced || !known ?
        "Это не лимит расходов; неизвестная стоимость не ограничивает списания." :
        "Лимит расходов backend: $" + estimate.cost_limit_usd + ".";
      byId("finalReviewCost").textContent = message;
      setFinalReviewStatus("Подтвердите стоимость перед запуском проверки.");
      if (!window.confirm(message + "\nЗапустить финальную проверку?")) {
        finalReviewStartButtons(false);
        setFinalReviewStatus("Запуск проверки отменён до отправки провайдеру.");
        return;
      }
      if (!finalReviewCurrent(generation)) return;
      payload.confirmed_estimate_usd = amount === undefined ? "0" : amount;
      finalReview.inFlight = true;
      finalReview.startUncertain = true;
      finalReview.startBaselineJobId = finalReview.lastKnownJobId;
      finalReviewRequest("POST", "/stage2/final-review/start", payload, generation, function (startError, started) {
        if (!finalReviewCurrent(generation)) return;
        finalReview.inFlight = false;
        if (startError) {
          finalReview.startUncertain = Boolean(startError.network || startError.invalidJson ||
            startError.status === 429 || startError.status >= 500);
          if (!finalReview.startUncertain) finalReview.startBaselineJobId = "";
          finalReviewStartButtons(finalReview.startUncertain);
          setFinalReviewStatus("Финальная проверка не запущена: " + startError.message);
          if (finalReview.startUncertain) {
            setFinalReviewStatus("Не удалось подтвердить запуск. Проверка могла начаться; повторите запрос статуса.");
            finalReview.retryAction = "status";
            byId("retryFinalReview").hidden = false;
          }
          // A changed estimate must be fetched and confirmed again. Other
          // conflicts (busy project, stale WAV, budget) need user action.
          if (startError.status === 409 && /estimate changed/i.test(startError.message)) {
            finalReviewStartButtons(true);
            submitFinalReview(sequence, layout, generation);
          }
          return;
        }
        var result = started.result || started;
        finalReview.startUncertain = false;
        finalReview.startBaselineJobId = "";
        finalReview.lastKnownJobId = String(result.job_id || "");
        finalReview.job = {id: result.job_id, status: "queued", source_sequence: sequence};
        finalReview.sourceSequence = sequence;
        finalReviewStartButtons(false);
        byId("cancelFinalReview").hidden = false;
        byId("cancelFinalReview").disabled = false;
        setFinalReviewStatus("Распознаю и сверяю собранный таймлайн…");
        pollFinalReviewJob();
      });
    });
  }

  function inspectFinalReviewLayout(sequence, callback) {
    var duration = Math.max(0.04, Number(sequence && sequence.durationSeconds || 0));
    callHost("inspectLayout", {
      fromSeconds: 0,
      toSeconds: duration
    }, function (error, response) {
      callback(error, response && (response.layout || response));
    });
  }

  function captureFinalReviewLayout(sequence) {
    var generation = finalReview.generation;
    if (!finalReviewCurrent(generation)) return;
    finalReview.layoutSequence = sequence;
    finalReview.inFlight = true;
    setFinalReviewStatus("Создаю manifest собранной sequence…");
    inspectFinalReviewLayout(sequence, function (error, layout) {
      if (!finalReviewCurrent(generation)) return;
      finalReview.layoutSequence = null;
      finalReview.inFlight = false;
      if (!error && layout && layout.sequence &&
          String(layout.sequence.sequenceId) !== String(sequence.sequenceId)) {
        error = new Error("Активная sequence не совпадает с сохранённой sequence «" + sequence.sequenceId + "».");
      }
      if (error) {
        finalReviewStartButtons(false);
        setFinalReviewStatus("Не удалось создать manifest sequence: " + error.message);
        return;
      }
      submitFinalReview(sequence, layout, generation);
    });
  }

  function pollFinalReviewExport(outputPath, jobId, sequence) {
    var previousSize = -1;
    var previousModified = -1;
    var stableChecks = 0;
    var attempts = 0;
    var exportFinished = false;
    function check() {
      var generation = finalReview.generation;
      if (!finalReviewCurrent(generation) || exportFinished) return;
      attempts += 1;
      finalReview.inFlight = true;
      callHost("fileInfo", {path: outputPath}, function (error, response) {
        if (!finalReviewCurrent(generation) || exportFinished) return;
        finalReview.inFlight = false;
        var file = response && response.file;
        if (!error && file && file.exists && file.size > 44 &&
            file.size === previousSize && file.modifiedMs === previousModified) {
          stableChecks += 1;
        } else {
          stableChecks = 0;
        }
        if (file) { previousSize = file.size; previousModified = file.modifiedMs; }
        if (stableChecks >= 3) {
          exportFinished = true;
          finalReview.exportResume = null;
          clearFinalReviewTimer();
          captureFinalReviewLayout(sequence);
          return;
        }
        if (attempts >= 1800) {
          finalReview.exportResume = null;
          finalReviewStartButtons(false);
          setFinalReviewStatus("Ожидание финального WAV остановлено через 60 минут. Job ID: " + jobId);
          log("Ожидание final-review экспорта остановлено через 60 минут.", response, "error");
          return;
        }
        scheduleCheck(2000);
      });
    }
    function scheduleCheck(delay) {
      clearFinalReviewTimer();
      var generation = finalReview.generation;
      if (!finalReviewCurrent(generation)) return;
      finalReview.timer = window.setTimeout(function () {
        if (!finalReviewCurrent(generation)) return;
        finalReview.timer = null;
        check();
      }, delay);
    }
    finalReview.exportResume = function () { scheduleCheck(0); };
    scheduleCheck(1000);
  }

  function beginFinalReview(fromWav) {
    if (!config().project || finalReview.recoveredProject !== config().project) {
      finalReviewStartButtons(true);
      setFinalReviewStatus("Дождитесь восстановления финальной проверки проекта или повторите запрос статуса.");
      return;
    }
    if (finalReview.startUncertain) {
      setFinalReviewStatus("Не удалось подтвердить запуск. Проверка могла начаться; повторите запрос статуса.");
      return;
    }
    var outputPath = finalReviewOutputPath();
    if (!outputPath) {
      setFinalReviewStatus("Укажите каталог projects и проект для финальной проверки.");
      return;
    }
    invalidateFinalReview();
    finalReview.project = config().project;
    finalReview.job = null;
    finalReview.sourceSequence = null;
    finalReview.applyRequestId = "";
    finalReview.retryIndex = 0;
    finalReview.exportResume = null;
    finalReview.layoutSequence = null;
    finalReview.resume = false;
    finalReview.retryAction = "status";
    finalReviewStartButtons(true);
    byId("retryFinalReview").hidden = true;
    byId("cancelFinalReview").hidden = true;
    setFinalReviewStatus("Проверяю активную sequence…");
    var generation = finalReview.generation;
    finalReview.inFlight = true;
    callHost("inspect", {}, function (error, response) {
      if (!finalReviewCurrent(generation)) return;
      finalReview.inFlight = false;
      var sequence = response && (response.sequence || response);
      if (error || !sequence || !sequence.sequenceId) {
        finalReviewStartButtons(false);
        setFinalReviewStatus("Не удалось прочитать собранную sequence: " + (error ? error.message : "нет sequence"));
        return;
      }
      finalReview.sourceSequence = sequence;
      finalReview.inFlight = true;
      if (fromWav) {
        captureFinalReviewLayout(sequence);
      } else {
        setFinalReviewStatus("Экспортирую звук собранного таймлайна из Premiere…");
        callHost("exportFinalReview", {outputPath: outputPath, expectedSequenceId: sequence.sequenceId, overwrite: true}, function (exportError, exported) {
          if (!finalReviewCurrent(generation)) return;
          finalReview.inFlight = false;
          if (exportError) {
            finalReviewStartButtons(false);
            setFinalReviewStatus("Не удалось экспортировать финальный WAV: " + exportError.message);
            return;
          }
          pollFinalReviewExport(exported.job.outputPath, exported.job.jobId, sequence);
        });
      }
    });
  }

  function startFinalReviewFromWav() { beginFinalReview(true); }
  function startFinalReview() { beginFinalReview(false); }

  function applyFinalReview() {
    beginFinalReviewApply(false);
  }

  function beginFinalReviewApply(reuseRequestId) {
    if (!finalReview.job || finalReview.job.status !== "completed" ||
        !finalReview.sourceSequence || finalReview.applyBusy ||
        !finalReviewCurrent(finalReview.generation)) return;
    clearFinalReviewTimer();
    var generation = finalReview.generation;
    finalReview.applyBusy = true;
    if (!reuseRequestId || !finalReview.applyRequestId) {
      finalReview.applyRequestId = "cep-" + Date.now().toString(36) + "-" + Math.random().toString(36).slice(2);
    }
    byId("retryFinalReview").hidden = true;
    byId("applyFinalReview").disabled = true;
    inspectFinalReviewSource(generation, function () {
      finalReviewRequest("POST", "/stage2/final-review/jobs/" + encodeURIComponent(finalReview.job.id) + "/apply", {
        project: finalReview.project,
        request_id: finalReview.applyRequestId
      }, generation, function (error, response) {
        if (!finalReviewCurrent(generation)) return;
        if (error) {
          finalReview.applyBusy = false;
          finalReview.retryAction = "apply";
          byId("retryFinalReview").hidden = false;
          setFinalReviewStatus("Не удалось подготовить итерацию. Повторный запрос использует тот же ID: " + error.message);
          log("Не удалось подготовить итерацию: " + error.message, response, "error");
          return;
        }
        var result = response.result || response;
        callHost("clone", { expectedSequenceId: finalReview.sourceSequence.sequenceId, nameSuffix: " · RC iteration " + result.iteration.id }, function (cloneError, cloned) {
          if (!finalReviewCurrent(generation)) return;
          if (cloneError) {
            finalReview.applyBusy = false;
            byId("applyFinalReview").disabled = false;
            log("Черновая sequence не создана: " + cloneError.message, cloned, "error");
            return;
          }
          var corrections = result.corrections || [];
          if (!corrections.length) {
            finalReview.applyBusy = false;
            setFinalReviewStatus("Создана черновая sequence «" + cloned.draft.name + "»; автоматических правок нет.");
            log("Черновая итерация создана; исходная sequence не изменена.", result, "success");
            return;
          }
          var items = corrections.map(function (item) {
            return {
              segmentId: item.segment_id,
              blockUid: item.block_uid,
              sourceSequenceId: item.source_sequence_id || "",
              sourceSequenceName: item.source_sequence_name || "",
              sourceInSeconds: Number(item.source_in_sec),
              sourceOutSeconds: Number(item.source_out_sec),
              timelineSeconds: Number(item.timeline_seconds),
              placeholderIdentity: item.placeholder_identity || ("RC_BLOCK " + item.block_uid),
              placeholderIdentityRequired: true,
              placeholderName: "",
              placeholderNames: []
            };
          });
          callHost("conform", {
            items: items,
            videoTrackIndex: CONFORM_VIDEO_TRACK_INDEX,
            audioTrackIndex: CONFORM_AUDIO_TRACK_INDEX,
            expectedTargetSequenceId: cloned.draft.sequenceId,
            expectedTargetSequenceName: cloned.draft.name,
            expectedProjectPath: cloned.draft.projectPath,
            expectedTimelineDuration: Number(cloned.draft.durationSeconds || 0),
            registeredSourceGuards: [],
            enableExplicitTrackShiftBatch: true,
            repairStaleInsertions: true
          }, function (conformError, conformed) {
            if (!finalReviewCurrent(generation)) return;
            finalReview.applyBusy = false;
            if (conformError) {
              setFinalReviewStatus("Черновая sequence создана, но правки границ не применены: " + conformError.message);
              log("Исправления остались подготовленными в отчёте.", conformed, "error");
              return;
            }
            setFinalReviewStatus("Создана и исправлена черновая sequence «" + cloned.draft.name + "».");
            log("Финальная итерация применена только к черновой sequence.", conformed, "success");
          });
        });
      });
    });
  }

  function bind() {
    byId("saveConfig").addEventListener("click", saveConfig);
    byId("refreshProjects").addEventListener("click", loadProjects);
    byId("requestRetry").addEventListener("click", retryPendingRequest);
    byId("checkConnection").addEventListener("click", checkConnection);
    byId("refreshWhisper").addEventListener("click", refreshWhisperStatusManually);
    byId("resumeWhisper").addEventListener("click", resumeTranscriptionJob);
    byId("cancelWhisper").addEventListener("click", cancelTranscriptionJob);
    byId("startFinalReview").addEventListener("click", startFinalReview);
    byId("startFinalReviewFromWav").addEventListener("click", startFinalReviewFromWav);
    byId("applyFinalReview").addEventListener("click", applyFinalReview);
    byId("retryFinalReview").addEventListener("click", retryFinalReview);
    byId("cancelFinalReview").addEventListener("click", cancelFinalReview);
    byId("apiToken").addEventListener("change", function () {
      loadProjects();
      refreshWhisperStatus();
      syncConfigCardOpenState();
      hideManualRetry();
    });
    byId("projectsRoot").addEventListener("change", function () {
      loadProjects();
      syncConfigCardOpenState();
      hideManualRetry();
    });
    byId("projectName").addEventListener("change", function () {
      window.localStorage.setItem("roughCutStage2.project", byId("projectName").value);
      currentWhisperJobId = "";
      finalReviewProjectChanged();
      guideAudioPath = "";
      byId("guideOutputPath").value = defaultGuideOutput();
      refreshWhisperStatus();
      syncConfigCardOpenState();
      hideManualRetry();
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
    document.addEventListener("visibilitychange", handleVisibilityChange);
  }

  function init() {
    loadConfig();
    finalReviewStartButtons(true);
    bind();
    syncConfigCardOpenState();
    renderWhisperIdle("Введите ключ доступа, чтобы видеть прогресс.");
    loadProjects();
    callHost("ping", undefined, function (error, response) {
      if (error) {
        setHostBadge("error", "Premiere: ошибка");
        log("Модуль Premiere недоступен: " + error.message, undefined, "error");
        return;
      }
      setHostBadge("ok", response.host || "Premiere");
      byId("log").textContent = "";
      log("Панель готова.", response, "success");
    });
  }

  if (window.__ROUGH_CUT_TEST_MODE__) {
    window.__ROUGH_CUT_TEST_HOOKS__ = {
      etaFromProgress: etaFromProgress,
      formatDuration: formatDuration,
      refreshWhisperStatus: refreshWhisperStatus,
      refreshWhisperStatusManually: refreshWhisperStatusManually,
      handleVisibilityChange: handleVisibilityChange,
      pollExport: pollExport,
      loadFreshTimeline: loadFreshTimeline,
      restoreFinalReview: restoreFinalReview,
      pollFinalReviewJob: pollFinalReviewJob,
      retryFinalReview: retryFinalReview,
      cancelFinalReview: cancelFinalReview,
      finalReviewProjectChanged: finalReviewProjectChanged,
      startFinalReview: startFinalReview,
      startFinalReviewFromWav: startFinalReviewFromWav,
      applyFinalReview: applyFinalReview,
      getFinalReviewState: function () {
        return {
          timer: finalReview.timer,
          retryIndex: finalReview.retryIndex,
          generation: finalReview.generation,
          visible: finalReview.visible,
          resume: finalReview.resume,
          job: finalReview.job,
          sourceSequence: finalReview.sourceSequence,
          applyRequestId: finalReview.applyRequestId,
          layoutSequence: finalReview.layoutSequence,
          startUncertain: finalReview.startUncertain,
          startBaselineJobId: finalReview.startBaselineJobId
        };
      },
      renderWhisperJob: renderWhisperJob,
      sourceRegistrationBlockReason: sourceRegistrationBlockReason,
      sourceIdentityMatchesSequence: sourceIdentityMatchesSequence,
      registeredSourceGuard: registeredSourceGuard,
      request: request,
      log: log,
      saveConfig: saveConfig,
      hideManualRetry: hideManualRetry,
      hasPendingManualRetry: function () {
        return pendingManualRetry !== null;
      },
      getExportPollTerminalCallCount: function () {
        return exportPollTerminalCallCount;
      }
    };
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
