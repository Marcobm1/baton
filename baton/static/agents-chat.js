// Agents chat (OpenCode, Gemini, Claude Code, Codex). Baton keeps the active
// process on the server, persists turns into SQLite (chat_threads/chat_messages)
// for cross-device history, and replays events over SSE.
(function () {
  var root = document.querySelector("[data-agents]");
  if (!root || !root.querySelector("[data-form]")) return;
  var T = JSON.parse(document.getElementById("chat-strings").textContent);
  var bootEl = document.getElementById("chat-bootstrap");
  var boot = bootEl ? JSON.parse(bootEl.textContent) : {};
  var BC = window.BatonChat;
  var el = BC.el, md = BC.md, selectedLabel = BC.selectedLabel, shortFolder = BC.shortFolder;
  var createAssistantBubble = BC.createAssistantBubble;
  var csrf = root.dataset.csrf;
  function $(s) { return root.querySelector(s); }
  var log = $("[data-log]"), empty = $("[data-empty]"), form = $("[data-form]"), text = $("[data-text]");
  var send = $("[data-send]"), modelSel = $("[data-oc-model]"), effortSel = $("[data-oc-effort]");
  var folderSel = $("[data-oc-folder]"), resumeSel = $("[data-oc-resume]"), payg = $("[data-payg-warn]"), fixed = $("[data-fixed]");
  var fixedText = $("[data-fixed-text]"), fullBadge = $("[data-full-badge]");
  var fullWarn = $("[data-full-warn]"), fullNote = $("[data-full-note]");
  var confirmRadio = root.querySelector('[data-mode][value="confirm"]');
  var fullRadio = root.querySelector('[data-mode][value="full"]');
  var scrollBtn = $("[data-scroll-bottom]");
  var scrollEl = $("[data-chat-scroll]");
  var scroller = BC.createScroller(scrollBtn, scrollEl);
  var attachments = BC.createAttachments(root, text);
  var sandboxReady = root.dataset.sandboxReady === "1";

  var agent = root.dataset.agent || "opencode";
  var suffix = agent === "opencode" ? "" : "-" + agent;
  var STORE = "baton-agents-chat" + suffix, PREFS = "baton-agents-prefs" + suffix;
  var effortsByModel = modelSel.dataset.efforts ? JSON.parse(modelSel.dataset.efforts) : null;
  var allEfforts = Array.from(effortSel.options).map(function (o) { return o.cloneNode(true); });
  function setEfforts() {
    if (!effortsByModel) return;
    var allowed = effortsByModel[modelSel.value] || [], was = effortSel.value;
    effortSel.replaceChildren.apply(effortSel, allEfforts.filter(function (o) {
      return o.value === "default" || allowed.indexOf(o.value) >= 0;
    }).map(function (o) { return o.cloneNode(true); }));
    if (effortSel.querySelector('option[value="' + CSS.escape(was) + '"]')) effortSel.value = was;
  }
  var chat = null, threadId = "", after = 0, source = null, busy = false, current = null;
  var refused = false;
  var toolsView = BC.createToolsView(log, {
    T: T,
    showToolName: true,
    onInsert: function (kind) {
      empty.hidden = true;
      if (kind !== "notice") current = null;
    }
  });

  // ------------------------------------------------------------ helpers
  function api(path, body) {
    return fetch(path, {
      method: "POST", headers: { "Content-Type": "application/json", "X-Baton-CSRF": csrf },
      body: JSON.stringify(body || {})
    }).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (j) {
        if (!r.ok) { var e = new Error(j.detail || "HTTP " + r.status); e.status = r.status; throw e; }
        return j;
      });
    });
  }
  function add(node) { log.appendChild(node); empty.hidden = true; return node; }
  function note(cls, msg) { return toolsView.onNotice(msg, null, cls); }

  function isLocalModel(m) {
    return typeof m === "string" && m.toLowerCase().indexOf("local-") === 0 && m.indexOf("/") > 0;
  }
  function isAbliteratedName(m) {
    return /abliterat|uncensor/i.test(m || "");
  }
  function updateFullMode() {
    if (!fullRadio) return;
    if (chat) {
      if (fullWarn) fullWarn.hidden = chat.mode !== "full";
      return;
    }
    var m = modelSel.value || "";
    var allowed = sandboxReady && agent === "opencode" && isLocalModel(m) && !isAbliteratedName(m);
    fullRadio.disabled = !allowed;
    if (!allowed && fullRadio.checked && confirmRadio) confirmRadio.checked = true;
    if (fullNote) {
      if (!sandboxReady || agent !== "opencode") fullNote.textContent = T.full_off;
      else if (isAbliteratedName(m)) fullNote.textContent = T.full_abliterated;
      else if (!isLocalModel(m)) fullNote.textContent = T.full_local_only;
      else fullNote.textContent = T.full_help;
    }
    if (fullWarn) fullWarn.hidden = !(allowed && fullRadio.checked);
  }

  // ------------------------------------------------------------- choices
  var catalog = $("[data-catalog]");
  var groups = Array.from(modelSel.children);
  var buttons = new Map();
  function chooseCatalog(key, model) {
    var matching = groups.filter(function (g) { return g.dataset.catalogKey === key; });
    var active = model || modelSel.value;
    if (active) {
      matching.sort(function (a, b) {
        var aHas = Array.from(a.children).some(function (o) { return o.value === active; });
        var bHas = Array.from(b.children).some(function (o) { return o.value === active; });
        return (bHas ? 1 : 0) - (aHas ? 1 : 0);
      });
      matching.forEach(function (g) {
        var opt = Array.from(g.children).find(function (o) { return o.value === active; });
        if (opt && g.firstElementChild !== opt) g.insertBefore(opt, g.firstElementChild);
      });
    }
    modelSel.replaceChildren(...matching);
    if (model) modelSel.value = model;
    setEfforts();
    buttons.forEach(function (button, id) {
      button.setAttribute("aria-pressed", String(id === key));
      button.classList.toggle("ghost", id !== key);
    });
    showPayg();
    updateFullMode();
  }
  function restoreModel(model) {
    var group = groups.find(function (g) {
      return Array.from(g.children).some(function (option) { return option.value === model; });
    });
    if (group) chooseCatalog(group.dataset.catalogKey, model);
  }
  groups.forEach(function (group) {
    var key = group.dataset.catalogKey;
    if (buttons.has(key)) return;
    var button = el("button", "btn small", group.dataset.catalogLabel);
    button.type = "button";
    button.addEventListener("click", function () {
      if (button.getAttribute("aria-pressed") === "true") return;
      chooseCatalog(key);
      savePrefs();
    });
    catalog.appendChild(button);
    buttons.set(key, button);
  });
  catalog.hidden = buttons.size < 2;
  if (groups.length) chooseCatalog(groups[0].dataset.catalogKey);
  try {
    var prefs = JSON.parse(localStorage.getItem(PREFS) || "{}");
    restoreModel(prefs.model);
    [[effortSel, prefs.effort], [folderSel, prefs.folder]].forEach(function (p) {
      if (p[1] && p[0].querySelector('option[value="' + CSS.escape(p[1]) + '"]')) p[0].value = p[1];
    });
  } catch (e) {}
  if (boot.selected_folder) {
    var matchOpt = Array.from(folderSel.options).find(function (o) {
      return o.value.toLowerCase() === String(boot.selected_folder).toLowerCase();
    });
    if (matchOpt) folderSel.value = matchOpt.value;
  }
  function savePrefs() {
    try {
      localStorage.setItem(PREFS, JSON.stringify({ model: modelSel.value, effort: effortSel.value, folder: folderSel.value }));
    } catch (e) {}
  }
  function showPayg() {
    var o = modelSel.options[modelSel.selectedIndex];
    payg.hidden = !(o && o.hasAttribute("data-payg"));
  }
  var header = window.BatonChat.initHeader({
    root: root,
    csrf: csrf,
    labels: {
      newChat: T.new_chat,
      untitled: T.untitled,
      rename: T.rename,
      delConfirm: T.del_confirm
    },
    onAfterRename: function () { sidebar.refresh(threadId); },
    onAfterDelete: function () {
      clearView();
      history.replaceState(null, "", "/chat?agent=" + encodeURIComponent(agent));
      sidebar.refresh("");
    }
  });
  function syncHeaderContext(info) {
    var opt = folderSel && folderSel.options[folderSel.selectedIndex];
    if (info && info.folder) {
      var matched = Array.from((folderSel && folderSel.options) || []).find(function (o) {
        return o.value === info.folder || o.dataset.label === info.folder || o.textContent === info.folder;
      });
      var b = matched ? (matched.dataset.branch || "") : "";
      header.setContext(info.folder, b);
      return;
    }
    var label = (opt && (opt.dataset.label || opt.textContent)) || "";
    var branch = (opt && opt.dataset.branch) || "";
    header.setContext(label, branch);
  }
  var agentSel = $("[data-agent-switch]");
  var composer = null;
  function syncComposer() { if (composer) composer.sync(); }
  function catalogTabs() {
    var tabs = [];
    buttons.forEach(function (button, key) {
      tabs.push({
        label: button.textContent,
        active: button.getAttribute("aria-pressed") === "true",
        onPick: function () {
          chooseCatalog(key);
          savePrefs();
          syncComposer();
        }
      });
    });
    return tabs;
  }
  [modelSel, effortSel, folderSel].forEach(function (s) {
    s.addEventListener("change", function () {
      if (s === modelSel) setEfforts();
      savePrefs(); showPayg(); updateFullMode(); syncHeaderContext(); syncComposer();
    });
  });
  if (resumeSel) {
    resumeSel.addEventListener("change", function () {
      var o = resumeSel.options[resumeSel.selectedIndex];
      if (!o || !o.value) { syncComposer(); return; }
      if (o.dataset.folder && folderSel.querySelector('option[value="' + CSS.escape(o.dataset.folder) + '"]')) {
        folderSel.value = o.dataset.folder;
      }
      if (o.dataset.model && modelSel.querySelector('option[value="' + CSS.escape(o.dataset.model) + '"]')) {
        modelSel.value = o.dataset.model;
        setEfforts();
      }
      if (o.dataset.effort && effortSel.querySelector('option[value="' + CSS.escape(o.dataset.effort) + '"]')) {
        effortSel.value = o.dataset.effort;
      }
      showPayg(); updateFullMode(); syncHeaderContext(); syncComposer();
    });
  }
  root.querySelectorAll("[data-mode]").forEach(function (r) {
    r.addEventListener("change", function () { updateFullMode(); syncComposer(); });
  });
  function lock(info) {
    [modelSel, effortSel, folderSel, resumeSel].forEach(function (s) { if (s) s.disabled = !!info; });
    buttons.forEach(function (button) { button.disabled = !!info; });
    root.querySelectorAll("[data-mode]").forEach(function (r) {
      if (info) r.disabled = true;
      else if (r.value === "confirm") r.disabled = false;
    });
    root.classList.toggle("is-started", !!info);
    if (fixed) fixed.hidden = !info;
    if (info) {
      restoreModel(info.model);
      modelSel.value = info.model;
      setEfforts();
      effortSel.value = info.effort;
      if (info.mode === "full" && fullRadio) fullRadio.checked = true;
      else if (confirmRadio) confirmRadio.checked = true;
      var msg = T.fixed.replace("{model}", info.model).replace("{folder}", info.folder);
      if (fixedText) fixedText.textContent = msg; else if (fixed) fixed.textContent = msg;
      if (fullBadge) fullBadge.hidden = info.mode !== "full";
    } else {
      if (fullBadge) fullBadge.hidden = true;
      if (confirmRadio) confirmRadio.checked = true;
      if (resumeSel) resumeSel.value = "";
      if (!modelSel.value && groups.length) chooseCatalog(groups[0].dataset.catalogKey);
    }
    showPayg();
    updateFullMode();
    syncHeaderContext(info);
    syncComposer();
  }
  function setBusy(on) {
    busy = on;
    send.textContent = on ? T.stop : T.send;
    if (on) send.setAttribute("data-busy", ""); else send.removeAttribute("data-busy");
  }

  composer = window.BatonChat.initComposerControls({
    root: root,
    labels: { locked: T.locked, filterModels: T.filter_models },
    chips: [
      { id: "agent", title: T.lbl_agent, select: agentSel, label: function () { return selectedLabel(agentSel); }, isLocked: function () { return !!chat; } },
      { id: "model", title: T.lbl_model, select: modelSel, label: function () { return selectedLabel(modelSel); }, isLocked: function () { return !!chat; }, selectExtra: function () { return { catalogTabs: catalogTabs() }; } },
      { id: "effort", title: T.lbl_effort, select: effortSel, label: function () { return selectedLabel(effortSel); }, isHidden: function () { return effortSel.options.length <= 1; }, isLocked: function () { return !!chat; } },
      { id: "folder", title: T.lbl_folder, select: folderSel, label: function () { return shortFolder(folderSel); }, isLocked: function () { return !!chat; } },
      {
        id: "mode", title: T.lbl_mode, shortcut: "Shift+Tab",
        label: function () { return fullRadio && fullRadio.checked ? T.full_label : T.confirm_label; },
        tone: function () { return fullRadio && fullRadio.checked ? "bad" : ""; },
        isLocked: function () { return !!chat; },
        renderPopover: function (body, close) {
          var modes = [
            { val: "confirm", radio: confirmRadio, title: T.confirm_label, desc: T.confirm_help, danger: false },
            { val: "full", radio: fullRadio, title: T.full_label, desc: fullNote ? fullNote.textContent : T.full_off, danger: true }
          ];
          modes.forEach(function (item) {
            if (!item.radio) return;
            var active = item.radio.checked;
            var row = el("button", "cc-pop-mode" + (active ? " is-active" : "") + (item.danger ? " is-danger" : ""));
            row.type = "button";
            row.disabled = !!item.radio.disabled;
            var top = el("span", "cc-pop-mode-head");
            top.appendChild(el("span", "cc-pop-mode-title", item.title));
            if (active) top.appendChild(el("span", "cc-pop-check", "✓"));
            row.appendChild(top);
            if (item.desc) row.appendChild(el("span", "cc-pop-mode-desc", item.desc));
            row.addEventListener("click", function () {
              item.radio.checked = true;
              updateFullMode();
              syncComposer();
              close(false);
              if (!text.disabled) text.focus();
            });
            body.appendChild(row);
          });
        }
      },
      { id: "resume", title: T.lbl_resume, select: resumeSel, label: function () { return selectedLabel(resumeSel); }, isHidden: function () { return !resumeSel; }, isLocked: function () { return !!chat; } }
    ],
    isBusy: function () { return !!busy; },
    onCancel: function () {
      if (busy && chat) api("/api/agents/" + encodeURIComponent(chat.id) + "/cancel").catch(function () {});
    },
    onCycleMode: function (openChip) {
      if (chat) return;
      if (fullRadio && !fullRadio.disabled) {
        if (fullRadio.checked && confirmRadio) confirmRadio.checked = true;
        else fullRadio.checked = true;
        updateFullMode();
        syncComposer();
      } else {
        openChip("mode");
      }
    }
  });

  // ------------------------------------------------------------- history
  function clearView() {
    if (source) { source.close(); source = null; }
    if (chat) api("/api/agents/" + encodeURIComponent(chat.id) + "/close").catch(function () {});
    forget();
    threadId = "";
    header.setThread("", "");
    toolsView.clear();
    log.querySelectorAll(":scope > :not([data-empty])").forEach(function (n) { n.remove(); });
    empty.hidden = false;
    after = 0; current = null;
    attachments.clear();
    text.disabled = send.disabled = false;
    setBusy(false);
    lock(null);
    sidebar.setActive("");
  }

  function renderSavedThread(th) {
    clearView();
    threadId = th.id || "";
    header.setThread(threadId, th.title || "");
    sidebar.setActive(threadId);
    if (th.model) restoreModel(th.model);
    if (th.effort && effortSel.querySelector('option[value="' + CSS.escape(th.effort) + '"]')) {
      effortSel.value = th.effort;
    }
    if (th.folder_key && folderSel.querySelector('option[value="' + CSS.escape(th.folder_key) + '"]')) {
      folderSel.value = th.folder_key;
    }
    if (resumeSel && th.session_id && resumeSel.querySelector('option[value="' + CSS.escape(th.session_id) + '"]')) {
      resumeSel.value = th.session_id;
    }
    syncHeaderContext();
    syncComposer();
    (th.messages || []).forEach(function (m) {
      if (m.role === "user") {
        add(el("article", "msg msg-user")).appendChild(el("div", "msg-body", m.content || ""));
      } else {
        var b = createAssistantBubble(T);
        add(b.el);
        if (m.reasoning) {
          b.think.hidden = false;
          b.thinkText.textContent = m.reasoning;
        }
        b.body.innerHTML = md(m.content || "");
        if (m.content) b.attachCopy(function () { return m.content; });
      }
    });
    history.replaceState(null, "", "/chat?agent=" + encodeURIComponent(agent) + "&thread=" + encodeURIComponent(threadId));
    scroller.follow(true);
  }

  var sidebar = BC.initSidebar({
    csrf: csrf,
    agent: agent,
    activeId: (boot.active_thread && boot.active_thread.id) || "",
    initialThreads: boot.threads || [],
    labels: {
      empty: T.history_empty,
      untitled: T.untitled,
      rename: T.rename,
      del: T.del,
      delConfirm: T.del_confirm
    },
    onNew: function () {
      clearView();
      history.replaceState(null, "", "/chat?agent=" + encodeURIComponent(agent));
      text.focus();
    },
    onSelect: function (th) {
      fetch("/api/chat-threads/" + encodeURIComponent(th.id), { cache: "no-store" })
        .then(function (r) { return r.ok ? r.json() : null; })
        .then(function (full) { if (full) renderSavedThread(full); })
        .catch(function () {});
    },
    onRenamedActive: function (title) { header.setTitle(title); },
    onActiveMeta: function (th) { if (th && th.title) header.setTitle(th.title); },
    onDeletedActive: function () {
      clearView();
      history.replaceState(null, "", "/chat?agent=" + encodeURIComponent(agent));
    }
  });

  // ------------------------------------------------------------ drawing
  var statusTextEl = $("[data-status-text]");
  var statusMetaEl = $("[data-status-meta]");

  function setStatus(liveMsg) {
    if (statusTextEl) statusTextEl.textContent = liveMsg || T.kbd_hint || "";
    if (statusMetaEl) {
      var turnCount = log.querySelectorAll(".msg").length;
      statusMetaEl.textContent = turnCount ? (selectedLabel(modelSel) + " · " + turnCount) : selectedLabel(modelSel);
    }
  }
  function stopCurrentThink() {
    if (current && current.thinkTimer) current.thinkTimer.stop();
  }
  function assistant() {
    if (current) return current;
    toolsView.resetGroup();
    var b = createAssistantBubble(T);
    add(b.el);
    current = { el: b.el, bubble: b, text: "", thought: "", queued: false, thinkTimer: b.thinkTimer };
    return current;
  }
  function paint(m) {
    if (m.queued) return;
    m.queued = true;
    requestAnimationFrame(function () {
      m.queued = false;
      if (m.thought) { m.bubble.think.hidden = false; m.bubble.thinkText.textContent = m.thought; }
      m.bubble.body.innerHTML = md(m.text);
      if (m.text) m.bubble.attachCopy(function () { return m.text; });
      scroller.follow(false);
    });
  }
  function handle(ev) {
    after = ev.seq + 1;
    if (ev.type === "user") {
      stopCurrentThink(); current = null; toolsView.resetGroup(); refused = false;
      add(el("article", "msg msg-user")).appendChild(el("div", "msg-body", ev.text));
      setStatus(T.thinking); scroller.follow(true); return;
    } else if (ev.type === "text") {
      var m = assistant(); m.thinkTimer.stop(); m.text += ev.text; paint(m); return;
    } else if (ev.type === "thought") {
      var t = assistant(); t.thinkTimer.start(); t.thought += ev.text; setStatus(T.thinking); paint(t); return;
    } else if (ev.type === "tool") {
      stopCurrentThink();
      toolsView.onTool(ev);
      var st = ev.status || "pending";
      if (st === "in_progress" || st === "pending") setStatus((ev.kind || "tool") + ": " + (ev.title || ev.name || ""));
    } else if (ev.type === "permission") {
      stopCurrentThink();
      toolsView.onPermission(ev, function (allow) {
        return api("/api/agents/" + encodeURIComponent(chat.id) + "/permission", { id: ev.id, allow: allow });
      });
      setStatus(T.perm_title + " · " + (T.perm_keys || "1 / 2"));
    } else if (ev.type === "permission_done") {
      toolsView.settlePerm(ev.id, ev.outcome);
      refused = refused || ev.outcome !== "allowed";
    } else if (ev.type === "plan") {
      stopCurrentThink();
      current = null;
      note("muted small", T.plan + ": " + ev.entries.map(function (e) { return (e.status === "completed" ? "☑ " : "☐ ") + e.content; }).join(" · "));
    } else if (ev.type === "notice") {
      note("muted small", (T[ev.key] || ev.key).replace("{effort}", ev.effort || "").replace("{session}", ev.session || ""));
    } else if (ev.type === "error") {
      stopCurrentThink(); current = null; note("warn", T.error + " " + ev.text);
    } else if (ev.type === "done") {
      stopCurrentThink();
      current = null;
      toolsView.resetGroup();
      if (ev.reason !== "end_turn" && ev.reason !== "completed" && !(ev.reason === "cancelled" && refused)) {
        note("muted small", ev.reason === "cancelled" ? T.stopped : T.done_reason.replace("{reason}", ev.reason));
      }
      setStatus("");
      sidebar.refresh(threadId);
    } else if (ev.type === "closed") {
      stopCurrentThink(); closed();
    }
    scroller.follow(false);
  }
  function closed() {
    note("warn", T.closed);
    setBusy(false);
    setStatus("");
    text.disabled = true;
    send.disabled = true;
  }

  // -------------------------------------------------------------- stream
  function listen() {
    if (source || !chat) return;
    source = new EventSource("/api/agents/" + encodeURIComponent(chat.id) + "/events?after=" + after);
    source.onmessage = function (e) { try { handle(JSON.parse(e.data)); } catch (err) {} };
    source.addEventListener("idle", function () {
      source.close(); source = null;
      stopCurrentThink();
      setBusy(false);
      setStatus("");
      sidebar.refresh(threadId);
    });
    source.onerror = function () {
      fetch("/api/agents/" + encodeURIComponent(chat.id), { cache: "no-store" }).then(function (r) {
        if (r.status === 404 && source) { source.close(); source = null; forget(); closed(); }
      }).catch(function () {});
    };
  }
  function forget() {
    chat = null;
    try { localStorage.removeItem(STORE); } catch (e) {}
  }

  // ---------------------------------------------------------------- send
  async function ask() {
    var content = text.value.trim();
    var imgs = attachments.list();
    if ((!content && !imgs.length) || busy) return;
    setBusy(true);
    try {
      if (!chat) {
        var mode = root.querySelector("[data-mode]:checked");
        note("muted small ag-starting", T.starting);
        chat = await api("/api/agents", {
          agent: agent, model: modelSel.value, effort: effortSel.value,
          folder: folderSel.value, mode: mode ? mode.value : "confirm",
          resume: resumeSel ? resumeSel.value : "",
          thread_id: threadId || ""
        });
        var s = log.querySelector(".ag-starting"); if (s) s.remove();
        if (chat.thread_id) {
          threadId = chat.thread_id;
          header.setThread(threadId, undefined);
          history.replaceState(null, "", "/chat?agent=" + encodeURIComponent(agent) + "&thread=" + encodeURIComponent(threadId));
        }
        try { localStorage.setItem(STORE, JSON.stringify({ id: chat.id, thread_id: threadId })); } catch (e) {}
        lock(chat);
      }
      await api("/api/agents/" + encodeURIComponent(chat.id) + "/prompt", { text: content, images: imgs });
      text.value = "";
      attachments.clear();
      autosize();
      sidebar.refresh(threadId);
      listen();
    } catch (e) {
      var s2 = log.querySelector(".ag-starting"); if (s2) s2.remove();
      note("warn", T.error + " " + e.message);
      if (e.status === 404) { forget(); lock(null); }
      setBusy(false);
    }
  }

  // -------------------------------------------------------------- events
  var autosize = BC.bindAutosize(text);
  text.addEventListener("keydown", function (e) {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); ask(); }
  });
  form.addEventListener("submit", function (e) {
    e.preventDefault();
    if (busy && chat) api("/api/agents/" + encodeURIComponent(chat.id) + "/cancel").catch(function () {});
    else ask();
  });
  root.querySelectorAll("[data-new]").forEach(function (btn) {
    btn.addEventListener("click", function () {
      clearView();
      history.replaceState(null, "", "/chat?agent=" + encodeURIComponent(agent));
      text.focus();
    });
  });

  showPayg();
  syncHeaderContext();
  setStatus("");
  if (boot.active_thread && boot.active_thread.agent === agent) {
    renderSavedThread(boot.active_thread);
  } else {
    var saved = null;
    try { saved = JSON.parse(localStorage.getItem(STORE) || "null"); } catch (e) {}
    if (saved && saved.id) {
      fetch("/api/agents/" + encodeURIComponent(saved.id), { cache: "no-store" })
        .then(function (r) { return r.ok ? r.json() : null; })
        .then(function (info) {
          if (!info) { forget(); return; }
          chat = info;
          if (info.thread_id) {
            threadId = info.thread_id;
            header.setThread(threadId, undefined);
            sidebar.refresh(threadId);
          }
          lock(info);
          setBusy(info.busy);
          listen();
        }).catch(function () {});
    }
  }
  document.addEventListener("visibilitychange", function () {
    if (!document.hidden && chat && !source) listen();
  });
})();
