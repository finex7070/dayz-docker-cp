// Keeps values that age on their own up to date, without reloading the page.
//
// Any element marked `data-live="<key>"` is filled from the status endpoint.
// Two rates are at work: the poll fetches the truth from the server, and a
// local one second tick advances durations in between - otherwise the uptime
// would sit frozen until the next poll and look broken.

(function () {
  "use strict";

  var script = document.currentScript;
  var url = script.dataset.statusUrl;

  var POLL_MS = 10000;
  var TICK_MS = 1000;

  var nodes = Array.prototype.slice.call(document.querySelectorAll("[data-live]"));
  if (!url || !nodes.length) return;

  // Remember the classes the page shipped with, so a state class can be
  // swapped without eating the layout classes next to it.
  nodes.forEach(function (node) {
    node.liveBaseClass = node.className;
  });

  // Mirrors the `duration` template filter in routes/dashboard.py, which
  // renders the very same value before this script takes over.
  function formatDuration(seconds) {
    var total = Math.max(0, Math.floor(seconds));
    var days = Math.floor(total / 86400);
    var hours = Math.floor((total % 86400) / 3600);
    var minutes = Math.floor((total % 3600) / 60);
    var rest = total % 60;

    if (days) return days + "d " + hours + "h " + minutes + "m";
    if (hours) return hours + "h " + minutes + "m";
    if (minutes) return minutes + "m " + rest + "s";
    return rest + "s";
  }

  function paint(node) {
    if (node.liveValue === undefined) return;
    // null means "there is no value right now" - a stopped server has no
    // uptime. Rendering the dash beats leaving the last number on screen,
    // where it looks like a value that simply stopped updating.
    if (node.liveValue === null) {
      node.textContent = "—";
      return;
    }
    node.textContent =
      node.dataset.liveFormat === "duration"
        ? formatDuration(node.liveValue)
        : String(node.liveValue);
  }

  function apply(data) {
    nodes.forEach(function (node) {
      var classKey = node.dataset.liveClass;
      if (classKey && Object.prototype.hasOwnProperty.call(data, classKey)) {
        node.className = (node.liveBaseClass + " " + data[classKey]).trim();
      }

      var key = node.dataset.live;
      if (!Object.prototype.hasOwnProperty.call(data, key)) return;
      node.liveValue = data[key];
      paint(node);
    });
  }

  function tick() {
    nodes.forEach(function (node) {
      if (node.dataset.liveFormat !== "duration") return;
      if (typeof node.liveValue !== "number") return;
      node.liveValue += TICK_MS / 1000;
      paint(node);
    });
  }

  function poll() {
    // A hidden tab shows nobody anything - skip the request and catch up when
    // it comes back into view.
    if (document.hidden) return;

    fetch(url, { headers: { Accept: "application/json" }, credentials: "same-origin" })
      .then(function (response) {
        // Signed out: the endpoint redirects to the login form. Reload rather
        // than leave the page sitting there with values that never move again.
        if (response.redirected || response.status === 401) {
          window.location.reload();
          return null;
        }
        return response.ok ? response.json() : null;
      })
      .then(function (data) {
        if (data) apply(data);
      })
      .catch(function () {
        // A missed poll corrects itself on the next one.
      });
  }

  document.addEventListener("visibilitychange", function () {
    if (!document.hidden) poll();
  });

  window.setInterval(tick, TICK_MS);
  window.setInterval(poll, POLL_MS);
  poll();
})();
