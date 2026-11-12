"""The H6 held-out page (packet interaction-model-2026-11-12 §6): five widget shapes on one page,
served from memory, logging what a person's trusted input did into localStorage under `steer-h6`,
read back over CDP by the suite. Committed (sealed) before the interaction model's verb admission
merges, so it measures the model, not a rule written for it.

Each shape is a row of the model's table in markup the gallery, H2-H5 and `widget_patterns.py` do
not use (the gallery holds one idiom per row; these are the same capabilities, written otherwise):
- u1: a toolbar toggle `<button aria-pressed>` "Mute notifications" inside `role=toolbar`;
- u2: a native `<input type=checkbox role=switch>` "Auto-save" (a switch on an input, not a
  button);
- u3: a file tree: `role=tree` with a folder `treeitem` "Reports" that opens to reveal "2024";
  the task opens the folder and selects the year;
- u4: a `role=grid` of invoices built from `<div>`s, a row selected by `aria-selected`;
- u5: a "Sort" menu button whose `role=menu` holds `menuitemradio` rows; the task picks "Newest
  first".

`window.PENDING` stays 0: every save is synchronous.
"""

from __future__ import annotations

LOG_KEY = "steer-h6"
INVOICES = ("Invoice 2040", "Invoice 2041", "Invoice 2042", "Invoice 2043")
SORTS = ("Relevance", "Newest first", "Oldest first")

PAGE = """<!doctype html>
<html lang="en">
<head><meta charset="utf-8"><title>Steer workspace</title>
<style>
  body { font-family: system-ui, sans-serif; margin: 12px; }
  section { border: 1px solid #ccc; padding: 6px 8px; margin: 6px 0; }
  [aria-pressed="true"], [aria-selected="true"], [aria-checked="true"] { background: #cde; }
  [role="grid"] { display: table; } [role="row"] { display: table-row; }
  [role="gridcell"] { display: table-cell; padding: 2px 8px; }
  [role="menu"] { list-style: none; padding: 2px; border: 1px solid #999; width: 10em; }
  [hidden] { display: none !important; }
</style></head>
<body>
<section aria-label="Notifications">
  <div role="toolbar" aria-label="Message tools">
    <button id="mute" aria-pressed="false">Mute notifications</button>
    <button id="pin">Pin chat</button>
  </div>
</section>
<section aria-label="Editor">
  <label><input type="checkbox" role="switch" id="autosave"> Auto-save</label>
</section>
<section aria-label="Files">
  <ul role="tree" aria-label="Documents">
    <li role="treeitem" id="reports" aria-expanded="false" aria-selected="false">
      <span>Reports</span>
      <ul role="group" id="reports-group" hidden>
        <li role="treeitem" id="y2023" aria-selected="false">2023</li>
        <li role="treeitem" id="y2024" aria-selected="false">2024</li>
      </ul>
    </li>
    <li role="treeitem" id="drafts" aria-selected="false">Drafts</li>
  </ul>
</section>
<section aria-label="Billing">
  <div role="grid" aria-label="Invoices">
INVOICES_HERE
  </div>
</section>
<section aria-label="Results">
  <button id="sort" aria-haspopup="menu" aria-expanded="false"
          aria-controls="sort-menu">Sort</button>
  <ul id="sort-menu" role="menu" aria-label="Sort order" hidden>
SORTS_HERE
  </ul>
</section>
<script>
(function () {
  var KEY = "LOG_KEY_HERE";
  window.PENDING = 0;
  function put(e, entry) {
    if (!e.isTrusted) { entry.untrusted = true; }
    var log = JSON.parse(localStorage.getItem(KEY) || "[]");
    log.push(entry);
    localStorage.setItem(KEY, JSON.stringify(log));
  }
  var mute = document.getElementById("mute");
  mute.addEventListener("click", function (e) {
    var on = mute.getAttribute("aria-pressed") !== "true";
    mute.setAttribute("aria-pressed", on ? "true" : "false");
    put(e, {kind: "mute", on: on});
  });
  var autosave = document.getElementById("autosave");
  autosave.addEventListener("change", function (e) {
    put(e, {kind: "autosave", on: autosave.checked});
  });
  var reports = document.getElementById("reports");
  reports.firstElementChild.addEventListener("click", function () {
    var open = reports.getAttribute("aria-expanded") !== "true";
    reports.setAttribute("aria-expanded", open ? "true" : "false");
    document.getElementById("reports-group").hidden = !open;
  });
  document.querySelectorAll("[role=treeitem]").forEach(function (item) {
    item.addEventListener("click", function (e) {
      if (item === reports && e.target !== item) { return; }
      e.stopPropagation();
      document.querySelectorAll("[role=treeitem]").forEach(function (o) {
        o.setAttribute("aria-selected", o === item ? "true" : "false");
      });
      if (item.id === "y2023" || item.id === "y2024") {
        put(e, {kind: "year", value: item.textContent.trim()});
      }
    });
  });
  document.querySelectorAll("[role=row]").forEach(function (row) {
    row.addEventListener("click", function (e) {
      document.querySelectorAll("[role=row]").forEach(function (o) {
        o.setAttribute("aria-selected", o === row ? "true" : "false");
      });
      put(e, {kind: "invoice", value: row.textContent.trim()});
    });
  });
  var sort = document.getElementById("sort"), menu = document.getElementById("sort-menu");
  sort.addEventListener("click", function () {
    var open = sort.getAttribute("aria-expanded") !== "true";
    sort.setAttribute("aria-expanded", open ? "true" : "false");
    menu.hidden = !open;
  });
  document.querySelectorAll("[role=menuitemradio]").forEach(function (r) {
    r.addEventListener("click", function (e) {
      document.querySelectorAll("[role=menuitemradio]").forEach(function (o) {
        o.setAttribute("aria-checked", o === r ? "true" : "false");
      });
      sort.setAttribute("aria-expanded", "false");
      menu.hidden = true;
      put(e, {kind: "sort", value: r.textContent.trim()});
    });
  });
})();
</script>
</body></html>
"""


def page_html() -> str:
    invoices = "\n".join(
        f'    <div role="row" id="inv-{name.split()[1]}" aria-selected="false">'
        f'<span role="gridcell">{name}</span></div>'
        for name in INVOICES
    )
    sorts = "\n".join(
        f'    <li role="menuitemradio" id="sort-{name.split()[0].lower()}"'
        f' aria-checked="{"true" if i == 0 else "false"}" tabindex="-1">{name}</li>'
        for i, name in enumerate(SORTS)
    )
    return (
        PAGE.replace("INVOICES_HERE", invoices)
        .replace("SORTS_HERE", sorts)
        .replace("LOG_KEY_HERE", LOG_KEY)
    )
