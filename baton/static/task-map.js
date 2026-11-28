(function () {
  "use strict";

  var root = document.getElementById("task-map-root");
  var dataEl = document.getElementById("task-map-data");
  var strEl = document.getElementById("task-map-strings");
  if (!root || !dataEl || !strEl) return;

  var S = JSON.parse(strEl.textContent || "{}");
  var csrf = root.dataset.csrf || "";

  var W = 216, H = 86, GY = 12;
  var LANE_HEAD = 30, LANE_PAD = 12, BAND_GAP = 14, LEFT = 20, TOP = 16, SUB_H = 22;
  var FOLD_DONE_MIN = 3;
  var GAP_MIN = 34, GAP_MAX = 146, GAP_STEP = 16;
  var PREFS_KEY = "baton-tm-prefs";
  var NS = "http://www.w3.org/2000/svg";

  var state = JSON.parse(dataEl.textContent || "{}");
  var confirmed = cloneData(state);
  var undoStack = [];
  var redoStack = [];
  var lastLayout = null;
  var mapUrl = "/api/projects/" + encodePid(state.pid) + "/map";
  var pending = null;
  var saveTimer = null;
  var sending = false;
  var keepalive = false;
  var editVersion = 0;
  var refreshNeeded = false;
  var refreshing = false;
  var redrawNeeded = false;
  var pointerDown = false;
  var events = null;
  var liveStatus = null;

  function loadPrefs() {
    try {
      return JSON.parse(localStorage.getItem(PREFS_KEY) || "{}") || {};
    } catch (e) {
      return {};
    }
  }
  function savePrefs() {
    try {
      localStorage.setItem(PREFS_KEY, JSON.stringify({
        orientation: ui.orientation,
        autoOrder: ui.autoOrder,
        gap: ui.gap,
        compact: ui.compact,
        hideDone: ui.hideDone,
        foldDone: ui.foldDone,
        hideTransitive: ui.hideTransitive,
      }));
    } catch (e) {}
  }

  var phone = window.matchMedia("(width < 640px)");
  var savedPrefs = loadPrefs();
  var ui = {
    selected: null,
    selEdge: null,
    hover: null,
    hideDone: !!savedPrefs.hideDone,
    compact: savedPrefs.compact !== false,
    foldDone: savedPrefs.foldDone !== false,
    hideTransitive: savedPrefs.hideTransitive !== false,
    autoOrder: savedPrefs.autoOrder !== false,
    orientation: savedPrefs.orientation === "tb" ? "tb" : "lr",
    gap: typeof savedPrefs.gap === "number" ? Math.min(GAP_MAX, Math.max(GAP_MIN, savedPrefs.gap)) : 66,
    zoom: 1,
    expanded: new Set(),
    unfoldedLanes: new Set(),
    filter: "",
    view: phone.matches ? "board" : "graph",
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

  var graphEl = document.getElementById("tm-graph");
  var boardEl = document.getElementById("tm-board");
  var helpEl = document.getElementById("tm-help");
  var menuEl = document.getElementById("tm-menu");
  var lastSelection = null;

  phone.addEventListener("change", function () { setView(phone.matches ? "board" : "graph"); });

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

  function matchesFilter(t, q) {
    if (!q) return true;
    return (t.id + " " + t.title + " " + (t.desc || "")).toLowerCase().indexOf(q) !== -1;
  }

  // Exact DAG transitive-edge detection on the currently visible subgraph:
  // edge (dep -> task) is transitive iff another visible dependency of `task`
  // already reaches `dep`.
  function computeTransitiveEdges(visTasks) {
    var byId = {};
    visTasks.forEach(function (t) { byId[t.id] = t; });
    var ancestors = {};
    var visiting = new Set();

    function getAnc(tid) {
      if (ancestors[tid]) return ancestors[tid];
      if (visiting.has(tid)) return new Set();
      visiting.add(tid);
      var acc = new Set();
      var task = byId[tid];
      if (task) {
        (task.deps || []).forEach(function (dep) {
          if (byId[dep]) {
            acc.add(dep);
            getAnc(dep).forEach(function (a) { acc.add(a); });
          }
        });
      }
      visiting.delete(tid);
      ancestors[tid] = acc;
      return acc;
    }
    visTasks.forEach(function (t) { getAnc(t.id); });

    var redundant = new Set();
    visTasks.forEach(function (t) {
      var validDeps = (t.deps || []).filter(function (d) { return !!byId[d]; });
      validDeps.forEach(function (dep) {
        for (var i = 0; i < validDeps.length; i++) {
          var other = validDeps[i];
          if (other !== dep && ancestors[other] && ancestors[other].has(dep)) {
            redundant.add(dep + "->" + t.id);
            break;
          }
        }
      });
    });
    return redundant;
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
    state.running = liveStatus && liveStatus.sha === state.sha ? liveStatus.running : payload.running || {};
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

  function snapshot(map) {
    return JSON.stringify({groups: map.groups, tasks: map.tasks});
  }

  function editingField() {
    var field = document.activeElement;
    return field && root.contains(field) && field.id !== "tm-search" && field.matches("input, textarea, select");
  }

  // Async updates must leave a partially typed title, description or subtask alone.
  function syncWhenIdle() {
    if (pending || sending || pointerDown || mdpEl.open || editingField()) return;
    if (redrawNeeded) {
      redrawNeeded = false;
      applyServerPayload(confirmed);
      render();
    }
    if (refreshNeeded && !refreshing) reloadFromServer(false);
  }
  root.addEventListener("focusout", function () { setTimeout(syncWhenIdle, 0); });
  stageEl.addEventListener("pointerdown", function () { pointerDown = true; });
  function endPointer() {
    if (pointerDown) {
      pointerDown = false;
      setTimeout(syncWhenIdle, 0);
    }
  }
  window.addEventListener("pointerup", endPointer);
  window.addEventListener("pointercancel", endPointer);

  function enqueueSave(msg, before) {
    editVersion++;
    if (pending) pending.message = "Batch map edits";
    else pending = {base: before, message: msg};
    clearTimeout(saveTimer);
    saveTimer = setTimeout(flushSave, 1500);
    ui.syncState = "pending";
    renderSync();
  }

  async function flushSave() {
    clearTimeout(saveTimer);
    saveTimer = null;
    if (!pending || sending) return;
    var batch = pending;
    var current = snapshot(state);
    var draft = batch.unacknowledged || current;
    pending = null;
    if (current !== draft) pending = {base: draft, message: batch.message};
    if (draft === batch.base) {
      ui.syncState = "saved";
      renderSync();
      syncWhenIdle();
      return;
    }
    sending = true;
    ui.syncState = "saving";
    renderSync();
    var body = batch.retryBody || JSON.stringify(Object.assign(JSON.parse(draft), {
      sha: confirmed.sha,
      base: JSON.parse(batch.base),
      message: batch.message,
    }));
    // Browsers cap outstanding keepalive bodies at 64 KiB.
    keepalive = new Blob([body]).size <= 65536;
    try {
      var res = await fetch(mapUrl, {
        method: "POST",
        headers: {"Content-Type": "application/json", "X-Baton-CSRF": csrf},
        body: body,
        keepalive: keepalive,
      });
      var data = await res.json();
      if (res.status === 409 && data.map) {
        undoStack.length = redoStack.length = 0;
        confirmed = cloneData(data.map);
        redrawNeeded = true;
        ui.syncState = pending ? "pending" : "err";
        showToast(S.conflict, true, false);
      } else if (res.ok) {
        confirmed = cloneData(data);
        if (data.merged) undoStack.length = redoStack.length = 0;
        redrawNeeded = true;
        ui.syncState = pending ? "pending" : "saved";
      } else {
        throw new Error(data.detail || ("HTTP " + res.status));
      }
    } catch (err) {
      // A missing acknowledgement may hide a successful commit. Replay that
      // exact request first so even a later undo can use its confirmed base.
      if (!res || res.ok) {
        batch.unacknowledged = draft;
        batch.retryBody = body;
      }
      if (pending) batch.message = "Batch map edits";
      pending = batch;
      clearTimeout(saveTimer);
      ui.syncState = "err";
      showToast(err.message, true, false);
    } finally {
      sending = false;
      renderSync();
      if (pending && saveTimer === null && ui.syncState !== "err") flushSave();
      else syncWhenIdle();
    }
  }

  function flushOnLeave() {
    if (editingField()) document.activeElement.blur();
    flushSave();
  }
  document.addEventListener("visibilitychange", function () {
    if (document.hidden) flushOnLeave();
  });
  window.addEventListener("beforeunload", function (event) {
    flushOnLeave();
    if ((sending && (pending || !keepalive)) || (mdpEl.open && mdText.value !== mdBase.next_markdown)) {
      event.preventDefault();
      event.returnValue = true;
    }
  });
  window.addEventListener("pagehide", function () {
    flushOnLeave();
    if (events) events.close();
    events = null;
  });

  function commit(msg, mutate) {
    if (!canEdit()) return false;
    var before = snapshot(state);
    mutate();
    if (snapshot(state) === before) { render(); return false; }
    undoStack.push(before);
    redoStack.length = 0;
    render();
    showToast(msg, false, true);
    enqueueSave(msg, before);
    return true;
  }

  function undo() {
    if (!canEdit() || !undoStack.length) return;
    var before = snapshot(state);
    redoStack.push(before);
    var prev = JSON.parse(undoStack.pop());
    state.groups = prev.groups;
    state.tasks = prev.tasks;
    fixSelection();
    render();
    enqueueSave("Undo last change", before);
  }

  function redo() {
    if (!canEdit() || !redoStack.length) return;
    var before = snapshot(state);
    undoStack.push(before);
    var next = JSON.parse(redoStack.pop());
    state.groups = next.groups;
    state.tasks = next.tasks;
    fixSelection();
    render();
    enqueueSave("Redo change", before);
  }

  function enableMap() {
    if (!state.can_write || sending) return;
    sending = true;
    editVersion++;
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
    }).finally(function () {
      sending = false;
      renderSync();
      syncWhenIdle();
    });
  }

  async function reloadFromServer(force) {
    if (refreshing || sending || pending) return;
    var version = editVersion;
    refreshNeeded = false;
    refreshing = true;
    try {
      var res = await fetch(mapUrl + (force ? "?refresh=1" : ""));
      if (res.status >= 500) throw new Error("HTTP " + res.status);
      if (!res.ok) {
        ui.syncState = "err";
        renderSync();
        return;
      }
      var data = await res.json();
      if (version !== editVersion || sending || pending || pointerDown || mdpEl.open || editingField()) {
        refreshNeeded = true;
      } else if (data.sha !== confirmed.sha || force) {
        undoStack.length = redoStack.length = 0;
        applyServerPayload(data);
        ui.syncState = "saved";
        render();
      }
    } catch (err) {
      ui.syncState = "err";
      renderSync();
      // Reconnecting delivers a new snapshot even if no other task changes.
      if (events) events.close();
      events = null;
      setTimeout(connectStream, 2000);
    } finally {
      refreshing = false;
      if (refreshNeeded) syncWhenIdle();
    }
  }

  function connectStream() {
    if (!window.EventSource || events) return;
    events = new EventSource(mapUrl + "/stream");
    events.addEventListener("status", function (event) {
      var live = JSON.parse(event.data);
      liveStatus = live;
      if (JSON.stringify(live.running) !== JSON.stringify(confirmed.running)) {
        confirmed.running = state.running = live.running;
        redrawNeeded = true;
        // Cards can update while a side-panel field has uncommitted text.
        if (editingField() && !pointerDown && !stageEl.contains(document.activeElement)) {
          renderView();
        }
      }
      if (live.sha !== confirmed.sha) refreshNeeded = true;
      syncWhenIdle();
    });
  }
  window.addEventListener("pageshow", connectStream);

  // --------------------------------------------------------- mutations
  function newTask(gid, deps, successor) {
    if (!canEdit()) return;
    var id = nextId(gid);
    ui.selected = id;
    ui.selEdge = null;
    deps = deps || [];
    commit("Add " + id + (deps.length ? " after " + deps.join(", ") : ""), function () {
      if (successor) T(successor).deps.push(id);
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

  // --------------------------------------------------------- TM-1 folding & barycenter layout
  function laneDoneInfo(gid) {
    var doneTasks = state.tasks.filter(function (t) { return t.group === gid && t.done; });
    var foldable = !ui.hideDone && ui.foldDone && doneTasks.length >= FOLD_DONE_MIN;
    var q = ui.filter.trim().toLowerCase();
    var forceOpen = false;
    if (foldable) {
      if (ui.unfoldedLanes.has(gid)) forceOpen = true;
      else if (ui.selected) {
        var sel = T(ui.selected);
        if (sel && sel.group === gid && sel.done) forceOpen = true;
      }
      if (!forceOpen && q) {
        for (var i = 0; i < doneTasks.length; i++) {
          if (matchesFilter(doneTasks[i], q)) { forceOpen = true; break; }
        }
      }
    }
    return {
      doneCount: doneTasks.length,
      foldable: foldable,
      folded: foldable && !forceOpen,
    };
  }

  function cardH(t) {
    return H + (ui.expanded.has(t.id) && t.subs && t.subs.length ? t.subs.length * SUB_H + 9 : 0);
  }

  // 3-sweep barycenter crossing minimization within each (lane, depth) bucket.
  function orderColumnsBarycenter(lanes, visTasks, visIds, maxDepth) {
    var fileIndex = {};
    state.tasks.forEach(function (t, i) { fileIndex[t.id] = i; });

    var childrenMap = {};
    visTasks.forEach(function (t) {
      (t.deps || []).forEach(function (d) {
        if (visIds.has(d)) {
          if (!childrenMap[d]) childrenMap[d] = [];
          childrenMap[d].push(t.id);
        }
      });
    });

    var rank = {};
    lanes.forEach(function (L, laneIdx) {
      var base = laneIdx * 1000;
      Object.keys(L.cols).forEach(function (k) {
        L.cols[k].forEach(function (t, idx) {
          rank[t.id] = base + idx * 10;
        });
      });
    });

    function refreshRanks(depthKey) {
      lanes.forEach(function (L, laneIdx) {
        var col = L.cols[depthKey];
        if (!col) return;
        var base = laneIdx * 1000;
        col.forEach(function (t, idx) { rank[t.id] = base + idx * 10; });
      });
    }

    function sweep(depthKey, neighboursFn) {
      lanes.forEach(function (L) {
        var col = L.cols[depthKey];
        if (!col || col.length < 2) return;
        var scored = col.map(function (t) {
          var ns = neighboursFn(t);
          var score = rank[t.id];
          if (ns.length) {
            var sum = 0;
            for (var i = 0; i < ns.length; i++) sum += rank[ns[i]];
            score = sum / ns.length;
          }
          return { t: t, score: score, cur: rank[t.id], idx: fileIndex[t.id] };
        });
        scored.sort(function (a, b) {
          if (a.score !== b.score) return a.score - b.score;
          if (a.cur !== b.cur) return a.cur - b.cur;
          return a.idx - b.idx;
        });
        L.cols[depthKey] = scored.map(function (x) { return x.t; });
      });
      refreshRanks(depthKey);
    }

    var parentsOf = function (t) {
      return (t.deps || []).filter(function (d) { return visIds.has(d); });
    };
    var kidsOf = function (t) { return childrenMap[t.id] || []; };

    for (var pass = 0; pass < 3; pass++) {
      if (pass % 2 === 0) {
        for (var k = 1; k <= maxDepth; k++) sweep(k, parentsOf);
      } else {
        for (var r = maxDepth - 1; r >= 0; r--) sweep(r, kidsOf);
      }
    }
  }

  function layout() {
    var foldedByGroup = {};
    state.groups.forEach(function (g) { foldedByGroup[g.id] = laneDoneInfo(g.id); });

    var vis = state.tasks.filter(function (t) {
      if (ui.hideDone && t.done && t.id !== ui.selected) return false;
      if (t.done && foldedByGroup[t.group] && foldedByGroup[t.group].folded) return false;
      return true;
    });
    var visIds = new Set(vis.map(function (t) { return t.id; }));
    var transitiveSet = computeTransitiveEdges(vis);

    var depth = {};
    var visiting = new Set();
    var maxDepth = 0;

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
      if (depth[t.id] > maxDepth) maxDepth = depth[t.id];
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
      return {
        g: g,
        idx: idx,
        ts: ts,
        cols: cols,
        ks: ks,
        min: min,
        max: max,
        doneInfo: foldedByGroup[g.id],
      };
    });

    if (ui.autoOrder && vis.length > 1) {
      orderColumnsBarycenter(lanes, vis, visIds, maxDepth);
    }

    var pos = {};
    var colStep = W + ui.gap;

    if (ui.orientation === "tb") {
      // Top-to-Bottom: depth flows along Y, cards in the same (lane, depth) sit side-by-side along X.
      var rowGap = Math.max(LANE_HEAD + LANE_PAD * 2 + 16, Math.round(ui.gap * 1.05));
      var levelH = {};
      for (var lv = 0; lv <= maxDepth; lv++) {
        var maxH = H;
        vis.forEach(function (t) {
          if (depth[t.id] === lv) maxH = Math.max(maxH, cardH(t));
        });
        levelH[lv] = maxH;
      }
      var levelTop = {};
      var curY = TOP + LANE_HEAD + LANE_PAD;
      for (var lv2 = 0; lv2 <= maxDepth; lv2++) {
        levelTop[lv2] = curY;
        curY += levelH[lv2] + rowGap;
      }

      lanes.forEach(function (L) {
        var maxInRow = 1;
        L.ks.forEach(function (k) { maxInRow = Math.max(maxInRow, L.cols[k].length); });
        L.innerW = maxInRow * W + Math.max(0, maxInRow - 1) * GY;
      });

      var vBands = [];
      lanes.forEach(function (L) {
        var band = null;
        if (ui.compact) {
          for (var i = 0; i < vBands.length; i++) {
            var fits = vBands[i].lanes.every(function (o) {
              return L.max < o.min || L.min > o.max;
            });
            if (fits) { band = vBands[i]; break; }
          }
        }
        if (!band) { band = { lanes: [] }; vBands.push(band); }
        band.lanes.push(L);
      });

      var x = LEFT;
      var maxY = 0;
      vBands.forEach(function (b) {
        var bw = Math.max.apply(null, b.lanes.map(function (L) { return L.innerW; })) + LANE_PAD * 2;
        b.lanes.forEach(function (L) {
          L.x = x;
          L.w = bw;
          L.y = levelTop[L.min] - LANE_HEAD - LANE_PAD;
          var bottom = L.ks.length
            ? (levelTop[L.max] + levelH[L.max] + LANE_PAD)
            : (L.y + LANE_HEAD + LANE_PAD * 2 + 46);
          L.hh = bottom - L.y;
          L.ks.forEach(function (k) {
            var cx = x + LANE_PAD;
            L.cols[k].forEach(function (t) {
              pos[t.id] = { x: cx, y: levelTop[k], h: cardH(t), depth: k };
              cx += W + GY;
            });
          });
          maxY = Math.max(maxY, L.y + L.hh);
        });
        x += bw + BAND_GAP;
      });

      var contentW = Math.max(W + LEFT * 2, x + LEFT);
      var contentH = Math.max(240, maxY + TOP);
      return {
        lanes: lanes,
        pos: pos,
        transitive: transitiveSet,
        levelTop: levelTop,
        levelH: levelH,
        contentW: contentW,
        contentH: contentH,
        w: contentW + 80,
        h: Math.max(420, contentH + 70),
      };
    }

    // Left-to-Right (default): depth flows along X, cards in the same (lane, depth) stack along Y.
    lanes.forEach(function (L) {
      var h = 0;
      L.ks.forEach(function (k) {
        var colH = L.cols[k].reduce(function (a, t) { return a + cardH(t) + GY; }, 0) - GY;
        h = Math.max(h, colH);
      });
      if (!L.ks.length) h = 48;
      L.h = h;
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

    var y = TOP;
    var maxX = W + LEFT;
    bands.forEach(function (b) {
      var bh = Math.max.apply(null, b.lanes.map(function (L) { return L.h; })) + LANE_HEAD + LANE_PAD * 2;
      b.lanes.forEach(function (L) {
        L.x = LEFT + L.min * colStep;
        L.y = y;
        L.w = (L.max - L.min) * colStep + W + LANE_PAD * 2;
        L.hh = bh;
        L.ks.forEach(function (k) {
          var cy = y + LANE_HEAD + LANE_PAD;
          L.cols[k].forEach(function (t) {
            pos[t.id] = { x: LEFT + LANE_PAD + k * colStep, y: cy, h: cardH(t), depth: k };
            cy += cardH(t) + GY;
          });
        });
        maxX = Math.max(maxX, L.x + L.w);
      });
      y += bh + BAND_GAP;
    });

    var cW = maxX + LEFT;
    var cH = y + TOP;
    return {
      lanes: lanes,
      pos: pos,
      transitive: transitiveSet,
      colStep: colStep,
      contentW: cW,
      contentH: cH,
      w: cW + colStep,
      h: Math.max(420, cH + 60),
    };
  }

  function laneAt(pt) {
    if (!lastLayout) return null;
    for (var i = 0; i < lastLayout.lanes.length; i++) {
      var L = lastLayout.lanes[i];
      if (pt.x >= L.x && pt.x <= L.x + L.w && pt.y >= L.y && pt.y <= L.y + L.hh) return L;
    }
    return null;
  }

  function curveH(x1, y1, x2, y2) {
    var dx = Math.max(30, Math.abs(x2 - x1) * 0.5);
    return "M" + x1 + "," + y1 + " C" + (x1 + dx) + "," + y1 + " " + (x2 - dx) + "," + y2 + " " + x2 + "," + y2;
  }

  function curveV(x1, y1, x2, y2) {
    var dy = Math.max(26, Math.abs(y2 - y1) * 0.5);
    return "M" + x1 + "," + y1 + " C" + x1 + "," + (y1 + dy) + " " + x2 + "," + (y2 - dy) + " " + x2 + "," + y2;
  }

  function edgePath(a, b) {
    if (ui.orientation === "tb") {
      return {
        d: curveV(a.x + W / 2, a.y + a.h, b.x + W / 2, b.y - 2),
        mx: (a.x + b.x) / 2 + W / 2,
        my: (a.y + a.h + b.y) / 2,
      };
    }
    return {
      d: curveH(a.x + W, a.y + 42, b.x - 2, b.y + 42),
      mx: (a.x + W + b.x) / 2,
      my: (a.y + b.y) / 2 + 42,
    };
  }

  // --------------------------------------------------------- render
  function render() {
    root.classList.toggle("tm-editable", canEdit());
    root.classList.toggle("tm-readonly", !canEdit());
    root.classList.toggle("tm-dir-tb", ui.orientation === "tb");
    if (menuEl.matches(":popover-open")) menuEl.hidePopover();
    if (ui.selected !== lastSelection) {
      history.replaceState(history.state, "", location.pathname + location.search + (ui.selected ? "#" + ui.selected : ""));
      lastSelection = ui.selected;
    }
    var focusedCard = document.activeElement.matches(".tm-card");
    renderBanner();
    renderBar();
    renderView();
    renderPanel();
    if (focusedCard && ui.selected) focusCard(ui.selected);
    if (hintEl) hintEl.hidden = !canEdit() || ui.view === "board";
  }

  function renderSync() {
    var button = document.getElementById("tm-save-now");
    if (button) {
      button.hidden = !pending;
      button.onclick = flushSave;
    }
    var source = document.getElementById("tm-md");
    if (source) source.disabled = !!pending || sending;
    var reload = document.getElementById("tm-reload");
    if (reload) reload.disabled = !!pending || sending;
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
    var displayOpen = !!mainEl.querySelector(".tm-display-pop");

    barEl.innerHTML =
      '<span class="tm-seg tm-view">' + ["graph", "board"].map(function (view) {
        return '<button type="button" data-view="' + view + '" aria-pressed="' + (ui.view === view) + '">' + esc(S[view]) + '</button>';
      }).join("") + '</span><div class="tm-chips"' + (ui.view === "board" ? " hidden" : "") + '>' +
        '<span class="tm-chip"><b>' + counts.ready + "</b> " + esc(S.ready_p) + "</span>" +
        '<span class="tm-chip"><b>' + counts.blocked + "</b> " + esc(S.blocked_p) + "</span>" +
        (counts.owner ? '<span class="tm-chip"><b>' + counts.owner + "</b> " + esc(S.owner_p) + "</span>" : "") +
        (counts.running ? '<span class="tm-chip"><b>' + counts.running + "</b> " + esc(S.running_p) + "</span>" : "") +
        '<span class="tm-chip"><b>' + counts.done + "</b> " + esc(S.done_p) + "</span>" +
      "</div>" +
      '<div class="tm-spacer"></div>' +
      '<button type="button" class="tm-btn" id="tm-save-now" hidden>' + esc(S.save_now) + "</button>" +
      '<span id="tm-sync" class="tm-sync"></span>' +
      '<input class="tm-search" id="tm-search" aria-label="' + esc(S.filter) + '" placeholder="' + esc(S.filter) + '" value="' + esc(ui.filter) + '">' +
      '<button type="button" class="tm-btn' + (displayOpen ? " on" : "") + '" id="tm-display"' + (ui.view === "board" ? " hidden" : "") + '>' + esc(S.display) + " ▾</button>" +
      '<span class="tm-seg"' + (ui.view === "board" ? " hidden" : "") + '>' +
        '<button type="button" id="tm-zout" aria-label="Zoom out">−</button>' +
        '<span class="tm-zoomv">' + Math.round(ui.zoom * 100) + "%</span>" +
        '<button type="button" id="tm-zin" aria-label="Zoom in">+</button>' +
        '<button type="button" id="tm-fit" title="' + esc(S.fit_title) + '">' + esc(S.fit) + "</button>" +
      "</span>" +
      (editable ? '<button type="button" class="tm-btn" id="tm-undo"' + (undoStack.length ? "" : " disabled") + ' title="' + esc(S.undo) + ' (Ctrl+Z)">↶</button>' : "") +
      (editable ? '<button type="button" class="tm-btn" id="tm-redo"' + (redoStack.length ? "" : " disabled") + ' title="' + esc(S.redo) + ' (Ctrl+Shift+Z)">↷</button>' : "") +
      '<button type="button" class="tm-btn" id="tm-reload" title="' + esc(S.reload) + '">↻</button>' +
      '<button type="button" class="tm-btn" id="tm-shortcuts" aria-label="' + esc(S.shortcuts) + '" title="' + esc(S.shortcuts) + '">?</button>' +
      '<button type="button" class="tm-btn" id="tm-md">' + esc(S.source) + "</button>" +
      (editable ? '<button type="button" class="tm-btn" id="tm-group">' + esc(S.new_group) + "</button>" : "") +
      (editable ? '<button type="button" class="tm-btn primary" id="tm-new">' + esc(S.new_task) + "</button>" : "");

    renderSync();

    var s = document.getElementById("tm-search");
    s.oninput = function () { ui.filter = s.value; render(); };
    if (focused) { s.focus(); s.setSelectionRange(s.value.length, s.value.length); }

    document.getElementById("tm-display").onclick = function (e) { toggleDisplayPop(e.currentTarget); };
    document.getElementById("tm-zout").onclick = function () { setZoom(ui.zoom - 0.1); };
    document.getElementById("tm-zin").onclick = function () { setZoom(ui.zoom + 0.1); };
    document.getElementById("tm-fit").onclick = fitToScreen;
    document.getElementById("tm-reload").onclick = function () { reloadFromServer(true); };
    document.getElementById("tm-md").onclick = openMarkdown;
    document.getElementById("tm-shortcuts").onclick = function () { helpEl.showModal(); };
    barEl.querySelectorAll("[data-view]").forEach(function (btn) {
      btn.onclick = function () {
        setView(btn.dataset.view);
        barEl.querySelector('[data-view="' + ui.view + '"]').focus();
      };
    });

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
    ui.zoom = Math.min(1.6, Math.max(0.35, Math.round(z * 100) / 100));
    render();
  }

  function fitToScreen() {
    if (!lastLayout) return;
    var cw = canvasEl.clientWidth || 800;
    var ch = canvasEl.clientHeight || 500;
    var scale = Math.min((cw - 24) / lastLayout.contentW, (ch - 24) / lastLayout.contentH);
    ui.zoom = Math.min(1.2, Math.max(0.35, Math.round(scale * 100) / 100));
    render();
    canvasEl.scrollLeft = 0;
    canvasEl.scrollTop = 0;
  }

  function adjustSpacing(delta) {
    var next = Math.min(GAP_MAX, Math.max(GAP_MIN, ui.gap + delta));
    if (next === ui.gap) return;
    ui.gap = next;
    savePrefs();
    render();
    var rng = document.getElementById("tm-dp-gap");
    if (rng) rng.value = String(ui.gap);
    var lbl = document.getElementById("tm-dp-gapv");
    if (lbl) lbl.textContent = ui.gap + " px";
  }

  function toggleDisplayPop(anchor) {
    var existing = mainEl.querySelector(".tm-display-pop");
    if (existing) { existing.remove(); renderBar(); return; }
    var other = mainEl.querySelector(".tm-pop");
    if (other) other.remove();

    var pop = document.createElement("div");
    pop.className = "tm-pop tm-display-pop";
    var r = anchor.getBoundingClientRect();
    var mr = mainEl.getBoundingClientRect();
    pop.style.left = Math.max(8, Math.min(mr.width - 276, r.right - mr.left - 268)) + "px";
    pop.style.top = "8px";

    function fillPop() {
      pop.innerHTML =
        '<div class="tm-dp-row">' +
          '<span class="tm-dp-label"><span>' + esc(S.orient) + '</span><span class="tm-mono">L</span></span>' +
          '<div class="tm-dp-seg">' +
            '<button type="button" data-dir="lr" class="' + (ui.orientation === "lr" ? "on" : "") + '">' + esc(S.orient_lr) + "</button>" +
            '<button type="button" data-dir="tb" class="' + (ui.orientation === "tb" ? "on" : "") + '">' + esc(S.orient_tb) + "</button>" +
          "</div>" +
        "</div>" +
        '<div class="tm-dp-row">' +
          '<span class="tm-dp-label"><span>' + esc(S.order) + '</span><span class="tm-mono">M</span></span>' +
          '<div class="tm-dp-seg">' +
            '<button type="button" data-ord="auto" class="' + (ui.autoOrder ? "on" : "") + '">' + esc(S.order_auto) + "</button>" +
            '<button type="button" data-ord="manual" class="' + (!ui.autoOrder ? "on" : "") + '">' + esc(S.order_manual) + "</button>" +
          "</div>" +
        "</div>" +
        '<div class="tm-dp-row">' +
          '<span class="tm-dp-label"><span>' + esc(S.spacing) + '</span><span class="tm-mono" id="tm-dp-gapv">' + ui.gap + " px</span></span>" +
          '<input type="range" class="tm-dp-range" id="tm-dp-gap" min="' + GAP_MIN + '" max="' + GAP_MAX + '" step="' + GAP_STEP + '" value="' + ui.gap + '">' +
        "</div>" +
        '<div class="tm-dp-checks">' +
          '<label class="tm-dp-chk"><input type="checkbox" id="tm-dp-compact"' + (ui.compact ? " checked" : "") + "><span>" + esc(S.compact) + "</span></label>" +
          '<label class="tm-dp-chk"><input type="checkbox" id="tm-dp-hidedone"' + (ui.hideDone ? " checked" : "") + "><span>" + esc(S.hide_done) + "</span></label>" +
          '<label class="tm-dp-chk"><input type="checkbox" id="tm-dp-fold"' + (ui.foldDone ? " checked" : "") + "><span>" + esc(S.fold_done) + "</span></label>" +
          '<label class="tm-dp-chk"><input type="checkbox" id="tm-dp-trans"' + (ui.hideTransitive ? " checked" : "") + "><span>" + esc(S.hide_transitive) + "</span></label>" +
        "</div>";

      pop.querySelectorAll("[data-dir]").forEach(function (b) {
        b.onclick = function () {
          ui.orientation = b.dataset.dir;
          savePrefs();
          fillPop();
          render();
        };
      });
      pop.querySelectorAll("[data-ord]").forEach(function (b) {
        b.onclick = function () {
          ui.autoOrder = b.dataset.ord === "auto";
          savePrefs();
          fillPop();
          render();
        };
      });
      var rng = pop.querySelector("#tm-dp-gap");
      rng.oninput = function () {
        ui.gap = Number(rng.value);
        savePrefs();
        var lbl = pop.querySelector("#tm-dp-gapv");
        if (lbl) lbl.textContent = ui.gap + " px";
        render();
      };
      pop.querySelector("#tm-dp-compact").onchange = function (e) { ui.compact = e.target.checked; savePrefs(); render(); };
      pop.querySelector("#tm-dp-hidedone").onchange = function (e) { ui.hideDone = e.target.checked; savePrefs(); render(); };
      pop.querySelector("#tm-dp-fold").onchange = function (e) { ui.foldDone = e.target.checked; savePrefs(); render(); };
      pop.querySelector("#tm-dp-trans").onchange = function (e) { ui.hideTransitive = e.target.checked; savePrefs(); render(); };
    }

    fillPop();
    mainEl.appendChild(pop);
    renderBar();
  }

  function setView(view) {
    var pop = mainEl.querySelector(".tm-pop");
    if (pop) pop.remove();
    ui.view = view;
    ui.selEdge = ui.hover = null;
    render();
  }

  function renderView() {
    var board = ui.view === "board";
    graphEl.hidden = board;
    boardEl.hidden = !board;
    if (board) {
      renderBoard();
    } else {
      lastLayout = layout();
      renderMap(lastLayout);
      applyHighlight();
    }
  }

  function renderBoard() {
    var columns = {blocked: [], ready: [], owner: [], running: [], done: []};
    var q = ui.filter.trim().toLowerCase();
    state.tasks.forEach(function (t) {
      if (!matchesFilter(t, q)) return;
      var st = statusOf(t);
      columns[st === "queued" ? "running" : st].push(
        '<button type="button" class="tm-card tm-s-' + st + (ui.selected === t.id ? " sel" : "") +
        '" data-id="' + esc(t.id) + '"><span class="tm-c-top"><span class="tm-c-id">' + esc(t.id) +
        '</span><span class="tm-badge">' + esc(statusLabel(st)) + '</span></span><span class="tm-c-title">' +
        esc(t.title) + '</span></button>'
      );
    });
    boardEl.innerHTML = Object.keys(columns).map(function (st) {
      return '<section class="tm-column" data-state="' + st + '"><h3>' + esc(statusLabel(st)) +
        '<span>' + columns[st].length + '</span></h3>' + columns[st].join("") + '</section>';
    }).join("");
  }

  boardEl.addEventListener("click", function (event) {
    var card = event.target.closest(".tm-card");
    if (card) selectCard(card.dataset.id);
  });

  function renderMap(L) {
    stageEl.style.width = L.w + "px";
    stageEl.style.height = L.h + "px";
    stageEl.style.transform = "scale(" + ui.zoom + ")";
    sizerEl.style.width = (L.w * ui.zoom) + "px";
    sizerEl.style.height = (L.h * ui.zoom) + "px";

    var editable = canEdit();
    var stubsHtml = "";

    lanesEl.innerHTML = L.lanes.map(function (l) {
      var all = state.tasks.filter(function (t) { return t.group === l.g.id; });
      var done = all.filter(function (t) { return t.done; }).length;
      var pct = all.length ? (done / all.length * 100) : 0;
      var di = l.doneInfo;
      var foldPill = "";
      if (di && di.foldable) {
        var label = (S.done_stub || "%s done").replace("%s", String(di.doneCount));
        var title = di.folded ? S.fold_toggle_show : S.fold_toggle_hide;
        foldPill = '<button type="button" class="tm-lane-fold" data-act="fold-toggle" data-g="' + esc(l.g.id) + '" title="' + esc(title) + '">✓ ' + esc(label) + " " + (di.folded ? "▸" : "▾") + "</button>";
      }
      if (di && di.folded && !l.ts.length) {
        var stubLabel = (S.done_stub || "%s done").replace("%s", String(di.doneCount));
        stubsHtml += '<button type="button" class="tm-done-stub" data-act="fold-toggle" data-g="' + esc(l.g.id) +
          '" style="left:' + (l.x + LANE_PAD) + "px;top:" + (l.y + LANE_HEAD + LANE_PAD) + "px;width:" + W + 'px;height:44px">✓ ' + esc(stubLabel) + " ▸</button>";
      }
      return '<div class="tm-lane" data-g="' + esc(l.g.id) + '" style="left:' + l.x + "px;top:" + l.y + "px;width:" + l.w + "px;height:" + l.hh + 'px">' +
        '<div class="tm-lane-head">' +
          '<span class="tm-lane-dot c' + (l.idx % 6) + '"></span>' +
          '<span class="tm-lane-name"' + (editable ? ' data-act="rename" title="' + esc(S.rename_hint) + '"' : "") + ">" + esc(l.g.name) + "</span>" +
          '<span class="tm-lane-pfx">' + esc(l.g.id) + "</span>" +
          '<span class="tm-lane-count">' + done + "/" + all.length + "</span>" +
          '<span class="tm-lane-bar"><i style="width:' + pct + '%"></i></span>' +
          foldPill +
          (editable ? '<button type="button" class="tm-ibtn" data-act="add-lane" data-g="' + esc(l.g.id) + '" title="' + esc(S.add_in_group) + '">+</button>' : "") +
          (editable && !all.length && state.groups.length > 1 ? '<button type="button" class="tm-ibtn" data-act="lane-del" data-g="' + esc(l.g.id) + '" title="' + esc(S.del_group) + '">✕</button>' : "") +
        "</div></div>";
    }).join("");

    cardsEl.innerHTML = stubsHtml + state.tasks.filter(function (t) { return !!L.pos[t.id]; }).map(function (t) {
      var p = L.pos[t.id];
      var st = statusOf(t);
      var subs = t.subs || [];
      var subsDone = subs.filter(function (x) { return x.done; }).length;
      var open = ui.expanded.has(t.id);
      var rec = parseRecommended(t.desc || "").rec;
      var ag = rec.agent || (t.recommended && t.recommended.agent) || "";
      return '<div tabindex="0" class="tm-card tm-s-' + st + (ui.selected === t.id ? " sel" : "") + '" data-id="' + esc(t.id) + '" style="left:' + p.x + "px;top:" + p.y + "px;width:" + W + "px;height:" + p.h + 'px">' +
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
        var ep = edgePath(a, b);
        var isSel = ui.selEdge && ui.selEdge.from === dId && ui.selEdge.to === t.id;
        var isTrans = ui.hideTransitive && L.transitive.has(dId + "->" + t.id);
        var depTask = T(dId);
        svg += '<path class="tm-edge' + (depTask && depTask.done ? " from-done" : "") +
          (isTrans ? " transitive" : "") + (isSel ? " sel" : "") +
          '" data-from="' + esc(dId) + '" data-to="' + esc(t.id) + '"' +
          (isTrans ? ' data-trans="1"' : "") +
          ' d="' + ep.d + '" marker-end="url(#' + (isSel ? "tm-arrS" : "tm-arr") + ')"/>';
        svg += '<path class="tm-edge-hit' + (isTrans && !isSel ? " transitive-off" : "") +
            '" data-from="' + esc(dId) + '" data-to="' + esc(t.id) + '"' +
            (isTrans ? ' data-trans="1"' : "") + ' d="' + ep.d + '"/>';
        if (isSel && editable) {
          delBtn = '<button type="button" class="tm-edge-del" data-act="edge-del" style="left:' + ep.mx + "px;top:" + ep.my + 'px">✕ ' + esc(S.unlink) + " " + esc(dId) + " → " + esc(t.id) + "</button>";
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
      c.classList.toggle("fout", !!(q && t && !matchesFilter(t, q)));
    });
    edgesEl.querySelectorAll("path.tm-edge").forEach(function (p) {
      if (p.classList.contains("sel")) return;
      var from = p.dataset.from;
      var to = p.dataset.to;
      var isTrans = p.dataset.trans === "1";
      if (isTrans) {
        var reveal = !!(focus && (focus === from || focus === to));
        p.classList.toggle("rev", reveal);
        p.classList.toggle("hl", reveal);
        p.setAttribute("marker-end", reveal ? "url(#tm-arrH)" : "url(#tm-arr)");
        return;
      }
      var on = !!(chain && chain.has(from) && chain.has(to));
      p.classList.toggle("hl", on);
      p.classList.toggle("dim", !!(chain && !on));
      p.setAttribute("marker-end", on ? "url(#tm-arrH)" : "url(#tm-arr)");
    });
    edgesEl.querySelectorAll("path.tm-edge-hit[data-trans='1']").forEach(function (h) {
      var reveal = !!(focus && (focus === h.dataset.from || focus === h.dataset.to)) ||
        !!(ui.selEdge && ui.selEdge.from === h.dataset.from && ui.selEdge.to === h.dataset.to);
      h.classList.toggle("transitive-off", !reveal);
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
      '<div class="tm-p-head"><span class="tm-c-id">' + esc(t.id) + '</span><span class="tm-badge st-' + st + '">' + esc(statusLabel(st)) + '</span><span class="tm-spacer"></span><button type="button" class="tm-ibtn" id="tm-p-close" aria-label="' + esc(S.close) + '">✕</button></div>' +
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
    document.getElementById("tm-p-close").onclick = closePanel;

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

  async function copyText(text) {
    try {
      await navigator.clipboard.writeText(text);
      showToast(S.copied, false, false);
    } catch (err) {
      showToast(S.copy_error, true, false);
    }
  }

  var mdText = document.getElementById("tm-md-text");
  var mdApply = document.getElementById("tm-md-apply");
  var mdError = document.getElementById("tm-md-error");
  var mdClose = document.getElementById("tm-md-close");
  var mdBase;

  function openMarkdown() {
    if (pending || sending) return;
    mdBase = confirmed;
    mdText.value = mdBase.next_markdown;
    mdText.readOnly = !canEdit();
    mdApply.hidden = !canEdit();
    mdApply.disabled = true;
    mdError.hidden = true;
    mdpEl.showModal();
    mdText.focus();
  }

  mdText.oninput = function () { mdApply.disabled = mdText.value === mdBase.next_markdown; };
  mdClose.onclick = function () { mdpEl.close(); };
  mdpEl.oncancel = function (e) { if (sending) e.preventDefault(); };
  mdpEl.onclose = function () {
    document.getElementById("tm-md").focus();
    syncWhenIdle();
  };
  document.getElementById("tm-md-copy").onclick = function () { copyText(mdText.value); };
  mdApply.onclick = async function () {
    if (!canEdit() || sending || pending) return;
    sending = true;
    editVersion++;
    mdApply.disabled = mdClose.disabled = mdText.readOnly = true;
    mdError.hidden = true;
    try {
      var res = await fetch(mapUrl, {
        method: "POST",
        headers: {"Content-Type": "application/json", "X-Baton-CSRF": csrf},
        body: JSON.stringify({sha: mdBase.sha, base: JSON.parse(snapshot(mdBase)),
          next_markdown: mdText.value, message: "Edit ## Next"}),
      });
      var data = await res.json();
      if (res.status === 409 && data.map) {
        applyServerPayload(data.map);
        undoStack.length = redoStack.length = 0;
        throw new Error(S.md_conflict);
      }
      if (!res.ok) throw new Error(data.detail || ("HTTP " + res.status));
      undoStack.length = redoStack.length = 0;
      if (!data.merged) undoStack.push(snapshot(mdBase));
      applyServerPayload(data);
      ui.syncState = "saved";
      mdpEl.close();
      showToast(S.saved, false, false);
    } catch (err) {
      mdError.textContent = err.message;
      mdError.hidden = false;
    } finally {
      sending = mdClose.disabled = mdText.readOnly = false;
      mdApply.disabled = false;
      render();
      syncWhenIdle();
    }
  };

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
    var pop = mainEl.querySelector(".tm-pop");
    if (pop) { pop.remove(); renderBar(); }
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
    } else if (act === "fold-toggle") {
      var fg = b.dataset.g;
      if (ui.unfoldedLanes.has(fg)) ui.unfoldedLanes.delete(fg); else ui.unfoldedLanes.add(fg);
      render();
    } else if (act === "edge-del" && ui.selEdge) {
      unlink(ui.selEdge.from, ui.selEdge.to);
    } else if (act === "lane-del" && canEdit()) {
      deleteGroup(b.dataset.g);
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
    renameGroup(n);
  });

  function renameGroup(n) {
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
  }

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
      if (ui.orientation === "tb") {
        var sx = p.x + W / 2;
        temp.setAttribute("d", mode === "out" ? curveV(sx, p.y + p.h, pt.x, pt.y) : curveV(pt.x, pt.y, sx, p.y));
      } else {
        var sy = p.y + 42;
        temp.setAttribute("d", mode === "out" ? curveH(p.x + W, sy, pt.x, pt.y) : curveH(pt.x, pt.y, p.x, sy));
      }
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

    if (ui.orientation === "tb") {
      for (var i = 0; i < col.length; i++) {
        if (pt.x < P[col[i].id].x + W / 2) { before = col[i]; break; }
      }
      var afterV = before ? null : (col.length ? col[col.length - 1] : null);
      var xV = before ? (P[before.id].x - GY / 2 - 1)
        : afterV ? (P[afterV.id].x + W + GY / 2 - 1)
        : (lane.x + LANE_PAD - 4);
      var yV = lastLayout.levelTop ? lastLayout.levelTop[d] : (lane.y + LANE_HEAD + LANE_PAD);
      return {
        gid: lane.g.id, before: before ? before.id : null, after: afterV ? afterV.id : null,
        x: xV, y: yV, vert: true,
      };
    }

    for (var j = 0; j < col.length; j++) {
      if (pt.y < P[col[j].id].y + P[col[j].id].h / 2) { before = col[j]; break; }
    }
    var after = before ? null : (col.length ? col[col.length - 1] : null);
    var y = before ? (P[before.id].y - GY / 2 - 1)
      : after ? (P[after.id].y + P[after.id].h + GY / 2 - 1)
      : (lane.y + LANE_HEAD + LANE_PAD - 4);
    var colStep = lastLayout.colStep || (W + ui.gap);
    return {
      gid: lane.g.id, before: before ? before.id : null, after: after ? after.id : null,
      x: LEFT + LANE_PAD + d * colStep, y: y, vert: false,
    };
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
        dropIndEl.classList.toggle("vert", !!target.vert);
        dropIndEl.style.left = target.x + "px";
        dropIndEl.style.top = target.y + "px";
        dropIndEl.style.width = target.vert ? "3px" : (W + "px");
        dropIndEl.style.height = target.vert ? (H + "px") : "3px";
      } else {
        dropIndEl.hidden = true;
      }
    }
    function up() {
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", up);
      dropIndEl.hidden = true;
      if (!dragging) {
        selectCard(id);
        return;
      }
      el.classList.remove("dragging");
      el.style.transform = "";
      if (!target) { render(); return; }
      var t = T(id);
      var moved = t.group !== target.gid;
      var switchedManual = false;
      if (!moved && ui.autoOrder) {
        ui.autoOrder = false;
        savePrefs();
        switchedManual = true;
      }
      commit(moved ? ("Move " + id + " to " + (G(target.gid) ? G(target.gid).name : target.gid)) : ("Reorder " + id), function () {
        var arr = state.tasks;
        var task = T(id);
        arr.splice(arr.indexOf(task), 1);
        task.group = target.gid;
        if (target.before) arr.splice(arr.indexOf(T(target.before)), 0, task);
        else if (target.after) arr.splice(arr.indexOf(T(target.after)) + 1, 0, task);
        else arr.push(task);
      });
      if (switchedManual) showToast(S.switched_manual, false, false);
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

  function deleteGroup(gid) {
    if (state.groups.length < 2 || state.tasks.some(function (t) { return t.group === gid; })) return;
    commit("Delete empty group " + G(gid).name, function () {
      state.groups = state.groups.filter(function (g) { return g.id !== gid; });
    });
  }

  function focusCard(id) {
    var surface = ui.view === "board" ? boardEl : cardsEl;
    var card = surface.querySelector('.tm-card[data-id="' + CSS.escape(id) + '"]');
    if (card) {
      card.focus({preventScroll: true});
      card.scrollIntoView({block: "nearest", inline: "nearest"});
    }
  }

  function selectCard(id) {
    ui.selected = id;
    ui.selEdge = ui.hover = null;
    if (!matchesFilter(T(id), ui.filter.trim().toLowerCase())) ui.filter = "";
    render();
    focusCard(id);
    if (phone.matches) document.getElementById("tm-p-close").focus();
  }

  function closePanel() {
    var id = ui.selected;
    ui.selected = null;
    render();
    if (id) focusCard(id);
  }

  function followHash() {
    var id = location.hash.slice(1);
    if (T(id)) selectCard(id);
    else if (ui.selected) { ui.selected = null; render(); }
  }
  window.addEventListener("hashchange", followHash);

  function moveSelection(key) {
    var surface = ui.view === "board" ? boardEl : cardsEl;
    var cards = Array.from(surface.querySelectorAll(".tm-card:not(.fout)"));
    var current = cards.includes(document.activeElement) ? document.activeElement :
      cards.find(function (card) { return card.dataset.id === ui.selected; });
    if (!current) {
      if (cards.length) selectCard(cards[0].dataset.id);
      return;
    }
    var origin = current.getBoundingClientRect();
    var horizontal = key === "ArrowLeft" || key === "ArrowRight";
    var sign = key === "ArrowLeft" || key === "ArrowUp" ? -1 : 1;
    var nearest = null, best = Infinity;
    cards.forEach(function (card) {
      var rect = card.getBoundingClientRect();
      var dx = rect.left - origin.left, dy = rect.top - origin.top;
      var along = sign * (horizontal ? dx : dy);
      var across = Math.abs(horizontal ? dy : dx);
      var score = along + 2 * across;
      if (along > 1 && score < best) { nearest = card; best = score; }
    });
    if (nearest) selectCard(nearest.dataset.id);
  }

  root.addEventListener("contextmenu", function (e) {
    var card = e.target.closest(".tm-card");
    var edge = e.target.closest(".tm-edge-hit");
    var lane = e.target.closest(".tm-lane");
    if (!card && !edge && !(lane && canEdit())) return;
    e.preventDefault();
    menuEl.replaceChildren();
    function action(label, run, disabled) {
      var button = document.createElement("button");
      button.type = "button";
      button.setAttribute("role", "menuitem");
      button.textContent = label;
      button.disabled = !!disabled;
      button.onclick = function () { menuEl.hidePopover(); run(); };
      menuEl.appendChild(button);
    }
    if (card) {
      var t = T(card.dataset.id);
      action(S.launch, function () { location.assign(launchHref(t)); }, statusOf(t) !== "ready");
      if (canEdit()) {
        action(t.done ? S.reopen : S.mark_done, function () { setDone(t.id, !t.done); });
        action(S.add_after, function () { newTask(t.group, [t.id]); });
        action(S.add_before, function () { newTask(t.group, [], t.id); });
      }
      action(S.copy_id, function () { copyText(t.id); });
      if (canEdit()) action(S.del, function () {
        selectCard(t.id);
        var del = document.getElementById("tm-p-del");
        del.click();
        del.focus();
      });
    } else if (edge) {
      var from = edge.dataset.from, to = edge.dataset.to;
      action(S.jump_from + " · " + from, function () { selectCard(from); });
      action(S.jump_to + " · " + to, function () { selectCard(to); });
      if (canEdit()) action(S.unlink, function () { unlink(from, to); });
    } else {
      var gid = lane.dataset.g;
      action(S.add_in_group, function () { newTask(gid); });
      action(S.rename_group, function () { renameGroup(lane.querySelector(".tm-lane-name")); });
      action(S.del_group, function () { deleteGroup(gid); }, !lane.querySelector('[data-act="lane-del"]'));
    }
    menuEl.showPopover();
    var box = menuEl.getBoundingClientRect();
    menuEl.style.left = Math.max(0, Math.min(e.clientX, innerWidth - box.width)) + "px";
    menuEl.style.top = Math.max(0, Math.min(e.clientY, innerHeight - box.height)) + "px";
    menuEl.querySelector("button:not(:disabled)").focus();
  });

  menuEl.onkeydown = function (e) {
    if (e.key !== "ArrowUp" && e.key !== "ArrowDown") return;
    e.preventDefault();
    var buttons = Array.from(menuEl.querySelectorAll("button:not(:disabled)"));
    var i = buttons.indexOf(document.activeElement) + (e.key === "ArrowDown" ? 1 : -1);
    buttons[(i + buttons.length) % buttons.length].focus();
  };

  // --------------------------------------------------------- keyboard shortcuts
  document.addEventListener("keydown", function (e) {
    if (helpEl.open || mdpEl.open || menuEl.matches(":popover-open")) return;
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
    if (mod || e.altKey) return;
    if (e.key === "?") {
      e.preventDefault();
      helpEl.showModal();
    } else if (e.key.startsWith("Arrow") && !panelEl.contains(e.target)) {
      e.preventDefault();
      moveSelection(e.key);
    } else if (e.key === "Enter" && (e.target.matches(".tm-card") || (ui.selected && e.target === document.body))) {
      e.preventDefault();
      selectCard(e.target.dataset.id || ui.selected);
      document.getElementById(canEdit() ? "tm-p-title" : "tm-p-close").focus();
    } else if (e.key === "/") {
      e.preventDefault();
      var s = document.getElementById("tm-search");
      if (s) s.focus();
    } else if (e.key === "Escape") {
      var pop = mainEl.querySelector(".tm-pop");
      if (pop) pop.remove();
      ui.selEdge = null;
      closePanel();
    } else if ((e.key === "Delete" || e.key === "Backspace") && ui.selEdge && canEdit()) {
      unlink(ui.selEdge.from, ui.selEdge.to);
    } else if (ui.view === "graph" && e.key === "0") {
      e.preventDefault();
      fitToScreen();
    } else if (ui.view === "graph" && e.key.toLowerCase() === "l") {
      ui.orientation = ui.orientation === "lr" ? "tb" : "lr";
      savePrefs();
      render();
    } else if (ui.view === "graph" && e.key.toLowerCase() === "m") {
      ui.autoOrder = !ui.autoOrder;
      savePrefs();
      render();
    } else if (ui.view === "graph" && e.key === "[") {
      adjustSpacing(-GAP_STEP);
    } else if (ui.view === "graph" && e.key === "]") {
      adjustSpacing(GAP_STEP);
    } else if (e.key.toLowerCase() === "n" && canEdit()) {
      var sel = ui.selected ? T(ui.selected) : null;
      newTask(sel ? sel.group : state.groups[0].id);
    }
  });

  if (T(location.hash.slice(1))) followHash();
  else render();
  connectStream();
})();
