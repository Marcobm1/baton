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
  // Supports an internal scroll container (.chat-scroll) and falls back to window.
  function createScroller(btn, container) {
    var pinned = true;
    var sc = container || null;
    function distanceFromBottom() {
      if (sc) return Math.max(0, sc.scrollHeight - (sc.scrollTop + sc.clientHeight));
      var doc = document.documentElement;
      return Math.max(0, doc.scrollHeight - (window.scrollY + window.innerHeight));
    }
    function scrollToBottom(smooth) {
      if (sc) {
        sc.scrollTo({ top: sc.scrollHeight, behavior: smooth ? "smooth" : "auto" });
      } else if (smooth) {
        window.scrollTo({ top: document.documentElement.scrollHeight, behavior: "smooth" });
      } else {
        window.scrollTo(0, document.documentElement.scrollHeight);
      }
    }
    function sync() {
      var dist = distanceFromBottom();
      pinned = dist < 110;
      if (btn) btn.hidden = dist < 180;
    }
    (sc || window).addEventListener("scroll", sync, { passive: true });
    if (btn) {
      btn.addEventListener("click", function () {
        pinned = true;
        scrollToBottom(true);
        btn.hidden = true;
      });
    }
    return {
      follow: function (force) {
        if (force || pinned) {
          scrollToBottom(false);
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
    var toggleBtns = Array.from(document.querySelectorAll("[data-sidebar-toggle]"));
    var newBtn = document.querySelector("[data-sidebar-new]");
    if (!shell || !listEl) return { refresh: function () {}, setActive: function () {}, closeDrawer: function () {} };

    var csrf = opts.csrf || "";
    var currentAgent = opts.agent || "local";
    var labels = opts.labels || {};
    var activeId = opts.activeId || "";
    var timer = null;

    function setOpen(on) {
      shell.classList.toggle("sidebar-open", !!on);
      toggleBtns.forEach(function (b) { b.setAttribute("aria-expanded", String(!!on)); });
    }
    toggleBtns.forEach(function (b) {
      b.addEventListener("click", function () { setOpen(!shell.classList.contains("sidebar-open")); });
    });
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
          var cleanTitle = next.trim();
          fetch("/api/chat-threads/" + encodeURIComponent(th.id), {
            method: "PATCH",
            headers: { "Content-Type": "application/json", "X-Baton-CSRF": csrf },
            body: JSON.stringify({ title: cleanTitle })
          }).then(function (r) {
            if (!r.ok) return;
            if (th.id === activeId && opts.onRenamedActive) opts.onRenamedActive(cleanTitle);
            refresh();
          });
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
        .then(function (d) {
          if (!d || !Array.isArray(d.threads)) return;
          render(d.threads);
          if (activeId && opts.onActiveMeta) {
            var match = d.threads.find(function (t) { return t.id === activeId; });
            if (match) opts.onActiveMeta(match);
          }
        })
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

  // Slim header controller: conversation title, folder & git branch badges,
  // and the "⋯" overflow menu (Convert to task, Rename, Delete).
  function initHeader(opts) {
    var root = opts.root || document;
    var titleEl = root.querySelector("[data-chat-title]");
    var folderBadge = root.querySelector("[data-chat-folder-badge]");
    var branchBadge = root.querySelector("[data-chat-branch-badge]");
    var menuEl = root.querySelector("[data-chat-menu]");
    var toTaskLinks = Array.from(root.querySelectorAll("[data-to-task]"));
    var renameBtn = root.querySelector("[data-thread-rename]");
    var deleteBtn = root.querySelector("[data-thread-delete]");
    var csrf = opts.csrf || "";
    var labels = opts.labels || {};
    var state = { threadId: "", title: "" };

    function closeMenu() {
      if (menuEl) menuEl.open = false;
    }

    if (menuEl) {
      document.addEventListener("click", function (ev) {
        if (menuEl.open && !menuEl.contains(ev.target)) closeMenu();
      });
      document.addEventListener("keydown", function (ev) {
        if (ev.key === "Escape" && menuEl.open) closeMenu();
      });
    }

    function setThread(id, title) {
      state.threadId = id || "";
      if (title !== undefined) state.title = title || "";
      else if (!state.threadId) state.title = "";
      if (titleEl) {
        titleEl.textContent = state.title || (state.threadId ? (labels.untitled || "Untitled") : (labels.newChat || "New chat"));
      }
      toTaskLinks.forEach(function (a) {
        a.hidden = !state.threadId;
        if (state.threadId) a.href = "/tasks/new?from_thread=" + encodeURIComponent(state.threadId);
      });
      if (renameBtn) renameBtn.disabled = !state.threadId;
      if (deleteBtn) deleteBtn.disabled = !state.threadId;
    }

    function setTitle(title) {
      state.title = title || "";
      if (titleEl) {
        titleEl.textContent = state.title || (state.threadId ? (labels.untitled || "Untitled") : (labels.newChat || "New chat"));
      }
    }

    function setContext(folder, branch) {
      if (folderBadge) {
        folderBadge.textContent = folder || "";
        folderBadge.title = folder || "";
        folderBadge.hidden = !folder;
      }
      if (branchBadge) {
        branchBadge.textContent = branch ? "⎇ " + branch : "";
        branchBadge.title = branch || "";
        branchBadge.hidden = !branch;
      }
    }

    if (renameBtn) {
      renameBtn.addEventListener("click", function () {
        closeMenu();
        if (!state.threadId) return;
        var next = window.prompt(labels.rename || "Rename", state.title || "");
        if (!next || !next.trim()) return;
        var cleanTitle = next.trim();
        fetch("/api/chat-threads/" + encodeURIComponent(state.threadId), {
          method: "PATCH",
          headers: { "Content-Type": "application/json", "X-Baton-CSRF": csrf },
          body: JSON.stringify({ title: cleanTitle })
        }).then(function (r) {
          if (!r.ok) return;
          setTitle(cleanTitle);
          if (opts.onAfterRename) opts.onAfterRename(cleanTitle);
        });
      });
    }

    if (deleteBtn) {
      deleteBtn.addEventListener("click", function () {
        closeMenu();
        if (!state.threadId) return;
        if (!window.confirm(labels.delConfirm || "Delete this conversation?")) return;
        var doomed = state.threadId;
        fetch("/api/chat-threads/" + encodeURIComponent(doomed), {
          method: "DELETE",
          headers: { "X-Baton-CSRF": csrf }
        }).then(function (r) {
          if (!r.ok) return;
          setThread("", "");
          if (opts.onAfterDelete) opts.onAfterDelete();
        });
      });
    }

    setThread("", "");

    return {
      setThread: setThread,
      setTitle: setTitle,
      setContext: setContext,
      closeMenu: closeMenu
    };
  }

  // Image attachment picker & clipboard paste helper (PNG, JPEG, WebP, GIF; max 4, <= 4 MiB each).
  var ALLOWED_IMG = { "image/png": 1, "image/jpeg": 1, "image/webp": 1, "image/gif": 1 };
  function createAttachments(root, textarea) {
    var btn = root.querySelector("[data-attach-btn]");
    var input = root.querySelector("[data-attach-input]");
    var bar = root.querySelector("[data-attach-bar]");
    var items = [];
    if (!btn || !input || !bar) {
      return { list: function () { return []; }, clear: function () {}, renderThumbs: function () {} };
    }
    function renderBar() {
      bar.innerHTML = "";
      bar.hidden = items.length === 0;
      items.forEach(function (url, idx) {
        var wrap = document.createElement("span");
        wrap.className = "attach-thumb";
        var img = document.createElement("img");
        img.src = url;
        img.alt = "";
        var rm = document.createElement("button");
        rm.type = "button";
        rm.className = "attach-rm";
        rm.textContent = "✕";
        rm.addEventListener("click", function () { items.splice(idx, 1); renderBar(); });
        wrap.appendChild(img);
        wrap.appendChild(rm);
        bar.appendChild(wrap);
      });
    }
    function addFiles(fileList) {
      Array.from(fileList || []).forEach(function (file) {
        if (items.length >= 4) return;
        if (!ALLOWED_IMG[file.type] || file.size <= 0 || file.size > 4 * 1024 * 1024) return;
        var reader = new FileReader();
        reader.onload = function () {
          if (typeof reader.result === "string" && items.length < 4) {
            items.push(reader.result);
            renderBar();
          }
        };
        reader.readAsDataURL(file);
      });
    }
    btn.addEventListener("click", function () { if (!textarea.disabled) input.click(); });
    input.addEventListener("change", function () { addFiles(input.files); input.value = ""; });
    textarea.addEventListener("paste", function (ev) {
      var clip = ev.clipboardData && ev.clipboardData.files;
      if (clip && clip.length) {
        var imgs = Array.from(clip).filter(function (f) { return ALLOWED_IMG[f.type]; });
        if (imgs.length) { ev.preventDefault(); addFiles(imgs); }
      }
    });
    return {
      list: function () { return items.slice(); },
      clear: function () { items = []; renderBar(); },
      renderThumbs: function (container, urls) {
        if (!container || !urls || !urls.length) return;
        var row = document.createElement("div");
        row.className = "msg-imgs";
        urls.forEach(function (u) {
          var img = document.createElement("img");
          img.src = u;
          img.alt = "";
          row.appendChild(img);
        });
        container.appendChild(row);
      }
    };
  }

  // Shared LCS diff renderer used by Agents chat and local model tool harness.
  function diffLines(oldText, newText) {
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

  function diffBlock(d, unchangedTpl) {
    var wrap = document.createElement("div");
    wrap.className = "ag-block ag-diff-card";
    var raw = diffLines(d.old, d.new).slice(0, 2000);
    var oldNo = 0, newNo = 0, added = 0, removed = 0;
    var rows = raw.map(function (r) {
      var kind = r[0];
      if (kind === "+") { newNo++; added++; return { kind: "+", oldNo: "", newNo: String(newNo), text: r[1] }; }
      if (kind === "-") { oldNo++; removed++; return { kind: "-", oldNo: String(oldNo), newNo: "", text: r[1] }; }
      oldNo++; newNo++;
      return { kind: " ", oldNo: String(oldNo), newNo: String(newNo), text: r[1] };
    });

    // Keep +/- lines and 3 lines of context around each change; collapse long unchanged stretches.
    var keep = new Uint8Array(rows.length);
    var hasChanges = added > 0 || removed > 0;
    for (var i = 0; i < rows.length; i++) {
      if (!hasChanges || rows[i].kind !== " ") {
        var lo = Math.max(0, i - 3), hi = Math.min(rows.length - 1, i + 3);
        for (var k = lo; k <= hi; k++) keep[k] = 1;
      }
    }

    var head = document.createElement("div");
    head.className = "ag-diff-head";
    var pathEl = document.createElement("span");
    pathEl.className = "ag-path";
    pathEl.textContent = d.path || "";
    head.appendChild(pathEl);
    if (added || removed) {
      var stats = document.createElement("span");
      stats.className = "ag-diff-stats";
      if (added) {
        var sa = document.createElement("span");
        sa.className = "ag-stat-add";
        sa.textContent = "+" + added;
        stats.appendChild(sa);
      }
      if (removed) {
        var sd = document.createElement("span");
        sd.className = "ag-stat-del";
        sd.textContent = "-" + removed;
        stats.appendChild(sd);
      }
      head.appendChild(stats);
    }
    wrap.appendChild(head);

    var table = document.createElement("div");
    table.className = "ag-diff ag-diff-table";
    var hiddenRun = 0;
    function flushGap() {
      if (hiddenRun <= 0) return;
      var gap = document.createElement("div");
      gap.className = "ag-diff-gap";
      gap.textContent = "··· " + (unchangedTpl || "{n} unchanged lines").replace("{n}", String(hiddenRun)) + " ···";
      table.appendChild(gap);
      hiddenRun = 0;
    }
    rows.forEach(function (row, idx) {
      if (!keep[idx]) { hiddenRun++; return; }
      flushGap();
      var line = document.createElement("div");
      line.className = "ag-diff-row " + (row.kind === "+" ? "add" : row.kind === "-" ? "del" : "ctx");
      var ln1 = document.createElement("span");
      ln1.className = "ag-ln";
      ln1.textContent = row.oldNo;
      var ln2 = document.createElement("span");
      ln2.className = "ag-ln";
      ln2.textContent = row.newNo;
      var sign = document.createElement("span");
      sign.className = "ag-sign";
      sign.textContent = row.kind;
      var code = document.createElement("span");
      code.className = "ag-diff-code";
      code.textContent = row.text;
      line.appendChild(ln1);
      line.appendChild(ln2);
      line.appendChild(sign);
      line.appendChild(code);
      table.appendChild(line);
    });
    flushGap();
    wrap.appendChild(table);
    return wrap;
  }

  // Inline approval 1/2 keyboard shortcuts (never fires while typing in an input/textarea).
  var pendingPerms = [];
  document.addEventListener("keydown", function (ev) {
    if (!pendingPerms.length) return;
    if (ev.ctrlKey || ev.altKey || ev.metaKey) return;
    if (ev.key !== "1" && ev.key !== "2") return;
    var active = document.activeElement;
    if (active && (active.tagName === "INPUT" || active.tagName === "TEXTAREA" || active.tagName === "SELECT" || active.isContentEditable)) {
      return;
    }
    var top = pendingPerms[pendingPerms.length - 1];
    if (!top) return;
    var target = ev.key === "1" ? top.allowBtn : top.denyBtn;
    if (target && !target.disabled) {
      ev.preventDefault();
      target.click();
    }
  });
  function registerPendingPerm(id, allowBtn, denyBtn) {
    unregisterPendingPerm(id);
    pendingPerms.push({ id: id, allowBtn: allowBtn, denyBtn: denyBtn });
  }
  function unregisterPendingPerm(id) {
    pendingPerms = pendingPerms.filter(function (item) { return item.id !== id; });
  }
  function setPermButton(btn, keyLabel, text) {
    btn.innerHTML = "";
    var kbd = document.createElement("kbd");
    kbd.className = "ag-kbd";
    kbd.textContent = keyLabel;
    var span = document.createElement("span");
    span.textContent = text;
    btn.appendChild(kbd);
    btn.appendChild(span);
  }

  // Collapsed "Thinking… Ns" -> "Thought for Ns" live timer for reasoning blocks.
  function createThinkTimer(summaryEl, labels) {
    var started = 0, timer = null, finalSec = null;
    function renderLive() {
      if (!summaryEl || !started) return;
      var s = Math.max(0, Math.floor((Date.now() - started) / 1000));
      summaryEl.textContent = (labels.thinkingS || "Thinking… {s}s").replace("{s}", String(s));
    }
    return {
      start: function () {
        if (started || finalSec !== null) return;
        started = Date.now();
        renderLive();
        timer = setInterval(renderLive, 1000);
      },
      stop: function () {
        if (timer) { clearInterval(timer); timer = null; }
        if (started && finalSec === null) {
          finalSec = Math.max(1, Math.round((Date.now() - started) / 1000));
          if (summaryEl) {
            summaryEl.textContent = (labels.thoughtFor || "Thought for {s}s").replace("{s}", String(finalSec));
          }
        }
        return finalSec;
      },
      seconds: function () { return finalSec; }
    };
  }

  // Composer control centre (UI-3): bottom-row chips with upward popovers,
  // locked-state display when a session starts, and Esc / Shift+Tab shortcuts.
  function initComposerControls(opts) {
    var root = opts.root || document;
    var form = root.querySelector("[data-form]");
    var popover = root.querySelector("[data-cc-popover]");
    var chips = opts.chips || [];
    var labels = opts.labels || {};
    var chipEls = {};
    var openId = "";

    chips.forEach(function (cfg) {
      var btn = root.querySelector('[data-cc-chip="' + cfg.id + '"]');
      if (btn) chipEls[cfg.id] = btn;
    });

    function mk(tag, cls, txt) {
      var el = document.createElement(tag);
      if (cls) el.className = cls;
      if (txt != null) el.textContent = txt;
      return el;
    }

    function close(restoreFocus) {
      if (!openId) return;
      var prev = chipEls[openId];
      if (prev) prev.setAttribute("aria-expanded", "false");
      openId = "";
      if (popover) {
        popover.hidden = true;
        popover.innerHTML = "";
        popover.style.left = "";
      }
      if (restoreFocus) {
        var box = root.querySelector("[data-text]");
        if (prev && !prev.disabled) prev.focus();
        else if (box && !box.disabled) box.focus();
      }
    }

    function positionPopover(btn) {
      if (!popover || !form || !btn) return;
      if (window.innerWidth < 600) {
        popover.style.left = "";
        return;
      }
      var formRect = form.getBoundingClientRect();
      var btnRect = btn.getBoundingClientRect();
      var popW = popover.offsetWidth || 260;
      var rawLeft = btnRect.left - formRect.left;
      var maxLeft = Math.max(0, formRect.width - popW);
      popover.style.left = Math.max(0, Math.min(rawLeft, maxLeft)) + "px";
    }

    function renderSelectList(body, cfg, selectEl, extra) {
      if (!selectEl) return;
      extra = extra || {};
      if (extra.catalogTabs && extra.catalogTabs.length > 1) {
        var tabBar = mk("div", "cc-pop-tabs");
        extra.catalogTabs.forEach(function (tab) {
          var tb = mk("button", "cc-pop-tab" + (tab.active ? " is-active" : ""), tab.label);
          tb.type = "button";
          tb.addEventListener("click", function () {
            tab.onPick();
            open(cfg.id);
          });
          tabBar.appendChild(tb);
        });
        body.appendChild(tabBar);
      }

      var allOpts = Array.from(selectEl.options);
      var listWrap = mk("div", "cc-pop-list");
      var searchInput = null;
      if (allOpts.length > 8) {
        searchInput = mk("input", "cc-pop-search");
        searchInput.type = "search";
        searchInput.placeholder = labels.filterModels || "Filter…";
        searchInput.setAttribute("aria-label", labels.filterModels || "Filter");
        body.appendChild(searchInput);
      }
      body.appendChild(listWrap);

      function draw(query) {
        var q = (query || "").trim().toLowerCase();
        listWrap.innerHTML = "";
        var children = Array.from(selectEl.children);
        var hasGroups = children.some(function (c) { return c.tagName === "OPTGROUP"; });

        function addOpt(opt) {
          var text = (opt.textContent || "").trim();
          var val = opt.value || "";
          if (q && text.toLowerCase().indexOf(q) < 0 && val.toLowerCase().indexOf(q) < 0) return false;
          var active = opt.value === selectEl.value;
          var row = mk("button", "cc-pop-opt" + (active ? " is-active" : ""));
          row.type = "button";
          row.disabled = !!opt.disabled;
          var main = mk("span", "cc-pop-opt-main", text);
          row.appendChild(main);
          var right = mk("span", "cc-pop-opt-right");
          if (opt.hasAttribute("data-payg")) right.appendChild(mk("span", "cc-pop-tag warn", "PAYG"));
          if (opt.dataset && opt.dataset.branch) right.appendChild(mk("span", "cc-pop-tag", "⎇ " + opt.dataset.branch));
          if (active) right.appendChild(mk("span", "cc-pop-check", "✓"));
          row.appendChild(right);
          row.addEventListener("click", function () {
            selectEl.value = opt.value;
            selectEl.dispatchEvent(new Event("change", { bubbles: true }));
            close(false);
            sync();
            var box = root.querySelector("[data-text]");
            if (box && !box.disabled) box.focus();
          });
          listWrap.appendChild(row);
          return true;
        }

        if (hasGroups) {
          children.forEach(function (grp) {
            if (grp.tagName !== "OPTGROUP") return;
            var grpOpts = Array.from(grp.children);
            var matched = grpOpts.filter(function (opt) {
              if (!q) return true;
              return (opt.textContent || "").toLowerCase().indexOf(q) >= 0 || (opt.value || "").toLowerCase().indexOf(q) >= 0;
            });
            if (!matched.length) return;
            if (children.length > 1 && grp.label) listWrap.appendChild(mk("div", "cc-pop-group", grp.label));
            matched.forEach(addOpt);
          });
        } else {
          allOpts.forEach(addOpt);
        }
      }

      draw("");
      if (searchInput) {
        searchInput.addEventListener("input", function () { draw(searchInput.value); });
        setTimeout(function () { searchInput.focus(); }, 0);
      }
    }

    function open(id) {
      if (!popover) return;
      var cfg = chips.find(function (c) { return c.id === id; });
      var btn = chipEls[id];
      if (!cfg || !btn || btn.disabled) return;
      if (openId && openId !== id && chipEls[openId]) {
        chipEls[openId].setAttribute("aria-expanded", "false");
      }
      openId = id;
      btn.setAttribute("aria-expanded", "true");
      popover.innerHTML = "";
      var head = mk("div", "cc-pop-head");
      head.appendChild(mk("span", "cc-pop-title", cfg.title || id));
      if (cfg.shortcut) head.appendChild(mk("kbd", "cc-pop-kbd", cfg.shortcut));
      popover.appendChild(head);
      var body = mk("div", "cc-pop-body");
      popover.appendChild(body);
      if (cfg.renderPopover) {
        cfg.renderPopover(body, close, renderSelectList);
      } else if (cfg.select) {
        renderSelectList(body, cfg, cfg.select, cfg.selectExtra ? cfg.selectExtra() : null);
      }
      popover.hidden = false;
      positionPopover(btn);
    }

    function sync() {
      chips.forEach(function (cfg) {
        var btn = chipEls[cfg.id];
        if (!btn) return;
        var hide = cfg.isHidden ? !!cfg.isHidden() : false;
        btn.hidden = hide;
        if (hide) {
          if (openId === cfg.id) close(false);
          return;
        }
        var locked = cfg.isLocked ? !!cfg.isLocked() : false;
        var tone = cfg.tone ? (cfg.tone() || "") : "";
        var text = cfg.label ? (cfg.label() || "") : "";
        btn.disabled = locked;
        btn.classList.toggle("is-locked", locked);
        btn.classList.toggle("is-warn", tone === "warn");
        btn.classList.toggle("is-bad", tone === "bad");
        btn.classList.toggle("is-accent", tone === "accent");
        btn.innerHTML = "";
        if (cfg.prefix) btn.appendChild(mk("span", "cc-chip-prefix", cfg.prefix));
        btn.appendChild(mk("span", "cc-chip-label", text));
        btn.appendChild(mk("span", "cc-chip-caret", locked ? "🔒" : "▾"));
        btn.title = locked ? ((cfg.title || "") + " · " + (labels.locked || "Locked")) : (cfg.title || text);
        if (locked && openId === cfg.id) close(false);
      });
    }

    chips.forEach(function (cfg) {
      var btn = chipEls[cfg.id];
      if (!btn) return;
      btn.addEventListener("click", function (ev) {
        ev.stopPropagation();
        if (openId === cfg.id) close(false);
        else open(cfg.id);
      });
    });

    document.addEventListener("click", function (ev) {
      if (!openId || !popover) return;
      if (popover.contains(ev.target)) return;
      var btn = chipEls[openId];
      if (btn && btn.contains(ev.target)) return;
      close(false);
    });

    document.addEventListener("keydown", function (ev) {
      if (ev.key === "Escape") {
        if (openId) {
          ev.preventDefault();
          close(true);
          return;
        }
        var shell = document.querySelector("[data-chat-shell]");
        if (shell && shell.classList.contains("sidebar-open")) {
          shell.classList.remove("sidebar-open");
          return;
        }
        var menu = root.querySelector("[data-chat-menu]");
        if (menu && menu.open) {
          menu.open = false;
          return;
        }
        if (opts.isBusy && opts.isBusy() && opts.onCancel) {
          ev.preventDefault();
          opts.onCancel();
        }
        return;
      }
      if (ev.key === "Tab" && ev.shiftKey && !ev.ctrlKey && !ev.altKey && !ev.metaKey) {
        if (!opts.onCycleMode) return;
        var active = document.activeElement;
        if (active && root.contains(active)) {
          ev.preventDefault();
          opts.onCycleMode(function (id) { open(id); });
        }
      }
    });

    window.addEventListener("resize", function () {
      if (openId && chipEls[openId]) positionPopover(chipEls[openId]);
    });

    sync();
    return { sync: sync, close: close, open: open };
  }

  window.BatonChat = {
    esc: esc, md: md, createScroller: createScroller,
    initSidebar: initSidebar, initHeader: initHeader,
    initComposerControls: initComposerControls,
    createAttachments: createAttachments, diffBlock: diffBlock,
    registerPendingPerm: registerPendingPerm,
    unregisterPendingPerm: unregisterPendingPerm,
    setPermButton: setPermButton,
    createThinkTimer: createThinkTimer
  };

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
