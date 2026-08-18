// Boot static/app.js against captured API responses and assert the page filled in.
//
// Run it with tests/frontend/run.sh, from the repository root -- jsc resolves
// load() and readFile() against the working directory.
//
// Why this exists: the bug that motivated it was a blank dashboard where every
// endpoint answered 200. app.js threw on one undefined key from /api/meta, boot()
// aborted part-way, and the page kept its header and lost every panel. curl could
// not see that, and neither could a Python test. This runs the real app.js against
// real captured payloads and checks the resulting DOM, which is the only place that
// class of failure is visible.
"use strict";

const HERE = "tests/frontend/";
const STATIC = "src/web/static/";

load(HERE + "domshim.js");

// Fixtures are real responses, captured with curl against a fully loaded database
// (see fixtures/README.md for how to refresh them). Hand-written payloads would
// test app.js against my idea of the API rather than the API.
const fixtures = {
  "api/meta": JSON.parse(readFile(HERE + "fixtures/meta.json")),
  "api/overview": JSON.parse(readFile(HERE + "fixtures/overview.json")),
  "api/geography": JSON.parse(readFile(HERE + "fixtures/geography.json")),
  "api/quality": JSON.parse(readFile(HERE + "fixtures/quality.json")),
  "api/trips": JSON.parse(readFile(HERE + "fixtures/trips.json")),
  "api/trip/50": JSON.parse(readFile(HERE + "fixtures/trip50.json")),
  "api/pushdown": JSON.parse(readFile(HERE + "fixtures/pushdown.json")),
};

const requested = [];

// app.js reads its chart colours out of CSS custom properties, which the shim's
// getComputedStyle cannot compute. These are the values from styles.css.
const CSS_TOKENS = {
  "--series-1": "#2a78d6", "--series-2": "#eb6834",
  "--surface": "#ffffff", "--line": "#e3e6ea",
  "--seq-100": "#eaf1fb", "--seq-200": "#cfe0f6", "--seq-300": "#a9c8ee",
  "--seq-400": "#7dabe3", "--seq-500": "#4f8bd6", "--seq-600": "#2a6cc0",
  "--seq-700": "#1a4f92",
};

const { root } = __shim.install(readFile(STATIC + "index.html"), CSS_TOKENS);

// fetch() returns an already-settled promise: jsc has no event loop to pump, so
// every await in app.js resolves on the microtask queue that drain() flushes below.
globalThis.fetch = (path) => {
  const key = String(path).split("?")[0].replace(/^\/+/, "");
  requested.push(String(path));
  if (!(key in fixtures)) {
    return Promise.reject(new Error("shim: no fixture for " + key));
  }
  const body = JSON.stringify(fixtures[key]);
  return Promise.resolve({
    ok: true, status: 200, statusText: "OK",
    text: () => Promise.resolve(body),
  });
};

const failures = [];
function check(label, condition, detail) {
  if (condition) print("  ok   " + label);
  else { failures.push(label + (detail ? " -- " + detail : ""));
         print("  FAIL " + label + (detail ? " -- " + detail : "")); }
}

let uncaught = null;
try {
  load(STATIC + "app.js");
} catch (error) {
  uncaught = error;
}

// drainMicrotasks() is a jsc shell builtin. Without it boot()'s first await parks
// forever and every assertion below fails against an untouched page -- which looks
// exactly like the bug this harness is meant to catch, so it is worth being loud
// about: a total failure here is more likely the harness than app.js.
function drain() {
  for (let i = 0; i < 50; i += 1) {
    drainMicrotasks();
    const pending = globalThis.__deferred.splice(0);
    for (const fn of pending) { try { fn(); } catch (e) { uncaught = e; } }
    drainMicrotasks();
  }
}
drain();

print("\n=== boot ===");
check("app.js ran without throwing", !uncaught,
      uncaught && (uncaught.message + " @ " + (uncaught.line || "?")));
check("fetched /api/meta", requested.some((r) => r.includes("api/meta")));
check("fetched /api/overview", requested.some((r) => r.includes("api/overview")),
      "requested: " + requested.join(" "));

const text = (sel) => { const n = root.querySelector(sel);
                        return n ? n.textContent : "(no such node)"; };

print("\n=== header and footer ===");
check("summary shows the trip count", /787,060 trips/.test(text("#summary")),
      text("#summary"));
check("footer names the transport", /Embedded Python/.test(text("#footer-line")),
      text("#footer-line").slice(0, 60));

// The filter bar is four chip dropdowns built from /api/meta: 12 months, 7 days,
// 24 hours, 8 boroughs. A dropdown that comes back empty is the signature of a
// missing list in /api/meta -- the failure this harness exists for.
print("\n=== filter bar ===");
const dropdowns = root.querySelectorAll("#filter-bar .dd");
check("four filter dropdowns", dropdowns.length === 4, "got " + dropdowns.length);
check("payment is not a filter", !root.querySelector("#f-payment"));
const chipsOf = (i) => (dropdowns[i] || root).querySelectorAll(".chip");
const ddText = (i, sel) => { const n = (dropdowns[i] || root).querySelector(sel);
                             return n ? n.textContent : "(missing)"; };
for (const [i, name, want] of [[0, "month", 12], [1, "dow", 7], [2, "hour", 24],
                               [3, "borough", 8]]) {
  check(name + " has " + want + " chips", chipsOf(i).length === want,
        "got " + chipsOf(i).length);
}
for (const [sel, want] of [["#trip-rule", 17], ["#pushdown-case", 3]]) {
  const node = root.querySelector(sel);
  const n = node ? node.options.length : -1;
  check(sel + " has " + want + " options", n === want, "got " + n);
}

// A closed dropdown has to report its own selection, so the summary is asserted
// alongside the state it summarises.
(dropdowns[0] || root).querySelector(".dd-toggle").dispatch("click");
chipsOf(0)[2].dispatch("click");
check("chip records its selection",
      chipsOf(0)[2].getAttribute("aria-pressed") === "true");
check("month button summarises one pick", /Mar/.test(ddText(0, ".dd-value")),
      ddText(0, ".dd-value"));

const dayQuick = (dropdowns[1] || root).querySelectorAll(".dd-quick button");
check("day dropdown offers All, None, Weekdays, Weekend", dayQuick.length === 4,
      "got " + dayQuick.length);
dayQuick[2].dispatch("click");                       // Weekdays
check("a run of days reads as a range", /Mon – Fri/.test(ddText(1, ".dd-value")),
      ddText(1, ".dd-value"));

root.querySelector("#filters").dispatch("submit");
drain();
check("apply sends the selection to IRIS",
      requested.some((r) => r.includes("api/overview?month=3&dow=1")),
      requested.join(" "));
check("apply goes quiet once applied",
      root.querySelector("#filters-apply").disabled === true);

root.querySelector("#filters-clear").dispatch("click");
drain();
check("clear empties every dropdown",
      ddText(0, ".dd-value") === "All" && ddText(1, ".dd-value") === "All",
      ddText(0, ".dd-value") + " / " + ddText(1, ".dd-value"));
check("clear refetched unfiltered",
      requested[requested.length - 1] === "api/overview",
      requested[requested.length - 1]);

print("\n=== overview panels ===");
const kpis = root.querySelector("#kpis");
check("#kpis has 5 tiles", kpis && kpis.children.length === 5,
      kpis ? String(kpis.children.length) : "missing");
check("KPI text includes a fare", /\$/.test(text("#kpis")), text("#kpis").slice(0, 80));
for (const sel of ["#chart-monthly", "#chart-hourly", "#chart-heatmap"]) {
  const node = root.querySelector(sel);
  const svg = node && node.querySelector("svg");
  const marks = svg ? svg.querySelectorAll(".mark").length : 0;
  const paths = svg ? svg.querySelectorAll("path").length : 0;
  check(sel + " drew an svg", !!svg,
        node ? "content: " + node.textContent.slice(0, 60) : "missing node");
  check(sel + " has marks", marks + paths > 0, "marks=" + marks + " paths=" + paths);
}
check("heatmap has 168 cells",
      (root.querySelector("#chart-heatmap") || root)
        .querySelectorAll(".cell").length === 168,
      String((root.querySelector("#chart-heatmap") || root)
        .querySelectorAll(".cell").length));

print("\n=== tab switching ===");
const tabs = root.querySelectorAll(".tabs [role=tab]");
check("five tab buttons", tabs.length === 5, String(tabs.length));

function clickTab(name) {
  const button = tabs.find((t) => t.dataset.tab === name);
  if (!button) { failures.push("no tab button " + name); return; }
  button.dispatch("click");
  drain();
}

clickTab("geography");
check("geography fetched", requested.some((r) => r.includes("api/geography")));
check("#chart-boroughs drew bars",
      (root.querySelector("#chart-boroughs") || root)
        .querySelectorAll(".mark").length > 0);
check("#table-payments has rows",
      (root.querySelector("#table-payments") || root)
        .querySelectorAll("tbody tr").length > 0,
      String((root.querySelector("#table-payments") || root)
        .querySelectorAll("tbody tr").length));
check("#table-routes has rows",
      (root.querySelector("#table-routes") || root)
        .querySelectorAll("tbody tr").length > 0);

clickTab("quality");
check("quality fetched", requested.some((r) => r.includes("api/quality")));
check("#table-policies has 3 rows",
      (root.querySelector("#table-policies") || root)
        .querySelectorAll("tbody tr").length === 3,
      String((root.querySelector("#table-policies") || root)
        .querySelectorAll("tbody tr").length));
check("#chart-rules drew grouped bars",
      (root.querySelector("#chart-rules") || root)
        .querySelectorAll(".mark").length > 0);

clickTab("trips");
check("trips fetched", requested.some((r) => r.includes("api/trips")));
const tripRows = (root.querySelector("#trip-table") || root)
  .querySelectorAll("tbody tr").length;
check("#trip-table has 50 rows", tripRows === 50, String(tripRows));
check("#trip-position reports the page",
      /Page 1 of/.test(text("#trip-position")), text("#trip-position"));

// The push-down tab is deliberately inert until asked: its pulled half moves every
// matching trip into Python, which is the measurement, not a page-load side effect.
clickTab("pushdown");
check("pushdown NOT fetched on tab open",
      !requested.some((r) => r.includes("api/pushdown")));
root.querySelector("#pushdown-run").dispatch("click");
drain();
check("pushdown fetched on demand",
      requested.some((r) => r.includes("api/pushdown")));
check("#chart-pushdown drew bars",
      (root.querySelector("#chart-pushdown") || root)
        .querySelectorAll(".mark").length > 0,
      (root.querySelector("#pushdown-status") || root).textContent.slice(0, 120));
check("#table-pushdown has rows",
      (root.querySelector("#table-pushdown") || root)
        .querySelectorAll("tbody tr").length > 0);

print("\n=== result ===");
if (failures.length) {
  print(failures.length + " failure(s):");
  for (const f of failures) print("  - " + f);
  print("FAILED");                 // run.sh greps for this to set an exit status
} else {
  print("all checks passed");
}
