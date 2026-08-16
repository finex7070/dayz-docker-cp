// Schedules: one editor at the top, the list below.
//
// Every write returns the full list, and the table is rebuilt from that rather
// than patched in place. An entry's next run changes as a side effect of
// saving it, so a locally patched row would show a time the server does not
// agree with.

(function () {
  "use strict";

  var script = document.currentScript;
  var cfg = {
    listUrl: script.dataset.listUrl,
    baseUrl: script.dataset.baseUrl,
    csrf: script.dataset.csrf,
    needsCommand: (script.dataset.needsCommand || "").split(",").filter(Boolean),
    maxActions: parseInt(script.dataset.maxActions, 10) || 10,
  };

  var el = {
    id: document.getElementById("schedule-id"),
    name: document.getElementById("schedule-name"),
    cron: document.getElementById("schedule-cron"),
    preset: document.getElementById("schedule-preset"),
    enabled: document.getElementById("schedule-enabled"),
    rows: document.getElementById("action-rows"),
    template: document.getElementById("action-template"),
    list: document.getElementById("schedule-rows"),
    error: document.getElementById("schedule-error"),
    title: document.getElementById("editor-title"),
    cancel: document.getElementById("editor-cancel"),
    add: document.getElementById("add-action"),
    limit: document.getElementById("action-limit"),
  };

  if (!el.list) return;

  var busy = false;

  // --- helpers -------------------------------------------------------------

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
      if (r.status === 401) {
        window.location.reload();
        return { ok: false, handled: true };
      }
      return r.json().catch(function () {
        // Every answer of ours is JSON, so anything else is the CSRF check
        // rejecting an expired session before the route ever ran.
        window.location.reload();
        return { ok: false, handled: true };
      });
    });
  }

  function setError(message) {
    el.error.hidden = !message;
    el.error.textContent = message || "";
  }

  function text(value) {
    var node = document.createElement("span");
    node.textContent = value;
    return node;
  }

  // --- action rows ---------------------------------------------------------

  // Only for pairing each row's switch with its own label - a cloned template
  // would otherwise give every row the same id, and clicking one label would
  // toggle the first row's switch.
  var seq = 0;

  function addActionRow(action) {
    var row = el.template.content.firstElementChild.cloneNode(true);
    var kind = row.querySelector("[data-action-kind]");
    var command = row.querySelector("[data-action-command]");
    var delay = row.querySelector("[data-action-delay]");
    var carryOn = row.querySelector("[data-action-continue]");

    carryOn.id = "action-continue-" + (++seq);
    row.querySelector("[data-action-continue-label]").htmlFor = carryOn.id;

    if (action) {
      kind.value = action.kind;
      command.value = action.command || "";
      delay.value = action.delay ? String(action.delay) : "";
      carryOn.checked = !!action.continue_on_fail;
    }
    syncCommand(kind, command);
    kind.addEventListener("change", function () { syncCommand(kind, command); });

    el.rows.appendChild(row);
    syncRowButtons();
    return row;
  }

  function readRow(row) {
    return {
      kind: row.querySelector("[data-action-kind]").value,
      command: row.querySelector("[data-action-command]").value.trim(),
      // Sent as typed, so a "5m" comes back as the server's own complaint
      // about it rather than as a silent zero.
      delay: row.querySelector("[data-action-delay]").value.trim(),
      continue_on_fail: row.querySelector("[data-action-continue]").checked,
    };
  }

  // Below the row it came from, not at the end: one copies an announcement to
  // write the next announcement, and the next one belongs after this one.
  function copyActionRow(row) {
    if (actionRows().length >= cfg.maxActions) return;
    var copy = addActionRow(readRow(row));
    el.rows.insertBefore(copy, row.nextSibling);
    syncRowButtons();
    copy.querySelector("[data-action-delay]").focus();
  }

  function actionRows() {
    return Array.prototype.slice.call(el.rows.querySelectorAll(".action-row"));
  }

  // Up on the first row and down on the last would do nothing, and a row that
  // cannot be removed is the last one - saying so is cheaper than explaining it
  // after the click did not happen.
  function syncRowButtons() {
    var rows = actionRows();
    var full = rows.length >= cfg.maxActions;
    rows.forEach(function (row, index) {
      row.querySelector("[data-schedule-action='move-up']").disabled = index === 0;
      row.querySelector("[data-schedule-action='move-down']").disabled =
        index === rows.length - 1;
      row.querySelector("[data-schedule-action='remove-action']").disabled =
        rows.length < 2;
      row.querySelector("[data-schedule-action='copy-action']").disabled = full;
    });
    // The limit is the server's, and it refuses the save rather than the row -
    // which would be found out after everything else had been typed in.
    el.add.disabled = full;
    el.limit.hidden = !full;
  }

  function moveActionRow(row, delta) {
    var rows = actionRows();
    var target = rows.indexOf(row) + delta;
    if (target < 0 || target >= rows.length) return;
    // Moving the node keeps what was typed into it; re-rendering would not.
    if (delta < 0) el.rows.insertBefore(row, rows[target]);
    else el.rows.insertBefore(rows[target], row);
    syncRowButtons();
  }

  // The command field only exists for the kinds that take one - showing an
  // empty box next to "Stop the server" invites typing something into it.
  function syncCommand(kind, command) {
    var needed = cfg.needsCommand.indexOf(kind.value) !== -1;
    command.hidden = !needed;
    if (!needed) command.value = "";
  }

  function readActions() {
    return actionRows().map(readRow);
  }

  // --- editor --------------------------------------------------------------

  function reset() {
    el.id.value = "";
    el.name.value = "";
    el.cron.value = "";
    el.preset.value = "";
    el.enabled.checked = true;
    el.rows.textContent = "";
    addActionRow(null);
    el.title.textContent = "New schedule";
    el.cancel.hidden = true;
    setError("");
  }

  function edit(schedule) {
    el.id.value = schedule.id;
    el.name.value = schedule.name;
    el.cron.value = schedule.cron;
    el.preset.value = "";
    el.enabled.checked = schedule.enabled;
    el.rows.textContent = "";
    schedule.actions.forEach(addActionRow);
    el.title.textContent = "Editing: " + schedule.name;
    el.cancel.hidden = false;
    setError("");
    el.name.focus();
  }

  function save() {
    if (busy) return;
    var id = el.id.value;
    var body = {
      name: el.name.value,
      cron: el.cron.value,
      enabled: el.enabled.checked,
      actions: readActions(),
    };

    busy = true;
    post(id ? cfg.baseUrl + id : cfg.baseUrl, body).then(function (res) {
      busy = false;
      if (res.handled) return;
      if (res.schedules) show(res.schedules);
      if (!res.ok) {
        setError(res.error || "The schedule could not be saved.");
        return;
      }
      reset();
    });
  }

  // --- list ----------------------------------------------------------------

  function render(schedules) {
    el.list.textContent = "";

    if (!schedules.length) {
      var empty = document.createElement("tr");
      var cell = document.createElement("td");
      cell.colSpan = 6;
      cell.className = "text-secondary";
      cell.textContent = "No schedules yet.";
      empty.appendChild(cell);
      el.list.appendChild(empty);
      return;
    }

    schedules.forEach(function (schedule) {
      el.list.appendChild(row(schedule));
    });
  }

  function row(schedule) {
    var tr = document.createElement("tr");
    if (!schedule.enabled) tr.classList.add("text-secondary");

    var name = document.createElement("td");
    name.appendChild(text(schedule.name));
    if (!schedule.enabled) {
      var off = document.createElement("span");
      off.className = "badge text-bg-secondary ms-2";
      off.textContent = "disabled";
      name.appendChild(off);
    }
    tr.appendChild(name);

    var when = document.createElement("td");
    when.className = "font-monospace small";
    when.appendChild(text(schedule.cron));
    tr.appendChild(when);

    var actions = document.createElement("td");
    actions.className = "small";
    actions.appendChild(text(summary(schedule.actions)));
    // The chain in full on hover: a restart with five warnings is six lines,
    // and six lines per entry is what made the list unreadable.
    actions.title = schedule.actions.map(function (action, index) {
      return index + 1 + ". " + action.label + notes(action);
    }).join("\n");
    tr.appendChild(actions);

    var next = document.createElement("td");
    next.className = "small";
    next.appendChild(text(schedule.enabled ? schedule.next_run_text : "—"));
    tr.appendChild(next);

    tr.appendChild(lastRun(schedule));
    tr.appendChild(buttons(schedule));
    return tr;
  }

  // "5 × RCON command, Restart the server" - kinds in the order they first
  // appear, so the summary still reads as the shape of the chain. A count of
  // one is left off; "1 ×" is noise.
  function summary(actions) {
    var order = [];
    var counts = {};
    actions.forEach(function (action) {
      var kind = action.kind_label || action.kind;
      if (!(kind in counts)) { counts[kind] = 0; order.push(kind); }
      counts[kind] += 1;
    });
    return order.map(function (kind) {
      return counts[kind] > 1 ? counts[kind] + " × " + kind : kind;
    }).join(", ");
  }

  // What the row would otherwise hide: a five-minute wait in front of an action
  // is the difference between a restart at 04:00 and one at 04:05.
  function notes(action) {
    var parts = [];
    if (action.delay) parts.push("after " + action.delay + "s");
    if (action.continue_on_fail) parts.push("continues on failure");
    return parts.length ? " (" + parts.join(", ") + ")" : "";
  }

  function lastRun(schedule) {
    var cell = document.createElement("td");
    cell.className = "small";
    cell.appendChild(text(schedule.last_run_text));

    if (schedule.last_run) {
      var badge = document.createElement("span");
      badge.className = "badge ms-2 " + (schedule.last_ok ? "text-bg-success" : "text-bg-danger");
      badge.textContent = schedule.last_ok ? "ok" : "failed";
      // The full result is long and only interesting when something went
      // wrong, so it lives in the tooltip rather than in the row.
      badge.title = schedule.last_result || "";
      cell.appendChild(badge);
    }
    return cell;
  }

  function buttons(schedule) {
    var cell = document.createElement("td");
    cell.className = "text-end text-nowrap";

    [
      { action: "run", label: "Run now", css: "btn-outline-secondary" },
      { action: "toggle", label: schedule.enabled ? "Disable" : "Enable", css: "btn-outline-secondary" },
      { action: "duplicate", label: "Duplicate", css: "btn-outline-secondary" },
      { action: "edit", label: "Edit", css: "btn-outline-secondary" },
      { action: "delete", label: "Delete", css: "btn-outline-danger" },
    ].forEach(function (spec) {
      var btn = document.createElement("button");
      btn.className = "btn btn-sm ms-1 " + spec.css;
      btn.textContent = spec.label;
      btn.dataset.scheduleAction = spec.action;
      btn.dataset.scheduleId = schedule.id;
      cell.appendChild(btn);
    });
    return cell;
  }

  function find(id) {
    return current.filter(function (item) { return item.id === id; })[0];
  }

  // --- wiring --------------------------------------------------------------

  // The last list the server sent. The row buttons carry ids, not objects, so
  // this is what an id is resolved against.
  var current = [];

  function show(schedules) {
    current = schedules;
    render(schedules);
  }

  el.list.addEventListener("click", function (event) {
    var btn = event.target.closest("[data-schedule-action]");
    if (!btn || busy) return;

    var schedule = find(btn.dataset.scheduleId);
    if (!schedule) return;
    var action = btn.dataset.scheduleAction;

    if (action === "edit") {
      edit(schedule);
      window.scrollTo({ top: 0, behavior: "smooth" });
      return;
    }
    if (action === "delete"
        && !window.confirm("Delete the schedule \"" + schedule.name + "\"?")) return;
    if (action === "run" && !window.confirm(runPrompt(schedule))) return;

    busy = true;
    btn.disabled = true;
    var url = cfg.baseUrl + schedule.id + (action === "toggle" ? "" : "/" + action);
    var body = action === "toggle" ? { enabled: !schedule.enabled } : {};

    post(url, body).then(function (res) {
      busy = false;
      if (res.handled) return;
      if (res.schedules) show(res.schedules);
      setError(res.ok ? "" : (res.error || "That did not work."));
      // A run answers before it has finished, so the row it came back with
      // still shows the run before this one.
      if (res.ok && action === "run") window.setTimeout(refresh, 2000);
    });
  });

  // How long the chain waits before it is done, so the confirmation does not
  // promise something immediate when the entry sleeps for five minutes.
  function runPrompt(schedule) {
    var seconds = schedule.actions.reduce(function (total, action) {
      return total + (action.delay || 0);
    }, 0);
    if (!seconds) return "Run \"" + schedule.name + "\" now?";
    return "Run \"" + schedule.name + "\" now? Its delays add up to "
      + seconds + " seconds, so it runs in the background and the result "
      + "appears in the table when it is done.";
  }

  document.addEventListener("click", function (event) {
    var btn = event.target.closest("[data-schedule-action]");
    if (!btn || el.list.contains(btn)) return;
    var action = btn.dataset.scheduleAction;

    if (action === "add-action") { event.preventDefault(); addActionRow(null); }
    else if (action === "copy-action") {
      event.preventDefault();
      copyActionRow(btn.closest(".action-row"));
    }
    else if (action === "remove-action") {
      event.preventDefault();
      // Never leave the editor with no actions: an entry without one cannot be
      // saved, and an empty list gives no obvious way back.
      if (actionRows().length > 1) {
        btn.closest(".action-row").remove();
        syncRowButtons();
      }
    }
    else if (action === "move-up" || action === "move-down") {
      event.preventDefault();
      moveActionRow(btn.closest(".action-row"), action === "move-up" ? -1 : 1);
      // Reordering is done in runs, so the pointer stays where the next click
      // wants it - and moves to the twin when this one has hit the end.
      if (btn.disabled) {
        var twin = btn.parentElement.querySelector(
          "[data-schedule-action='" + (action === "move-up" ? "move-down" : "move-up") + "']");
        if (twin && !twin.disabled) twin.focus();
      } else {
        btn.focus();
      }
    }
    else if (action === "save") { event.preventDefault(); save(); }
    else if (action === "reset") { event.preventDefault(); reset(); }
  });

  el.preset.addEventListener("change", function () {
    if (el.preset.value) el.cron.value = el.preset.value;
  });

  var boot = document.getElementById("schedule-data");
  show(JSON.parse(boot.textContent || "[]"));
  reset();

  function refresh() {
    if (busy) return;
    fetch(cfg.listUrl, { headers: { Accept: "application/json" }, credentials: "same-origin" })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (data) { if (data) show(data.schedules); })
      .catch(function () {});
  }

  // Next-run times drift as entries fire; a slow refresh keeps the page honest
  // without polling like a status view.
  window.setInterval(refresh, 30000);
})();
