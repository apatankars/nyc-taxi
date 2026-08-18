"""The analytics workflows, and the flag policy they run under.

    docker exec -w /src nyc-taxi-iris /usr/irissys/bin/irispython analytics.py
    docker exec -w /src nyc-taxi-iris /usr/irissys/bin/irispython analytics.py zones
    docker exec -w /src nyc-taxi-iris /usr/irissys/bin/irispython analytics.py policy
    docker exec -w /src nyc-taxi-iris /usr/irissys/bin/irispython analytics.py sql \\
        "SELECT TOP 5 pu_zone, COUNT(*) FROM Taxi.TripEnriched GROUP BY pu_zone"

Every workflow pushes the whole computation into IRIS -- GROUP BY, aggregation,
ordering, row limit all server-side -- so what comes back to Python is a finished
table of tens of rows, not 787,060.

On flag handling: do NOT default to flag_count = 0. Excluding all 106,345 flagged
trips drops the average trip distance from 19.02 to 2.90 miles, but also silently
discards the 55,613-trip meter-metadata block -- a *correlated* population, not
noise -- which biases a demand count. Instead exclude per measure (see
rules.MEASURE_RULES: each metric names only the rules that undermine it) and
report what was excluded next to every number.
"""

import sys

from db import banner, exec_sql, log
import rules

TOP_N = 12

# Analytics read the enriched view, not Taxi.Trip, so borough and zone names are
# already resolved and correctly cased. See stage1_schema.build_views().
VIEW = "Taxi.TripEnriched"

DOW_NAMES = {1: "Mon", 2: "Tue", 3: "Wed", 4: "Thu",
             5: "Fri", 6: "Sat", 7: "Sun"}
MONTH_NAMES = {1: "Jan", 2: "Feb", 3: "Mar", 4: "Apr", 5: "May", 6: "Jun",
               7: "Jul", 8: "Aug", 9: "Sep", 10: "Oct", 11: "Nov", 12: "Dec"}


# --------------------------------------------------------------------------
# reading results
# --------------------------------------------------------------------------
# Aggregates read back through a view arrive as strings, so every numeric column
# needs casting at the boundary. Doing it here rather than in each workflow keeps
# the int()/float() noise out of the reporting code.

def _i(value):
    return int(value) if value not in (None, "") else 0


def _f(value):
    return float(value) if value not in (None, "") else 0.0


# --------------------------------------------------------------------------
# flag policy
# --------------------------------------------------------------------------

def exclude_for(measure, alias="t"):
    """SQL predicate excluding only the rules that undermine `measure`.

    NOT EXISTS against TripFlag rather than a mask test on Trip: TripFlag's bitmap
    index on rule_id makes this an index read, while any bitmask expression forces
    a full 787k-row scan.
    """
    ids = sorted(rules.IDS[name] for name in rules.MEASURE_RULES[measure])
    joined = ",".join(str(i) for i in ids)
    return (f"NOT EXISTS (SELECT 1 FROM Taxi.TripFlag f "
            f"WHERE f.trip_id = {alias}.trip_id AND f.rule_id IN ({joined}))")


def real_zones(alias="t", both_ends=True):
    """Exclude the lookup's own 'Unknown' / 'Outside of NYC' codes."""
    ids = ",".join(str(z) for z in rules.UNKNOWN_ZONES)
    clause = f"{alias}.pu_location_id NOT IN ({ids})"
    if both_ends:
        clause += f" AND {alias}.do_location_id NOT IN ({ids})"
    return clause


def report_exclusions(label, where):
    """Print how many rows a policy dropped, alongside the result it produced.

    Not optional decoration. A number derived from a filtered population is only
    interpretable next to the size of what was filtered out.
    """
    total = _i(exec_sql(f"SELECT COUNT(*) FROM {VIEW} t")[0][0])
    kept = _i(exec_sql(f"SELECT COUNT(*) FROM {VIEW} t WHERE {where}")[0][0])
    dropped = total - kept
    log(f"  policy [{label}]: kept {kept:,} of {total:,} "
        f"({100.0 * dropped / total:.2f}% excluded)")
    return kept


# --------------------------------------------------------------------------
# workflow 1 -- busiest zones
# --------------------------------------------------------------------------

def busiest_zones(top=TOP_N):
    """Where trips start and end.

    Counting trips, so the policy is the permissive one: a trip with a broken
    meter still happened at a real place. Only out-of-year pickups and the
    unplaceable zone codes come out.
    """
    banner("WORKFLOW 1 -- busiest pickup and drop-off zones")
    where = f"{exclude_for('count')} AND {real_zones(both_ends=False)}"
    report_exclusions("count", where)

    for role, id_col, zone_col, borough_col in [
        ("PICKUP", "pu_location_id", "pu_zone", "pu_borough"),
        ("DROP-OFF", "do_location_id", "do_zone", "do_borough"),
    ]:
        log("")
        log(f"  Top {top} {role} zones")
        log(f"    {'zone':34s} {'borough':14s} {'trips':>9}  {'share':>6}")
        rows = exec_sql(f"""
            SELECT TOP {top} {zone_col}, {borough_col}, COUNT(*) AS trips
            FROM {VIEW} t
            WHERE {where}
            GROUP BY {id_col}, {zone_col}, {borough_col}
            ORDER BY 3 DESC
        """)
        grand = _i(exec_sql(f"SELECT COUNT(*) FROM {VIEW} t WHERE {where}")[0][0])
        for zone, borough, trips in rows:
            n = _i(trips)
            log(f"    {str(zone):34.34s} {str(borough):14.14s} {n:>9,}  "
                f"{100.0 * n / grand:>5.2f}%")

    log("")
    log("  By borough (pickup):")
    for borough, trips in exec_sql(f"""
        SELECT pu_borough, COUNT(*) FROM {VIEW} t
        WHERE {where} GROUP BY pu_borough ORDER BY 2 DESC
    """):
        log(f"    {str(borough):16s} {_i(trips):>9,}")


# --------------------------------------------------------------------------
# workflow 2 -- activity over time
# --------------------------------------------------------------------------

def activity_by_time():
    """Demand by hour of day, day of week, and month. Same permissive policy."""
    banner("WORKFLOW 2 -- activity by hour, day and month")
    where = exclude_for("count")
    report_exclusions("count", where)

    log("")
    log("  By hour of day (bar = share of all trips)")
    rows = exec_sql(f"""
        SELECT pickup_hour, COUNT(*) FROM {VIEW} t
        WHERE {where} AND pickup_hour IS NOT NULL
        GROUP BY pickup_hour ORDER BY pickup_hour
    """)
    counts = {_i(h): _i(n) for h, n in rows}
    peak = max(counts.values()) if counts else 1
    for hour in range(24):
        n = counts.get(hour, 0)
        bar = "#" * int(round(40.0 * n / peak))
        log(f"    {hour:02d}:00  {n:>7,}  {bar}")
    busiest = max(counts, key=counts.get)
    log(f"    peak hour: {busiest:02d}:00 with {counts[busiest]:,} pickups")

    log("")
    log("  By day of week")
    for dow, n, avg_fare in exec_sql(f"""
        SELECT pickup_dow, COUNT(*), AVG(fare_amount) FROM {VIEW} t
        WHERE {where} AND pickup_dow IS NOT NULL
        GROUP BY pickup_dow ORDER BY pickup_dow
    """):
        log(f"    {DOW_NAMES.get(_i(dow), '?'):5s} {_i(n):>8,} trips   "
            f"avg fare ${_f(avg_fare):>6.2f}")

    log("")
    log("  By month")
    for month, n in exec_sql(f"""
        SELECT pickup_month, COUNT(*) FROM {VIEW} t
        WHERE {where} AND pickup_month IS NOT NULL
        GROUP BY pickup_month ORDER BY pickup_month
    """):
        log(f"    {MONTH_NAMES.get(_i(month), '?'):5s} {_i(n):>8,}")


# --------------------------------------------------------------------------
# workflow 3 -- money and distance across zones
# --------------------------------------------------------------------------

def fares_across_zones(top=TOP_N):
    """Compare fares, tips and distances by borough and by zone.

    Here the policy tightens, because these are *averages of measurements* rather
    than counts of events, and a single 1,571-mile row moves a mean. Each column
    is filtered on its own measure, and tips additionally restrict to card
    payments -- see rules.CARD_PAYMENT.
    """
    banner("WORKFLOW 3 -- fares, tips and distance across zones")
    money = f"{exclude_for('fare')} AND {exclude_for('distance')}"
    where = f"{money} AND {real_zones()}"
    report_exclusions("fare + distance", where)
    log(f"  tips restricted to payment_type = {rules.CARD_PAYMENT} (card): cash "
        f"fares record tip 0.00 in 100% of cases, which is unobserved, not zero")

    log("")
    log("  By pickup borough")
    log(f"    {'borough':16s} {'trips':>8} {'fare':>7} {'$/mile':>7} "
        f"{'miles':>7} {'card tip':>9} {'tip %':>6}")
    for row in exec_sql(f"""
        SELECT pu_borough,
               COUNT(*),
               AVG(fare_amount),
               AVG(trip_distance),
               AVG(CASE WHEN payment_type = {rules.CARD_PAYMENT}
                        THEN tip_amount END),
               AVG(CASE WHEN payment_type = {rules.CARD_PAYMENT} AND fare_amount > 0
                        THEN 100.0 * tip_amount / fare_amount END)
        FROM {VIEW} t WHERE {where}
        GROUP BY pu_borough ORDER BY 2 DESC
    """):
        borough, n, fare, miles, tip, tip_pct = row
        # Ratio of the means, not the mean of per-trip ratios: weights each mile
        # equally, which is the right choice for a rate.
        per_mile = _f(fare) / _f(miles) if _f(miles) else 0.0
        log(f"    {str(borough):16s} {_i(n):>8,} {_f(fare):>7.2f} {per_mile:>7.2f} "
            f"{_f(miles):>7.2f} {_f(tip):>9.2f} {_f(tip_pct):>5.1f}%")

    # HAVING keeps the tail from dominating: a zone with nine trips is not
    # evidence about that zone, it is evidence about nine trips.
    log("")
    log(f"  Top {top} zones by average fare (minimum 500 trips)")
    log(f"    {'zone':32s} {'borough':13s} {'trips':>7} {'fare':>7} {'miles':>7}")
    for zone, borough, n, fare, miles in exec_sql(f"""
        SELECT TOP {top} pu_zone, pu_borough, COUNT(*), AVG(fare_amount),
               AVG(trip_distance)
        FROM {VIEW} t WHERE {where}
        GROUP BY pu_location_id, pu_zone, pu_borough
        HAVING COUNT(*) >= 500
        ORDER BY 4 DESC
    """):
        log(f"    {str(zone):32.32s} {str(borough):13.13s} {_i(n):>7,} "
            f"{_f(fare):>7.2f} {_f(miles):>7.2f}")


# --------------------------------------------------------------------------
# workflow 4 -- origin/destination pairs
# --------------------------------------------------------------------------

def top_od_pairs(top=TOP_N):
    """The most common routes, with what they cost and how long they take."""
    banner("WORKFLOW 4 -- most common origin / destination pairs")
    where = (f"{exclude_for('count')} AND {exclude_for('distance')} "
             f"AND {exclude_for('duration')} AND {real_zones()}")
    report_exclusions("count + distance + duration", where)

    log("")
    log(f"    {'origin -> destination':52s} {'trips':>7} {'mi':>6} {'min':>6} {'fare':>7}")
    for row in exec_sql(f"""
        SELECT TOP {top} pu_zone, do_zone, COUNT(*), AVG(trip_distance),
               AVG(duration_min), AVG(fare_amount)
        FROM {VIEW} t WHERE {where}
        GROUP BY pu_location_id, do_location_id, pu_zone, do_zone
        ORDER BY 3 DESC
    """):
        pu, do, n, miles, minutes, fare = row
        pair = f"{pu} -> {do}"
        log(f"    {pair:52.52s} {_i(n):>7,} {_f(miles):>6.2f} "
            f"{_f(minutes):>6.1f} {_f(fare):>7.2f}")

    log("")
    log("  Same-zone round trips (origin == destination), top 5:")
    for zone, n in exec_sql(f"""
        SELECT TOP 5 pu_zone, COUNT(*) FROM {VIEW} t
        WHERE {where} AND pu_location_id = do_location_id
        GROUP BY pu_location_id, pu_zone ORDER BY 2 DESC
    """):
        log(f"    {str(zone):34.34s} {_i(n):>7,}")


# --------------------------------------------------------------------------
# workflow 5 -- what the policy choice actually costs
# --------------------------------------------------------------------------

def policy_impact():
    """The same four numbers under four policies.

    Read this before trusting the others: it shows the flag policy moving an
    answer further than the question does. Every number is measured at run time,
    not quoted -- a hardcoded ratio goes stale the first time a threshold changes.
    """
    banner("WORKFLOW 5 -- how much does the flag policy change the answer?")

    invalid_only = (
        "NOT EXISTS (SELECT 1 FROM Taxi.TripFlag f "
        "JOIN Taxi.TripQuality q ON q.rule_id = f.rule_id "
        "WHERE f.trip_id = t.trip_id AND q.severity = 'invalid')")
    per_measure = f"{exclude_for('fare')} AND {exclude_for('distance')}"

    policies = [
        ("all rows (no filter)", "1=1"),
        ("flag_count = 0 (strictest)", "t.flag_count = 0"),
        ("exclude severity='invalid'", invalid_only),
        ("per-measure (what we use)", per_measure),
    ]

    total = _i(exec_sql(f"SELECT COUNT(*) FROM {VIEW} t")[0][0])
    log(f"    {'policy':30s} {'trips':>9} {'kept':>7} {'avg fare':>9} "
        f"{'avg miles':>10} {'avg min':>8}")
    distances = {}
    for label, where in policies:
        row = exec_sql(f"""
            SELECT COUNT(*), AVG(fare_amount), AVG(trip_distance), AVG(duration_min)
            FROM {VIEW} t WHERE {where}
        """)[0]
        n = _i(row[0])
        distances[label] = _f(row[2])
        log(f"    {label:30s} {n:>9,} {100.0 * n / total:>6.1f}% "
            f"{_f(row[1]):>9.2f} {_f(row[2]):>10.2f} {_f(row[3]):>8.2f}")

    loose, tight = distances[policies[0][0]], distances[policies[1][0]]
    log("")
    log(f"  Read the distance column again: {loose:.2f} miles unfiltered vs "
        f"{tight:.2f} strictest,")
    log(f"  a factor of {loose / tight:.1f} on the same {total:,} rows. That gap is "
        f"the reason")
    log("  'just average it' is not an available option on this dataset.")

    # %EXACT here for the same reason the view uses it: these two columns come
    # straight off Taxi.TripQuality, and without it they print as UNUSUAL and
    # METER_METADATA_MISSING -- the uppercase SQL collation, not the stored value.
    log("")
    log("  What the strictest policy would have thrown away:")
    thrown = exec_sql("""
        SELECT TOP 6 %EXACT(q.rule_name), %EXACT(q.severity), COUNT(*)
        FROM Taxi.TripFlag f JOIN Taxi.TripQuality q ON q.rule_id = f.rule_id
        GROUP BY q.rule_name, q.severity ORDER BY 3 DESC
    """)
    for name, severity, n in thrown:
        log(f"    {str(name):26s} {str(severity):8s} {_i(n):>8,}")

    # Name the largest 'unusual' block rather than assuming which rule it is: the
    # point holds for whichever rule tops that list after a threshold change.
    biggest = next(((n, _i(c)) for n, s, c in thrown if str(s) == "unusual"), None)
    if biggest:
        log("")
        log(f"  {biggest[0]} is the one that matters: {biggest[1]:,} trips whose")
        log("  fares and distances are sound -- the flag marks missing meter")
        log("  metadata, not a bad measurement. flag_count = 0 discards all of them,")
        log("  and they are a correlated block rather than a scatter, so removing")
        log("  them tilts a demand count instead of just shrinking it.")


# --------------------------------------------------------------------------
# ad-hoc SQL
# --------------------------------------------------------------------------

def run_sql(statement):
    """Escape hatch for one-off questions, so exploring does not mean editing code."""
    banner("ad-hoc SQL")
    log(f"  {statement}")
    log("")
    rows = exec_sql(statement)
    if not rows:
        log("  (no rows)")
        log("  A Taxi.mask_names() call on a grouped column returns empty rather")
        log("  than erroring -- group on the function expression instead.")
        return
    for row in rows:
        log("    " + " | ".join(str(value) for value in row))
    log("")
    log(f"  {len(rows):,} rows")


WORKFLOWS = {
    "zones": busiest_zones,
    "time": activity_by_time,
    "fares": fares_across_zones,
    "pairs": top_od_pairs,
    "policy": policy_impact,
}


def main(argv):
    if argv and argv[0] == "sql":
        run_sql(" ".join(argv[1:]))
        return
    if not argv:
        for name in ("policy", "zones", "time", "fares", "pairs"):
            WORKFLOWS[name]()
        return
    for name in argv:
        if name not in WORKFLOWS:
            log(f"unknown workflow {name!r}; "
                f"choose from {', '.join(WORKFLOWS)} or: sql <statement>")
            return
        WORKFLOWS[name]()


if __name__ == "__main__":
    main(sys.argv[1:])
