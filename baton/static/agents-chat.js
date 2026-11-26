// Agents chat (OpenCode, Gemini, Claude Code, Codex). Baton keeps the active
// process on the server, persists turns into SQLite (chat_threads/chat_messages)
// for cross-device history, and replays events over SSE.
(function () {
  var root = document.querySelector("[data-agents]");
  if (!root || !root.querySelector("[data-form]")) return;
  var T = JSON.parse(document.getElementById("agents-strings").textContent);
  var bootEl = document.getElementById("chat-bootstrap");
  var boot = bootEl ? JSON.parse(bootEl.textContent) : {};
  var md = window.BatonChat.md;
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
  var scroller = window.BatonChat.createScroller(scrollBtn);
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
  var chat = null, threadId = "", after = 0, source = null, busy = false, current = null, tools = {}, perms = {};
  var refused = false;

  // ------------------------------------------------------------ helpers
  function el(tag, cls, txt) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (txt != null) e.textContent = txt;
    return e;
  }
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
  function note(cls, msg) { add(el("p", "ag-note " + cls, msg)); }

  function attachCopy(article, getText) {
    var meta = article.querySelector(".msg-meta");
    if (!meta) {
      meta = el("p", "msg-meta muted small");
      article.appendChild(meta);
    }
    if (meta.querySelector("[data-msg-copy]")) return;
    var copy = el("button", "", T.copy || "Copy");
    copy.type = "button";
    copy.setAttribute("data-msg-copy", "");
    copy.addEventListener("click", function () {
      try {
        navigator.clipboard.writeText(getText() || "").then(function () {
          copy.textContent = T.copied || "Copied";
          setTimeout(function () { copy.textContent = T.copy || "Copy"; }, 1400);
        });
      } catch (e) {}
    });
    meta.appendChild(copy);
  }

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
  [modelSel, effortSel, folderSel].forEach(function (s) {
    s.addEventListener("change", function () {
      if (s === modelSel) setEfforts();
      savePrefs(); showPayg(); updateFullMode();
    });
  });
  if (resumeSel) {
    resumeSel.addEventListener("change", function () {
      var o = resumeSel.options[resumeSel.selectedIndex];
      if (!o || !o.value) return;
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
      showPayg(); updateFullMode();
    });
  }
  root.querySelectorAll("[data-mode]").forEach(function (r) {
    r.addEventListener("change", updateFullMode);
  });
  function lock(info) {
    [modelSel, effortSel, folderSel, resumeSel].forEach(function (s) { if (s) s.disabled = !!info; });
    buttons.forEach(function (button) { button.disabled = !!info; });
    root.querySelectorAll("[data-mode]").forEach(function (r) {
      if (info) r.disabled = true;
      else if (r.value === "confirm") r.disabled = false;
    });
    root.classList.toggle("is-started", !!info);
    fixed.hidden = !info;
    if (info) {
      restoreModel(info.model);
      modelSel.value = info.model;
      setEfforts();
      effortSel.value = info.effort;
      if (info.mode === "full" && fullRadio) fullRadio.checked = true;
      else if (confirmRadio) confirmRadio.checked = true;
      var msg = T.fixed.replace("{model}", info.model).replace("{folder}", info.folder);
      if (fixedText) fixedText.textContent = msg; else fixed.textContent = msg;
      if (fullBadge) fullBadge.hidden = info.mode !== "full";
    } else {
      if (fullBadge) fullBadge.hidden = true;
      if (confirmRadio) confirmRadio.checked = true;
      if (resumeSel) resumeSel.value = "";
      if (!modelSel.value && groups.length) chooseCatalog(groups[0].dataset.catalogKey);
    }
    showPayg();
    updateFullMode();
  }
  function setBusy(on) {
    busy = on;
    send.textContent = on ? T.stop : T.send;
    if (on) send.setAttribute("data-busy", ""); else send.removeAttribute("data-busy");
  }

  // ------------------------------------------------------------- history
  var toTaskLinks = root.querySelectorAll("[data-to-task]");
  function syncToTask() {
    toTaskLinks.forEach(function (a) {
      a.hidden = !threadId;
      if (threadId) a.href = "/tasks/new?from_thread=" + encodeURIComponent(threadId);
    });
  }
  var activeGroup = null;

  function clearView() {
    if (source) { source.close(); source = null; }
    if (chat) api("/api/agents/" + encodeURIComponent(chat.id) + "/close").catch(function () {});
    forget();
    threadId = "";
    syncToTask();
    log.querySelectorAll(":scope > :not([data-empty])").forEach(function (n) { n.remove(); });
    empty.hidden = false;
    after = 0; current = null; activeGroup = null; tools = {}; perms = {};
    text.disabled = send.disabled = false;
    setBusy(false);
    lock(null);
    sidebar.setActive("");
  }

  function renderSavedThread(th) {
    clearView();
    threadId = th.id || "";
    syncToTask();
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
    (th.messages || []).forEach(function (m) {
      if (m.role === "user") {
        add(el("article", "msg msg-user")).appendChild(el("div", "msg-body", m.content || ""));
      } else {
        var a = add(el("article", "msg msg-assistant"));
        a.innerHTML = '<details class="msg-think" hidden><summary></summary><div class="msg-think-text"></div></details>' +
                      '<div class="msg-body"></div><p class="msg-meta muted small"></p>';
        a.querySelector("summary").textContent = T.reasoning;
        if (m.reasoning) {
          var tk = a.querySelector(".msg-think");
          tk.hidden = false;
          tk.querySelector(".msg-think-text").textContent = m.reasoning;
        }
        a.querySelector(".msg-body").innerHTML = md(m.content || "");
        if (m.content) attachCopy(a, function () { return m.content; });
      }
    });
    history.replaceState(null, "", "/chat?agent=" + encodeURIComponent(agent) + "&thread=" + encodeURIComponent(threadId));
    scroller.follow(true);
  }

  var sidebar = window.BatonChat.initSidebar({
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
    onDeletedActive: function () {
      clearView();
      history.replaceState(null, "", "/chat?agent=" + encodeURIComponent(agent));
    }
  });

  // ------------------------------------------------------------ drawing
  function assistant() {
    if (current) return current;
    activeGroup = null;
    var a = add(el("article", "msg msg-assistant"));
    a.innerHTML = '<details class="msg-think" hidden><summary></summary><div class="msg-think-text"></div></details>' +
                  '<div class="msg-body"></div><p class="msg-meta muted small"></p>';
    a.querySelector("summary").textContent = T.reasoning;
    current = { el: a, text: "", thought: "", queued: false };
    return current;
  }
  function paint(m) {
    if (m.queued) return;
    m.queued = true;
    requestAnimationFrame(function () {
      m.queued = false;
      var think = m.el.querySelector(".msg-think");
      if (m.thought) { think.hidden = false; think.querySelector(".msg-think-text").textContent = m.thought; }
      m.el.querySelector(".msg-body").innerHTML = md(m.text);
      if (m.text) attachCopy(m.el, function () { return m.text; });
      scroller.follow(false);
    });
  }
  function block(label, body, cls) {
    var d = el("div", "ag-block");
    d.appendChild(el("span", "label", label));
    d.appendChild(el("pre", cls || "", body));
    return d;
  }
  function diff(oldText, newText) {
    var a = (oldText || "").split("\n"), b = (newText || "").split("\n"), out = [];
    if (oldText == null || oldText === "") return b.map(function (l) { return ["+", l]; });
    if (a.length * b.length > 250000) {
      return a.map(function (l) { return ["-", l]; }).concat(b.map(function (l) { return ["+", l]; }));
    }
    var n = a.length, m = b.length, L = [];
    for (var i = 0; i <= n; i++) { L.push(new Uint32Array(m + 1)); }
    for (i = n - 1; i >= 0; i--) for (var j = m - 1; j >= 0; j--) {
      L[i][j] = a[i] === b[j] ? L[i + 1][j + 1] + 1 : Math.max(L[i + 1][j], L[i][j + 1]);
    }
    i = 0; j = 0;
    while (i < n && j < m) {
      if (a[i] === b[j]) { out.push([" ", a[i]]); i++; j++; }
      else if (L[i + 1][j] >= L[i][j + 1]) { out.push(["-", a[i++]]); }
      else { out.push(["+", b[j++]]); }
    }
    while (i < n) out.push(["-", a[i++]]);
    while (j < m) out.push(["+", b[j++]]);
    return out;
  }
  function diffBlock(d) {
    var wrap = el("div", "ag-block");
    wrap.appendChild(el("span", "ag-path", d.path));
    var pre = el("pre", "ag-diff");
    diff(d.old, d.new).slice(0, 2000).forEach(function (row) {
      var line = el("span", row[0] === "+" ? "add" : row[0] === "-" ? "del" : "", row[0] + " " + row[1] + "\n");
      pre.appendChild(line);
    });
    wrap.appendChild(pre);
    return wrap;
  }
  function readToolCategory(ev) {
    if ((ev.diffs || []).length) return null;
    var k = String(ev.kind || "").toLowerCase();
    var n = String(ev.name || ev.title || "").toLowerCase();
    if (k === "execute" || k === "edit" || k === "write" || k === "delete") return null;
    if (k === "search" || k === "glob" || k === "grep" || /\b(grep|glob|search|find)\b/.test(n)) return "search";
    if (k === "read" || k === "fetch" || k === "list" || /\b(read|view_file|ls|list)\b/.test(n)) return "read";
    return null;
  }
  function updateGroupSummary(grp) {
    var reads = 0, searches = 0, running = 0, failed = 0;
    grp.items.forEach(function (it) {
      if (it.cat === "search") searches++;
      else reads++;
      if (it.status === "pending" || it.status === "in_progress") running++;
      else if (it.status === "failed") failed++;
    });
    var parts = [];
    if (reads) parts.push((T.grp_read || "{n} read").replace("{n}", String(reads)));
    if (searches) parts.push((T.grp_search || "{n} search").replace("{n}", String(searches)));
    if (!parts.length) parts.push((T.grp_steps || "{n} steps").replace("{n}", String(grp.items.length)));
    grp.titleEl.textContent = parts.join(" · ");
    var st = running ? "in_progress" : (failed ? "failed" : "completed");
    grp.statusEl.className = "ag-status ag-" + st;
    grp.statusEl.textContent = T["st_" + st] || st;
  }
  function ensureReadGroup() {
    if (activeGroup) return activeGroup;
    var box = add(el("details", "ag-tool-group"));
    var sum = el("summary");
    var titleEl = el("span", "ag-group-title", "");
    var statusEl = el("span", "ag-status", "");
    sum.appendChild(titleEl);
    sum.appendChild(statusEl);
    var list = el("div", "ag-tool-group-list");
    box.appendChild(sum);
    box.appendChild(list);
    activeGroup = { el: box, list: list, titleEl: titleEl, statusEl: statusEl, items: new Map() };
    return activeGroup;
  }
  function tool(ev) {
    var cat = readToolCategory(ev);
    var card = tools[ev.id];
    var grp = card && card._group ? card._group : null;
    if (!card) {
      current = null;
      if (cat) {
        grp = ensureReadGroup();
        card = tools[ev.id] = el("details", "ag-tool");
        card._group = grp;
        grp.list.appendChild(card);
        empty.hidden = true;
      } else {
        activeGroup = null;
        card = tools[ev.id] = add(el("details", "ag-tool"));
      }
    } else if (!cat && grp) {
      // Tool gained a diff/edit payload on update: promote out of read-only group.
      grp.items.delete(ev.id);
      if (grp.items.size === 0) grp.el.remove();
      else updateGroupSummary(grp);
      card._group = null;
      grp = null;
      activeGroup = null;
      log.appendChild(card);
    }
    card.innerHTML = "";
    var sum = el("summary");
    sum.appendChild(el("span", "ag-kind", ev.kind || "tool"));
    sum.appendChild(el("span", "ag-title", ev.title || ev.name || ev.id));
    sum.appendChild(el("span", "ag-status ag-" + (ev.status || "pending"), T["st_" + ev.status] || ev.status || ""));
    card.appendChild(sum);
    if (ev.name) card.appendChild(block("tool", ev.name));
    if (ev.input) card.appendChild(block(T.input, ev.input));
    (ev.diffs || []).forEach(function (d) { card.appendChild(diffBlock(d)); });
    if (ev.output) card.appendChild(block(T.output, ev.output));
    if (grp) {
      grp.items.set(ev.id, { cat: cat || "read", status: ev.status || "pending" });
      updateGroupSummary(grp);
    }
  }
  function permission(ev) {
    activeGroup = null;
    var box = perms[ev.id] = add(el("div", "ag-perm"));
    current = null;
    box.appendChild(el("strong", "", T.perm_title + ": " + (ev.title || ev.kind)));
    if (ev.command) box.appendChild(block(T.command, ev.command, "ag-cmd"));
    (ev.diffs || []).forEach(function (d) { box.appendChild(diffBlock(d)); });
    if (!ev.command && !(ev.diffs || []).length && ev.input) box.appendChild(block(T.input, ev.input));
    if ((ev.locations || []).length) box.appendChild(el("p", "muted small", ev.locations.join("\n")));
    var row = el("div", "ag-perm-actions");
    var yes = el("button", "btn small", T.allow), no = el("button", "btn small danger", T.deny);
    yes.type = no.type = "button";
    yes.disabled = !ev.can_allow;
    [[yes, true], [no, false]].forEach(function (b) {
      b[0].addEventListener("click", function () {
        yes.disabled = no.disabled = true;
        api("/api/agents/" + encodeURIComponent(chat.id) + "/permission", { id: ev.id, allow: b[1] })
          .catch(function (e) { settle(ev.id, e.status === 404 ? "expired" : "error", e.message); });
      });
    });
    row.appendChild(yes); row.appendChild(no);
    var when = new Date(ev.expires * 1000).toLocaleTimeString(T.lang, { hour: "2-digit", minute: "2-digit" });
    row.appendChild(el("span", "muted small", T.expires.replace("{time}", when)));
    box.appendChild(row);
  }
  function settle(id, outcome, detail) {
    var box = perms[id];
    if (!box) return;
    var row = box.querySelector(".ag-perm-actions");
    if (row) row.remove();
    box.classList.add("ag-perm-" + outcome);
    var label = { allowed: T.allowed, denied: T.denied, timeout: T.timeout, cancelled: T.cancelled,
                  expired: T.expired }[outcome] || (T.error + " " + (detail || ""));
    if (!box.querySelector(".ag-outcome")) box.appendChild(el("p", "ag-outcome", label));
  }
  function handle(ev) {
    after = ev.seq + 1;
    if (ev.type === "user") { current = null; activeGroup = null; refused = false; add(el("article", "msg msg-user")).appendChild(el("div", "msg-body", ev.text)); scroller.follow(true); return; }
    else if (ev.type === "text") { var m = assistant(); m.text += ev.text; paint(m); return; }
    else if (ev.type === "thought") { var t = assistant(); t.thought += ev.text; paint(t); return; }
    else if (ev.type === "tool") tool(ev);
    else if (ev.type === "permission") permission(ev);
    else if (ev.type === "permission_done") { settle(ev.id, ev.outcome); refused = refused || ev.outcome !== "allowed"; }
    else if (ev.type === "plan") {
      current = null;
      activeGroup = null;
      note("muted small", T.plan + ": " + ev.entries.map(function (e) { return (e.status === "completed" ? "☑ " : "☐ ") + e.content; }).join(" · "));
    }
    else if (ev.type === "notice") {
      note("muted small", (T[ev.key] || ev.key).replace("{effort}", ev.effort || "").replace("{session}", ev.session || ""));
    }
    else if (ev.type === "error") { current = null; activeGroup = null; note("warn", T.error + " " + ev.text); }
    else if (ev.type === "done") {
      current = null;
      activeGroup = null;
      if (ev.reason !== "end_turn" && ev.reason !== "completed" && !(ev.reason === "cancelled" && refused)) {
        note("muted small", ev.reason === "cancelled" ? T.stopped : T.done_reason.replace("{reason}", ev.reason));
      }
      sidebar.refresh(threadId);
    }
    else if (ev.type === "closed") { closed(); }
    scroller.follow(false);
  }
  function closed() {
    note("warn", T.closed);
    setBusy(false);
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
      setBusy(false);
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
    if (!content || busy) return;
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
          syncToTask();
          history.replaceState(null, "", "/chat?agent=" + encodeURIComponent(agent) + "&thread=" + encodeURIComponent(threadId));
        }
        try { localStorage.setItem(STORE, JSON.stringify({ id: chat.id, thread_id: threadId })); } catch (e) {}
        lock(chat);
      }
      await api("/api/agents/" + encodeURIComponent(chat.id) + "/prompt", { text: content });
      text.value = "";
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
  function autosize() { text.style.height = "auto"; text.style.height = Math.min(text.scrollHeight, 192) + "px"; }
  text.addEventListener("input", autosize);
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
            syncToTask();
            sidebar.setActive(threadId);
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
