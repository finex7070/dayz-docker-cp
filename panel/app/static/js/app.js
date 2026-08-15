// Panel front-end helpers.
//
// Deliberately plain: no build step, no framework. The pages are server
// rendered; JavaScript only adds the few things HTML cannot do on its own.

(function () {
  "use strict";

  // Auto-dismiss success messages. Warnings and errors stay until dismissed --
  // an operator should not miss "the server crashed" because it faded away.
  document.querySelectorAll(".alert-success").forEach(function (alert) {
    window.setTimeout(function () {
      if (window.bootstrap && window.bootstrap.Alert) {
        window.bootstrap.Alert.getOrCreateInstance(alert).close();
      }
    }, 5000);
  });

  // Copy-to-clipboard for anything carrying data-copy.
  document.addEventListener("click", function (event) {
    var button = event.target.closest("[data-copy]");
    if (!button) return;
    event.preventDefault();

    var text = button.dataset.copy;
    var done = function (ok) {
      var was = button.dataset.label || button.textContent;
      button.dataset.label = was;
      button.textContent = ok ? "Copied" : "Press Ctrl+C";
      window.setTimeout(function () { button.textContent = was; }, 1500);
    };

    // navigator.clipboard exists only in a secure context, and the panel is
    // meant to be reached over plain HTTP on a LAN address. The textarea
    // fallback is not legacy cruft here - it is the path most installs take.
    if (window.navigator.clipboard && window.isSecureContext) {
      window.navigator.clipboard.writeText(text).then(
        function () { done(true); },
        function () { done(legacyCopy(text)); }
      );
      return;
    }
    done(legacyCopy(text));
  });

  function legacyCopy(text) {
    var field = document.createElement("textarea");
    field.value = text;
    field.setAttribute("readonly", "");
    field.style.position = "fixed";
    field.style.opacity = "0";
    document.body.appendChild(field);
    field.select();

    var ok = false;
    try {
      ok = document.execCommand("copy");
    } catch (error) {
      ok = false;
    }
    document.body.removeChild(field);
    return ok;
  }
})();
