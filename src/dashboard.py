"""Query layer behind the web dashboard: one SQL statement per panel.

No HTML and no Flask here -- each function returns JSON-ready dicts, so the same
code serves src/web/taxi_app.py and an interactive irispython session.

All aggregation happens in IRIS. GROUP BY, AVG, ordering, row limits and the
bitmask decoding are server-side, so a panel returns tens of rows rather than
trips. pushdown_compare() measures what that is worth.

Averages use the per-measure flag exclusions from rules.MEASURE_RULES (via
analytics.exclude_for): counting trips and averaging fares need different
exclusions. policy_comparison() shows what the choice costs.
"""

import time

import analytics
import rules
from db import MODE, exec_columns, exec_sql, iter_rows, scalar

VIEW = analytics.VIEW

DOW_NAMES = analytics.DOW_NAMES
MONTH_NAMES = analytics.MONTH_NAMES

# payment_type codes and the TLC's meanings. 6 ('voided trip') does appear.
PAYMENT_NAMES = {
    1: "Credit card",
    2: "Cash",
    3: "No charge",
    4: "Dispute",
    5: "Unknown",
    6: "Voided trip",
}


# --------------------------------------------------------------------------
# values crossing into JSON
# --------------------------------------------------------------------------
# Embedded Python hands back int, float and str, but SQL NULL arrives as the
# empty string rather than None -- so `if value:` on a numeric column is a bug
# waiting to happen and `json.dumps` would emit "" where the front end expects a
# number. Every value read out of a result row goes through num() or txt().

def num(value, default=0):
    """Coerce a SQL numeric to int or float, treating NULL as `default`."""
    if isinstance(value, bool):
        return int(value)
    if value is None or value == "":
        return default
    if isinstance(value, (int, float)):
        return value
    text = str(value)
    try:
        return int(text)
    except ValueError:
        try:
            return float(text)
        except ValueError:
            return default


def txt(value, default=""):
    """Coerce a SQL string to str, treating NULL as `default`."""
    if value is None or value == "":
        return default
    return str(value)


def _round(value, places=2):
    return round(num(value, 0.0), places)


def _round_or_none(value, places=2):
    """Like _round, but keeps NULL distinct from zero -- for the single-trip view,
    where implied_mph is NULL when duration_min is zero."""
    if value is None or value == "":
        return None
    return round(num(value, 0.0), places)


# --------------------------------------------------------------------------
# filters
# --------------------------------------------------------------------------

class Filters:
    """The filter bar as a SQL predicate builder.

    One object, so every panel applies the same selection while each asks for the
    flag exclusions its own measure needs. Values bind as `?` parameters: safer,
    and one entry in the IRIS query cache whatever the user picks.
    """

    def __init__(self, months=(), dows=(), hours=(), boroughs=()):
        self.months = _int_list(months, 1, 12)
        self.dows = _int_list(dows, 1, 7)
        self.hours = _int_list(hours, 0, 23)
        self.boroughs = [str(b)[:50] for b in (boroughs or []) if str(b).strip()]

    @classmethod
    def from_request(cls, args):
        """Build from a Flask MultiDict (or anything with getlist)."""
        return cls(months=args.getlist("month"), dows=args.getlist("dow"),
                   hours=args.getlist("hour"), boroughs=args.getlist("borough"))

    def as_dict(self):
        return {"months": self.months, "dows": self.dows, "hours": self.hours,
                "boroughs": self.boroughs}

    def where(self, measure="count", zones=None):
        """Return (sql_predicate, params) for this selection.

        `measure` names the exclusions to apply, or None for no flag filtering.
        `zones` is None, 'pickup' or 'both': whether to drop the lookup's own
        'Unknown'/'Outside of NYC' codes, which only geographic panels want.
        """
        parts = [analytics.exclude_for(measure) if measure else "1=1"]
        params = []

        for column, values in (("pickup_month", self.months),
                               ("pickup_dow", self.dows),
                               ("pickup_hour", self.hours)):
            if values:
                parts.append(f"t.{column} IN ({_placeholders(values)})")
                params.extend(values)

        if self.boroughs:
            # pu_borough is %EXACT() in the view, so it compares in real casing.
            parts.append(f"t.pu_borough IN ({_placeholders(self.boroughs)})")
            params.extend(self.boroughs)

        if zones:
            parts.append(analytics.real_zones(both_ends=(zones == "both")))

        return " AND ".join(parts), params

    def only(self, measure, expression):
        """Narrow one aggregate to the rows valid for its own measure.

        Conditional aggregation, so a single scan can serve a trip count and
        several correctly scoped averages. Without it, February reports a mean
        distance four times the real figure from a handful of rows whose distance
        arrived with a thousands separator.
        """
        return f"CASE WHEN {analytics.exclude_for(measure)} THEN {expression} END"


def _placeholders(values):
    return ",".join("?" for _ in values)


def _int_list(values, low, high):
    """Parse and range-check a repeated query parameter. Bad values are dropped
    rather than raising -- these arrive from a URL, and a stale bookmark should
    not 400 the page."""
    out = []
    for value in values or []:
        try:
            number = int(value)
        except (TypeError, ValueError):
            continue
        if low <= number <= high and number not in out:
            out.append(number)
    return sorted(out)


def _clamp(value, low, high):
    return max(low, min(high, int(num(value, low))))


# --------------------------------------------------------------------------
# headline figures
# --------------------------------------------------------------------------

def kpis(filters):
    """The figures at the top of the page.

    Separate statements, because trips, distance, duration and fare each carry
    their own exclusions -- one SELECT would force a single policy on all four.
    Each average reports COUNT of the same expression, so the tile can say how
    many trips it was computed over without a second scan.
    """
    count_where, count_params = filters.where("count")
    trips = num(scalar(f"SELECT COUNT(*) FROM {VIEW} t WHERE {count_where}",
                       *count_params))
    tiles = [{"key": "trips", "label": "Trips", "value": trips, "unit": "count",
              "over": trips}]

    for key, label, column, measure, unit in [
        ("distance", "Average distance", "t.trip_distance", "distance", "mi"),
        ("duration", "Average duration", "t.duration_min", "duration", "min"),
        ("fare", "Average fare", "t.fare_amount", "fare", "$"),
    ]:
        where, params = filters.where(measure)
        row = exec_sql(f"SELECT AVG({column}), COUNT({column}) FROM {VIEW} t "
                       f"WHERE {where}", *params)[0]
        tiles.append({"key": key, "label": label, "unit": unit,
                      "value": _round(row[0]), "over": num(row[1])})

    # Tips from card payments only: a cash tip never reaches the meter, so cash
    # rows record 0.00 for a tip that was never observed. See rules.CARD_PAYMENT.
    tip_where, tip_params = filters.where("tip")
    tip_where += f" AND t.payment_type = {rules.CARD_PAYMENT} AND t.fare_amount > 0"
    row = exec_sql(f"""
        SELECT AVG(t.tip_amount), AVG(100.0 * t.tip_amount / t.fare_amount),
               COUNT(*)
        FROM {VIEW} t WHERE {tip_where}
    """, *tip_params)[0]
    tiles.append({"key": "tip", "label": "Average tip", "unit": "$",
                  "value": _round(row[0]),
                  "secondary": f"{_round(row[1], 1)}% of fare, card only",
                  "over": num(row[2])})
    return tiles


# --------------------------------------------------------------------------
# activity over time
# --------------------------------------------------------------------------

def monthly(filters):
    """Trips by month, with the average fare and distance behind each point."""
    where, params = filters.where("count")
    rows = exec_sql(f"""
        SELECT t.pickup_month, COUNT(*),
               AVG({filters.only('fare', 't.fare_amount')}),
               AVG({filters.only('distance', 't.trip_distance')})
        FROM {VIEW} t
        WHERE {where} AND t.pickup_month IS NOT NULL
        GROUP BY t.pickup_month ORDER BY t.pickup_month
    """, *params)
    return [{"month": num(r[0]), "label": MONTH_NAMES.get(num(r[0]), "?"),
             "trips": num(r[1]), "avg_fare": _round(r[2]),
             "avg_distance": _round(r[3])} for r in rows]


def hourly(filters):
    """Trips by hour of day, collapsed across the week."""
    where, params = filters.where("count")
    rows = exec_sql(f"""
        SELECT t.pickup_hour, COUNT(*),
               AVG({filters.only('fare', 't.fare_amount')})
        FROM {VIEW} t WHERE {where} AND t.pickup_hour IS NOT NULL
        GROUP BY t.pickup_hour ORDER BY t.pickup_hour
    """, *params)
    return [{"hour": num(r[0]), "label": f"{num(r[0]):02d}", "trips": num(r[1]),
             "avg_fare": _round(r[2])} for r in rows]


def hour_dow(filters):
    """The 24x7 demand grid: one GROUP BY on two bitmap-indexed columns returns
    the finished 168-cell heatmap."""
    where, params = filters.where("count")
    rows = exec_sql(f"""
        SELECT t.pickup_dow, t.pickup_hour, COUNT(*),
               AVG({filters.only('fare', 't.fare_amount')})
        FROM {VIEW} t
        WHERE {where} AND t.pickup_dow IS NOT NULL AND t.pickup_hour IS NOT NULL
        GROUP BY t.pickup_dow, t.pickup_hour
        ORDER BY t.pickup_dow, t.pickup_hour
    """, *params)

    cells = [{"dow": num(r[0]), "hour": num(r[1]), "trips": num(r[2]),
              "avg_fare": _round(r[3])} for r in rows]
    return {
        "cells": cells,
        "dow_names": [DOW_NAMES[d] for d in sorted(DOW_NAMES)],
        "max_trips": max((c["trips"] for c in cells), default=0),
        "peak": max(cells, key=lambda c: c["trips"]) if cells else None,
    }


# --------------------------------------------------------------------------
# geography
# --------------------------------------------------------------------------

def boroughs(filters):
    """Pickup borough rollup: volume, money and distance in one statement.

    COUNT(*) over the count population, each average narrowed to its own measure
    inline -- one query per column would scan four times for a seven-row table.
    """
    where, params = filters.where("count", zones="pickup")
    rows = exec_sql(f"""
        SELECT t.pu_borough,
               COUNT(*),
               AVG({filters.only('fare', 't.fare_amount')}),
               AVG({filters.only('distance', 't.trip_distance')}),
               AVG(CASE WHEN t.payment_type = {rules.CARD_PAYMENT}
                         AND t.fare_amount > 0
                        THEN 100.0 * t.tip_amount / t.fare_amount END)
        FROM {VIEW} t WHERE {where}
        GROUP BY t.pu_borough ORDER BY 2 DESC
    """, *params)

    total = sum(num(r[1]) for r in rows) or 1
    return [{"borough": txt(r[0], "(unmapped)"),
             "trips": num(r[1]),
             "share": _round(100.0 * num(r[1]) / total, 1),
             "avg_fare": _round(r[2]),
             "avg_distance": _round(r[3]),
             "tip_pct": _round(r[4], 1)} for r in rows]


def zones(filters, top=12, role="pickup"):
    """Busiest zones, either end. GROUP BY on the id, label from the join."""
    id_col, zone_col, borough_col = (
        ("pu_location_id", "pu_zone", "pu_borough") if role == "pickup"
        else ("do_location_id", "do_zone", "do_borough"))
    where, params = filters.where(
        "count", zones="pickup" if role == "pickup" else "both")
    top = _clamp(top, 1, 50)

    # Grouping by the id as well as the name is not redundant: the id carries the
    # bitmap index, and two zones sharing a name in different boroughs stay apart.
    rows = exec_sql(f"""
        SELECT TOP ? t.{zone_col}, t.{borough_col}, COUNT(*),
               AVG({filters.only('fare', 't.fare_amount')})
        FROM {VIEW} t WHERE {where}
        GROUP BY t.{id_col}, t.{zone_col}, t.{borough_col}
        ORDER BY 3 DESC
    """, top, *params)

    grand = num(scalar(f"SELECT COUNT(*) FROM {VIEW} t WHERE {where}",
                       *params)) or 1
    return [{"zone": txt(r[0], "(unmapped)"), "borough": txt(r[1], "(unmapped)"),
             "trips": num(r[2]),
             "share": _round(100.0 * num(r[2]) / grand, 2),
             "avg_fare": _round(r[3])} for r in rows]


def od_pairs(filters, top=15):
    """Most common origin -> destination pairs.

    Same-zone trips are reported separately rather than filtered out: a common,
    real pattern that would otherwise dominate the ranking.
    """
    top = _clamp(top, 1, 50)
    where, params = filters.where("count", zones="both")

    rows = exec_sql(f"""
        SELECT TOP ? t.pu_zone, t.do_zone, COUNT(*),
               AVG({filters.only('distance', 't.trip_distance')}),
               AVG({filters.only('duration', 't.duration_min')}),
               AVG({filters.only('fare', 't.fare_amount')})
        FROM {VIEW} t WHERE {where} AND t.pu_location_id <> t.do_location_id
        GROUP BY t.pu_location_id, t.do_location_id, t.pu_zone, t.do_zone
        ORDER BY 3 DESC
    """, top, *params)

    circular = exec_sql(f"""
        SELECT TOP 5 t.pu_zone, COUNT(*),
               AVG({filters.only('fare', 't.fare_amount')})
        FROM {VIEW} t WHERE {where} AND t.pu_location_id = t.do_location_id
        GROUP BY t.pu_location_id, t.pu_zone ORDER BY 2 DESC
    """, *params)

    return {
        "rows": [{"origin": txt(r[0], "(unmapped)"),
                  "destination": txt(r[1], "(unmapped)"),
                  "trips": num(r[2]),
                  "avg_distance": _round(r[3]),
                  "avg_duration": _round(r[4], 1),
                  "avg_fare": _round(r[5])} for r in rows],
        "circular": [{"zone": txt(r[0], "(unmapped)"), "trips": num(r[1]),
                      "avg_fare": _round(r[2])} for r in circular],
    }


def payment_mix(filters):
    """Trips and tipping behaviour by payment type. The cash row's average tip is
    0.00 across every trip in it, which is what an unobserved value looks like
    once it is stored as a number."""
    where, params = filters.where("count")
    rows = exec_sql(f"""
        SELECT t.payment_type, COUNT(*), AVG(t.tip_amount), AVG(t.fare_amount),
               SUM(CASE WHEN t.tip_amount > 0 THEN 1 ELSE 0 END)
        FROM {VIEW} t WHERE {where}
        GROUP BY t.payment_type ORDER BY 2 DESC
    """, *params)

    total = sum(num(r[1]) for r in rows) or 1
    return [{"label": PAYMENT_NAMES.get(num(r[0], -1), "Not recorded"),
             "trips": num(r[1]),
             "share": _round(100.0 * num(r[1]) / total, 1),
             "avg_tip": _round(r[2]),
             "avg_fare": _round(r[3]),
             "tipped_pct": _round(100.0 * num(r[4]) / (num(r[1]) or 1), 1)}
            for r in rows]


# --------------------------------------------------------------------------
# trip quality
# --------------------------------------------------------------------------

def rule_impact():
    """Every rule, how many trips it flags, and how many it flags alone.

    The sole-flag count is the one that decides a policy: a rule that only fires
    alongside others costs nothing extra to exclude, while one with a large
    sole-flag count single-handedly decides what flag_count = 0 throws away.

    Correlated subqueries rather than joins, so a rule flagging nothing still
    returns a row at 0 instead of disappearing.
    """
    rows = exec_sql("""
        SELECT q.rule_id, q.rule_name, q.phase, q.severity, q.description,
               (SELECT COUNT(*) FROM Taxi.TripFlag f WHERE f.rule_id = q.rule_id),
               (SELECT COUNT(*) FROM Taxi.TripFlag f
                  JOIN Taxi.Trip t ON t.trip_id = f.trip_id
                 WHERE f.rule_id = q.rule_id AND t.flag_count = 1)
        FROM Taxi.TripQuality q ORDER BY q.rule_id
    """)
    total = num(scalar("SELECT COUNT(*) FROM Taxi.Trip")) or 1
    return {
        "rows": [{"rule_id": num(r[0]), "rule_name": txt(r[1]), "phase": txt(r[2]),
                  "severity": txt(r[3]), "description": txt(r[4]),
                  "trips": num(r[5]), "only_flag": num(r[6]),
                  "pct": _round(100.0 * num(r[5]) / total, 2)} for r in rows],
        "total_trips": total,
    }


# The population every flagged value is compared against: the trips no rule
# objected to. Deliberately *not* the per-measure policy the rest of the page
# reports under -- a comparison needs one fixed referent, and "the rows nothing
# objected to" is the only one that means the same thing for all sixteen rules.
# It is a yardstick here, not a reporting policy; policy_comparison() below is
# where reporting under it is shown to be the wrong choice.
CLEAN = "t.flag_count = 0"

# One join, used by both flag panels, to reach the trips a single rule flagged.
# TripFlag's bitmap index on rule_id makes this an index read: the flagged side of
# every comparison below touches thousands of rows, not 787,060.
FLAGGED_FROM = f"""
    FROM Taxi.TripFlag f JOIN {VIEW} t ON t.trip_id = f.trip_id
    WHERE f.rule_id = ?
"""


def _flag_label(count):
    return ("No flags" if count == 0
            else "1 flag" if count == 1 else f"{count} flags")


def flag_coverage():
    """How much of the dataset carries a flag, and how many flags at a time.

    Unfiltered, like rule_impact(): how much of the data the rule set touches is a
    fact about the dataset, and recomputing it inside the filter bar would read as
    the rules changing when only the selection did.
    """
    counts = [(num(r[0]), num(r[1])) for r in exec_sql(f"""
        SELECT t.flag_count, COUNT(*) FROM {VIEW} t
        GROUP BY t.flag_count ORDER BY t.flag_count
    """)]
    total = sum(n for _, n in counts) or 1
    clean = dict(counts).get(0, 0)

    # Severity, not count: two 'unusual' flags on one trip is a weaker claim than
    # one 'invalid'. Same EXISTS-over-TripFlag shape as the policy table, so the
    # two panels are counting the same thing.
    invalid = num(scalar(f"""
        SELECT COUNT(*) FROM {VIEW} t
        WHERE EXISTS (SELECT 1 FROM Taxi.TripFlag f
                        JOIN Taxi.TripQuality q ON q.rule_id = f.rule_id
                       WHERE f.trip_id = t.trip_id AND q.severity = 'invalid')
    """))

    return {
        "rows": [{"flags": flags, "label": _flag_label(flags), "trips": n,
                  "share": _round(100.0 * n / total, 2)} for flags, n in counts],
        "totals": {
            "trips": total,
            "clean": clean,
            "flagged": total - clean,
            "flagged_pct": _round(100.0 * (total - clean) / total, 2),
            "invalid": invalid,
            "invalid_pct": _round(100.0 * invalid / total, 2),
            "unusual_only": total - clean - invalid,
            "max_flags": max((flags for flags, _ in counts), default=0),
        },
    }


# --------------------------------------------------------------------------
# a flag's values against the unflagged population's
# --------------------------------------------------------------------------
# Both panels below read rules.FIELDS: each rule names the one column it is an
# argument about, so "what does this flag mean" becomes a comparison of the same
# number over two populations rather than a count of rows.

def _stats_sql(spec):
    """The four aggregates describing one field. Ordered, because the caller reads
    them back positionally out of a row holding several fields' worth."""
    expr = spec["expr"]
    return (f"COUNT({expr}), AVG({expr}), MIN({expr}), MAX({expr})")


def _stats(row, offset, population):
    """Read one field's four aggregates back out of a result row.

    `valued` is not the same as the population size: passenger_count is NULL on
    every meter-metadata row, and a share computed against the wrong denominator
    is the kind of error this dashboard exists to argue against.
    """
    return {"trips": population,
            "valued": num(row[offset]),
            "avg": _round_or_none(row[offset + 1], 3),
            "min": _round_or_none(row[offset + 2], 2),
            "max": _round_or_none(row[offset + 3], 2)}


def _ratio(spec, flagged, baseline):
    """How many times the unflagged average the flagged average is.

    None unless both are positive: fare_negative averages -$10.19 against $17.39,
    and '-0.6x' invites reading a sign flip as a small difference. None too where
    the field is not a quantity -- the ratio of two calendar years is 0.99 and
    means nothing.
    """
    if not spec.get("ratio", True):
        return None
    if not flagged or not baseline or flagged <= 0 or baseline <= 0:
        return None
    return _round(flagged / baseline, 3)


def flag_profiles():
    """Every rule that flags anything, with the flagged rows' values for the column
    the rule is about beside the unflagged population's values for that column.

    Two statements for sixteen rules and nine fields: one GROUP BY over TripFlag
    for the flagged side, one scan for the baseline. The alternative -- a query per
    rule -- is sixteen scans of 787,060 rows to produce fifteen table rows.
    """
    fields = sorted(set(rules.RULE_FIELD.values()))
    aggregates = ", ".join(_stats_sql(rules.FIELDS[key]) for key in fields)
    at = {key: 2 + 4 * i for i, key in enumerate(fields)}   # column of each field

    flagged = {num(r[0]): r for r in exec_sql(f"""
        SELECT f.rule_id, COUNT(*), {aggregates}
        FROM Taxi.TripFlag f JOIN {VIEW} t ON t.trip_id = f.trip_id
        GROUP BY f.rule_id
    """)}
    base = exec_sql(f"SELECT COUNT(*), {aggregates} FROM {VIEW} t "
                    f"WHERE {CLEAN}")[0]
    base_at = {key: 1 + 4 * i for i, key in enumerate(fields)}
    clean_trips = num(base[0])
    total = num(scalar("SELECT COUNT(*) FROM Taxi.Trip")) or 1

    catalogue = exec_sql("""
        SELECT q.rule_id, q.rule_name, q.severity, q.phase, q.description
        FROM Taxi.TripQuality q ORDER BY q.rule_id
    """)

    rows = []
    for rule in catalogue:
        rule_id, name = num(rule[0]), txt(rule[1])
        row = flagged.get(rule_id)
        # A rule matching nothing has no values to compare, so it is omitted here
        # rather than shown as a row of dashes; the chart above still counts it.
        if row is None or num(row[1]) == 0:
            continue
        key = rules.RULE_FIELD.get(name)
        spec = rules.FIELDS.get(key, {})
        trips = num(row[1])
        flagged_stats = _stats(row, at[key], trips) if key else None
        baseline_stats = _stats(base, base_at[key], clean_trips) if key else None
        rows.append({
            "rule_id": rule_id, "rule_name": name, "severity": txt(rule[2]),
            "phase": txt(rule[3]), "description": txt(rule[4]),
            "trips": trips,
            "pct": _round(100.0 * trips / total, 2),
            "field": key,
            "field_label": spec.get("label", ""),
            "unit": spec.get("unit", ""),
            "flagged": flagged_stats,
            "baseline": baseline_stats,
            "ratio": _ratio(spec, flagged_stats and flagged_stats["avg"],
                            baseline_stats and baseline_stats["avg"]),
        })

    rows.sort(key=lambda r: -r["trips"])
    return {
        "rows": rows,
        "total_trips": total,
        "baseline": {"label": "Trips no rule flagged", "trips": clean_trips},
        # The biggest flagged population, so the distribution panel opens on the
        # rule that decides the most rows rather than on rule_id 1.
        "default_rule": rows[0]["rule_id"] if rows else None,
    }


def _edge_text(value):
    """A bucket edge as it should read on an axis: 0.5, 20, 1,000."""
    return f"{int(value):,}" if float(value) == int(value) else f"{value:g}"


def _bucket_label(lo, hi, spec):
    if spec.get("integer") and hi - lo == 1:
        return _edge_text(lo)
    # [0, 0.01) is the exact-zero spike, not a range anyone measured.
    if lo == 0 and hi <= 0.01:
        return "0"
    return f"{_edge_text(lo)} - {_edge_text(hi)}"


def _range_buckets(spec):
    """(label, predicate) per bucket, from the field's edges.

    Open at both ends: a value below the first edge or above the last still gets
    counted, so the two populations' shares each sum to the rows that have a value.
    """
    expr, edges = spec["expr"], spec["edges"]
    buckets = [(f"under {_edge_text(edges[0])}", f"{expr} < {edges[0]}")]
    for lo, hi in zip(edges, edges[1:]):
        buckets.append((_bucket_label(lo, hi, spec),
                        f"{expr} >= {lo} AND {expr} < {hi}"))
    buckets.append((f"{_edge_text(edges[-1])} and over", f"{expr} >= {edges[-1]}"))
    return buckets


def _bucket_row(select, from_where, *params):
    return exec_sql(f"SELECT {select} {from_where}", *params)[0]


def _range_distribution(spec, rule_id):
    """Bucket counts for both populations, one statement each.

    SUM(CASE ...) per bucket rather than GROUP BY on a bucket expression: the
    bucket set is then fixed by the code instead of by which buckets happen to be
    non-empty, so an empty bucket is a zero rather than a missing row.
    """
    buckets = _range_buckets(spec)
    sums = ", ".join(f"SUM(CASE WHEN {p} THEN 1 ELSE 0 END)" for _, p in buckets)
    flagged = _bucket_row(sums, FLAGGED_FROM, rule_id)
    base = _bucket_row(sums, f"FROM {VIEW} t WHERE {CLEAN}")
    return [{"label": label, "flagged": num(flagged[i]), "baseline": num(base[i])}
            for i, (label, _) in enumerate(buckets)]


def _value_distribution(spec, rule_id, cap=12):
    """One bar per distinct value, for a field a bucket would only blur.

    Two GROUP BYs and a merge, so a value present in one population and not the
    other still gets a bar -- which for pickup_year is the entire finding.
    """
    expr = spec["expr"]
    flagged = {num(r[0]): num(r[1]) for r in exec_sql(f"""
        SELECT {expr}, COUNT(*) {FLAGGED_FROM} GROUP BY {expr}
    """, rule_id)}
    base = {num(r[0]): num(r[1]) for r in exec_sql(f"""
        SELECT {expr}, COUNT(*) FROM {VIEW} t WHERE {CLEAN} GROUP BY {expr}
    """)}
    values = sorted(set(flagged) | set(base))
    kept = values[:cap]
    # No thousands separator here, unlike a bucket edge: these are years, and
    # '2,008' reads as a quantity.
    rows = [{"label": f"{v:g}", "flagged": flagged.get(v, 0),
             "baseline": base.get(v, 0)} for v in kept]
    return rows, len(values) - len(kept)


def flag_distribution(rule_id):
    """One rule: where its flagged rows fall on the axis it is an argument about,
    against where the unflagged rows fall on the same axis.

    Each bar is a share of its *own* population, because the populations differ by
    two orders of magnitude -- 573 flagged trips against 680,715 unflagged ones --
    and raw counts would draw the flagged series as a flat line at zero.
    """
    rule_id = int(num(rule_id, 0))
    found = exec_sql("""
        SELECT q.rule_id, q.rule_name, q.severity, q.phase, q.predicate,
               q.description
        FROM Taxi.TripQuality q WHERE q.rule_id = ?
    """, rule_id)
    if not found:
        return None
    rule = found[0]
    name = txt(rule[1])
    key = rules.RULE_FIELD.get(name)
    spec = rules.FIELDS.get(key)
    if spec is None:
        return None

    # COUNT(*) leads each row, so the population size and the four field
    # aggregates come back together: `trips` counts the rows, `valued` the ones
    # where this field is not NULL.
    stats = f"COUNT(*), {_stats_sql(spec)}"
    flagged_row = _bucket_row(stats, FLAGGED_FROM, rule_id)
    base_row = _bucket_row(stats, f"FROM {VIEW} t WHERE {CLEAN}")
    flagged = _stats(flagged_row, 1, num(flagged_row[0]))
    base = _stats(base_row, 1, num(base_row[0]))

    dropped = 0
    if spec.get("mode") == "value":
        rows, dropped = _value_distribution(spec, rule_id)
    else:
        rows = _range_distribution(spec, rule_id)

    # Percentages against the rows that *have* a value, and buckets empty in both
    # populations dropped -- a run of zero-height pairs says nothing and costs the
    # bars that do.
    for row in rows:
        row["flagged_pct"] = _round(
            100.0 * row["flagged"] / flagged["valued"], 2) if flagged["valued"] else 0.0
        row["baseline_pct"] = _round(
            100.0 * row["baseline"] / base["valued"], 2) if base["valued"] else 0.0

    return {
        "rule_id": num(rule[0]), "rule_name": name, "severity": txt(rule[2]),
        "phase": txt(rule[3]), "predicate": txt(rule[4]),
        "description": txt(rule[5]),
        "field": key, "field_label": spec["label"], "unit": spec["unit"],
        "mode": spec.get("mode", "range"),
        "flagged": flagged, "baseline": base,
        "rows": [r for r in rows if r["flagged"] or r["baseline"]],
        "values_dropped": dropped,
    }


def policy_comparison(filters):
    """The same four figures under three flag policies, over the current
    selection.

    The panel to read before trusting any other number on the page: the spread on
    average distance is roughly sixfold, driven by a few hundred rows.
    """
    where, params = filters.where(measure=None)
    per_measure = (f"{analytics.exclude_for('fare')} AND "
                   f"{analytics.exclude_for('distance')}")
    policies = [
        ("All rows", "no flag filter", "1=1"),
        ("flag_count = 0", "drops every flagged trip", "t.flag_count = 0"),
        # Intersection of the fare and distance exclusions, so both averages come
        # from one population and the row compares against the others. The KPI
        # tiles use each measure's exclusions alone, so they differ slightly.
        ("Per measure", "fare and distance exclusions together", per_measure),
    ]

    universe = num(scalar(f"SELECT COUNT(*) FROM {VIEW} t WHERE {where}",
                          *params)) or 1
    rows = []
    for label, note, clause in policies:
        row = exec_sql(f"""
            SELECT COUNT(*), AVG(t.fare_amount), AVG(t.trip_distance),
                   AVG(t.duration_min)
            FROM {VIEW} t WHERE {where} AND {clause}
        """, *params)[0]
        rows.append({"policy": label, "note": note, "trips": num(row[0]),
                     "kept_pct": _round(100.0 * num(row[0]) / universe, 1),
                     "avg_fare": _round(row[1]),
                     "avg_distance": _round(row[2]),
                     "avg_duration": _round(row[3])})
    return {"rows": rows, "universe": universe}


# --------------------------------------------------------------------------
# individual trips
# --------------------------------------------------------------------------

PAGE_SIZE = 50

TRIP_ORDERS = {
    "trip_id": "t.trip_id",
    "fare": "t.fare_amount DESC",
    "distance": "t.trip_distance DESC",
    "duration": "t.duration_min DESC",
    "flags": "t.flag_count DESC, t.trip_id",
}


def trip_page(filters, page=1, size=PAGE_SIZE, order="trip_id", show="all",
              rule_id=None):
    """A page of individual trips, paged inside IRIS.

    This is the investigation view, so `show` ('all', 'flagged', 'clean') and
    `rule_id` narrow it to the records a quality rule picked out.

    No flag exclusions apply here, unlike every aggregate panel: the exclusions
    exist to keep a broken measurement out of an average, and hiding the flagged
    rows from the list would hide exactly what this view is for.

    IRIS SQL has no LIMIT/OFFSET: TOP gives the upper bound and %VID (the result
    set's own row number) the lower, so TOP fetches offset+size rows and the outer
    SELECT discards the offset. Deep paging costs more than shallow, so the UI
    keeps pages small and leads with filters.
    """
    size = _clamp(size, 1, 200)
    page = max(int(page or 1), 1)
    offset = (page - 1) * size
    order_sql = TRIP_ORDERS.get(order, TRIP_ORDERS["trip_id"])

    where, params = filters.where(measure=None)
    if show == "flagged":
        where += " AND t.flag_count > 0"
    elif show == "clean":
        where += " AND t.flag_count = 0"
    if rule_id:
        where += (" AND EXISTS (SELECT 1 FROM Taxi.TripFlag f "
                  "WHERE f.trip_id = t.trip_id AND f.rule_id = ?)")
        params = params + [int(num(rule_id, 0))]

    # Taxi.mask_names is a Python UDF (stage1_schema.build_functions) decoding
    # flag_mask into rule names. Called on the page's 50 rows, after TOP has cut
    # the result down -- in a WHERE clause it would be opaque to the optimiser and
    # force a full scan. The CAST is required: without it the column reaches the
    # function as a string and the bitwise AND fails.
    rows = exec_sql(f"""
        SELECT * FROM (
            SELECT TOP ?
                   t.trip_id,
                   CAST(t.pickup_ts AS VARCHAR(19)) AS pickup,
                   t.pu_zone, t.do_zone,
                   t.trip_distance, t.duration_min, t.fare_amount, t.tip_amount,
                   t.payment_type, t.flag_count,
                   Taxi.mask_names(CAST(t.flag_mask AS BIGINT)) AS flag_names
            FROM {VIEW} t WHERE {where}
            ORDER BY {order_sql}
        ) WHERE %VID > ?
    """, offset + size, *params, offset)

    matched = num(scalar(f"SELECT COUNT(*) FROM {VIEW} t WHERE {where}", *params))
    return {
        "page": page, "size": size, "matched": matched, "order": order,
        "show": show, "rule_id": int(num(rule_id, 0)) or None,
        "pages": (matched + size - 1) // size if size else 0,
        "rows": [{"trip_id": num(r[0]), "pickup": txt(r[1]),
                  "pu_zone": txt(r[2], "(unmapped)"),
                  "do_zone": txt(r[3], "(unmapped)"),
                  "distance": _round(r[4]), "duration": _round(r[5], 1),
                  "fare": _round(r[6]), "tip": _round(r[7]),
                  "payment": PAYMENT_NAMES.get(num(r[8], -1), "Not recorded"),
                  "flag_count": num(r[9]),
                  "flags": [n for n in txt(r[10]).split(",") if n]}
                 for r in rows],
    }


def trip_detail(trip_id):
    """One trip, its flags, and the raw text it was parsed from.

    The raw row is the part worth having: a flagged value is only settleable
    against what the file actually said, and '1,571.97' with a thousands
    separator reads differently from 1571.97. Trip.raw_id points at the TripRaw
    row, so this is a keyed read rather than a search.
    """
    trip_id = int(num(trip_id, 0))
    rows = exec_sql(f"""
        SELECT t.trip_id, CAST(t.pickup_ts AS VARCHAR(19)),
               CAST(t.dropoff_ts AS VARCHAR(19)),
               t.pu_zone, t.pu_borough, t.do_zone, t.do_borough,
               t.passenger_count, t.trip_distance, t.duration_min, t.implied_mph,
               t.fare_amount, t.tip_amount, t.total_amount, t.payment_type
        FROM {VIEW} t WHERE t.trip_id = ?
    """, trip_id)
    if not rows:
        return None
    r = rows[0]

    flags = exec_sql("""
        SELECT q.rule_name, q.severity, q.description
        FROM Taxi.TripFlag f JOIN Taxi.TripQuality q ON q.rule_id = f.rule_id
        WHERE f.trip_id = ? ORDER BY q.rule_id
    """, trip_id)

    raw_id = num(scalar("SELECT raw_id FROM Taxi.Trip WHERE trip_id = ?", trip_id),
                 None)
    raw = None
    if raw_id:
        columns, raw_rows = exec_columns(
            "SELECT * FROM Taxi.TripRaw WHERE %ID = ?", raw_id)
        if raw_rows:
            raw = {"raw_id": raw_id,
                   "fields": [{"name": c, "value": txt(v, "(empty)")}
                              for c, v in zip(columns, raw_rows[0])]}

    return {
        "trip_id": num(r[0]), "pickup": txt(r[1]), "dropoff": txt(r[2]),
        "pu_zone": txt(r[3], "(unmapped)"), "pu_borough": txt(r[4]),
        "do_zone": txt(r[5], "(unmapped)"), "do_borough": txt(r[6]),
        "passenger_count": num(r[7], None),
        "distance": _round_or_none(r[8]), "duration": _round_or_none(r[9], 1),
        "implied_mph": _round_or_none(r[10], 1),
        "fare": _round_or_none(r[11]), "tip": _round_or_none(r[12]),
        "total": _round_or_none(r[13]),
        "payment": PAYMENT_NAMES.get(num(r[14], -1), "Not recorded"),
        "flags": [{"rule_name": txt(f[0]), "severity": txt(f[1]),
                   "description": txt(f[2])} for f in flags],
        "raw": raw,
    }


# --------------------------------------------------------------------------
# stretch goal: aggregate in IRIS, or pull the rows into Python?
# --------------------------------------------------------------------------

PUSHDOWN_CASES = {
    "borough": {"label": "Trips and average fare by pickup borough",
                "group": "t.pu_borough"},
    "hour": {"label": "Trips and average fare by pickup hour",
             "group": "t.pickup_hour"},
    "payment": {"label": "Trips and average fare by payment type",
                "group": "t.payment_type"},
}


def pushdown_compare(filters, case="borough"):
    """Compute one aggregate twice: in IRIS, then row by row in Python.

    Both halves produce the same table. Measured on the borough rollup over all
    787,060 trips: 0.15s pushed vs 3.06s pulled, and 8 rows crossing the boundary
    instead of 787,053. The row ratio is the durable number -- it is a property of
    the query, while the timing depends on the machine.
    """
    spec = PUSHDOWN_CASES.get(case) or PUSHDOWN_CASES["borough"]
    group = spec["group"]
    where, params = filters.where("count")

    started = time.time()
    pushed_rows = exec_sql(f"""
        SELECT {group}, COUNT(*), AVG(t.fare_amount)
        FROM {VIEW} t WHERE {where} AND {group} IS NOT NULL
        GROUP BY {group}
    """, *params)
    pushed_seconds = time.time() - started
    pushed = {txt(r[0], "(none)"): (num(r[1]), _round(r[2], 4))
              for r in pushed_rows}

    started = time.time()
    totals, counts = {}, {}
    fetched = 0
    for row in iter_rows(f"""
        SELECT {group}, t.fare_amount
        FROM {VIEW} t WHERE {where} AND {group} IS NOT NULL
    """, *params):
        fetched += 1
        key = txt(row[0], "(none)")
        counts[key] = counts.get(key, 0) + 1
        totals[key] = totals.get(key, 0.0) + num(row[1], 0.0)
    pulled_seconds = time.time() - started
    pulled = {key: (counts[key], round(totals[key] / counts[key], 4))
              for key in counts}

    # Counts should agree exactly; averages to within floating-point noise, since
    # SQL AVG is decimal and Python's sum is not. Reporting the largest
    # disagreement beats asserting equality -- it is the number that would grow
    # if one side were wrong.
    keys = sorted(set(pushed) | set(pulled))
    mismatches = [k for k in keys
                  if pushed.get(k, (None,))[0] != pulled.get(k, (None,))[0]]
    deltas = [abs(pushed[k][1] - pulled[k][1])
              for k in keys if k in pushed and k in pulled]

    return {
        "case": case,
        "label": spec["label"],
        "mode": MODE,
        "rows": [{"key": key,
                  "trips": pushed.get(key, (0, 0.0))[0],
                  "avg_fare_iris": pushed.get(key, (0, 0.0))[1],
                  "avg_fare_python": pulled.get(key, (0, 0.0))[1]}
                 for key in sorted(keys, key=lambda k: -pushed.get(k, (0,))[0])],
        "pushed": {"label": "GROUP BY in IRIS",
                   "seconds": _round(pushed_seconds, 3),
                   "rows_returned": len(pushed_rows)},
        "pulled": {"label": "Row by row in Python",
                   "seconds": _round(pulled_seconds, 3),
                   "rows_returned": fetched},
        "speedup": _round(pulled_seconds / pushed_seconds
                          if pushed_seconds > 0 else 0.0, 1),
        "row_ratio": round(fetched / len(pushed_rows)) if pushed_rows else None,
        "count_mismatches": mismatches,
        "max_avg_delta": _round(max(deltas), 6) if deltas else 0.0,
    }


# --------------------------------------------------------------------------
# what the UI needs before it can draw a filter bar
# --------------------------------------------------------------------------

def meta():
    """Filter options, row counts and transport mode. Fetched once on load, so
    the filter bar is built from the database rather than from constants
    duplicated in JavaScript."""
    borough_rows = exec_sql(f"""
        SELECT t.pu_borough, COUNT(*) FROM {VIEW} t
        WHERE t.pu_borough IS NOT NULL
        GROUP BY t.pu_borough ORDER BY 2 DESC
    """)
    span = exec_sql("""
        SELECT CAST(MIN(pickup_ts) AS VARCHAR(19)),
               CAST(MAX(pickup_ts) AS VARCHAR(19))
        FROM Taxi.Trip WHERE pickup_ts IS NOT NULL
    """)
    rule_rows = exec_sql("SELECT rule_id, %EXACT(rule_name) FROM Taxi.TripQuality "
                         "ORDER BY rule_id")

    return {
        "mode": MODE,
        "totals": {
            "trips": num(scalar("SELECT COUNT(*) FROM Taxi.Trip")),
            "flagged": num(scalar("SELECT COUNT(*) FROM Taxi.Trip "
                                  "WHERE flag_count <> 0")),
            "zones": num(scalar("SELECT COUNT(*) FROM Taxi.Zone")),
            "rules": num(scalar("SELECT COUNT(*) FROM Taxi.TripQuality")),
        },
        "span": {"first": txt(span[0][0]) if span else "",
                 "last": txt(span[0][1]) if span else ""},
        "boroughs": [{"name": txt(r[0]), "trips": num(r[1])}
                     for r in borough_rows],
        "months": [{"value": m, "label": MONTH_NAMES[m]}
                   for m in sorted(MONTH_NAMES)],
        "dows": [{"value": d, "label": DOW_NAMES[d]} for d in sorted(DOW_NAMES)],
        "hours": [{"value": h, "label": f"{h:02d}:00"} for h in range(24)],
        # For the flagged-trip browser's rule filter. %EXACT because rule_name
        # comes straight off the table and the SQL collation would upper-case it.
        "rules": [{"value": num(r[0]), "label": txt(r[1])} for r in rule_rows],
        "measure_rules": {measure: sorted(names)
                          for measure, names in rules.MEASURE_RULES.items()},
        "pushdown_cases": [{"value": key, "label": spec["label"]}
                           for key, spec in PUSHDOWN_CASES.items()],
    }
