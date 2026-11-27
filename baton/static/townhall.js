// Town Hall frontend: dynamic 2–4 participant picker + live SSE progress viewer.
(function () {
  // --------------------------------------------------- 1. Dynamic model picker
  var famEl = document.getElementById("th-families-data");
  var i18nEl = document.getElementById("th-picker-i18n");
  if (famEl) {
    var families = [];
    var T = {};
    try { families = JSON.parse(famEl.textContent || "[]"); } catch (_e) {}
    try { T = JSON.parse((i18nEl && i18nEl.textContent) || "{}"); } catch (_e) {}
    var famById = {};
    families.forEach(function (f) { famById[f.id] = f; });

    function opt(value, label, opts) {
      var o = document.createElement("option");
      o.value = value;
      o.textContent = label;
      if (opts && opts.disabled) o.disabled = true;
      if (opts && opts.selected) o.selected = true;
      return o;
    }

    function familyOptionLabel(f, demoOn) {
      var q = f.quota && f.quota.summary ? f.quota.summary : "";
      if (q) {
        var low = f.quota.exhausted ? " · " + (T.quota_low || "low") : "";
        return f.label + " (" + q + low + ")";
      }
      if (!f.available && !demoOn && !(f.models && f.models.length)) {
        return f.label + " (" + (T.not_installed || "not installed") + ")";
      }
      return f.label;
    }

    function modelsFor(f, demoOn) {
      if (!f) return [];
      if (f.models && f.models.length) return f.models;
      if (demoOn && f.demo_models && f.demo_models.length) return f.demo_models;
      return [];
    }

    document.querySelectorAll("[data-th-picker]").forEach(function (picker) {
      var minSlots = parseInt(picker.getAttribute("data-min") || "2", 10) || 2;
      var maxSlots = parseInt(picker.getAttribute("data-max") || "4", 10) || 4;
      var slotsBox = picker.querySelector("[data-th-slots]");
      var addBtn = picker.querySelector("[data-th-add]");
      var countEl = picker.querySelector("[data-th-count]");
      var modId = picker.getAttribute("data-moderator-target") || "";
      var modSel = modId ? document.getElementById(modId) : null;
      var form = picker.closest("form");
      var demoBox = form ? form.querySelector("[data-th-demo]") : null;

      function isDemo() {
        return !!(demoBox && demoBox.checked);
      }

      function populateFamilySelect(sel, keepVal) {
        var demoOn = isDemo();
        var cur = keepVal !== undefined ? keepVal : sel.value;
        sel.replaceChildren();
        sel.appendChild(opt("", T.choose_family || "Choose family…", { selected: !cur }));
        families.forEach(function (f) {
          var list = modelsFor(f, demoOn);
          var disabled = !demoOn && !f.available && !list.length;
          sel.appendChild(opt(f.id, familyOptionLabel(f, demoOn), {
            disabled: disabled,
            selected: cur === f.id && !disabled,
          }));
        });
      }

      function populateModelSelect(modelSel, familyId, keepVal) {
        var demoOn = isDemo();
        var f = famById[familyId];
        var cur = keepVal !== undefined ? keepVal : "";
        modelSel.replaceChildren();
        if (!f) {
          modelSel.disabled = true;
          modelSel.appendChild(opt("", T.choose_family_first || "Pick a family first…", { selected: true }));
          return;
        }
        var list = modelsFor(f, demoOn);
        if (!list.length) {
          modelSel.disabled = true;
          modelSel.appendChild(opt("", T.no_models || "No models available", { selected: true }));
          return;
        }
        modelSel.disabled = false;
        var hasCur = list.some(function (m) { return m.id === cur; });
        modelSel.appendChild(opt("", T.choose_model || "Choose model…", { selected: !hasCur }));
        list.forEach(function (m) {
          modelSel.appendChild(opt(m.id, m.label || m.id, { selected: hasCur && m.id === cur }));
        });
      }

      function syncModerator() {
        if (!modSel) return;
        var prev = modSel.value;
        var slots = Array.from(slotsBox.querySelectorAll("[data-th-slot]"));
        modSel.replaceChildren();
        var anyChosen = false;
        slots.forEach(function (slot, idx) {
          var famSel = slot.querySelector("[data-th-family]");
          var mSel = slot.querySelector("[data-th-model]");
          var fid = famSel ? famSel.value : "";
          var mid = mSel ? mSel.value : "";
          var fam = famById[fid];
          var label;
          if (fam && mid) {
            anyChosen = true;
            var mOpt = mSel.options[mSel.selectedIndex];
            label = (idx + 1) + " · " + fam.label + " — " + ((mOpt && mOpt.textContent) || mid);
          } else if (fam) {
            label = (idx + 1) + " · " + fam.label;
          } else {
            label = (idx + 1) + " · " + (T.slot_label || "Model") + " " + (idx + 1);
          }
          modSel.appendChild(opt(String(idx), label, { selected: prev === String(idx) }));
        });
        if (!anyChosen && !modSel.options.length) {
          modSel.appendChild(opt("0", T.moderator_pick || "Choose participants first…", { selected: true }));
        } else if (modSel.selectedIndex < 0 && modSel.options.length) {
          modSel.selectedIndex = 0;
        }
      }

      function syncControls() {
        var slots = Array.from(slotsBox.querySelectorAll("[data-th-slot]"));
        slots.forEach(function (slot, idx) {
          var num = slot.querySelector("[data-th-num]");
          if (num) num.textContent = String(idx + 1);
          var rm = slot.querySelector("[data-th-remove]");
          if (rm) rm.disabled = slots.length <= minSlots;
        });
        if (countEl) countEl.textContent = slots.length + "/" + maxSlots;
        if (addBtn) addBtn.disabled = slots.length >= maxSlots;
        syncModerator();
      }

      function wireSlot(slot) {
        var famSel = slot.querySelector("[data-th-family]");
        var modelSel = slot.querySelector("[data-th-model]");
        var rmBtn = slot.querySelector("[data-th-remove]");
        if (famSel) {
          famSel.addEventListener("change", function () {
            populateModelSelect(modelSel, famSel.value, "");
            syncModerator();
          });
        }
        if (modelSel) {
          modelSel.addEventListener("change", function () {
            syncModerator();
          });
        }
        if (rmBtn) {
          rmBtn.addEventListener("click", function () {
            var count = slotsBox.querySelectorAll("[data-th-slot]").length;
            if (count <= minSlots) return;
            slot.remove();
            syncControls();
          });
        }
      }

      function createSlot() {
        var row = document.createElement("div");
        row.className = "th-slot";
        row.setAttribute("data-th-slot", "");

        var num = document.createElement("span");
        num.className = "th-slot-num";
        num.setAttribute("data-th-num", "");

        var famSel = document.createElement("select");
        famSel.name = "participant_provider";
        famSel.required = true;
        famSel.setAttribute("data-th-family", "");
        famSel.setAttribute("aria-label", T.choose_family || "Choose family…");
        populateFamilySelect(famSel, "");

        var modelSel = document.createElement("select");
        modelSel.name = "participant_model";
        modelSel.required = true;
        modelSel.setAttribute("data-th-model", "");
        modelSel.setAttribute("aria-label", T.choose_model || "Choose model…");
        populateModelSelect(modelSel, "", "");

        var rmBtn = document.createElement("button");
        rmBtn.type = "button";
        rmBtn.className = "btn ghost small th-slot-rm";
        rmBtn.setAttribute("data-th-remove", "");
        rmBtn.setAttribute("aria-label", T.remove_model || "Remove participant");
        rmBtn.title = T.remove_model || "Remove participant";
        rmBtn.textContent = "×";

        row.appendChild(num);
        row.appendChild(famSel);
        row.appendChild(modelSel);
        row.appendChild(rmBtn);
        wireSlot(row);
        return row;
      }

      // Wire SSR initial slots (2 empty rows, no preselected model).
      slotsBox.querySelectorAll("[data-th-slot]").forEach(function (slot) {
        var famSel = slot.querySelector("[data-th-family]");
        var modelSel = slot.querySelector("[data-th-model]");
        populateFamilySelect(famSel, famSel.value);
        populateModelSelect(modelSel, famSel.value, modelSel.value);
        wireSlot(slot);
      });

      if (addBtn) {
        addBtn.addEventListener("click", function () {
          if (slotsBox.querySelectorAll("[data-th-slot]").length >= maxSlots) return;
          var slot = createSlot();
          slotsBox.appendChild(slot);
          syncControls();
          var famSel = slot.querySelector("[data-th-family]");
          if (famSel) famSel.focus();
        });
      }

      if (demoBox) {
        demoBox.addEventListener("change", function () {
          slotsBox.querySelectorAll("[data-th-slot]").forEach(function (slot) {
            var famSel = slot.querySelector("[data-th-family]");
            var modelSel = slot.querySelector("[data-th-model]");
            var prevFam = famSel.value;
            var prevMod = modelSel.value;
            populateFamilySelect(famSel, prevFam);
            populateModelSelect(modelSel, famSel.value, prevMod);
          });
          syncModerator();
        });
      }

      syncControls();
    });
  }

  // --------------------------------------------------- 2. Live SSE viewer
  var box = document.getElementById("th-live-log");
  if (!box) return;
  var baseUrl = box.getAttribute("data-events-url");
  if (!baseUrl || typeof EventSource === "undefined") return;
  var statusUrl = box.getAttribute("data-status-url") || "";
  var statusEl = document.getElementById("th-live-status");
  var liveI18nEl = document.getElementById("th-live-i18n");
  var L = {};
  try { L = JSON.parse((liveI18nEl && liveI18nEl.textContent) || "{}"); } catch (_e) {}

  var es = null;
  var lines = [];
  var lastSeq = 0;
  var reconnectDelay = 1000;
  var reconnectTimer = null;
  var finished = false;
  var reconnecting = false;
  // Map participant_id -> { name, startedAt } for currently active turns.
  var activeTurns = {};

  function setStatus(text, cls) {
    if (!statusEl) return;
    statusEl.textContent = text;
    statusEl.className = "th-pill " + (cls || "is-running");
  }

  function renderActiveStatus() {
    if (finished || reconnecting) return;
    var ids = Object.keys(activeTurns);
    if (!ids.length) {
      setStatus(L.connected || "Connected · waiting for next step", "is-running");
      return;
    }
    var now = Date.now();
    var minStart = now;
    var names = ids.map(function (id) {
      var t = activeTurns[id];
      if (t.startedAt < minStart) minStart = t.startedAt;
      return t.name;
    });
    var secs = Math.max(0, Math.floor((now - minStart) / 1000));
    var prefix = L.thinking || "Thinking";
    setStatus(prefix + " (" + secs + "s): " + names.join(", "), "is-running");
  }

  var tickTimer = window.setInterval(renderActiveStatus, 1000);

  function formatEvent(ev) {
    var p = ev.payload || {};
    if (ev.type === "session_started") {
      return "-> Started (" + (p.mode || "council") + (p.workspace_isolated ? ", isolated worktree" : "") + ")";
    }
    if (ev.type === "phase_started") {
      var extra = p.moderator_id ? " · moderator: " + p.moderator_id : "";
      return "-> Phase: " + (p.phase || "") + " (round " + (p.round_num || 0) + extra + ")";
    }
    if (ev.type === "guest_selected") {
      return "-> Consensus guest (" + (p.method || "") + "): " + (p.chosen_model || "");
    }
    if (ev.type === "guest_fallback") {
      return "-> Guest fallback: " + (p.failed_model || "?") + " failed -> switched to " + (p.fallback_model || "?");
    }
    if (ev.type === "turn_started") {
      return null;
    }
    if (ev.type === "turn_completed") {
      var dur = typeof p.duration_seconds === "number" ? " (" + p.duration_seconds.toFixed(1) + "s)" : "";
      var st = p.ok ? ("ok" + dur) : ("error" + dur + ": " + (p.error || "failed"));
      return "   [" + (p.participant_name || p.participant_id || "?") + "] " + st;
    }
    if (ev.type === "budget_reached") {
      return "-> Time budget reached; moving to synthesis";
    }
    if (ev.type === "session_failed" && p.error) {
      return "-> Session failed: " + p.error;
    }
    return "-> " + ev.type;
  }

  function finishAndReload() {
    if (finished) return;
    finished = true;
    if (reconnectTimer) { window.clearTimeout(reconnectTimer); reconnectTimer = null; }
    if (tickTimer) { window.clearInterval(tickTimer); tickTimer = null; }
    if (es) { es.close(); es = null; }
    window.location.reload();
  }

  function checkSessionDone(onStillRunning) {
    if (!statusUrl || typeof fetch === "undefined") {
      if (onStillRunning) onStillRunning();
      return;
    }
    fetch(statusUrl, { credentials: "same-origin", cache: "no-store" })
      .then(function (resp) { return resp.ok ? resp.json() : null; })
      .then(function (data) {
        if (data && data.status && data.status !== "running") {
          finishAndReload();
          return;
        }
        if (onStillRunning) onStillRunning();
      })
      .catch(function () {
        if (onStillRunning) onStillRunning();
      });
  }

  function connect() {
    if (finished) return;
    var sep = baseUrl.indexOf("?") >= 0 ? "&" : "?";
    var streamUrl = lastSeq > 0 ? (baseUrl + sep + "after=" + encodeURIComponent(String(lastSeq))) : baseUrl;
    es = new EventSource(streamUrl);

    es.onopen = function () {
      reconnectDelay = 1000;
      reconnecting = false;
      renderActiveStatus();
    };

    es.onmessage = function (e) {
      if (e.lastEventId) {
        var parsedId = parseInt(e.lastEventId, 10);
        if (!isNaN(parsedId) && parsedId > lastSeq) lastSeq = parsedId;
      }
      var ev;
      try {
        ev = JSON.parse(e.data);
      } catch (_err) {
        return;
      }
      var p = ev.payload || {};
      if (ev.type === "turn_started") {
        var pid = p.participant_id || p.participant_name || "?";
        activeTurns[pid] = {
          name: p.participant_name || pid,
          startedAt: Date.now(),
        };
        renderActiveStatus();
      } else if (ev.type === "turn_completed") {
        var cid = p.participant_id || p.participant_name || "?";
        delete activeTurns[cid];
        renderActiveStatus();
      } else if (ev.type === "phase_started") {
        activeTurns = {};
        renderActiveStatus();
      }

      var line = formatEvent(ev);
      if (line !== null) {
        lines.push(line);
        box.textContent = lines.join("\n");
        box.scrollTop = box.scrollHeight;
      }
      if (ev.type === "session_completed" || ev.type === "session_cancelled" || ev.type === "session_failed") {
        finishAndReload();
      }
    };

    es.onerror = function () {
      if (finished) return;
      if (es) { es.close(); es = null; }
      reconnecting = true;
      setStatus(L.reconnecting || "Reconnecting…", "is-running");
      var delay = reconnectDelay;
      reconnectDelay = Math.min(reconnectDelay * 2, 10000);
      reconnectTimer = window.setTimeout(function () {
        reconnectTimer = null;
        checkSessionDone(connect);
      }, delay);
    };
  }

  connect();
})();
