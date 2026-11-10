"""The H4 held-out page (packet h4-2026-11-10): five widget shapes on one page, served from
memory, logging what a person's trusted input did into localStorage under `steer-h4`, read
back over CDP by the suite. Code-owned ids only.

Each shape is written in an idiom that `widget_patterns.py`, the #169 fallback fixture and the
H2/H3 pages do not use, on purpose (the principal's review of the packet): a pass must come from
the roles and patterns Chrome exposes, not from markup Steer was tuned on.
- q1: a native `<details>` whose `<summary>` reads "Returns policy" (H3's n5 was an
  `aria-expanded` button). A `toggle` event is never trusted on its own, so the page remembers
  the trusted click on the summary and logs the open state the toggle reports;
- q2: a native `<select size="8">` named "Languages" with 24 options, "Swahili" the 20th, so it
  is scrolled out of view inside a box that is itself on screen (H3's n7 was an ARIA listbox
  below the fold; the fixture's `<select size="5">` has three options and nothing to scroll);
- q3: dependent selects "Country" then "City": choosing a country fills City with that
  country's cities and resets it to "Choose..."; only the city change is logged, with the
  country it was chosen under; `window.finalState()` reports both values;
- q4: a tablist of `<a role="tab" href="#...">` inside a `<ul role="tablist">` with
  `aria-selected` and panels (H2's b1 and the fixture use `<button role="tab">`);
- q5: a consent checkbox "I agree to the terms" inside a fieldset "Sign-up".

`window.PENDING` counts saves still in flight; every H4 save is synchronous, so it stays 0 and
the suite's wait returns at once.
"""

from __future__ import annotations

import json

LOG_KEY = "steer-h4"
POLICY_TEXT = "Returns are accepted within 30 days."
LANGUAGES = (
    "Amharic",
    "Arabic",
    "Bengali",
    "Czech",
    "Danish",
    "Dutch",
    "Finnish",
    "French",
    "German",
    "Greek",
    "Hindi",
    "Hungarian",
    "Italian",
    "Japanese",
    "Korean",
    "Malay",
    "Polish",
    "Portuguese",
    "Romanian",
    "Swahili",
    "Tamil",
    "Thai",
    "Turkish",
    "Vietnamese",
)
CITIES: dict[str, tuple[str, ...]] = {
    "Germany": ("Berlin", "Hamburg", "Munich"),
    "Japan": ("Kyoto", "Osaka", "Sapporo", "Tokyo"),
    "Spain": ("Barcelona", "Madrid", "Seville"),
}
"""One city per first letter within a country, so a typed letter picks one."""
TABS = ("Account", "Security", "Billing")

PAGE = """<!doctype html>
<html lang="en">
<head><meta charset="utf-8"><title>Steer shop settings</title>
<style>
  body { font-family: system-ui, sans-serif; margin: 12px; }
  section { border: 1px solid #ccc; padding: 8px; margin: 8px 0; }
  #lang { width: 12em; }
  ul[role="tablist"] { list-style: none; padding: 0; margin: 0 0 8px; display: flex; gap: 8px; }
  a[role="tab"] { padding: 4px 8px; border: 1px solid #999; text-decoration: none; color: #000; }
  a[role="tab"][aria-selected="true"] { background: #cde; }
</style></head>
<body>
<section aria-label="Preferences">
  <label>Languages
    <select id="lang" size="8">
LANGUAGES_HERE
    </select></label>
</section>
<section aria-label="Help">
  <details id="policy">
    <summary id="policy-summary">Returns policy</summary>
    <p>POLICY_TEXT_HERE</p>
    <p>Refunds reach the original card within five working days.</p>
  </details>
</section>
<section aria-label="Delivery">
  <label>Country <select id="country">
    <option value="">Choose&hellip;</option>
COUNTRIES_HERE
  </select></label>
  <label>City <select id="city">
    <option value="">Choose&hellip;</option>
  </select></label>
</section>
<section aria-label="Settings">
  <ul role="tablist" aria-label="Settings tabs">
TABS_HERE
  </ul>
PANELS_HERE
</section>
<section aria-label="Sign-up">
  <fieldset><legend>Sign-up</legend>
    <label><input type="checkbox" id="consent"> I agree to the terms</label>
    <span id="consent-status" role="status"></span>
  </fieldset>
</section>
<script>
(function () {
  var KEY = "LOG_KEY_HERE";
  var CITIES = CITIES_HERE;
  window.PENDING = 0;
  function put(e, entry) {
    if (!e.isTrusted) { entry.untrusted = true; }
    var log = JSON.parse(localStorage.getItem(KEY) || "[]");
    log.push(entry);
    localStorage.setItem(KEY, JSON.stringify(log));
  }
  document.getElementById("lang").addEventListener("change", function (e) {
    put(e, {kind: "language", value: e.target.value});
  });
  var policy = document.getElementById("policy");
  var policyClickTrusted = false;
  document.getElementById("policy-summary").addEventListener("click", function (e) {
    policyClickTrusted = e.isTrusted;
  });
  policy.addEventListener("toggle", function () {
    // a toggle carries no isTrusted of its own: the summary click before it does
    put({isTrusted: policyClickTrusted}, {kind: "policy", open: policy.open});
    policyClickTrusted = false;
  });
  var country = document.getElementById("country");
  var city = document.getElementById("city");
  country.addEventListener("change", function () {
    city.textContent = "";
    var names = [""].concat(CITIES[country.value] || []);
    for (var name of names) {
      var option = document.createElement("option");
      option.value = name;
      option.textContent = name || "Choose" + String.fromCharCode(8230);
      city.appendChild(option);
    }
    city.value = "";
  });
  city.addEventListener("change", function (e) {
    put(e, {kind: "city", country: country.value, value: city.value});
  });
  window.finalState = function () {
    var log = JSON.parse(localStorage.getItem(KEY) || "[]");
    var kinds = Array.from(new Set(log.map(function (x) { return x.kind; }))).sort();
    return {kinds: kinds, state: {country: country.value, city: city.value},
            untrusted: log.some(function (x) { return x.untrusted === true; })};
  };
  for (var tab of document.querySelectorAll("a[role=tab]")) {
    tab.addEventListener("click", function (e) {
      e.preventDefault();
      for (var t of document.querySelectorAll("a[role=tab]")) {
        var on = t === this;
        t.setAttribute("aria-selected", on ? "true" : "false");
        document.getElementById(t.getAttribute("aria-controls")).hidden = !on;
      }
      put(e, {kind: "tab", name: this.textContent});
    });
  }
  var consent = document.getElementById("consent");
  consent.addEventListener("change", function (e) {
    document.getElementById("consent-status").textContent = consent.checked ? "Agreed" : "";
    put(e, {kind: "consent", on: consent.checked});
  });
})();
</script>
</body></html>
"""


def page_html() -> str:
    languages = "\n".join(
        f'      <option id="l-{name.lower()}">{name}</option>' for name in LANGUAGES
    )
    countries = "\n".join(f"    <option>{name}</option>" for name in CITIES)
    tabs = "\n".join(
        f'    <li><a role="tab" id="t-{name.lower()}" href="#panel-{name.lower()}"'
        f' aria-controls="panel-{name.lower()}"'
        f' aria-selected="{"true" if i == 0 else "false"}">{name}</a></li>'
        for i, name in enumerate(TABS)
    )
    panels = "\n".join(
        f'  <div role="tabpanel" id="panel-{name.lower()}"{"" if i == 0 else " hidden"}>'
        f"{name} settings</div>"
        for i, name in enumerate(TABS)
    )
    cities = json.dumps(CITIES, sort_keys=True)
    return (
        PAGE.replace("LANGUAGES_HERE", languages)
        .replace("COUNTRIES_HERE", countries)
        .replace("TABS_HERE", tabs)
        .replace("PANELS_HERE", panels)
        .replace("CITIES_HERE", cities)
        .replace("POLICY_TEXT_HERE", POLICY_TEXT)
        .replace("LOG_KEY_HERE", LOG_KEY)
    )
