// Dashboard: live console (SSE) and the server control buttons.
//
// The buttons sit here rather than with the status tiles because they share
// the same status payload: whether Start is clickable is the same question as
// what the state tile shows, and answering it twice invites the two to
// disagree on screen.

(function () {
  "use strict";

  var script = document.currentScript;
  var cfg = {
    streamUrl: script.dataset.streamUrl,
    sendUrl: script.dataset.sendUrl,
    statusUrl: script.dataset.statusUrl,
    csrf: script.dataset.csrf,
  };

  var el = {
    output: document.getElementById("console-output"),
    status: document.getElementById("console-status"),
    dot: document.getElementById("console-dot"),
    form: document.getElementById("console-form"),
    input: document.getElementById("console-input"),
    send: document.getElementById("console-send"),
    hint: document.getElementById("console-hint"),
    actions: document.getElementById("server-actions"),
    blocked: document.getElementById("server-blocked"),
    error: document.getElementById("server-error"),
    rconDot: document.getElementById("rcon-dot"),
    rconStatus: document.getElementById("rcon-status"),
  };

  if (!el.output || !el.actions) return;

  var MAX_LINES = 3000;
  var cursor = "";
  var source = null;
  var reconnectTimer = null;
  var lineCount = 0;

  // The stream is only open while a server process exists. A stopped server
  // produces no output, so a connection would show "live" next to a console
  // that can never say anything - and it would hold one of the few stream
  // slots, which are only freed when a write notices the tab is gone.
  var streaming = false;

  // --- console ------------------------------------------------------------

  function atBottom() {
    return el.output.scrollHeight - el.output.scrollTop - el.output.clientHeight < 40;
  }

  function append(lines) {
    if (!lines || !lines.length) return;
    var wasAtBottom = atBottom();

    var text = lines.join("\n");
    el.output.textContent += (el.output.textContent ? "\n" : "") + text;
    lineCount += lines.length;

    if (lineCount > MAX_LINES) {
      // Trim from the front so a server that logs for days does not grow the
      // page until the browser gives up.
      var kept = el.output.textContent.split("\n").slice(-MAX_LINES);
      el.output.textContent = kept.join("\n");
      lineCount = kept.length;
    }

    if (wasAtBottom) el.output.scrollTop = el.output.scrollHeight;
  }

  function setConsoleStatus(text, state) {
    el.status.textContent = text;
    el.dot.className = "status-dot status-dot-inline " + (state || "");
  }

  function connect() {
    window.clearTimeout(reconnectTimer);
    if (source) source.close();

    var url = cfg.streamUrl + (cursor ? "?after=" + encodeURIComponent(cursor) : "");
    setConsoleStatus("connecting ...", "");
    source = new EventSource(url);

    source.onopen = function () {
      setConsoleStatus("live", "ok");
    };

    source.onmessage = function (event) {
      var data = JSON.parse(event.data);
      cursor = data.cursor;
      if (data.reset) {
        el.output.textContent = "";
        lineCount = 0;
      }
      append(data.note ? [data.note].concat(data.lines) : data.lines);
    };

    source.onerror = function () {
      // EventSource retries by itself, but not once the server answered with
      // an error status (no free slot, expired session). Retry on our own
      // terms and keep the cursor so nothing is missed in between.
      source.close();
      source = null;
      if (!streaming) return;   // the server stopped, this is not a failure
      setConsoleStatus("reconnecting ...", "warn");
      reconnectTimer = window.setTimeout(connect, 5000);
    };
  }

  function disconnect() {
    window.clearTimeout(reconnectTimer);
    if (source) {
      source.close();
      source = null;
    }
    // The output stays on screen: what the server said on its way down is
    // usually the reason anyone is looking.
    setConsoleStatus("idle", "");
  }

  // --- controls -----------------------------------------------------------

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
      // An expired session fails the CSRF check first, so it arrives as 400.
      if (r.status === 400 || r.status === 401) {
        window.location.reload();
        return { ok: false, handled: true };
      }
      return r.json().catch(function () {
        return { ok: r.ok };
      });
    });
  }

  function setAlert(node, message) {
    node.hidden = !message;
    node.textContent = message || "";
  }

  // The RCON session has more states than "on" and "off", and the difference
  // matters: no password is something the operator has to fix, while "not
  // connected yet" resolves itself a few seconds after a start.
  function rconLabel(rcon) {
    if (rcon.connected) return { text: "RCON connected", state: "ok", title: "" };
    if (!rcon.configured) {
      return {
        text: "RCON off",
        state: "",
        title: "No RCON password is set (Settings - General - BattlEye).",
      };
    }
    if (rcon.state === "connecting") return { text: "RCON connecting ...", state: "warn", title: "" };
    if (rcon.error) return { text: "RCON unavailable", state: "warn", title: rcon.error };
    return { text: "RCON idle", state: "", title: "Connects once the server is running." };
  }

  function applyRcon(rcon) {
    var label = rconLabel(rcon);
    if (el.rconStatus) {
      el.rconStatus.textContent = label.text;
      el.rconStatus.title = label.title;
    }
    if (el.rconDot) el.rconDot.className = "status-dot status-dot-inline " + label.state;
    el.input.disabled = !rcon.connected;
    if (el.send) el.send.disabled = !rcon.connected;
  }

  function applyStatus(status) {
    if (!status) return;
    setAlert(el.blocked, status.blocked_reason);
    setAlert(el.error, status.error);

    var rcon = status.rcon || {};
    el.actions.querySelectorAll("[data-server-action]").forEach(function (btn) {
      var action = btn.dataset.serverAction;
      if (action === "start") btn.disabled = !status.can_start;
      else if (action === "restart" || action === "stop") btn.disabled = !status.can_stop;
      // A backup needs the job slot, not the server: it can run while the
      // server is stopped, and must not while SteamCMD is writing.
      else if (action === "backup") btn.disabled = !!status.job_busy;
      // Lock and Unlock are RCON commands, not process control: a running
      // server without an RCON session cannot do them.
      else btn.disabled = !rcon.connected;
    });
    applyRcon(rcon);

    // A pid exists from the moment the process is spawned, so the stream opens
    // early enough to catch the startup output rather than joining midway.
    var wanted = status.pid !== null && status.pid !== undefined;
    if (wanted === streaming) return;

    streaming = wanted;
    if (wanted) connect();
    else disconnect();
  }

  function refreshStatus() {
    fetch(cfg.statusUrl, {
      headers: { Accept: "application/json" },
      credentials: "same-origin",
    })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(applyStatus)
      .catch(function () {});
  }

  el.actions.addEventListener("click", function (event) {
    var btn = event.target.closest("[data-server-action]");
    if (!btn || btn.disabled) return;
    event.preventDefault();

    var action = btn.dataset.serverAction;
    // Only the two that interrupt play ask back. Lock is reversible with the
    // button next to it, and a confirm on every one of them trains the habit
    // of clicking it away.
    if ((action === "restart" || action === "stop")
        && !window.confirm("Really " + action + " the server?")) return;

    btn.disabled = true;
    // Backup lives on its own page and brings its own URL along; everything
    // else is a verb on /server.
    post(btn.dataset.url || "/server/" + action).then(function (res) {
      if (res && res.handled) return;
      if (res && res.error) setAlert(el.error, res.error);
      // "Updating before start: mods." - the work happens in the background,
      // so without this the button click would look like it did nothing.
      if (res && res.message) append(["[panel] " + res.message]);
      if (res && res.ok && action === "backup") {
        append(["[panel] Backup started - follow it on the Backups page."]);
      }
      refreshStatus();
    });
  });

  // --- command entry ------------------------------------------------------

  var history = [];
  var historyAt = -1;
  var defaultHint = el.hint.innerHTML;

  function setHint(message) {
    if (message) {
      el.hint.textContent = message;
      el.hint.classList.add("text-warning");
    } else {
      el.hint.innerHTML = defaultHint;
      el.hint.classList.remove("text-warning");
    }
  }

  el.form.addEventListener("submit", function (event) {
    event.preventDefault();
    var command = el.input.value.trim();
    if (!command) return;

    post(cfg.sendUrl, { command: command }).then(function (res) {
      if (res && res.handled) return;
      if (res && res.error) {
        setHint(res.error);
        return;
      }
      setHint("");
      // The answer is not printed here: the server route puts both the command
      // and its answer into the shared buffer, so it arrives over the stream
      // like everything else - and every open tab sees it, not just this one.
      if (history[history.length - 1] !== command) history.push(command);
      historyAt = history.length;
      el.input.value = "";
    });
  });

  el.input.addEventListener("keydown", function (event) {
    if (event.key !== "ArrowUp" && event.key !== "ArrowDown") return;
    if (!history.length) return;
    event.preventDefault();

    historyAt += event.key === "ArrowUp" ? -1 : 1;
    if (historyAt < 0) historyAt = 0;
    if (historyAt >= history.length) {
      historyAt = history.length;
      el.input.value = "";
      return;
    }
    el.input.value = history[historyAt];
    el.input.setSelectionRange(el.input.value.length, el.input.value.length);
  });

  // No connect() here: the status poll decides, and it runs immediately.
  refreshStatus();
  window.setInterval(refreshStatus, 5000);

  window.addEventListener("beforeunload", function () {
    if (source) source.close();
  });
})();
