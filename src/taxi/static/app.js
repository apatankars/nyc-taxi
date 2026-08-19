/* Dashboard front-end: no framework, no build step, no CDN.
 *
 * Three reasons it is hand-rolled rather than built on a chart library:
 *
 *  1. It has to work on a laptop with no network, in a room, on demand. A CDN
 *     script tag is a single point of failure five minutes before a demo.
 *  2. The charts needed here are a column chart, a line chart, a heatmap and a
 *     ranked bar list. That is about 200 lines of SVG.
 *  3. Every mark spec the design system asks for -- thin bars, 4px rounded
 *     data-ends, 2px lines, a 2px surface gap between fills, recessive grid --
 *     is easier to satisfy directly than to talk a library out of its defaults.
 *
 * Colour is never written here as a hex. Marks reference the CSS custom
 * properties in styles.css by role, so light and dark swap in one place and the
 * palette stays auditable in a single file.
 */

"use strict";

const SVG_NS = "http://www.w3.org/2000/svg";

/* ------------------------------------------------------------------ utils */

const fmtInt = (v) =>
  v === null || v === undefined ? "—" : Math.round(v).toLocaleString("en-US");

const fmtNum = (v, dp = 1) =>
  v === null || v === undefined
    ? "—"
    : Number(v).toLocaleString("en-US", {
        minimumFractionDigits: dp,
        maximumFractionDigits: dp,
      });

const fmtMoney = (v, dp = 2) =>
  v === null || v === undefined ? "—" : "$" + fmtNum(v, dp);

const fmtPct = (v, dp = 1) =>
  v === null || v === undefined ? "—" : fmtNum(v, dp) + "%";

/** Compact axis ticks: 12,345 -> "12k". Keeps the left gutter narrow. */
function fmtCompact(v) {
  const n = Math.abs(v);
  if (n >= 1e9) return (v / 1e9).toFixed(n % 1e9 === 0 ? 0 : 1) + "b";
  if (n >= 1e6) return (v / 1e6).toFixed(n % 1e6 === 0 ? 0 : 1) + "m";
  if (n >= 1e3) return (v / 1e3).toFixed(n % 1e3 === 0 ? 0 : 1) + "k";
  return String(Math.round(v * 100) / 100);
}

function el(tag, attrs = {}, children = []) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") node.className = v;
    else if (k === "html") node.innerHTML = v;
    else if (k === "text") node.textContent = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else node.setAttribute(k, v);
  }
  for (const child of [].concat(children)) {
    if (child) node.append(child);
  }
  return node;
}

function svg(tag, attrs = {}) {
  const node = document.createElementNS(SVG_NS, tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === null || v === undefined || v === false) continue;
    node.setAttribute(k, v);
  }
  return node;
}

/** Title-case a snake_case key for table headers and labels. */
function prettify(key) {
  const overrides = {
    pct: "%",
    pct_of_trips: "% of trips",
    avg_mph: "Avg mph",
    avg_tip_pct: "Avg tip %",
    fare_per_mile: "Fare / mile",
    od: "OD",
    iris_seconds: "IRIS seconds",
    python_seconds: "Python seconds",
    iris_rows_moved: "IRIS rows moved",
    python_rows_moved: "Python rows moved",
    rows_ratio: "Rows ratio",
    same_answer: "Same answer",
    rows_per_second: "Rows / second",
    projected_full_file_seconds: "Projected full file (s)",
  };
  if (overrides[key]) return overrides[key];
  return key.replace(/_/g, " ").replace(/^./, (c) => c.toUpperCase());
}

/* ---------------------------------------------------------------- tooltip */

const tooltip = {
  node: document.getElementById("tooltip"),
  show(event, title, rows) {
    this.node.replaceChildren(
      el("div", { class: "t-title", text: title }),
      ...rows.map(([label, value]) =>
        el("div", { class: "t-row" }, [
          el("span", { text: label }),
          el("span", { class: "v", text: value }),
        ])
      )
    );
    this.node.classList.add("on");
    this.move(event);
  },
  move(event) {
    const box = this.node.getBoundingClientRect();
    let x = event.clientX + 14;
    let y = event.clientY + 14;
    if (x + box.width > window.innerWidth - 8) x = event.clientX - box.width - 14;
    if (y + box.height > window.innerHeight - 8) y = event.clientY - box.height - 14;
    this.node.style.left = Math.max(8, x) + "px";
    this.node.style.top = Math.max(8, y) + "px";
  },
  hide() {
    this.node.classList.remove("on");
  },
};

/* ------------------------------------------------------- chart primitives */

/**
 * Render on mount and on width change, so charts survive a resize.
 *
 * Height is not fixed at call time: `data-height` on the host overrides it, which
 * is how the expand button gets a genuinely re-laid-out chart rather than a
 * scaled-up one. The last-drawn size is keyed on both dimensions so a height
 * change alone still triggers a redraw.
 */
function mountChart(host, draw, height) {
  let lastSize = "";
  const run = () => {
    const width = host.clientWidth;
    const h = Number(host.dataset.height) || height;
    const size = width + "x" + h;
    if (width < 40 || size === lastSize) return;
    lastSize = size;
    host.replaceChildren(draw(width, h));
  };
  // Let the expand/collapse path force a redraw even at an unchanged width.
  host.redraw = () => {
    lastSize = "";
    run();
  };
  run();
  new ResizeObserver(() => requestAnimationFrame(run)).observe(host);
  // The first run can land before layout settles (hidden tab, web font).
  requestAnimationFrame(run);
}

/**
 * Round tick values covering [0, max], so the axis reads 0/5k/10k not 0/4.3k.
 *
 * The last tick must be >= max, not <= it: the charts scale their marks against
 * it, so a top tick below the maximum draws the tallest bar past the top of the
 * plot. Hence the explicit ceil rather than accumulating until we pass max.
 */
function niceTicks(max, count = 4) {
  if (!(max > 0)) return [0, 1];
  const rough = max / count;
  const magnitude = Math.pow(10, Math.floor(Math.log10(rough)));
  const step = [1, 2, 2.5, 5, 10].map((m) => m * magnitude).find((s) => s >= rough);
  const top = Math.ceil(max / step - 1e-9) * step;
  const ticks = [];
  for (let i = 0; i * step <= top + step * 1e-6; i++) {
    ticks.push(Math.round(i * step * 1e6) / 1e6);
  }
  return ticks;
}

/** A bar with a 4px rounded data-end and a square end at the baseline. */
function barPathUp(x, y, w, h, r = 4) {
  const rr = Math.max(0, Math.min(r, w / 2, h));
  const bottom = y + h;
  return `M${x},${bottom} L${x},${y + rr} Q${x},${y} ${x + rr},${y} ` +
    `L${x + w - rr},${y} Q${x + w},${y} ${x + w},${y + rr} L${x + w},${bottom} Z`;
}

/**
 * Column chart. One series or two; two gets a legend from the caller.
 *
 * opts: { rows, xKey, series:[{key,label,color}], xLabel(row,i), valueFmt,
 *         tooltipTitle(row), tooltipRows(row), height, labelEvery }
 */
function columnChart(host, opts) {
  const rows = opts.rows || [];
  const series = opts.series;
  const valueFmt = opts.valueFmt || fmtInt;

  mountChart(host, (width, height) => {
    const padL = 46, padR = 10, padT = opts.directLabels ? 22 : 12, padB = 26;
    const plotW = Math.max(20, width - padL - padR);
    const plotH = Math.max(20, height - padT - padB);
    const root = svg("svg", {
      viewBox: `0 0 ${width} ${height}`,
      width, height, role: "img",
    });

    const values = rows.flatMap((r) => series.map((s) => Number(r[s.key]) || 0));
    const ticks = niceTicks(Math.max(...values, 0));
    const scaleMax = ticks[ticks.length - 1] || 1;
    const yOf = (v) => padT + plotH - (v / scaleMax) * plotH;

    // Recessive grid, drawn first so marks sit on top of it.
    for (const t of ticks) {
      const y = yOf(t);
      const label = svg("text", { class: "tick", x: padL - 8, y: y + 4, "text-anchor": "end" });
      label.textContent = fmtCompact(t);
      root.append(
        svg("line", {
          class: t === 0 ? "baseline" : "gridline",
          x1: padL, x2: padL + plotW, y1: y, y2: y,
        }),
        label
      );
    }

    const n = rows.length || 1;
    const bandW = plotW / n;
    const nS = series.length;
    const barW = Math.max(2, Math.min(24, bandW * 0.72 / nS - (nS > 1 ? 2 : 0)));
    const groupW = barW * nS + 2 * (nS - 1);

    // Show every k-th x label, so ticks never collide at narrow widths.
    const every = opts.labelEvery || Math.max(1, Math.ceil(n / Math.floor(plotW / 38)));

    rows.forEach((row, i) => {
      const bandX = padL + bandW * i;
      const startX = bandX + (bandW - groupW) / 2;
      const marks = [];

      series.forEach((s, j) => {
        const value = Number(row[s.key]) || 0;
        const x = startX + j * (barW + 2); // 2px surface gap between adjacent fills
        const y = yOf(value);
        const h = Math.max(value > 0 ? 1.5 : 0, padT + plotH - y);
        const mark = svg("path", {
          class: "mark",
          d: barPathUp(x, y, barW, h),
          fill: s.color,
        });
        marks.push(mark);
        root.append(mark);

        if (opts.directLabels) {
          const label = svg("text", {
            class: "datalabel",
            x: x + barW / 2,
            y: y - 6,
            "text-anchor": "middle",
          });
          label.textContent = valueFmt(value);
          root.append(label);
        }
      });

      if (i % every === 0) {
        const label = svg("text", {
          class: "tick",
          x: bandX + bandW / 2,
          y: padT + plotH + 16,
          "text-anchor": "middle",
        });
        label.textContent = opts.xLabel ? opts.xLabel(row, i) : row[opts.xKey];
        root.append(label);
      }

      // Hit target spans the whole band, not just the bar.
      const hit = svg("rect", {
        x: bandX, y: padT, width: bandW, height: plotH, fill: "transparent",
      });
      hit.addEventListener("pointerenter", (e) => {
        marks.forEach((m) => m.classList.add("active"));
        tooltip.show(e, opts.tooltipTitle(row), opts.tooltipRows(row));
      });
      hit.addEventListener("pointermove", (e) => tooltip.move(e));
      hit.addEventListener("pointerleave", () => {
        marks.forEach((m) => m.classList.remove("active"));
        tooltip.hide();
      });
      root.append(hit);
    });

    return root;
  }, opts.height || 200);
}

/**
 * Line chart with a crosshair. One measure only -- a second measure of a
 * different scale becomes a second chart, never a second y-axis.
 */
function lineChart(host, opts) {
  const rows = opts.rows || [];
  mountChart(host, (width, height) => {
    const padL = 46, padR = 10, padT = 12, padB = 26;
    const plotW = Math.max(20, width - padL - padR);
    const plotH = Math.max(20, height - padT - padB);
    const root = svg("svg", { viewBox: `0 0 ${width} ${height}`, width, height, role: "img" });

    const values = rows.map((r) => Number(r[opts.yKey]) || 0);
    const rawMax = Math.max(...values, 0);
    const ticks = niceTicks(rawMax);
    const scaleMax = ticks[ticks.length - 1] || 1;
    const yOf = (v) => padT + plotH - (v / scaleMax) * plotH;
    const xOf = (i) => padL + (rows.length < 2 ? plotW / 2 : (plotW * i) / (rows.length - 1));

    for (const t of ticks) {
      const y = yOf(t);
      root.append(
        svg("line", { class: t === 0 ? "baseline" : "gridline", x1: padL, x2: padL + plotW, y1: y, y2: y })
      );
      const label = svg("text", { class: "tick", x: padL - 8, y: y + 4, "text-anchor": "end" });
      label.textContent = fmtCompact(t);
      root.append(label);
    }

    const points = rows.map((r, i) => [xOf(i), yOf(Number(r[opts.yKey]) || 0)]);
    root.append(
      svg("polyline", {
        class: "mark",
        points: points.map(([x, y]) => `${x},${y}`).join(" "),
        fill: "none",
        stroke: opts.color,
        "stroke-width": 2,
        "stroke-linejoin": "round",
        "stroke-linecap": "round",
      })
    );

    const every = Math.max(1, Math.ceil(rows.length / Math.floor(plotW / 38)));
    rows.forEach((row, i) => {
      if (i % every !== 0) return;
      const label = svg("text", {
        class: "tick", x: xOf(i), y: padT + plotH + 16, "text-anchor": "middle",
      });
      label.textContent = opts.xLabel ? opts.xLabel(row, i) : row[opts.xKey];
      root.append(label);
    });

    const crosshair = svg("line", { class: "crosshair", y1: padT, y2: padT + plotH, opacity: 0 });
    // 8px diameter marker with a 2px surface ring, per the mark spec.
    const marker = svg("circle", { class: "marker", r: 4, fill: opts.color, opacity: 0 });
    root.append(crosshair, marker);

    const surface = svg("rect", {
      x: padL, y: padT, width: plotW, height: plotH, fill: "transparent",
    });
    surface.addEventListener("pointermove", (event) => {
      const box = root.getBoundingClientRect();
      const localX = ((event.clientX - box.left) / box.width) * width;
      const ratio = (localX - padL) / plotW;
      const i = Math.max(0, Math.min(rows.length - 1, Math.round(ratio * (rows.length - 1))));
      const [x, y] = points[i];
      crosshair.setAttribute("x1", x);
      crosshair.setAttribute("x2", x);
      crosshair.setAttribute("opacity", 1);
      marker.setAttribute("cx", x);
      marker.setAttribute("cy", y);
      marker.setAttribute("opacity", 1);
      tooltip.show(event, opts.tooltipTitle(rows[i]), opts.tooltipRows(rows[i]));
    });
    surface.addEventListener("pointerleave", () => {
      crosshair.setAttribute("opacity", 0);
      marker.setAttribute("opacity", 0);
      tooltip.hide();
    });
    root.append(surface);

    return root;
  }, opts.height || 200);
}

/** Sequential heatmap. One hue, seven steps, near-zero recedes to the surface. */
function heatmap(host, opts) {
  const { days, hours, matrix } = opts.data;
  const max = Math.max(...matrix.flat(), 1);
  const RAMP = 7;

  mountChart(host, (width, height) => {
    const padL = 34, padR = 6, padT = 16, padB = 6;
    const plotW = Math.max(20, width - padL - padR);
    // Cells are 20px tall normally; when expanded the host states a height and
    // the rows grow to fill it rather than leaving the extra space blank.
    const cellH = height > 0
      ? Math.max(20, (height - padT - padB) / days.length)
      : 20;
    const totalH = padT + days.length * cellH + padB;
    const cellW = plotW / hours.length;
    const root = svg("svg", {
      viewBox: `0 0 ${width} ${totalH}`, width, height: totalH, role: "img",
    });

    hours.forEach((h, j) => {
      if (j % 3 !== 0) return;
      const label = svg("text", {
        class: "tick", x: padL + cellW * (j + 0.5), y: padT - 5, "text-anchor": "middle",
      });
      label.textContent = String(h).padStart(2, "0");
      root.append(label);
    });

    days.forEach((day, i) => {
      const y = padT + i * cellH;
      const label = svg("text", { class: "tick", x: padL - 7, y: y + cellH / 2 + 4, "text-anchor": "end" });
      label.textContent = day.slice(0, 3);
      root.append(label);

      hours.forEach((hour, j) => {
        const value = matrix[i][j];
        const step = Math.max(1, Math.min(RAMP, Math.ceil((value / max) * RAMP)));
        // 2px surface gap comes from the cell's stroke, set in CSS.
        const cell = svg("rect", {
          class: "cell mark",
          x: padL + cellW * j + 1,
          y: y + 1,
          width: Math.max(1, cellW - 2),
          height: cellH - 2,
          rx: 2,
          fill: `var(--ramp-${step})`,
        });
        cell.addEventListener("pointerenter", (e) => {
          cell.classList.add("active");
          tooltip.show(e, `${day} at ${String(hour).padStart(2, "0")}:00`, [
            ["Trips", fmtInt(value)],
            ["Share of busiest hour", fmtPct((value / max) * 100)],
          ]);
        });
        cell.addEventListener("pointermove", (e) => tooltip.move(e));
        cell.addEventListener("pointerleave", () => {
          cell.classList.remove("active");
          tooltip.hide();
        });
        root.append(cell);
      });
    });

    return root;
  }, 0);
}

/**
 * Ranked horizontal bars, in HTML rather than SVG: the labels are long, wrap
 * awkwardly and need ellipsis, all of which CSS does properly and SVG does not.
 */
function rankedBars(host, opts) {
  const rows = opts.rows || [];
  const valueOf = (row) => Number(opts.value(row)) || 0;
  const max = Math.max(...rows.map(valueOf), 1);
  const valueFmt = opts.valueFmt || fmtInt;

  const list = el("div", { class: "ranked" });
  for (const row of rows) {
    const value = valueOf(row);
    const bar = el("div", {
      class: "bar",
      style: `width:${Math.max(0.6, (value / max) * 100)}%;background:${
        opts.color ? opts.color(row) : "var(--series-1)"
      }`,
    });
    const line = el("div", { class: "row" + (opts.onClick ? " clickable" : "") }, [
      opts.name(row),
      el("div", { class: "track" }, [bar]),
      el("div", { class: "val", text: valueFmt(value) }),
    ]);
    line.addEventListener("pointerenter", (e) =>
      tooltip.show(e, opts.tooltipTitle(row), opts.tooltipRows(row))
    );
    line.addEventListener("pointermove", (e) => tooltip.move(e));
    line.addEventListener("pointerleave", () => tooltip.hide());
    if (opts.onClick) {
      line.tabIndex = 0;
      line.addEventListener("click", () => opts.onClick(row, line));
      line.addEventListener("keydown", (e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          opts.onClick(row, line);
        }
      });
    }
    list.append(line);
  }
  host.replaceChildren(list);
}

/* ------------------------------------------------------------ table view */

function dataTable(rows, options = {}) {
  if (!rows || !rows.length) {
    return el("p", { class: "status-msg", text: "No rows." });
  }
  const columns = options.columns || Object.keys(rows[0]);
  const format = options.format || {};

  // Alignment is decided per *column*, not per cell, and the header follows it.
  // Deciding per cell means a NULL rendered as an em dash silently jumps to the
  // left of an otherwise right-aligned column, and a left-aligned header sits
  // over digits that end at the far edge of the column.
  //
  // "Looks numeric" rather than "is a number", because IRIS hands some NUMERIC
  // columns back as strings; a column of digits should line up either way. It
  // takes every non-null value agreeing, so a zone name that happens to be "5"
  // cannot drag a column of names to the right.
  //
  // Thousands separators are stripped before the test. The row viewer shows the
  // CSV's own text, and one "2,555.47" among the distances was enough to fail the
  // column and left-align every number in it under a right-aligned header.
  const looksNumeric = (v) =>
    typeof v === "number" ||
    (typeof v === "string" &&
      v.trim() !== "" &&
      Number.isFinite(Number(v.replace(/,/g, ""))));
  const present = (v) => v !== null && v !== undefined;
  const numericCols = new Set(
    columns.filter((c) => {
      const values = rows.map((r) => r[c]).filter(present);
      return values.length > 0 && values.every(looksNumeric);
    })
  );

  const head = el(
    "tr",
    {},
    columns.map((c) =>
      el("th", { class: numericCols.has(c) ? "num" : null, text: prettify(c) })
    )
  );
  // options.markCells(row) -> the column names worth pointing at on that row.
  // Per row rather than per column, because which cell matters depends on which
  // rule fired, and that differs from row to row.
  const markCells = options.markCells;
  const body = rows.map((row) => {
    const marked = markCells ? new Set(markCells(row)) : null;
    return el(
      "tr",
      {},
      columns.map((c) => {
        const raw = row[c];
        const text = format[c]
          ? format[c](raw, row)
          : raw === null || raw === undefined
          ? "—"
          : typeof raw === "number"
          ? fmtNum(raw, Number.isInteger(raw) ? 0 : 2)
          : String(raw);
        const classes = [
          numericCols.has(c) ? "num" : null,
          marked && marked.has(c) ? "tested" : null,
        ].filter(Boolean);
        return el("td", { class: classes.join(" ") || null, text });
      })
    );
  });

  return el("div", { class: "table-wrap" }, [
    el("table", { class: "data" }, [
      el("thead", {}, [head]),
      el("tbody", {}, body),
    ]),
  ]);
}

/* ----------------------------------------------------------- expand overlay */

/* One figure at a time is lifted out of the page and into #overlay. The card is
 * moved, not cloned: a clone would lose the ResizeObserver its charts hang off
 * and redraw at the wrong size. A placeholder holds the card's place in the grid
 * so the page behind does not reflow while the overlay is up. */
let expanded = null;

/** Fill most of the viewport, but never demand a height the window cannot give. */
function expandedPlotHeight() {
  return Math.round(Math.max(240, Math.min(660, window.innerHeight * 0.6)));
}

function collapseFigure() {
  if (!expanded) return;
  const { card, slot, plot, button } = expanded;
  expanded = null;
  card.classList.remove("expanded");
  if (plot) {
    delete plot.dataset.height;
    if (plot.redraw) plot.redraw();
  }
  slot.replaceWith(card);
  const overlay = document.getElementById("overlay");
  overlay.hidden = true;
  overlay.replaceChildren();
  document.body.classList.remove("modal-open");
  if (button) {
    button.textContent = "Expand";
    button.setAttribute("aria-expanded", "false");
  }
}

function expandFigure(card, plot, button) {
  collapseFigure();
  const slot = el("div", { class: "card-slot" });
  card.replaceWith(slot);
  card.classList.add("expanded");
  const overlay = document.getElementById("overlay");
  overlay.replaceChildren(card);
  overlay.hidden = false;
  document.body.classList.add("modal-open");
  expanded = { card, slot, plot, button };
  if (button) {
    button.textContent = "Close";
    button.setAttribute("aria-expanded", "true");
  }
  // Width comes from the overlay's layout; height has to be stated, or the chart
  // would sit at its small-card height inside a much taller card.
  if (plot) {
    plot.dataset.height = expandedPlotHeight();
    if (plot.redraw) plot.redraw();
  }
}

/**
 * The figure shell: title, optional legend, plot host, and a footer with the row
 * count, an "Expand" button, and a "Table" button that reveals the same numbers
 * as text -- identity and value are never colour-only.
 */
function figure(opts) {
  const plot = el("div", { class: "plot" });
  const caption = el("figcaption", {}, [
    el("div", { class: "title", text: opts.title }),
    opts.subtitle ? el("div", { class: "subtitle", text: opts.subtitle }) : null,
  ]);

  const parts = [caption];
  if (opts.legend && opts.legend.length >= 2) {
    parts.push(
      el(
        "div",
        { class: "legend" },
        opts.legend.map((s) =>
          el("span", {}, [
            el("i", { style: `background:${s.color}` }),
            el("span", { text: s.label }),
          ])
        )
      )
    );
  }
  parts.push(plot);
  if (opts.rampLegend) {
    parts.push(
      el("div", { class: "ramp-legend" }, [
        el("span", { text: "0" }),
        el("div", { class: "strip" }),
        el("span", { text: fmtInt(opts.rampLegend) }),
        el("span", { text: "trips" }),
      ])
    );
  }

  const card = el("figure", { class: "card" + (opts.full ? " full" : "") }, parts);

  const actions = el("div", { class: "fig-actions" });
  const foot = el("div", { class: "fig-foot" }, [
    el("span", { class: "note-text", text: opts.footNote || "" }),
    actions,
  ]);
  const tail = [foot];

  if (opts.tableRows) {
    const wrap = el("div", { hidden: true });
    const toggle = el("button", {
      type: "button",
      text: "Table",
      "aria-expanded": "false",
    });
    toggle.addEventListener("click", () => {
      const showing = !wrap.hidden;
      wrap.hidden = showing;
      toggle.setAttribute("aria-expanded", String(!showing));
      toggle.textContent = showing ? "Table" : "Hide table";
      if (!showing && !wrap.childElementCount) {
        wrap.append(dataTable(opts.tableRows, opts.tableOptions));
      }
    });
    actions.append(toggle);
    tail.push(wrap);
  }

  const expand = el("button", {
    type: "button",
    text: "Expand",
    "aria-expanded": "false",
    "aria-label": `Show "${opts.title}" larger`,
  });
  expand.addEventListener("click", () => {
    if (expanded && expanded.card === card) collapseFigure();
    else expandFigure(card, plot, expand);
  });
  actions.append(expand);

  card.append(...tail);
  return { card, plot };
}

function statTile(label, value, hint) {
  return el("div", { class: "tile" }, [
    el("div", { class: "label", text: label }),
    el("div", { class: "value", text: value }),
    hint ? el("div", { class: "hint", text: hint }) : null,
  ]);
}

/* ------------------------------------------------------------------ fetch */

async function getJSON(url, init) {
  const response = await fetch(url, init);
  const payload = await response.json().catch(() => ({ error: response.statusText }));
  if (!response.ok) throw new Error(payload.error || `HTTP ${response.status}`);
  return payload;
}

function fail(host, error) {
  host.replaceChildren(
    el("p", { class: "status-msg bad", text: `Could not load: ${error.message}` }),
    el("p", {
      class: "status-msg",
      text: "Is the container up, and has `python -m taxi.cli pipeline` been run?",
    })
  );
}

function busy(host) {
  host.replaceChildren(el("p", { class: "status-msg", text: "Querying IRIS…" }));
}

const SERIES_1 = "var(--series-1)";
const SERIES_2 = "var(--series-2)";
const SERIES_3 = "var(--series-3)";

/**
 * The footer line every figure carries: how many trip rows the marks above rest
 * on. Different figures rest on different counts by design -- a top-12 ranking
 * covers fewer rows than the borough roll-up beside it -- so the number is summed
 * from the rows actually drawn rather than assumed to be the whole file.
 */
function rowsNote(rows, key = "trips") {
  const total = (rows || []).reduce((sum, r) => sum + (Number(r[key]) || 0), 0);
  return fmtInt(total) + " rows";
}

/* --------------------------------------------------------------- overview */

const CONTRAST_METRICS = [
  { key: "avg_miles", title: "Average trip distance", unit: "miles", fmt: (v) => fmtNum(v, 2) },
  { key: "avg_mph", title: "Average speed", unit: "mph", fmt: (v) => fmtNum(v, 2) },
  { key: "avg_minutes", title: "Average duration", unit: "minutes", fmt: (v) => fmtNum(v, 1) },
  { key: "max_miles", title: "Longest single trip", unit: "miles", fmt: (v) => fmtNum(v, 0) },
];

async function renderOverview(host) {
  busy(host);
  let data;
  try {
    data = await getJSON("/api/overview");
  } catch (error) {
    return fail(host, error);
  }

  const h = data.headline;
  const impact = {};
  for (const row of data.cleaning_impact) impact[row.metric] = row;
  const cleanPct = h.trips ? (h.clean_trips / h.trips) * 100 : 0;

  // measure -> usable rows, so each figure can print its own denominator.
  const usable = {};
  for (const row of data.coverage || []) usable[row.measure] = row.usable_rows;
  // "distance" for one measure, "tip, fare" for two: the binding constraint is
  // the narrowest of them.
  const usableFor = (dependsOn) => {
    const counts = String(dependsOn || "")
      .split(",")
      .map((m) => usable[m.trim()])
      .filter((v) => typeof v === "number");
    return counts.length ? Math.min(...counts) : null;
  };

  const hero = el("div", { class: "hero full" }, [
    el("div", { class: "label", text: "Trips that passed every quality rule" }),
    el("div", { class: "value", text: fmtInt(h.clean_trips) }),
    el("div", {
      class: "caption",
      text:
        `${fmtPct(cleanPct)} of ${fmtInt(h.trips)} rows. ` +
        `The other ${fmtInt(h.trips - h.clean_trips)} are flagged, not deleted.`,
    }),
  ]);

  const tiles = el("div", { class: "grid tiles" }, [
    statTile("Rows in the file", fmtInt(h.trips), "every line accounted for"),
    statTile("Passed all 14 rules", fmtPct(cleanPct), fmtInt(h.clean_trips) + " trips"),
    statTile("Typical trip", fmtNum(h.typical_miles, 2) + " mi", fmtNum(h.typical_minutes, 1) + " min, usable rows only"),
    statTile("Typical fare", fmtMoney(h.typical_fare), "rows with a usable fare"),
    statTile("Total recorded revenue", "$" + fmtCompact(h.total_revenue), "all rows, as recorded"),
    statTile("Pickup zones seen", fmtInt(h.pickup_zones), "of 265 in the lookup"),
  ]);

  // Small multiples rather than one grouped chart: miles, mph and minutes are
  // different units, and putting them on a shared axis would be a dual-scale
  // chart wearing a disguise.
  const contrast = el("div", { class: "grid thirds" });
  for (const metric of CONTRAST_METRICS) {
    const row = impact[metric.key];
    if (!row) continue;
    // Three bars, not two. The middle one drops a row if *any* rule flagged it;
    // the right one drops a row only if a rule bearing on this measure flagged
    // it. Where they disagree, the middle bar is throwing away usable rows.
    const rows = [
      { label: "All rows", value: row.all_rows, note: "No rows excluded" },
      {
        label: "Any flag",
        value: row.strict_clean,
        note: "Excludes every row any of the 14 rules flagged",
      },
      {
        // Kept short because it is an axis label in a 240px card; the line above
        // the small multiples and the tooltip carry the full meaning.
        label: "Related",
        value: row.selective,
        note: `Excludes only rows flagged for ${row.depends_on}`,
      },
    ];
    const rowsUsed = usableFor(row.depends_on);
    const fig = figure({
      title: metric.title,
      subtitle: metric.unit,
      legend: [
        { label: "Unfiltered", color: SERIES_2 },
        { label: "Filtered", color: SERIES_1 },
      ],
      tableRows: rows,
      tableOptions: { format: { value: (v) => metric.fmt(v) } },
      // Just the denominator of the rightmost bar -- the one the dashboard
      // actually uses. The other two counts are the hero number and the tile
      // beside it, and repeating both in all four cards was three lines of
      // wrapped text fighting the buttons for the same row.
      footNote: rowsUsed === null ? "" : fmtInt(rowsUsed) + " usable rows",
    });
    contrast.append(fig.card);
    columnChart(fig.plot, {
      rows,
      xKey: "label",
      series: [{ key: "value", label: metric.title, color: SERIES_1 }],
      directLabels: true,
      valueFmt: metric.fmt,
      height: 170,
      labelEvery: 1,
      xLabel: (r) => r.label,
      tooltipTitle: (r) => r.label,
      tooltipRows: (r) => [
        [metric.title, `${metric.fmt(r.value)} ${metric.unit}`],
        ["Rows", r.note],
      ],
    });
    // Colour follows the entity, not the rank: unfiltered is always slot 2 and
    // both filtered bars are slot 1, in every one of these four charts. Two hues
    // for three bars is deliberate -- the two filters are the same kind of thing,
    // and a third generated hue is never the answer.
    fig.plot.querySelectorAll("path.mark").forEach((mark, i) => {
      mark.setAttribute("fill", i === 0 ? SERIES_2 : SERIES_1);
    });
  }

  const issuesFig = figure({
    title: "Quality rules broken per trip",
    subtitle: "0 means the trip passed all 14",
    full: true,
    tableRows: data.issue_distribution,
    tableOptions: { format: { pct: (v) => fmtPct(v, 2) } },
    footNote: rowsNote(data.issue_distribution),
  });
  columnChart(issuesFig.plot, {
    rows: data.issue_distribution,
    xKey: "issues",
    series: [{ key: "trips", label: "Trips", color: SERIES_1 }],
    height: 190,
    directLabels: data.issue_distribution.length <= 8,
    valueFmt: (v) => fmtCompact(v),
    xLabel: (r) => r.issues,
    tooltipTitle: (r) => (r.issues === 0 ? "No issues" : `${r.issues} issue${r.issues > 1 ? "s" : ""}`),
    tooltipRows: (r) => [["Trips", fmtInt(r.trips)], ["Share", fmtPct(r.pct, 2)]],
  });

  const boroughFig = figure({
    title: "Pickup boroughs, after enrichment",
    subtitle: "Borough and zone come from the lookup join, not from the trip file",
    full: true,
    tableRows: data.boroughs,
    tableOptions: {
      format: {
        avg_fare: (v) => fmtMoney(v),
        avg_total: (v) => fmtMoney(v),
        revenue: (v) => fmtMoney(v, 0),
        avg_tip_pct: (v) => fmtPct(v),
      },
    },
    footNote: rowsNote(data.boroughs),
  });
  rankedBars(boroughFig.plot, {
    rows: data.boroughs,
    name: (r) => el("div", { class: "name", text: r.borough }),
    value: (r) => r.trips,
    tooltipTitle: (r) => r.borough,
    tooltipRows: (r) => [
      ["Trips", fmtInt(r.trips)],
      ["Avg distance", fmtNum(r.avg_miles, 2) + " mi"],
      ["Avg fare", fmtMoney(r.avg_fare)],
      ["Avg tip", fmtPct(r.avg_tip_pct)],
      ["Revenue", fmtMoney(r.revenue, 0)],
    ],
  });

  host.replaceChildren(
    el("div", { class: "grid" }, [hero]),
    tiles,
    // One line, not an essay: the three bars in each small multiple below are the
    // only thing here that needs explaining, and the tooltips repeat it per bar.
    el("p", {
      class: "prose",
      html: "", 
    }),
    contrast,
    el("div", { class: "grid" }, [issuesFig.card, boroughFig.card])
  );
}

/* ---------------------------------------------------------------- quality */

const SEVERITY = {
  error: { color: "var(--status-critical)", glyph: "▲", label: "error" },
  warn: { color: "var(--status-warning)", glyph: "●", label: "warning" },
};

async function renderQuality(host) {
  busy(host);
  let data;
  try {
    data = await getJSON("/api/quality");
  } catch (error) {
    return fail(host, error);
  }

  const sampleHost = el("div", { class: "grid" });

  const rulesFig = figure({
    title: "Rules by rows flagged",
    subtitle: "Select a rule to inspect the rows it caught",
    full: true,
    tableRows: data.rules,
    tableOptions: { format: { pct_of_trips: (v) => fmtPct(v, 3) } },
    footNote: rowsNote(data.issue_distribution) + " checked against 14 rules",
  });

  rankedBars(rulesFig.plot, {
    rows: data.rules,
    // Status colour never travels alone -- every row carries the glyph and word.
    color: (r) => (SEVERITY[r.severity] || SEVERITY.warn).color,
    name: (r) => {
      const meta = SEVERITY[r.severity] || SEVERITY.warn;
      return el("div", { class: "name" }, [
        el("span", { class: `chip ${r.severity}` }, [
          el("span", { class: "glyph", text: meta.glyph }),
          el("span", { text: meta.label }),
        ]),
        el("span", { text: " " + r.rule }),
      ]);
    },
    value: (r) => r.flagged,
    tooltipTitle: (r) => r.rule,
    tooltipRows: (r) => [
      ["Severity", (SEVERITY[r.severity] || SEVERITY.warn).label],
      ["Rows flagged", fmtInt(r.flagged)],
      ["Share of trips", fmtPct(r.pct_of_trips, 3)],
      ["Rule", r.description],
      ["Invalidates", r.invalidates],
    ],
    onClick: (row, node) => {
      rulesFig.plot.querySelectorAll(".row").forEach((n) => n.removeAttribute("aria-selected"));
      node.setAttribute("aria-selected", "true");
      showSample(sampleHost, row);
    },
  });

  // Every figure in the dashboard rests on one of these row counts, and they are
  // deliberately not all the same number. Showing them is what keeps the
  // selective filter honest rather than merely flattering.
  const coverage = data.coverage || [];
  const coverageFig = figure({
    title: "Rows usable per measure",
    subtitle: "The denominator behind every figure in this dashboard",
    full: true,
    tableRows: coverage,
    tableOptions: { format: { pct_of_trips: (v) => fmtPct(v, 2) } },
    footNote:
      "Bottom row is the blunt filter: no rule fired anywhere on the row",
  });
  rankedBars(coverageFig.plot, {
    rows: coverage,
    name: (r) => el("div", { class: "name", text: r.measure }),
    value: (r) => r.usable_rows,
    tooltipTitle: (r) => r.measure,
    tooltipRows: (r) => [
      ["Meaning", r.meaning],
      ["Usable rows", fmtInt(r.usable_rows)],
      ["Share of file", fmtPct(r.pct_of_trips, 2)],
      ["Rules bearing on it", fmtInt(r.rules)],
    ],
  });

  host.replaceChildren(
    el("div", { class: "grid" }, [rulesFig.card]),
    sampleHost,
    el("div", { class: "grid" }, [coverageFig.card])
  );
}

async function showSample(host, rule) {
  host.replaceChildren(el("p", { class: "status-msg", text: `Fetching rows flagged by ${rule.rule}…` }));
  let data;
  try {
    data = await getJSON(`/api/quality/sample?rule=${encodeURIComponent(rule.rule)}&limit=12`);
  } catch (error) {
    return fail(host, error);
  }
  const card = el("figure", { class: "card full" }, [
    el("figcaption", {}, [
      el("div", { class: "title", text: `Rows flagged by ${rule.rule}` }),
      el("div", { class: "subtitle", text: `${rule.description} — ${fmtInt(rule.flagged)} rows in total, longest 12 shown` }),
    ]),
    dataTable(data.rows, {
      format: {
        TripDistance: (v) => fmtNum(v, 2),
        TripMinutes: (v) => fmtNum(v, 1),
        AvgMph: (v) => fmtNum(v, 2),
        FareAmount: (v) => fmtMoney(v),
        TipAmount: (v) => fmtMoney(v),
        TotalAmount: (v) => fmtMoney(v),
      },
    }),
  ]);
  host.replaceChildren(card);
}

/* ------------------------------------------------------------------ zones */

const zoneFmt = {
  avg_miles: (v) => fmtNum(v, 2),
  avg_minutes: (v) => fmtNum(v, 1),
  avg_fare: (v) => fmtMoney(v),
  avg_total: (v) => fmtMoney(v),
  avg_tip_pct: (v) => fmtPct(v),
  fare_per_mile: (v) => fmtMoney(v),
  avg_mph: (v) => fmtNum(v, 1),
  revenue: (v) => fmtMoney(v, 0),
};

async function renderZones(host) {
  busy(host);
  const clean = document.getElementById("zones-clean").checked;
  let data;
  try {
    data = await getJSON(`/api/zones?clean=${clean ? 1 : 0}&limit=12`);
  } catch (error) {
    return fail(host, error);
  }
  function zoneFigure(title, subtitle, rows) {
    const fig = figure({
      title,
      subtitle,
      tableRows: rows,
      tableOptions: { format: zoneFmt },
      footNote: rowsNote(rows),
    });
    rankedBars(fig.plot, {
      rows,
      name: (r) => el("div", { class: "name", title: `${r.zone} (${r.borough})`, text: r.zone }),
      value: (r) => r.trips,
      tooltipTitle: (r) => r.zone,
      tooltipRows: (r) => [
        ["Borough", r.borough],
        ["Trips", fmtInt(r.trips)],
        ["Avg distance", fmtNum(r.avg_miles, 2) + " mi"],
        ["Avg duration", fmtNum(r.avg_minutes, 1) + " min"],
        ["Avg fare", fmtMoney(r.avg_fare)],
        ["Avg tip", fmtPct(r.avg_tip_pct)],
      ],
    });
    return fig.card;
  }

  const fareFig = figure({
    title: "Most expensive zones per mile",
    subtitle: "Summed fare over summed distance, so short trips do not dominate",
    full: true,
    tableRows: data.fare_per_mile,
    tableOptions: { format: zoneFmt },
    footNote: rowsNote(data.fare_per_mile) + " · zones with 1,000+ trips",
  });
  rankedBars(fareFig.plot, {
    rows: data.fare_per_mile,
    name: (r) => el("div", { class: "name", title: `${r.zone} (${r.borough})`, text: r.zone }),
    value: (r) => r.fare_per_mile,
    valueFmt: (v) => fmtMoney(v),
    tooltipTitle: (r) => r.zone,
    tooltipRows: (r) => [
      ["Borough", r.borough],
      ["Fare per mile", fmtMoney(r.fare_per_mile)],
      ["Avg distance", fmtNum(r.avg_miles, 2) + " mi"],
      ["Avg speed", fmtNum(r.avg_mph, 1) + " mph"],
      ["Trips", fmtInt(r.trips)],
    ],
  });

  // The table is the figure here, so no "Table" toggle repeating it below.
  const boroughFig = figure({
    title: "By pickup borough",
    subtitle: "The coarsest view of the enrichment join",
    full: true,
    footNote: rowsNote(data.boroughs),
  });
  boroughFig.plot.append(dataTable(data.boroughs, { format: zoneFmt }));

  host.replaceChildren(
    el("div", { class: "grid" }, [
      zoneFigure("Busiest pickup zones", "Where journeys start", data.pickup),
      zoneFigure("Busiest drop-off zones", "Where they end", data.dropoff),
    ]),
    el("div", { class: "grid" }, [fareFig.card]),
    el("div", { class: "grid" }, [boroughFig.card])
  );
}

/* ------------------------------------------------------------------- time */

async function renderTime(host) {
  busy(host);
  const clean = document.getElementById("time-clean").checked;
  let data;
  try {
    data = await getJSON(`/api/time?clean=${clean ? 1 : 0}`);
  } catch (error) {
    return fail(host, error);
  }
  const hourLabel = (r) => String(r.pickup_hour).padStart(2, "0");

  const hourFig = figure({
    title: "Trips by hour of day",
    subtitle: "Pickup hour, all twelve months combined",
    tableRows: data.hour,
    tableOptions: { format: { avg_mph: (v) => fmtNum(v, 2), avg_minutes: (v) => fmtNum(v, 1), avg_fare: (v) => fmtMoney(v) } },
    footNote: rowsNote(data.hour),
  });
  columnChart(hourFig.plot, {
    rows: data.hour,
    xKey: "pickup_hour",
    series: [{ key: "trips", label: "Trips", color: SERIES_1 }],
    height: 200,
    xLabel: hourLabel,
    tooltipTitle: (r) => hourLabel(r) + ":00",
    tooltipRows: (r) => [
      ["Trips", fmtInt(r.trips)],
      ["Avg speed", fmtNum(r.avg_mph, 2) + " mph"],
      ["Avg duration", fmtNum(r.avg_minutes, 1) + " min"],
      ["Avg fare", fmtMoney(r.avg_fare)],
    ],
  });

  // Speed gets its own chart. Trips and mph on one pair of axes would be a
  // dual-axis chart, which is the single most common way to mislead with one.
  const speedFig = figure({
    title: "Average speed by hour of day",
    subtitle: "mph — the congestion signal, on its own axis",
    tableRows: data.hour,
    tableOptions: { format: { avg_mph: (v) => fmtNum(v, 2), avg_minutes: (v) => fmtNum(v, 1), avg_fare: (v) => fmtMoney(v) } },
    footNote: rowsNote(data.hour),
  });
  lineChart(speedFig.plot, {
    rows: data.hour,
    xKey: "pickup_hour",
    yKey: "avg_mph",
    color: SERIES_1,
    height: 200,
    xLabel: hourLabel,
    tooltipTitle: (r) => hourLabel(r) + ":00",
    tooltipRows: (r) => [
      ["Avg speed", fmtNum(r.avg_mph, 2) + " mph"],
      ["Avg duration", fmtNum(r.avg_minutes, 1) + " min"],
      ["Trips", fmtInt(r.trips)],
    ],
  });

  const dowFig = figure({
    title: "Trips by day of week",
    tableRows: data.day_of_week,
    tableOptions: { format: { avg_miles: (v) => fmtNum(v, 2), avg_fare: (v) => fmtMoney(v), avg_tip_pct: (v) => fmtPct(v) } },
    footNote: rowsNote(data.day_of_week),
  });
  columnChart(dowFig.plot, {
    rows: data.day_of_week,
    xKey: "day",
    series: [{ key: "trips", label: "Trips", color: SERIES_1 }],
    height: 190,
    labelEvery: 1,
    xLabel: (r) => String(r.day).slice(0, 3),
    tooltipTitle: (r) => r.day,
    tooltipRows: (r) => [
      ["Trips", fmtInt(r.trips)],
      ["Avg distance", fmtNum(r.avg_miles, 2) + " mi"],
      ["Avg fare", fmtMoney(r.avg_fare)],
      ["Avg tip", fmtPct(r.avg_tip_pct)],
    ],
  });

  const monthFig = figure({
    title: "Trips by month",
    tableRows: data.month,
    tableOptions: { format: { avg_miles: (v) => fmtNum(v, 2), avg_fare: (v) => fmtMoney(v), revenue: (v) => fmtMoney(v, 0) } },
    footNote: rowsNote(data.month),
  });
  columnChart(monthFig.plot, {
    rows: data.month,
    xKey: "month_name",
    series: [{ key: "trips", label: "Trips", color: SERIES_1 }],
    height: 190,
    labelEvery: 1,
    xLabel: (r) => r.month_name,
    tooltipTitle: (r) => r.month_name,
    tooltipRows: (r) => [
      ["Trips", fmtInt(r.trips)],
      ["Avg distance", fmtNum(r.avg_miles, 2) + " mi"],
      ["Avg fare", fmtMoney(r.avg_fare)],
      ["Revenue", fmtMoney(r.revenue, 0)],
    ],
  });

  const peak = Math.max(...data.heatmap.matrix.flat(), 1);
  const heatRows = [];
  data.heatmap.days.forEach((day, i) => {
    data.heatmap.hours.forEach((hour, j) => {
      heatRows.push({ day, hour, trips: data.heatmap.matrix[i][j] });
    });
  });
  const heatFig = figure({
    title: "Day of week × hour of day",
    subtitle: "168 cells, all aggregated in IRIS; pandas only pivots the result",
    full: true,
    rampLegend: peak,
    tableRows: heatRows,
    footNote: rowsNote(heatRows) + " · busiest cell " + fmtInt(peak),
  });
  heatmap(heatFig.plot, { data: data.heatmap });

  host.replaceChildren(
    el("div", { class: "grid" }, [hourFig.card, speedFig.card]),
    el("div", { class: "grid" }, [dowFig.card, monthFig.card]),
    el("div", { class: "grid" }, [heatFig.card])
  );
}

/* ------------------------------------------------------------------ pairs */

async function renderPairs(host) {
  busy(host);
  const clean = document.getElementById("pairs-clean").checked;
  const cross = document.getElementById("pairs-cross").checked;
  let data;
  try {
    data = await getJSON(`/api/pairs?clean=${clean ? 1 : 0}&cross=${cross ? 1 : 0}&limit=15`);
  } catch (error) {
    return fail(host, error);
  }

  const pairs = data.pairs.map((r) => ({
    ...r,
    route: `${r.from_zone} → ${r.to_zone}`,
  }));

  const fig = figure({
    title: cross ? "Busiest cross-borough routes" : "Busiest origin/destination pairs",
    subtitle: "Grouped on four enriched columns at once, ranked in IRIS",
    full: true,
    tableRows: pairs,
    tableOptions: {
      columns: ["from_borough", "from_zone", "to_borough", "to_zone", "trips", "avg_miles", "avg_minutes", "avg_total"],
      format: {
        avg_miles: (v) => fmtNum(v, 2),
        avg_minutes: (v) => fmtNum(v, 1),
        avg_total: (v) => fmtMoney(v),
      },
    },
    footNote:
      rowsNote(pairs) + " in these " + pairs.length + " routes" +
      (cross ? " · cross-borough only" : ""),
  });
  rankedBars(fig.plot, {
    rows: pairs,
    name: (r) => el("div", { class: "name", title: r.route, text: r.route }),
    value: (r) => r.trips,
    tooltipTitle: (r) => r.route,
    tooltipRows: (r) => [
      ["From", `${r.from_zone} (${r.from_borough})`],
      ["To", `${r.to_zone} (${r.to_borough})`],
      ["Trips", fmtInt(r.trips)],
      ["Avg distance", fmtNum(r.avg_miles, 2) + " mi"],
      ["Avg duration", fmtNum(r.avg_minutes, 1) + " min"],
      ["Avg total", fmtMoney(r.avg_total)],
    ],
  });

  let tippingCard = null;
  try {
    const tip = await getJSON("/api/tipping");
    // One table, not two: this figure *is* the table, so it does not also get a
    // "Table" toggle that would render the same rows a second time underneath.
    const tipTable = {
      columns: ["borough", "payment", "trips", "avg_tip_pct", "avg_tip", "avg_fare"],
      format: {
        avg_tip_pct: (v) => fmtPct(v, 2),
        avg_tip: (v) => fmtMoney(v),
        avg_fare: (v) => fmtMoney(v),
      },
    };
    const tipFig = figure({
      title: "Tipping by borough and payment type",
      subtitle: "Cash tips are never metered, so the payment types are kept apart",
      full: true,
      footNote: rowsNote(tip.tipping) + " · combinations of 500+ trips",
    });
    tipFig.plot.append(dataTable(tip.tipping, tipTable));
    tippingCard = tipFig.card;
  } catch (error) {
    tippingCard = el("p", { class: "status-msg bad", text: `Tipping view failed: ${error.message}` });
  }

  host.replaceChildren(
    el("div", { class: "grid" }, [fig.card]),
    el("div", { class: "grid" }, [tippingCard])
  );
}

/* ----------------------------------------------------------------- browse */

/** "—" for a missing value, the formatter for anything else. */
const orDash = (fn) => (v) =>
  v === null || v === undefined || v === "" ? "—" : fn(Number(v));

// Source columns are printed exactly as they arrived in the CSV. Reformatting
// them would destroy the evidence: "2,555.47" is *why* that row was flagged, and
// running it through a number formatter yields either 2555.47 -- which looks
// clean and makes the flag look arbitrary -- or NaN. Only TripMinutes and AvgMph
// are formatted, because those two IRIS computed rather than read.
const verbatim = (v) =>
  v === null || v === undefined || String(v).trim() === "" ? "—" : String(v);

const BROWSE_FORMAT = {
  PickupDateTime: verbatim,
  DropoffDateTime: verbatim,
  PassengerCount: verbatim,
  TripDistance: verbatim,
  FareAmount: verbatim,
  TipAmount: verbatim,
  TotalAmount: verbatim,
  TripMinutes: orDash((v) => fmtNum(v, 1)),
  AvgMph: orDash((v) => fmtNum(v, 2)),
};

const BROWSE_TITLES = {
  flagged: "Flagged rows",
  all: "Trip rows",
  clean: "Rows that passed every rule",
};

const BROWSE_SUBTITLES = {
  flagged: "Most-flagged rows first",
  all: "Most-flagged rows first",
  clean: "Earliest pickups first",
};

// The rule options are appended once, from the payload, so the picker cannot
// drift out of step with the rule registry in quality.py.
let browseRulesLoaded = false;

async function renderBrowse(host) {
  busy(host);
  const select = document.getElementById("browse-rule");
  const selector = select.value;
  const limit = Number(document.getElementById("browse-limit").value) || 25;

  let data;
  try {
    data = await getJSON(
      `/api/rows?rule=${encodeURIComponent(selector)}&limit=${limit}`
    );
  } catch (error) {
    return fail(host, error);
  }

  if (!browseRulesLoaded && data.rules && data.rules.length) {
    browseRulesLoaded = true;
    const group = el("optgroup", { label: "Flagged by one rule" });
    for (const rule of data.rules) {
      group.append(
        el("option", { value: rule.name, text: rule.name, title: rule.description })
      );
    }
    select.append(group);
    select.value = selector;
  }

  // Each row carries a Tested map of rule name -> the cells that rule read on
  // that row, computed in quality.py. So with one rule selected the shading is
  // just that rule's entry, and with none it is the union across the rules that
  // fired. The page never needs to know what any rule tests, and the map is per
  // row, so a flagged trip with a real pickup zone and an unrecognised drop-off
  // shades only the drop-off.
  const rule = (data.rules || []).find((r) => r.name === selector);
  const markCells = (row) => {
    const tested = row.Tested || {};
    return rule ? tested[rule.name] || [] : Object.values(tested).flat();
  };

  // Tested is data for the shading, not a column to print.
  const columns = Object.keys(data.rows[0] || {}).filter((c) => c !== "Tested");
  const anyFlagged = data.rows.some((r) => Number(r.Issues) > 0);
  const fig = figure({
    title: BROWSE_TITLES[selector] || `Rows flagged by ${selector}`,
    subtitle: rule ? rule.description : BROWSE_SUBTITLES[selector] || "",
    full: true,
    footNote:
      fmtInt(data.matching) + " rows match · " +
      fmtInt(data.rows.length) + " shown · values as they arrive in the CSV" +
      (!anyFlagged
        ? ""
        : rule
        ? " · the shaded cell is the value this rule tested"
        : " · a shaded cell is the value one of the row's flags tested"),
  });
  fig.plot.append(
    dataTable(data.rows, { columns, format: BROWSE_FORMAT, markCells })
  );

  host.replaceChildren(el("div", { class: "grid" }, [fig.card]));
}

/* ------------------------------------------------------------------ bench */

async function renderBench(host) {
  const button = document.getElementById("bench-run");
  button.disabled = true;
  button.textContent = "Running…";
  host.replaceChildren(
    el("p", { class: "status-msg", text: "Running all three arms of each comparison…" })
  );

  let data;
  try {
    data = await getJSON("/api/bench?repeat=3&ingest_rows=50000", { method: "POST" });
  } catch (error) {
    button.disabled = false;
    button.textContent = "Re-run the comparison";
    return fail(host, error);
  }
  button.disabled = false;
  button.textContent = "Re-run the comparison";

  const agg = data.aggregation;
  const allAgree = agg.every((r) => r.same_answer);
  // The third arm needs pandas inside the IRIS process. If the server has no
  // pandas the API sends nulls and a reason; the other two arms still stand, so
  // the panel drops to two series rather than refusing to draw.
  const hasEmbedded = agg.some((r) => r.embedded_seconds != null);

  const timeSeries = [
    { key: "iris_seconds", label: "IRIS SQL", color: SERIES_1 },
    { key: "python_seconds", label: "host pandas", color: SERIES_2 },
  ];
  const timeLegend = [
    { label: "GROUP BY in IRIS", color: SERIES_1 },
    { label: "Pulled into pandas on the host", color: SERIES_2 },
  ];
  const timeColumns = ["comparison", "iris_seconds", "python_seconds"];
  if (hasEmbedded) {
    timeSeries.push({ key: "embedded_seconds", label: "embedded", color: SERIES_3 });
    timeLegend.push({ label: "Same pandas, inside IRIS", color: SERIES_3 });
    timeColumns.push("embedded_seconds");
  }
  timeColumns.push("speedup", "same_answer");

  const timeFig = figure({
    title: "Time to answer the same question",
    subtitle: `Median of ${data.repeat} runs, seconds — lower is better`,
    full: true,
    legend: timeLegend,
    tableRows: agg,
    tableOptions: {
      columns: timeColumns,
      format: {
        iris_seconds: (v) => fmtNum(v, 3) + "s",
        python_seconds: (v) => fmtNum(v, 3) + "s",
        embedded_seconds: (v) => (v == null ? "—" : fmtNum(v, 3) + "s"),
        speedup: (v) => fmtNum(v, 1) + "×",
        same_answer: (v) => (v ? "yes" : "NO"),
      },
    },
    footNote: allAgree
      ? `Every answer was asserted equal to the IRIS-side answer before timing was reported (${hasEmbedded ? "three" : "two"} arms)`
      : "WARNING: at least one arm disagreed — see the table",
  });
  columnChart(timeFig.plot, {
    rows: agg,
    xKey: "comparison",
    series: timeSeries,
    height: 210,
    labelEvery: 1,
    valueFmt: (v) => fmtNum(v, 2),
    directLabels: true,
    xLabel: (r) => r.comparison.replace(/_/g, " "),
    tooltipTitle: (r) => r.question || r.comparison,
    tooltipRows: (r) =>
      [
        ["IRIS SQL", fmtNum(r.iris_seconds, 3) + "s"],
        ["Host pandas", fmtNum(r.python_seconds, 3) + "s"],
        hasEmbedded && [
          "Embedded Python",
          r.embedded_seconds == null ? "—" : fmtNum(r.embedded_seconds, 3) + "s",
        ],
        ["pandas ÷ IRIS", fmtNum(r.speedup, 1) + "×"],
        ["Same answer", r.same_answer ? "yes" : "NO"],
      ].filter(Boolean),
  });

  // Rows moved spans five orders of magnitude. A bar chart of 8 against 787,060
  // would draw one invisible mark, so this stays a table -- the honest form.
  const movedColumns = ["comparison", "iris_rows_moved", "python_rows_moved"];
  if (hasEmbedded) movedColumns.push("embedded_rows_moved", "embedded_rows_scanned");
  movedColumns.push("rows_ratio");
  const movedCard = el("figure", { class: "card full" }, [
    el("figcaption", {}, [
      el("div", { class: "title", text: "How much data crossed the driver" }),
      el("div", { class: "subtitle", text: "Not charted on purpose: the ratio spans five orders of magnitude, and a bar for 8 rows beside one for 787,060 would be invisible" }),
    ]),
    dataTable(agg, {
      columns: movedColumns,
      format: {
        rows_ratio: (v) => fmtInt(v) + "×",
        embedded_rows_moved: (v) => (v == null ? "—" : fmtInt(v)),
        embedded_rows_scanned: (v) => (v == null ? "—" : fmtInt(v)),
      },
    }),
  ]);

  // Why the embedded arm does not win: both row-by-row arms spend nearly all
  // their time building the frame, not reducing it. Same table, both splits.
  const splitCard =
    hasEmbedded &&
    el("figure", { class: "card full" }, [
      el("figcaption", {}, [
        el("div", { class: "title", text: "Where the time goes in the two pandas arms" }),
        el("div", {
          class: "subtitle",
          text:
            "Fetching 787,060 rows into a frame dominates both; the reduce itself is " +
            "hundredths of a second. Moving the code into IRIS removes the driver, not the row-by-row cost",
        }),
      ]),
      dataTable(agg, {
        columns: [
          "comparison",
          "python_fetch_seconds",
          "python_reduce_seconds",
          "embedded_fetch_seconds",
          "embedded_reduce_seconds",
        ],
        format: {
          python_fetch_seconds: (v) => fmtNum(v, 3) + "s",
          python_reduce_seconds: (v) => fmtNum(v, 3) + "s",
          embedded_fetch_seconds: (v) => (v == null ? "—" : fmtNum(v, 3) + "s"),
          embedded_reduce_seconds: (v) => (v == null ? "—" : fmtNum(v, 3) + "s"),
        },
      }),
    ]);

  const ingestFig = figure({
    title: "Loading the same rows three ways",
    subtitle: `Rows per second, measured on ${fmtInt(data.ingest_rows)} rows — higher is better`,
    full: true,
    tableRows: data.ingest,
    tableOptions: {
      format: {
        seconds: (v) => fmtNum(v, 3) + "s",
        rows_per_second: (v) => fmtInt(v),
        projected_full_file_seconds: (v) => fmtNum(v, 1) + "s",
      },
    },
    footNote:
      "Projections are extrapolated from a subset and understate LOAD DATA, which " +
      "measures 0.79s on the real full file",
  });
  rankedBars(ingestFig.plot, {
    rows: data.ingest,
    name: (r) => el("div", { class: "name", title: r.approach, text: r.approach }),
    value: (r) => r.rows_per_second,
    valueFmt: (v) => fmtCompact(v) + "/s",
    tooltipTitle: (r) => r.approach,
    tooltipRows: (r) => [
      ["Rows measured", fmtInt(r.rows_measured)],
      ["Elapsed", fmtNum(r.seconds, 3) + "s"],
      ["Rows per second", fmtInt(r.rows_per_second)],
      ["Projected full file", fmtNum(r.projected_full_file_seconds, 1) + "s"],
    ],
  });

  host.replaceChildren(
    el("p", {
      class: "prose",
      html:
        `Over <strong>${fmtInt(data.total_trips)}</strong> trips. ` +
        (allAgree
          ? `All ${hasEmbedded ? "three arms" : "both sides"} returned identical answers to all three questions.`
          : "<strong>At least one comparison disagreed</strong> — treat the timings with suspicion.") +
        (hasEmbedded
          ? " Embedded Python is not the middle case one might expect: it removes the driver " +
            "but still materialises every row in Python, and that — not the wire — is the cost."
          : data.embedded_note
          ? ` Embedded Python arm skipped: ${data.embedded_note}.`
          : ""),
    }),
    el("div", { class: "grid" }, [timeFig.card]),
    el("div", { class: "grid" }, [movedCard]),
    ...(splitCard ? [el("div", { class: "grid" }, [splitCard])] : []),
    el("div", { class: "grid" }, [ingestFig.card])
  );
}

/* ------------------------------------------------------------------- shell */

const PANELS = {
  overview: { render: renderOverview, host: "overview-body", loaded: false },
  quality: { render: renderQuality, host: "quality-body", loaded: false },
  zones: { render: renderZones, host: "zones-body", loaded: false },
  time: { render: renderTime, host: "time-body", loaded: false },
  pairs: { render: renderPairs, host: "pairs-body", loaded: false },
  browse: { render: renderBrowse, host: "browse-body", loaded: false },
  bench: { render: renderBench, host: "bench-body", loaded: true },
};

function showTab(name) {
  for (const button of document.querySelectorAll("nav.tabs button")) {
    button.setAttribute("aria-selected", String(button.dataset.tab === name));
  }
  for (const [key, panel] of Object.entries(PANELS)) {
    document.getElementById("panel-" + key).hidden = key !== name;
    if (key === name && !panel.loaded) {
      panel.loaded = true;
      panel.render(document.getElementById(panel.host));
    }
  }
  history.replaceState(null, "", "#" + name);
}

function reload(name) {
  const panel = PANELS[name];
  panel.loaded = true;
  panel.render(document.getElementById(panel.host));
}

function init() {
  document.getElementById("tabs").addEventListener("click", (event) => {
    const tab = event.target.closest("button[data-tab]");
    if (tab) showTab(tab.dataset.tab);
  });

  document.getElementById("zones-clean").addEventListener("change", () => reload("zones"));
  document.getElementById("time-clean").addEventListener("change", () => reload("time"));
  document.getElementById("pairs-clean").addEventListener("change", () => reload("pairs"));
  document.getElementById("pairs-cross").addEventListener("change", () => reload("pairs"));
  document.getElementById("bench-run").addEventListener("click", () => reload("bench"));
  document.getElementById("browse-rule").addEventListener("change", () => reload("browse"));
  document.getElementById("browse-limit").addEventListener("change", () => reload("browse"));

  // An expanded figure closes on Escape and on a click outside the card, not
  // only on the button that opened it.
  const overlay = document.getElementById("overlay");
  overlay.addEventListener("click", (event) => {
    if (event.target === overlay) collapseFigure();
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") collapseFigure();
  });

  const requested = location.hash.replace("#", "");
  showTab(PANELS[requested] ? requested : "overview");

  // The comparison takes seconds because the Python arm moves every trip across
  // the driver. Start it on load so the tab is populated by the time it is
  // clicked, rather than making the click the thing that waits.
  reload("bench");
}

document.addEventListener("DOMContentLoaded", init);
