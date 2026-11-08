"""The H3 held-out page (design 12 [v2.53]): seven widget shapes neither the development set nor
H1/H2 used, served from memory, each logging what a person's trusted input did into
localStorage under `steer-h3`, read back over CDP by the suite. Code-owned ids only.

Shapes (the reviewers' H3 list, widget-verbs and offscreen-lines packets, 2026-11-08):
- a checkbox the "server" confirms after 600 ms (WV-03);
- a mixed "Select all" checkbox over three children (WV-26);
- two selects that both offer France, only the shipping one may change (WV-05);
- a radio group; a tablist with three tabs;
- a "Show more comments" button below a long article (offscreen, below the fold);
- a destinations listbox scrolled inside its own 140 px box, the wanted option out of view.
"""

from __future__ import annotations

LOG_KEY = "steer-h3"
DESTINATIONS = (
    "Accra",
    "Agra",
    "Amman",
    "Baku",
    "Bergen",
    "Bogota",
    "Cairo",
    "Cusco",
    "Delhi",
    "Dubrovnik",
    "Fez",
    "Hanoi",
    "Havana",
    "Hobart",
    "Jaipur",
    "Kandy",
    "Krakow",
    "Kyoto",
    "Lima",
    "Lisbon",
)

PAGE = """<!doctype html>
<html lang="en">
<head><meta charset="utf-8"><title>Steer account centre</title>
<style>
  body { font-family: system-ui, sans-serif; margin: 12px; }
  section { border: 1px solid #ccc; padding: 8px; margin: 8px 0; }
  #dest { height: 140px; overflow: auto; border: 1px solid #999; padding: 0; margin: 0; }
  #dest li { list-style: none; padding: 4px 8px; cursor: default; }
  #dest li[aria-selected="true"] { background: #cde; }
  .hidden { display: none; }
</style></head>
<body>
<section aria-label="Email">
  <label><input type="checkbox" id="e-digest"> Weekly digest</label>
  <span id="e-status" role="status"></span>
</section>
<section aria-label="Notifications">
  <label><input type="checkbox" id="n-all"> Select all</label>
  <ul>
    <li><label><input type="checkbox" class="n-one" checked> Mentions</label></li>
    <li><label><input type="checkbox" class="n-one"> Replies</label></li>
    <li><label><input type="checkbox" class="n-one"> Follows</label></li>
  </ul>
</section>
<section aria-label="Addresses">
  <label>Shipping country <select id="a-ship">
    <option>Germany</option><option>France</option><option>Spain</option></select></label>
  <label>Billing country <select id="a-bill">
    <option>Germany</option><option>France</option><option>Spain</option></select></label>
</section>
<section aria-label="Plan">
  <fieldset><legend>Billing period</legend>
    <label><input type="radio" name="period" id="p-month" checked> Monthly</label>
    <label><input type="radio" name="period" id="p-year"> Annual</label>
  </fieldset>
</section>
<section aria-label="Account settings">
  <div role="tablist" aria-label="Settings tabs">
    <button role="tab" id="t-account" aria-selected="true">Account</button>
    <button role="tab" id="t-security" aria-selected="false">Security</button>
    <button role="tab" id="t-privacy" aria-selected="false">Privacy</button>
  </div>
  <div id="t-panel" role="tabpanel">Account details</div>
</section>
<section aria-label="Trips">
  <p id="dest-label">Destinations</p>
  <ul id="dest" role="listbox" aria-labelledby="dest-label">
DESTINATIONS_HERE
  </ul>
</section>
<article aria-label="Story">
  <h2>Release notes</h2>
  <p>Long article text.</p>
  <div style="height:2600px"></div>
  <ul id="comments" aria-label="Comments"><li>First comment</li></ul>
  <button id="more-comments">Show more comments</button>
</article>
<script>
(function () {
  var KEY = "LOG_KEY_HERE";
  function put(e, entry) {
    if (!e.isTrusted) { entry.untrusted = true; }
    var log = JSON.parse(localStorage.getItem(KEY) || "[]");
    log.push(entry);
    localStorage.setItem(KEY, JSON.stringify(log));
  }
  var digest = document.getElementById("e-digest");
  digest.addEventListener("change", function (e) {
    var wanted = digest.checked;
    digest.disabled = true;
    document.getElementById("e-status").textContent = "Saving";
    setTimeout(function () {
      digest.disabled = false;
      document.getElementById("e-status").textContent = "Saved";
      put(e, {kind: "digest", on: wanted});
    }, 600);
  });
  var all = document.getElementById("n-all");
  all.indeterminate = true;
  all.addEventListener("change", function (e) {
    for (var box of document.querySelectorAll(".n-one")) { box.checked = all.checked; }
    put(e, {kind: "select_all", on: all.checked});
  });
  document.getElementById("a-ship").addEventListener("change", function (e) {
    put(e, {kind: "shipping", value: e.target.value});
  });
  document.getElementById("a-bill").addEventListener("change", function (e) {
    put(e, {kind: "billing", value: e.target.value});
  });
  for (var id of ["p-month", "p-year"]) {
    document.getElementById(id).addEventListener("change", function (e) {
      put(e, {kind: "period", value: e.target.id === "p-year" ? "Annual" : "Monthly"});
    });
  }
  for (var tab of document.querySelectorAll("[role=tab]")) {
    tab.addEventListener("click", function (e) {
      for (var t of document.querySelectorAll("[role=tab]")) {
        t.setAttribute("aria-selected", t === this ? "true" : "false");
      }
      document.getElementById("t-panel").textContent = this.textContent + " details";
      put(e, {kind: "tab", name: this.textContent});
    });
  }
  for (var item of document.querySelectorAll("#dest li")) {
    item.addEventListener("click", function (e) {
      for (var o of document.querySelectorAll("#dest li")) {
        o.setAttribute("aria-selected", o === this ? "true" : "false");
      }
      put(e, {kind: "destination", value: this.textContent});
    });
  }
  document.getElementById("more-comments").addEventListener("click", function (e) {
    var li = document.createElement("li"); li.textContent = "Second comment";
    document.getElementById("comments").appendChild(li);
    put(e, {kind: "more_comments"});
  });
})();
</script>
</body></html>
"""


def page_html() -> str:
    options = "\n".join(
        f'    <li role="option" id="d-{name.lower()}" aria-selected="false">{name}</li>'
        for name in DESTINATIONS
    )
    return PAGE.replace("DESTINATIONS_HERE", options).replace("LOG_KEY_HERE", LOG_KEY)
