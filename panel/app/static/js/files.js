// Files: a browser over the server directory, with an editor for text files.
//
// The listing always comes from the server, including after a write. A page
// that patched its own rows would show a size and a timestamp it invented,
// and those are exactly the two things one checks after saving.

(function () {
  "use strict";

  var script = document.currentScript;
  var cfg = {
    listUrl: script.dataset.listUrl,
    readUrl: script.dataset.readUrl,
    saveUrl: script.dataset.saveUrl,
    uploadUrl: script.dataset.uploadUrl,
    mkdirUrl: script.dataset.mkdirUrl,
    deleteUrl: script.dataset.deleteUrl,
    downloadUrl: script.dataset.downloadUrl,
    renameUrl: script.dataset.renameUrl,
    moveUrl: script.dataset.moveUrl,
    bulkDeleteUrl: script.dataset.bulkDeleteUrl,
    compressUrl: script.dataset.compressUrl,
    extractUrl: script.dataset.extractUrl,
    createUrl: script.dataset.createUrl,
    maxUploadMb: parseInt(script.dataset.maxUploadMb, 10) || 0,
    csrf: script.dataset.csrf,
  };

  var el = {
    crumbs: document.getElementById("file-crumbs"),
    rows: document.getElementById("file-rows"),
    count: document.getElementById("file-count"),
    selectAll: document.getElementById("file-select-all"),
    selection: document.getElementById("file-selection"),
    browser: document.getElementById("file-browser"),
    error: document.getElementById("file-error"),
    note: document.getElementById("file-note"),
    input: document.getElementById("file-input"),
    editor: document.getElementById("file-editor"),
    editorPath: document.getElementById("editor-path"),
    editorState: document.getElementById("editor-state"),
    editorText: document.getElementById("editor-text"),
    editorDownload: document.getElementById("editor-download"),
    dialog: document.getElementById("file-dialog"),
    dialogForm: document.getElementById("file-dialog-form"),
    dialogTitle: document.getElementById("file-dialog-title"),
    dialogLabel: document.getElementById("file-dialog-label"),
    dialogHelp: document.getElementById("file-dialog-help"),
    dialogInput: document.getElementById("file-dialog-input"),
    dialogOk: document.getElementById("file-dialog-ok"),
  };

  if (!el.rows) return;

  // --- dialog --------------------------------------------------------------

  var modal = new bootstrap.Modal(el.dialog);
  var pending = null;   // resolve() of the question currently on screen

  // Resolves with the typed value, or null when the visitor backs out. One
  // place for every question the page asks, so none of them can be silently
  // suppressed the way a native prompt can.
  function ask(spec) {
    return new Promise(function (resolve) {
      pending = resolve;
      el.dialogTitle.textContent = spec.title;
      el.dialogLabel.textContent = spec.label || "";
      el.dialogHelp.textContent = spec.help || "";
      el.dialogInput.hidden = spec.input === false;
      el.dialogLabel.hidden = spec.input === false;
      el.dialogInput.value = spec.value || "";
      el.dialogInput.placeholder = spec.placeholder || "";
      el.dialogOk.textContent = spec.okLabel || "OK";
      el.dialogOk.className = "btn " + (spec.danger ? "btn-danger" : "btn-primary");
      modal.show();
    });
  }

  function settle(value) {
    var resolve = pending;
    pending = null;
    if (resolve) resolve(value);
  }

  el.dialogForm.addEventListener("submit", function (event) {
    event.preventDefault();
    // Read before hiding: the hide handler settles with null, and it would
    // otherwise win the race against this one.
    var value = el.dialogInput.hidden ? "" : el.dialogInput.value.trim();
    settle(value);
    modal.hide();
  });

  el.dialog.addEventListener("hidden.bs.modal", function () { settle(null); });
  el.dialog.addEventListener("shown.bs.modal", function () {
    if (!el.dialogInput.hidden) el.dialogInput.select();
  });

  var here = "";
  var parent = null;    // path of the parent directory, null at the root
  var open = null;      // the file in the editor, or null
  var dirty = false;

  // --- transport -----------------------------------------------------------

  function post(url, body) {
    return send(url, { "Content-Type": "application/json" }, JSON.stringify(body || {}));
  }

  function send(url, headers, body) {
    headers["X-CSRFToken"] = cfg.csrf;
    headers.Accept = "application/json";
    return fetch(url, { method: "POST", credentials: "same-origin", headers: headers, body: body })
      .then(read, failed);
  }

  function get(url) {
    return fetch(url, { headers: { Accept: "application/json" }, credentials: "same-origin" })
      .then(read, failed);
  }

  // fetch only rejects when the request never completed - a dropped connection,
  // a server that closed mid-body. Swallowing that leaves a button that looks
  // broken, so it becomes a message like any other failure.
  function failed(error) {
    return { ok: false, error: "The panel could not be reached: " + error.message };
  }

  function read(r) {
    if (r.status === 401) {
      window.location.reload();
      return { ok: false, handled: true };
    }
    return r.json().catch(function () {
      window.location.reload();
      return { ok: false, handled: true };
    });
  }

  function setError(message) {
    el.error.hidden = !message;
    el.error.textContent = message || "";
  }

  function setNote(message) {
    el.note.hidden = !message;
    el.note.textContent = message || "";
  }

  function handle(res, onOk) {
    if (res.handled) return false;
    setError(res.ok ? "" : (res.error || "That did not work."));
    if (res.ok && onOk) onOk(res);
    return res.ok;
  }

  // --- browsing ------------------------------------------------------------

  function go(path) {
    get(cfg.listUrl + "?path=" + encodeURIComponent(path || "")).then(function (res) {
      handle(res, render);
    });
  }

  function render(listing) {
    here = listing.path || "";
    parent = listing.parent;
    el.rows.textContent = "";
    el.count.textContent = listing.entries.length + " entries in /" + here;
    if (el.selectAll) el.selectAll.checked = false;

    el.crumbs.textContent = "";
    listing.crumbs.forEach(function (crumb, index) {
      var item = document.createElement("li");
      item.className = "breadcrumb-item";
      if (index === listing.crumbs.length - 1) {
        item.classList.add("active");
        item.textContent = crumb.name;
      } else {
        var link = document.createElement("a");
        link.href = "#";
        link.textContent = crumb.name;
        link.dataset.fileAction = "open-dir";
        link.dataset.filePath = crumb.path;
        item.appendChild(link);
      }
      el.crumbs.appendChild(item);
    });

    // The way up is a row, not a button: it is where the eye already is, and
    // it puts "leave this directory" in the same list as "enter that one".
    if (parent !== null && parent !== undefined) el.rows.appendChild(upRow());

    if (!listing.entries.length) {
      var empty = document.createElement("tr");
      var cell = document.createElement("td");
      cell.colSpan = 5;
      cell.className = "text-secondary";
      cell.textContent = "This directory is empty.";
      empty.appendChild(cell);
      el.rows.appendChild(empty);
      updateSelection();
      return;
    }

    listing.entries.forEach(function (entry) {
      el.rows.appendChild(row(entry));
    });
    updateSelection();
  }

  function upRow() {
    var tr = document.createElement("tr");
    tr.className = "file-up";

    tr.appendChild(document.createElement("td"));

    var name = document.createElement("td");
    var link = document.createElement("a");
    link.href = "#";
    link.className = "text-secondary";
    link.textContent = "↰ ..";
    link.dataset.fileAction = "open-dir";
    link.dataset.filePath = parent;
    name.appendChild(link);
    tr.appendChild(name);

    ["", "", ""].forEach(function () {
      tr.appendChild(document.createElement("td"));
    });
    return tr;
  }

  function row(entry) {
    var tr = document.createElement("tr");

    var pick = document.createElement("td");
    pick.className = "file-select";
    var box = document.createElement("input");
    box.className = "form-check-input mt-0";
    box.type = "checkbox";
    box.dataset.filePath = entry.path;
    box.dataset.fileDir = entry.is_dir ? "1" : "";
    pick.appendChild(box);
    tr.appendChild(pick);

    var name = document.createElement("td");
    var link = document.createElement("a");
    link.href = "#";
    link.textContent = (entry.is_dir ? "\u{1F4C1} " : "\u{1F4C4} ") + entry.name;
    link.dataset.fileAction = entry.is_dir ? "open-dir" : "open-file";
    link.dataset.filePath = entry.path;
    name.appendChild(link);
    tr.appendChild(name);

    var size = document.createElement("td");
    size.className = "text-end small text-secondary";
    size.textContent = entry.is_dir ? "—" : bytes(entry.size);
    tr.appendChild(size);

    var when = document.createElement("td");
    when.className = "text-end small text-secondary text-nowrap";
    when.textContent = entry.modified_text;
    tr.appendChild(when);

    tr.appendChild(menu(entry));
    return tr;
  }

  // One menu per row rather than a row of buttons: with five entries the
  // buttons would be wider than the names they belong to.
  function menu(entry) {
    var cell = document.createElement("td");
    cell.className = "text-end file-menu-col";

    var wrap = document.createElement("div");
    wrap.className = "dropdown";

    var toggle = document.createElement("button");
    toggle.className = "btn btn-sm btn-link text-secondary py-0 px-2";
    toggle.type = "button";
    toggle.textContent = "⋯";
    toggle.setAttribute("data-bs-toggle", "dropdown");
    toggle.setAttribute("aria-expanded", "false");
    toggle.title = "Actions";
    wrap.appendChild(toggle);

    var list = document.createElement("ul");
    list.className = "dropdown-menu dropdown-menu-end";

    var items = [{ action: "rename", label: "Rename" }, { action: "move", label: "Move" }];
    if (!entry.is_dir) {
      items.push({ action: "open-file", label: "Edit" });
      items.push({ action: "download", label: "Download" });
    }
    items.push({ action: "zip", label: "Zip" });
    // Offered on the suffix alone. Whether it really is an archive is decided
    // by reading it, which is the server's job and not worth a round trip
    // before the menu can be drawn.
    if (!entry.is_dir && /\.zip$/i.test(entry.name)) {
      items.push({ action: "extract", label: "Extract here" });
    }
    items.push({ action: "delete", label: "Delete", css: "text-danger" });

    items.forEach(function (spec) {
      var li = document.createElement("li");
      var node;
      if (spec.action === "download") {
        node = document.createElement("a");
        node.href = cfg.downloadUrl + "?path=" + encodeURIComponent(entry.path);
        node.setAttribute("download", "");
      } else {
        node = document.createElement("button");
        node.type = "button";
        node.dataset.fileAction = spec.action;
        node.dataset.filePath = entry.path;
        // Carried so the delete dialog can say what a folder takes with it.
        if (entry.is_dir) node.dataset.fileDir = "1";
      }
      node.className = "dropdown-item " + (spec.css || "");
      node.textContent = spec.label;
      li.appendChild(node);
      list.appendChild(li);
    });

    wrap.appendChild(list);
    cell.appendChild(wrap);
    return cell;
  }

  // --- selection -----------------------------------------------------------

  function boxes() {
    return Array.prototype.slice.call(el.rows.querySelectorAll("input[type=checkbox]"));
  }

  function selected() {
    return boxes().filter(function (box) { return box.checked; })
      .map(function (box) { return box.dataset.filePath; });
  }

  function updateSelection() {
    var count = selected().length;
    el.selection.hidden = !count;
    el.selection.textContent = count + " selected";
    document.querySelectorAll("[data-file-action^='bulk-']").forEach(function (btn) {
      btn.hidden = !count;
    });
  }

  function bytes(value) {
    if (value < 1024) return value + " B";
    if (value < 1024 * 1024) return (value / 1024).toFixed(1) + " KB";
    return (value / 1024 / 1024).toFixed(1) + " MB";
  }

  // --- editor --------------------------------------------------------------

  function openFile(path) {
    get(cfg.readUrl + "?path=" + encodeURIComponent(path)).then(function (res) {
      if (!handle(res)) return;
      open = res.path;
      el.editorPath.textContent = res.path;
      el.editorText.value = res.text;
      el.editorDownload.href = cfg.downloadUrl + "?path=" + encodeURIComponent(res.path);
      el.editor.hidden = false;
      el.browser.hidden = true;
      setDirty(false);
      setNote("");
      el.editorText.focus();
    });
  }

  function setDirty(value) {
    dirty = value;
    el.editorState.textContent = value ? "unsaved changes" : "saved";
    el.editorState.className = "small " + (value ? "text-warning" : "text-secondary");
  }

  function closeEditor() {
    // Asking is worth the friction here: the textarea is the only place in the
    // panel where minutes of work live in the browser alone.
    if (!dirty) return leaveEditor();
    ask({
      title: "Unsaved changes",
      input: false,
      help: open + " has changes that were never saved. Close it and throw them "
            + "away? There is no backup copy to go back to.",
      okLabel: "Discard changes",
      danger: true,
    }).then(function (answer) {
      if (answer !== null) leaveEditor();
    });
  }

  function leaveEditor() {
    open = null;
    el.editor.hidden = true;
    el.browser.hidden = false;
    setDirty(false);
    go(here);
  }

  function save(andClose) {
    if (!open) return;
    var saved = open;
    post(cfg.saveUrl, { path: open, text: el.editorText.value }).then(function (res) {
      handle(res, function () {
        setDirty(false);
        setNote("Saved " + saved + ".");
        // Only after the answer: closing on the click would claim the save
        // worked before anyone knew whether it did.
        if (andClose) leaveEditor();
      });
    });
  }

  // --- actions -------------------------------------------------------------

  function upload(files) {
    if (!files.length) return;

    // Checked here rather than left to the server: a body over the limit is
    // rejected mid-transfer, and the connection dies before the answer can be
    // read. The upload would look like it did nothing at all.
    var total = 0;
    Array.prototype.forEach.call(files, function (file) { total += file.size; });
    if (cfg.maxUploadMb && total > cfg.maxUploadMb * 1024 * 1024) {
      setNote("");
      setError(
        "That is " + bytes(total) + " in one request - the limit is "
        + cfg.maxUploadMb + " MB. Upload fewer files at a time, or raise "
        + "MAX_UPLOAD_MB in .env and recreate the container."
      );
      return;
    }

    var form = new FormData();
    form.append("path", here);
    Array.prototype.forEach.call(files, function (file) { form.append("files", file); });

    setNote("Uploading ...");
    send(cfg.uploadUrl, {}, form).then(function (res) {
      if (res.handled) return;
      if (res.listing) render(res.listing);
      // Partial success is a real outcome: one bad name among five uploads
      // must not read as either "all done" or "nothing happened".
      setError(res.ok ? (res.error || "") : (res.error || "The upload failed."));
      setNote(res.ok ? "Uploaded " + res.written.length + " file(s)." : "");
    });
  }

  document.addEventListener("click", function (event) {
    var node = event.target.closest("[data-file-action]");
    if (!node) return;
    var action = node.dataset.fileAction;
    var path = node.dataset.filePath;

    if (action === "open-dir") { event.preventDefault(); go(path); return; }
    if (action === "open-file") { event.preventDefault(); openFile(path); return; }
    if (action === "pick") { event.preventDefault(); el.input.click(); return; }
    if (action === "save") { event.preventDefault(); save(false); return; }
    if (action === "save-close") { event.preventDefault(); save(true); return; }
    if (action === "close") { event.preventDefault(); closeEditor(); return; }

    event.preventDefault();

    if (action === "mkdir") {
      ask({
        title: "New folder",
        label: "Name",
        placeholder: "storage",
        help: "Created in /" + (here || ""),
        okLabel: "Create",
      }).then(function (name) {
        if (!name) return;
        post(cfg.mkdirUrl, { path: here, name: name }).then(function (res) {
          handle(res, function () { render(res.listing); setNote("Created " + res.created); });
        });
      });
      return;
    }

    if (action === "create") {
      ask({
        title: "New file",
        label: "Name",
        placeholder: "types.xml",
        help: "Created empty in /" + (here || "") + " and opened in the editor.",
        okLabel: "Create",
      }).then(function (name) {
        if (!name) return;
        post(cfg.createUrl, { path: here, name: name }).then(function (res) {
          handle(res, function () {
            render(res.listing);
            setNote("Created " + res.created);
            // Straight into the editor: an empty file is never the goal, it is
            // the step before writing something into it.
            openFile(res.created);
          });
        });
      });
      return;
    }

    if (action === "delete") {
      ask({
        title: "Delete",
        input: false,
        // No copy is kept either way - the Backups page is what a deleted file
        // comes back from.
        help: node.dataset.fileDir
          ? "Delete \"" + path + "\" and everything in it?"
          : "Delete \"" + path + "\"?",
        okLabel: "Delete",
        danger: true,
      }).then(function (answer) {
        if (answer === null) return;
        post(cfg.deleteUrl, { path: path }).then(function (res) {
          handle(res, function () {
            render(res.listing);
            setNote("Deleted " + res.deleted);
          });
        });
      });
      return;
    }

    if (action === "rename") {
      var current = path.indexOf("/") === -1 ? path : path.substring(path.lastIndexOf("/") + 1);
      ask({ title: "Rename", label: "New name", value: current, okLabel: "Rename" })
        .then(function (name) {
          if (!name || name === current) return;
          post(cfg.renameUrl, { path: path, name: name }).then(function (res) {
            handle(res, function () { render(res.listing); setNote("Renamed to " + res.path); });
          });
        });
      return;
    }

    if (action === "move") { moveTo([path]); return; }
    if (action === "bulk-move") { moveTo(selected()); return; }
    if (action === "zip") { zip([path]); return; }
    if (action === "bulk-zip") { zip(selected()); return; }

    if (action === "extract") {
      ask({
        title: "Extract here",
        input: false,
        help: "Unpack " + path.split("/").pop() + " into /" + (here || "")
              + "? Files it replaces are backed up first.",
        okLabel: "Extract",
      }).then(function (answer) {
        if (answer === null) return;
        post(cfg.extractUrl, { path: path }).then(function (res) {
          if (res.handled) return;
          if (res.listing) render(res.listing);
          setError(res.error || "");
          if (!res.ok) return;
          setNote("Unpacked " + res.files + " file" + (res.files === 1 ? "" : "s")
                  + (res.replaced ? ", " + res.replaced + " replaced" : "") + ".");
        });
      });
      return;
    }

    if (action === "bulk-delete") {
      var picked = selected();
      if (!picked.length) return;
      ask({
        title: "Delete selected",
        input: false,
        help: "Delete " + picked.length + " selected entr"
              + (picked.length === 1 ? "y" : "ies")
              + "? A folder goes with everything in it.",
        okLabel: "Delete",
        danger: true,
      }).then(function (answer) {
        if (answer === null) return;
        post(cfg.bulkDeleteUrl, { paths: picked }).then(function (res) {
          if (res.handled) return;
          if (res.listing) render(res.listing);
          setError(res.error || "");
          setNote(res.ok ? "Deleted " + res.done.length + " entr"
                  + (res.done.length === 1 ? "y" : "ies") + "." : "");
        });
      });
    }
  });

  // One entry gives the archive its name; several fall back to the folder they
  // are in, because a zip called after the first of eight files is a lie.
  function zip(paths) {
    if (!paths.length) return;
    var base = paths.length === 1
      ? paths[0].split("/").pop()
      : (here.split("/").pop() || "server");

    ask({
      title: "Zip " + paths.length + " entr" + (paths.length === 1 ? "y" : "ies"),
      label: "Archive name",
      value: base + ".zip",
      help: "Written into /" + (here || "") + ".",
      okLabel: "Zip",
    }).then(function (name) {
      if (!name) return;
      post(cfg.compressUrl, { paths: paths, target: here, name: name })
        .then(function (res) {
          if (res.handled) return;
          if (res.listing) render(res.listing);
          setError(res.error || "");
          if (!res.ok) return;
          setNote("Packed " + res.files + " file" + (res.files === 1 ? "" : "s")
                  + " into " + res.created + " (" + bytes(res.size) + ").");
        });
    });
  }

  function moveTo(paths) {
    if (!paths.length) return;
    ask({
      title: "Move " + paths.length + " entr" + (paths.length === 1 ? "y" : "ies"),
      label: "Target directory",
      // A path from the root, because that is what the breadcrumb shows.
      value: here,
      placeholder: "mpmissions/dayzOffline.chernarusplus",
      help: "Path from the server directory. Leave empty for the root.",
      okLabel: "Move",
    }).then(function (target) {
      if (target === null) return;
      post(cfg.moveUrl, { paths: paths, target: target }).then(function (res) {
        if (res.handled) return;
        if (res.listing) render(res.listing);
        setError(res.error || "");
        setNote(res.ok ? "Moved " + res.done.length + " entr"
                + (res.done.length === 1 ? "y" : "ies") + " to /" + target : "");
      });
    });
  }

  el.rows.addEventListener("change", function (event) {
    if (event.target.type === "checkbox") updateSelection();
  });

  el.selectAll.addEventListener("change", function () {
    boxes().forEach(function (box) { box.checked = el.selectAll.checked; });
    updateSelection();
  });

  el.input.addEventListener("change", function () {
    upload(el.input.files);
    el.input.value = "";
  });

  el.editorText.addEventListener("input", function () {
    if (!dirty) setDirty(true);
  });

  el.editorText.addEventListener("keydown", function (event) {
    // Ctrl+S is what one reaches for in a text field, and the browser's own
    // "save page" dialog is never what was meant here.
    if ((event.ctrlKey || event.metaKey) && event.key === "s") {
      event.preventDefault();
      save(false);
    }
  });

  window.addEventListener("beforeunload", function (event) {
    if (!dirty) return;
    event.preventDefault();
    event.returnValue = "";
  });

  render(JSON.parse(document.getElementById("file-data").textContent || "{}"));
})();
