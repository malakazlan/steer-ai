"""The H2 held-out page (design 12 [v2.45]): eight small widgets the development set never used,
served from memory, each logging what a person's trusted input did into localStorage under
`steer-h2`, read back over CDP by the suite. Code-owned ids only, never page text."""

from __future__ import annotations

LOG_KEY = "steer-h2"

PAGE = """<!doctype html>
<html lang="en">
<head><meta charset="utf-8"><title>Steer probe workspace</title>
<style>
  body { font-family: system-ui, sans-serif; margin: 12px; }
  section { border: 1px solid #ccc; padding: 8px; margin: 8px 0; }
  .hidden { display: none; }
  #more-list li { padding: 4px 0; }
</style></head>
<body>
<nav aria-label="Menu bar">
  <button id="menu-file" aria-haspopup="true">File</button>
  <button id="menu-help" aria-haspopup="true">Help</button>
  <div id="help-items" class="hidden" role="menu" aria-label="Help">
    <button role="menuitem" id="help-docs">Documentation</button>
    <button role="menuitem" id="help-about">About</button>
  </div>
  <div id="about" class="hidden" role="dialog" aria-label="About">Steer probe workspace 1.0</div>
</nav>
<section aria-label="Contact">
  <h2>Contact us</h2>
  <label>Name <input id="f-name" name="name"></label>
  <label>Email <input id="f-email" name="email"></label>
  <button id="f-send">Send message</button>
</section>
<section aria-label="Settings">
  <h2>Settings</h2>
  <label><input type="checkbox" id="s-dark"> Dark mode</label>
  <label>Country <select id="s-country">
    <option value="">Choose a country</option>
    <option>Germany</option><option>Pakistan</option><option>Peru</option>
  </select></label>
</section>
<section aria-label="Account">
  <div role="tablist" aria-label="Account tabs">
    <button role="tab" id="tab-profile" aria-selected="true">Profile</button>
    <button role="tab" id="tab-billing" aria-selected="false">Billing</button>
  </div>
  <div id="panel" role="tabpanel">Profile details</div>
</section>
<section aria-label="Todo">
  <h2>Todo</h2>
  <input id="t-new" aria-label="New task">
  <ul id="t-list" aria-label="Tasks">
    <li>call mom <button data-del="call mom">Delete call mom</button></li>
    <li>pay rent <button data-del="pay rent">Delete pay rent</button></li>
  </ul>
</section>
<section aria-label="Feed">
  <ul id="more-list" aria-label="Feed items"></ul>
  <button id="more">Load more</button>
</section>
<script>
(function () {
  var KEY = "LOG_KEY_HERE";
  function put(e, entry) {
    if (!e.isTrusted) { entry.untrusted = true; }
    var log = JSON.parse(localStorage.getItem(KEY) || "[]");
    log.push(entry);
    localStorage.setItem(KEY, JSON.stringify(log));
  }
  var feed = document.getElementById("more-list");
  for (var i = 0; i < 80; i++) {
    var li = document.createElement("li"); li.textContent = "Feed item " + i; feed.appendChild(li);
  }
  document.getElementById("menu-help").addEventListener("click", function () {
    document.getElementById("help-items").classList.toggle("hidden");
  });
  document.getElementById("help-about").addEventListener("click", function (e) {
    document.getElementById("about").classList.remove("hidden");
    put(e, {kind: "about"});
  });
  document.getElementById("f-send").addEventListener("click", function (e) {
    put(e, {kind: "contact", name: document.getElementById("f-name").value,
             email: document.getElementById("f-email").value});
  });
  document.getElementById("s-dark").addEventListener("change", function (e) {
    put(e, {kind: "dark", on: e.target.checked});
  });
  document.getElementById("s-country").addEventListener("change", function (e) {
    put(e, {kind: "country", value: e.target.value});
  });
  document.getElementById("tab-billing").addEventListener("click", function (e) {
    this.setAttribute("aria-selected", "true");
    document.getElementById("tab-profile").setAttribute("aria-selected", "false");
    document.getElementById("panel").textContent = "Billing details";
    put(e, {kind: "tab", name: "Billing"});
  });
  document.getElementById("t-new").addEventListener("keydown", function (e) {
    if (e.key !== "Enter" || !this.value.trim()) { return; }
    var li = document.createElement("li"); li.textContent = this.value;
    document.getElementById("t-list").appendChild(li);
    put(e, {kind: "todo_add", text: this.value.trim()});
    this.value = "";
  });
  document.getElementById("t-list").addEventListener("click", function (e) {
    var b = e.target.closest("button[data-del]");
    if (!b) { return; }
    b.parentElement.remove();
    put(e, {kind: "todo_delete", text: b.dataset.del});
  });
  document.getElementById("more").addEventListener("click", function (e) {
    put(e, {kind: "load_more"});
  });
})();
</script>
</body></html>
""".replace("LOG_KEY_HERE", LOG_KEY)


def page_html() -> str:
    return PAGE
