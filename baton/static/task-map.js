(function () {
  "use strict";

  var root = document.getElementById("task-map-root");
  var dataEl = document.getElementById("task-map-data");
  var strEl = document.getElementById("task-map-strings");
  if (!root || !dataEl || !strEl) return;

  var S = JSON.parse(strEl.textContent || "{}");
  var csrf = root.dataset.csrf || "";

  var W = 216, H = 86, GX = 66, GY = 12, COLW = W + GX;
  var LANE_HEAD = 30, LANE_PAD = 12, BAND_GAP = 14, LEFT = 20, TOP = 16, SUB_H = 22;
  var NS = "http://www.w3.org/2000/svg";

  var state = JSON.parse(dataEl.textContent || "{}");
  var confirmed = cloneData(state);
  var undoStack = [];
  var redoStack = [];
  var lastLayout = null;
  var saveChain = Promise.resolve();
  var pendingSaves = 0;

  var ui = {
    selected: null,
    selEdge: null,
    hover: null,
    hideDone: false,
    compact: true,
    zoom: 1,
    expanded: new Set(),
    filter: "",
    md: false,
    syncState: "", // "", "saving", "saved", "err"
    armedDelete: false,
  };

  var barEl = document.getElementById("tm-bar");
  var bannerEl = document.getElementById("tm-banner");
  var canvasEl = document.getElementById("tm-canvas");
  var sizerEl = document.getElementById("tm-sizer");
  var stageEl = document.getElementById("tm-stage");
  var lanesEl = document.getElementById("tm-lanes");
  var edgesEl = document.getElementById("tm-edges");
  var cardsEl = document.getElementById("tm-cards");
  var dropIndEl = document.getElementById("tm-drop-ind");
  var panelEl = document.getElementById("tm-panel");
  var mdpEl = document.getElementById("tm-mdp");
  var toastEl = document.getElementById("tm-toast");
  var mainEl = document.getElementById("tm-main");
  var hintEl = document.getElementById("tm-hint");

  // Mobile "Show map anyway" toggle
  var showMobileBtn = document.getElementById("tm-show-mobile");
  var mobileNotice = document.getElementById("tm-mobile-notice");
  if (showMobileBtn) {
    showMobileBtn.addEventListener("click", function () {
      root.classList.add("tm-force-mobile");
      if (mobileNotice) mobileNotice.hidden = true;
      render();
    });
  }

  function cloneData(src) {
    return JSON.parse(JSON.stringify({
      groups: src.groups || [],
      tasks: src.tasks || [],
      sha: src.sha || "",
      enabled: !!src.enabled,
      can_write: !!src.can_write,
      next_markdown: src.next_markdown || "",
      problems: src.problems || [],
      running: src.running || {},
    }));
  }

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  function encodePid(pid) {
    return String(pid || "").split("/").map(encodeURIComponent).join("/");
  }

  function canEdit() {
    return !!(state.enabled && state.can_write);
  }

  function T(id) {
    for (var i = 0; i < state.tasks.length; i++) {
      if (state.tasks[i].id === id) return state.tasks[i];
    }
    return null;
  }

  function G(id) {
    for (var i = 0; i < state.groups.length; i++) {
      if (state.groups[i].id === id) return state.groups[i];
    }
    return null;
  }

  function todayIso() {
    var d = new Date();
    return d.getFullYear() + "-" + String(d.getMonth() + 1).padStart(2, "0") + "-" + String(d.getDate()).padStart(2, "0");
  }

  function statusOf(t) {
    if (t.done) return "done";
    var run = state.running && state.running[t.id];
    if (run) return run.status === "queued" ? "queued" : "running";
    for (var i = 0; i < (t.deps || []).length; i++) {
      var dep = T(t.deps[i]);
      if (dep && !dep.done) return "blocked";
    }
    if (t.owner) return "owner";
    return "ready";
  }

  function statusLabel(s) {
    return S["st_" + s] || s;
  }

  function nextId(gid) {
    var n = 0;
    var re = /^([A-Z][A-Z0-9]{0,3})-(\d+)$/;
    for (var i = 0; i < state.tasks.length; i++) {
      var m = state.tasks[i].id.match(re);
      if (m && m[1] === gid) n = Math.max(n, parseInt(m[2], 10));
    }
    return gid + "-" + String(n + 1).padStart(2, "0");
  }

  function dependsOn(a, b, seen) {
    seen = seen || new Set();
    var t = T(a);
    if (!t) return false;
    var deps = t.deps || [];
    for (var i = 0; i < deps.length; i++) {
      var d = deps[i];
      if (d === b) return true;
      if (!seen.has(d)) {
        seen.add(d);
        if (dependsOn(d, b, seen)) return true;
      }
    }
    return false;
  }

  function linkProblem(src, tgt) {
    if (src === tgt) return S.err_self;
    var target = T(tgt);
    if (!target) return S.err_exists;
    if ((target.deps || []).indexOf(src) !== -1) return S.err_exists;
    if (dependsOn(src, tgt)) return S.err_cycle;
    return null;
  }

  function dependents(id) {
    return state.tasks.filter(function (t) {
      return (t.deps || []).indexOf(id) !== -1;
    }).map(function (t) { return t.id; });
  }

  function chainOf(id) {
    var set = new Set([id]);
    function up(x) {
      var t = T(x);
      if (!t) return;
      (t.deps || []).forEach(function (d) {
        if (!set.has(d)) { set.add(d); up(d); }
      });
    }
    function down(x) {
      dependents(x).forEach(function (d) {
        if (!set.has(d)) { set.add(d); down(d); }
      });
    }
    up(id);
    down(id);
    return set;
  }

  function parseRecommended(desc) {
    var m = (desc || "").match(/^\s*(?:recommended|recomendado)\s*:\s*(.+?)\s*$/im);
    if (!m) return { rec: {}, rest: desc || "" };
    var parts = m[1].split(",").map(function (x) { return x.trim(); });
    var map = {
      "claude": "claude-code", "claude code": "claude-code", "claude-code": "claude-code",
      "codex": "codex", "gemini": "gemini", "antigravity": "gemini", "agy": "gemini",
      "opencode": "opencode", "local": "local", "local ai": "local", "ia local": "local",
    };
    var agent = map[(parts[0] || "").toLowerCase()] || "";
    var rest = (desc || "").replace(m[0], "").replace(/\n{3,}/g, "\n\n").trim();
    if (!agent) return { rec: {}, rest: rest };
    return {
      rec: { agent: agent, model: parts[1] || "", effort: (parts[2] || "").toLowerCase() },
      rest: rest,
    };
  }

  function parseRepoLine(desc) {
    var m = (desc || "").match(/^\s*(?:repo|repositorio)\s*:\s*([\w.-]+\/[\w.-]+)\s*$/im);
    if (!m) return { repo: "", rest: desc || "" };
    var rest = (desc || "").replace(m[0], "").replace(/\n{3,}/g, "\n\n").trim();
    return { repo: m[1], rest: rest };
  }

  function launchHref(t) {
    var p1 = parseRecommended(t.desc || "");
    var p2 = parseRepoLine(p1.rest);
    var rec = p1.rec.agent ? p1.rec : (t.recommended || {});
    var targetRepo = p2.repo || t.repo || "";
    var cleanDesc = p2.rest;
    if (t.subs && t.subs.length) {
      var checklist = t.subs.map(function (s) {
        return "- [" + (s.done ? "x" : " ") + "] " + s.text;
      }).join("\n");
      cleanDesc = cleanDesc ? (cleanDesc + "\n\n" + checklist) : checklist;
    }
    var description = cleanDesc || (state.provisional_details || "%s").replace("%s", t.title);
    if (targetRepo && targetRepo.toLowerCase() !== (state.repo || "").toLowerCase()) {
      var header = (state.from_project_fmt || "%s (%s)")
        .replace("%s", state.name || state.repo)
        .replace("%s", state.repo + "/" + state.path);
      description = header + "\n\n" + description;
    } else if (state.path && state.path !== "STATUS.md") {
      description = state.path + "\n\n" + description;
    }
    var q = new URLSearchParams();
    q.set("repo", targetRepo || state.repo);
    q.set("title", "[" + t.id + "] " + t.title);
    q.set("details", description);
    if (rec && rec.agent) {
      q.set("agent", rec.agent);
      if (rec.model) q.set("model", rec.model);
      if (rec.effort) q.set("effort", rec.effort);
    }
    return "/tasks/new?" + q.toString();
  }

  function serializePreview() {
    var valid = new Set(state.tasks.map(function (t) { return t.id; }));
    var out = ["## Next"];
    state.groups.forEach(function (g) {
      out.push("", "### " + g.name + " (" + g.id + ")");
      state.tasks.forEach(function (t) {
        if (t.group !== g.id) return;
        var parts = ["- [" + (t.done ? "x" : " ") + "] [" + t.id + "]"];
        if (t.done && t.date) parts.push(t.date);
        if (t.owner) parts.push(state.owner_tag || "(Owner)");
        parts.push(t.title);
        var deps = (t.deps || []).filter(function (d) { return valid.has(d); });
        var line = parts.join(" ");
        if (deps.length) line += " (after: " + deps.join(", ") + ")";
        out.push(line);
        if (t.desc) {
          t.desc.split("\n").forEach(function (dl) {
            out.push(dl.trim() ? "  " + dl : "");
          });
        }
        (t.subs || []).forEach(function (s) {
          out.push("  - [" + (s.done ? "x" : " ") + "] " + s.text);
        });
      });
    });
    return out.join("\n") + "\n";
  }

  // --------------------------------------------------------- toast & persistence
  var toastTimer = null;
  function showToast(text, isErr, withUndo) {
    toastEl.className = "tm-toast" + (isErr ? " err" : "");
    toastEl.innerHTML = "<span>" + esc(text) + "</span>" +
      (withUndo ? '<button type="button" id="tm-toast-undo">' + esc(S.undo) + "</button>" : "");
    toastEl.hidden = false;
    if (withUndo) {
      var btn = document.getElementById("tm-toast-undo");
      if (btn) btn.onclick = function () { undo(); toastEl.hidden = true; };
    }
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { toastEl.hidden = true; }, isErr ? 4200 : 2600);
  }

  function applyServerPayload(payload) {
    state.groups = payload.groups || [];
    state.tasks = payload.tasks || [];
    state.sha = payload.sha || state.sha;
    state.enabled = !!payload.enabled;
    state.can_write = !!payload.can_write;
    state.running = payload.running || {};
    state.problems = payload.problems || [];
    state.next_markdown = payload.next_markdown || "";
    confirmed = cloneData(state);
    fixSelection();
  }

  function fixSelection() {
    if (ui.selected && !T(ui.selected)) ui.selected = null;
    if (ui.selEdge) {
      var tgt = T(ui.selEdge.to);
      if (!tgt || (tgt.deps || []).indexOf(ui.selEdge.from) === -1) ui.selEdge = null;
    }
  }

  function enqueueSave(msg, groupsSnap, tasksSnap) {
    pendingSaves += 1;
    ui.syncState = "saving";
    renderSync();

    saveChain = saveChain.then(function () {
      return fetch("/api/projects/" + encodePid(state.pid) + "/map", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-Baton-CSRF": csrf,
        },
        body: JSON.stringify({
          sha: state.sha,
          message: msg,
          groups: groupsSnap,
          tasks: tasksSnap,
        }),
      }).then(function (res) {
        return res.json().catch(function () { return {}; }).then(function (data) {
          return { status: res.status, ok: res.ok, data: data };
        });
      }).then(function (r) {
        if (r.ok) {
          applyServerPayload(r.data);
          ui.syncState = "saved";
          render();
          return;
        }
        if (r.status === 409 && r.data && r.data.map) {
          applyServerPayload(r.data.map);
          undoStack.length = 0;
          redoStack.length = 0;
          ui.syncState = "err";
          showToast(S.conflict, true, false);
          render();
          return;
        }
        // Roll back optimistic edit on validation or GitHub error.
        state.groups = cloneData(confirmed).groups;
        state.tasks = cloneData(confirmed).tasks;
        fixSelection();
        ui.syncState = "err";
        showToast((r.data && r.data.detail) || ("Error (" + r.status + ")"), true, false);
        render();
      }).catch(function () {
        state.groups = cloneData(confirmed).groups;
        state.tasks = cloneData(confirmed).tasks;
        fixSelection();
        ui.syncState = "err";
        showToast("Network error while saving STATUS.md", true, false);
        render();
      }).finally(function () {
        pendingSaves = Math.max(0, pendingSaves - 1);
        if (pendingSaves === 0 && ui.syncState === "saving") ui.syncState = "saved";
        renderSync();
      });
    });
  }

  function commit(msg, mutate) {
    if (!canEdit()) return false;
    var before = JSON.stringify({ groups: state.groups, tasks: state.tasks });
    mutate();
    var after = JSON.stringify({ groups: state.groups, tasks: state.tasks });
    if (after === before) { render(); return false; }
    undoStack.push(before);
    redoStack.length = 0;
    var snap = JSON.parse(after);
    render();
    showToast(msg, false, true);
    enqueueSave(msg, snap.groups, snap.tasks);
    return true;
  }

  function undo() {
    if (!canEdit() || !undoStack.length) return;
    redoStack.push(JSON.stringify({ groups: state.groups, tasks: state.tasks }));
    var prev = JSON.parse(undoStack.pop());
    state.groups = prev.groups;
    state.tasks = prev.tasks;
    fixSelection();
    render();
    enqueueSave("Undo last change", prev.groups, prev.tasks);
  }

  function redo() {
    if (!canEdit() || !redoStack.length) return;
    undoStack.push(JSON.stringify({ groups: state.groups, tasks: state.tasks }));
    var next = JSON.parse(redoStack.pop());
    state.groups = next.groups;
    state.tasks = next.tasks;
    fixSelection();
    render();
    enqueueSave("Redo change", next.groups, next.tasks);
  }

  function enableMap() {
    if (!state.can_write) return;
    ui.syncState = "saving";
    renderSync();
    fetch("/api/projects/" + encodePid(state.pid) + "/map/enable", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-Baton-CSRF": csrf,
      },
      body: JSON.stringify({ sha: state.sha }),
    }).then(function (res) {
      return res.json().catch(function () { return {}; }).then(function (data) {
        return { status: res.status, ok: res.ok, data: data };
      });
    }).then(function (r) {
      if (r.ok) {
        applyServerPayload(r.data);
        ui.syncState = "saved";
        showToast(S.saved, false, false);
        render();
      } else if (r.status === 409 && r.data && r.data.map) {
        applyServerPayload(r.data.map);
        ui.syncState = "err";
        showToast(S.conflict, true, false);
        render();
      } else {
        ui.syncState = "err";
        showToast((r.data && r.data.detail) || ("Error (" + r.status + ")"), true, false);
        renderSync();
      }
    }).catch(function () {
      ui.syncState = "err";
      showToast("Network error", true, false);
      renderSync();
    });
  }

  function reloadFromServer() {
    ui.syncState = "saving";
    renderSync();
    fetch("/api/projects/" + encodePid(state.pid) + "/map?refresh=1")
      .then(function (res) { return res.ok ? res.json() : Promise.reject(res.status); })
      .then(function (data) {
        applyServerPayload(data);
        undoStack.length = 0;
        redoStack.length = 0;
        ui.syncState = "saved";
        render();
      })
      .catch(function () {
        ui.syncState = "err";
        renderSync();
      });
  }

  // --------------------------------------------------------- mutations
  function newTask(gid, deps) {
    if (!canEdit()) return;
    var id = nextId(gid);
    ui.selected = id;
    ui.selEdge = null;
    deps = deps || [];
    commit("Add " + id + (deps.length ? " after " + deps.join(", ") : ""), function () {
      state.tasks.push({
        id: id,
        group: gid,
        title: S.new_task_default || "New task",
        done: false,
        date: null,
        owner: false,
        deps: deps.slice(),
        desc: "",
        subs: [],
        recommended: {},
        repo: "",
      });
    });
    setTimeout(function () {
      var inp = document.getElementById("tm-p-title");
      if (inp) { inp.focus(); inp.select(); }
    }, 0);
  }

  function link(src, tgt) {
    if (!canEdit()) return;
    var p = linkProblem(src, tgt);
    if (p) { showToast(p, true, false); return; }
    commit("Link " + src + " -> " + tgt, function () {
      T(tgt).deps.push(src);
    });
  }

  function unlink(src, tgt) {
    if (!canEdit()) return;
    ui.selEdge = null;
    commit("Unlink " + src + " -> " + tgt, function () {
      var t = T(tgt);
      t.deps = (t.deps || []).filter(function (d) { return d !== src; });
    });
  }

  function deleteTask(id) {
    if (!canEdit()) return;
    ui.selected = null;
    commit("Delete " + id, function () {
      state.tasks = state.tasks.filter(function (t) { return t.id !== id; });
      state.tasks.forEach(function (t) {
        t.deps = (t.deps || []).filter(function (d) { return d !== id; });
      });
    });
  }

  function setDone(id, done) {
    if (!canEdit()) return;
    commit((done ? "Complete " : "Reopen ") + id, function () {
      var t = T(id);
      t.done = done;
      t.date = done ? todayIso() : null;
    });
  }

  // --------------------------------------------------------- layout engine
  function visible(t) {
    return !(ui.hideDone && t.done);
  }

  function cardH(t) {
    return H + (ui.expanded.has(t.id) && t.subs && t.subs.length ? t.subs.length * SUB_H + 9 : 0);
  }

  function layout() {
    var vis = state.tasks.filter(visible);
    var visIds = new Set(vis.map(function (t) { return t.id; }));
    var depth = {};
    var visiting = new Set();

    function calcDepth(t) {
      if (depth[t.id] != null) return depth[t.id];
      if (visiting.has(t.id)) return 0;
      visiting.add(t.id);
      var m = -1;
      (t.deps || []).forEach(function (id) {
        if (visIds.has(id)) {
          var dep = T(id);
          if (dep) m = Math.max(m, calcDepth(dep));
        }
      });
      visiting.delete(t.id);
      depth[t.id] = m + 1;
      return depth[t.id];
    }
    vis.forEach(calcDepth);

    var lanes = state.groups.map(function (g, idx) {
      var ts = vis.filter(function (t) { return t.group === g.id; });
      var cols = {};
      ts.forEach(function (t) {
        var k = depth[t.id];
        if (!cols[k]) cols[k] = [];
        cols[k].push(t);
      });
      var ks = Object.keys(cols).map(Number);
      var min = ks.length ? Math.min.apply(null, ks) : 0;
      var max = ks.length ? Math.max.apply(null, ks) : 0;
      var h = 0;
      ks.forEach(function (k) {
        var colH = cols[k].reduce(function (a, t) { return a + cardH(t) + GY; }, 0) - GY;
        h = Math.max(h, colH);
      });
      if (!ks.length) h = 44;
      return { g: g, idx: idx, ts: ts, cols: cols, min: min, max: max, h: h };
    });

    var bands = [];
    lanes.forEach(function (L) {
      var band = null;
      if (ui.compact) {
        for (var i = 0; i < bands.length; i++) {
          var fits = bands[i].lanes.every(function (o) {
            return L.max < o.min || L.min > o.max;
          });
          if (fits) { band = bands[i]; break; }
        }
      }
      if (!band) { band = { lanes: [] }; bands.push(band); }
      band.lanes.push(L);
    });

    var pos = {};
    var y = TOP;
    var maxX = COLW;
    bands.forEach(function (b) {
      var bh = Math.max.apply(null, b.lanes.map(function (L) { return L.h; })) + LANE_HEAD + LANE_PAD * 2;
      b.lanes.forEach(function (L) {
        L.x = LEFT + L.min * COLW;
        L.y = y;
        L.w = (L.max - L.min) * COLW + W + LANE_PAD * 2;
        L.hh = bh;
        Object.keys(L.cols).forEach(function (k) {
          var cy = y + LANE_HEAD + LANE_PAD;
          L.cols[k].forEach(function (t) {
            pos[t.id] = { x: LEFT + LANE_PAD + Number(k) * COLW, y: cy, h: cardH(t), depth: Number(k) };
            cy += cardH(t) + GY;
          });
        });
        maxX = Math.max(maxX, L.x + L.w);
      });
      y += bh + BAND_GAP;
    });

    return { lanes: lanes, pos: pos, w: maxX + COLW, h: Math.max(420, y + 70) };
  }

  function laneAt(pt) {
    if (!lastLayout) return null;
    for (var i = 0; i < lastLayout.lanes.length; i++) {
      var L = lastLayout.lanes[i];
      if (pt.x >= L.x && pt.x <= L.x + L.w && pt.y >= L.y && pt.y <= L.y + L.hh) return L;
    }
    return null;
  }

  function curve(x1, y1, x2, y2) {
    var dx = Math.max(30, Math.abs(x2 - x1) * 0.5);
    return "M" + x1 + "," + y1 + " C" + (x1 + dx) + "," + y1 + " " + (x2 - dx) + "," + y2 + " " + x2 + "," + y2;
  }

  // --------------------------------------------------------- render
  function render() {
    root.classList.toggle("tm-editable", canEdit());
    root.classList.toggle("tm-readonly", !canEdit());
    lastLayout = layout();
    renderBanner();
    renderBar();
    renderMap(lastLayout);
    renderPanel();
    renderMd();
    applyHighlight();
    if (hintEl) hintEl.hidden = !canEdit();
  }

  function renderSync() {
    var el = document.getElementById("tm-sync");
    if (!el) return;
    el.className = "tm-sync" + (ui.syncState ? " " + ui.syncState : "");
    if (ui.syncState === "saving") el.textContent = S.saving;
    else if (ui.syncState === "saved") el.textContent = "✓ " + S.saved;
    else if (ui.syncState === "err") el.textContent = "!";
    else el.textContent = "";
  }

  function renderBanner() {
    var html = "";
    if (!state.enabled) {
      html += '<div class="tm-banner"><div class="tm-banner-copy"><strong>' + esc(S.enable_title) + "</strong><span>" +
        esc(state.can_write ? S.enable_desc : S.readonly_token) + "</span></div>" +
        (state.can_write ? '<button type="button" class="tm-btn primary" id="tm-enable-btn">' + esc(S.enable_btn) + "</button>" : "") +
        "</div>";
    } else if (!state.can_write) {
      html += '<div class="tm-banner tm-banner-warn"><div class="tm-banner-copy"><span>' + esc(S.readonly_token) + "</span></div></div>";
    }
    if (state.problems && state.problems.length) {
      html += '<div class="tm-banner tm-banner-warn"><div class="tm-banner-copy"><strong>' + esc(S.problems) + "</strong><span>" +
        esc(state.problems.join(" · ")) + "</span></div></div>";
    }
    bannerEl.innerHTML = html;
    bannerEl.hidden = !html;
    var enableBtn = document.getElementById("tm-enable-btn");
    if (enableBtn) enableBtn.onclick = enableMap;
  }

  function renderBar() {
    var counts = { ready: 0, blocked: 0, owner: 0, running: 0, done: 0 };
    state.tasks.forEach(function (t) {
      var st = statusOf(t);
      if (st === "queued") st = "running";
      counts[st] = (counts[st] || 0) + 1;
    });
    var focused = document.activeElement && document.activeElement.id === "tm-search";
    var editable = canEdit();

    barEl.innerHTML =
      '<div class="tm-chips">' +
        '<span class="tm-chip"><b>' + counts.ready + "</b> " + esc(S.ready_p) + "</span>" +
        '<span class="tm-chip"><b>' + counts.blocked + "</b> " + esc(S.blocked_p) + "</span>" +
        (counts.owner ? '<span class="tm-chip"><b>' + counts.owner + "</b> " + esc(S.owner_p) + "</span>" : "") +
        (counts.running ? '<span class="tm-chip"><b>' + counts.running + "</b> " + esc(S.running_p) + "</span>" : "") +
        '<span class="tm-chip"><b>' + counts.done + "</b> " + esc(S.done_p) + "</span>" +
      "</div>" +
      '<div class="tm-spacer"></div>' +
      '<span id="tm-sync" class="tm-sync"></span>' +
      '<input class="tm-search" id="tm-search" placeholder="' + esc(S.filter) + '" value="' + esc(ui.filter) + '">' +
      '<button type="button" class="tm-btn' + (ui.hideDone ? " on" : "") + '" id="tm-hide">' + esc(S.hide_done) + "</button>" +
      '<button type="button" class="tm-btn' + (ui.compact ? " on" : "") + '" id="tm-compact">' + esc(S.compact) + "</button>" +
      '<span class="tm-seg"><button type="button" id="tm-zout" aria-label="Zoom out">−</button><span class="tm-zoomv">' + Math.round(ui.zoom * 100) + '%</span><button type="button" id="tm-zin" aria-label="Zoom in">+</button></span>' +
      (editable ? '<button type="button" class="tm-btn" id="tm-undo"' + (undoStack.length ? "" : " disabled") + ' title="' + esc(S.undo) + ' (Ctrl+Z)">↶</button>' : "") +
      (editable ? '<button type="button" class="tm-btn" id="tm-redo"' + (redoStack.length ? "" : " disabled") + ' title="' + esc(S.redo) + ' (Ctrl+Shift+Z)">↷</button>' : "") +
      '<button type="button" class="tm-btn" id="tm-reload" title="' + esc(S.reload) + '">↻</button>' +
      '<button type="button" class="tm-btn' + (ui.md ? " on" : "") + '" id="tm-md">' + esc(S.source) + "</button>" +
      (editable ? '<button type="button" class="tm-btn" id="tm-group">' + esc(S.new_group) + "</button>" : "") +
      (editable ? '<button type="button" class="tm-btn primary" id="tm-new">' + esc(S.new_task) + "</button>" : "");

    renderSync();

    var s = document.getElementById("tm-search");
    s.oninput = function () { ui.filter = s.value; applyHighlight(); };
    if (focused) { s.focus(); s.setSelectionRange(s.value.length, s.value.length); }

    document.getElementById("tm-hide").onclick = function () { ui.hideDone = !ui.hideDone; render(); };
    document.getElementById("tm-compact").onclick = function () { ui.compact = !ui.compact; render(); };
    document.getElementById("tm-zout").onclick = function () { setZoom(ui.zoom - 0.1); };
    document.getElementById("tm-zin").onclick = function () { setZoom(ui.zoom + 0.1); };
    document.getElementById("tm-reload").onclick = reloadFromServer;
    document.getElementById("tm-md").onclick = function () { ui.md = !ui.md; render(); };

    if (editable) {
      document.getElementById("tm-undo").onclick = undo;
      document.getElementById("tm-redo").onclick = redo;
      document.getElementById("tm-group").onclick = function (e) { openGroupPop(e.currentTarget); };
      document.getElementById("tm-new").onclick = function () {
        var sel = ui.selected ? T(ui.selected) : null;
        newTask(sel ? sel.group : state.groups[0].id);
      };
    }
  }

  function setZoom(z) {
    ui.zoom = Math.min(1.6, Math.max(0.4, Math.round(z * 10) / 10));
    render();
  }

  function renderMap(L) {
    stageEl.style.width = L.w + "px";
    stageEl.style.height = L.h + "px";
    stageEl.style.transform = "scale(" + ui.zoom + ")";
    sizerEl.style.width = (L.w * ui.zoom) + "px";
    sizerEl.style.height = (L.h * ui.zoom) + "px";

    var editable = canEdit();
    lanesEl.innerHTML = L.lanes.map(function (l) {
      var all = state.tasks.filter(function (t) { return t.group === l.g.id; });
      var done = all.filter(function (t) { return t.done; }).length;
      var pct = all.length ? (done / all.length * 100) : 0;
      return '<div class="tm-lane" data-g="' + esc(l.g.id) + '" style="left:' + l.x + "px;top:" + l.y + "px;width:" + l.w + "px;height:" + l.hh + 'px">' +
        '<div class="tm-lane-head">' +
          '<span class="tm-lane-dot c' + (l.idx % 6) + '"></span>' +
          '<span class="tm-lane-name"' + (editable ? ' data-act="rename" title="' + esc(S.rename_hint) + '"' : "") + ">" + esc(l.g.name) + "</span>" +
          '<span class="tm-lane-pfx">' + esc(l.g.id) + "</span>" +
          '<span class="tm-lane-count">' + done + "/" + all.length + "</span>" +
          '<span class="tm-lane-bar"><i style="width:' + pct + '%"></i></span>' +
          (editable ? '<button type="button" class="tm-ibtn" data-act="add-lane" data-g="' + esc(l.g.id) + '" title="' + esc(S.add_in_group) + '">+</button>' : "") +
          (editable && !all.length && state.groups.length > 1 ? '<button type="button" class="tm-ibtn" data-act="lane-del" data-g="' + esc(l.g.id) + '" title="' + esc(S.del_group) + '">✕</button>' : "") +
        "</div></div>";
    }).join("");

    cardsEl.innerHTML = state.tasks.filter(function (t) { return !!L.pos[t.id]; }).map(function (t) {
      var p = L.pos[t.id];
      var st = statusOf(t);
      var subs = t.subs || [];
      var subsDone = subs.filter(function (x) { return x.done; }).length;
      var open = ui.expanded.has(t.id);
      var rec = parseRecommended(t.desc || "").rec;
      var ag = rec.agent || (t.recommended && t.recommended.agent) || "";
      return '<div class="tm-card tm-s-' + st + (ui.selected === t.id ? " sel" : "") + '" data-id="' + esc(t.id) + '" style="left:' + p.x + "px;top:" + p.y + "px;width:" + W + "px;height:" + p.h + 'px">' +
        '<div class="tm-c-top"><span class="tm-c-id">' + esc(t.id) + '</span><span class="tm-badge">' + esc(statusLabel(st)) + "</span></div>" +
        '<div class="tm-c-title" title="' + esc(t.title) + '">' + esc(t.title) + "</div>" +
        '<div class="tm-c-foot">' +
          (subs.length ? '<button type="button" class="tm-c-subs" data-act="subs" data-id="' + esc(t.id) + '" title="' + esc(S.toggle_subs) + '">☑ ' + subsDone + "/" + subs.length + " " + (open ? "▴" : "▾") + "</button>" : "") +
          (t.owner ? "<span>" + esc(state.owner_tag || "(Owner)") + "</span>" : "") +
          (ag ? '<span class="tm-c-agent">· ' + esc(ag) + "</span>" : "") +
          (t.done && t.date ? "<span>" + esc(t.date) + "</span>" : "") +
        "</div>" +
        (open && subs.length ? '<ul class="tm-c-sublist">' + subs.map(function (x, i) {
          return '<li class="' + (x.done ? "done" : "") + '"><input type="checkbox" data-act="sub" data-id="' + esc(t.id) + '" data-i="' + i + '"' + (x.done ? " checked" : "") + (editable ? "" : " disabled") + "><span>" + esc(x.text) + "</span></li>";
        }).join("") + "</ul>" : "") +
        '<span class="tm-port in" title="' + esc(S.port_in) + '"></span>' +
        '<span class="tm-port out" title="' + esc(S.port_out) + '"></span>' +
        (editable ? '<button type="button" class="tm-c-plus" data-act="plus" data-id="' + esc(t.id) + '" title="' + esc(S.add_after) + '">+</button>' : "") +
      "</div>";
    }).join("");

    var svg = '<defs>' +
      '<marker id="tm-arr" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto"><path d="M0,0L10,5L0,10z" class="tm-edge-arr"/></marker>' +
      '<marker id="tm-arrH" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto"><path d="M0,0L10,5L0,10z" class="tm-edge-arr-h"/></marker>' +
      '<marker id="tm-arrS" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto"><path d="M0,0L10,5L0,10z" class="tm-edge-arr-s"/></marker></defs>';
    var delBtn = "";

    state.tasks.forEach(function (t) {
      var b = L.pos[t.id];
      if (!b) return;
      (t.deps || []).forEach(function (dId) {
        var a = L.pos[dId];
        if (!a) return;
        var path = curve(a.x + W, a.y + 42, b.x - 2, b.y + 42);
        var isSel = ui.selEdge && ui.selEdge.from === dId && ui.selEdge.to === t.id;
        var depTask = T(dId);
        svg += '<path class="tm-edge' + (depTask && depTask.done ? " from-done" : "") + (isSel ? " sel" : "") + '" data-from="' + esc(dId) + '" data-to="' + esc(t.id) + '" d="' + path + '" marker-end="url(#' + (isSel ? "tm-arrS" : "tm-arr") + ')"/>';
        if (editable) {
          svg += '<path class="tm-edge-hit" data-from="' + esc(dId) + '" data-to="' + esc(t.id) + '" d="' + path + '"/>';
        }
        if (isSel && editable) {
          delBtn = '<button type="button" class="tm-edge-del" data-act="edge-del" style="left:' + ((a.x + W + b.x) / 2) + "px;top:" + ((a.y + b.y) / 2 + 42) + 'px">✕ ' + esc(S.unlink) + " " + esc(dId) + " → " + esc(t.id) + "</button>";
        }
      });
    });

    edgesEl.setAttribute("width", String(L.w));
    edgesEl.setAttribute("height", String(L.h));
    edgesEl.innerHTML = svg;
    if (delBtn) cardsEl.insertAdjacentHTML("beforeend", delBtn);
  }

  function applyHighlight() {
    var focus = ui.hover || ui.selected;
    var chain = focus && T(focus) ? chainOf(focus) : null;
    var q = ui.filter.trim().toLowerCase();
    cardsEl.querySelectorAll(".tm-card").forEach(function (c) {
      var id = c.dataset.id;
      var t = T(id);
      c.classList.toggle("dim", !!(chain && !chain.has(id)));
      c.classList.toggle("fout", !!(q && t && (t.id + " " + t.title + " " + (t.desc || "")).toLowerCase().indexOf(q) === -1));
    });
    edgesEl.querySelectorAll("path.tm-edge").forEach(function (p) {
      if (p.classList.contains("sel")) return;
      var on = !!(chain && chain.has(p.dataset.from) && chain.has(p.dataset.to));
      p.classList.toggle("hl", on);
      p.classList.toggle("dim", !!(chain && !on));
      p.setAttribute("marker-end", on ? "url(#tm-arrH)" : "url(#tm-arr)");
    });
  }

  // --------------------------------------------------------- side panel
  function renderPanel() {
    var t = ui.selected ? T(ui.selected) : null;
    if (!t) { panelEl.hidden = true; return; }
    var activeId = document.activeElement ? document.activeElement.id : "";
    panelEl.hidden = false;
    var editable = canEdit();
    var dis = editable ? "" : " disabled";
    var st = statusOf(t);
    var blocks = dependents(t.id);
    var candidates = state.tasks.filter(function (x) {
      return x.id !== t.id && !linkProblem(x.id, t.id);
    });
    var run = state.running && state.running[t.id];
    var runUrl = run && typeof run.url === "string" && run.url.indexOf("/tasks/") === 0 ? run.url : "";
    var subs = t.subs || [];
    var subsDone = subs.filter(function (x) { return x.done; }).length;

    panelEl.innerHTML =
      '<div class="tm-p-head"><span class="tm-c-id">' + esc(t.id) + '</span><span class="tm-badge st-' + st + '">' + esc(statusLabel(st)) + '</span><span class="tm-spacer"></span><button type="button" class="tm-ibtn" id="tm-p-close" aria-label="Close">✕</button></div>' +
      '<label class="tm-f"><span>' + esc(S.title) + '</span><input type="text" id="tm-p-title" maxlength="200" value="' + esc(t.title) + '"' + dis + "></label>" +
      '<label class="tm-chk"><input type="checkbox" id="tm-p-owner"' + (t.owner ? " checked" : "") + dis + "><span>" + esc((S.owner_chk || "%s").replace("%s", state.owner_tag || "(Owner)")) + "</span></label>" +
      '<label class="tm-f"><span>' + esc(S.group) + '</span><select id="tm-p-group"' + dis + ">" +
        state.groups.map(function (g) {
          return '<option value="' + esc(g.id) + '"' + (g.id === t.group ? " selected" : "") + ">" + esc(g.name) + " (" + esc(g.id) + ")</option>";
        }).join("") +
      "</select></label>" +
      '<label class="tm-f"><span>' + esc(S.desc) + '</span><textarea id="tm-p-desc" maxlength="5000" placeholder="' + esc(S.desc_ph) + '"' + dis + ">" + esc(t.desc || "") + "</textarea></label>" +
      '<div class="tm-f"><span>' + esc(S.deps) + '</span><div class="tm-deps">' +
        ((t.deps || []).length ? t.deps.map(function (d) {
          var dt = T(d);
          return '<span class="tm-dep"><span title="' + esc(dt ? dt.title : d) + '"><b class="tm-mono">' + esc(d) + "</b> " + esc(dt ? dt.title : "") + "</span>" +
            (editable ? '<button type="button" data-undep="' + esc(d) + '" title="' + esc(S.unlink) + '">✕</button>' : "") + "</span>";
        }).join("") : '<span class="tm-muted">' + esc(S.deps_none) + "</span>") +
      "</div>" +
        (editable ? '<select id="tm-p-add-dep"><option value="">' + esc(S.dep_add) + "</option>" +
          candidates.map(function (x) {
            return '<option value="' + esc(x.id) + '">' + esc(x.id) + " · " + esc(x.title) + "</option>";
          }).join("") + "</select>" : "") +
      "</div>" +
      '<div class="tm-f"><span>' + esc(S.blocks) + '</span><div class="tm-deps">' +
        (blocks.length ? blocks.map(function (d) {
          var dt = T(d);
          return '<span class="tm-dep"><span><b class="tm-mono">' + esc(d) + "</b> " + esc(dt ? dt.title : "") + "</span>" +
            (editable ? '<button type="button" data-unblock="' + esc(d) + '" title="' + esc(S.unlink) + '">✕</button>' : "") + "</span>";
        }).join("") : '<span class="tm-muted">' + esc(S.blocks_none) + "</span>") +
      "</div></div>" +
      '<div class="tm-f"><span>' + esc(S.subs) + (subs.length ? " · " + subsDone + "/" + subs.length : "") + "</span>" +
        subs.map(function (x, i) {
          return '<div class="tm-subrow ' + (x.done ? "done" : "") + '"><input type="checkbox" data-subchk="' + i + '"' + (x.done ? " checked" : "") + dis + '><input type="text" data-subtxt="' + i + '" value="' + esc(x.text) + '"' + dis + ">" +
            (editable ? '<button type="button" class="tm-ibtn" data-subdel="' + i + '">✕</button>' : "") + "</div>";
        }).join("") +
        (editable ? '<div class="tm-subrow"><span style="width:13px"></span><input type="text" id="tm-p-subnew" placeholder="' + esc(S.sub_add) + '"></div>' : "") +
      "</div>" +
      '<div class="tm-p-actions">' +
        (editable ? '<button type="button" class="tm-btn" id="tm-p-done">' + esc(t.done ? S.reopen : S.mark_done) + "</button>" : "") +
        (runUrl ? '<a class="tm-btn primary" href="' + esc(runUrl) + '">' + esc(S.view_active) + "</a>" : "") +
        (!runUrl && st === "ready" ? '<a class="tm-btn primary" href="' + esc(launchHref(t)) + '">' + esc(S.launch) + "</a>" : "") +
        '<span class="tm-spacer"></span>' +
        (editable ? '<button type="button" class="tm-btn danger" id="tm-p-del">' + esc(S.del) + "</button>" : "") +
      "</div>";

    ui.armedDelete = false;
    var id = t.id;
    document.getElementById("tm-p-close").onclick = function () { ui.selected = null; render(); };

    if (!editable) return;

    var titleInp = document.getElementById("tm-p-title");
    titleInp.onkeydown = function (e) { if (e.key === "Enter") titleInp.blur(); };
    titleInp.onchange = function () {
      var v = titleInp.value.replace(/\s+/g, " ").trim();
      if (v) commit("Rename " + id, function () { T(id).title = v; });
      else render();
    };
    document.getElementById("tm-p-owner").onchange = function (e) {
      commit((e.target.checked ? "Mark " : "Unmark ") + id + " as owner action", function () {
        T(id).owner = e.target.checked;
      });
    };
    document.getElementById("tm-p-group").onchange = function (e) {
      var gid = e.target.value;
      commit("Move " + id + " to " + (G(gid) ? G(gid).name : gid), function () {
        T(id).group = gid;
      });
    };
    document.getElementById("tm-p-desc").onchange = function (e) {
      var val = e.target.value.replace(/\s+$/, "");
      commit("Edit " + id + " description", function () { T(id).desc = val; });
    };
    document.getElementById("tm-p-add-dep").onchange = function (e) {
      if (e.target.value) link(e.target.value, id);
    };
    panelEl.querySelectorAll("[data-undep]").forEach(function (b) {
      b.onclick = function () { unlink(b.dataset.undep, id); };
    });
    panelEl.querySelectorAll("[data-unblock]").forEach(function (b) {
      b.onclick = function () { unlink(id, b.dataset.unblock); };
    });
    panelEl.querySelectorAll("[data-subchk]").forEach(function (c) {
      c.onchange = function () {
        var idx = Number(c.dataset.subchk);
        commit((c.checked ? "Check" : "Uncheck") + " subtask of " + id, function () {
          T(id).subs[idx].done = c.checked;
        });
      };
    });
    panelEl.querySelectorAll("[data-subtxt]").forEach(function (inp) {
      inp.onkeydown = function (e) { if (e.key === "Enter") inp.blur(); };
      inp.onchange = function () {
        var idx = Number(inp.dataset.subtxt);
        var v = inp.value.replace(/\s+/g, " ").trim();
        if (v) commit("Rename subtask of " + id, function () { T(id).subs[idx].text = v; });
        else render();
      };
    });
    panelEl.querySelectorAll("[data-subdel]").forEach(function (b) {
      b.onclick = function () {
        var idx = Number(b.dataset.subdel);
        commit("Remove subtask of " + id, function () { T(id).subs.splice(idx, 1); });
      };
    });
    var sn = document.getElementById("tm-p-subnew");
    sn.onkeydown = function (e) {
      if (e.key === "Enter" && sn.value.trim()) {
        var v = sn.value.replace(/\s+/g, " ").trim();
        commit("Add subtask to " + id, function () {
          T(id).subs.push({ text: v, done: false });
        });
        setTimeout(function () {
          var el = document.getElementById("tm-p-subnew");
          if (el) el.focus();
        }, 0);
      }
    };
    document.getElementById("tm-p-done").onclick = function () { setDone(id, !T(id).done); };
    document.getElementById("tm-p-del").onclick = function (e) {
      if (!ui.armedDelete) {
        ui.armedDelete = true;
        e.target.classList.add("armed");
        e.target.textContent = S.del_confirm;
        return;
      }
      deleteTask(id);
    };
    if (activeId === "tm-p-subnew") {
      var subNew = document.getElementById("tm-p-subnew");
      if (subNew) subNew.focus();
    }
  }

  function renderMd() {
    if (!ui.md) { mdpEl.hidden = true; return; }
    mdpEl.hidden = false;
    var text = serializePreview();
    mdpEl.innerHTML =
      '<div class="tm-p-head"><b>STATUS.md · ## Next</b><span class="tm-spacer"></span><button type="button" class="tm-ibtn" id="tm-md-close" aria-label="Close">✕</button></div>' +
      '<div class="tm-muted">' + esc(S.md_note) + "</div>" +
      "<pre>" + esc(text) + "</pre>" +
      '<div><button type="button" class="tm-btn" id="tm-md-copy">' + esc(S.copy) + "</button></div>";
    document.getElementById("tm-md-close").onclick = function () { ui.md = false; render(); };
    document.getElementById("tm-md-copy").onclick = function () {
      if (navigator.clipboard) {
        navigator.clipboard.writeText(text).then(function () { showToast(S.copied, false, false); });
      }
    };
  }

  // --------------------------------------------------------- group popover
  function openGroupPop(anchor) {
    var existing = mainEl.querySelector(".tm-pop");
    if (existing) existing.remove();
    var pop = document.createElement("div");
    pop.className = "tm-pop";
    var r = anchor.getBoundingClientRect();
    var mr = mainEl.getBoundingClientRect();
    pop.style.left = Math.max(8, r.right - mr.left - 255) + "px";
    pop.style.top = "8px";
    pop.innerHTML =
      '<label class="tm-f"><span>' + esc(S.group_name) + '</span><input type="text" id="tm-g-name" maxlength="80" placeholder="API"></label>' +
      '<label class="tm-f"><span>' + esc(S.group_pfx) + '</span><input type="text" id="tm-g-pfx" class="tm-mono" maxlength="4" placeholder="AP"></label>' +
      '<div style="display:flex;gap:6px"><button type="button" class="tm-btn primary" id="tm-g-ok">' + esc(S.create) + '</button><button type="button" class="tm-btn" id="tm-g-cancel">' + esc(S.cancel) + "</button></div>";
    mainEl.appendChild(pop);

    var name = document.getElementById("tm-g-name");
    var pfx = document.getElementById("tm-g-pfx");
    var touched = false;
    name.focus();
    name.oninput = function () {
      if (!touched) pfx.value = name.value.replace(/[^A-Za-z]/g, "").slice(0, 2).toUpperCase();
    };
    pfx.oninput = function () {
      touched = true;
      pfx.value = pfx.value.toUpperCase().replace(/[^A-Z0-9]/g, "");
    };
    function ok() {
      var n = name.value.replace(/\s+/g, " ").trim();
      var p = pfx.value.trim();
      if (!n || !/^[A-Z][A-Z0-9]{0,3}$/.test(p)) { showToast(S.err_group, true, false); return; }
      if (G(p)) { showToast(S.err_group_dup, true, false); return; }
      pop.remove();
      commit("Add group " + n + " (" + p + ")", function () {
        state.groups.push({ id: p, name: n });
      });
    }
    document.getElementById("tm-g-ok").onclick = ok;
    document.getElementById("tm-g-cancel").onclick = function () { pop.remove(); };
    pop.onkeydown = function (e) {
      if (e.key === "Enter") ok();
      if (e.key === "Escape") pop.remove();
    };
  }

  // --------------------------------------------------------- pointer interactions
  function stagePt(e) {
    var r = stageEl.getBoundingClientRect();
    return { x: (e.clientX - r.left) / ui.zoom, y: (e.clientY - r.top) / ui.zoom };
  }

  var lastClick = { t: 0, x: 0, y: 0 };

  stageEl.addEventListener("pointerdown", function (e) {
    if (e.button !== 0) return;
    var port = e.target.closest(".tm-port");
    var card = e.target.closest(".tm-card");
    if (port && card && canEdit()) {
      e.preventDefault();
      startLink(card.dataset.id, port.classList.contains("out") ? "out" : "in", e);
      return;
    }
    if (e.target.closest("button,input,textarea,select,.tm-lane-rename")) return;
    var hit = e.target.closest(".tm-edge-hit");
    if (hit && canEdit()) {
      ui.selEdge = { from: hit.dataset.from, to: hit.dataset.to };
      ui.selected = null;
      render();
      return;
    }
    if (card) {
      startCardDrag(card.dataset.id, e);
      return;
    }
    startPan(e);
  });

  stageEl.addEventListener("click", function (e) {
    var b = e.target.closest("[data-act]");
    if (!b) return;
    var act = b.dataset.act;
    if (act === "add-lane") newTask(b.dataset.g);
    else if (act === "plus") newTask(T(b.dataset.id).group, [b.dataset.id]);
    else if (act === "subs") {
      var id = b.dataset.id;
      if (ui.expanded.has(id)) ui.expanded.delete(id); else ui.expanded.add(id);
      render();
    } else if (act === "edge-del" && ui.selEdge) {
      unlink(ui.selEdge.from, ui.selEdge.to);
    } else if (act === "lane-del" && canEdit()) {
      var gid = b.dataset.g;
      var grp = G(gid);
      commit("Delete empty group " + (grp ? grp.name : gid), function () {
        state.groups = state.groups.filter(function (x) { return x.id !== gid; });
      });
    }
  });

  stageEl.addEventListener("change", function (e) {
    var c = e.target.closest('[data-act="sub"]');
    if (!c || !canEdit()) return;
    var id = c.dataset.id;
    var idx = Number(c.dataset.i);
    commit((c.checked ? "Check" : "Uncheck") + " subtask of " + id, function () {
      T(id).subs[idx].done = c.checked;
    });
  });

  stageEl.addEventListener("dblclick", function (e) {
    var n = e.target.closest('[data-act="rename"]');
    if (!n || !canEdit()) return;
    var gid = n.closest(".tm-lane").dataset.g;
    var g = G(gid);
    var inp = document.createElement("input");
    inp.className = "tm-lane-rename";
    inp.maxLength = 80;
    inp.value = g.name;
    n.replaceWith(inp);
    inp.focus();
    inp.select();
    var finished = false;
    function done(save) {
      if (finished) return;
      finished = true;
      var v = inp.value.replace(/\s+/g, " ").trim();
      if (save && v && v !== g.name) {
        commit("Rename group " + gid + " to " + v, function () { G(gid).name = v; });
      } else {
        render();
      }
    }
    inp.onkeydown = function (ev) {
      if (ev.key === "Enter") done(true);
      if (ev.key === "Escape") done(false);
    };
    inp.onblur = function () { done(true); };
  });

  stageEl.addEventListener("pointerover", function (e) {
    var c = e.target.closest(".tm-card");
    var id = c ? c.dataset.id : null;
    if (id !== ui.hover) { ui.hover = id; applyHighlight(); }
  });
  stageEl.addEventListener("pointerleave", function () {
    ui.hover = null;
    applyHighlight();
  });

  function clearDropMarks() {
    cardsEl.querySelectorAll(".tm-card.drop-ok,.tm-card.drop-bad").forEach(function (c) {
      c.classList.remove("drop-ok", "drop-bad");
    });
  }

  function startLink(id, mode, e) {
    var p = lastLayout.pos[id];
    var temp = document.createElementNS(NS, "path");
    temp.setAttribute("class", "tm-edge temp");
    edgesEl.appendChild(temp);
    var start = { x: e.clientX, y: e.clientY };

    function move(ev) {
      var pt = stagePt(ev);
      var sy = p.y + 42;
      temp.setAttribute("d", mode === "out" ? curve(p.x + W, sy, pt.x, pt.y) : curve(pt.x, pt.y, p.x, sy));
      clearDropMarks();
      var el = document.elementFromPoint(ev.clientX, ev.clientY);
      var card = el && el.closest(".tm-card");
      if (card && card.dataset.id !== id) {
        var ok = mode === "out" ? !linkProblem(id, card.dataset.id) : !linkProblem(card.dataset.id, id);
        card.classList.add(ok ? "drop-ok" : "drop-bad");
      }
    }
    function up(ev) {
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", up);
      temp.remove();
      clearDropMarks();
      var el = document.elementFromPoint(ev.clientX, ev.clientY);
      var card = el && el.closest(".tm-card");
      if (card) {
        var other = card.dataset.id;
        if (other !== id) {
          if (mode === "out") link(id, other); else link(other, id);
        }
        return;
      }
      if (Math.hypot(ev.clientX - start.x, ev.clientY - start.y) < 12) return;
      if (mode === "out") {
        var lane = laneAt(stagePt(ev));
        newTask(lane ? lane.g.id : T(id).group, [id]);
      } else {
        showToast(S.err_in_drop, true, false);
      }
    }
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", up);
  }

  function dropTarget(id, pt) {
    var lane = laneAt(pt);
    if (!lane) return null;
    var P = lastLayout.pos;
    var d = P[id].depth;
    var col = (lane.cols[d] || []).filter(function (t) { return t.id !== id; });
    var before = null;
    for (var i = 0; i < col.length; i++) {
      if (pt.y < P[col[i].id].y + P[col[i].id].h / 2) { before = col[i]; break; }
    }
    var after = before ? null : (col.length ? col[col.length - 1] : null);
    var y = before ? (P[before.id].y - GY / 2 - 1)
      : after ? (P[after.id].y + P[after.id].h + GY / 2 - 1)
      : (lane.y + LANE_HEAD + LANE_PAD - 4);
    return { gid: lane.g.id, before: before ? before.id : null, after: after ? after.id : null, x: LEFT + LANE_PAD + d * COLW, y: y };
  }

  function startCardDrag(id, e) {
    var el = cardsEl.querySelector('.tm-card[data-id="' + CSS.escape(id) + '"]');
    var start = { x: e.clientX, y: e.clientY };
    var dragging = false;
    var target = null;

    function move(ev) {
      if (!canEdit()) return;
      var dx = ev.clientX - start.x;
      var dy = ev.clientY - start.y;
      if (!dragging && Math.hypot(dx, dy) < 5) return;
      dragging = true;
      el.classList.add("dragging");
      el.style.transform = "translate(" + (dx / ui.zoom) + "px," + (dy / ui.zoom) + "px)";
      target = dropTarget(id, stagePt(ev));
      if (target) {
        dropIndEl.hidden = false;
        dropIndEl.style.left = target.x + "px";
        dropIndEl.style.top = target.y + "px";
        dropIndEl.style.width = W + "px";
      } else {
        dropIndEl.hidden = true;
      }
    }
    function up() {
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", up);
      dropIndEl.hidden = true;
      if (!dragging) {
        ui.selected = id;
        ui.selEdge = null;
        render();
        return;
      }
      el.classList.remove("dragging");
      el.style.transform = "";
      if (!target) { render(); return; }
      var t = T(id);
      var moved = t.group !== target.gid;
      commit(moved ? ("Move " + id + " to " + (G(target.gid) ? G(target.gid).name : target.gid)) : ("Reorder " + id), function () {
        var arr = state.tasks;
        var task = T(id);
        arr.splice(arr.indexOf(task), 1);
        task.group = target.gid;
        if (target.before) arr.splice(arr.indexOf(T(target.before)), 0, task);
        else if (target.after) arr.splice(arr.indexOf(T(target.after)) + 1, 0, task);
        else arr.push(task);
      });
    }
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", up);
  }

  function startPan(e) {
    var sx = canvasEl.scrollLeft;
    var sy = canvasEl.scrollTop;
    var x0 = e.clientX;
    var y0 = e.clientY;
    var moved = false;
    canvasEl.classList.add("panning");

    function move(ev) {
      var dx = ev.clientX - x0;
      var dy = ev.clientY - y0;
      if (Math.hypot(dx, dy) > 3) moved = true;
      canvasEl.scrollLeft = sx - dx;
      canvasEl.scrollTop = sy - dy;
    }
    function up(ev) {
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", up);
      canvasEl.classList.remove("panning");
      if (moved) return;
      var now = Date.now();
      var dbl = now - lastClick.t < 380 && Math.hypot(ev.clientX - lastClick.x, ev.clientY - lastClick.y) < 8;
      lastClick = { t: now, x: ev.clientX, y: ev.clientY };
      if (dbl && canEdit()) {
        var lane = laneAt(stagePt(ev));
        if (lane) { newTask(lane.g.id); return; }
      }
      if (ui.selected || ui.selEdge) {
        ui.selected = null;
        ui.selEdge = null;
        render();
      }
    }
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", up);
  }

  // --------------------------------------------------------- keyboard shortcuts
  document.addEventListener("keydown", function (e) {
    var typing = e.target && e.target.closest("input,textarea,select");
    var mod = e.ctrlKey || e.metaKey;
    if (mod && e.key.toLowerCase() === "z" && !typing) {
      e.preventDefault();
      if (e.shiftKey) redo(); else undo();
      return;
    }
    if (mod && e.key.toLowerCase() === "y" && !typing) {
      e.preventDefault();
      redo();
      return;
    }
    if (typing) {
      if (e.key === "Escape") e.target.blur();
      return;
    }
    if (e.key === "/") {
      e.preventDefault();
      var s = document.getElementById("tm-search");
      if (s) s.focus();
    } else if (e.key === "Escape") {
      var pop = mainEl.querySelector(".tm-pop");
      if (pop) pop.remove();
      ui.selected = null;
      ui.selEdge = null;
      render();
    } else if ((e.key === "Delete" || e.key === "Backspace") && ui.selEdge && canEdit()) {
      unlink(ui.selEdge.from, ui.selEdge.to);
    } else if (e.key.toLowerCase() === "n" && canEdit()) {
      var sel = ui.selected ? T(ui.selected) : null;
      newTask(sel ? sel.group : state.groups[0].id);
    }
  });

  render();
})();
