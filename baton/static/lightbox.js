// Native <dialog> lightbox for task and delivery image artifacts.
(function () {
  var dialog = document.getElementById("artifact-lightbox");
  if (!dialog) return;
  var img = dialog.querySelector("[data-lb-img]");
  var cap = dialog.querySelector("[data-lb-cap]");
  var closeBtn = dialog.querySelector("[data-lb-close]");

  document.addEventListener("click", function (ev) {
    var trigger = ev.target.closest("[data-lightbox-src]");
    if (!trigger) return;
    ev.preventDefault();
    img.src = trigger.dataset.lightboxSrc;
    img.alt = trigger.dataset.lightboxCap || "";
    cap.textContent = trigger.dataset.lightboxCap || "";
    cap.hidden = !cap.textContent;
    if (typeof dialog.showModal === "function") dialog.showModal();
    else dialog.setAttribute("open", "");
  });

  function close() {
    if (typeof dialog.close === "function") dialog.close();
    else dialog.removeAttribute("open");
    img.removeAttribute("src");
  }

  if (closeBtn) closeBtn.addEventListener("click", close);
  dialog.addEventListener("click", function (ev) {
    if (ev.target === dialog) close();
  });
})();
