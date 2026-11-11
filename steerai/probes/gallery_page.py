"""The gallery (packet interaction-model-2026-11-12 §5): one local page holding every control idiom
the interaction model's table covers, native and ARIA, each logging the trusted event a person's
input caused into localStorage under `steer-gallery`. It is the bar before any held-out set: each
row's capability tokens are measured once on it (Chrome's real mapping), and each row's verb must
act and verify on it through the planner 3/3.

Rows (id: control, idiom, the verb the model gives it):
- g01 button, `<button>`: click
- g02 toggle button, `aria-pressed`: check
- g03 switch, `role=switch`: check
- g04 checkbox, native: check
- g05 checkbox, `role=checkbox`: check
- g06 tri-state checkbox, `aria-checked=mixed`: check
- g07 radios in a fieldset, native: select
- g08 radiogroup, ARIA: select
- g09 tablist of buttons: select
- g10 listbox, ARIA: select
- g11 `<select size=6>`: select
- g12 `<select>`: choose
- g13 `<select>` with `<optgroup>`: choose
- g14 ARIA combobox with a listbox popup: choose
- g15 `<details><summary>`: expand
- g16 disclosure button, `aria-expanded`: expand
- g17 menu button + `role=menu`: expand, then click the menuitem
- g18 `menuitemcheckbox`: check
- g19 `menuitemradio`: select
- g20 tree + treeitem: expand, select
- g21 grid with a selectable row: select
- g22 text field: type
- g23 search box, `type=search`: type, submit
- g24 textarea: type
- g25 link: click
- g26 slider: no verb (`range`)
- g27 spinbutton: no verb (`range`)
"""

from __future__ import annotations

LOG_KEY = "steer-gallery"

PAGE = """<!doctype html>
<html lang="en">
<head><meta charset="utf-8"><title>Steer gallery</title>
<style>
  body { font-family: system-ui, sans-serif; margin: 10px; font-size: 13px; }
  section { border: 1px solid #ccc; padding: 4px 8px; margin: 4px 0; }
  [role="menu"], [role="listbox"]#g14-list { list-style: none; padding: 2px; margin: 2px 0;
    border: 1px solid #999; width: 10em; }
  [hidden] { display: none !important; }
  [role="option"][aria-selected="true"], [role="treeitem"][aria-selected="true"],
  [role="row"][aria-selected="true"] { background: #cde; }
  [role="switch"][aria-checked="true"], [aria-pressed="true"] { background: #cde; }
</style></head>
<body>
<section aria-label="Buttons">
  <button id="g01">Save draft</button>
  <button id="g02" aria-pressed="false">Bold</button>
  <button id="g03" role="switch" aria-checked="false">Dark mode</button>
</section>
<section aria-label="Boxes">
  <label><input type="checkbox" id="g04"> Email me updates</label>
  <span id="g05" role="checkbox" aria-checked="false" tabindex="0">Remember this device</span>
  <span id="g06" role="checkbox" aria-checked="mixed" tabindex="0">Select all files</span>
</section>
<section aria-label="Choices">
  <fieldset><legend>Delivery speed</legend>
    <label><input type="radio" name="speed" id="g07-standard" value="Standard" checked>
      Standard</label>
    <label><input type="radio" name="speed" id="g07-express" value="Express"> Express</label>
  </fieldset>
  <div role="radiogroup" aria-label="Paper size">
    <span role="radio" id="g08-a4" aria-checked="true" tabindex="0">A4</span>
    <span role="radio" id="g08-letter" aria-checked="false" tabindex="-1">Letter</span>
  </div>
  <div role="tablist" aria-label="Account tabs">
    <button role="tab" id="g09-profile" aria-selected="true">Profile</button>
    <button role="tab" id="g09-billing" aria-selected="false">Billing details</button>
  </div>
  <ul role="listbox" id="g10" aria-label="Favourite fruit" tabindex="0">
    <li role="option" id="g10-apple" aria-selected="true">Apple</li>
    <li role="option" id="g10-mango" aria-selected="false">Mango</li>
  </ul>
  <label>Shelf <select id="g11" size="6">
    <option>Fiction</option><option>History</option><option>Poetry</option>
  </select></label>
</section>
<section aria-label="Dropdowns">
  <label>Country <select id="g12">
    <option value="">Choose&hellip;</option><option>Canada</option><option>Kenya</option>
  </select></label>
  <label>Time zone <select id="g13">
    <option value="">Choose&hellip;</option>
    <optgroup label="Asia"><option>Asia/Tokyo</option><option>Asia/Dubai</option></optgroup>
    <optgroup label="Europe"><option>Europe/Oslo</option><option>Europe/Rome</option></optgroup>
  </select></label>
  <label id="g14-label">Colour</label>
  <input id="g14" role="combobox" aria-labelledby="g14-label" aria-expanded="false"
         aria-controls="g14-list" aria-autocomplete="list" readonly value="">
  <ul id="g14-list" role="listbox" aria-label="Colours" hidden>
    <li role="option" id="g14-red">Red</li><li role="option" id="g14-teal">Teal</li>
  </ul>
</section>
<section aria-label="Disclosures">
  <details id="g15"><summary id="g15-summary">Shipping policy</summary>
    <p>Ships in 2 days.</p></details>
  <button id="g16" aria-expanded="false" aria-controls="g16-panel">Show filters</button>
  <div id="g16-panel" hidden>Filters panel</div>
  <button id="g17" aria-haspopup="menu" aria-expanded="false"
          aria-controls="g17-menu">Share</button>
  <ul id="g17-menu" role="menu" aria-label="Share options" hidden>
    <li role="menuitem" id="g17-copy" tabindex="-1">Copy link</li>
    <li role="menuitemcheckbox" id="g18" aria-checked="false" tabindex="-1">Show word count</li>
    <li role="menuitemradio" id="g19-small" aria-checked="true" tabindex="-1">Small font</li>
    <li role="menuitemradio" id="g19-large" aria-checked="false" tabindex="-1">Large font</li>
  </ul>
</section>
<section aria-label="Structures">
  <ul role="tree" aria-label="Folders">
    <li role="treeitem" id="g20" aria-expanded="false" aria-selected="false">Projects
      <ul role="group" id="g20-group" hidden>
        <li role="treeitem" id="g20-alpha" aria-selected="false">Alpha</li>
      </ul>
    </li>
  </ul>
  <table role="grid" aria-label="Orders">
    <tr role="row" id="g21-1001" aria-selected="false"><td role="gridcell">Order 1001</td></tr>
    <tr role="row" id="g21-1002" aria-selected="false"><td role="gridcell">Order 1002</td></tr>
  </table>
</section>
<section aria-label="Fields">
  <label>Full name <input id="g22" type="text"></label>
  <label>Find articles <input id="g23" type="search"></label>
  <label>Comments <textarea id="g24" rows="2"></textarea></label>
  <a id="g25" href="#terms">Terms of service</a>
  <label>Volume <input id="g26" type="range" min="0" max="10" value="3"></label>
  <label>Quantity <input id="g27" type="number" min="1" max="9" value="1"></label>
  <span id="status" role="status"></span>
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
  function say(text) { document.getElementById("status").textContent = text; }
  function on(id, type, fn) { document.getElementById(id).addEventListener(type, fn); }
  function ariaToggle(el, attr) {
    var now = el.getAttribute(attr) === "true" ? "false" : "true";
    el.setAttribute(attr, now);
    return now === "true";
  }
  function keyActivates(e) { return e.key === " " || e.key === "Enter"; }
  on("g01", "click", function (e) { say("Draft saved"); put(e, {row: "g01"}); });
  on("g02", "click", function (e) { put(e, {row: "g02", on: ariaToggle(this, "aria-pressed")}); });
  on("g03", "click", function (e) { put(e, {row: "g03", on: ariaToggle(this, "aria-checked")}); });
  on("g04", "change", function (e) { put(e, {row: "g04", on: this.checked}); });
  ["g05", "g06"].forEach(function (id) {
    var el = document.getElementById(id);
    function flip(e) {
      var on = el.getAttribute("aria-checked") !== "true";
      el.setAttribute("aria-checked", on ? "true" : "false");
      put(e, {row: id, on: on});
    }
    el.addEventListener("click", flip);
    el.addEventListener("keydown", function (e) {
      if (e.key === " ") { e.preventDefault(); flip(e); }
    });
  });
  document.querySelectorAll("input[name=speed]").forEach(function (r) {
    r.addEventListener("change", function (e) { put(e, {row: "g07", value: this.value}); });
  });
  document.querySelectorAll("[role=radio]").forEach(function (r) {
    r.addEventListener("click", function (e) {
      document.querySelectorAll("[role=radio]").forEach(function (o) {
        o.setAttribute("aria-checked", o === r ? "true" : "false");
      });
      put(e, {row: "g08", value: r.textContent});
    });
  });
  document.querySelectorAll("[role=tab]").forEach(function (t) {
    t.addEventListener("click", function (e) {
      document.querySelectorAll("[role=tab]").forEach(function (o) {
        o.setAttribute("aria-selected", o === t ? "true" : "false");
      });
      put(e, {row: "g09", value: t.textContent});
    });
  });
  document.querySelectorAll("#g10 [role=option]").forEach(function (o) {
    o.addEventListener("click", function (e) {
      document.querySelectorAll("#g10 [role=option]").forEach(function (x) {
        x.setAttribute("aria-selected", x === o ? "true" : "false");
      });
      put(e, {row: "g10", value: o.textContent});
    });
  });
  on("g11", "change", function (e) { put(e, {row: "g11", value: this.value}); });
  on("g12", "change", function (e) { put(e, {row: "g12", value: this.value}); });
  on("g13", "change", function (e) { put(e, {row: "g13", value: this.value}); });
  var combo = document.getElementById("g14"), list = document.getElementById("g14-list");
  combo.addEventListener("click", function () {
    var open = combo.getAttribute("aria-expanded") !== "true";
    combo.setAttribute("aria-expanded", open ? "true" : "false");
    list.hidden = !open;
  });
  document.querySelectorAll("#g14-list [role=option]").forEach(function (o) {
    o.addEventListener("click", function (e) {
      combo.value = o.textContent;
      combo.setAttribute("aria-expanded", "false");
      list.hidden = true;
      put(e, {row: "g14", value: o.textContent});
    });
  });
  var details = document.getElementById("g15"), summaryTrusted = false;
  on("g15-summary", "click", function (e) { summaryTrusted = e.isTrusted; });
  details.addEventListener("toggle", function () {
    put({isTrusted: summaryTrusted}, {row: "g15", open: details.open});
    summaryTrusted = false;
  });
  on("g16", "click", function (e) {
    var open = ariaToggle(this, "aria-expanded");
    document.getElementById("g16-panel").hidden = !open;
    put(e, {row: "g16", open: open});
  });
  var share = document.getElementById("g17"), menu = document.getElementById("g17-menu");
  share.addEventListener("click", function () {
    var open = share.getAttribute("aria-expanded") !== "true";
    share.setAttribute("aria-expanded", open ? "true" : "false");
    menu.hidden = !open;
  });
  on("g17-copy", "click", function (e) {
    say("Link copied"); share.setAttribute("aria-expanded", "false"); menu.hidden = true;
    put(e, {row: "g17", item: "Copy link"});
  });
  on("g18", "click", function (e) { put(e, {row: "g18", on: ariaToggle(this, "aria-checked")}); });
  document.querySelectorAll("[role=menuitemradio]").forEach(function (r) {
    r.addEventListener("click", function (e) {
      document.querySelectorAll("[role=menuitemradio]").forEach(function (o) {
        o.setAttribute("aria-checked", o === r ? "true" : "false");
      });
      put(e, {row: "g19", value: r.textContent});
    });
  });
  var folder = document.getElementById("g20");
  folder.addEventListener("click", function (e) {
    if (e.target !== folder) { return; }
    var open = ariaToggle(folder, "aria-expanded");
    document.getElementById("g20-group").hidden = !open;
    put(e, {row: "g20", open: open});
  });
  on("g20-alpha", "click", function (e) {
    e.stopPropagation();
    this.setAttribute("aria-selected", "true");
    put(e, {row: "g20-alpha", selected: true});
  });
  document.querySelectorAll("[role=row]").forEach(function (row) {
    row.addEventListener("click", function (e) {
      document.querySelectorAll("[role=row]").forEach(function (o) {
        o.setAttribute("aria-selected", o === row ? "true" : "false");
      });
      put(e, {row: "g21", value: row.textContent.trim()});
    });
  });
  on("g22", "change", function (e) { put(e, {row: "g22", value: this.value}); });
  on("g23", "keydown", function (e) {
    if (e.key === "Enter") {
      say("Results for " + this.value);
      put(e, {row: "g23", value: this.value});
    }
  });
  on("g24", "change", function (e) { put(e, {row: "g24", value: this.value}); });
  on("g25", "click", function (e) {
    e.preventDefault(); say("Terms opened"); put(e, {row: "g25"});
  });
})();
</script>
</body></html>
"""


def page_html() -> str:
    return PAGE.replace("LOG_KEY_HERE", LOG_KEY)
