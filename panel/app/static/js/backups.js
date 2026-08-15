// Backups page: start a backup, restore or delete a snapshot, follow the job.
//
// Same shape as the mods page: one job at a time, polled with a cursor into
// its output, and a reload when it finishes - because a finished restore or
// delete changes the very list this page is showing.
(function () {
  "use strict";

  var cfg = document.currentScript.dataset;
  var RUN_URL = cfg.runUrl;
  var RETENTION_URL = cfg.retentionUrl;
  var ACTIVE_URL = cfg.activeUrl;
  var KINDS = cfg.kinds || "";
  var CSRF = cfg.csrf;

  var BUSY_MS = 1000;
  var IDLE_MS = 5000;

  var el = {
    job: document.getElementById("backup-job"),
    jobTitle: document.getElementById("backup-job-title"),
    jobState: document.getElementById("backup-job-state"),
    jobDetail: document.getElementById("backup-job-detail"),
    jobOutput: document.getElementById("backup-job-output"),
    jobError: document.getElementById("backup-job-error"),
    dialog: document.getElementById("backup-dialog"),
    dialogForm: document.getElementById("backup-dialog-form"),
    dialogTitle: document.getElementById("backup-dialog-title"),
    dialogBody: document.getElementById("backup-dialog-body"),
    dialogOk: document.getElementById("backup-dialog-ok"),
  };

  var jobId = null;
  var cursor = 0;
  var wasUnfinished = false;
  var timer = null;

  // --- confirmation dialog ---------------------------------------------------
  //
  // A Bootstrap modal rather than window.confirm(): browsers let a page
  // suppress the native dialogs, and a "restore" that silently does nothing
  // because a checkbox was ticked once is the worst possible failure here.

  var modal = window.bootstrap ? new window.bootstrap.Modal(el.dialog) : null;
  var pending = null;

  function ask(spec) {
    return new Promise(function (resolve) {
      if (!modal) {
        resolve(window.confirm(spec.title));
        return;
      }
      el.dialogTitle.textContent = spec.title;
      el.dialogBody.innerHTML = spec.body;
      el.dialogOk.textContent = spec.ok || "OK";
      el.dialogOk.className = "btn " + (spec.danger === false ? "btn-primary" : "btn-danger");
      pending = resolve;
      modal.show();
    });
  }

  if (el.dialogForm) {
    el.dialogForm.addEventListener("submit", function (event) {
      event.preventDefault();
      modal.hide();
      settle(true);
    });
    el.dialog.addEventListener("hidden.bs.modal", function () { settle(false); });
  }

  function settle(value) {
    var resolve = pending;
    pending = null;
    if (resolve) resolve(value);
  }

  // --- starting things -------------------------------------------------------

  function post(url) {
    return fetch(url, {
      method: "POST",
      credentials: "same-origin",
      headers: { "X-CSRFToken": CSRF, Accept: "application/json" },
    })
      .then(function (r) { return r.json().catch(function () { return null; }); })
      .then(function (data) {
        if (data && data.error) showError(data.error);
        if (data && data.ok) {
          jobId = null;      // a fresh job: start its output from the top
          cursor = 0;
          wasUnfinished = true;
          el.jobOutput.textContent = "";
          showError("");
          schedule(200);
        }
        return data;
      })
      .catch(function () {
        showError("The panel did not answer. Reload the page to see what happened.");
      });
  }

  document.addEventListener("click", function (event) {
    var btn = event.target.closest("[data-backup-action]");
    if (!btn || btn.disabled) return;
    event.preventDefault();

    var action = btn.dataset.backupAction;
    var row = btn.closest("[data-snapshot]");
    var id = row ? row.dataset.snapshot : "";
    var when = row ? row.querySelector("td").textContent.trim().split("\n")[0] : "";

    if (action === "run") {
      post(RUN_URL);
      return;
    }
    if (action === "retention") {
      ask({
        title: "Apply the retention rules now?",
        body: "Snapshots outside the rules are deleted and the space they hold "
            + "is freed. This cannot be undone.",
        ok: "Apply",
      }).then(function (yes) { if (yes) post(RETENTION_URL); });
      return;
    }
    if (action === "restore") {
      ask({
        title: "Restore this snapshot?",
        body: "<p>The server directory is put back to its state of <strong>"
            + escapeHtml(when) + "</strong>. Anything created since then is "
            + "<strong>deleted</strong> - that is what makes it a restore and "
            + "not a copy over the top.</p>"
            + "<p class='mb-0'>The panel stops the server, takes a "
            + "<code>pre-restore</code> snapshot of the current state and starts "
            + "the server again afterwards.</p>",
        ok: "Stop the server and restore",
      }).then(function (yes) { if (yes) post("/backups/" + id + "/restore"); });
      return;
    }
    if (action === "delete") {
      ask({
        title: "Delete this snapshot?",
        body: "The snapshot from <strong>" + escapeHtml(when) + "</strong> is "
            + "removed and the blocks no other snapshot uses are freed.",
        ok: "Delete",
      }).then(function (yes) { if (yes) post("/backups/" + id + "/delete"); });
    }
  });

  function escapeHtml(text) {
    var div = document.createElement("div");
    div.textContent = text;
    return div.innerHTML;
  }

  // --- job output ------------------------------------------------------------

  function showError(message) {
    el.jobError.hidden = !message;
    el.jobError.textContent = message || "";
    if (message) el.job.hidden = false;
  }

  function setBadge(state) {
    var map = {
      running: "text-bg-primary",
      success: "text-bg-success",
      failed: "text-bg-danger",
      cancelled: "text-bg-secondary",
    };
    el.jobState.className = "badge " + (map[state] || "text-bg-secondary");
    el.jobState.textContent = state.replace("_", " ");
  }

  function appendLines(lines) {
    if (!lines || !lines.length) return;
    var atBottom =
      el.jobOutput.scrollHeight - el.jobOutput.scrollTop - el.jobOutput.clientHeight < 40;
    el.jobOutput.textContent +=
      (el.jobOutput.textContent ? "\n" : "") + lines.join("\n");
    if (atBottom) el.jobOutput.scrollTop = el.jobOutput.scrollHeight;
  }

  function render(job) {
    if (!job) {
      schedule(IDLE_MS);
      return;
    }

    if (job.id !== jobId) {
      jobId = job.id;
      cursor = 0;
      el.jobOutput.textContent = "";
    }
    if (!job.is_final) wasUnfinished = true;

    // A job that had already finished when this page opened is history. Showing
    // it would make a completed run look like one that is stuck.
    if (job.is_final && !wasUnfinished) {
      schedule(IDLE_MS);
      return;
    }

    el.job.hidden = false;
    el.jobTitle.textContent = job.title;
    el.jobDetail.textContent = job.detail || "";
    setBadge(job.state);

    if (job.gap) appendLines(["[panel] ... earlier output dropped ..."]);
    appendLines(job.lines);
    cursor = job.next_index;
    showError(job.error || "");

    if (wasUnfinished && job.is_final && job.state === "success") {
      // The snapshot list and the repository size on this page are both stale
      // now - a reload is simpler and more honest than patching the table.
      window.setTimeout(function () { window.location.reload(); }, 1500);
      return;
    }

    schedule(job.is_final ? IDLE_MS : BUSY_MS);
  }

  function poll() {
    fetch(ACTIVE_URL + "?after=" + cursor + "&kinds=" + encodeURIComponent(KINDS), {
      credentials: "same-origin",
      headers: { Accept: "application/json" },
    })
      .then(function (r) {
        if (r.status === 401 || r.redirected) {
          window.location.reload();
          return null;
        }
        return r.json();
      })
      .then(function (data) {
        if (!data) return;
        render(data.job);
        // Another page's job holds the only slot: say so instead of letting a
        // click fail with "another job is already running".
        document.querySelectorAll('[data-backup-action="run"]').forEach(function (btn) {
          btn.disabled = data.busy;
          btn.title = data.busy ? "Busy: " + data.busy_title : "";
        });
      })
      .catch(function () { schedule(IDLE_MS); });
  }

  function schedule(delay) {
    window.clearTimeout(timer);
    timer = window.setTimeout(poll, delay);
  }

  poll();
})();
