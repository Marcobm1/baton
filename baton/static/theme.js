// Theme buttons (Auto / Light / Dark) and collapsible desktop sidebar rail.
// Choices are persisted in localStorage and applied before first paint in <head>.
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

  function isCollapsed() { return d.dataset.sidebar === "collapsed"; }
  function paintSidebar() {
    var collapsed = isCollapsed();
    document.querySelectorAll("[data-sidebar-toggle]").forEach(function (b) {
      var label = collapsed ? (b.dataset.labelExpand || "Expand sidebar") : (b.dataset.labelCollapse || "Collapse sidebar");
      b.setAttribute("aria-expanded", String(!collapsed));
      b.setAttribute("aria-label", label);
      b.title = label;
    });
  }
  document.querySelectorAll("[data-sidebar-toggle]").forEach(function (b) {
    b.addEventListener("click", function () {
      var next = isCollapsed() ? "expanded" : "collapsed";
      d.dataset.sidebar = next;
      try { localStorage.setItem("baton-sidebar", next); } catch (e) {}
      paintSidebar();
    });
  });
  paintSidebar();

  var activeTab = document.querySelector('.tab-bar a[aria-current="page"]');
  if (activeTab && typeof activeTab.scrollIntoView === "function") {
    try { activeTab.scrollIntoView({ inline: "center", block: "nearest" }); } catch (e) {}
  }
})();
