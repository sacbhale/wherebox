// Small progressive enhancements. Pages work without JS except the htmx quick actions and
// capture (the photo tray and the capture POSTs need JS).
(function () {
  "use strict";

  function selectText(input) {
    input.focus();
    input.select();
    input.setSelectionRange(0, input.value.length);
  }

  function flash(button, text) {
    var original = button.textContent;
    button.textContent = text;
    setTimeout(function () { button.textContent = original; }, 1500);
  }

  function copy(button) {
    var input = document.querySelector(button.getAttribute("data-copy"));
    if (!input) return;
    var value = input.value;
    if (navigator.clipboard && window.isSecureContext) {
      navigator.clipboard.writeText(value).then(
        function () { flash(button, "Copied"); },
        function () { fallback(button, input); }
      );
    } else {
      fallback(button, input);
    }
  }

  // Plain-HTTP LAN access has no clipboard API: select the text and try execCommand.
  function fallback(button, input) {
    selectText(input);
    var ok = false;
    try { ok = document.execCommand("copy"); } catch (e) { ok = false; }
    flash(button, ok ? "Copied" : "Selected, copy it");
  }

  document.addEventListener("click", function (event) {
    var target = event.target.closest("[data-copy], [data-print]");
    if (!target) return;
    if (target.hasAttribute("data-copy")) copy(target);
    if (target.hasAttribute("data-print")) window.print();
  });

  // The ⋯ action sheet is loaded by htmx into #sheet; emptying #sheet closes it.
  function closeSheet() {
    var sheet = document.getElementById("sheet");
    if (sheet) sheet.innerHTML = "";
  }

  document.addEventListener("click", function (event) {
    if (event.target.closest("[data-close-sheet]")) closeSheet();
  });

  document.addEventListener("keydown", function (event) {
    if (event.key === "Escape") closeSheet();
  });

  document.addEventListener("htmx:afterRequest", function (event) {
    var elt = event.detail.elt;
    if (!event.detail.successful || !elt || !elt.closest) return;
    // One tap on a sheet action (Used up, a Move-to destination) also closes the sheet.
    if (elt.closest("[data-sheet-action]")) closeSheet();
    // A scan-to-move toast goes away once its Undo went through.
    if (elt.closest("[data-toast-undo]")) {
      var toast = elt.closest(".toast");
      if (toast) toast.remove();
    }
  });

  // Location tree: remember which nodes are open, per viewer. Storage may be unavailable
  // (private mode, blocked site data); the tree then just starts collapsed.
  var TREE_KEY = "inventory.openLocations";

  function openLocations() {
    try {
      var ids = JSON.parse(localStorage.getItem(TREE_KEY) || "[]");
      return Array.isArray(ids) ? ids.map(String) : [];
    } catch (e) {
      return [];
    }
  }

  function saveOpenLocations(ids) {
    try { localStorage.setItem(TREE_KEY, JSON.stringify(ids)); } catch (e) { /* no storage */ }
  }

  // Reopen remembered nodes, and drop remembered ids that are no longer in the tree (deleted
  // locations), in a single write.
  function restoreTree() {
    var tree = document.getElementById("location-tree");
    if (!tree) return;
    var open = openLocations();
    if (!open.length) return;
    var kept = [];
    tree.querySelectorAll("details.tree-branch[data-location-id]").forEach(function (node) {
      var id = node.getAttribute("data-location-id");
      if (open.indexOf(id) === -1) return;
      node.open = true;
      kept.push(id);
    });
    if (kept.length !== open.length) saveOpenLocations(kept);
  }

  // "toggle" does not bubble; listen in the capture phase. Restoring fires toggles too; those
  // match what is stored, so they write nothing.
  document.addEventListener("toggle", function (event) {
    var node = event.target;
    if (!node.matches || !node.matches("details.tree-branch[data-location-id]")) return;
    var id = node.getAttribute("data-location-id");
    var open = openLocations();
    var stored = open.indexOf(id) !== -1;
    if (node.open === stored) return;
    saveOpenLocations(node.open ? open.concat(id) : open.filter(function (o) { return o !== id; }));
  }, true);

  restoreTree();

  // Forms marked data-show-errors also swap 4xx answers (the server's message partial);
  // htmx 2 drops them by default. Only the marked element's own requests qualify (not
  // those of elements inside it), and only HTML answers.
  document.addEventListener("htmx:beforeSwap", function (event) {
    // detail.elt is the swap target here; the requesting element is requestConfig.elt.
    var elt = event.detail.requestConfig && event.detail.requestConfig.elt;
    var xhr = event.detail.xhr;
    var status = xhr ? xhr.status : 0;
    var type = xhr ? xhr.getResponseHeader("Content-Type") || "" : "";
    if (status >= 400 && status < 500 && type.indexOf("text/html") === 0 &&
        elt && elt.hasAttribute && elt.hasAttribute("data-show-errors")) {
      event.detail.shouldSwap = true;
      event.detail.isError = false;
      // data-error-target: show the message there (next to the form that posted), replacing
      // the previous one, and scroll it into view.
      var slot = elt.getAttribute("data-error-target");
      var target = slot && document.querySelector(slot);
      if (target) {
        event.detail.target = target;
        event.detail.swapOverride = "innerHTML";
      }
    }
  });

  document.addEventListener("htmx:afterSwap", function (event) {
    var target = event.detail.target;
    if (target && target.hasAttribute && target.hasAttribute("data-form-message")) {
      target.scrollIntoView({ block: "nearest" });
    }
  });

  // Comparison page: the pickers (#compare-controls) follow the start button, which the run
  // status locks while a comparison runs and unlocks, out of band, once it has ended.
  document.addEventListener("htmx:afterSettle", function () {
    var button = document.getElementById("compare-start-button");
    var controls = document.getElementById("compare-controls");
    if (button && controls) controls.disabled = button.hasAttribute("data-locked");
  });

  // The run status is replaced on every poll; keep the per-model "Details" the user opened.
  var openRunDetails = [];
  document.addEventListener("htmx:beforeSwap", function (event) {
    var target = event.detail.target;
    if (!target || !target.matches || !target.matches(".compare-status")) return;
    openRunDetails = [];
    target.querySelectorAll(".model-progress-row").forEach(function (row) {
      var details = row.querySelector("details.model-progress-detail");
      if (details && details.open) openRunDetails.push(row.getAttribute("data-model-id"));
    });
  });
  document.addEventListener("htmx:afterSettle", function () {
    if (!openRunDetails.length) return;
    document.querySelectorAll(".compare-status .model-progress-row").forEach(function (row) {
      if (openRunDetails.indexOf(row.getAttribute("data-model-id")) === -1) return;
      var details = row.querySelector("details.model-progress-detail");
      if (details) details.open = true;
    });
  });

  // ---------------------------------------------------------------- photo tray
  // Forms marked data-tray (partials/photo_tray.html) collect photos from their picker inputs
  // (no name, so the browser never posts them) into form._trayFiles, show thumbnails and send
  // them as "photos" with the htmx request. Send is disabled while the tray is empty or a
  // request is under way; the tray empties after a successful send.

  function trayFiles(form) {
    if (!form._trayFiles) form._trayFiles = [];
    return form._trayFiles;
  }

  // The form's limit element ([data-tray-max]; data-tray-room when the limit is the capture
  // group's remaining room rather than the per-send limit).
  function trayHolder(form) {
    var holder = form.querySelector("[data-tray-max]");
    var id = holder && holder.id;
    // The element may have been replaced out of band (by id), so look it up again.
    if (id && document.getElementById(id)) holder = document.getElementById(id);
    return holder;
  }

  function trayMax(form) {
    var holder = trayHolder(form);
    var max = holder ? parseInt(holder.getAttribute("data-tray-max"), 10) : 6;
    return isNaN(max) ? 6 : max;
  }

  function trayRoom(form) {
    var holder = trayHolder(form);
    return !!holder && holder.hasAttribute("data-tray-room");
  }

  // Why no more photos fit: the group's room or the per-send limit.
  function limitText(form, max) {
    return trayRoom(form) ? "this capture has room for " + max : "at most " + max + " per send";
  }

  function plural(n) { return n + " photo" + (n === 1 ? "" : "s"); }

  function renderTray(form, note) {
    var files = trayFiles(form);
    var max = trayMax(form);
    var list = form.querySelector("[data-tray-thumbs]");
    var count = form.querySelector("[data-tray-count]");
    var send = form.querySelector("[data-tray-send]");
    if (list) {
      list.innerHTML = "";
      files.forEach(function (entry, index) {
        var li = document.createElement("li");
        li.className = "tray-thumb";
        var img = document.createElement("img");
        img.src = entry.url;
        img.alt = "Photo " + (index + 1);
        var remove = document.createElement("button");
        remove.type = "button";
        remove.className = "tray-remove secondary";
        remove.setAttribute("data-tray-remove", String(index));
        remove.setAttribute("aria-label", "Remove photo " + (index + 1));
        remove.textContent = "\u00d7";
        li.appendChild(img);
        li.appendChild(remove);
        list.appendChild(li);
      });
    }
    var full = files.length >= max;
    form.querySelectorAll("[data-tray-input]:not([data-blocked])").forEach(function (input) {
      input.disabled = full;
    });
    if (count) {
      var text;
      var room = trayRoom(form);
      var upTo = room ? "this capture has room for " + max : "up to " + max;
      if (max <= 0) text = "No more photos can be added to this capture.";
      else if (!files.length) text = "No photos yet (" + upTo + ").";
      else if (full) text = plural(files.length) + " ready" + (room ? " (all this capture has room for)." : " (the most per send).");
      else text = plural(files.length) + " ready (" + upTo + ").";
      count.textContent = note ? text + " " + note : text;
    }
    if (send) {
      send.disabled = !files.length || form._traySending === true;
      send.textContent = "Send (" + plural(files.length) + ")";
    }
  }

  function clearTray(form) {
    trayFiles(form).forEach(function (entry) { URL.revokeObjectURL(entry.url); });
    form._trayFiles = [];
    renderTray(form);
  }

  document.addEventListener("change", function (event) {
    var input = event.target;
    if (!input.matches || !input.matches("[data-tray-input]")) return;
    var form = input.closest("form[data-tray]");
    if (!form) return;
    var files = trayFiles(form);
    var max = trayMax(form);
    var refused = 0;
    Array.prototype.forEach.call(input.files || [], function (file) {
      if (files.length >= max) { refused += 1; return; }
      files.push({ file: file, url: URL.createObjectURL(file) });
    });
    // Picking the same file again must fire "change" again; the files live in the array.
    input.value = "";
    renderTray(form, refused ? plural(refused) + " not added: " + limitText(form, max) + "." : "");
  });

  document.addEventListener("click", function (event) {
    var remove = event.target.closest("[data-tray-remove]");
    if (!remove) return;
    var form = remove.closest("form[data-tray]");
    if (!form) return;
    var files = trayFiles(form);
    var index = parseInt(remove.getAttribute("data-tray-remove"), 10);
    if (isNaN(index)) return;
    var entry = files.splice(index, 1)[0];
    if (entry) URL.revokeObjectURL(entry.url);
    renderTray(form);
  });

  document.addEventListener("htmx:configRequest", function (event) {
    var form = event.detail.elt;
    if (!form || !form.matches || !form.matches("form[data-tray]")) return;
    var files = trayFiles(form);
    if (!files.length || form._traySending) {
      event.preventDefault();  // never an empty (or a second) send
      return;
    }
    files.forEach(function (entry) {
      event.detail.formData.append("photos", entry.file, entry.file.name || "photo.jpg");
    });
  });

  document.addEventListener("htmx:beforeRequest", function (event) {
    var form = event.detail.elt;
    if (!form || !form.matches || !form.matches("form[data-tray]")) return;
    form._traySending = true;
    var message = form.querySelector("[data-form-message]");
    if (message) message.innerHTML = "";
    renderTray(form);
  });

  document.addEventListener("htmx:afterRequest", function (event) {
    var form = event.detail.elt;
    if (!form || !form.matches || !form.matches("form[data-tray]")) return;
    form._traySending = false;
    var status = event.detail.xhr ? event.detail.xhr.status : 0;
    // Sent: empty the tray. Refused (4xx message shown in the form): keep the photos, so
    // one can be removed and the rest sent again.
    if (status >= 200 && status < 300) clearTray(form);
    else renderTray(form);
  });

  // A tray form removed by an htmx swap frees its thumbnails' object URLs.
  document.addEventListener("htmx:beforeCleanupElement", function (event) {
    var elt = event.target;
    if (elt && elt.matches && elt.matches("form[data-tray]") && elt._trayFiles) clearTray(elt);
  });

  // A tray limit replaced out of band (another angle was added) re-renders its tray.
  document.addEventListener("htmx:oobAfterSwap", function (event) {
    var elt = event.target;
    var form = elt && elt.closest && elt.closest("form[data-tray]");
    if (form) renderTray(form);
  });

  // ---------------------------------------------------------------- annotated gallery
  // [data-gallery] (partials/gallery.html): list rows ([data-gallery-row], aria-pressed) and
  // boxes (.box) share data-key. Tapping a row highlights its boxes (tap again to clear) and
  // scrolls the first into view; tapping a box highlights its row. [data-gallery-model]
  // buttons (comparison report) show one model's boxes at a time.

  function highlight(gallery, key, toggle) {
    var rows = gallery.querySelectorAll("[data-gallery-row]");
    var current = gallery.querySelector('[data-gallery-row][aria-pressed="true"]');
    // key null: clear the highlight.
    var on = key !== null && !(toggle && current && current.getAttribute("data-key") === key);
    rows.forEach(function (row) {
      row.setAttribute("aria-pressed", String(on && row.getAttribute("data-key") === key));
    });
    var first = null;
    gallery.querySelectorAll(".box").forEach(function (box) {
      var match = on && box.getAttribute("data-key") === key;
      box.classList.toggle("is-active", match);
      if (match && !first) first = box;
    });
    gallery.classList.toggle("has-active", on);
    return on ? first : null;
  }

  document.addEventListener("click", function (event) {
    var gallery = event.target.closest("[data-gallery]");
    if (!gallery) return;
    var row = event.target.closest("[data-gallery-row]");
    if (row) {
      var box = highlight(gallery, row.getAttribute("data-key"), true);
      if (box) box.scrollIntoView({ block: "nearest", inline: "center", behavior: "smooth" });
      return;
    }
    var hit = event.target.closest(".box");
    if (hit && hit.getAttribute("data-key")) {
      highlight(gallery, hit.getAttribute("data-key"), false);
      var match = gallery.querySelector('[data-gallery-row][data-key="' + CSS.escape(hit.getAttribute("data-key")) + '"]');
      if (match) match.scrollIntoView({ block: "nearest", behavior: "smooth" });
      return;
    }
    var model = event.target.closest("[data-gallery-model]");
    if (model) {
      var index = model.getAttribute("data-gallery-model");
      // Another model's boxes: drop the highlight of the previous one.
      highlight(gallery, null, false);
      gallery.querySelectorAll("[data-gallery-model]").forEach(function (button) {
        button.setAttribute("aria-pressed", String(button === model));
      });
      gallery.querySelectorAll(".box[data-model]").forEach(function (box) {
        box.hidden = box.getAttribute("data-model") !== index;
      });
    }
  });

  // A gallery opened with an item highlighted (/captures/{id}?item=...) shows its first box.
  var opened = document.querySelector("[data-gallery][data-scroll-active] .box.is-active");
  if (opened) opened.scrollIntoView({ block: "center", inline: "center" });

  // Comparison case form: add a blank item row from a <template>, or remove a row.
  document.addEventListener("click", function (event) {
    var add = event.target.closest("[data-add-row]");
    if (add) {
      var template = document.getElementById(add.getAttribute("data-add-row"));
      var rows = document.querySelector(add.getAttribute("data-rows"));
      if (!template || !rows) return;
      rows.appendChild(template.content.cloneNode(true));
      var input = rows.lastElementChild && rows.lastElementChild.querySelector("input");
      if (input) input.focus();
      return;
    }
    var remove = event.target.closest("[data-remove-row]");
    if (remove && remove.closest(".case-row")) remove.closest(".case-row").remove();
  });

  // Bulk add: the numbered-series helper appends "Shelf 1" ... "Shelf 10" to the names list
  // (zero-padded when "from" is typed with a leading zero, e.g. 01). Hidden without JS.
  document.querySelectorAll("[data-series]").forEach(function (box) { box.hidden = false; });

  document.addEventListener("click", function (event) {
    var button = event.target.closest("[data-series-fill]");
    if (!button) return;
    var box = button.closest("[data-series]");
    var error = box.querySelector("[data-series-error]");
    var max = parseInt(box.getAttribute("data-max"), 10);
    var stem = box.querySelector("[data-series-stem]").value.trim();
    var fromText = box.querySelector("[data-series-from]").value.trim();
    var toText = box.querySelector("[data-series-to]").value.trim();
    var message = "";
    if (!/^\d+$/.test(fromText) || !/^\d+$/.test(toText)) {
      message = "Enter whole numbers for From and To.";
    } else if (parseInt(toText, 10) < parseInt(fromText, 10)) {
      message = "To must not be less than From.";
    } else if (parseInt(toText, 10) - parseInt(fromText, 10) >= max) {
      message = "At most " + max + " locations at a time.";
    }
    error.textContent = message;
    error.hidden = !message;
    if (message) return;
    var width = fromText.length > 1 && fromText.charAt(0) === "0" ? fromText.length : 0;
    var lines = [];
    for (var n = parseInt(fromText, 10); n <= parseInt(toText, 10); n++) {
      var number = String(n);
      while (number.length < width) number = "0" + number;
      lines.push(stem ? stem + " " + number : number);
    }
    var names = box.closest("form").querySelector("textarea[name=names]");
    var current = names.value.replace(/\s+$/, "");
    names.value = (current ? current + "\n" : "") + lines.join("\n");
  });

  // Enter in the series fields adds to the list instead of submitting the save form.
  document.addEventListener("keydown", function (event) {
    if (event.key !== "Enter" || !event.target.matches("[data-series] input")) return;
    event.preventDefault();
    event.target.closest("[data-series]").querySelector("[data-series-fill]").click();
  });

  // Label sheet: remember the printer offset per device, and reapply it when a sheet is
  // opened without one. Storage may be unavailable; the offset then starts at 0.
  var OFFSET_KEY = "inventory.labelOffset";
  document.addEventListener("click", function (event) {
    if (!event.target.closest("[data-offset-reset]")) return;
    try { localStorage.removeItem(OFFSET_KEY); } catch (e) { /* no storage */ }
    var next = new URLSearchParams(window.location.search);
    next.set("dx", "0");
    next.set("dy", "0");
    next.delete("marked");
    next.delete("unmarked");
    window.location.assign("?" + next.toString());
  });
  if (document.querySelector("[data-label-offset]")) {
    var params = new URLSearchParams(window.location.search);
    try {
      if (params.has("dx") || params.has("dy")) {
        localStorage.setItem(OFFSET_KEY, JSON.stringify({
          dx: params.get("dx") || "0", dy: params.get("dy") || "0"
        }));
      } else {
        var saved = JSON.parse(localStorage.getItem(OFFSET_KEY) || "null") || {};
        var dx = Number(saved.dx) || 0, dy = Number(saved.dy) || 0;
        var valid = [dx, dy].every(function (v) { return Math.abs(v) <= 5 && v * 4 === Math.round(v * 4); });
        if (valid && (dx || dy)) {
          params.set("dx", String(dx));
          params.set("dy", String(dy));
          window.location.replace("?" + params.toString());
        }
      }
    } catch (e) { /* no storage: no remembered offset */ }
  }

  // Label sheet: "All" / "None" / "Not printed only" set the boxes; any change reloads the
  // sheet with the new selection. After the print dialog closes, prompt to mark them printed
  // (never marked automatically).
  function submitPick() {
    var form = document.getElementById("sheet-form");
    if (form) form.submit();
  }
  document.addEventListener("click", function (event) {
    var button = event.target.closest("[data-pick]");
    if (!button) return;
    var mode = button.getAttribute("data-pick");
    document.querySelectorAll('#pick-list input[name="pick"]').forEach(function (box) {
      box.checked = mode === "all" || (mode === "unprinted" && !box.hasAttribute("data-printed"));
    });
    submitPick();
  });
  document.addEventListener("change", function (event) {
    if (event.target.matches('#pick-list input[name="pick"]')) submitPick();
  });
  window.addEventListener("afterprint", function () {
    var check = document.getElementById("print-check");
    var mark = document.querySelector("[data-mark-printed]");
    if (!check || !mark) return;
    check.hidden = false;
    mark.classList.add("is-prompt");
    mark.focus();
  });

  // ---------------------------------------------------------------- inline rename
  // partials/item_row.html: tapping the name loads the row with a text input (hx-get); the
  // input arrives selected, Enter saves (the form's hx-post), Esc cancels (re-fetches the row).
  document.addEventListener("htmx:afterSettle", function () {
    var input = document.querySelector("[data-rename-input]");
    if (input) selectText(input);
  });

  document.addEventListener("keydown", function (event) {
    if (event.key !== "Escape" || !event.target.matches("[data-rename-input]")) return;
    var cancel = event.target.closest("[data-rename]").querySelector("[data-rename-cancel]");
    if (cancel) cancel.click();
  });

  // ---------------------------------------------------------------- location photo
  // Tapping an outline on the location's photo scrolls to its item row and flashes it.
  document.addEventListener("click", function (event) {
    var box = event.target.closest(".box[data-overview-key]");
    if (!box) return;
    var row = document.getElementById("item-" + box.getAttribute("data-overview-key"));
    if (!row) return;
    row.scrollIntoView({ block: "center", behavior: "smooth" });
    row.classList.remove("flash");
    void row.offsetWidth;  // restart the animation on a second tap
    row.classList.add("flash");
  });

  // ---------------------------------------------------------------- Fix outline
  // [data-draw] (item_photo.html): "Fix outline" switches to drawing mode, where a pointer
  // drag (mouse or touch, captured) on the photo draws a rectangle; fractions of the photo
  // fill the form, and Save is enabled once each side is at least data-min.

  // The saved outline's page URL carries ?saved=1 (it shows the message); drop it so a reload
  // or a shared link does not repeat the message.
  try {
    var here = new URL(window.location.href);
    if (here.searchParams.has("saved") && document.querySelector("[data-draw]")) {
      here.searchParams.delete("saved");
      history.replaceState(null, "", here.pathname + here.search + here.hash);
    }
  } catch (e) { /* no URL or history API: leave it */ }

  function clamp(value) { return Math.min(Math.max(value, 0), 1); }

  function drawParts(root) {
    return {
      surface: root.querySelector(".gallery-image"),
      form: root.querySelector("[data-draw-form]"),
      idle: root.querySelector("[data-draw-idle]"),
      save: root.querySelector("[data-draw-save]"),
      status: root.querySelector("[data-draw-status]")
    };
  }

  function clearRect(root) {
    var parts = drawParts(root);
    var rect = parts.surface.querySelector(".draw-rect");
    if (rect) rect.remove();
    parts.save.disabled = true;
    root._drawPointer = null;
  }

  function setDrawing(root, on) {
    var parts = drawParts(root);
    clearRect(root);
    root.classList.toggle("is-drawing", on);
    parts.form.hidden = !on;
    parts.idle.hidden = on;
    parts.status.textContent = on ? "Drag on the photo to draw the new outline." : "";
  }

  function drawPoint(surface, event) {
    var bounds = surface.getBoundingClientRect();
    return {
      x: clamp((event.clientX - bounds.left) / bounds.width),
      y: clamp((event.clientY - bounds.top) / bounds.height)
    };
  }

  function showRect(root, a, b) {
    var surface = drawParts(root).surface;
    var rect = surface.querySelector(".draw-rect");
    if (!rect) {
      rect = document.createElement("span");
      rect.className = "draw-rect";
      rect.setAttribute("aria-hidden", "true");
      surface.appendChild(rect);
    }
    var box = { x0: Math.min(a.x, b.x), y0: Math.min(a.y, b.y), x1: Math.max(a.x, b.x), y1: Math.max(a.y, b.y) };
    rect.style.left = box.x0 * 100 + "%";
    rect.style.top = box.y0 * 100 + "%";
    rect.style.width = (box.x1 - box.x0) * 100 + "%";
    rect.style.height = (box.y1 - box.y0) * 100 + "%";
    return box;
  }

  document.addEventListener("click", function (event) {
    var root = event.target.closest("[data-draw]");
    if (!root) return;
    if (event.target.closest("[data-draw-start]")) setDrawing(root, true);
    else if (event.target.closest("[data-draw-cancel]")) setDrawing(root, false);
  });

  document.addEventListener("keydown", function (event) {
    if (event.key !== "Escape") return;
    var root = document.querySelector("[data-draw].is-drawing");
    if (root) setDrawing(root, false);
  });

  document.addEventListener("pointerdown", function (event) {
    var surface = event.target.closest && event.target.closest("[data-draw].is-drawing .gallery-image");
    if (!surface || event.button !== 0 || !event.isPrimary) return;
    var root = surface.closest("[data-draw]");
    event.preventDefault();
    try { surface.setPointerCapture(event.pointerId); } catch (e) { /* synthetic pointer */ }
    root._drawPointer = event.pointerId;
    root._drawStart = drawPoint(surface, event);
    drawParts(root).save.disabled = true;
    showRect(root, root._drawStart, root._drawStart);
  });

  function drawRoot(event) {
    var root = event.target.closest && event.target.closest("[data-draw].is-drawing");
    return root && root._drawPointer === event.pointerId ? root : null;
  }

  document.addEventListener("pointermove", function (event) {
    var root = drawRoot(event);
    if (root) showRect(root, root._drawStart, drawPoint(drawParts(root).surface, event));
  });

  document.addEventListener("pointerup", function (event) {
    var root = drawRoot(event);
    if (!root) return;
    var parts = drawParts(root);
    var box = showRect(root, root._drawStart, drawPoint(parts.surface, event));
    root._drawPointer = null;
    var min = parseFloat(root.getAttribute("data-min")) || 0.02;
    var ok = box.x1 - box.x0 >= min && box.y1 - box.y0 >= min;
    ["x0", "y0", "x1", "y1"].forEach(function (key) {
      parts.form.querySelector("[data-draw-" + key + "]").value = ok ? box[key].toFixed(6) : "";
    });
    parts.save.disabled = !ok;
    parts.status.textContent = ok ? "Outline drawn. Tap Save to keep it, or drag again."
      : "That outline is too small; drag a bigger one.";
  });

  document.addEventListener("pointercancel", function (event) {
    var root = drawRoot(event);
    if (!root) return;
    clearRect(root);
    drawParts(root).status.textContent = "Drawing was interrupted; drag again.";
  });

  // Move focus into a freshly opened sheet (keyboard and screen-reader users).
  document.addEventListener("htmx:afterSwap", function (event) {
    if (!event.detail.target || event.detail.target.id !== "sheet") return;
    var first = event.detail.target.querySelector(".sheet button, .sheet a, .sheet input");
    if (first) first.focus();
  });
})();
