// Live view of the running SteamCMD job.
//
// Polling rather than a stream: this runs at most one job at a time, output is
// modest, and a poll survives a dropped connection without extra plumbing.
// The log stream in phase 5 has different requirements and uses SSE.

(function () {
  "use strict";

  var script = document.currentScript;
  var cfg = {
    activeUrl: script.dataset.activeUrl,
    installUrl: script.dataset.installUrl,
    updateUrl: script.dataset.updateUrl,
    csrf: script.dataset.csrf,
    kinds: script.dataset.kinds || "",
  };

  var el = {
    card: document.getElementById("steamcmd-card"),
    detail: document.getElementById("job-detail"),
    output: document.getElementById("job-output"),
    outputWrap: document.getElementById("job-output-wrap"),
    title: document.getElementById("job-title"),
    state: document.getElementById("job-state"),
    error: document.getElementById("job-error"),
    guardForm: document.getElementById("guard-form"),
    guardPrompt: document.getElementById("guard-prompt"),
    guardCode: document.getElementById("guard-code"),
    actions: document.getElementById("job-actions"),
  };

  if (!el.card) return;

  // What the server rendered into the detail line - restored whenever there is
  // nothing else to say there.
  var baseDetail = el.detail.textContent.trim();

  var cursor = parseInt(script.dataset.initialCursor || "0", 10);
  var jobId = script.dataset.initialId || null;
  var timer = null;

  // Did we watch this job while it was still working? Only then is finishing
  // an event worth reloading for. Without this the last finished install stays
  // the active job forever and every fresh page load would reload again.
  var wasUnfinished = false;

  var IDLE_MS = 4000;   // nothing running: just notice when something starts
  var BUSY_MS = 1000;   // a job is running: keep the output moving

  function post(url, body) {
    return fetch(url, {
      method: "POST",
      credentials: "same-origin",
      headers: {
        "X-CSRFToken": cfg.csrf,
        "Content-Type": "application/json",
        Accept: "application/json",
      },
      body: JSON.stringify(body || {}),
    }).then(function (r) {
      // The CSRF token lives in the session, so an expired session shows up as
      // 400 rather than 401 - the CSRF check runs before the login check.
      // Either way the page is stale: reload so the user lands on the login form.
      if (r.status === 400 || r.status === 401) {
        window.location.reload();
        return { ok: false, handled: true };
      }
      return r.json().catch(function () {
        return { ok: r.ok };
      });
    });
  }

  function setBadge(state) {
    var map = {
      running: "text-bg-primary",
      queued: "text-bg-secondary",
      needs_guard: "text-bg-warning",
      success: "text-bg-success",
      failed: "text-bg-danger",
      cancelled: "text-bg-secondary",
    };
    el.state.className = "badge " + (map[state] || "text-bg-secondary");
    el.state.textContent = state.replace("_", " ");
  }

  function scrollToEnd() {
    el.output.scrollTop = el.output.scrollHeight;
  }

  function setActionsDisabled(disabled) {
    el.actions.querySelectorAll("[data-job-action]").forEach(function (btn) {
      if (btn.dataset.jobAction !== "cancel") btn.disabled = disabled;
    });
  }

  function appendLines(lines) {
    if (!lines || !lines.length) return;
    // Stay pinned to the bottom only if the user has not scrolled up.
    var atBottom =
      el.output.scrollHeight - el.output.scrollTop - el.output.clientHeight < 40;
    el.output.textContent += (el.output.textContent ? "\n" : "") + lines.join("\n");
    if (atBottom) scrollToEnd();
  }

  function render(payload) {
    var job = payload.job;

    // Something else holds the exclusive job slot - a mod download, say. It is
    // not shown here, but it does decide whether these buttons can work.
    var blocked = payload.busy && (!job || job.is_final);
    if (blocked) {
      // Named as somebody else's work: under a heading that says "Server
      // files", a bare "Backup ... is running." reads as if this card were
      // doing it, when the point is only why its buttons are dead.
      el.detail.textContent = "Another job is running: " + payload.busy_title + ".";
    } else if (!job) {
      el.detail.textContent = baseDetail;
    }
    setActionsDisabled(blocked);

    if (!job) {
      schedule(blocked ? BUSY_MS : IDLE_MS);
      return;
    }

    // A different job than the one we were following: start its output fresh.
    if (job.id !== jobId) {
      jobId = job.id;
      cursor = 0;
      wasUnfinished = false;
      el.output.textContent = "";
    }
    if (!job.is_final) wasUnfinished = true;

    // A job that had already finished when this page opened is history, not
    // something happening now. Its output would otherwise sit here for days,
    // under a card whose subtitle is supposed to say when the server files
    // were last updated.
    if (job.is_final && !wasUnfinished) {
      el.outputWrap.hidden = true;
      if (!blocked) el.detail.textContent = baseDetail;
      schedule(blocked ? BUSY_MS : IDLE_MS);
      return;
    }

    el.outputWrap.hidden = false;
    el.title.textContent = job.title;
    setBadge(job.state);
    el.detail.textContent = job.detail || "";

    if (job.gap) {
      appendLines(["[panel] ... earlier output dropped from the buffer ..."]);
    }
    appendLines(job.lines);
    cursor = job.next_index;

    el.error.hidden = !job.error;
    el.error.textContent = job.error || "";

    var guarding = job.state === "needs_guard";
    el.guardForm.hidden = !guarding;
    if (guarding) {
      el.guardPrompt.textContent = job.guard_prompt || "";
      if (document.activeElement !== el.guardCode) el.guardCode.focus();
    }

    var running = !job.is_final;
    el.actions.querySelectorAll('[data-job-action="cancel"]').forEach(function (btn) {
      btn.hidden = !running;
    });
    setActionsDisabled(running);

    // An install that just finished changes what the rest of the page shows.
    if (wasUnfinished && job.is_final && job.state === "success" && job.kind === "install") {
      window.setTimeout(function () {
        window.location.reload();
      }, 1500);
      return;
    }

    schedule(running ? BUSY_MS : IDLE_MS);
  }

  function poll() {
    fetch(cfg.activeUrl + "?after=" + cursor + "&kinds=" + encodeURIComponent(cfg.kinds), {
      headers: { Accept: "application/json" },
      credentials: "same-origin",
    })
      .then(function (r) {
        if (r.status === 401 || r.redirected) {
          window.location.reload(); // session expired
          return null;
        }
        return r.json();
      })
      .then(function (data) {
        if (data) render(data);
      })
      .catch(function () {
        schedule(IDLE_MS); // network hiccup - try again later
      });
  }

  function schedule(delay) {
    window.clearTimeout(timer);
    timer = window.setTimeout(poll, delay);
  }

  el.actions.addEventListener("click", function (event) {
    var btn = event.target.closest("[data-job-action]");
    if (!btn) return;
    event.preventDefault();

    var action = btn.dataset.jobAction;
    var url =
      action === "install" ? cfg.installUrl
      : action === "update" ? cfg.updateUrl
      : "/jobs/" + jobId + "/cancel";

    btn.disabled = true;
    post(url).then(function (res) {
      if (res && res.handled) return;

      // The server is still running: the panel asks before overwriting files
      // it has open, and only then repeats the request with force.
      if (res && res.needs_confirm) {
        if (!window.confirm(res.error)) {
          poll();
          return;
        }
        return post(url, { force: true }).then(finish);
      }
      finish(res);
    });

    function finish(res) {
      if (res && res.handled) return;
      if (res && res.error) {
        el.error.hidden = false;
        el.error.textContent = res.error;
      }
      cursor = 0;
      poll();
    }
  });

  el.guardForm.addEventListener("submit", function (event) {
    event.preventDefault();
    var code = el.guardCode.value.trim();
    if (!code || !jobId) return;

    post("/jobs/" + jobId + "/guard", { code: code }).then(function (res) {
      if (res && res.handled) return;
      el.guardCode.value = "";
      if (res && res.ok === false) {
        el.error.hidden = false;
        el.error.textContent = res.error || "The code was not accepted.";
      }
      poll();
    });
  });

  // The server-rendered output opens scrolled to the top, where the oldest
  // lines are. What matters is how the job ended, so start at the end.
  scrollToEnd();
  poll();
})();
