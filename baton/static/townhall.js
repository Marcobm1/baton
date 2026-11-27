// Live SSE progress viewer for a running Town Hall session in Huo.
(function () {
  var box = document.getElementById("th-live-log");
  if (!box) return;
  var url = box.getAttribute("data-events-url");
  if (!url || typeof EventSource === "undefined") return;

  var es = new EventSource(url);
  var lines = [];

  function formatEvent(ev) {
    var p = ev.payload || {};
    if (ev.type === "session_started") {
      return "-> Started (" + (p.mode || "council") + (p.workspace_isolated ? ", isolated worktree" : "") + ")";
    }
    if (ev.type === "phase_started") {
      return "-> Phase: " + (p.phase || "") + " (round " + (p.round_num || 0) + ")";
    }
    if (ev.type === "guest_selected") {
      return "-> 4th guest (" + (p.method || "") + "): " + (p.chosen_model || "");
    }
    if (ev.type === "turn_completed") {
      var st = p.ok ? "ok" : ("error: " + (p.error || "failed"));
      return "   [" + (p.participant_name || p.participant_id || "?") + "] " + st;
    }
    if (ev.type === "budget_reached") {
      return "-> Time budget reached; moving to synthesis";
    }
    return "-> " + ev.type;
  }

  es.onmessage = function (e) {
    var ev;
    try {
      ev = JSON.parse(e.data);
    } catch (_err) {
      return;
    }
    lines.push(formatEvent(ev));
    box.textContent = lines.join("\n");
    box.scrollTop = box.scrollHeight;
    if (ev.type === "session_completed" || ev.type === "session_cancelled" || ev.type === "session_failed") {
      es.close();
      window.location.reload();
    }
  };

  es.onerror = function () {
    es.close();
  };
})();
