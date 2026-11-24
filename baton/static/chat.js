// Chat page: talks to a local model through Baton (/api/chat/<model>), which
// relays the model's streamed answer (server-sent events). The conversation is
// kept in this browser only (localStorage, one per model).
(function () {
  var root = document.querySelector("[data-chat]");
  if (!root || !root.querySelector("[data-form]")) return;
  var T = JSON.parse(document.getElementById("chat-strings").textContent);
  var csrf = root.dataset.csrf;
  var log = root.querySelector("[data-log]"), empty = root.querySelector("[data-empty]");
  var form = root.querySelector("[data-form]"), text = root.querySelector("[data-text]");
  var send = root.querySelector("[data-send]"), modelSel = root.querySelector("[data-model]");
  var effortSel = root.querySelector("[data-effort]"), off = root.querySelector("[data-off]");
  var offText = root.querySelector("[data-off-text]"), startForm = root.querySelector("[data-start-form]");
  var messages = [], busy = null, poll = null;

  // ------------------------------------------------------------ storage
  function key() { return "baton-chat:" + modelSel.value; }
  function save() { try { localStorage.setItem(key(), JSON.stringify(messages.slice(-200))); } catch (e) {} }
  function load() {
    try { var v = JSON.parse(localStorage.getItem(key()) || "[]"); return Array.isArray(v) ? v : []; }
    catch (e) { return []; }
  }
  try { var eff = localStorage.getItem("baton-chat-effort"); if (eff) effortSel.value = eff; } catch (e) {}

  // ----------------------------------------------------------- markdown
  // A small, safe subset: everything is escaped first, then code, headings,
  // lists, bold, italics and http(s) links are added back.
  function esc(s) {
    return s.replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }
  function inline(raw) {
    return raw.split(/(`[^`\n]+`)/).map(function (part, i) {
      if (i % 2) return "<code>" + esc(part.slice(1, -1)) + "</code>";
      return esc(part)
        .replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>")
        .replace(/(^|[\s(])\*([^*\n]+)\*(?=[\s).,;:!?]|$)/g, "$1<em>$2</em>")
        .replace(/\[([^\]\n]+)\]\((https?:\/\/[^\s)]+)\)/g,
                 '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>');
    }).join("");
  }
  function md(src) {
    var out = [], lines = src.replace(/\r/g, "").split("\n"), i = 0, list = null, para = [];
    function flush() {
      if (para.length) { out.push("<p>" + para.map(inline).join("<br>") + "</p>"); para = []; }
    }
    function close() { if (list) { out.push("</" + list + ">"); list = null; } }
    while (i < lines.length) {
      var line = lines[i], fence = line.match(/^\s*```\s*([\w+#.-]*)/);
      if (fence) {
        flush(); close();
        var code = [];
        for (i++; i < lines.length && !/^\s*```/.test(lines[i]); i++) code.push(lines[i]);
        i++;
        out.push("<pre><code>" + esc(code.join("\n")) + "</code></pre>");
        continue;
      }
      var h = line.match(/^(#{1,4})\s+(.*)/), ul = line.match(/^\s*[-*+]\s+(.*)/), ol = line.match(/^\s*\d+[.)]\s+(.*)/);
      if (h) {
        flush(); close();
        var n = h[1].length + 2;
        out.push("<h" + n + ">" + inline(h[2]) + "</h" + n + ">");
      } else if (ul || ol) {
        flush();
        var tag = ul ? "ul" : "ol";
        if (list !== tag) { close(); out.push("<" + tag + ">"); list = tag; }
        out.push("<li>" + inline((ul || ol)[1]) + "</li>");
      } else if (!line.trim()) {
        flush(); close();
      } else {
        close(); para.push(line);
      }
      i++;
    }
    flush(); close();
    return out.join("");
  }

  // ----------------------------------------------------------- messages
  function scrollDown() { window.scrollTo(0, document.body.scrollHeight); }
  function bubble(role) {
    var el = document.createElement("article");
    el.className = "msg msg-" + role;
    if (role === "assistant") {
      el.innerHTML = '<details class="msg-think" hidden><summary></summary><div class="msg-think-text"></div></details>' +
                     '<div class="msg-body"></div><p class="msg-meta muted small"><span></span></p>';
      el.querySelector("summary").textContent = T.reasoning;
    } else {
      el.innerHTML = '<div class="msg-body"></div>';
    }
    log.appendChild(el);
    empty.hidden = true;
    return el;
  }
  function fill(el, m) {
    var think = el.querySelector(".msg-think");
    if (m.reasoning) {
      think.hidden = false;
      think.querySelector(".msg-think-text").textContent = m.reasoning;
    }
    el.querySelector(".msg-body").innerHTML = md(m.content || "");
    el.querySelector(".msg-meta span").textContent = m.meta || "";
    var meta = el.querySelector(".msg-meta");
    if (m.content && !meta.querySelector("button") && m.meta !== T.thinking && m.meta !== T.writing) {
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
  }
  function renderAll() {
    log.querySelectorAll(".msg").forEach(function (el) { el.remove(); });
    messages.forEach(function (m) {
      var el = bubble(m.role);
      if (m.role === "assistant") fill(el, m);
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
          if (o) o.dataset.status = m.status;
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
  async function ask() {
    var content = text.value.trim();
    if (!content || busy || !ready()) return;
    messages.push({ role: "user", content: content });
    text.value = "";
    autosize();
    bubble("user").querySelector(".msg-body").textContent = content;
    var m = { role: "assistant", content: "", reasoning: "", meta: T.thinking };
    var el = bubble("assistant");
    fill(el, m);
    scrollDown();
    busy = new AbortController();
    setBusy(true);
    var timings = null, failed = false, queued = false, done = false;
    try {
      var r = await fetch("/api/chat/" + encodeURIComponent(modelSel.value), {
        method: "POST", signal: busy.signal,
        headers: { "Content-Type": "application/json", "X-Baton-CSRF": csrf },
        body: JSON.stringify({
          messages: messages.map(function (x) { return { role: x.role, content: x.content }; }),
          reasoning_effort: effortSel.value })
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
            if (done) return;   // the answer already ended: keep its final line
            m.meta = m.content ? T.writing : T.thinking;
            fill(el, m);
            scrollDown();
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
      } else if (failed) {
        // Nothing came back: take the question out again so it can be resent.
        messages.pop();
        text.value = content;
        autosize();
      }
      fill(el, m);
      setBusy(false);
      save();
      scrollDown();
      text.focus();
    }
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
    messages = load();
    renderAll();
    showState();
    history.replaceState(null, "", "/chat?model=" + encodeURIComponent(modelSel.value));
  });
  effortSel.addEventListener("change", function () {
    try { localStorage.setItem("baton-chat-effort", effortSel.value); } catch (e) {}
  });
  root.querySelector("[data-new]").addEventListener("click", function () {
    if (busy) busy.abort();
    messages = [];
    save();
    renderAll();
    text.focus();
  });

  messages = load();
  renderAll();
  showState();
  scrollDown();
  // Keep the model's state fresh (loading -> running, stopped elsewhere).
  poll = setInterval(function () { if (!document.hidden) refresh(); }, 4000);
})();
