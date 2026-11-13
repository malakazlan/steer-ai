"""The H7 held-out page (V2 §12 step 8, [V2.4]): eight widget rows and two commit fixtures on
one page, served from memory, logging what a person's input did into localStorage under
`steer-h7`, read back over CDP by the suite. Sealed before any V2 step 3 code, written without
looking at v2 runs.

Each row is markup no earlier page (H2-H6), the gallery or `widget_patterns.py` uses:
- w1: a disclosure `<button aria-expanded aria-controls>` "Shipping details" over a hidden panel;
- w2: a `role=listbox` that keeps focus on itself and marks the current option with
  `aria-activedescendant`; the task picks "Teal";
- w3: a `role=tablist` of three tabs over three tab panels; the task opens "Pricing";
- w4: a switch inside a custom element's open shadow root ("High contrast");
- w5: an `<input role=combobox>` whose options are a sibling `role=listbox` popup it controls,
  filtered as the person types; the task sets the city to "Lisbon";
- w6: a modal dialog flow: "Rename project" opens `role=dialog aria-modal=true` with a name
  field and Save; the task renames the project to "Orion";
- w7: a two-level menu: a "Format" menubar item opens a menu whose "Text size" item opens a
  submenu; the task picks "Large";
- w8: a mouse-only rating with no ARIA and no keyboard path: five plain `<span>` stars;
  the task gives 4 stars.
Commit fixtures:
- c1: a team chat with a message box and Send; the send is the commit;
- c2: a drafts list whose Delete opens a confirm dialog; the confirmed delete is the commit.

`window.PENDING` stays 0: every save is synchronous.
"""

from __future__ import annotations

LOG_KEY = "steer-h7"
COLOURS = ("Crimson", "Teal", "Amber", "Slate")
TABS = ("Overview", "Pricing", "Reviews")
CITIES = ("Lima", "Lisbon", "Ljubljana", "London", "Lyon")
SIZES = ("Small", "Medium", "Large")
DRAFTS = ("Q3 plan", "Hiring notes", "Offsite agenda")

PAGE = """<!doctype html>
<html lang="en">
<head><meta charset="utf-8"><title>Steer studio</title>
<style>
  body { font-family: system-ui, sans-serif; margin: 12px; }
  section { border: 1px solid #ccc; padding: 6px 8px; margin: 6px 0; }
  [aria-selected="true"], [aria-checked="true"] { background: #cde; }
  .opt-current { outline: 2px solid #36c; }
  [role="listbox"] { list-style: none; padding: 2px; margin: 2px 0; width: 12em; }
  [role="dialog"] { position: fixed; top: 30%; left: 30%; background: #fff;
    border: 2px solid #333; padding: 12px; z-index: 10; }
  .menu, .submenu { list-style: none; padding: 2px; margin: 0; border: 1px solid #999;
    background: #fff; width: 9em; }
  .stars span { cursor: pointer; font-size: 22px; padding: 0 2px; }
  steer-toggle { display: inline-block; }
  [hidden] { display: none !important; }
</style></head>
<body>
<section aria-label="Order">
  <button id="ship-toggle" aria-expanded="false"
          aria-controls="ship-panel">Shipping details</button>
  <div id="ship-panel" hidden><p>Ships in 2 days from the Porto warehouse.</p></div>
</section>
<section aria-label="Theme">
  <span id="colour-label">Theme colour</span>
  <ul role="listbox" id="colours" tabindex="0" aria-labelledby="colour-label"
      aria-activedescendant="col-crimson">
COLOURS_HERE
  </ul>
</section>
<section aria-label="Plan">
  <div role="tablist" aria-label="Plan details">
TABS_HERE
  </div>
PANELS_HERE
</section>
<section aria-label="Display">
  <steer-toggle id="contrast-host" label="High contrast"></steer-toggle>
</section>
<section aria-label="Travel">
  <label for="city">City</label>
  <input id="city" role="combobox" aria-autocomplete="list" aria-expanded="false"
         aria-controls="city-options" autocomplete="off">
  <ul role="listbox" id="city-options" aria-label="Cities" hidden>
CITIES_HERE
  </ul>
</section>
<section aria-label="Project">
  <p>Project: <span id="project-name">Untitled</span></p>
  <button id="rename-open">Rename project</button>
  <div role="dialog" id="rename-dialog" aria-modal="true" aria-labelledby="rename-title" hidden>
    <h2 id="rename-title">Rename project</h2>
    <label for="rename-field">Project name</label>
    <input id="rename-field" type="text">
    <button id="rename-save">Save</button>
    <button id="rename-cancel">Cancel</button>
  </div>
</section>
<section aria-label="Formatting">
  <div role="menubar" aria-label="Editor menu">
    <span role="menuitem" id="format" aria-haspopup="menu" aria-expanded="false"
          tabindex="0">Format</span>
  </div>
  <ul class="menu" role="menu" id="format-menu" aria-label="Format" hidden>
    <li role="menuitem" id="bold" tabindex="-1">Bold</li>
    <li role="menuitem" id="text-size" aria-haspopup="menu" aria-expanded="false"
        tabindex="-1">Text size</li>
  </ul>
  <ul class="submenu" role="menu" id="size-menu" aria-label="Text size" hidden>
SIZES_HERE
  </ul>
</section>
<section aria-label="Feedback">
  <div>Rate this recipe</div>
  <div class="stars" id="stars">STARS_HERE</div>
  <div id="rating-caption">Not rated yet</div>
</section>
<section aria-label="Team chat">
  <label for="chat-box">Message</label>
  <textarea id="chat-box" rows="2"></textarea>
  <button id="chat-send">Send</button>
  <ul id="chat-log" aria-label="Messages"></ul>
</section>
<section aria-label="Drafts">
  <ul id="drafts" aria-label="Drafts list">
DRAFTS_HERE
  </ul>
  <div role="alertdialog" id="confirm-delete" aria-modal="true" aria-labelledby="confirm-text"
       hidden>
    <p id="confirm-text"></p>
    <button id="confirm-yes">Delete</button>
    <button id="confirm-no">Cancel</button>
  </div>
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
  function byId(id) { return document.getElementById(id); }

  var ship = byId("ship-toggle");
  ship.addEventListener("click", function (e) {
    var open = ship.getAttribute("aria-expanded") !== "true";
    ship.setAttribute("aria-expanded", open ? "true" : "false");
    byId("ship-panel").hidden = !open;
    put(e, {kind: "disclosure", open: open});
  });

  var colours = byId("colours");
  colours.querySelectorAll("li").forEach(function (li) {
    li.addEventListener("click", function (e) {
      colours.querySelectorAll("li").forEach(function (o) {
        o.setAttribute("aria-selected", o === li ? "true" : "false");
        o.classList.toggle("opt-current", o === li);
      });
      colours.setAttribute("aria-activedescendant", li.id);
      colours.focus();
      put(e, {kind: "colour", value: li.textContent.trim()});
    });
  });

  document.querySelectorAll("[role=tab]").forEach(function (tab) {
    tab.addEventListener("click", function (e) {
      document.querySelectorAll("[role=tab]").forEach(function (o) {
        var on = o === tab;
        o.setAttribute("aria-selected", on ? "true" : "false");
        o.tabIndex = on ? 0 : -1;
        byId(o.getAttribute("aria-controls")).hidden = !on;
      });
      put(e, {kind: "tab", value: tab.textContent.trim()});
    });
  });

  customElements.define("steer-toggle", class extends HTMLElement {
    connectedCallback() {
      var root = this.attachShadow({mode: "open"});
      var host = this;
      root.innerHTML = '<button role="switch" aria-checked="false" style="display:block;' +
        'width:10em;padding:4px">' + this.getAttribute("label") + '</button>';
      var button = root.querySelector("button");
      button.addEventListener("click", function (e) {
        var on = button.getAttribute("aria-checked") !== "true";
        button.setAttribute("aria-checked", on ? "true" : "false");
        put(e, {kind: "contrast", on: on});
      });
    }
  });

  var city = byId("city"), cities = byId("city-options");
  function filterCities() {
    var text = city.value.trim().toLowerCase();
    var any = false;
    cities.querySelectorAll("li").forEach(function (li) {
      var show = li.textContent.toLowerCase().indexOf(text) === 0;
      li.hidden = !show;
      any = any || show;
    });
    cities.hidden = !any;
    city.setAttribute("aria-expanded", any ? "true" : "false");
  }
  city.addEventListener("focus", filterCities);
  city.addEventListener("input", filterCities);
  cities.querySelectorAll("li").forEach(function (li) {
    li.addEventListener("click", function (e) {
      city.value = li.textContent.trim();
      cities.hidden = true;
      city.setAttribute("aria-expanded", "false");
      put(e, {kind: "city", value: city.value});
    });
  });

  var dialog = byId("rename-dialog");
  byId("rename-open").addEventListener("click", function () {
    byId("rename-field").value = "";
    dialog.hidden = false;
    byId("rename-field").focus();
  });
  byId("rename-cancel").addEventListener("click", function () { dialog.hidden = true; });
  byId("rename-save").addEventListener("click", function (e) {
    var name = byId("rename-field").value.trim();
    if (!name) { return; }
    byId("project-name").textContent = name;
    dialog.hidden = true;
    put(e, {kind: "rename", value: name});
  });

  var format = byId("format"), formatMenu = byId("format-menu");
  var size = byId("text-size"), sizeMenu = byId("size-menu");
  format.addEventListener("click", function () {
    var open = format.getAttribute("aria-expanded") !== "true";
    format.setAttribute("aria-expanded", open ? "true" : "false");
    formatMenu.hidden = !open;
    if (!open) { sizeMenu.hidden = true; size.setAttribute("aria-expanded", "false"); }
  });
  size.addEventListener("click", function () {
    var open = size.getAttribute("aria-expanded") !== "true";
    size.setAttribute("aria-expanded", open ? "true" : "false");
    sizeMenu.hidden = !open;
  });
  sizeMenu.querySelectorAll("[role=menuitemradio]").forEach(function (item) {
    item.addEventListener("click", function (e) {
      sizeMenu.querySelectorAll("[role=menuitemradio]").forEach(function (o) {
        o.setAttribute("aria-checked", o === item ? "true" : "false");
      });
      sizeMenu.hidden = true;
      formatMenu.hidden = true;
      format.setAttribute("aria-expanded", "false");
      size.setAttribute("aria-expanded", "false");
      put(e, {kind: "size", value: item.textContent.trim()});
    });
  });

  byId("stars").querySelectorAll("span").forEach(function (star) {
    star.addEventListener("click", function (e) {
      var value = Number(star.getAttribute("data-v"));
      byId("stars").querySelectorAll("span").forEach(function (o) {
        o.innerHTML = Number(o.getAttribute("data-v")) <= value ? "&#9733;" : "&#9734;";
      });
      byId("rating-caption").textContent = "Your rating: " + value + " of 5";
      put(e, {kind: "rating", value: value});
    });
  });

  byId("chat-send").addEventListener("click", function (e) {
    var text = byId("chat-box").value.trim();
    if (!text) { return; }
    var li = document.createElement("li");
    li.textContent = text;
    byId("chat-log").appendChild(li);
    byId("chat-box").value = "";
    put(e, {kind: "send", text: text});
  });

  var confirmBox = byId("confirm-delete"), pending = null;
  document.querySelectorAll(".draft-delete").forEach(function (button) {
    button.addEventListener("click", function () {
      pending = button.closest("li");
      byId("confirm-text").textContent =
        'Delete draft "' + pending.getAttribute("data-name") + '"?';
      confirmBox.hidden = false;
    });
  });
  byId("confirm-no").addEventListener("click", function () {
    confirmBox.hidden = true;
    pending = null;
  });
  byId("confirm-yes").addEventListener("click", function (e) {
    if (!pending) { return; }
    var name = pending.getAttribute("data-name");
    pending.remove();
    pending = null;
    confirmBox.hidden = true;
    put(e, {kind: "delete", value: name});
  });
})();
</script>
</body></html>
"""


def _slug(name: str) -> str:
    return name.lower().replace(" ", "-")


def page_html() -> str:
    colours = "\n".join(
        f'    <li role="option" id="col-{_slug(name)}" aria-selected="false">{name}</li>'
        for name in COLOURS
    )
    tabs = "\n".join(
        f'    <button role="tab" id="tab-{_slug(name)}" aria-controls="panel-{_slug(name)}"'
        f' aria-selected="{"true" if i == 0 else "false"}"'
        f' tabindex="{0 if i == 0 else -1}">{name}</button>'
        for i, name in enumerate(TABS)
    )
    panels = "\n".join(
        f'  <div role="tabpanel" id="panel-{_slug(name)}" aria-labelledby="tab-{_slug(name)}"'
        f"{'' if i == 0 else ' hidden'}><p>{name} of the Studio plan.</p></div>"
        for i, name in enumerate(TABS)
    )
    cities = "\n".join(
        f'    <li role="option" id="city-{_slug(name)}">{name}</li>' for name in CITIES
    )
    sizes = "\n".join(
        f'    <li role="menuitemradio" id="size-{_slug(name)}"'
        f' aria-checked="{"true" if name == "Medium" else "false"}" tabindex="-1">{name}</li>'
        for name in SIZES
    )
    drafts = "\n".join(
        f'    <li id="draft-{_slug(name)}" data-name="{name}">{name}'
        f' <button class="draft-delete" id="del-{_slug(name)}"'
        f' aria-label="Delete {name}">Delete</button></li>'
        for name in DRAFTS
    )
    return (
        PAGE.replace("COLOURS_HERE", colours)
        .replace("TABS_HERE", tabs)
        .replace("PANELS_HERE", panels)
        .replace("CITIES_HERE", cities)
        .replace("SIZES_HERE", sizes)
        .replace("DRAFTS_HERE", drafts)
        .replace("STARS_HERE", "".join(f'<span data-v="{v}">&#9734;</span>' for v in range(1, 6)))
        .replace("LOG_KEY_HERE", LOG_KEY)
    )
