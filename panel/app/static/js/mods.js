// Mods page: install, update and remove mods, and follow the download job.
//
// Polling, like the SteamCMD panel on the dashboard: one job at a time, modest
// output, and a poll that recovers from a dropped connection by itself.

(function () {
  "use strict";

  var script = document.currentScript;
  var CSRF = script.dataset.csrf;
  var ACTIVE_URL = script.dataset.activeUrl;
  var KINDS = script.dataset.kinds || "";

  var IDLE_MS = 5000;
  var BUSY_MS = 1000;

  var el = {
    query: document.getElementById("mod-query"),
    installNote: document.getElementById("install-note"),
    modzip: document.getElementById("modzip-file"),
    modzipNote: document.getElementById("modzip-note"),
    search: document.getElementById("mod-search"),
    results: document.getElementById("mod-results"),
    clear: document.getElementById("mod-clear"),
    pager: document.getElementById("mod-pager"),
    pagerInfo: document.getElementById("mod-pager-info"),
    job: document.getElementById("mod-job"),
    jobTitle: document.getElementById("mod-job-title"),
    jobState: document.getElementById("mod-job-state"),
    jobDetail: document.getElementById("mod-job-detail"),
    jobOutput: document.getElementById("mod-job-output"),
    jobError: document.getElementById("mod-job-error"),
    modlist: document.getElementById("modlist-file"),
    modlistNote: document.getElementById("modlist-note"),
  };

  var cursor = 0;
  var jobId = null;
  var wasUnfinished = false;
  var timer = null;

  // Paging is server side, so the page number has to survive between clicks.
  var searchQuery = "";
  var searchPage = 1;

  // --- helpers ---------------------------------------------------------------

  function post(url, body) {
    return fetch(url, {
      method: "POST",
      credentials: "same-origin",
      headers: {
        "X-CSRFToken": CSRF,
        "Content-Type": "application/json",
        Accept: "application/json",
      },
      body: JSON.stringify(body || {}),
    }).then(readJson);
  }

  function readJson(response) {
    // An expired session fails the CSRF check before the login check, so it
    // arrives as an HTML 400 rather than a 401. Either way the page is stale
    // and reloading lands on the login form.
    var type = response.headers.get("Content-Type") || "";
    var stale = response.status === 401 || response.redirected ||
      (response.status === 400 && type.indexOf("json") === -1);
    if (stale) {
      window.location.reload();
      return { ok: false, handled: true };
    }
    return response.json().catch(function () {
      return { ok: false, error: "The panel sent an unreadable reply." };
    });
  }

  // Each card answers in its own box: what went wrong belongs next to the
  // field it was about, not in the job output further down the page.
  function note(node, message, bad) {
    if (!node) return;
    node.hidden = !message;
    node.textContent = message || "";
    node.className = "alert mt-3 mb-0 " + (bad ? "alert-danger" : "alert-secondary");
  }

  function modlistNote(message, bad) {
    note(el.modlistNote, message, bad);
  }

  function showError(message) {
    el.jobError.hidden = !message;
    el.jobError.textContent = message || "";
    if (message) el.job.hidden = false;
  }

  // A mod action that needs confirmation asks once, then repeats with force.
  function send(url, body) {
    return post(url, body).then(function (res) {
      if (res.handled) return res;
      if (res.needs_confirm) {
        if (!window.confirm(res.error)) return { ok: false, cancelled: true };
        var forced = Object.assign({}, body || {}, { force: true });
        return post(url, forced);
      }
      return res;
    });
  }

  function afterChange(res) {
    if (res.handled || res.cancelled) return;
    if (res.ok === false) {
      showError(res.error || "The request failed.");
      return;
    }
    if (res.job_id) {
      showError("");
      cursor = 0;
      jobId = res.job_id;
      poll();
      return;
    }
    window.location.reload();
  }

  // --- job output ------------------------------------------------------------

  function setBadge(state) {
    var map = {
      running: "text-bg-primary",
      queued: "text-bg-secondary",
      needs_guard: "text-bg-warning",
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
      wasUnfinished = false;
      el.jobOutput.textContent = "";
    }
    if (!job.is_final) wasUnfinished = true;

    // A job that was already finished when this page opened is history, not
    // something happening now. Showing it makes a completed run look like one
    // that is stuck - and it can contradict the list below it, because the
    // mod it installed may have been removed since.
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

    var running = !job.is_final;
    document.querySelectorAll('[data-mod-action="cancel"]').forEach(function (btn) {
      btn.hidden = !running;
    });

    // A finished download changed the mod list this page is showing.
    if (wasUnfinished && job.is_final) {
      if (job.state === "success") {
        window.setTimeout(function () {
          window.location.reload();
        }, 1200);
        return;
      }
    }

    schedule(running ? BUSY_MS : IDLE_MS);
  }

  function poll() {
    // Only mod jobs: a server file update belongs on the dashboard, under the
    // card that started it.
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
        if (data) render(data.job);
      })
      .catch(function () {
        schedule(IDLE_MS);
      });
  }

  function schedule(delay) {
    window.clearTimeout(timer);
    timer = window.setTimeout(poll, delay);
  }

  // --- add and search --------------------------------------------------------

  // Uploading a mod is multipart, so it cannot go through post(): the browser
  // has to set the boundary itself. That also means the retry after a
  // confirmation appends the flag to the form rather than to a JSON body.
  function uploadMod(button) {
    var file = el.modzip && el.modzip.files[0];
    if (!file) {
      note(el.modzipNote, "Choose a zipped mod first.", true);
      return;
    }

    function attempt(force) {
      var body = new FormData();
      body.append("mod", file);
      if (force) body.append("force", "1");
      return fetch("/mods/upload", {
        method: "POST",
        credentials: "same-origin",
        headers: { "X-CSRFToken": CSRF, Accept: "application/json" },
        body: body,
      }).then(readJson);
    }

    button.disabled = true;
    note(el.modzipNote, "Uploading " + file.name + " ...");
    attempt(false).then(function (res) {
      if (res.needs_confirm) {
        if (!window.confirm(res.error)) return { ok: false, cancelled: true };
        return attempt(true);
      }
      return res;
    }).then(function (res) {
      button.disabled = false;
      if (res.handled) return;
      if (res.cancelled) {
        note(el.modzipNote, "");
        return;
      }
      if (!res.ok) {
        note(el.modzipNote, res.error || "The mod could not be unpacked.", true);
        return;
      }
      el.modzip.value = "";
      note(el.modzipNote, res.message || "");
      afterChange(res);
    });
  }

  function renderResults(data) {
    el.results.innerHTML = "";
    el.pager.hidden = true;
    // Something is on screen either way - even "Nothing found." is worth a way
    // back to an empty page.
    if (el.clear) el.clear.hidden = false;

    if (!data.items.length) {
      el.results.textContent =
        data.page > 1 ? "No more results." : "Nothing found.";
      return;
    }

    data.items.forEach(function (item) {
      el.results.appendChild(resultCard(item));
    });

    // Six results fill three rows exactly, so the pager only appears once
    // there is genuinely a second page.
    if (data.pages > 1) {
      el.pager.hidden = false;
      el.pagerInfo.textContent =
        "Page " + data.page + " of " + data.pages + " · " + data.total + " results";
      el.pager.querySelector('[data-mod-action="prev"]').disabled = data.page <= 1;
      el.pager.querySelector('[data-mod-action="next"]').disabled = data.page >= data.pages;
    }
  }

  function resultCard(item) {
    var col = document.createElement("div");
    col.className = "col-md-6";

    var card = document.createElement("div");
    card.className = "card h-100";

    var body = document.createElement("div");
    body.className = "card-body py-2 d-flex gap-3";

    body.appendChild(thumbnail(item));

    var text = document.createElement("div");
    text.className = "flex-grow-1 overflow-hidden";

    // The title opens the real workshop page: the description, screenshots and
    // comments there are what tell you whether a mod is worth installing, and
    // none of that belongs in this panel.
    var link = document.createElement("a");
    link.href = item.url;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    link.className = "fw-bold text-truncate d-block";
    link.title = item.title + " - open on Steam";
    link.textContent = item.title;
    text.appendChild(link);

    var meta = document.createElement("div");
    meta.className = "small text-secondary";
    meta.textContent = "ID " + item.workshop_id + " · " + item.size_mb + " MB";
    text.appendChild(meta);

    var button = document.createElement("button");
    button.className = "btn btn-sm mt-2 " +
      (item.installed ? "btn-outline-secondary" : "btn-outline-primary");
    button.textContent = item.installed ? "Installed" : "Install";
    button.disabled = item.installed;
    button.addEventListener("click", function () {
      button.disabled = true;
      send("/mods/install", {
        query: String(item.workshop_id),
      }).then(afterChange);
    });
    text.appendChild(button);

    body.appendChild(text);
    card.appendChild(body);
    col.appendChild(card);
    return col;
  }

  function thumbnail(item) {
    if (!item.preview_url) {
      var blank = document.createElement("div");
      blank.className = "mod-thumb mod-thumb-empty";
      blank.textContent = "no image";
      return blank;
    }

    var img = document.createElement("img");
    img.className = "mod-thumb";
    img.src = item.preview_url;
    img.alt = "";
    img.loading = "lazy";
    // The image comes from Steam's CDN, so the browser needs internet even
    // though the panel itself does not. A dead image must not leave a broken
    // icon sitting in the card.
    img.referrerPolicy = "no-referrer";
    img.addEventListener("error", function () {
      var fallback = document.createElement("div");
      fallback.className = "mod-thumb mod-thumb-empty";
      fallback.textContent = "no image";
      img.replaceWith(fallback);
    });
    return img;
  }

  function clearSearch() {
    searchQuery = "";
    searchPage = 1;
    el.search.value = "";
    el.results.innerHTML = "";
    el.pager.hidden = true;
    if (el.clear) el.clear.hidden = true;
    el.search.focus();
  }

  function runSearch(page) {
    searchPage = page;
    el.results.textContent = "Searching...";
    el.pager.hidden = true;
    if (el.clear) el.clear.hidden = false;

    fetch("/mods/search?q=" + encodeURIComponent(searchQuery) + "&page=" + page, {
      credentials: "same-origin",
      headers: { Accept: "application/json" },
    })
      .then(readJson)
      .then(function (res) {
        if (res.handled) return;
        if (res.ok) renderResults(res);
        else el.results.textContent = res.error || "Search failed.";
      });
  }

  // --- wiring ----------------------------------------------------------------

  document.addEventListener("click", function (event) {
    var target = event.target.closest("[data-mod-action]");
    if (!target || target.tagName === "SELECT" || target.type === "checkbox") return;

    var action = target.dataset.modAction;
    var row = target.closest("[data-mod-id]");
    var id = row ? row.dataset.modId : null;
    event.preventDefault();

    if (action === "install") {
      target.disabled = true;
      note(el.installNote, "");
      // No type: a mod arrives as a client mod, and the row it lands in is
      // where one says otherwise.
      send("/mods/install", { query: el.query.value })
        .then(function (res) {
          target.disabled = false;
          if (res.ok === false && res.error) note(el.installNote, res.error, true);
          else afterChange(res);
        });
      return;
    }

    if (action === "upload") {
      uploadMod(target);
      return;
    }

    if (action === "import") {
      var file = el.modlist && el.modlist.files[0];
      if (!file) {
        modlistNote("Choose a mod list first.", true);
        return;
      }
      var body = new FormData();
      body.append("modlist", file);
      target.disabled = true;
      modlistNote("");
      // No Content-Type of our own: the browser has to set the multipart
      // boundary, and one written by hand has none.
      fetch("/mods/import", {
        method: "POST",
        credentials: "same-origin",
        headers: { "X-CSRFToken": CSRF, Accept: "application/json" },
        body: body,
      }).then(readJson).then(function (res) {
        target.disabled = false;
        if (res.handled) return;
        if (!res.ok) {
          // Next to the file field, not in the job box further down: what went
          // wrong is about the file that was just picked.
          modlistNote(res.error || "The mod list could not be imported.", true);
          return;
        }
        el.modlist.value = "";
        modlistNote(res.message || "");
        afterChange(res);
      });
      return;
    }

    if (action === "search") {
      searchQuery = el.search.value.trim();
      if (!searchQuery) return clearSearch();
      runSearch(1);   // a new search always starts at the first page
      return;
    }

    if (action === "clear") { clearSearch(); return; }

    if (action === "prev" || action === "next") {
      runSearch(action === "prev" ? searchPage - 1 : searchPage + 1);
      el.search.scrollIntoView({ block: "nearest" });
      return;
    }

    if (action === "update-all") {
      send("/mods/update-all").then(afterChange);
      return;
    }

    if (action === "sync-keys") {
      // It empties the directory before it fills it, so it asks first.
      if (!window.confirm(
        "Rebuild server/keys? Every key but the DayZ one is removed and copied "
        + "back from the mods that are switched on.")) return;
      send("/mods/keys/sync").then(afterChange);
      return;
    }

    if (action === "cancel" && jobId) {
      post("/jobs/" + jobId + "/cancel").then(poll);
      return;
    }

    if (!id) return;

    if (action === "update" || action === "reinstall") {
      send("/mods/" + id + "/" + action).then(afterChange);
    } else if (action === "delete") {
      var name = row.querySelector("strong").textContent;
      if (!window.confirm("Delete " + name + " from the server directory?")) return;
      send("/mods/" + id + "/delete").then(afterChange);
    }
  });

  // --- load order by drag ----------------------------------------------------
  //
  // The rows move in the page as one drags, and the order that comes out of it
  // is posted whole. Sending "one up" per step would need the same arithmetic
  // on both sides, and the two would disagree the first time a drop landed
  // three rows away.

  var rows = document.getElementById("mod-rows");
  var dragging = null;
  var orderBefore = "";

  function order() {
    return Array.from(rows.querySelectorAll("tr[data-mod-id]"))
      .map(function (row) { return row.dataset.modId; });
  }

  function saveOrder() {
    var now = order();
    if (now.join() === orderBefore) return;      // picked up and put back
    post("/mods/order", { ids: now }).then(function (res) {
      if (res.handled) return;
      // A refused order means the page is out of date, and reloading is the
      // only honest way back - the list on screen is not what is stored.
      if (res.ok === false) {
        showError(res.error || "The order could not be saved.");
        window.setTimeout(function () { window.location.reload(); }, 1500);
      }
    });
  }

  function moveBy(row, offset) {
    var all = Array.from(rows.querySelectorAll("tr[data-mod-id]"));
    var index = all.indexOf(row);
    var target = index + offset;
    if (target < 0 || target >= all.length) return;
    orderBefore = order().join();
    if (offset < 0) rows.insertBefore(row, all[target]);
    else rows.insertBefore(row, all[target].nextSibling);
    saveOrder();
  }

  if (rows) {
    rows.addEventListener("dragstart", function (event) {
      var handle = event.target.closest("[data-mod-handle]");
      if (!handle) {
        event.preventDefault();          // nothing else in a row is draggable
        return;
      }
      dragging = handle.closest("tr[data-mod-id]");
      orderBefore = order().join();
      dragging.classList.add("mod-dragging");
      event.dataTransfer.effectAllowed = "move";
      // Firefox starts no drag at all without data on the transfer.
      event.dataTransfer.setData("text/plain", dragging.dataset.modId);
    });

    rows.addEventListener("dragover", function (event) {
      if (!dragging) return;
      event.preventDefault();
      event.dataTransfer.dropEffect = "move";
      var over = event.target.closest("tr[data-mod-id]");
      if (!over || over === dragging) return;
      // Past the middle of the row under the pointer means below it - so the
      // last place in the list is reachable, not only the gaps between rows.
      var box = over.getBoundingClientRect();
      var below = event.clientY > box.top + box.height / 2;
      rows.insertBefore(dragging, below ? over.nextSibling : over);
    });

    rows.addEventListener("drop", function (event) {
      event.preventDefault();
    });

    rows.addEventListener("dragend", function () {
      if (!dragging) return;
      dragging.classList.remove("mod-dragging");
      dragging = null;
      saveOrder();
    });

    // Without this the order is mouse-only, and a list of twenty mods is
    // exactly where a keyboard is faster.
    rows.addEventListener("keydown", function (event) {
      var handle = event.target.closest("[data-mod-handle]");
      if (!handle) return;
      if (event.key !== "ArrowUp" && event.key !== "ArrowDown") return;
      event.preventDefault();
      moveBy(handle.closest("tr[data-mod-id]"), event.key === "ArrowUp" ? -1 : 1);
      handle.focus();
    });
  }

  document.addEventListener("change", function (event) {
    var target = event.target.closest("[data-mod-action]");
    if (!target) return;

    var row = target.closest("[data-mod-id]");
    if (!row) return;
    var id = row.dataset.modId;

    if (target.dataset.modAction === "type") {
      post("/mods/" + id + "/type", { mod_type: target.value }).then(afterChange);
    } else if (target.dataset.modAction === "enabled") {
      // Reloads like every other change: switching a mod on moves its keys,
      // and the key count in the row would otherwise still show the old one.
      post("/mods/" + id + "/enabled", { enabled: target.checked }).then(afterChange);
    }
  });

  if (el.query) {
    el.query.addEventListener("keydown", function (event) {
      if (event.key === "Enter") {
        event.preventDefault();
        document.querySelector('[data-mod-action="install"]').click();
      }
    });
  }

  if (el.search) {
    el.search.addEventListener("keydown", function (event) {
      if (event.key === "Enter") {
        event.preventDefault();
        document.querySelector('[data-mod-action="search"]').click();
      }
      // Escape empties a search field everywhere else, so it does here too.
      if (event.key === "Escape") {
        event.preventDefault();
        clearSearch();
      }
    });
  }

  poll();
})();
