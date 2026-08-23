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

  // What the last click was told - kept until the next one, see applyStatus.
  var actionError = "";

  // The panel connects for the command itself, so there is no session to
  // report - what matters is whether a command can be sent, and what went
  // wrong the last time one was.
  function rconLabel(rcon) {
    if (!rcon.configured) {
      return {
        text: "RCON off",
        state: "",
        title: "No RCON password is set (Settings - General - BattlEye).",
      };
    }
    if (!rcon.ready) {
      return { text: "RCON idle", state: "", title: "Needs a running server." };
    }
    if (rcon.error) return { text: "RCON error", state: "warn", title: rcon.error };
    return { text: "RCON ready", state: "ok", title: "" };
  }

  function applyRcon(rcon) {
    var label = rconLabel(rcon);
    if (el.rconStatus) {
      el.rconStatus.textContent = label.text;
      el.rconStatus.title = label.title;
    }
    if (el.rconDot) el.rconDot.className = "status-dot status-dot-inline " + label.state;
    el.input.disabled = !rcon.ready;
    if (el.send) el.send.disabled = !rcon.ready;
  }

  function applyStopButton(btn, status) {
    var killing = status.state === "stopping";
    btn.textContent = killing ? "Kill" : "Stop";
    btn.classList.toggle("btn-danger", killing);
    btn.classList.toggle("btn-outline-danger", !killing);
    btn.dataset.force = killing ? "1" : "";
    btn.disabled = killing ? !status.can_kill : !status.can_stop;
    btn.title = killing
      ? "End the process now, without waiting for it to save"
      : "Ask the server to shut down and save";
  }

  function applyStatus(status) {
    if (!status) return;
    setAlert(el.blocked, status.blocked_reason);
    // Both share the band, and the poll that follows a click must not wipe
    // what the click just said: the server's own error is rarely set, so
    // "" would overwrite the answer within a moment of showing it.
    setAlert(el.error, status.error || actionError);

    var rcon = status.rcon || {};
    el.actions.querySelectorAll("[data-server-action]").forEach(function (btn) {
      var action = btn.dataset.serverAction;
      if (action === "start") btn.disabled = !status.can_start;
      // The same button, twice over: Stop while the server runs, Kill once a
      // shutdown is under way and not moving. Two buttons would leave one of
      // them greyed out at all times.
      else if (action === "stop") applyStopButton(btn, status);
      else if (action === "restart") btn.disabled = !status.can_stop;
      // A backup needs the job slot, not the server: it can run while the
      // server is stopped, and must not while SteamCMD is writing.
      else if (action === "backup") btn.disabled = !!status.job_busy;
      // Lock and Unlock go over RCON, which needs a running server and a
      // password - the connection itself is opened by the click.
      else btn.disabled = !rcon.ready;
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
    var force = action === "stop" && btn.dataset.force === "1";
    // Only the ones that interrupt play ask back. Lock is reversible with the
    // button next to it, and a confirm on every one of them trains the habit
    // of clicking it away.
    if (force) {
      if (!window.confirm(
        "Kill the server process now?\n\n"
        + "Everything it has not written yet is lost."
      )) return;
    } else if ((action === "restart" || action === "stop")
        && !window.confirm("Really " + action + " the server?")) return;

    btn.disabled = true;
    actionError = "";
    setAlert(el.error, "");
    // Backup lives on its own page and brings its own URL along; everything
    // else is a verb on /server.
    post(btn.dataset.url || "/server/" + action, force ? { force: true } : null)
      .then(function (res) {
        if (res && res.handled) return;
        actionError = (res && res.error) || "";
        if (actionError) setAlert(el.error, actionError);
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
