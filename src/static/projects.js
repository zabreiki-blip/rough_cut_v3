(function () {
  const newProject = document.getElementById("new_project");
  const btnCreate = document.getElementById("btn_create");
  const btnReload = document.getElementById("btn_reload");
  const btnClearLog = document.getElementById("btn_clear_log");
  const log = document.getElementById("log");
  const toastContainer = document.getElementById("toast-container");

  function addLog(msg) {
    const ts = new Date().toLocaleTimeString("ru-RU");
    log.textContent += `[${ts}] ${msg}\n`;
    log.scrollTop = log.scrollHeight;
  }

  function toast(message, type = "info") {
    const el = document.createElement("div");
    el.className = `toast ${type}`;
    el.textContent = message;
    toastContainer.appendChild(el);
    setTimeout(() => {
      el.style.opacity = "0";
      el.style.transform = "translateX(20px)";
      el.style.transition = "all 200ms";
      setTimeout(() => el.remove(), 200);
    }, 3000);
  }

  async function createProject() {
    const name = (newProject.value || "").trim();
    if (!name) {
      toast("Укажи имя проекта", "error");
      newProject.focus();
      return;
    }
    if (!/^[a-zA-Z0-9_-]+$/.test(name)) {
      toast("Недопустимые символы в имени", "error");
      newProject.focus();
      return;
    }
    btnCreate.disabled = true;
    try {
      addLog(`Создание проекта ${name}...`);
      const res = await fetch("/projects", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ project: name }),
      });
      const data = await res.json();
      if (!res.ok || data.ok === false) {
        throw new Error(data.error || "Ошибка создания проекта");
      }
      toast(`Проект "${data.project}" создан`, "success");
      addLog(`Проект готов: ${data.project}`);
      setTimeout(() => { window.location.href = data.url; }, 600);
    } catch (err) {
      toast(err.message || String(err), "error");
      addLog(`ERROR: ${err.message || String(err)}`);
    } finally {
      btnCreate.disabled = false;
    }
  }

  btnCreate.addEventListener("click", createProject);
  newProject.addEventListener("keydown", (e) => {
    if (e.key === "Enter") createProject();
  });
  btnReload.addEventListener("click", () => window.location.reload());
  if (btnClearLog) {
    btnClearLog.addEventListener("click", () => { log.textContent = ""; });
  }

  const list = window.__PROJECT_LIST__ || [];
  addLog(`Проектов: ${list.length}`);
})();
