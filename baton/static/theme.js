// Theme buttons: Auto (follow the system), Light, Dark. The choice is kept in
// localStorage; the inline script in <head> applies it before the first paint.
(function () {
  var d = document.documentElement, order = ["auto", "light", "dark"];
  function current() { return d.dataset.theme || "auto"; }
  function paint() {
    var theme = current();
    document.querySelectorAll("[data-theme-choice]").forEach(function (b) {
      b.setAttribute("aria-pressed", String(b.dataset.themeChoice === theme));
    });
    document.querySelectorAll("[data-theme-cycle]").forEach(function (b) { b.dataset.current = theme; });
  }
  function apply(theme) {
    if (theme === "auto") delete d.dataset.theme; else d.dataset.theme = theme;
    try { localStorage.setItem("baton-theme", theme); } catch (e) {}
    paint();
  }
  document.querySelectorAll("[data-theme-choice]").forEach(function (b) {
    b.addEventListener("click", function () { apply(b.dataset.themeChoice); });
  });
  document.querySelectorAll("[data-theme-cycle]").forEach(function (b) {
    b.addEventListener("click", function () { apply(order[(order.indexOf(current()) + 1) % order.length]); });
  });
  paint();
})();
