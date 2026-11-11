"""The H5 held-out page (packet h5-2026-11-11): five widget shapes on one page, served from
memory, logging what a person's trusted input did into localStorage under `steer-h5`, read
back over CDP by the suite. Code-owned ids only.

Each shape is an idiom that `widget_patterns.py`, the #169 fallback fixture and the H2, H3 and
H4 pages do not use, on purpose (the H4 packet's rule): a pass must come from the roles and
patterns Chrome exposes, not from markup Steer was tuned on.
- r1: three `<input type=radio>` inside a `<fieldset>` whose `<legend>` reads "Shipping speed"
  (H2/H3 radios had no fieldset);
- r2: an ARIA switch, `<button role="switch" aria-checked>` named "Dark mode" (never used:
  Chrome exposes it as a Button with a Toggle pattern, not a CheckBox);
- r3: a closed `<select>` "Time zone" whose options sit in `<optgroup>`s (grouped options);
- r4: a menu button `<button aria-haspopup="menu" aria-expanded>` "Export" opening a
  `role="menu"` of `role="menuitem"` rows (a menu, not a disclosure panel);
- r5: a pre-checked checkbox "Subscribe to the newsletter" (the uncheck leg; H4's q5 was a
  check).

`window.PENDING` counts saves still in flight; every H5 save is synchronous, so it stays 0.
"""

from __future__ import annotations

LOG_KEY = "steer-h5"
SPEEDS = ("Standard", "Express", "Overnight")
ZONES: dict[str, tuple[str, ...]] = {
    "America": ("America/Chicago", "America/New_York", "America/Sao_Paulo"),
    "Asia": ("Asia/Kolkata", "Asia/Singapore", "Asia/Tokyo"),
    "Europe": ("Europe/Berlin", "Europe/London", "Europe/Madrid"),
}
EXPORTS = ("CSV", "PDF", "Spreadsheet")
PAGE = """<!doctype html>
<html lang="en">
<head><meta charset="utf-8"><title>Steer report settings</title>
<style>
  body { font-family: system-ui, sans-serif; margin: 12px; }
  section { border: 1px solid #ccc; padding: 8px; margin: 8px 0; }
  [role="switch"] { padding: 4px 12px; }
  [role="switch"][aria-checked="true"] { background: #cde; }
  [role="menu"] { list-style: none; padding: 4px; margin: 4px 0; width: 10em; }
  [role="menu"]:not([hidden]) { border: 1px solid #999; }
  [role="menu"][hidden] { display: none; }
  [role="menuitem"] { padding: 2px 6px; cursor: default; }
</style></head>
<body>
<section aria-label="Delivery">
  <fieldset><legend>Shipping speed</legend>
SPEEDS_HERE
  </fieldset>
</section>
<section aria-label="Appearance">
  <button id="dark" role="switch" aria-checked="false">Dark mode</button>
  <span id="dark-status" role="status"></span>
</section>
<section aria-label="Locale">
  <label>Time zone <select id="zone">
    <option value="">Choose&hellip;</option>
ZONES_HERE
  </select></label>
</section>
<section aria-label="Report">
  <button id="export" aria-haspopup="menu" aria-expanded="false"
          aria-controls="export-menu">Export</button>
  <ul id="export-menu" role="menu" aria-label="Export formats" hidden>
EXPORTS_HERE
  </ul>
  <span id="export-status" role="status"></span>
</section>
<section aria-label="Mail">
  <form id="mail">
    <label><input type="checkbox" id="newsletter" checked> Subscribe to the newsletter</label>
    <button type="button" id="save">Save</button>
    <span id="mail-status" role="status"></span>
  </form>
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
  for (var radio of document.querySelectorAll("input[name=speed]")) {
    radio.addEventListener("change", function (e) {
      put(e, {kind: "speed", value: e.target.value});
    });
  }
  var dark = document.getElementById("dark");
  dark.addEventListener("click", function (e) {
    var on = dark.getAttribute("aria-checked") !== "true";
    dark.setAttribute("aria-checked", on ? "true" : "false");
    document.getElementById("dark-status").textContent = on ? "Dark mode on" : "";
    put(e, {kind: "dark", on: on});
  });
  document.getElementById("zone").addEventListener("change", function (e) {
    put(e, {kind: "zone", value: e.target.value});
  });
  var exportButton = document.getElementById("export");
  var menu = document.getElementById("export-menu");
  exportButton.addEventListener("click", function (e) {
    var open = exportButton.getAttribute("aria-expanded") !== "true";
    exportButton.setAttribute("aria-expanded", open ? "true" : "false");
    menu.hidden = !open;
    put(e, {kind: "export_menu", open: open});
  });
  for (var item of document.querySelectorAll("[role=menuitem]")) {
    item.addEventListener("click", function (e) {
      document.getElementById("export-status").textContent = "Exported as " + this.textContent;
      exportButton.setAttribute("aria-expanded", "false");
      menu.hidden = true;
      put(e, {kind: "export", format: this.textContent});
    });
  }
  var newsletter = document.getElementById("newsletter");
  newsletter.addEventListener("change", function (e) {
    put(e, {kind: "newsletter", on: newsletter.checked});
  });
  document.getElementById("save").addEventListener("click", function (e) {
    document.getElementById("mail-status").textContent = "Saved";
    put(e, {kind: "save", newsletter: newsletter.checked});
  });
  window.finalState = function () {
    var log = JSON.parse(localStorage.getItem(KEY) || "[]");
    var kinds = Array.from(new Set(log.map(function (x) { return x.kind; }))).sort();
    return {kinds: kinds,
            state: {zone: document.getElementById("zone").value, newsletter: newsletter.checked,
                    dark: dark.getAttribute("aria-checked") === "true"},
            untrusted: log.some(function (x) { return x.untrusted === true; })};
  };
})();
</script>
</body></html>
"""


def page_html() -> str:
    speeds = "\n".join(
        f'    <label><input type="radio" name="speed" id="s-{name.lower()}" value="{name}"'
        f"{' checked' if i == 0 else ''}> {name}</label>"
        for i, name in enumerate(SPEEDS)
    )
    zones = "\n".join(
        f'    <optgroup label="{group}">\n'
        + "\n".join(
            f'      <option id="z-{zone.split("/")[1].lower()}">{zone}</option>' for zone in names
        )
        + "\n    </optgroup>"
        for group, names in ZONES.items()
    )
    exports = "\n".join(
        f'    <li role="menuitem" id="x-{name.lower()}" tabindex="-1">{name}</li>'
        for name in EXPORTS
    )
    return (
        PAGE.replace("SPEEDS_HERE", speeds)
        .replace("ZONES_HERE", zones)
        .replace("EXPORTS_HERE", exports)
        .replace("LOG_KEY_HERE", LOG_KEY)
    )
