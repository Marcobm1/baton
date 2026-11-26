// Live 5-second refresh for /machines (polls /api/machines without reloading the page).
(function () {
  var root = document.querySelector("[data-machines]");
  var strEl = document.getElementById("mach-strings");
  if (!root || !strEl) return;
  var T = JSON.parse(strEl.textContent);

  function fmtRate(bps) {
    var v = Number(bps || 0);
    if (v >= 1024 * 1024) return (v / (1024 * 1024)).toFixed(1) + " MiB/s";
    return (v / 1024).toFixed(1) + " KiB/s";
  }

  function updateCard(m) {
    var card = root.querySelector('[data-machine-id="' + CSS.escape(m.id) + '"]');
    if (!card) return;
    card.classList.toggle("is-offline", m.status === "offline");
    var badge = card.querySelector('[data-field="status"]');
    if (badge) {
      badge.className = "mach-badge st-" + m.status;
      badge.textContent = T[m.status] || m.status;
    }
    var x = m.metrics || {};
    var osEl = card.querySelector('[data-field="os"]');
    if (osEl) osEl.textContent = m.os_info || "—";
    var cpu = card.querySelector('[data-field="cpu"]');
    if (cpu) {
      cpu.textContent = x.cpu_pct != null
        ? Math.round(x.cpu_pct) + "%" + (x.cpu_temp_c != null ? " · " + Math.round(x.cpu_temp_c) + " °C" : "")
        : "—";
    }
    var ram = card.querySelector('[data-field="ram"]');
    if (ram) {
      ram.textContent = x.ram_pct != null
        ? Math.round(x.ram_pct) + "% (" + Number(x.ram_used_gb || 0).toFixed(1) + "/" + Number(x.ram_total_gb || 0).toFixed(1) + " GiB)"
        : "—";
    }
    var disk = card.querySelector('[data-field="disk"]');
    if (disk) {
      disk.textContent = x.disk_pct != null
        ? Math.round(x.disk_pct) + "% (" + Math.round(x.disk_free_gb || 0) + " GiB free)"
        : "—";
    }
    var gpu = card.querySelector('[data-field="gpu"]');
    if (gpu) {
      var g = x.gpu;
      gpu.textContent = g
        ? Math.round(g.util_pct || 0) + "%" + (g.temp_c != null ? " · " + Math.round(g.temp_c) + " °C" : "")
          + (g.vram_total_gb ? " · " + Number(g.vram_used_gb || 0).toFixed(1) + "/" + Number(g.vram_total_gb).toFixed(1) + " GiB" : "")
        : "—";
    }
    var net = card.querySelector('[data-field="net"]');
    if (net && x.net) {
      net.textContent = "↓ " + fmtRate(x.net.rx_bps) + " · ↑ " + fmtRate(x.net.tx_bps);
    }
    var logins = card.querySelector('[data-field="logins"]');
    if (logins) logins.textContent = String((x.logins || []).length);
    var seen = card.querySelector('[data-field="seen"]');
    if (seen) {
      if (m.last_seen) {
        var d = new Date(m.last_seen * 1000);
        seen.textContent = T.last_seen + ": " + d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
      } else {
        seen.textContent = T.pending;
      }
    }
  }

  function poll() {
    if (document.hidden) return;
    fetch("/api/machines", { cache: "no-store" })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (j) { if (j && Array.isArray(j.machines)) j.machines.forEach(updateCard); })
      .catch(function () {});
  }

  setInterval(poll, 5000);
})();
