"""The H3 held-out page (design 12 [v2.54]): seven widget shapes neither the development set nor
H1/H2 used, served from memory, logging what a person's trusted input did into localStorage under
`steer-h3`, read back over CDP by the suite. Code-owned ids only.

Shapes (the reviewers' H3 lists, widget-verbs and offscreen-lines packets, 2026-11-08; revised
after the H3 review of PR #175):
- n1: a checkbox the "server" confirms after 600 ms: it stays enabled and reads its old value
  until then (WV-03: a second toggle in that window double-flips it);
- n2: a mixed "Select all" over three children, judged on the final state, so ticking the
  children one by one passes too (WV-26);
- n3: two selects that both offer France; only the shipping one may change (WV-05);
- n4: a radio group;
- n5: an accordion button (`aria-expanded`) that shows the shipping answers;
- n6: "Show more comments" at the bottom of a 180 px scrolling comments box that is on screen;
- n7: a destinations listbox below the fold, its "Kyoto" option scrolled out of view inside it.

`window.PENDING` counts saves still in flight; the suite waits for 0 before reading.
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
COMMENTS = 12

PAGE = """<!doctype html>
<html lang="en">
<head><meta charset="utf-8"><title>Steer account centre</title>
<style>
  body { font-family: system-ui, sans-serif; margin: 12px; }
  section { border: 1px solid #ccc; padding: 8px; margin: 8px 0; }
  #comments-box { height: 180px; overflow: auto; border: 1px solid #999; }
  #dest { height: 140px; overflow: auto; border: 1px solid #999; padding: 0; margin: 0; }
  #dest li { list-style: none; padding: 4px 8px; cursor: default; }
  #dest li[aria-selected="true"] { background: #cde; }
</style></head>
<body>
<section aria-label="Email">
  <label><input type="checkbox" id="e-digest"> Weekly digest</label>
  <span id="e-status" role="status"></span>
</section>
<section aria-label="Notifications">
  <label><input type="checkbox" id="n-all"> Select all</label>
  <ul>
    <li><label><input type="checkbox" class="n-one" data-name="Mentions" checked>
      Mentions</label></li>
    <li><label><input type="checkbox" class="n-one" data-name="Replies"> Replies</label></li>
    <li><label><input type="checkbox" class="n-one" data-name="Follows"> Follows</label></li>
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
<section aria-label="Help">
  <button id="faq-ship-btn" aria-expanded="false" aria-controls="faq-ship">
    Shipping questions</button>
  <div id="faq-ship" hidden><p>Orders ship within two days.</p></div>
</section>
<section aria-label="Discussion">
  <div id="comments-box">
    <ul id="comments" aria-label="Comments">
COMMENTS_HERE
    </ul>
    <button id="more-comments">Show more comments</button>
  </div>
</section>
<div style="height:2400px"></div>
<section aria-label="Trips">
  <p id="dest-label">Destinations</p>
  <ul id="dest" role="listbox" aria-labelledby="dest-label">
DESTINATIONS_HERE
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
  var digest = document.getElementById("e-digest");
  digest.addEventListener("click", function (e) {
    var wanted = digest.checked;
    e.preventDefault();  // the box keeps its old value until the server answers
    window.PENDING += 1;
    document.getElementById("e-status").textContent = "Saving";
    setTimeout(function () {
      digest.checked = wanted;
      document.getElementById("e-status").textContent = "Saved";
      put(e, {kind: "digest", on: wanted});
      window.PENDING -= 1;
    }, 600);
  });
  var all = document.getElementById("n-all");
  all.indeterminate = true;
  function children() { return Array.from(document.querySelectorAll(".n-one")); }
  all.addEventListener("change", function (e) {
    for (var box of children()) { box.checked = all.checked; }
    put(e, {kind: "notifications"});
  });
  for (var child of children()) {
    child.addEventListener("change", function (e) {
      var on = children().filter(function (b) { return b.checked; }).length;
      all.checked = on === children().length;
      all.indeterminate = on > 0 && on < children().length;
      put(e, {kind: "notifications"});
    });
  }
  window.finalState = function () {
    var log = JSON.parse(localStorage.getItem(KEY) || "[]");
    var kinds = Array.from(new Set(log.map(function (x) { return x.kind; }))).sort();
    var state = {};
    for (var box of children()) { state[box.dataset.name] = box.checked; }
    return {kinds: kinds, state: state,
            untrusted: log.some(function (x) { return x.untrusted === true; })};
  };
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
  document.getElementById("faq-ship-btn").addEventListener("click", function (e) {
    var open = this.getAttribute("aria-expanded") !== "true";
    this.setAttribute("aria-expanded", open ? "true" : "false");
    document.getElementById("faq-ship").hidden = !open;
    put(e, {kind: "faq", name: "Shipping", open: open});
  });
  document.getElementById("more-comments").addEventListener("click", function (e) {
    var li = document.createElement("li"); li.textContent = "Comment " + (COMMENTS_COUNT + 1);
    document.getElementById("comments").appendChild(li);
    put(e, {kind: "more_comments"});
  });
  for (var item of document.querySelectorAll("#dest li")) {
    item.addEventListener("click", function (e) {
      for (var o of document.querySelectorAll("#dest li")) {
        o.setAttribute("aria-selected", o === this ? "true" : "false");
      }
      put(e, {kind: "destination", value: this.textContent});
    });
  }
})();
</script>
</body></html>
"""


def page_html() -> str:
    options = "\n".join(
        f'    <li role="option" id="d-{name.lower()}" aria-selected="false">{name}</li>'
        for name in DESTINATIONS
    )
    comments = "\n".join(f"      <li>Comment {i}</li>" for i in range(1, COMMENTS + 1))
    return (
        PAGE.replace("DESTINATIONS_HERE", options)
        .replace("COMMENTS_HERE", comments)
        .replace("COMMENTS_COUNT", str(COMMENTS))
        .replace("LOG_KEY_HERE", LOG_KEY)
    )
