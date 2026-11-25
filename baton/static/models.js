// Models page: every 5 seconds, read /api/models and update the numbers in
// place. When a model changes state (loading -> running, stopped elsewhere...)
// or the model list changes, the page reloads so its buttons match.
(function () {
  var root = document.querySelector("[data-models]");
  if (!root) return;
  document.querySelectorAll("form[data-confirm]").forEach(function (form) {
    form.addEventListener("submit", function (event) {
      if (!window.confirm(form.dataset.confirm)) event.preventDefault();
    });
  });
  var order = [], states = {};
  root.querySelectorAll("[data-model]").forEach(function (el) {
    order.push(el.dataset.model);
    states[el.dataset.model] = el.dataset.state;
  });

  var filters = root.querySelectorAll("[data-family-filter]");
  var groups = root.querySelectorAll("[data-family-group]");
  function setFamily(key) {
    filters.forEach(function (btn) {
      var on = btn.dataset.familyFilter === key;
      btn.setAttribute("aria-pressed", String(on));
      if (on) btn.setAttribute("aria-current", "true");
      else btn.removeAttribute("aria-current");
    });
    groups.forEach(function (g) {
      g.hidden = key !== "all" && g.dataset.familyGroup !== key;
    });
  }
  filters.forEach(function (btn) {
    btn.addEventListener("click", function () { setFamily(btn.dataset.familyFilter); });
  });

  function changed(models) {
    if (models.length !== order.length) return true;
    return models.some(function (m, i) { return m.id !== order[i] || states[m.id] !== m.status; });
  }

  function tick() {
    fetch("/api/models", { cache: "no-store" })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (view) {
        if (!view) return;
        if (changed(view.models)) { location.reload(); return; }
        root.querySelectorAll("[data-k]").forEach(function (el) {
          var value = view.show[el.dataset.k];
          if (value !== undefined) el.textContent = value;
        });
        root.querySelectorAll("[data-w]").forEach(function (el) {
          var value = view.show[el.dataset.w];
          if (value !== undefined) el.style.width = Math.min(100, value).toFixed(1) + "%";
        });
        root.querySelectorAll("[data-lv]").forEach(function (el) {
          var level = view.levels[el.dataset.lv];
          if (level) el.className = el.className.replace(/\blv-\w+/, "lv-" + level);
        });
      })
      .catch(function () {});
  }
  setInterval(function () { if (!document.hidden) tick(); }, 5000);
})();
