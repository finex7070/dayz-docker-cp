// Settings page: reveal password fields, and keep the active tab in the URL.
//
// The tab lives in the query string because saving a form redirects, and
// landing back on the first tab after saving the third one is disorienting.

(function () {
  "use strict";

  document.addEventListener("click", function (event) {
    var btn = event.target.closest("[data-toggle-password]");
    if (!btn) return;

    var input = document.querySelector(btn.dataset.togglePassword);
    if (!input) return;

    var hidden = input.type === "password";
    input.type = hidden ? "text" : "password";
    btn.textContent = hidden ? "Hide" : "Show";
  });

  document.querySelectorAll('[data-bs-toggle="tab"]').forEach(function (button) {
    button.addEventListener("shown.bs.tab", function () {
      var id = (button.dataset.bsTarget || "").replace("#tab-", "");
      if (!id) return;
      var url = new URL(window.location.href);
      url.searchParams.set("tab", id);
      window.history.replaceState({}, "", url);
    });
  });
})();
