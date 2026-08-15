// Log viewer: pick a type, pick a file, read it.
//
// Not a live stream. These files are already on disk and most of them belong
// to servers that stopped hours ago; a tail would sit there producing nothing.
// The auto reload polls size and mtime instead and only fetches the file when
// one of them moved - a few hundred bytes per check rather than the file.

(function () {
  "use strict";

  var script = document.currentScript;
  var cfg = {
    filesUrl: script.dataset.filesUrl,
    contentUrl: script.dataset.contentUrl,
    statUrl: script.dataset.statUrl,
    downloadUrl: script.dataset.downloadUrl,
  };

  var el = {
    card: document.getElementById("log-card"),
    type: document.getElementById("log-type"),
    file: document.getElementById("log-file"),
    filter: document.getElementById("log-filter"),
    autoreload: document.getElementById("log-autoreload"),
    download: document.getElementById("log-download"),
    output: document.getElementById("log-output"),
    detail: document.getElementById("log-detail"),
    truncated: document.getElementById("log-truncated"),
  };

  if (!el.card) return;

  var POLL_MS = 3000;

  var lines = [];
  var known = { size: -1, modified: -1 };
  var pollTimer = null;

  var ERROR = /\b(error|failed|failure|cannot|exception|fatal)\b/i;
  var WARN = /\b(warn|warning|deprecated|missing)\b/i;

  function escapeHtml(text) {
    return text.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  }

  function lineHtml(line) {
    var cls = ERROR.test(line) ? "log-error" : WARN.test(line) ? "log-warn" : "";
    var body = escapeHtml(line);
    return cls ? '<span class="' + cls + '">' + body + "</span>" : body;
  }

  function params() {
    return (
      "?type=" + encodeURIComponent(el.type.value) +
      "&name=" + encodeURIComponent(el.file.value || "")
    );
  }

  function formatSize(bytes) {
    if (bytes < 1024) return bytes + " B";
    if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + " KB";
    return (bytes / 1024 / 1024).toFixed(1) + " MB";
  }

  function render() {
    var needle = el.filter.value.trim().toLowerCase();
    var visible = needle
      ? lines.filter(function (l) { return l.toLowerCase().indexOf(needle) !== -1; })
      : lines;

    el.output.innerHTML = visible.map(lineHtml).join("\n");
    el.detail.textContent = needle
      ? visible.length + " of " + lines.length + " lines"
      : lines.length + " lines";
    el.output.scrollTop = el.output.scrollHeight;
  }

  function loadFiles() {
    el.file.innerHTML = "";
    return fetch(cfg.filesUrl + "?type=" + encodeURIComponent(el.type.value), {
      headers: { Accept: "application/json" },
      credentials: "same-origin",
    })
      .then(function (r) { return r.json(); })
      .then(function (data) {
        if (!data.files.length) {
          lines = ["[panel] No files of this type in the profiles directory yet."];
          el.file.innerHTML = '<option value="">no files</option>';
          render();
          return false;
        }
        // Newest first, and pre-selected: that is the file of the most recent
        // server run, which is what someone opening this page is after.
        data.files.forEach(function (file, index) {
          var option = document.createElement("option");
          option.value = file.name;
          option.textContent =
            file.name + "  ·  " + formatSize(file.size) +
            (index === 0 ? "  ·  newest" : "");
          el.file.appendChild(option);
        });
        return true;
      });
  }

  function loadContent() {
    if (!el.file.value) return;
    fetch(cfg.contentUrl + params(), {
      headers: { Accept: "application/json" },
      credentials: "same-origin",
    })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (data) {
        if (!data) return;
        lines = data.lines;
        known.size = data.size;
        known.modified = data.modified;
        el.truncated.hidden = !data.truncated;
        render();
      })
      .catch(function () {});
  }

  function poll() {
    window.clearTimeout(pollTimer);
    if (!el.autoreload.checked || !el.file.value) return;

    fetch(cfg.statUrl + params(), {
      headers: { Accept: "application/json" },
      credentials: "same-origin",
    })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (data) {
        if (data && (data.size !== known.size || data.modified !== known.modified)) {
          loadContent();
        }
      })
      .catch(function () {})
      .finally(function () {
        pollTimer = window.setTimeout(poll, POLL_MS);
      });
  }

  function updateDownload() {
    el.download.href = cfg.downloadUrl + params();
  }

  function selectType() {
    window.history.replaceState({}, "", "?type=" + encodeURIComponent(el.type.value));
    loadFiles().then(function (hasFiles) {
      updateDownload();
      if (hasFiles) loadContent();
      poll();
    });
  }

  el.type.addEventListener("change", selectType);
  el.file.addEventListener("change", function () {
    updateDownload();
    known = { size: -1, modified: -1 };
    loadContent();
  });
  el.filter.addEventListener("input", render);
  el.autoreload.addEventListener("change", poll);

  selectType();
})();
