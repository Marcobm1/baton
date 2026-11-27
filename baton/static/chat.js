// Local model chat: talks to a local llama-server model through Baton (/api/chat/<model>),
// persists threads in SQLite (/api/chat-threads) so desktop & phone stay in sync,
// and supports copy, regenerate, edit & resend, and sticky auto-scroll.
(function () {
  var root = document.querySelector("[data-chat]");
  if (!root || !root.querySelector("[data-form]")) return;
  var T = JSON.parse(document.getElementById("chat-strings").textContent);
  var bootEl = document.getElementById("chat-bootstrap");
  var boot = bootEl ? JSON.parse(bootEl.textContent) : {};
  var csrf = root.dataset.csrf;
  var log = root.querySelector("[data-log]"), empty = root.querySelector("[data-empty]");
  var form = root.querySelector("[data-form]"), text = root.querySelector("[data-text]");
  var send = root.querySelector("[data-send]"), modelSel = root.querySelector("[data-model]");
  var effortSel = root.querySelector("[data-effort]"), off = root.querySelector("[data-off]");
  var offText = root.querySelector("[data-off-text]"), startForm = root.querySelector("[data-start-form]");
  var scrollBtn = root.querySelector("[data-scroll-bottom]");
  var scrollEl = root.querySelector("[data-chat-scroll]");
  var presetSel = root.querySelector("[data-preset]");
  var customSys = root.querySelector("[data-custom-system]");
  var ctxMeter = root.querySelector("[data-ctx-meter]");
  var ctxText = root.querySelector("[data-ctx-text]");
  var ctxFill = root.querySelector("[data-ctx-fill]");
  var harnessRadios = root.querySelectorAll("[data-harness-mode]");
  var harnessFolderWrap = root.querySelector("[data-harness-folder-wrap]");
  var harnessFolderSel = root.querySelector("[data-harness-folder]");
  var harnessWarn = root.querySelector("[data-harness-warn]");
  var messages = [], threadId = "", busy = null, activeRunId = "", lastUsedTokens = 0;

  var BC = window.BatonChat;
  var mkEl = BC.el, md = BC.md, selectedLabel = BC.selectedLabel, shortFolder = BC.shortFolder;
  var attachCopyButton = BC.attachCopyButton, createAssistantBubble = BC.createAssistantBubble;
  var scroller = BC.createScroller(scrollBtn, scrollEl);
  var attachments = BC.createAttachments(root, text);

  var header = BC.initHeader({
    root: root,
    csrf: csrf,
    labels: {
      newChat: T.new_chat,
      untitled: T.untitled,
      rename: T.rename,
      delConfirm: T.del_confirm
    },
    onAfterRename: function () { sidebar.refresh(threadId); },
    onAfterDelete: function () { startNew(); sidebar.refresh(""); }
  });

  try { var eff = localStorage.getItem("baton-chat-effort"); if (eff && effortSel.querySelector('option[value="' + CSS.escape(eff) + '"]')) effortSel.value = eff; } catch (e) {}
  if (harnessFolderSel) {
    try {
      var savedFolder = localStorage.getItem("baton-chat-harness-folder");
      if (savedFolder && !harnessFolderSel.value && harnessFolderSel.querySelector('option[value="' + CSS.escape(savedFolder) + '"]')) {
        harnessFolderSel.value = savedFolder;
      }
    } catch (e) {}
  }
  if (presetSel) {
    try {
      var savedPreset = localStorage.getItem("baton-chat-preset") || "";
      if (presetSel.querySelector('option[value="' + CSS.escape(savedPreset) + '"]')) presetSel.value = savedPreset;
      if (customSys) customSys.value = localStorage.getItem("baton-chat-custom-sys") || "";
    } catch (e) {}
  }
  function harnessOn() {
    var onRadio = root.querySelector('[data-harness-mode][value="on"]');
    return !!(onRadio && onRadio.checked);
  }
  function syncHeaderContext() {
    if (!harnessOn() || !harnessFolderSel || !harnessFolderSel.value) {
      header.setContext("", "");
      return;
    }
    var opt = harnessFolderSel.options[harnessFolderSel.selectedIndex];
    var label = (opt && (opt.dataset.label || opt.textContent)) || harnessFolderSel.value;
    var branch = (opt && opt.dataset.branch) || "";
    header.setContext(label, branch);
  }
  function syncHarnessUi() {
    var on = harnessOn();
    if (harnessFolderWrap) harnessFolderWrap.hidden = !on;
    syncHeaderContext();
    if (!harnessWarn) return;
    if (!on) {
      harnessWarn.hidden = true;
      harnessWarn.classList.remove("is-abliterated");
      return;
    }
    var folderOpt = harnessFolderSel && harnessFolderSel.options[harnessFolderSel.selectedIndex];
    var hasFolder = !!(harnessFolderSel && harnessFolderSel.value);
    var msg = hasFolder
      ? (T.lh_warn_folder || "").replace("{folder}", folderOpt ? folderOpt.textContent : harnessFolderSel.value)
      : (T.lh_warn || "");
    var o = option();
    var ablit = !!(o && o.dataset.abliterated === "1");
    if (ablit && T.lh_warn_abliterated) msg += " " + T.lh_warn_abliterated;
    harnessWarn.textContent = msg;
    harnessWarn.classList.toggle("is-abliterated", ablit);
    harnessWarn.hidden = !msg;
  }
  function syncPresetUi() {
    if (customSys && presetSel) customSys.hidden = presetSel.value !== "custom";
  }
  function currentSystemPrompt() {
    if (!presetSel || !presetSel.value) return "";
    if (presetSel.value === "custom") return customSys ? customSys.value.trim() : "";
    return (T.presets && T.presets[presetSel.value]) || "";
  }
  syncPresetUi();
  syncHarnessUi();

  function formatTok(n) {
    return n >= 1000 ? (n / 1000).toFixed(n >= 10000 ? 0 : 1).replace(/\.0$/, "") + "k" : String(n);
  }
  function updateContextMeter() {
    if (!ctxMeter || !ctxText || !ctxFill) return;
    var o = option();
    var limit = o ? parseInt(o.dataset.ctx || "0", 10) : 0;
    if (!limit || (!messages.length && !lastUsedTokens)) {
      ctxMeter.hidden = true;
      return;
    }
    var approx = Math.round(
      (currentSystemPrompt().length + messages.reduce(function (acc, m) {
        return acc + (m.content || "").length + (m.reasoning || "").length;
      }, 0)) / 4
    );
    var used = lastUsedTokens > 0 ? Math.max(lastUsedTokens, approx) : approx;
    var pct = Math.min(100, Math.round((used / limit) * 100));
    ctxMeter.hidden = false;
    ctxMeter.classList.toggle("is-warn", pct >= 75 && pct < 90);
    ctxMeter.classList.toggle("is-bad", pct >= 90);
    ctxText.textContent = (T.ctx_label || "Context") + ": " + formatTok(used) + " / " + formatTok(limit) + " · " + pct + "%";
    ctxFill.style.width = pct + "%";
  }

  function api(path, method, body) {
    return fetch(path, {
      method: method || "POST",
      headers: { "Content-Type": "application/json", "X-Baton-CSRF": csrf },
      body: body != null ? JSON.stringify(body) : undefined
    }).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (j) {
        if (!r.ok) { var e = new Error(j.detail || "HTTP " + r.status); e.status = r.status; throw e; }
        return j;
      });
    });
  }

  function persistThread() {
    if (!messages.length) return Promise.resolve();
    var payload = {
      replace: messages.map(function (m) {
        return { role: m.role, content: m.content || "", reasoning: m.reasoning || "", meta: m.meta || "", tools: m.tools || [] };
      }),
      model: modelSel.value,
      effort: effortSel.value
    };
    if (threadId) {
      return api("/api/chat-threads/" + encodeURIComponent(threadId) + "/messages", "POST", payload)
        .then(function (updated) {
          header.setThread(threadId, updated && updated.title ? updated.title : undefined);
          sidebar.refresh(threadId);
        })
        .catch(function () {});
    }
    return api("/api/chat-threads", "POST", {
      agent: "local", model: modelSel.value, effort: effortSel.value
    }).then(function (th) {
      threadId = th.id;
      header.setThread(threadId, th.title || "");
      history.replaceState(null, "", "/chat?agent=local&thread=" + encodeURIComponent(threadId));
      return api("/api/chat-threads/" + encodeURIComponent(threadId) + "/messages", "POST", payload);
    }).then(function (updated) {
      if (updated && updated.title) header.setTitle(updated.title);
      sidebar.refresh(threadId);
    }).catch(function () {});
  }

  function abortActive() {
    if (activeRunId) {
      var rid = activeRunId;
      activeRunId = "";
      api("/api/chat-harness/" + encodeURIComponent(rid) + "/cancel", "POST", {}).catch(function () {});
    }
    if (busy) busy.abort();
  }

  function loadThread(id) {
    if (!id) return;
    abortActive();
    fetch("/api/chat-threads/" + encodeURIComponent(id), { cache: "no-store" })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (th) {
        if (!th) return;
        applyThread(th);
      }).catch(function () {});
  }

  function applyThread(th) {
    threadId = th.id || "";
    lastUsedTokens = 0;
    header.setThread(threadId, th.title || "");
    if (th.model && modelSel.querySelector('option[value="' + CSS.escape(th.model) + '"]')) {
      modelSel.value = th.model;
    }
    if (th.effort && effortSel.querySelector('option[value="' + CSS.escape(th.effort) + '"]')) {
      effortSel.value = th.effort;
    }
    messages = (th.messages || []).map(function (m) {
      return { role: m.role, content: m.content || "", reasoning: m.reasoning || "", meta: m.meta || "", tools: m.tools || [] };
    });
    sidebar.setActive(threadId);
    renderAll();
    showState();
    syncHarnessUi();
    updateContextMeter();
    if (composer) composer.sync();
    history.replaceState(null, "", "/chat?agent=local&thread=" + encodeURIComponent(threadId));
    scroller.follow(true);
  }

  function startNew() {
    abortActive();
    threadId = "";
    lastUsedTokens = 0;
    attachments.clear();
    header.setThread("", "");
    messages = [];
    sidebar.setActive("");
    renderAll();
    updateContextMeter();
    if (composer) composer.sync();
    history.replaceState(null, "", "/chat?model=" + encodeURIComponent(modelSel.value));
    text.focus();
  }

  var sidebar = window.BatonChat.initSidebar({
    csrf: csrf,
    agent: "local",
    activeId: (boot.active_thread && boot.active_thread.id) || "",
    initialThreads: boot.threads || [],
    labels: {
      empty: T.history_empty,
      untitled: T.untitled,
      rename: T.rename,
      del: T.del,
      delConfirm: T.del_confirm
    },
    onNew: startNew,
    onSelect: function (th) { loadThread(th.id); },
    onRenamedActive: function (title) { header.setTitle(title); },
    onActiveMeta: function (th) { if (th && th.title) header.setTitle(th.title); },
    onDeletedActive: startNew
  });

  // ----------------------------------------------------------- messages & tools
  var statusTextEl = root.querySelector("[data-status-text]");
  function setStatus(liveMsg) {
    if (statusTextEl) {
      statusTextEl.textContent = liveMsg || T.kbd_hint || "";
      // The keys hint only shows while typing on a keyboard (CSS); live status always shows.
      statusTextEl.classList.toggle("is-hint", !liveMsg);
    }
  }

  function bubble(role, idx) {
    var el;
    if (role === "assistant") {
      var b = createAssistantBubble(T);
      el = b.el;
      el._bubble = b;
    } else {
      el = mkEl("article", "msg msg-user");
      el.innerHTML = '<div class="msg-body"></div><p class="msg-meta muted small msg-user-meta"></p>';
      if (typeof idx === "number") attachUserActions(el, idx);
    }
    log.appendChild(el);
    empty.hidden = true;
    return el;
  }

  function attachUserActions(el, idx) {
    var meta = el.querySelector(".msg-user-meta");
    if (!meta) return;
    meta.innerHTML = "";
    var edit = document.createElement("button");
    edit.type = "button";
    edit.textContent = T.edit;
    edit.addEventListener("click", function () {
      if (busy) return;
      var msg = messages[idx];
      if (!msg) return;
      text.value = msg.content || "";
      messages = messages.slice(0, idx);
      lastUsedTokens = 0;
      renderAll();
      updateContextMeter();
      persistThread();
      autosize();
      text.focus();
    });
    meta.appendChild(edit);
  }

  function fill(el, m, isLastAssistant) {
    var b = el._bubble;
    if (m.reasoning) {
      b.think.hidden = false;
      if (m.thinkSec) {
        b.thinkSummary.textContent = (T.thought_for || "Thought for {s}s").replace("{s}", String(m.thinkSec));
      }
      b.thinkText.textContent = m.reasoning;
    }
    b.body.innerHTML = md(m.content || "");
    b.metaText.textContent = m.meta || "";
    b.meta.querySelectorAll("button").forEach(function (btn) { btn.remove(); });
    var streaming = m.meta === T.thinking || m.meta === T.writing || m.meta === T.lh_running_tools || m.meta === T.lh_waiting_perm;
    if (!streaming && m.content) {
      attachCopyButton(b.meta, function () { return m.content; }, T);
    }
    if (!streaming && isLastAssistant && !busy && ready()) {
      var regen = document.createElement("button");
      regen.type = "button";
      regen.textContent = T.regenerate;
      regen.addEventListener("click", function () {
        if (busy || !messages.length) return;
        if (messages[messages.length - 1].role === "assistant") messages.pop();
        if (!messages.length || messages[messages.length - 1].role !== "user") return;
        renderAll();
        streamReply();
      });
      b.meta.appendChild(regen);
    }
  }

  function renderAll() {
    log.querySelectorAll(".msg").forEach(function (el) { el.remove(); });
    messages.forEach(function (m, idx) {
      var el = bubble(m.role, idx);
      if (m.role === "assistant") {
        if ((m.tools && m.tools.length) || (m.notices && m.notices.length)) {
          var view = BC.createToolsView(el._bubble.tools, { T: T });
          (m.tools || []).forEach(function (ev) { view.onTool(ev); });
          (m.notices || []).forEach(function (nt) { view.onNotice(nt); });
        }
        fill(el, m, idx === messages.length - 1);
      } else {
        if (m.images && m.images.length) attachments.renderThumbs(el.querySelector(".msg-body"), m.images);
        if (m.content) {
          var p = document.createElement("span");
          p.textContent = m.content;
          el.querySelector(".msg-body").appendChild(p);
        }
      }
    });
    empty.hidden = messages.length > 0;
  }

  function number(x, digits) {
    return Number(x).toLocaleString(T.lang, { minimumFractionDigits: digits, maximumFractionDigits: digits });
  }

  // ---------------------------------------------------------- the model
  function option() { return modelSel.options[modelSel.selectedIndex]; }
  function ready() { var s = option().dataset.status; return s === "running" || s === "external"; }
  function showState() {
    var o = option(), s = o.dataset.status, name = o.dataset.name;
    var waiting = s === "loading";
    off.hidden = ready();
    offText.textContent = (waiting ? T.loading : s === "port_busy" ? T.busy : T.off).replace("{name}", name);
    startForm.hidden = waiting || s === "port_busy";
    startForm.action = "/models/" + encodeURIComponent(o.value) + "/start";
    send.disabled = !ready() && !busy;
    text.disabled = !ready();
    updateContextMeter();
  }
  function refresh() {
    if (busy) return;
    fetch("/api/models", { cache: "no-store" })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (view) {
        if (!view) return;
        view.models.forEach(function (m) {
          var o = modelSel.querySelector('option[value="' + CSS.escape(m.id) + '"]');
          if (o) {
            o.dataset.status = m.status;
            o.dataset.ctx = m.ctx || 0;
            o.dataset.abliterated = m.abliterated ? "1" : "0";
            o.textContent = (m.uses_gpu ? "● " : "") + (o.dataset.name || m.name);
          }
        });
        showState();
        syncHarnessUi();
      })
      .catch(function () {});
  }
  function setBusy(on) {
    BC.setSendState(send, on, T);
    modelSel.disabled = on;
    harnessRadios.forEach(function (r) { r.disabled = on; });
    if (harnessFolderSel) harnessFolderSel.disabled = on;
    showState();
  }

  function buildPayloadMessages() {
    var out = [];
    var sys = currentSystemPrompt();
    if (sys) out.push({ role: "system", content: sys });
    messages.forEach(function (x) {
      if (x.role === "user" && x.images && x.images.length) {
        var parts = [];
        if (x.content) parts.push({ type: "text", text: x.content });
        x.images.forEach(function (url) { parts.push({ type: "image_url", image_url: { url: url } }); });
        out.push({ role: "user", content: parts });
      } else {
        out.push({ role: x.role, content: x.content || "" });
      }
    });
    return out;
  }

  // --------------------------------------------------------------- send
  async function streamHarnessReply(restoreOnFail) {
    var m = { role: "assistant", content: "", reasoning: "", meta: T.thinking, tools: [], notices: [] };
    var el = bubble("assistant");
    var thinkTimer = el._bubble.thinkTimer;
    var toolsView = BC.createToolsView(el._bubble.tools, { T: T });
    fill(el, m, false);
    scroller.follow(true);
    busy = new AbortController();
    activeRunId = "";
    setBusy(true);
    setStatus(T.thinking);
    var timings = null, rounds = 1, failed = false, queued = false, done = false;

    function schedulePaint() {
      if (queued || done) return;
      queued = true;
      requestAnimationFrame(function () {
        queued = false;
        if (done) return;
        fill(el, m, false);
        scroller.follow(false);
      });
    }

    try {
      var r = await fetch("/api/chat/" + encodeURIComponent(modelSel.value) + "/harness", {
        method: "POST", signal: busy.signal,
        headers: { "Content-Type": "application/json", "X-Baton-CSRF": csrf },
        body: JSON.stringify({
          messages: buildPayloadMessages(),
          reasoning_effort: effortSel.value,
          folder: harnessFolderSel ? harnessFolderSel.value : ""
        })
      });
      if (!r.ok) {
        var detail = "";
        try { detail = (await r.json()).detail; } catch (e) {}
        throw new Error(detail || "HTTP " + r.status);
      }
      var reader = r.body.getReader(), dec = new TextDecoder(), buf = "", problem = "";
      for (;;) {
        var part = await reader.read();
        if (part.done) break;
        buf += dec.decode(part.value, { stream: true });
        var events = buf.split("\n\n");
        buf = events.pop();
        events.forEach(function (ev) {
          ev.split("\n").forEach(function (line) {
            if (line.indexOf("data:") !== 0) return;
            var data = line.slice(5).trim(), j;
            if (!data || data === "[DONE]") return;
            try { j = JSON.parse(data); } catch (e) { return; }
            if (j.type === "run" && j.run_id) {
              activeRunId = j.run_id;
            } else if (j.type === "thought") {
              thinkTimer.start();
              m.reasoning += j.text || "";
              m.meta = T.thinking;
              setStatus(T.thinking);
              schedulePaint();
            } else if (j.type === "text") {
              var sec = thinkTimer.stop();
              if (sec) m.thinkSec = sec;
              m.content += j.text || "";
              m.meta = T.writing;
              setStatus(T.writing);
              schedulePaint();
            } else if (j.type === "tool") {
              var sec2 = thinkTimer.stop();
              if (sec2) m.thinkSec = sec2;
              toolsView.onTool(j, m.tools);
              m.meta = j.status === "in_progress" ? (T.lh_running_tools || T.thinking) : T.thinking;
              setStatus(m.meta);
              schedulePaint();
            } else if (j.type === "permission") {
              var sec3 = thinkTimer.stop();
              if (sec3) m.thinkSec = sec3;
              var runId = j.run_id || activeRunId;
              toolsView.onPermission(j, function (allow) {
                return api("/api/chat-harness/" + encodeURIComponent(runId) + "/permission", "POST", { id: j.id, allow: allow });
              });
              m.meta = T.lh_waiting_perm || T.thinking;
              setStatus((T.lh_waiting_perm || T.perm_title) + " · " + (T.perm_keys || "1 / 2"));
              fill(el, m, false);
              scroller.follow(true);
            } else if (j.type === "permission_done") {
              toolsView.settlePerm(j.id, j.outcome);
              m.meta = T.lh_running_tools || T.thinking;
              setStatus(m.meta);
              schedulePaint();
            } else if (j.type === "timings" && j.timings) {
              timings = j.timings;
              var liveUsed = (j.timings.prompt_n || 0) + (j.timings.predicted_n || 0);
              if (liveUsed > 0) { lastUsedTokens = liveUsed; updateContextMeter(); }
            } else if (j.type === "notice" && j.text) {
              toolsView.onNotice(j.text, m.notices);
              scroller.follow(false);
            } else if (j.type === "done") {
              if (j.timings) timings = j.timings;
              if (j.rounds) rounds = j.rounds;
            } else if (j.type === "error") {
              problem = j.message || "Harness error";
            }
          });
        });
        if (problem) throw new Error(problem);
      }
      if (timings) {
        var usedN = (timings.prompt_n || 0) + (timings.predicted_n || 0);
        if (usedN > 0) lastUsedTokens = usedN;
      }
      var metaParts = [];
      if (rounds > 1 && T.lh_rounds) metaParts.push(T.lh_rounds.replace("{n}", String(rounds)));
      if (timings) metaParts.push(number(timings.predicted_n, 0) + " tokens · " + number(timings.predicted_per_second, 1) + " tok/s");
      m.meta = metaParts.join(" · ");
    } catch (e) {
      if (e.name === "AbortError") { m.meta = T.stopped; }
      else { failed = true; m.meta = T.error + " " + e.message; }
    } finally {
      done = true;
      toolsView.clear();
      var finalS = thinkTimer.stop();
      if (finalS) m.thinkSec = finalS;
      busy = null;
      activeRunId = "";
      if (m.content || m.reasoning || (m.tools && m.tools.length) || (m.notices && m.notices.length)) {
        messages.push(m);
      } else if (failed && restoreOnFail != null) {
        messages.pop();
        text.value = restoreOnFail;
        autosize();
      }
      setBusy(false);
      setStatus("");
      renderAll();
      updateContextMeter();
      persistThread();
      scroller.follow(false);
      text.focus();
    }
  }

  async function streamReply(restoreOnFail) {
    if (harnessOn()) return streamHarnessReply(restoreOnFail);
    var m = { role: "assistant", content: "", reasoning: "", meta: T.thinking };
    var el = bubble("assistant");
    var thinkTimer = el._bubble.thinkTimer;
    fill(el, m, false);
    scroller.follow(true);
    busy = new AbortController();
    setBusy(true);
    setStatus(T.thinking);
    var timings = null, failed = false, queued = false, done = false;
    try {
      var r = await fetch("/api/chat/" + encodeURIComponent(modelSel.value), {
        method: "POST", signal: busy.signal,
        headers: { "Content-Type": "application/json", "X-Baton-CSRF": csrf },
        body: JSON.stringify({
          messages: buildPayloadMessages(),
          reasoning_effort: effortSel.value
        })
      });
      if (!r.ok) {
        var detail = "";
        try { detail = (await r.json()).detail; } catch (e) {}
        throw new Error(detail || "HTTP " + r.status);
      }
      var reader = r.body.getReader(), dec = new TextDecoder(), buf = "", problem = "";
      for (;;) {
        var part = await reader.read();
        if (part.done) break;
        buf += dec.decode(part.value, { stream: true });
        var events = buf.split("\n\n");
        buf = events.pop();
        events.forEach(function (ev) {
          ev.split("\n").forEach(function (line) {
            if (line.indexOf("data:") !== 0) return;
            var data = line.slice(5).trim(), j;
            if (!data || data === "[DONE]") return;
            try { j = JSON.parse(data); } catch (e) { return; }
            if (j.error) { problem = j.error.message || JSON.stringify(j.error); return; }
            var d = (j.choices && j.choices[0] && j.choices[0].delta) || {};
            if (d.reasoning_content) { thinkTimer.start(); m.reasoning += d.reasoning_content; }
            if (d.content) {
              var sec = thinkTimer.stop();
              if (sec) m.thinkSec = sec;
              m.content += d.content;
            }
            if (j.timings) timings = j.timings;
          });
        });
        if (problem) throw new Error(problem);
        if (!queued) {
          queued = true;
          requestAnimationFrame(function () {
            queued = false;
            if (done) return;
            m.meta = m.content ? T.writing : T.thinking;
            setStatus(m.meta);
            fill(el, m, false);
            scroller.follow(false);
          });
        }
      }
      if (timings) {
        var usedN = (timings.prompt_n || 0) + (timings.predicted_n || 0);
        if (usedN > 0) lastUsedTokens = usedN;
      }
      m.meta = timings ? number(timings.predicted_n, 0) + " tokens · " + number(timings.predicted_per_second, 1) + " tok/s" : "";
    } catch (e) {
      if (e.name === "AbortError") { m.meta = T.stopped; }
      else { failed = true; m.meta = T.error + " " + e.message; }
    } finally {
      done = true;
      var finalS = thinkTimer.stop();
      if (finalS) m.thinkSec = finalS;
      busy = null;
      if (m.content || m.reasoning) {
        messages.push(m);
      } else if (failed && restoreOnFail != null) {
        messages.pop();
        text.value = restoreOnFail;
        autosize();
      }
      setBusy(false);
      setStatus("");
      renderAll();
      updateContextMeter();
      persistThread();
      scroller.follow(false);
      text.focus();
    }
  }

  async function ask() {
    var content = text.value.trim();
    var imgs = attachments.list();
    if ((!content && !imgs.length) || busy || !ready()) return;
    messages.push({ role: "user", content: content, images: imgs });
    text.value = "";
    attachments.clear();
    autosize();
    renderAll();
    await streamReply(content);
  }

  // ------------------------------------------------------------- events
  var agentSel = root.querySelector("[data-agent-switch]");
  var composer = BC.initComposerControls({
    root: root,
    labels: { locked: T.locked, filterModels: T.filter_models },
    chips: [
      { id: "agent", title: T.lbl_agent, select: agentSel, label: function () { return selectedLabel(agentSel); }, isLocked: function () { return !!busy; } },
      { id: "model", title: T.lbl_model, select: modelSel, label: function () { return selectedLabel(modelSel); }, isLocked: function () { return !!busy; } },
      { id: "effort", title: T.lbl_effort, select: effortSel, label: function () { return selectedLabel(effortSel); }, isLocked: function () { return !!busy; } },
      {
        id: "mode", title: T.lbl_mode, shortcut: "Shift+Tab",
        label: function () { return harnessOn() ? T.lh_on : T.lh_off; },
        tone: function () { return harnessOn() ? "warn" : ""; },
        isLocked: function () { return !!busy; },
        renderPopover: function (body, close) {
          var offR = root.querySelector('[data-harness-mode][value="off"]');
          var onR = root.querySelector('[data-harness-mode][value="on"]');
          var items = [
            { radio: offR, title: T.lh_off, desc: "", warn: false },
            { radio: onR, title: T.lh_on, desc: [T.lh_help_tools, T.lh_help_safety].filter(Boolean).join(" "), warn: true }
          ];
          items.forEach(function (item) {
            if (!item.radio) return;
            var active = item.radio.checked;
            var row = mkEl("button", "cc-pop-mode" + (active ? " is-active" : "") + (item.warn ? " is-warn" : ""));
            row.type = "button";
            row.disabled = !!item.radio.disabled;
            var top = mkEl("span", "cc-pop-mode-head");
            top.appendChild(mkEl("span", "cc-pop-mode-title", item.title));
            if (active) top.appendChild(mkEl("span", "cc-pop-check", "✓"));
            row.appendChild(top);
            if (item.desc) row.appendChild(mkEl("span", "cc-pop-mode-desc", item.desc));
            row.addEventListener("click", function () {
              item.radio.checked = true;
              syncHarnessUi();
              composer.sync();
              close(false);
              if (!text.disabled) text.focus();
            });
            body.appendChild(row);
          });
        }
      },
      { id: "folder", title: T.lbl_folder, select: harnessFolderSel, label: function () { return shortFolder(harnessFolderSel); }, isHidden: function () { return !harnessOn() || !harnessFolderSel; }, isLocked: function () { return !!busy; } },
      { id: "preset", title: T.lbl_preset, select: presetSel, label: function () { return selectedLabel(presetSel); }, tone: function () { return presetSel && presetSel.value ? "accent" : ""; }, isHidden: function () { return !presetSel; }, isLocked: function () { return !!busy; } }
    ],
    isBusy: function () { return !!busy; },
    onCancel: function () { abortActive(); },
    onCycleMode: function () {
      if (busy) return;
      var offR = root.querySelector('[data-harness-mode][value="off"]');
      var onR = root.querySelector('[data-harness-mode][value="on"]');
      if (!offR || !onR) return;
      if (onR.checked) offR.checked = true;
      else onR.checked = true;
      syncHarnessUi();
      composer.sync();
    }
  });
  var origSetBusy = setBusy;
  setBusy = function (on) { origSetBusy(on); composer.sync(); };
  var origRefresh = refresh;
  refresh = function () {
    origRefresh();
    setTimeout(function () { composer.sync(); }, 250);
  };

  var autosize = BC.bindAutosize(text);
  text.addEventListener("keydown", function (e) {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); ask(); }
  });
  form.addEventListener("submit", function (e) {
    e.preventDefault();
    if (busy) abortActive(); else ask();
  });
  modelSel.addEventListener("change", function () {
    showState();
    syncHarnessUi();
    composer.sync();
    if (!threadId) history.replaceState(null, "", "/chat?model=" + encodeURIComponent(modelSel.value));
  });
  effortSel.addEventListener("change", function () {
    composer.sync();
    try { localStorage.setItem("baton-chat-effort", effortSel.value); } catch (e) {}
  });
  harnessRadios.forEach(function (r) {
    r.addEventListener("change", function () { syncHarnessUi(); composer.sync(); });
  });
  if (harnessFolderSel) {
    harnessFolderSel.addEventListener("change", function () {
      syncHarnessUi();
      composer.sync();
      try { localStorage.setItem("baton-chat-harness-folder", harnessFolderSel.value); } catch (e) {}
    });
  }
  if (presetSel) {
    presetSel.addEventListener("change", function () {
      syncPresetUi();
      updateContextMeter();
      composer.sync();
      try { localStorage.setItem("baton-chat-preset", presetSel.value); } catch (e) {}
    });
  }
  if (customSys) {
    customSys.addEventListener("input", function () {
      updateContextMeter();
      try { localStorage.setItem("baton-chat-custom-sys", customSys.value); } catch (e) {}
    });
  }
  root.querySelectorAll("[data-new]").forEach(function (btn) {
    btn.addEventListener("click", function () { startNew(); composer.sync(); });
  });

  if (boot.active_thread && boot.active_thread.agent === "local") {
    applyThread(boot.active_thread);
  } else {
    renderAll();
    showState();
    syncHarnessUi();
  }
  composer.sync();
  setInterval(function () { if (!document.hidden) refresh(); }, 4000);
})();
