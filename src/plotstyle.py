"""Shared chart palette and matplotlib defaults for the notebook.

Imported by notebooks/nyc_taxi_insights.ipynb so the figures are one visual
system rather than twelve independent guesses, and so the notebook cells stay
about the data.

Colour policy, stated because it constrains everything below: no hue is invented
here. Every value is taken from a pre-validated reference palette (the blue
sequential slot, plus the documented chrome/ink greys), and the charts use
**one hue plus grey** — a single series per plot, with emphasis where one bar is
the point. That means there is no multi-series categorical palette in this
project to get colour-blind separation wrong. It also matters because the
palette validator is a Node script and this machine has no `node`, so a custom
palette could not have been checked; staying on already-validated values is the
honest way around that rather than eyeballing it.

Two deliberate deviations from the reference spec, both matplotlib limits:

* **Square bar ends, not 4px-rounded.** Matplotlib cannot round one end of a bar.
  Faking it with FancyBboxPatch means a radius in data units, which distorts into
  an ellipse whenever the x and y scales differ — worse than square corners.
* **Light surface only.** A dark variant would need its own steps validated
  against the dark surface; the notebook renders on white for the presentation,
  so shipping one selected mode is the correct scope.
"""

import matplotlib as mpl
from matplotlib.ticker import StrMethodFormatter

# Reference palette values (light mode).
SURFACE = "#fcfcfb"   # chart surface
INK = "#0b0b0b"       # primary text
INK_2 = "#52514e"     # secondary text
MUTED = "#898781"     # axis labels, tick text
GRID = "#e1e0d9"      # hairline gridline
AXIS = "#c3c2b7"      # baseline / axis rule
SERIES = "#2a78d6"    # categorical slot 1 / sequential blue, the only hue used
DEEMPH = "#c3c2b7"    # de-emphasised marks in an emphasis chart

# Bars: capped rather than filling their slot, so the leftover band is air.
BAR_HEIGHT = 0.62


def apply_style():
    """Set the rcParams every figure inherits. Call once, at notebook top."""
    mpl.rcParams.update({
        "figure.facecolor": SURFACE,
        "figure.dpi": 110,
        "axes.facecolor": SURFACE,
        "axes.edgecolor": AXIS,
        "axes.linewidth": 1.0,
        # Only the two spines that carry meaning; the box around a plot is ink
        # that is not data.
        "axes.spines.top": False,
        "axes.spines.right": False,
        "axes.titlesize": 12,
        "axes.titleweight": "semibold",
        "axes.titlecolor": INK,
        "axes.titlelocation": "left",
        "axes.titlepad": 12,
        "axes.labelsize": 9.5,
        "axes.labelcolor": INK_2,
        "axes.grid": True,
        # Without this the hairline grid draws *over* the bars, which looks like
        # white slices cut out of every mark.
        "axes.axisbelow": True,
        "grid.color": GRID,
        "grid.linewidth": 1.0,
        "grid.linestyle": "-",       # solid: a dashed grid competes with the data
        "xtick.color": MUTED,
        "ytick.color": MUTED,
        "xtick.labelsize": 9,
        "ytick.labelsize": 9,
        "xtick.labelcolor": INK_2,
        "ytick.labelcolor": INK_2,
        "lines.linewidth": 2.0,      # 2px lines, round caps
        "lines.solid_capstyle": "round",
        "lines.markersize": 8,
        "font.family": "sans-serif",
        "font.sans-serif": ["Helvetica Neue", "Helvetica", "Arial",
                            "DejaVu Sans"],
        "font.size": 10,
        "legend.frameon": False,
        "figure.autolayout": False,
    })


def subtitle(ax, text):
    """One line of secondary ink under the title, for the caveat or the unit.

    Charts in this project nearly always need one: 'suspect trips excluded' or
    'card payments only' is the difference between a number and a misleading
    number, and it belongs on the chart rather than in the surrounding prose that
    gets cropped out of a screenshot.

    Re-setting the title with a bigger pad is not optional — matplotlib has no
    concept of a subtitle on an Axes, so without pushing the title up the two
    lines are drawn on top of each other.

    The `loc` round-trip is not decoration either. An Axes keeps three separate
    title artists, and `get_title()` reads the centre one, so with
    `axes.titlelocation = "left"` in force it returns '' and re-setting the title
    silently *erases* it — a title that was there a moment ago simply vanishes
    from the figure.

    Escape dollar signs as `\\$` in `text`. A *pair* of unescaped `$` turns
    everything between them into italic mathtext: '$16.47 to $20.11' renders as
    '16.47to20.11', silently losing both currency symbols and the spaces.
    """
    loc = mpl.rcParams["axes.titlelocation"]
    ax.set_title(ax.get_title(loc=loc), loc=loc, pad=28)
    ax.annotate(text, xy=(0, 1.02), xycoords="axes fraction",
                fontsize=9, color=MUTED, ha="left", va="bottom")


def bar_thickness(ax, n_bars, px=22, category_axis="y"):
    """Fraction of the category band a bar should occupy to render ~`px` thick.

    The spec caps bars at 24px and asks for the leftover band to be air, but
    matplotlib sizes bars as a fraction of the band — so the same 0.62 that looks
    right with ten bars renders a 47px slab with four. Measuring the axes and
    working backwards keeps every bar chart in the notebook the same weight
    regardless of how many categories it has.

    `category_axis` is 'y' for barh (categories run down the page) and 'x' for
    bar. Requires a draw first, hence the canvas call.
    """
    ax.figure.canvas.draw()
    box = ax.get_window_extent()
    band = (box.height if category_axis == "y" else box.width) / max(n_bars, 1)
    return min(0.85, px / band)


def thousands(ax, axis="y"):
    """Comma-group the tick labels on a count axis ('70000' -> '70,000')."""
    target = ax.yaxis if axis == "y" else ax.xaxis
    target.set_major_formatter(StrMethodFormatter("{x:,.0f}"))


def label_bar_ends(ax, values, fmt="{:,.0f}", pad=0.01):
    """Write each bar's value past its end, in ink rather than in the bar.

    The reference spec says label selectively and never put a number on every
    point — that rule is about lines and scatters, where a flood of numbers
    covers the marks. For a ranked bar chart the labels *are* the value axis:
    with them, the x gridlines and tick labels come off, which is a net removal
    of ink. Callers that keep the axis should not also call this.
    """
    span = max(values) or 1
    for i, value in enumerate(values):
        ax.annotate(fmt.format(value), xy=(value + span * pad, i),
                    va="center", ha="left", fontsize=9, color=INK_2)


def strip_x_axis(ax):
    """Remove the x scale entirely — for bar charts carrying direct labels."""
    ax.xaxis.set_visible(False)
    ax.grid(False)
    ax.spines["bottom"].set_visible(False)
