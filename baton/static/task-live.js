/* Live SSE updates for queued/running task pages (/tasks/{id}). */
(function () {
  "use strict";

  const root = document.querySelector("article.detail[data-task-id]");
  if (!root || typeof EventSource === "undefined") return;

  const taskId = root.dataset.taskId;
  const initialStatus = root.dataset.taskStatus || "";
  if (initialStatus !== "queued" && initialStatus !== "running") return;

  const noStep = root.dataset.noStep || "";
  const noProgress = root.dataset.noProgress || "";
  const noLog = root.dataset.noLog || "";

  const statusEl = document.getElementById("task-status");
  const lastStepEl = document.getElementById("task-last-step");
  const progressEl = document.getElementById("task-progress");
  const stepsEl = document.getElementById("task-steps");
  const logEl = document.getElementById("task-log");
  const cancelForm = root.querySelector("form.cancel-form");

  let lastStatus = initialStatus;
  const url = "/api/tasks/" + encodeURIComponent(taskId) + "/events";
  const source = new EventSource(url);

  function updateLog(text) {
    if (!logEl) return;
    const atBottom = logEl.scrollHeight - logEl.scrollTop - logEl.clientHeight < 32;
    logEl.textContent = text || noLog;
    if (atBottom) {
      logEl.scrollTop = logEl.scrollHeight;
    }
  }

  function updateSteps(steps) {
    if (!stepsEl || !Array.isArray(steps)) return;
    stepsEl.textContent = "";
    for (const s of steps) {
      const li = document.createElement("li");
      li.textContent = s;
      stepsEl.appendChild(li);
    }
  }

  source.addEventListener("update", function (ev) {
    let data;
    try {
      data = JSON.parse(ev.data);
    } catch (_err) {
      return;
    }
    if (data.status && statusEl) {
      statusEl.className = "pill st st-" + data.status;
      if (data.status_label) statusEl.textContent = data.status_label;
    }
    // Transition from queued -> running swaps queue controls for running view.
    if (lastStatus === "queued" && data.status === "running") {
      source.close();
      location.reload();
      return;
    }
    lastStatus = data.status || lastStatus;

    if (lastStepEl) {
      lastStepEl.textContent = data.last_step || noStep;
    }
    if (progressEl) {
      const prog = data.progress ? String(data.progress).slice(0, 240) : "";
      progressEl.textContent = prog || noProgress;
    }
    updateSteps(data.steps);
    updateLog(data.log);
    if (cancelForm && typeof data.can_cancel === "boolean") {
      cancelForm.hidden = !data.can_cancel;
    }
  });

  source.addEventListener("done", function () {
    source.close();
    location.reload();
  });

  window.addEventListener("beforeunload", function () {
    source.close();
  });
})();
