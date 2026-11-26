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
  var messages = [], threadId = "", busy = null;

  var md = window.BatonChat.md;
  var scroller = window.BatonChat.createScroller(scrollBtn);

  try { var eff = localStorage.getItem("baton-chat-effort"); if (eff && effortSel.querySelector('option[value="' + CSS.escape(eff) + '"]')) effortSel.value = eff; } catch (e) {}

  function api(path, method, body) {
    return fetch(path, {
      method: method || "POST",
      headers: { "Content-Type": "application/json", "X-Baton-CSRF": csrf },
      body: body != null ? JSON.stringify(body) : undefined
    }).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (j) {
        if (!r.ok) throw new Error(j.detail || "HTTP " + r.status);
        return j;
      });
    });
  }

  function persistThread() {
    if (!messages.length) return Promise.resolve();
    var payload = {
      replace: messages.map(function (m) {
        return { role: m.role, content: m.content || "", reasoning: m.reasoning || "", meta: m.meta || "" };
      }),
      model: modelSel.value,
      effort: effortSel.value
    };
    if (threadId) {
      return api("/api/chat-threads/" + encodeURIComponent(threadId) + "/messages", "POST", payload)
        .then(function () { sidebar.refresh(threadId); })
        .catch(function () {});
    }
    return api("/api/chat-threads", "POST", {
      agent: "local", model: modelSel.value, effort: effortSel.value
    }).then(function (th) {
      threadId = th.id;
      history.replaceState(null, "", "/chat?agent=local&thread=" + encodeURIComponent(threadId));
      return api("/api/chat-threads/" + encodeURIComponent(threadId) + "/messages", "POST", payload);
    }).then(function () {
      sidebar.refresh(threadId);
    }).catch(function () {});
  }

  function loadThread(id) {
    if (!id) return;
    if (busy) busy.abort();
    fetch("/api/chat-threads/" + encodeURIComponent(id), { cache: "no-store" })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (th) {
        if (!th) return;
        applyThread(th);
      }).catch(function () {});
  }

  function applyThread(th) {
    threadId = th.id || "";
    if (th.model && modelSel.querySelector('option[value="' + CSS.escape(th.model) + '"]')) {
      modelSel.value = th.model;
    }
    if (th.effort && effortSel.querySelector('option[value="' + CSS.escape(th.effort) + '"]')) {
      effortSel.value = th.effort;
    }
    messages = (th.messages || []).map(function (m) {
      return { role: m.role, content: m.content || "", reasoning: m.reasoning || "", meta: m.meta || "" };
    });
    sidebar.setActive(threadId);
    renderAll();
    showState();
    history.replaceState(null, "", "/chat?agent=local&thread=" + encodeURIComponent(threadId));
    scroller.follow(true);
  }

  function startNew() {
    if (busy) busy.abort();
    threadId = "";
    messages = [];
    sidebar.setActive("");
    renderAll();
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
    onDeletedActive: startNew
  });

  // ----------------------------------------------------------- messages
  function bubble(role, idx) {
    var el = document.createElement("article");
    el.className = "msg msg-" + role;
    if (role === "assistant") {
      el.innerHTML = '<details class="msg-think" hidden><summary></summary><div class="msg-think-text"></div></details>' +
                     '<div class="msg-body"></div><p class="msg-meta muted small"><span></span></p>';
      el.querySelector("summary").textContent = T.reasoning;
    } else {
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
      renderAll();
      persistThread();
      autosize();
      text.focus();
    });
    meta.appendChild(edit);
  }

  function fill(el, m, isLastAssistant) {
    var think = el.querySelector(".msg-think");
    if (m.reasoning) {
      think.hidden = false;
      think.querySelector(".msg-think-text").textContent = m.reasoning;
    }
    el.querySelector(".msg-body").innerHTML = md(m.content || "");
    var meta = el.querySelector(".msg-meta");
    meta.querySelector("span").textContent = m.meta || "";
    meta.querySelectorAll("button").forEach(function (b) { b.remove(); });
    var streaming = m.meta === T.thinking || m.meta === T.writing;
    if (!streaming && m.content) {
      var copy = document.createElement("button");
      copy.type = "button";
      copy.textContent = T.copy;
      copy.addEventListener("click", function () {
        try {
          navigator.clipboard.writeText(m.content).then(function () {
            copy.textContent = T.copied;
            setTimeout(function () { copy.textContent = T.copy; }, 1500);
          });
        } catch (e) {}
      });
      meta.appendChild(copy);
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
      meta.appendChild(regen);
    }
  }

  function renderAll() {
    log.querySelectorAll(".msg").forEach(function (el) { el.remove(); });
    messages.forEach(function (m, idx) {
      var el = bubble(m.role, idx);
      if (m.role === "assistant") fill(el, m, idx === messages.length - 1);
      else el.querySelector(".msg-body").textContent = m.content;
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
            o.textContent = (m.uses_gpu ? "● " : "") + (o.dataset.name || m.name);
          }
        });
        showState();
      })
      .catch(function () {});
  }
  function setBusy(on) {
    send.textContent = on ? T.stop : T.send;
    if (on) send.setAttribute("data-busy", ""); else send.removeAttribute("data-busy");
    modelSel.disabled = on;
    showState();
  }

  // --------------------------------------------------------------- send
  async function streamReply(restoreOnFail) {
    var m = { role: "assistant", content: "", reasoning: "", meta: T.thinking };
    var el = bubble("assistant");
    fill(el, m, false);
    scroller.follow(true);
    busy = new AbortController();
    setBusy(true);
    var timings = null, failed = false, queued = false, done = false;
    try {
      var r = await fetch("/api/chat/" + encodeURIComponent(modelSel.value), {
        method: "POST", signal: busy.signal,
        headers: { "Content-Type": "application/json", "X-Baton-CSRF": csrf },
        body: JSON.stringify({
          messages: messages.map(function (x) { return { role: x.role, content: x.content }; }),
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
            if (d.reasoning_content) m.reasoning += d.reasoning_content;
            if (d.content) m.content += d.content;
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
            fill(el, m, false);
            scroller.follow(false);
          });
        }
      }
      m.meta = timings ? number(timings.predicted_n, 0) + " tokens · " + number(timings.predicted_per_second, 1) + " tok/s" : "";
    } catch (e) {
      if (e.name === "AbortError") { m.meta = T.stopped; }
      else { failed = true; m.meta = T.error + " " + e.message; }
    } finally {
      done = true;
      busy = null;
      if (m.content || m.reasoning) {
        messages.push(m);
      } else if (failed && restoreOnFail != null) {
        messages.pop();
        text.value = restoreOnFail;
        autosize();
      }
      setBusy(false);
      renderAll();
      persistThread();
      scroller.follow(false);
      text.focus();
    }
  }

  async function ask() {
    var content = text.value.trim();
    if (!content || busy || !ready()) return;
    messages.push({ role: "user", content: content });
    text.value = "";
    autosize();
    var uEl = bubble("user", messages.length - 1);
    uEl.querySelector(".msg-body").textContent = content;
    await streamReply(content);
  }

  // ------------------------------------------------------------- events
  function autosize() { text.style.height = "auto"; text.style.height = Math.min(text.scrollHeight, 192) + "px"; }
  text.addEventListener("input", autosize);
  text.addEventListener("keydown", function (e) {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); ask(); }
  });
  form.addEventListener("submit", function (e) {
    e.preventDefault();
    if (busy) busy.abort(); else ask();
  });
  modelSel.addEventListener("change", function () {
    showState();
    if (!threadId) history.replaceState(null, "", "/chat?model=" + encodeURIComponent(modelSel.value));
  });
  effortSel.addEventListener("change", function () {
    try { localStorage.setItem("baton-chat-effort", effortSel.value); } catch (e) {}
  });
  root.querySelectorAll("[data-new]").forEach(function (btn) {
    btn.addEventListener("click", startNew);
  });

  if (boot.active_thread && boot.active_thread.agent === "local") {
    applyThread(boot.active_thread);
  } else {
    renderAll();
    showState();
  }
  setInterval(function () { if (!document.hidden) refresh(); }, 4000);
})();
