// Agents chat (OpenCode over ACP). Baton keeps the conversation on the server:
// this page sends messages and permission answers (always with the CSRF
// token) and replays the conversation's events (server-sent events), so a
// reload or a phone that went to sleep picks up where it was.
(function () {
  var root = document.querySelector("[data-agents]");
  if (!root || !root.querySelector("[data-form]")) return;
  var T = JSON.parse(document.getElementById("agents-strings").textContent);
  var md = window.BatonChat.md;
  var csrf = root.dataset.csrf;
  function $(s) { return root.querySelector(s); }
  var log = $("[data-log]"), empty = $("[data-empty]"), form = $("[data-form]"), text = $("[data-text]");
  var send = $("[data-send]"), modelSel = $("[data-oc-model]"), effortSel = $("[data-oc-effort]");
  var folderSel = $("[data-oc-folder]"), payg = $("[data-payg-warn]"), fixed = $("[data-fixed]");
  var STORE = "baton-agents-chat", PREFS = "baton-agents-prefs";
  var chat = null, after = 0, source = null, busy = false, current = null, tools = {}, perms = {};
  var refused = false;   // a request was denied in this turn: OpenCode then ends it as "cancelled"

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
  function scrollDown() { window.scrollTo(0, document.body.scrollHeight); }
  function add(node) { log.appendChild(node); empty.hidden = true; return node; }
  function note(cls, msg) { add(el("p", "ag-note " + cls, msg)); }

  // ------------------------------------------------------------- choices
  var catalog = $("[data-catalog]");
  var groups = Array.from(modelSel.children);
  var buttons = new Map();
  function chooseCatalog(key, model) {
    modelSel.replaceChildren(...groups.filter(function (g) { return g.dataset.catalogKey === key; }));
    if (model) modelSel.value = model;
    buttons.forEach(function (button, id) {
      button.setAttribute("aria-pressed", String(id === key));
      button.classList.toggle("ghost", id !== key);
    });
    showPayg();
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
    s.addEventListener("change", function () { savePrefs(); showPayg(); });
  });
  function lock(info) {
    // Model, effort, folder and mode are fixed on the server for the conversation.
    [modelSel, effortSel, folderSel].forEach(function (s) { s.disabled = !!info; });
    buttons.forEach(function (button) { button.disabled = !!info; });
    root.querySelectorAll("[data-mode]").forEach(function (r) { if (r.value === "confirm") r.disabled = !!info; });
    fixed.hidden = !info;
    if (info) {
      restoreModel(info.model);
      // A resumed session can use a model that has since left the catalog.
      modelSel.value = info.model;
      effortSel.value = info.effort;
      fixed.textContent = T.fixed.replace("{model}", info.model).replace("{folder}", info.folder);
    } else if (!modelSel.value && groups.length) {
      chooseCatalog(groups[0].dataset.catalogKey);
    }
    showPayg();
  }
  function setBusy(on) {
    busy = on;
    send.textContent = on ? T.stop : T.send;
    if (on) send.setAttribute("data-busy", ""); else send.removeAttribute("data-busy");
  }

  // ------------------------------------------------------------ drawing
  function assistant() {
    if (current) return current;
    var a = add(el("article", "msg msg-assistant"));
    a.innerHTML = '<details class="msg-think" hidden><summary></summary><div class="msg-think-text"></div></details>' +
                  '<div class="msg-body"></div>';
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
      scrollDown();
    });
  }
  function block(label, body, cls) {
    var d = el("div", "ag-block");
    d.appendChild(el("span", "label", label));
    d.appendChild(el("pre", cls || "", body));
    return d;
  }
  // A line diff (longest common subsequence) for small files; big ones show
  // the old and new text whole.
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
  function tool(ev) {
    var card = tools[ev.id];
    if (!card) {
      card = tools[ev.id] = add(el("details", "ag-tool"));
      current = null;      // text after a tool goes in a new bubble
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
  }
  function permission(ev) {
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
    if (ev.type === "user") { current = null; refused = false; add(el("article", "msg msg-user")).appendChild(el("div", "msg-body", ev.text)); }
    else if (ev.type === "text") { var m = assistant(); m.text += ev.text; paint(m); return; }
    else if (ev.type === "thought") { var t = assistant(); t.thought += ev.text; paint(t); return; }
    else if (ev.type === "tool") tool(ev);
    else if (ev.type === "permission") permission(ev);
    else if (ev.type === "permission_done") { settle(ev.id, ev.outcome); refused = refused || ev.outcome !== "allowed"; }
    else if (ev.type === "plan") {
      current = null;
      note("muted small", T.plan + ": " + ev.entries.map(function (e) { return (e.status === "completed" ? "☑ " : "☐ ") + e.content; }).join(" · "));
    }
    else if (ev.type === "notice") note("muted small", (T[ev.key] || ev.key).replace("{effort}", ev.effort || ""));
    else if (ev.type === "error") { current = null; note("warn", T.error + " " + ev.text); }
    else if (ev.type === "done") {
      current = null;
      if (ev.reason !== "end_turn" && !(ev.reason === "cancelled" && refused)) note("muted small", ev.reason === "cancelled" ? T.stopped : T.done_reason.replace("{reason}", ev.reason));
    }
    else if (ev.type === "closed") { closed(); }
    scrollDown();
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
    });
    source.onerror = function () {
      // The browser reconnects by itself (Last-Event-ID); if the conversation
      // is gone (Baton restarted), say so instead of retrying forever.
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
        chat = await api("/api/agents", { agent: "opencode", model: modelSel.value, effort: effortSel.value,
                                          folder: folderSel.value, mode: mode ? mode.value : "confirm" });
        var s = log.querySelector(".ag-starting"); if (s) s.remove();
        try { localStorage.setItem(STORE, JSON.stringify({ id: chat.id })); } catch (e) {}
        lock(chat);
      }
      await api("/api/agents/" + encodeURIComponent(chat.id) + "/prompt", { text: content });
      text.value = "";
      autosize();
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
  $("[data-new]").addEventListener("click", function () {
    if (source) { source.close(); source = null; }
    if (chat) api("/api/agents/" + encodeURIComponent(chat.id) + "/close").catch(function () {});
    forget();
    log.querySelectorAll(":scope > :not([data-empty])").forEach(function (n) { n.remove(); });
    empty.hidden = false;
    after = 0; current = null; tools = {}; perms = {};
    text.disabled = send.disabled = false;
    setBusy(false);
    lock(null);
    text.focus();
  });

  // A conversation still open on the server (reload, phone back from sleep).
  showPayg();
  var saved = null;
  try { saved = JSON.parse(localStorage.getItem(STORE) || "null"); } catch (e) {}
  if (saved && saved.id) {
    fetch("/api/agents/" + encodeURIComponent(saved.id), { cache: "no-store" })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (info) {
        if (!info) { forget(); return; }
        chat = info;
        lock(info);
        setBusy(info.busy);
        listen();
      }).catch(function () {});
  }
  document.addEventListener("visibilitychange", function () {
    if (!document.hidden && chat && !source) listen();   // catch up after sleep
  });
})();
