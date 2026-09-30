// Small progressive enhancements; everything works without JS except htmx quick actions.
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

  // Move focus into a freshly opened sheet (keyboard and screen-reader users).
  document.addEventListener("htmx:afterSwap", function (event) {
    if (!event.detail.target || event.detail.target.id !== "sheet") return;
    var first = event.detail.target.querySelector(".sheet button, .sheet a, .sheet input");
    if (first) first.focus();
  });
})();
