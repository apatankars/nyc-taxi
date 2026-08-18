/*
  Dashboard front end: no dependencies, no build step, charts drawn as inline SVG.

  Conventions the drawing code follows:
    - one y-scale per chart, never two;
    - one hue for magnitude across nominal categories, so a filter that changes
      the ranking does not repaint the bars;
    - sequential blue for the heatmap only;
    - selective direct labels (the extreme), the rest in the axis and the tooltip.
*/

"use strict";

const SVG_NS = "http://www.w3.org/2000/svg";
const BAR_MAX = 22;       // bars never thicker than this
const BAR_GAP = 2;        // surface-colour gap between adjacent marks
const BAR_RADIUS = 3;     // rounded data end; the baseline end stays square
const SEQ = ["--seq-100", "--seq-200", "--seq-300", "--seq-400",
             "--seq-500", "--seq-600", "--seq-700"];

const TABS = ["overview", "geography", "quality", "trips", "pushdown"];

const state = {
  meta: null,
  filters: new URLSearchParams(),
  sel: {},               // filter key -> Set of selected values, as strings
  tab: "overview",
  data: {},              // last payload per tab, so a resize can redraw
  tripPage: 1,
  selectedTrip: null,
  qualityRule: null,      // which flag the distribution panel is showing
};

// --------------------------------------------------------------------------
// helpers
// --------------------------------------------------------------------------

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

function el(tag, attrs = {}, children = []) {
  return fill(document.createElement(tag), attrs, children);
}

function svgEl(tag, attrs = {}, children = []) {
  return fill(document.createElementNS(SVG_NS, tag), attrs, children);
}

function fill(node, attrs, children) {
  for (const [key, value] of Object.entries(attrs)) {
    if (value === null || value === undefined || value === false) continue;
    if (key === "text") node.textContent = value;
    else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else node.setAttribute(key, value);
  }
  for (const child of [].concat(children)) {
    if (child === null || child === undefined || child === false) continue;
    node.appendChild(typeof child === "string"
      ? document.createTextNode(child) : child);
  }
  return node;
}

const token = (name) => getComputedStyle(document.documentElement)
  .getPropertyValue(name).trim();

const missing = (n) => n === null || n === undefined || Number.isNaN(n);
const fmtInt = (n) => missing(n) ? "-" : Math.round(n).toLocaleString("en-US");
/** Fixed decimals with thousands separators: 2,435,072.7, not 2435072.7. The
    quality panels print the tails of these distributions, where an ungrouped run
    of digits is the difference between 22,110 miles and 2,211. */
const grouped = (n, places) => Number(n).toLocaleString("en-US",
  { minimumFractionDigits: places, maximumFractionDigits: places });
// Sign in front of the currency symbol: "$-113.49" reads as a typo, and negative
// fares are refunds, a real business event this page argues about.
const fmtMoney = (n) => missing(n) ? "-"
  : (Number(n) < 0 ? "-$" : "$") + grouped(Math.abs(Number(n)), 2);
const fmtNum = (n, places = 2) => missing(n) ? "-" : Number(n).toFixed(places);
const fmtPct = (n, places = 1) => missing(n) ? "-"
  : Number(n).toFixed(places) + "%";

/** A field value in whatever unit that field is measured in. The quality panels
    compare one column at a time, and the column decides how its numbers read:
    dollars, miles, minutes, a bare count, a calendar year. */
function fmtUnit(value, unit) {
  if (missing(value)) return "-";
  switch (unit) {
    case "$": return fmtMoney(value);
    case "$/mi": return fmtMoney(value) + "/mi";
    case "mi": return grouped(value, 2) + " mi";
    case "min": return grouped(value, 1) + " min";
    case "mph": return grouped(value, 1) + " mph";
    // A whole number of passengers is "1", not "1.00"; their average is 1.31.
    case "count": return grouped(value, Number(value) % 1 === 0 ? 0 : 2);
    // A year is a label: no decimals, no thousands separator.
    case "year": return String(Math.round(value));
    default: return fmtNum(value, 2);
  }
}

/** A multiple of the unflagged average, readable across the four orders of
    magnitude these rules actually produce: 7,616x, 1.45x, 0.003x. Fixed
    precision would print the last of those as 0.00. */
function fmtRatio(value) {
  if (missing(value)) return "-";
  const n = Number(value);
  return (n >= 100 ? fmtInt(n) : n >= 1 ? fmtNum(n, 2) : fmtNum(n, 3)) + "x";
}

const tickText = (v) => v >= 1e6 ? (v / 1e6).toFixed(v % 1e6 ? 1 : 0) + "M"
  : v >= 1000 ? (v / 1000).toFixed(v % 1000 ? 1 : 0) + "k"
  : String(Math.round(v * 100) / 100);

/** Rough text width, for deciding whether a label fits before drawing it. */
const textWidth = (text, size = 11) => String(text).length * size * 0.58;

/**
 * A readable axis: round tick values, and a domain that ends on a tick, so the
 * last gridline is the end of the scale rather than a line short of the longest
 * bar. Charts scale against `hi`, not against the largest value.
 */
function niceScale(min, max, count = 4) {
  if (!(max > min)) return { lo: Math.min(0, min), hi: max || 1, values: [max || 1] };
  const raw = (max - min) / count;
  const magnitude = Math.pow(10, Math.floor(Math.log10(raw)));
  const step = [1, 2, 2.5, 5, 10].map((m) => m * magnitude)
    .find((s) => s >= raw) || magnitude * 10;
  const lo = Math.floor(min / step) * step;
  const hi = Math.ceil(max / step) * step;
  const values = [];
  // Round each tick off the step rather than accumulating it: a fractional step
  // drifts, and a tick labelled 18.000000000000004 is a real thing this prevents.
  for (let v = lo; v <= hi + step * 1e-6; v += step) {
    values.push(Math.round(v / step) * step);
  }
  return { lo, hi, values };
}

/** Bars are rounded at the data end and square at the baseline, so the baseline
    stays a straight line. */
function barPathH(x0, y, width, height, r = BAR_RADIUS) {
  const w = Math.max(width, 0.5);
  const radius = Math.min(r, w, height / 2);
  return `M${x0},${y} H${x0 + w - radius}`
       + ` A${radius},${radius} 0 0 1 ${x0 + w},${y + radius}`
       + ` V${y + height - radius}`
       + ` A${radius},${radius} 0 0 1 ${x0 + w - radius},${y + height}`
       + ` H${x0} Z`;
}

/** Band geometry: thickness capped, the leftover becomes air rather than fill. */
function band(extent, count, max = BAR_MAX) {
  const slot = count ? extent / count : extent;
  return { slot, thickness: Math.max(3, Math.min(max, slot - BAR_GAP)),
           offset: (slot - Math.max(3, Math.min(max, slot - BAR_GAP))) / 2 };
}

const ellipsis = (label, width) =>
  textWidth(label, 11.5) <= width - 10 ? label
    : label.slice(0, Math.max(3, Math.floor(width / 6.6) - 1)) + "…";

// --------------------------------------------------------------------------
// tooltip
// --------------------------------------------------------------------------
// Where the values that are not directly labelled live. Hit targets are the whole
// band, so hovering never requires precision.

const tooltip = {
  node: null,
  show(event, title, rows) {
    if (!this.node) this.node = $("#tooltip");
    this.node.innerHTML = "";
    this.node.appendChild(el("div", { class: "t", text: title }));
    for (const [label, value] of rows) {
      if (value === null || value === undefined) continue;
      this.node.appendChild(el("div", { class: "r" }, [
        el("span", { text: label }), el("b", { text: String(value) }),
      ]));
    }
    this.node.hidden = false;
    this.move(event);
  },
  move(event) {
    if (!this.node || this.node.hidden) return;
    const box = this.node.getBoundingClientRect();
    let x = event.clientX + 14;
    let y = event.clientY + 14;
    if (x + box.width > window.innerWidth - 8) x = event.clientX - box.width - 14;
    if (y + box.height > window.innerHeight - 8) y = event.clientY - box.height - 14;
    this.node.style.left = Math.max(8, x) + "px";
    this.node.style.top = Math.max(8, y) + "px";
  },
  hide() { if (this.node) this.node.hidden = true; },
};

/** Attach hover to a mark's hit area, plus an SVG <title> for screen readers. */
function hoverable(node, title, rows, onClick) {
  node.addEventListener("mouseenter", (e) => tooltip.show(e, title, rows));
  node.addEventListener("mousemove", (e) => tooltip.move(e));
  node.addEventListener("mouseleave", () => tooltip.hide());
  node.appendChild(svgEl("title", {
    text: title + " - " + rows.map(([k, v]) => `${k}: ${v}`).join(", "),
  }));
  if (onClick) {
    node.style.cursor = "pointer";
    node.addEventListener("click", onClick);
  }
  return node;
}

// --------------------------------------------------------------------------
// panels
// --------------------------------------------------------------------------

/** The swatch row. Its own function because the distribution panel draws a legend
    below its own controls rather than in a panel head. */
function legendRow(items) {
  return el("div", { class: "legend" }, items.map((item) =>
    el("span", {}, [el("span", { class: "swatch",
                                 style: `background:${item.color}` }),
                    el("span", { text: item.label })])));
}

function head(node, spec) {
  node.innerHTML = "";
  node.appendChild(el("h2", { text: spec.title }));
  if (spec.sub) node.appendChild(el("p", { class: "sub", text: spec.sub }));
  if (spec.legend) node.appendChild(legendRow(spec.legend));
  if (spec.ramp) {
    node.appendChild(el("div", { class: "ramp" }, [
      el("span", { text: "fewer" }),
      el("div", {}, SEQ.map((name) =>
        el("i", { style: `background:${token(name)}` }))),
      el("span", { text: `${fmtInt(spec.ramp.high)} trips` }),
    ]));
  }
}

function chart(node, spec) {
  head(node, spec);
  node.appendChild(spec.empty
    ? el("p", { class: "loading", text: "No rows match the current filters." })
    : spec.draw(Math.max(320, node.clientWidth)));
  if (spec.note) node.appendChild(el("p", { class: "note", text: spec.note }));
}

function tablePanel(node, spec) {
  head(node, spec);
  node.appendChild(spec.rows.length
    ? dataTable(spec)
    : el("p", { class: "loading", text: spec.emptyText || "No rows." }));
  if (spec.note) node.appendChild(el("p", { class: "note", text: spec.note }));
}

function dataTable({ columns, rows, onRowClick, selectedIndex }) {
  const table = el("table", {}, [
    el("thead", {}, [el("tr", {}, columns.map((col) =>
      el("th", { class: col.numeric ? "n" : "", scope: "col",
                 text: col.label })))]),
    el("tbody", {}, rows.map((row, index) => {
      const tr = el("tr", {
        class: [onRowClick ? "clickable" : "",
                index === selectedIndex ? "selected" : ""].filter(Boolean).join(" "),
      }, columns.map((col) => {
        const value = row[col.key];
        const empty = value === null || value === undefined || value === "";
        return el("td", {
          class: [col.numeric ? "n" : "", col.wrap ? "wrap" : "",
                  empty ? "null" : ""].filter(Boolean).join(" "),
          text: empty ? "-" : String(value),
        });
      }));
      if (onRowClick) {
        tr.tabIndex = 0;
        tr.addEventListener("click", () => onRowClick(row, index));
        tr.addEventListener("keydown", (event) => {
          if (event.key === "Enter" || event.key === " ") {
            event.preventDefault();
            onRowClick(row, index);
          }
        });
      }
      return tr;
    })),
  ]);
  return el("div", { class: "scroll" + (rows.length > 24 ? " tall" : "") },
            [table]);
}

// --------------------------------------------------------------------------
// charts
// --------------------------------------------------------------------------

/** Horizontal bars: magnitude across nominal categories. One hue, because length
    already carries the magnitude. Only the largest bar gets a value label. */
function barsH(width, rows, options = {}) {
  const { labelKey = "label", valueKey = "value", color = token("--series-1"),
          labelWidth = 150, rowHeight = 24, tip = () => [],
          labelFormat = fmtInt, onClick = null } = options;

  const height = rows.length * rowHeight + 30;
  const plotWidth = Math.max(80, width - labelWidth - 56);
  const axis = niceScale(0, Math.max(...rows.map((r) => Number(r[valueKey]) || 0), 1));
  const scale = (value) => (Number(value) || 0) / axis.hi * plotWidth;
  const { thickness, offset } = band(rowHeight, 1);
  const peak = rows.reduce((best, r, i) =>
    (Number(r[valueKey]) || 0) > (Number(rows[best][valueKey]) || 0) ? i : best, 0);

  const svg = svgEl("svg", { viewBox: `0 0 ${width} ${height}`, width, height,
                             role: "img" });
  for (const value of axis.values) {
    const x = labelWidth + scale(value);
    svg.appendChild(svgEl("line", { class: "grid", x1: x, x2: x, y1: 0,
                                    y2: rows.length * rowHeight }));
    svg.appendChild(svgEl("text", { class: "tick", x, y: height - 10,
                                    "text-anchor": "middle",
                                    text: tickText(value) }));
  }
  svg.appendChild(svgEl("line", { class: "baseline", x1: labelWidth, x2: labelWidth,
                                  y1: 0, y2: rows.length * rowHeight }));

  rows.forEach((row, index) => {
    const y = index * rowHeight;
    const value = Number(row[valueKey]) || 0;
    const label = String(row[labelKey]);
    svg.appendChild(svgEl("text", {
      class: "cat-label", x: labelWidth - 8, y: y + rowHeight / 2 + 4,
      "text-anchor": "end", text: ellipsis(label, labelWidth),
    }));
    svg.appendChild(svgEl("path", {
      class: "mark", d: barPathH(labelWidth, y + offset, scale(value), thickness),
      // A function where the rows are separate identities rather than one
      // category: the push-down chart's two bars keep their own colours whichever
      // one wins. Colour follows the entity, never its rank.
      fill: typeof color === "function" ? color(row, index) : color,
    }));
    if (index === peak || rows.length <= 3) {
      const text = labelFormat(value);
      svg.appendChild(svgEl("text", {
        class: "value-label", x: labelWidth + scale(value) + 6,
        y: y + rowHeight / 2 + 4, text,
      }));
    }
    svg.appendChild(hoverable(svgEl("rect", {
      class: "hit", x: labelWidth, y, width: plotWidth, height: rowHeight,
    }), label, tip(row), onClick && (() => onClick(row))));
  });
  return svg;
}

/** Two series per category, side by side in the same band. */
function groupedBarsH(width, rows, options = {}) {
  const { labelKey = "label", series = [], labelWidth = 170, rowHeight = 28,
          tip = () => [], onClick = null, tickFormat = tickText } = options;

  const height = rows.length * rowHeight + 30;
  const plotWidth = Math.max(80, width - labelWidth - 56);
  const axis = niceScale(0, Math.max(
    ...rows.flatMap((r) => series.map((s) => Number(r[s.key]) || 0)), 1));
  const scale = (value) => (Number(value) || 0) / axis.hi * plotWidth;
  const inner = band(rowHeight - 6, series.length, 10);

  const svg = svgEl("svg", { viewBox: `0 0 ${width} ${height}`, width, height,
                             role: "img" });
  for (const value of axis.values) {
    const x = labelWidth + scale(value);
    svg.appendChild(svgEl("line", { class: "grid", x1: x, x2: x, y1: 0,
                                    y2: rows.length * rowHeight }));
    svg.appendChild(svgEl("text", { class: "tick", x, y: height - 10,
                                    "text-anchor": "middle",
                                    text: tickFormat(value) }));
  }
  svg.appendChild(svgEl("line", { class: "baseline", x1: labelWidth, x2: labelWidth,
                                  y1: 0, y2: rows.length * rowHeight }));

  rows.forEach((row, index) => {
    const y = index * rowHeight;
    const label = String(row[labelKey]);
    svg.appendChild(svgEl("text", {
      class: "cat-label", x: labelWidth - 8, y: y + rowHeight / 2 + 4,
      "text-anchor": "end", text: ellipsis(label, labelWidth),
    }));
    series.forEach((s, si) => {
      svg.appendChild(svgEl("path", {
        class: "mark", fill: s.color,
        d: barPathH(labelWidth, y + 3 + si * inner.slot + inner.offset,
                    scale(row[s.key]), inner.thickness),
      }));
    });
    svg.appendChild(hoverable(svgEl("rect", {
      class: "hit", x: labelWidth, y, width: plotWidth, height: rowHeight,
    }), label, tip(row), onClick && (() => onClick(row))));
  });
  return svg;
}

/** A line over an ordered axis, zero-based, with the peak labelled. */
function lineChart(width, rows, options = {}) {
  const { labelKey = "label", valueKey = "value", color = token("--series-1"),
          height = 200, tip = () => [], everyNthLabel = 1,
          labelFormat = fmtInt } = options;

  const padLeft = 44, padRight = 16, padTop = 22, padBottom = 24;
  const plotWidth = Math.max(60, width - padLeft - padRight);
  const base = height - padBottom;
  const axis = niceScale(0, Math.max(...rows.map((r) => Number(r[valueKey]) || 0), 1));
  const x = (i) => padLeft + (rows.length === 1 ? plotWidth / 2
                              : i * plotWidth / (rows.length - 1));
  const y = (v) => base - (Number(v) || 0) / axis.hi * (base - padTop);

  const svg = svgEl("svg", { viewBox: `0 0 ${width} ${height}`, width, height,
                             role: "img" });
  for (const value of axis.values) {
    svg.appendChild(svgEl("line", { class: "grid", x1: padLeft,
                                    x2: padLeft + plotWidth,
                                    y1: y(value), y2: y(value) }));
    svg.appendChild(svgEl("text", { class: "tick", x: padLeft - 8, y: y(value) + 4,
                                    "text-anchor": "end",
                                    text: tickText(value) }));
  }
  svg.appendChild(svgEl("line", { class: "baseline", x1: padLeft, y1: base,
                                  x2: padLeft + plotWidth, y2: base }));

  svg.appendChild(svgEl("path", {
    d: rows.map((r, i) => `${i ? "L" : "M"}${x(i)},${y(r[valueKey])}`).join(" "),
    fill: "none", stroke: color, "stroke-width": 2,
    "stroke-linejoin": "round", "stroke-linecap": "round",
  }));

  const peak = rows.reduce((best, r, i) =>
    (Number(r[valueKey]) || 0) > (Number(rows[best][valueKey]) || 0) ? i : best, 0);

  rows.forEach((row, index) => {
    if (index % everyNthLabel === 0) {
      svg.appendChild(svgEl("text", { class: "tick", x: x(index), y: height - 8,
                                      "text-anchor": "middle",
                                      text: String(row[labelKey]) }));
    }
    // A 2px surface ring keeps the dot legible where it crosses a gridline.
    svg.appendChild(svgEl("circle", {
      cx: x(index), cy: y(row[valueKey]), r: 4, fill: color,
      stroke: token("--surface"), "stroke-width": 2,
    }));
    svg.appendChild(hoverable(svgEl("rect", {
      class: "hit", x: x(index) - plotWidth / (rows.length * 2 || 1),
      y: padTop - 12, width: plotWidth / (rows.length || 1),
      height: base - padTop + 14,
    }), String(row[labelKey]), tip(row)));
  });

  if (rows.length) {
    svg.appendChild(svgEl("text", {
      class: "value-label", x: x(peak), y: y(rows[peak][valueKey]) - 9,
      "text-anchor": peak === rows.length - 1 ? "end" : "middle",
      text: labelFormat(Number(rows[peak][valueKey])),
    }));
  }
  return svg;
}

/** Sequential one-hue grid: the encoding for a continuous magnitude. */
function heatmap(width, cells, options = {}) {
  const { rowNames = [], max = 1 } = options;
  const padLeft = 40, padTop = 18, cellHeight = 24, cols = 24;
  const rowCount = rowNames.length || 7;
  const cellWidth = Math.max(10, (width - padLeft - 8) / cols);
  const height = padTop + rowCount * cellHeight + 8;
  const steps = SEQ.map(token);
  const grid = new Map(cells.map((c) => [`${c.dow}:${c.hour}`, c]));

  const svg = svgEl("svg", { viewBox: `0 0 ${width} ${height}`, width, height,
                             role: "img" });
  for (let hour = 0; hour < cols; hour += 2) {
    svg.appendChild(svgEl("text", {
      class: "tick", x: padLeft + hour * cellWidth + cellWidth / 2, y: padTop - 6,
      "text-anchor": "middle", text: String(hour).padStart(2, "0"),
    }));
  }
  rowNames.forEach((name, index) => {
    svg.appendChild(svgEl("text", {
      class: "cat-label", x: padLeft - 8,
      y: padTop + index * cellHeight + cellHeight / 2 + 4,
      "text-anchor": "end", text: name,
    }));
  });

  for (let dow = 1; dow <= rowCount; dow += 1) {
    for (let hour = 0; hour < cols; hour += 1) {
      const cell = grid.get(`${dow}:${hour}`);
      const trips = cell ? cell.trips : 0;
      // Linear, checked against the distribution rather than assumed: these 168
      // cells spread evenly enough across the seven steps that a square-root
      // transform would empty the lightest step and pile a third of the grid into
      // one shade.
      const step = trips === 0 ? 0
        : Math.min(steps.length - 1, Math.floor(trips / max * steps.length));
      svg.appendChild(hoverable(svgEl("rect", {
        class: "cell mark", x: padLeft + hour * cellWidth,
        y: padTop + (dow - 1) * cellHeight, width: cellWidth, height: cellHeight,
        fill: trips === 0 ? token("--line") : steps[step],
      }), `${rowNames[dow - 1] || dow} ${String(hour).padStart(2, "0")}:00`,
        [["Trips", fmtInt(trips)],
         ["Average fare", cell ? fmtMoney(cell.avg_fare) : "-"]]));
    }
  }
  return svg;
}

// --------------------------------------------------------------------------
// fetching
// --------------------------------------------------------------------------

async function api(path) {
  const response = await fetch(path);
  const text = await response.text();
  let payload;
  try {
    payload = JSON.parse(text);
  } catch {
    // An HTML error page or an IRIS error envelope lands here; showing the start
    // of it beats "unexpected token '<'".
    throw new Error(`${response.status} ${response.statusText}: `
                    + text.slice(0, 300));
  }
  if (!response.ok && payload.error) throw new Error(payload.error);
  return payload;
}

const withFilters = (path) => {
  const query = state.filters.toString();
  return query ? `${path}?${query}` : path;
};

// --------------------------------------------------------------------------
// overview
// --------------------------------------------------------------------------

function renderOverview(data) {
  const host = $("#kpis");
  host.innerHTML = "";
  for (const tile of data.kpis) {
    const value = tile.unit === "$" ? fmtMoney(tile.value)
      : tile.unit === "count" ? fmtInt(tile.value)
      : fmtNum(tile.value, tile.unit === "min" ? 1 : 2) + " " + tile.unit;
    host.appendChild(el("div", {}, [
      el("div", { class: "kpi-label", text: tile.label }),
      el("div", { class: "kpi-value", text: value }),
      el("div", { class: "kpi-sub",
                  text: tile.secondary
                    ? `${tile.secondary}, ${fmtInt(tile.over)} trips`
                    : `over ${fmtInt(tile.over)} trips` }),
    ]));
  }
  $("#kpi-note").textContent =
    "Trip counts keep flagged trips - a broken meter reading does not mean nobody "
    + "got in the cab. Each average excludes only the rules that undermine it, "
    + "which is why the row counts differ. See Trip quality for what that costs.";

  chart($("#chart-monthly"), {
    title: "Trips by month",
    empty: !data.monthly.length,
    draw: (w) => lineChart(w, data.monthly, {
      valueKey: "trips",
      tip: (r) => [["Trips", fmtInt(r.trips)],
                   ["Average fare", fmtMoney(r.avg_fare)],
                   ["Average distance", fmtNum(r.avg_distance) + " mi"]],
    }),
  });

  chart($("#chart-hourly"), {
    title: "Trips by hour of day",
    empty: !data.hourly.length,
    draw: (w) => lineChart(w, data.hourly, {
      valueKey: "trips", everyNthLabel: 2,
      tip: (r) => [["Trips", fmtInt(r.trips)],
                   ["Average fare", fmtMoney(r.avg_fare)]],
    }),
  });

  const heat = data.heatmap;
  chart($("#chart-heatmap"), {
    title: "Demand by hour and day of week",
    sub: heat.peak
      ? `Busiest: ${heat.dow_names[heat.peak.dow - 1]} at `
        + `${String(heat.peak.hour).padStart(2, "0")}:00, `
        + `${fmtInt(heat.peak.trips)} pickups`
      : "",
    empty: !heat.cells.length,
    ramp: { high: heat.max_trips },
    draw: (w) => heatmap(w, heat.cells, { rowNames: heat.dow_names,
                                          max: heat.max_trips }),
    note: "168 cells from one GROUP BY on two bitmap-indexed columns.",
  });
}

// --------------------------------------------------------------------------
// geography
// --------------------------------------------------------------------------

function renderGeography(data) {
  chart($("#chart-boroughs"), {
    title: "Trips by pickup borough",
    empty: !data.boroughs.length,
    draw: (w) => barsH(w, data.boroughs, {
      labelKey: "borough", valueKey: "trips", labelWidth: 110, rowHeight: 26,
      tip: (r) => [["Trips", fmtInt(r.trips)], ["Share", fmtPct(r.share)],
                   ["Average fare", fmtMoney(r.avg_fare)],
                   ["Average distance", fmtNum(r.avg_distance) + " mi"],
                   ["Tip, card", fmtPct(r.tip_pct)]],
    }),
    note: "Unknown and outside-NYC zone codes are excluded from every geographic "
        + "panel; they resolve through the lookup but name no place.",
  });

  for (const [id, rows, title] of [
    ["#chart-pickup-zones", data.pickup_zones, "Busiest pickup zones"],
    ["#chart-dropoff-zones", data.dropoff_zones, "Busiest drop-off zones"],
  ]) {
    chart($(id), {
      title,
      sub: "Top 12, ranked in IRIS",
      empty: !rows.length,
      draw: (w) => barsH(w, rows, {
        labelKey: "zone", valueKey: "trips", labelWidth: 142,
        tip: (r) => [["Trips", fmtInt(r.trips)], ["Share", fmtPct(r.share, 2)],
                     ["Borough", r.borough],
                     ["Average fare", fmtMoney(r.avg_fare)]],
      }),
    });
  }

  tablePanel($("#table-payments"), {
    title: "Payment mix and tips",
    sub: "Cash records a 0.00 tip on every trip: unobserved, not zero",
    columns: [{ key: "label", label: "Payment" },
              { key: "trips", label: "Trips", numeric: true },
              { key: "share", label: "Share", numeric: true },
              { key: "avg_fare", label: "Avg fare", numeric: true },
              { key: "avg_tip", label: "Avg tip", numeric: true },
              { key: "tipped_pct", label: "Tipped", numeric: true }],
    rows: data.payments.map((r) => ({
      label: r.label, trips: fmtInt(r.trips), share: fmtPct(r.share),
      avg_fare: fmtMoney(r.avg_fare), avg_tip: fmtMoney(r.avg_tip),
      tipped_pct: fmtPct(r.tipped_pct),
    })),
    note: "Averaging tips across payment types halves the answer, so tip figures "
        + "elsewhere on this page are card-only.",
  });

  tablePanel($("#table-routes"), {
    title: "Most common routes",
    sub: `Top ${data.pairs.rows.length} origin to destination pairs, then the `
       + "busiest same-zone round trips",
    columns: [{ key: "route", label: "Route" },
              { key: "trips", label: "Trips", numeric: true },
              { key: "miles", label: "Miles", numeric: true },
              { key: "minutes", label: "Minutes", numeric: true },
              { key: "fare", label: "Avg fare", numeric: true }],
    rows: data.pairs.rows.map((r) => ({
      route: `${r.origin} → ${r.destination}`, trips: fmtInt(r.trips),
      miles: fmtNum(r.avg_distance), minutes: fmtNum(r.avg_duration, 1),
      fare: fmtMoney(r.avg_fare),
    })).concat(data.pairs.circular.map((r) => ({
      route: `${r.zone} → ${r.zone} (same zone)`, trips: fmtInt(r.trips),
      miles: null, minutes: null, fare: fmtMoney(r.avg_fare),
    }))),
  });
}

// --------------------------------------------------------------------------
// trip quality
// --------------------------------------------------------------------------

// Five panels answering one question in order: how much of the data is flagged,
// which rules do the flagging, how the flagged values compare with the unflagged
// ones rule by rule, where one rule's values fall across the whole range, and
// what excluding them costs.
//
// "Traditional, validated values" here means flag_count = 0 -- the trips no rule
// objected to at all. That is the yardstick, not the reporting policy: the last
// panel still argues against publishing figures computed under it, because the
// unflagged subset is a *biased* subset. One fixed population is what makes the
// comparison mean the same thing for all sixteen rules.

function renderQuality(data) {
  renderFlagCoverage(data.coverage);
  renderRuleImpact(data.rules);

  const profiles = data.profiles;
  // A remembered rule id can outlive the panel it came from -- a rule disabled
  // between loads, or a resize after the rule set changed -- so fall back to the
  // largest flagged population rather than draw an empty chart.
  if (!profiles.rows.some((row) => row.rule_id === state.qualityRule)) {
    state.qualityRule = profiles.default_rule;
  }
  renderFlagProfiles(profiles);
  renderDistributionFrame(profiles);
  const cached = state.data.flagDist;
  if (cached && cached.rule_id === state.qualityRule) {
    renderFlagDistribution(cached);          // a redraw, not a new selection
  } else if (state.qualityRule !== null) {
    loadFlagDistribution(state.qualityRule);
  }

  renderPolicies(data.policies);
}

/** How much of the dataset carries a flag at all, by number of flags. */
function renderFlagCoverage(coverage) {
  const t = coverage.totals;
  chart($("#chart-flag-coverage"), {
    title: "How much of the data is flagged",
    sub: `${fmtInt(t.flagged)} of ${fmtInt(t.trips)} trips (${fmtPct(t.flagged_pct, 2)}) `
       + `carry at least one flag; ${fmtInt(t.clean)} carry none, and those are the `
       + `comparison population for every panel below.`,
    empty: !coverage.rows.length,
    draw: (w) => barsH(w, coverage.rows, {
      labelKey: "label", valueKey: "trips", labelWidth: 96, rowHeight: 26,
      tip: (r) => [["Trips", fmtInt(r.trips)],
                   ["Share of all trips", fmtPct(r.share, 2)]],
    }),
    note: `Severity matters more than count: ${fmtInt(t.invalid)} trips `
        + `(${fmtPct(t.invalid_pct, 2)}) carry at least one 'invalid' flag - a value `
        + `that cannot describe a real trip - while the other ${fmtInt(t.unusual_only)} `
        + `flagged trips carry only 'unusual' ones, which are suspicious but possible. `
        + `Unfiltered, like the three panels below it: how much of the data the rules `
        + `touch is a fact about the dataset, not about the current selection.`,
  });
}

/** Per-rule flagged values against the unflagged population's, one row per rule. */
function renderFlagProfiles(profiles) {
  tablePanel($("#table-flag-profiles"), {
    title: "Flagged values against the validated ones",
    sub: "Each rule is a claim about one column, so each row compares that column "
       + "over the trips the rule flagged and over the "
       + `${fmtInt(profiles.baseline.trips)} trips no rule flagged. `
       + "Select a row to see the whole distribution.",
    columns: [{ key: "rule", label: "Rule" },
              { key: "severity", label: "Severity" },
              { key: "trips", label: "Flagged", numeric: true },
              { key: "pct", label: "Of all trips", numeric: true },
              { key: "field", label: "Column compared" },
              { key: "flagged_avg", label: "Flagged avg", numeric: true },
              { key: "flagged_range", label: "Flagged range", numeric: true },
              { key: "baseline_avg", label: "Unflagged avg", numeric: true },
              { key: "ratio", label: "Multiple", numeric: true }],
    rows: profiles.rows.map((r) => ({
      rule: r.rule_name, severity: r.severity, trips: fmtInt(r.trips),
      pct: fmtPct(r.pct, 2), field: r.field_label,
      flagged_avg: r.flagged ? fmtUnit(r.flagged.avg, r.unit) : null,
      // A rule that pins its column to one value says so in one number: repeating
      // "0.00 mi to 0.00 mi" reads as a formatting accident rather than a finding.
      flagged_range: !r.flagged ? null
        : r.flagged.min === r.flagged.max ? fmtUnit(r.flagged.min, r.unit)
        : `${fmtUnit(r.flagged.min, r.unit)} to ${fmtUnit(r.flagged.max, r.unit)}`,
      baseline_avg: r.baseline ? fmtUnit(r.baseline.avg, r.unit) : null,
      ratio: fmtRatio(r.ratio),
    })),
    selectedIndex: profiles.rows.findIndex((r) => r.rule_id === state.qualityRule),
    onRowClick: (row, index) => {
      loadFlagDistribution(profiles.rows[index].rule_id);
      renderFlagProfiles(profiles);          // move the highlight to this row
    },
    note: "The multiple is blank where it would mislead: against a zero or negative "
        + "flagged average (distance_nonpositive averages exactly 0, fare_negative "
        + "averages a refund) a multiple invites reading a sign flip as a small "
        + "difference, and the ratio of two calendar years is 0.99 and means "
        + "nothing. Rules matching no rows are omitted, so cast_failed is absent - "
        + "every row cast cleanly. meter_metadata_missing has no single number "
        + "behind it and is compared on fare_amount, because whether that "
        + "55,613-trip block is worth keeping is exactly the question of whether "
        + "its fares look like everyone else's.",
  });
}

/** The distribution panel's frame: heading, rule picker, and an empty body the
    chart loads into. Built in JS rather than in index.html because a loading
    state repaints the panel, and static markup would not survive it. */
function renderDistributionFrame(profiles) {
  const panel = $("#panel-flag-distribution");
  panel.innerHTML = "";
  panel.appendChild(el("h2", { text: "Where one flag's values fall" }));
  panel.appendChild(el("p", { class: "sub", text:
    "The same axis for both populations, each bar a share of its own population. "
    + "Shares rather than counts because the two groups differ by up to three "
    + "orders of magnitude: on a count axis the flagged series would be a flat "
    + "line at zero." }));

  const select = el("select", { id: "quality-rule",
    onchange: (event) => loadFlagDistribution(event.target.value) });
  for (const row of profiles.rows) {
    select.appendChild(el("option", { value: String(row.rule_id),
      text: `${row.rule_name} - ${fmtInt(row.trips)} trips` }));
  }
  if (state.qualityRule !== null) select.value = String(state.qualityRule);
  panel.appendChild(el("div", { class: "toolbar" }, [
    el("div", { class: "field" }, [
      el("label", { for: "quality-rule", text: "Flag" }), select])]));
  panel.appendChild(el("div", { id: "flag-dist-body" }));
}

function renderFlagDistribution(dist) {
  const body = $("#flag-dist-body");
  if (!body) return;
  body.innerHTML = "";
  const flagged = dist.flagged, base = dist.baseline, unit = dist.unit;

  // A rule that pins its column to one value should say so once: "averages 0.00,
  // range 0.00 to 0.00" is three statements of the same fact.
  const spread = (stats) => stats.min === stats.max
    ? `always ${fmtUnit(stats.min, unit)}`
    : `average ${fmtUnit(stats.avg, unit)}, range ${fmtUnit(stats.min, unit)} to `
      + `${fmtUnit(stats.max, unit)}`;

  body.appendChild(el("p", { class: "sub", text:
    `${dist.field_label} over the ${fmtInt(flagged.trips)} trips `
    + `${dist.rule_name} flags: ${spread(flagged)}. Over the `
    + `${fmtInt(base.trips)} trips no rule flagged: ${spread(base)}.` }));
  if (dist.predicate) {
    body.appendChild(el("p", { class: "sub" },
      ["The rule: ", el("code", { text: dist.predicate })]));
  }
  body.appendChild(legendRow([
    { label: `Flagged ${dist.rule_name}`, color: token("--series-1") },
    { label: "No rule flagged", color: token("--series-2") }]));

  if (!dist.rows.length) {
    body.appendChild(el("p", { class: "loading",
      text: `No ${dist.field_label} values on either side to compare.` }));
    return;
  }
  body.appendChild(groupedBarsH(Math.max(320, body.clientWidth), dist.rows, {
    labelWidth: 118, rowHeight: 26, tickFormat: (v) => fmtPct(v, 0),
    series: [{ key: "flagged_pct", color: token("--series-1") },
             { key: "baseline_pct", color: token("--series-2") }],
    tip: (r) => [["Flagged", `${fmtInt(r.flagged)} trips, `
                             + fmtPct(r.flagged_pct, 2)],
                 ["Unflagged", `${fmtInt(r.baseline)} trips, `
                               + fmtPct(r.baseline_pct, 2)]],
  }));

  const notes = [
    `Shares are of the rows that report a value: ${fmtInt(flagged.valued)} flagged `
    + `and ${fmtInt(base.valued)} unflagged.`,
    dist.mode === "value" ? "One bar per distinct value, because bucketing this "
                          + "column would blur the finding."
                          : "Buckets are half-open and open-ended at both tails, so "
                            + "a value outside the sensible range still lands "
                            + "somewhere; buckets empty in both populations are "
                            + "dropped.",
  ];
  if (flagged.valued < flagged.trips) {
    notes.push(`${fmtInt(flagged.trips - flagged.valued)} of the flagged trips `
             + `report no ${dist.field_label} at all: counted in the trip total, `
             + `in no bucket.`);
  }
  if (dist.values_dropped) {
    notes.push(`${fmtInt(dist.values_dropped)} further distinct values are not `
             + `shown.`);
  }
  body.appendChild(el("p", { class: "note", text: notes.join(" ") }));
}

/** One rule's distribution, fetched on demand: all of them up front would
    be sixteen scans of 787,060 rows to draw one chart. */
async function loadFlagDistribution(ruleId) {
  const id = Number(ruleId);
  state.qualityRule = id;
  const select = $("#quality-rule");
  if (select) select.value = String(id);
  const body = $("#flag-dist-body");
  if (!body) return;
  body.innerHTML = "";
  body.appendChild(el("p", { class: "loading", text: "Querying IRIS..." }));
  try {
    const data = await api(`api/quality/distribution?rule=${id}`);
    state.data.flagDist = data;
    // The picker may have moved on while this was in flight.
    if (state.qualityRule === data.rule_id) renderFlagDistribution(data);
  } catch (error) {
    const host = $("#flag-dist-body");
    if (!host) return;
    host.innerHTML = "";
    host.appendChild(el("pre", { class: "error", text: error.message }));
  }
}

function renderPolicies(policies) {
  tablePanel($("#table-policies"), {
    title: "What the flag policy costs",
    sub: "The same four figures under three policies, over the current selection",
    columns: [{ key: "policy", label: "Policy" },
              { key: "note", label: "Meaning" },
              { key: "trips", label: "Trips", numeric: true },
              { key: "kept_pct", label: "Kept", numeric: true },
              { key: "avg_fare", label: "Avg fare", numeric: true },
              { key: "avg_distance", label: "Avg miles", numeric: true },
              { key: "avg_duration", label: "Avg min", numeric: true }],
    rows: policies.rows.map((r) => ({
      policy: r.policy, note: r.note, trips: fmtInt(r.trips),
      kept_pct: fmtPct(r.kept_pct), avg_fare: fmtMoney(r.avg_fare),
      avg_distance: fmtNum(r.avg_distance),
      avg_duration: fmtNum(r.avg_duration, 1),
    })),
    note: "Read the distance column first: the spread between the loosest and "
        + "strictest policy comes from a few hundred rows out of 787,060, which is "
        + "why the page states its policy instead of presenting a bare average. The "
        + "panels above compare against the unflagged population because a "
        + "comparison needs one fixed yardstick; this one is the argument against "
        + "reporting under it, since those rows are a biased subset, not a random "
        + "sample.",
  });
}

function renderRuleImpact(impact) {
  const rows = impact.rows.filter((r) => r.trips > 0);
  chart($("#chart-rules"), {
    title: "What each rule flags",
    sub: "Select a bar to inspect those trips. Rules matching nothing are omitted.",
    empty: !rows.length,
    legend: [{ label: "Trips flagged", color: token("--series-1") },
             { label: "Trips where it is the only flag", color: token("--series-2") }],
    draw: (w) => groupedBarsH(w, rows, {
      labelKey: "rule_name",
      series: [{ key: "trips", color: token("--series-1") },
               { key: "only_flag", color: token("--series-2") }],
      tip: (r) => [["Trips flagged", fmtInt(r.trips)],
                   ["Only flag", fmtInt(r.only_flag)],
                   ["Share of all trips", fmtPct(r.pct, 2)],
                   ["Severity", r.severity], ["Phase", r.phase],
                   ["Rule", r.description]],
      onClick: (row) => inspectRule(row.rule_id),
    }),
    note: "The second series decides a policy argument: a rule that only ever "
        + "fires alongside others costs nothing to exclude, while one with a large "
        + "sole-flag count decides on its own what flag_count = 0 throws away.",
  });
}

/** Jump to the trip list, narrowed to one rule's flagged records. */
function inspectRule(ruleId) {
  $("#trip-show").value = "flagged";
  $("#trip-rule").value = String(ruleId);
  $("#trip-order").value = "flags";
  state.tripPage = 1;
  loadTab("trips");
}

// --------------------------------------------------------------------------
// trips
// --------------------------------------------------------------------------

function renderTrips(data) {
  const host = $("#trip-table");
  host.innerHTML = "";
  $("#trip-position").textContent =
    `Page ${fmtInt(data.page)} of ${fmtInt(data.pages)}, `
    + `${fmtInt(data.matched)} trips match`;
  $("#trip-prev").disabled = data.page <= 1;
  $("#trip-next").disabled = data.page >= data.pages;

  host.appendChild(dataTable({
    columns: [{ key: "trip_id", label: "Trip", numeric: true },
              { key: "pickup", label: "Pickup" },
              { key: "route", label: "Route" },
              { key: "distance", label: "Miles", numeric: true },
              { key: "duration", label: "Minutes", numeric: true },
              { key: "fare", label: "Fare", numeric: true },
              { key: "tip", label: "Tip", numeric: true },
              { key: "payment", label: "Payment" },
              { key: "flag_count", label: "Flags", numeric: true },
              { key: "flags", label: "Flagged by", wrap: true }],
    rows: data.rows.map((r) => ({
      trip_id: r.trip_id, pickup: r.pickup,
      route: `${r.pu_zone} → ${r.do_zone}`,
      distance: fmtNum(r.distance), duration: fmtNum(r.duration, 1),
      fare: fmtMoney(r.fare), tip: fmtMoney(r.tip), payment: r.payment,
      flag_count: r.flag_count || null, flags: r.flags.join(", "),
    })),
    selectedIndex: data.rows.findIndex((r) => r.trip_id === state.selectedTrip),
    onRowClick: (row) => loadTripDetail(row.trip_id),
  }));
}

async function loadTripDetail(tripId) {
  const panel = $("#trip-detail");
  state.selectedTrip = tripId;
  if (state.data.trips) renderTrips(state.data.trips);
  panel.hidden = false;
  panel.innerHTML = "";
  panel.appendChild(el("p", { class: "loading", text: `Loading trip ${tripId}...` }));
  try {
    const trip = await api(`api/trip/${tripId}`);
    panel.innerHTML = "";
    panel.appendChild(el("h2", { text: `Trip ${trip.trip_id}` }));
    panel.appendChild(el("p", { class: "sub",
      text: `${trip.pickup} to ${trip.dropoff} · ${trip.pu_zone} `
          + `(${trip.pu_borough}) → ${trip.do_zone} (${trip.do_borough})` }));

    panel.appendChild(dataTable({
      columns: [{ key: "field", label: "Typed value, from Taxi.Trip" },
                { key: "value", label: "" }],
      rows: [
        ["Distance", trip.distance === null ? null : fmtNum(trip.distance) + " mi"],
        ["Duration", trip.duration === null ? null
                   : fmtNum(trip.duration, 1) + " min"],
        ["Implied speed", trip.implied_mph === null ? null
                        : fmtNum(trip.implied_mph, 1) + " mph"],
        ["Fare", trip.fare === null ? null : fmtMoney(trip.fare)],
        ["Tip", trip.tip === null ? null : fmtMoney(trip.tip)],
        ["Total", trip.total === null ? null : fmtMoney(trip.total)],
        ["Passengers", trip.passenger_count],
        ["Payment", trip.payment],
      ].map(([field, value]) => ({ field, value })),
    }));

    panel.appendChild(el("h3", { text: trip.flags.length
      ? `${trip.flags.length} flag${trip.flags.length > 1 ? "s" : ""}`
      : "No flags" }));
    if (trip.flags.length) {
      panel.appendChild(dataTable({
        columns: [{ key: "rule", label: "Rule" },
                  { key: "severity", label: "Severity" },
                  { key: "description", label: "Why", wrap: true }],
        rows: trip.flags.map((f) => ({ rule: f.rule_name, severity: f.severity,
                                       description: f.description })),
      }));
    }

    if (trip.raw) {
      panel.appendChild(el("h3", { text: "The raw CSV text this was parsed from" }));
      panel.appendChild(el("p", { class: "note",
        text: `Taxi.TripRaw row ${trip.raw.raw_id}, every column VARCHAR. A flag is `
            + "an argument about a value, and this is the evidence: a trip_distance "
            + "that was literally \"0\", or a \"1,571.97\" whose thousands "
            + "separator survived the cast, reads differently from the typed number "
            + "above." }));
      panel.appendChild(dataTable({
        columns: [{ key: "name", label: "CSV column" },
                  { key: "value", label: "Text" }],
        rows: trip.raw.fields,
      }));
    }
  } catch (error) {
    panel.innerHTML = "";
    panel.appendChild(el("p", { class: "error", text: error.message }));
  }
}

// --------------------------------------------------------------------------
// push-down comparison
// --------------------------------------------------------------------------

async function runPushdown() {
  const status = $("#pushdown-status");
  const button = $("#pushdown-run");
  status.className = "";
  status.textContent = "Running both halves; the Python half moves every matching "
                     + "trip, so this takes a few seconds...";
  button.disabled = true;
  try {
    const params = new URLSearchParams(state.filters);
    params.set("case", $("#pushdown-case").value);
    const result = await api(`api/pushdown?${params}`);
    state.data.pushdown = result;
    renderPushdown(result);
    status.textContent = "";
  } catch (error) {
    status.className = "bad";
    status.textContent = error.message;
  } finally {
    button.disabled = false;
  }
}

function renderPushdown(result) {
  const rows = [
    { label: result.pushed.label, seconds: result.pushed.seconds,
      moved: result.pushed.rows_returned },
    { label: result.pulled.label, seconds: result.pulled.seconds,
      moved: result.pulled.rows_returned },
  ];

  const node = $("#chart-pushdown");
  node.hidden = false;
  chart(node, {
    title: "Time to produce the same table",
    sub: `${result.label} · ${result.mode.label}`,
    legend: [{ label: rows[0].label, color: token("--series-1") },
             { label: rows[1].label, color: token("--series-2") }],
    draw: (w) => barsH(w, rows, {
      labelKey: "label", valueKey: "seconds", labelWidth: 160, rowHeight: 30,
      labelFormat: (v) => fmtNum(v, 3) + " s",
      color: (row, index) => token(index === 0 ? "--series-1" : "--series-2"),
      tip: (r) => [["Seconds", fmtNum(r.seconds, 3)],
                   ["Rows returned", fmtInt(r.moved)]],
    }),
    note: `Push-down is ${result.speedup} times faster here and moves `
        + `${fmtInt(result.row_ratio)} times less data: ${fmtInt(rows[0].moved)} `
        + `rows crossed the boundary instead of ${fmtInt(rows[1].moved)}. The data `
        + `ratio is the durable number - it is a property of the query, identical `
        + `in both modes, while the timing depends on the transport.`,
  });

  const table = $("#table-pushdown");
  table.hidden = false;
  tablePanel(table, {
    title: "Do the two halves agree?",
    sub: result.count_mismatches.length
      ? `Counts disagree on ${result.count_mismatches.length} of the groups`
      : "Counts are identical; the largest disagreement between the two averages "
        + `is ${result.max_avg_delta}`,
    columns: [{ key: "key", label: "Group" },
              { key: "trips", label: "Trips", numeric: true },
              { key: "iris", label: "Avg fare, IRIS", numeric: true },
              { key: "python", label: "Avg fare, Python", numeric: true },
              { key: "delta", label: "Difference", numeric: true }],
    rows: result.rows.map((r) => ({
      key: r.key, trips: fmtInt(r.trips), iris: fmtNum(r.avg_fare_iris, 4),
      python: fmtNum(r.avg_fare_python, 4),
      delta: fmtNum(Math.abs(r.avg_fare_iris - r.avg_fare_python), 6),
    })),
    note: "Averages are expected to differ in the last decimal, because SQL AVG is "
        + "computed in decimal and Python's sum is binary floating point.",
  });
}

// --------------------------------------------------------------------------
// filters, tabs, boot
// --------------------------------------------------------------------------

function fillSelect(id, items, blank) {
  const select = $(id);
  select.innerHTML = "";
  if (blank) select.appendChild(el("option", { value: "", text: blank }));
  for (const item of items) {
    select.appendChild(el("option", { value: String(item.value),
                                      text: String(item.label) }));
  }
}

// --------------------------------------------------------------- filter bar
// Four dropdowns, each a button that states its own selection over a panel of
// toggle chips. The selection lives in state.sel and the chips are drawn from it,
// so the closed button and the open panel cannot disagree; nothing is read back
// out of the DOM. An empty set means no restriction, which is why the button says
// "All" rather than showing nothing.

const FILTER_SPECS = [
  { key: "month", label: "Month", param: "month", cols: 4,
    items: (meta) => meta.months,
    presets: [["Q1", [1, 2, 3]], ["Q2", [4, 5, 6]],
              ["Q3", [7, 8, 9]], ["Q4", [10, 11, 12]]] },
  { key: "dow", label: "Day", param: "dow", cols: 4,
    items: (meta) => meta.dows,
    presets: [["Weekdays", [1, 2, 3, 4, 5]], ["Weekend", [6, 7]]] },
  // Chips rather than a range slider: a night shift is 22:00 to 03:00, which no
  // single range over 0-23 can express.
  { key: "hour", label: "Hour", param: "hour", cols: 6,
    items: (meta) => meta.hours,
    chip: (item) => String(item.label).slice(0, 2),
    presets: [["Morning 06-09", [6, 7, 8, 9]],
              ["Evening 16-19", [16, 17, 18, 19]],
              ["Overnight 22-05", [22, 23, 0, 1, 2, 3, 4, 5]]] },
  { key: "borough", label: "Pickup borough", param: "borough", cols: 1,
    list: true,
    items: (meta) => meta.boroughs.map((b) =>
      ({ value: b.name, label: b.name, count: b.trips })) },
];

/** Params in option order, not click order, so the same selection always produces
    the same query string -- which is what tells Apply whether anything changed. */
function readFilters() {
  const params = new URLSearchParams();
  for (const spec of FILTER_SPECS) {
    for (const option of spec.options) {
      if (state.sel[spec.key].has(option.value)) params.append(spec.param,
                                                              option.value);
    }
  }
  return params;
}

/** What the closed button says: a run of adjacent values reads as a range, a
    short list reads as itself, anything else as a count. */
function filterSummary(spec) {
  const chosen = spec.options.filter((o) => state.sel[spec.key].has(o.value));
  if (!chosen.length || chosen.length === spec.options.length) return "All";
  if (chosen.length <= 2) return chosen.map((o) => o.label).join(", ");
  const first = spec.options.indexOf(chosen[0]);
  const run = chosen.every((o, i) => spec.options.indexOf(o) === first + i);
  if (run && !spec.list) {
    return `${chosen[0].label} – ${chosen[chosen.length - 1].label}`;
  }
  return `${chosen.length} of ${spec.options.length}`;
}

function buildFilterBar(meta) {
  const bar = $("#filter-bar");
  bar.innerHTML = "";
  for (const spec of FILTER_SPECS) {
    spec.options = spec.items(meta).map((item) =>
      ({ ...item, value: String(item.value), label: String(item.label) }));
    state.sel[spec.key] = new Set();

    const value = el("span", { class: "dd-value", text: "All" });
    // type="button" on every control in here: the filter bar is a form, and a
    // bare <button> inside it submits, which would apply on the first chip.
    const toggle = el("button", {
      type: "button", class: "dd-toggle", "aria-expanded": "false",
      onclick: () => toggleDropdown(spec),
    }, [el("span", { class: "dd-name", text: spec.label }), value,
        el("span", { class: "dd-caret", text: "▾", "aria-hidden": "true" })]);

    const chips = new Map();
    const grid = el("div", { class: "dd-grid",
                             style: `grid-template-columns:repeat(${spec.cols},1fr)` },
      spec.options.map((option) => {
        const chip = el("button", {
          type: "button", class: "chip", "aria-pressed": "false",
          onclick: () => {
            const set = state.sel[spec.key];
            if (set.has(option.value)) set.delete(option.value);
            else set.add(option.value);
            refreshFilter(spec);
          },
        }, [el("span", { text: spec.chip ? spec.chip(option) : option.label }),
            option.count === undefined ? null
              : el("span", { class: "chip-count", text: fmtInt(option.count) })]);
        chips.set(option.value, chip);
        return chip;
      }));

    const quick = el("div", { class: "dd-quick" },
      [["All", null], ["None", []]].concat(spec.presets || [])
        .map(([label, values]) => el("button", {
          type: "button", text: label,
          onclick: () => {
            state.sel[spec.key] = new Set(values === null
              ? [] : values.map(String).filter((v) => chips.has(v)));
            refreshFilter(spec);
          },
        })));

    const panel = el("div", { class: "dd-panel", role: "group",
                              "aria-label": spec.label }, [quick, grid]);
    panel.hidden = true;
    const wrap = el("div", { class: spec.list ? "dd dd-list" : "dd" },
                    [toggle, panel]);
    spec.nodes = { wrap, toggle, panel, value, chips };
    bar.appendChild(wrap);
  }
}

/** Repaint one dropdown from its set, then re-check whether Apply has work. */
function refreshFilter(spec) {
  const set = state.sel[spec.key];
  for (const [value, chip] of spec.nodes.chips) {
    chip.setAttribute("aria-pressed", String(set.has(value)));
  }
  spec.nodes.value.textContent = filterSummary(spec);
  const open = spec.nodes.panel.hidden === false;
  spec.nodes.wrap.setAttribute("class",
    ["dd", spec.list ? "dd-list" : "", set.size ? "set" : "", open ? "open" : ""]
      .filter(Boolean).join(" "));
  syncFilterActions();
}

/** Apply is live only while the bar differs from what the panels were drawn
    against; Clear only while something is selected. */
function syncFilterActions() {
  $("#filters-apply").disabled =
    readFilters().toString() === state.filters.toString();
  $("#filters-clear").disabled =
    !FILTER_SPECS.some((spec) => state.sel[spec.key].size);
}

function setDropdownOpen(spec, open) {
  spec.nodes.panel.hidden = !open;
  spec.nodes.toggle.setAttribute("aria-expanded", String(open));
  refreshFilter(spec);
}

function closeDropdowns() {
  for (const spec of FILTER_SPECS) {
    if (spec.nodes && spec.nodes.panel.hidden === false) {
      setDropdownOpen(spec, false);
    }
  }
}

function toggleDropdown(spec) {
  const wasOpen = spec.nodes.panel.hidden === false;
  closeDropdowns();
  if (!wasOpen) setDropdownOpen(spec, true);
}

/** Walked by hand rather than with closest(): a click anywhere else on the page
    closes the open panel, and the toggle's own handler has already run by then. */
function insideFilterBar(node) {
  for (let n = node; n; n = n.parentNode) {
    if (n.id === "filters") return true;
  }
  return false;
}

function clearFilters() {
  for (const spec of FILTER_SPECS) {
    state.sel[spec.key] = new Set();
    refreshFilter(spec);
  }
  applyFilters();
}

/** Reload the open tab and forget the others, so they refetch when opened. */
function applyFilters() {
  state.filters = readFilters();
  closeDropdowns();
  syncFilterActions();
  state.data = {};
  state.tripPage = 1;
  state.selectedTrip = null;
  $("#trip-detail").hidden = true;
  // A push-down timing belongs to the selection it was measured under, so drop it
  // rather than leave it sitting above a different one.
  for (const id of ["#chart-pushdown", "#table-pushdown"]) {
    $(id).hidden = true;
    $(id).innerHTML = "";
  }
  $("#pushdown-status").textContent = "";
  loadTab(state.tab);
}

const ENDPOINTS = {
  overview: ["api/overview", renderOverview],
  geography: ["api/geography", renderGeography],
  quality: ["api/quality", renderQuality],
};

async function loadTab(name) {
  state.tab = name;
  // In the URL so a tab survives a reload; replaceState rather than assigning
  // location.hash, which would stack up history entries.
  history.replaceState(null, "", "#" + name);
  for (const button of $$(".tabs [role=tab]")) {
    button.setAttribute("aria-selected", String(button.dataset.tab === name));
  }
  for (const panel of $$(".tab")) {
    panel.hidden = panel.id !== `panel-${name}`;
  }

  if (name === "trips") return loadTrips();
  // The push-down comparison is never run on load: its slow half is the
  // measurement, not a side effect of opening a tab.
  if (name === "pushdown" || state.data[name]) return;

  const [path, render] = ENDPOINTS[name] || [];
  if (!path) return;
  showLoading(`#panel-${name}`);
  try {
    const data = await api(withFilters(path));
    state.data[name] = data;
    render(data);
  } catch (error) {
    showError(`#panel-${name}`, error);
  }
}

async function loadTrips() {
  const params = new URLSearchParams(state.filters);
  params.set("page", String(state.tripPage));
  params.set("order", $("#trip-order").value);
  params.set("show", $("#trip-show").value);
  if ($("#trip-rule").value) params.set("rule", $("#trip-rule").value);
  const host = $("#trip-table");
  host.innerHTML = "";
  host.appendChild(el("p", { class: "loading", text: "Querying IRIS..." }));
  try {
    const data = await api(`api/trips?${params}`);
    state.data.trips = data;
    renderTrips(data);
  } catch (error) {
    host.innerHTML = "";
    host.appendChild(el("pre", { class: "error", text: error.message }));
  }
}

function showLoading(selector) {
  for (const panel of $$(`${selector} .panel`)) {
    panel.innerHTML = "";
    panel.appendChild(el("p", { class: "loading", text: "Querying IRIS..." }));
  }
}

function showError(selector, error) {
  const host = $(selector);
  const first = host.querySelector(".panel") || host;
  first.innerHTML = "";
  first.appendChild(el("h2", { text: "That query failed" }));
  first.appendChild(el("pre", { class: "error", text: error.message }));
}

/** Charts are drawn at a measured pixel width, so a resize redraws rather than
    rescales -- otherwise the 11px labels scale with the box. */
function redraw() {
  const render = (ENDPOINTS[state.tab] || [])[1];
  if (render && state.data[state.tab]) render(state.data[state.tab]);
  if (state.tab === "trips" && state.data.trips) renderTrips(state.data.trips);
  if (state.tab === "pushdown" && state.data.pushdown) {
    renderPushdown(state.data.pushdown);
  }
}

async function boot() {
  try {
    state.meta = await api("api/meta");
  } catch (error) {
    return showError("main", error);
  }
  const meta = state.meta;

  // $("#summary").textContent =
  //   `${fmtInt(meta.totals.trips)} trips · ${fmtInt(meta.totals.zones)} zones `
  //   + `· ${fmtInt(meta.totals.rules)} quality rules · `
  //   + `${fmtInt(meta.totals.flagged)} trips carry at least one flag`;
  // $("#footer-line").textContent =
  //   `${meta.mode.label}. Every figure is computed by one SQL statement inside `
  //   + `IRIS. Pickup timestamps span ${meta.span.first} to ${meta.span.last}; the `
  //   + `earliest is outside 2023, which is what the pickup_outside_2023 rule `
  //   + `catches.`;

  buildFilterBar(meta);
  fillSelect("#trip-rule", meta.rules, "Any rule");
  fillSelect("#pushdown-case", meta.pushdown_cases);

  $("#filters").addEventListener("submit", (event) => {
    event.preventDefault();
    applyFilters();
  });
  $("#filters-clear").addEventListener("click", clearFilters);
  document.addEventListener("click", (event) => {
    if (!insideFilterBar(event.target)) closeDropdowns();
  });
  document.addEventListener("keydown", (event) => {
    if (event.key !== "Escape") return;
    // Escape hands focus back to the button that opened the panel, so keyboard
    // use does not land back at the top of the page.
    const open = FILTER_SPECS.find((spec) => spec.nodes.panel.hidden === false);
    closeDropdowns();
    if (open && open.nodes.toggle.focus) open.nodes.toggle.focus();
  });

  for (const button of $$(".tabs [role=tab]")) {
    button.addEventListener("click", () => loadTab(button.dataset.tab));
  }
  for (const id of ["#trip-order", "#trip-show", "#trip-rule"]) {
    $(id).addEventListener("change", () => { state.tripPage = 1; loadTrips(); });
  }
  $("#trip-prev").addEventListener("click", () => {
    state.tripPage = Math.max(1, state.tripPage - 1);
    loadTrips();
  });
  $("#trip-next").addEventListener("click", () => {
    state.tripPage += 1;
    loadTrips();
  });
  $("#pushdown-run").addEventListener("click", runPushdown);

  let resizeTimer = null;
  window.addEventListener("resize", () => {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(redraw, 180);
  });

  state.filters = readFilters();
  syncFilterActions();
  const requested = location.hash.replace("#", "");
  loadTab(TABS.includes(requested) ? requested : "overview");
}

boot();
