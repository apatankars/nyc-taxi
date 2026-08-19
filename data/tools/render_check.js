/* Runs the dashboard's render code outside a browser, to catch what a glance
 * cannot: runtime errors in every panel, NaN geometry, marks escaping the
 * viewBox, and element IDs referenced by app.js that index.html does not define.
 *
 * Run it with macOS's built-in JavaScriptCore -- there is no Node on this
 * machine, and this needs no dependencies either way:
 *
 *     osascript -l JavaScript tools/render_check.js
 *
 * It is a shim, not a browser: it verifies the code paths execute and the
 * numbers they produce are sane. It says nothing about whether labels collide.
 */

ObjC.import("Foundation");

(function () {
  const ROOT = $.NSFileManager.defaultManager.currentDirectoryPath.js;

  function read(path) {
    const s = $.NSString.stringWithContentsOfFileEncodingError(
      ROOT + "/" + path, $.NSUTF8StringEncoding, null
    );
    if (!s.js) throw new Error("cannot read " + path);
    return s.js;
  }

  const problems = [];
  const notes = [];
  const fail = (msg) => problems.push(msg);

  /* ------------------------------------------------------------- DOM shim */

  const ALL_NODES = [];

  function makeNode(tag, ns) {
    const node = {
      tagName: String(tag).toUpperCase(),
      localName: tag,
      namespace: ns || null,
      // A real element always has a numeric clientWidth. Defaulting it here
      // matters: mountChart's `width < 40` guard is meant to catch a hidden
      // tab's 0, and `undefined < 40` is false, so leaving it unset would push
      // NaN geometry through every chart and look like an app bug.
      clientWidth: 620,
      attributes: {},
      children: [],
      _text: "",
      style: {},
      dataset: {},
      classList: {
        _set: new Set(),
        add(...c) { c.forEach((x) => this._set.add(x)); },
        remove(...c) { c.forEach((x) => this._set.delete(x)); },
        contains(c) { return this._set.has(c); },
      },
      _listeners: {},
      setAttribute(k, v) { this.attributes[k] = String(v); },
      getAttribute(k) { return this.attributes[k] ?? null; },
      removeAttribute(k) { delete this.attributes[k]; },
      addEventListener(type, fn) { (this._listeners[type] ||= []).push(fn); },
      append(...kids) {
        for (const k of kids) if (k) { this.children.push(k); k.parentNode = this; }
      },
      replaceChildren(...kids) { this.children = []; this.append(...kids); },
      get firstChild() { return this.children[0] ?? null; },
      get lastChild() { return this.children[this.children.length - 1] ?? null; },
      get childElementCount() { return this.children.length; },
      get textContent() { return this._text; },
      set textContent(v) { this._text = String(v); },
      set className(v) { this.attributes.class = String(v); },
      get className() { return this.attributes.class || ""; },
      set innerHTML(v) { this.attributes._html = String(v); },
      set hidden(v) { this.attributes.hidden = v ? "true" : null; },
      get hidden() { return !!this.attributes.hidden; },
      set tabIndex(v) { this.attributes.tabindex = String(v); },
      set title(v) { this.attributes.title = String(v); },
      // Cheap descendant match -- enough for the two selectors app.js uses.
      querySelectorAll(sel) {
        const want = sel.replace(/^[a-z]*\./, "").split(".").pop().trim();
        const out = [];
        const walk = (n) => {
          for (const c of n.children) {
            const cls = (c.attributes.class || "").split(/\s+/);
            const tagMatch = sel.startsWith(c.localName + ".") || sel === c.localName;
            if (cls.includes(want) || (tagMatch && cls.includes(want))) out.push(c);
            walk(c);
          }
        };
        walk(this);
        return out;
      },
      closest() { return null; },
    };
    ALL_NODES.push(node);
    return node;
  }

  // Element ids index.html declares -- app.js must not ask for anything else.
  const html = read("src/taxi/static/index.html");
  const declaredIds = new Set([...html.matchAll(/\bid="([^"]+)"/g)].map((m) => m[1]));
  const requestedIds = new Set();
  const idNodes = {};

  const document = {
    body: makeNode("body"),
    documentElement: makeNode("html"),
    createElement: (t) => makeNode(t, null),
    createElementNS: (ns, t) => makeNode(t, ns),
    getElementById(id) {
      requestedIds.add(id);
      if (!idNodes[id]) {
        idNodes[id] = makeNode("div");
        idNodes[id].attributes.id = id;
        // Checkbox controls: the panels read .checked off them.
        idNodes[id].checked = true;
        // Select controls: the row viewer reads .value off them, and a shim
        // value of "" would silently exercise a selector the app never sends.
        idNodes[id].value = { "browse-rule": "flagged", "browse-limit": "25" }[id] || "";
      }
      return idNodes[id];
    },
    querySelectorAll(sel) {
      if (sel === "nav.tabs button") {
        return ["overview", "quality", "zones", "time", "pairs", "browse", "bench"].map((t) => {
          const n = makeNode("button");
          n.dataset.tab = t;
          return n;
        });
      }
      return [];
    },
    addEventListener() {},
  };
  document.documentElement.dataset = {};
  document.body.dataset = {};

  /* ------------------------------------------------------- other globals */

  const PAYLOADS = {
    "/api/overview": "overview",
    "/api/quality": "quality",
    "/api/tipping": "tipping",
  };

  function payloadFor(url) {
    if (PAYLOADS[url]) return JSON.parse(read("tools/fixtures/" + PAYLOADS[url] + ".json"));
    if (url.startsWith("/api/zones")) return JSON.parse(read("tools/fixtures/zones.json"));
    if (url.startsWith("/api/time")) return JSON.parse(read("tools/fixtures/time.json"));
    if (url.startsWith("/api/pairs")) return JSON.parse(read("tools/fixtures/pairs.json"));
    if (url.startsWith("/api/rows")) return JSON.parse(read("tools/fixtures/rows.json"));
    if (url.startsWith("/api/quality/sample")) return JSON.parse(read("tools/fixtures/sample.json"));
    if (url.startsWith("/api/bench")) return JSON.parse(read("tools/fixtures/bench.json"));
    if (url.startsWith("/api/health")) {
      return { ok: true, version: "shim", target: "shim", tables: [{ table: "Taxi.Trip", rows: 787060 }] };
    }
    throw new Error("harness has no payload for " + url);
  }

  const sandbox = {
    document,
    // Charts render at a realistic width; the shim reports it via clientWidth.
    ResizeObserver: function () { this.observe = function () {}; },
    requestAnimationFrame: (fn) => fn(),
    fetch: (url) => {
      const body = payloadFor(url);
      return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(body) });
    },
    localStorage: { getItem: () => null, setItem: () => {} },
    matchMedia: () => ({ matches: false, addEventListener() {} }),
    location: { search: "", hash: "" },
    history: { replaceState() {} },
    window: { innerWidth: 1280, innerHeight: 900 },
    console: { log: () => {}, warn: () => {}, error: () => {} },
  };

  /* --------------------------------------------------------- load app.js */

  const src = read("src/taxi/static/app.js");
  const names = Object.keys(sandbox);
  const exposed = [
    "renderOverview", "renderQuality", "renderZones", "renderTime",
    "renderPairs", "renderBrowse", "renderBench", "columnChart", "lineChart", "heatmap",
    "rankedBars", "niceTicks", "barPathUp", "fmtCompact", "dataTable", "init",
  ];
  const loader = new Function(
    ...names,
    src.replace(/^"use strict";/, "") +
      "\n;return {" + exposed.map((n) => `${n}: typeof ${n} === "function" ? ${n} : null`).join(",") + "};"
  );
  const app = loader(...names.map((n) => sandbox[n]));

  /* ------------------------------------------------------------ geometry */

  function checkGeometry(label, root) {
    let marks = 0;
    const walk = (n) => {
      const a = n.attributes;
      for (const [k, v] of Object.entries(a)) {
        if (["x", "y", "x1", "x2", "y1", "y2", "cx", "cy", "r", "width", "height"].includes(k)) {
          if (!Number.isFinite(Number(v))) fail(`${label}: ${n.localName}[${k}] is not finite (${v})`);
          else if (Number(v) < -1) fail(`${label}: ${n.localName}[${k}] is negative (${v})`);
        }
        if (k === "d" && /NaN|undefined/.test(v)) fail(`${label}: path d contains NaN/undefined`);
        if (k === "points" && /NaN|undefined/.test(v)) fail(`${label}: polyline points contain NaN/undefined`);
        if (k === "fill" && /NaN|undefined/.test(v)) fail(`${label}: fill is ${v}`);
      }
      if ((a.class || "").includes("mark")) marks++;
      n.children.forEach(walk);
    };
    walk(root);
    if (marks === 0) fail(`${label}: rendered no marks`);
    return marks;
  }

  function countNodes(root) {
    let n = 0;
    const walk = (x) => { n++; x.children.forEach(walk); };
    walk(root);
    return n;
  }

  /* ------------------------------------------------------------ checks 1 */

  // niceTicks must always be ascending, start at 0 and cover the max.
  for (const max of [0, 1, 7, 8041, 787060, 0.79, 278990.28, 24.7]) {
    const t = app.niceTicks(max);
    if (t[0] !== 0) fail(`niceTicks(${max}) does not start at 0: ${t}`);
    if (t[t.length - 1] < max) fail(`niceTicks(${max}) tops out below the max: ${t}`);
    for (let i = 1; i < t.length; i++) {
      if (!(t[i] > t[i - 1])) fail(`niceTicks(${max}) is not ascending: ${t}`);
    }
    if (t.length > 12) fail(`niceTicks(${max}) produced ${t.length} ticks`);
  }

  // A zero-height bar must still produce a valid path.
  for (const [x, y, w, h] of [[0, 100, 20, 0], [10, 5, 24, 95], [0, 0, 1, 0.5]]) {
    const d = app.barPathUp(x, y, w, h);
    if (/NaN|undefined/.test(d)) fail(`barPathUp(${x},${y},${w},${h}) -> ${d}`);
  }

  if (app.fmtCompact(787060) !== "787.1k") notes.push("fmtCompact(787060) = " + app.fmtCompact(787060));

  /* ------------------------------------------------------------ checks 2 */

  const PANELS = [
    ["overview", app.renderOverview],
    ["quality", app.renderQuality],
    ["zones", app.renderZones],
    ["time", app.renderTime],
    ["pairs", app.renderPairs],
    ["browse", app.renderBrowse],
    ["bench", app.renderBench],
  ];

  function checkGeometryTree(label, host) {
    const walk = (n) => {
      if (n.namespace === "http://www.w3.org/2000/svg" && n.localName === "svg") {
        checkGeometry(label, n);
      }
      n.children.forEach(walk);
    };
    walk(host);
  }

  function out(text) {
    $.NSFileHandle.fileHandleWithStandardOutput.writeData(
      $.NSString.stringWithString(text + "\n").dataUsingEncoding($.NSUTF8StringEncoding)
    );
  }

  const results = [];
  const runs = PANELS.map(([name, render]) => {
    const host = makeNode("div");
    host.clientWidth = 620;
    try {
      return Promise.resolve(render(host)).then(
        () => results.push([name, host]),
        (e) => fail(`${name}: rejected -> ${e.message}\n${(e.stack || "").split("\n").slice(0, 4).join("\n")}`)
      );
    } catch (e) {
      fail(`${name}: threw synchronously -> ${e.message}`);
      return Promise.resolve();
    }
  });

  /* ------------------------------------------------------------ report */

  // The report runs in a microtask so the panels' awaits have settled first.
  // JavaScriptCore drains microtasks after this script's synchronous body
  // returns, so nothing here may depend on running before that point.
  Promise.all(runs).then(() => {
    const lines = [];
    lines.push("Render check — " + PANELS.length + " panels, real API payloads, DOM shim");
    lines.push("=".repeat(72));

    for (const [name, host] of results) {
      checkGeometryTree(name, host);
      // Ranked bars are HTML, not SVG, and their marks carry class "bar" rather
      // than "mark" -- a panel built only from them is still a panel of charts.
      let marks = 0, svgs = 0, tables = 0, bars = 0, tested = 0, source = 0;
      const walk = (x) => {
        const cls = x.attributes.class || "";
        if (cls.includes("mark")) marks++;
        if (cls.split(/\s+/).includes("bar")) bars++;
        if (cls.split(/\s+/).includes("tested")) tested++;
        if (x.localName === "svg") svgs++;
        if (x.localName === "table") tables++;
        if (x.localName === "pre") source++;
        // The panels catch their own fetch errors and render a message, so the
        // harness has to look for that -- otherwise a failed load reads as a
        // clean render of nothing.
        if (cls.includes("status-msg") && cls.includes("bad")) {
          fail(`${name}: rendered an error state -> "${x.textContent}"`);
        }
        x.children.forEach(walk);
      };
      walk(host);
      lines.push(
        `  ${name.padEnd(9)} ${String(countNodes(host)).padStart(5)} nodes  ` +
        `${String(svgs).padStart(2)} svg  ${String(marks).padStart(4)} marks  ` +
        `${String(bars).padStart(3)} bars  ${String(tables).padStart(2)} tables` +
        (tested ? `  ${tested} tested cells` : "") +
        (source ? `  ${source} source blocks` : "")
      );
      if (svgs === 0 && tables === 0 && bars === 0) {
        fail(`${name}: produced neither a chart nor a table`);
      }
      // The row viewer's whole point is pairing a flag with the value that
      // tripped it. The fixture is flagged rows, so if nothing is shaded the
      // rule -> column wiring has silently come apart.
      if (name === "browse" && tested === 0) {
        fail("browse: no cell was marked as tested by a rule");
      }
      // The bench tab prints the deployed Python function and the query that
      // calls it, because "the IRIS side is a Python UDF" is a claim and the
      // source is the evidence. The cards are skipped when the payload carries no
      // fitted tariff, so an empty count means either the endpoint stopped
      // sending one or the cards stopped rendering -- both silent otherwise.
      if (name === "bench" && source < 2) {
        fail(`bench: ${source} source blocks rendered, expected the function and the query`);
      }
    }
    if (results.length !== PANELS.length) {
      fail(`only ${results.length} of ${PANELS.length} panels rendered`);
    }

    const missing = [...requestedIds].filter((id) => !declaredIds.has(id));
    if (missing.length) fail("app.js requests ids absent from index.html: " + missing.join(", "));

    lines.push("");
    lines.push(`  ids requested by app.js: ${requestedIds.size}, all present: ${missing.length === 0}`);
    lines.push(`  nodes created in total:   ${ALL_NODES.length}`);

    if (notes.length) {
      lines.push("", "Notes:");
      notes.forEach((n) => lines.push("  - " + n));
    }

    lines.push("");
    if (problems.length) {
      lines.push(`FAILED — ${problems.length} problem(s):`);
      problems.forEach((p) => lines.push("  ✗ " + p));
    } else {
      lines.push("PASSED — no runtime errors, no non-finite geometry, no missing ids.");
    }
    out(lines.join("\n"));
  });
})();
