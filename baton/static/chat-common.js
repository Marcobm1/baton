// Shared by local model chat and agents chat:
//  - Safe Markdown (code blocks with copy bar, tables, blockquotes, rules, lists, inline formatting)
//  - Sticky auto-scroll with floating jump-to-bottom button
//  - Collapsible thread history sidebar (SQLite-backed, synced across desktop & mobile)
//  - Agent switcher
(function () {
  function esc(s) {
    return String(s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  function inline(raw) {
    return raw.split(/(`[^`\n]+`)/).map(function (part, i) {
      if (i % 2) return "<code>" + esc(part.slice(1, -1)) + "</code>";
      return esc(part)
        .replace(/~~([^~\n]+)~~/g, "<del>$1</del>")
        .replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>")
        .replace(/(^|[\s(])\*([^*\n]+)\*(?=[\s).,;:!?]|$)/g, "$1<em>$2</em>")
        .replace(/\[([^\]\n]+)\]\((https?:\/\/[^\s)]+)\)/g,
                 '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>');
    }).join("");
  }

  function splitTableRow(line) {
    var s = line.trim();
    if (s.charAt(0) === "|") s = s.slice(1);
    if (s.charAt(s.length - 1) === "|") s = s.slice(0, -1);
    return s.split("|").map(function (c) { return c.trim(); });
  }

  function isTableSep(line) {
    if (!line || line.indexOf("|") < 0) return false;
    var cells = splitTableRow(line);
    return cells.length >= 2 && cells.every(function (c) { return /^:?-{3,}:?$/.test(c); });
  }

  function md(src) {
    var out = [], lines = String(src || "").replace(/\r/g, "").split("\n"), i = 0;
    var list = null, para = [], quote = [];
    function flushPara() {
      if (para.length) { out.push("<p>" + para.map(inline).join("<br>") + "</p>"); para = []; }
    }
    function flushQuote() {
      if (quote.length) { out.push("<blockquote>" + quote.map(inline).join("<br>") + "</blockquote>"); quote = []; }
    }
    function closeList() { if (list) { out.push("</" + list + ">"); list = null; } }
    function flushAll() { flushPara(); flushQuote(); closeList(); }

    while (i < lines.length) {
      var line = lines[i], fence = line.match(/^\s*```\s*([\w+#.-]*)/);
      if (fence) {
        flushAll();
        var lang = fence[1] || "";
        var code = [];
        for (i++; i < lines.length && !/^\s*```/.test(lines[i]); i++) code.push(lines[i]);
        i++;
        var head = '<div class="code-head"><span class="code-lang">' + esc(lang || "code") +
                   '</span><button type="button" class="code-copy" data-code-copy>Copy</button></div>';
        out.push('<div class="code-block">' + head + '<pre><code>' + esc(code.join("\n")) + '</code></pre></div>');
        continue;
      }

      if (line.indexOf("|") >= 0 && i + 1 < lines.length && isTableSep(lines[i + 1])) {
        flushAll();
        var headers = splitTableRow(line);
        i += 2;
        var rows = [];
        while (i < lines.length && lines[i].trim() && lines[i].indexOf("|") >= 0) {
          rows.push(splitTableRow(lines[i]));
          i++;
        }
        var th = "<thead><tr>" + headers.map(function (c) { return "<th>" + inline(c) + "</th>"; }).join("") + "</tr></thead>";
        var tb = "<tbody>" + rows.map(function (r) {
          return "<tr>" + headers.map(function (_, idx) { return "<td>" + inline(r[idx] || "") + "</td>"; }).join("") + "</tr>";
        }).join("") + "</tbody>";
        out.push('<div class="md-table-wrap"><table class="md-table">' + th + tb + "</table></div>");
        continue;
      }

      if (/^\s*(?:---+|\*\*\*+|___+)\s*$/.test(line)) {
        flushAll();
        out.push("<hr>");
        i++;
        continue;
      }

      var bq = line.match(/^\s*>\s?(.*)$/);
      if (bq) {
        flushPara(); closeList();
        quote.push(bq[1]);
        i++;
        continue;
      }
      flushQuote();

      var h = line.match(/^(#{1,4})\s+(.*)/);
      var ul = line.match(/^\s*[-*+]\s+(.*)/);
      var ol = line.match(/^\s*\d+[.)]\s+(.*)/);
      if (h) {
        flushPara(); closeList();
        var n = h[1].length + 2;
        out.push("<h" + n + ">" + inline(h[2]) + "</h" + n + ">");
      } else if (ul || ol) {
        flushPara();
        var tag = ul ? "ul" : "ol";
        if (list !== tag) { closeList(); out.push("<" + tag + ">"); list = tag; }
        var item = (ul || ol)[1];
        var chk = ul && item.match(/^\[([ xX])\]\s+(.*)$/);
        if (chk) {
          var mark = chk[1].toLowerCase() === "x" ? "☑" : "☐";
          out.push('<li class="md-check">' + mark + " " + inline(chk[2]) + "</li>");
        } else {
          out.push("<li>" + inline(item) + "</li>");
        }
      } else if (!line.trim()) {
        flushPara(); closeList();
      } else {
        closeList(); para.push(line);
      }
      i++;
    }
    flushAll();
    return out.join("");
  }

  // Delegated copy handler for every fenced code block on the page.
  document.addEventListener("click", function (ev) {
    var btn = ev.target && ev.target.closest ? ev.target.closest("[data-code-copy]") : null;
    if (!btn) return;
    var wrap = btn.closest(".code-block");
    var code = wrap && wrap.querySelector("pre code");
    if (!code) return;
    var original = btn.textContent;
    try {
      navigator.clipboard.writeText(code.textContent || "").then(function () {
        btn.textContent = "✓";
        setTimeout(function () { btn.textContent = original; }, 1400);
      });
    } catch (e) {}
  });

  // Sticky auto-scroll: follows streamed tokens only when the user is near the bottom.
  function createScroller(btn) {
    var pinned = true;
    function distanceFromBottom() {
      var doc = document.documentElement;
      return Math.max(0, doc.scrollHeight - (window.scrollY + window.innerHeight));
    }
    function sync() {
      var dist = distanceFromBottom();
      pinned = dist < 110;
      if (btn) btn.hidden = dist < 180;
    }
    window.addEventListener("scroll", sync, { passive: true });
    if (btn) {
      btn.addEventListener("click", function () {
        pinned = true;
        window.scrollTo({ top: document.documentElement.scrollHeight, behavior: "smooth" });
        btn.hidden = true;
      });
    }
    return {
      follow: function (force) {
        if (force || pinned) {
          window.scrollTo(0, document.documentElement.scrollHeight);
          pinned = true;
          if (btn) btn.hidden = true;
        } else if (btn) {
          btn.hidden = false;
        }
      },
      pin: function () { pinned = true; }
    };
  }

  // Sidebar thread history controller shared by local and agents chat.
  function initSidebar(opts) {
    var shell = document.querySelector("[data-chat-shell]");
    var listEl = document.querySelector("[data-thread-list]");
    var searchEl = document.querySelector("[data-thread-search]");
    var backdrop = document.querySelector("[data-sidebar-backdrop]");
    var toggleBtn = document.querySelector("[data-sidebar-toggle]");
    var newBtn = document.querySelector("[data-sidebar-new]");
    if (!shell || !listEl) return { refresh: function () {}, setActive: function () {}, closeDrawer: function () {} };

    var csrf = opts.csrf || "";
    var currentAgent = opts.agent || "local";
    var labels = opts.labels || {};
    var activeId = opts.activeId || "";
    var timer = null;

    function setOpen(on) {
      shell.classList.toggle("sidebar-open", !!on);
      if (toggleBtn) toggleBtn.setAttribute("aria-expanded", String(!!on));
    }
    if (toggleBtn) toggleBtn.addEventListener("click", function () { setOpen(!shell.classList.contains("sidebar-open")); });
    if (backdrop) backdrop.addEventListener("click", function () { setOpen(false); });
    if (newBtn && opts.onNew) {
      newBtn.addEventListener("click", function () {
        setOpen(false);
        opts.onNew();
      });
    }

    function shortAgent(ag) {
      return { local: "Local", opencode: "OC", gemini: "Gemini", "claude-code": "Claude", codex: "Codex" }[ag] || ag;
    }

    function render(threads) {
      listEl.innerHTML = "";
      if (!threads || !threads.length) {
        var empty = document.createElement("p");
        empty.className = "muted small thread-empty";
        empty.textContent = labels.empty || "No conversations yet.";
        listEl.appendChild(empty);
        return;
      }
      threads.forEach(function (th) {
        var item = document.createElement("div");
        item.className = "thread-item" + (th.id === activeId ? " is-active" : "");
        item.dataset.threadId = th.id;

        var link = document.createElement("a");
        link.className = "thread-link";
        link.href = "/chat?agent=" + encodeURIComponent(th.agent) + "&thread=" + encodeURIComponent(th.id);
        var title = document.createElement("span");
        title.className = "thread-title";
        title.textContent = th.title || labels.untitled || "Untitled";
        var meta = document.createElement("span");
        meta.className = "thread-meta small muted";
        var sub = [shortAgent(th.agent), th.folder_label || th.model].filter(Boolean).join(" · ");
        meta.textContent = sub;
        link.appendChild(title);
        link.appendChild(meta);
        link.addEventListener("click", function (ev) {
          if (th.agent === currentAgent && opts.onSelect) {
            ev.preventDefault();
            activeId = th.id;
            markActive();
            setOpen(false);
            opts.onSelect(th);
          }
        });

        var actions = document.createElement("div");
        actions.className = "thread-actions";
        var ren = document.createElement("button");
        ren.type = "button";
        ren.className = "thread-act";
        ren.title = labels.rename || "Rename";
        ren.textContent = "✎";
        ren.addEventListener("click", function (ev) {
          ev.stopPropagation();
          var next = window.prompt(labels.rename || "Rename", th.title || "");
          if (!next || !next.trim()) return;
          fetch("/api/chat-threads/" + encodeURIComponent(th.id), {
            method: "PATCH",
            headers: { "Content-Type": "application/json", "X-Baton-CSRF": csrf },
            body: JSON.stringify({ title: next.trim() })
          }).then(function (r) { if (r.ok) refresh(); });
        });

        var del = document.createElement("button");
        del.type = "button";
        del.className = "thread-act danger";
        del.title = labels.del || "Delete";
        del.textContent = "✕";
        del.addEventListener("click", function (ev) {
          ev.stopPropagation();
          if (!window.confirm(labels.delConfirm || "Delete this conversation?")) return;
          fetch("/api/chat-threads/" + encodeURIComponent(th.id), {
            method: "DELETE",
            headers: { "X-Baton-CSRF": csrf }
          }).then(function (r) {
            if (!r.ok) return;
            if (th.id === activeId) {
              activeId = "";
              if (opts.onDeletedActive) opts.onDeletedActive();
            }
            refresh();
          });
        });

        actions.appendChild(ren);
        actions.appendChild(del);
        item.appendChild(link);
        item.appendChild(actions);
        listEl.appendChild(item);
      });
    }

    function markActive() {
      listEl.querySelectorAll(".thread-item").forEach(function (el) {
        el.classList.toggle("is-active", el.dataset.threadId === activeId);
      });
    }

    function refresh(nextActiveId) {
      if (typeof nextActiveId === "string") activeId = nextActiveId;
      var q = searchEl ? searchEl.value.trim() : "";
      var url = "/api/chat-threads" + (q ? "?q=" + encodeURIComponent(q) : "");
      return fetch(url, { cache: "no-store" })
        .then(function (r) { return r.ok ? r.json() : null; })
        .then(function (d) { if (d && Array.isArray(d.threads)) render(d.threads); })
        .catch(function () {});
    }

    if (searchEl) {
      searchEl.addEventListener("input", function () {
        clearTimeout(timer);
        timer = setTimeout(function () { refresh(); }, 200);
      });
    }

    // Wire prompt suggestion chips if present.
    document.querySelectorAll("[data-suggest]").forEach(function (chip) {
      chip.addEventListener("click", function () {
        var box = document.querySelector("[data-text]");
        if (!box || box.disabled) return;
        box.value = chip.dataset.suggest || chip.textContent || "";
        box.dispatchEvent(new Event("input"));
        box.focus();
      });
    });

    if (Array.isArray(opts.initialThreads)) render(opts.initialThreads);

    return {
      refresh: refresh,
      setActive: function (id) { activeId = id || ""; markActive(); },
      closeDrawer: function () { setOpen(false); }
    };
  }

  window.BatonChat = { esc: esc, md: md, createScroller: createScroller, initSidebar: initSidebar };

  // Agent switcher: the page is drawn for one agent; switching reloads it.
  var sw = document.querySelector("[data-agent-switch]");
  if (!sw) return;
  var params = new URLSearchParams(location.search);
  var wanted = params.get("agent") || (params.get("model") ? "local" : "") || (params.get("thread") ? sw.value : "");
  try {
    var last = localStorage.getItem("baton-chat-agent");
    if (!wanted && last && last !== sw.value) { location.replace("/chat?agent=" + encodeURIComponent(last)); return; }
    localStorage.setItem("baton-chat-agent", sw.value);
  } catch (e) {}
  sw.addEventListener("change", function () {
    try { localStorage.setItem("baton-chat-agent", sw.value); } catch (e) {}
    location.href = "/chat?agent=" + encodeURIComponent(sw.value);
  });
})();
