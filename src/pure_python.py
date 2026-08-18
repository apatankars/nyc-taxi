"""The stretch goal's control arm: the whole prototype again, pandas only.

Run:  python src/pure_python.py              all four workflows, from the CSVs
      python src/pure_python.py zones        one of them
      python src/pure_python.py --cache      reuse a parsed frame if one exists

**This module imports nothing from IRIS and never opens a connection.** That is
the point: it is the honest answer to "what if we just used Python?", built well
enough that the comparison in src/compare_approaches.py measures two real
implementations rather than a real one against a strawman.

Faithfulness is the whole game here, so it is worth being explicit about what
"the same" means. This file reproduces:

* the same **parse** as src/schema.py — 20 columns, thousands separators stripped,
  the same five nullable columns, the same closed domains;
* the same **16 quality rules** as src/build_quality.py, predicate for predicate;
* the same **enrichment** as src/build_enriched.py — borough/zone at both ends,
  calendar parts of the pickup;
* the same **7 aggregate workflows** as src/analytics.py, including the two house
  rules (suspect trips excluded everywhere, card payments only for tips) and the
  ratio-of-sums definitions.

src/compare_approaches.py checks that claim numerically instead of trusting it.

Three things had to be done differently, and each one is a finding rather than a
detail — they are what "reimplement the database in pandas" actually costs:

1. **Money is not float64.** IRIS stores the money columns as NUMERIC(12,2) and
   sums them exactly. pandas sums float64, so `total_amount - components` lands
   on 0.010000000000000009 instead of 0.01 and the component-mismatch rule
   changes its answer. Fixed by comparing in integer cents (see `_cents`).
2. **ROUND is not round().** IRIS rounds half away from zero; numpy rounds half
   to even, so IRIS's 2.5 -> 3 is pandas' 2.5 -> 2. Every displayed average would
   disagree with the SQL version by a cent every few dozen rows. Fixed by
   `round_half_up`, applied to the small result frames.
3. **The rules are written twice.** They cannot be imported from
   build_quality.py, because those are SQL strings and this module is meant to
   have no database in it. So there are now two definitions of "suspect", in two
   languages, that must be kept in step by hand — exactly the drift the single
   RULES tuple was built to prevent. compare_approaches.py asserts the two sets
   still agree; nothing but that test stops them separating.
"""

import hashlib
import os
import pickle
import sys
import time
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from typing import Callable

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")
TRIPS_CSV = os.path.join(DATA, "2023_Green_Taxi_Trip_Data.csv")
ZONES_CSV = os.path.join(DATA, "taxi_zone_lookup(in).csv")
# The pure-Python stand-in for "the data is already in the database". Written by
# --cache, gitignored, and the fair way to time a second question: an analyst who
# has to answer ten questions does not re-parse the CSV ten times.
CACHE = os.path.join(DATA, "trips_parsed.pkl")

TS_FORMAT = "%m/%d/%Y %I:%M:%S %p"

# CSV name -> the name used everywhere downstream. Deliberately the sql_name from
# src/schema.py, so a rule below reads the same as the SQL rule it mirrors and the
# two can be diffed by eye.
RENAME = {
    "VendorID": "vendor_id",
    "lpep_pickup_datetime": "pickup_ts",
    "lpep_dropoff_datetime": "dropoff_ts",
    "store_and_fwd_flag": "store_and_fwd_flag",
    "RatecodeID": "ratecode_id",
    "PULocationID": "pu_location_id",
    "DOLocationID": "do_location_id",
    "passenger_count": "passenger_count",
    "trip_distance": "trip_distance",
    "fare_amount": "fare_amount",
    "extra": "extra",
    "mta_tax": "mta_tax",
    "tip_amount": "tip_amount",
    "tolls_amount": "tolls_amount",
    "improvement_surcharge": "improvement_surcharge",
    "total_amount": "total_amount",
    "payment_type": "payment_type",
    "trip_type": "trip_type",
    "congestion_surcharge": "congestion_surcharge",
}

MONEY = ("fare_amount", "extra", "mta_tax", "tip_amount", "tolls_amount",
         "improvement_surcharge", "total_amount", "congestion_surcharge")

# The closed domains from src/schema.py, checked here too so the two arms reject
# (or in this file's case, report) the same rows. 99 is the TLC dictionary's own
# 'unknown' rate code and is legitimate.
DOMAINS = {
    "vendor_id": {1, 2},
    "ratecode_id": {1, 2, 3, 4, 5, 6, 99},
    "payment_type": {1, 2, 3, 4, 5, 6},
    "trip_type": {1, 2},
}
LOCATION_RANGE = (1, 265)


# ---------------------------------------------------------------------------
# Numeric fidelity helpers
# ---------------------------------------------------------------------------

def _cents(series):
    """A money column as exact integer cents.

    Every money value in the file has at most two decimals, so this is lossless,
    and it is the only way to get pandas to agree with a NUMERIC(12,2) column on
    a sum. `rint` before the cast because 12.29 * 100 is 1228.9999999999998 in
    binary float and a plain int() cast would truncate it to 1228.
    """
    return np.rint(series.fillna(0) * 100).astype("int64")


def round_half_up(values, places=2):
    """Round like IRIS does, not like numpy does.

    numpy rounds halves to even (2.5 -> 2, 3.5 -> 4); IRIS rounds them away from
    zero (2.5 -> 3, verified against the running instance). Going through
    Decimal(str(v)) rather than Decimal(v) matters as well: str() gives the
    shortest repr, so 2.675 quantizes to 2.68 as the SQL does, instead of to the
    2.67 that binary float's true value (2.67499999...) would produce.

    Only ever called on result frames of a few hundred rows, so the per-value
    Decimal cost is irrelevant.
    """
    quantum = Decimal(1).scaleb(-places)
    return pd.Series(
        [float(Decimal(str(v)).quantize(quantum, rounding=ROUND_HALF_UP))
         if pd.notna(v) else np.nan for v in values],
        index=getattr(values, "index", None), dtype="float64")


def _round_cols(frame, spec):
    """Apply round_half_up to several columns at once: {column: places}."""
    for column, places in spec.items():
        frame[column] = round_half_up(frame[column], places)
    return frame


# ---------------------------------------------------------------------------
# Load
# ---------------------------------------------------------------------------

def read_trips(path=TRIPS_CSV):
    """Parse the trip CSV into a typed frame, schema.py's rules in vector form.

    `thousands=","` is doing the work that schema.parse_number's comma strip does
    row by row: 578 values are written '1,069.67' inside a quoted field, and they
    are the largest values in their columns — dropping them would take the
    distance maximum from 278,990.28 to 976.84 and quietly discard the most
    extreme records in a data set whose stated purpose includes finding extreme
    records.

    `low_memory=False` silences a real warning rather than hiding one: read in
    chunks, store_and_fwd_flag gets a per-chunk dtype and comes back as mixed
    object. One dtype for the column is what the rest of this file assumes.

    ehail_fee is read and dropped. It is empty in all 787,060 rows, so keeping it
    would carry no information — but dropping it silently is a different claim
    from "we checked, it is entirely empty".
    """
    frame = pd.read_csv(
        path,
        thousands=",",
        low_memory=False,
        # Only an empty field is missing — pandas' default missing-value list
        # includes 'N/A', 'NA', 'null' and a dozen others, and deciding that a
        # string in a data file means "absent" is a decision that belongs to the
        # schema, not to the reader's defaults. See read_zones() for what that
        # default does to real data when nobody overrides it.
        keep_default_na=False,
        na_values=[""],
        parse_dates=["lpep_pickup_datetime", "lpep_dropoff_datetime"],
        date_format=TS_FORMAT,
    )
    frame = frame.drop(columns=[c for c in ("ehail_fee",) if c in frame.columns])
    frame = frame.rename(columns=RENAME)

    # Computed once here, as the loader does at insert time. Signed on purpose:
    # 1,033 trips drop off before they are picked up, and clamping at zero would
    # erase the evidence.
    frame["duration_min"] = (
        (frame.dropoff_ts - frame.pickup_ts).dt.total_seconds() / 60.0)
    return frame


def read_zones(path=ZONES_CSV):
    """The 265-row lookup, with two file quirks that are silent if missed.

    `encoding='utf-8-sig'` strips the byte-order mark that otherwise turns the
    first column name into '\\ufeffLocationID' — the header still looks right in
    an editor while every lookup by name fails.

    `keep_default_na=False` is the more interesting one, and it was found by the
    agreement check in src/compare_approaches.py rather than by reading the code.
    LocationID 265's borough is the literal string **'N/A'**, which is in pandas'
    default missing-value list, so a plain read_csv turns that borough into NaN.
    The effect is not an error anywhere: the merge succeeds, the groupby produces
    a NaN group, and 367 trips quietly report "no borough" while the same query
    against IRIS reports 'N/A'. A database never does this, because a VARCHAR
    column has no opinion about which strings mean absent.
    """
    zones = pd.read_csv(path, encoding="utf-8-sig",
                        keep_default_na=False, na_values=[""])
    zones.columns = ["location_id", "borough", "zone", "service_zone"]
    for column in ("borough", "zone", "service_zone"):
        zones[column] = zones[column].str.strip()
    return zones


def structural_report(frame):
    """Count rows that could not have been typed or are outside a closed domain.

    The mirror of src/validate_trips.py, and it should print zeros: the supplied
    file is structurally clean, 787,060 rows out of 787,060. It exists because
    "clean" is a measurement, not an assumption, and because a silent parse
    failure in pandas becomes NaN rather than an error — the row survives with a
    hole in it and every average downstream shifts a little.
    """
    problems = {}
    for column in ("pickup_ts", "dropoff_ts", "trip_distance", "total_amount",
                   "fare_amount"):
        n = int(frame[column].isna().sum())
        if n:
            problems[f"{column}: unparsed/empty"] = n
    for column, allowed in DOMAINS.items():
        bad = frame[column].notna() & ~frame[column].isin(allowed)
        if bad.any():
            problems[f"{column}: outside {sorted(allowed)}"] = int(bad.sum())
    low, high = LOCATION_RANGE
    for column in ("pu_location_id", "do_location_id"):
        bad = ~frame[column].between(low, high)
        if bad.any():
            problems[f"{column}: outside {low}..{high}"] = int(bad.sum())
    return problems


# ---------------------------------------------------------------------------
# Quality rules — the same sixteen, in pandas
# ---------------------------------------------------------------------------

SUSPECT = "suspect"
INFO = "info"


@dataclass(frozen=True)
class Rule:
    code: str
    test: Callable[[pd.DataFrame], pd.Series]
    severity: str


def _component_sum_mismatch(f):
    """|total - sum(components)| > $0.01, computed in cents.

    In float64 this rule is quietly wrong: the difference for a large block of
    rows evaluates to 0.010000000000000009, which is > 0.01, so ~73k rows flip
    from unflagged to flagged and the count no longer matches the SQL view.
    Integer cents removes the question. congestion_surcharge is filled with 0
    because it is NULL on the 55,613-row metadata block — in SQL a NULL would
    poison the whole expression and the rule would silently stop firing on
    exactly those rows; in pandas the sum would skip it instead, which is a
    different bug with the same shape.
    """
    components = sum(_cents(f[c]) for c in (
        "fare_amount", "extra", "mta_tax", "tip_amount", "tolls_amount",
        "improvement_surcharge", "congestion_surcharge"))
    return (_cents(f.total_amount) - components).abs() > 1


def _implied_mph(f):
    hours = (f.duration_min / 60.0).where(f.duration_min > 0)
    return f.trip_distance / hours


RULES = (
    # ---- distance -------------------------------------------------------
    Rule("zero_distance", lambda f: f.trip_distance == 0, SUSPECT),
    Rule("impossible_distance", lambda f: f.trip_distance > 100, SUSPECT),

    # ---- duration -------------------------------------------------------
    Rule("dropoff_before_pickup", lambda f: f.duration_min <= 0, SUSPECT),
    Rule("duration_over_12h", lambda f: f.duration_min > 720, SUSPECT),
    Rule("duration_under_1min",
         lambda f: (f.duration_min > 0) & (f.duration_min < 1), SUSPECT),

    # ---- money ----------------------------------------------------------
    Rule("negative_fare", lambda f: f.fare_amount < 0, SUSPECT),
    Rule("extreme_fare", lambda f: f.fare_amount > 500, SUSPECT),
    Rule("negative_total", lambda f: f.total_amount < 0, SUSPECT),

    # ---- money vs. distance ---------------------------------------------
    Rule("charged_without_moving",
         lambda f: (f.fare_amount > 0) & (f.trip_distance == 0), SUSPECT),
    Rule("moved_without_charge",
         lambda f: (f.trip_distance > 0) & (f.fare_amount == 0), SUSPECT),
    Rule("implausible_speed", lambda f: _implied_mph(f) > 80, SUSPECT),

    # ---- passengers -----------------------------------------------------
    Rule("zero_passengers", lambda f: f.passenger_count == 0, SUSPECT),
    Rule("excess_passengers", lambda f: f.passenger_count > 6, SUSPECT),

    # ---- informational --------------------------------------------------
    Rule("pickup_outside_2023",
         lambda f: (f.pickup_ts < pd.Timestamp("2023-01-01"))
         | (f.pickup_ts >= pd.Timestamp("2024-01-01")), INFO),
    Rule("missing_trip_metadata", lambda f: f.ratecode_id.isna(), INFO),
    Rule("component_sum_mismatch", _component_sum_mismatch, INFO),
)

SUSPECT_RULES = tuple(r for r in RULES if r.severity == SUSPECT)
INFO_RULES = tuple(r for r in RULES if r.severity == INFO)


def flag(frame):
    """One 0/1 column per rule, plus the roll-ups. The pandas Taxi.TripQuality.

    `fillna(False)` on every predicate is not defensive noise, it is the
    NULL-handling decision the SQL view makes explicitly with CASE WHEN: a
    missing passenger_count compares False against 0, and counting it as
    "not flagged" is right because a missing count is missing_trip_metadata's
    business, not zero_passengers'. Leave it as NA and the flag column becomes
    object dtype, the sums come back wrong, and nothing raises.
    """
    flags = pd.DataFrame(index=frame.index)
    for rule in RULES:
        flags[rule.code] = rule.test(frame).fillna(False).astype("int8")
    flags["suspect_flags"] = flags[[r.code for r in SUSPECT_RULES]].sum(axis=1)
    flags["info_flags"] = flags[[r.code for r in INFO_RULES]].sum(axis=1)
    flags["is_suspect"] = (flags.suspect_flags > 0).astype("int8")
    return flags


def suspect_trips(frame, flags):
    """The pandas Taxi.SuspectTrip: flagged rows, enriched, with a reason string."""
    reasons = pd.Series("", index=frame.index)
    for rule in SUSPECT_RULES:
        reasons = reasons.str.cat(
            np.where(flags[rule.code] == 1, rule.code + " ", ""))
    out = frame[flags.is_suspect == 1].copy()
    out["implied_mph"] = round_half_up(_implied_mph(out), 1)
    out["flag_count"] = flags.suspect_flags[flags.is_suspect == 1]
    out["flags"] = reasons[flags.is_suspect == 1].str.strip()
    return out


# ---------------------------------------------------------------------------
# Enrich
# ---------------------------------------------------------------------------

def enrich(frame, zones=None):
    """Zone names at both ends, calendar parts of the pickup, and is_suspect.

    Left joins for the same reason the view uses them: every LocationID in this
    file is in 1..265 so an inner join would lose nothing today, but an
    enrichment step that silently drops rows when a lookup misses is the last
    thing you want in a data-quality prototype.

    IRIS's DATEPART('dw') numbers Sunday as 1; pandas' dayofweek numbers Monday
    as 0. The arithmetic below converts, so both arms label and sort the week
    identically — an off-by-one here would reorder every weekday chart without
    changing a single number, which is the hardest kind of error to notice.
    """
    zones = read_zones() if zones is None else zones
    out = frame.copy()
    for side, key in (("pu", "pu_location_id"), ("do", "do_location_id")):
        renamed = zones.rename(columns={
            "borough": f"{side}_borough", "zone": f"{side}_zone",
            "service_zone": f"{side}_service_zone"})
        out = out.merge(renamed, how="left",
                        left_on=key, right_on="location_id")
        out = out.drop(columns="location_id")

    out["pickup_hour"] = out.pickup_ts.dt.hour
    out["pickup_dow"] = (out.pickup_ts.dt.dayofweek + 1) % 7 + 1
    out["pickup_day_name"] = out.pickup_ts.dt.day_name()
    out["pickup_month"] = out.pickup_ts.dt.month
    out["pickup_month_name"] = out.pickup_ts.dt.month_name()

    out = pd.concat([out, flag(out)[["is_suspect", "suspect_flags",
                                     "info_flags"]]], axis=1)
    return out


def _fingerprint():
    """What the cached frame depends on, so a stale one can be detected.

    This function exists because of a bug the comparison harness caught rather
    than one anybody predicted. The first version of the cache had no
    invalidation at all: a rule was fixed, the pickle was not rebuilt, and the
    cached arm went on cheerfully reporting the *old* answers while every other
    arm reported the new ones. Nothing failed — the numbers were merely wrong.

    That is the concrete version of "a view cannot go stale". `Taxi.TripEnriched`
    recomputes from the base table on every read, so changing a predicate in
    build_quality.py changes every consumer's answer immediately. Reproducing
    that guarantee outside a database means writing this function, and hashing
    the rule bodies (`__code__.co_code`) rather than just their names, because
    editing a threshold from 500 to 400 leaves the name alone.
    """
    stat = os.stat(TRIPS_CSV)
    rule_bytes = b"".join(r.test.__code__.co_code for r in RULES)
    return {
        "csv_size": stat.st_size,
        "csv_mtime": stat.st_mtime,
        "columns": sorted(RENAME.values()),
        "rules": [r.code for r in RULES],
        # sha1, not hash(): Python salts the built-in hash of bytes per process,
        # so a fingerprint built with it would never match on the next run and
        # the cache would silently degrade into no cache at all.
        "rule_hash": hashlib.sha1(rule_bytes).hexdigest(),
    }


def build(use_cache=False, verbose=False):
    """The whole pipeline: CSV -> typed -> flagged -> enriched frame.

    `use_cache` is what makes the comparison in compare_approaches.py fair. The
    database only parses the file once; letting the pandas arm do the same is the
    difference between an honest measurement and a rigged one. The cache is a
    pickle rather than Parquet only because pyarrow is not installed here —
    Parquet would be the real choice (columnar, compressed, and it can be read a
    column at a time), and it would make this arm faster, not slower.
    """
    if use_cache and os.path.exists(CACHE):
        started = time.perf_counter()
        with open(CACHE, "rb") as f:
            cached = pickle.load(f)
        if cached.get("fingerprint") == _fingerprint():
            if verbose:
                print(f"loaded cached frame in "
                      f"{time.perf_counter() - started:.2f}s "
                      f"({len(cached['frame']):,} rows)")
            return cached["frame"]
        if verbose:
            print("cache is stale (source file or rules changed) — rebuilding")

    started = time.perf_counter()
    frame = enrich(read_trips())
    if verbose:
        print(f"parsed and enriched {len(frame):,} rows in "
              f"{time.perf_counter() - started:.2f}s")
    if use_cache:
        with open(CACHE, "wb") as f:
            pickle.dump({"fingerprint": _fingerprint(), "frame": frame}, f,
                        protocol=pickle.HIGHEST_PROTOCOL)
        if verbose:
            print(f"cached to {os.path.relpath(CACHE, ROOT)} "
                  f"({os.path.getsize(CACHE) / 1e6:.0f} MB)")
    return frame


# ---------------------------------------------------------------------------
# The four workflows — same answers as src/analytics.py, same column names
# ---------------------------------------------------------------------------

def _clean(frame):
    """The house rule: suspect trips are excluded from every analytic.

    In SQL this is `WHERE is_suspect = 0`, five characters of intent that every
    consumer of the view gets for free. Here it is a function that each of the
    seven workflows below has to remember to call — and forgetting it in one of
    them produces a plausible number rather than an error.
    """
    return frame[frame.is_suspect == 0]


def busiest_zones(frame, direction="pickup", top=15):
    side = "pu" if direction == "pickup" else "do"
    out = (_clean(frame)
           .groupby([f"{side}_zone", f"{side}_borough"], dropna=False)
           .agg(trips=("fare_amount", "size"),
                avg_fare=("fare_amount", "mean"),
                avg_tip=("tip_amount", "mean"),
                avg_miles=("trip_distance", "mean"),
                avg_minutes=("duration_min", "mean"))
           .reset_index()
           .rename(columns={f"{side}_zone": "zone", f"{side}_borough": "borough"})
           .sort_values("trips", ascending=False)
           .head(top)
           .reset_index(drop=True))
    return _round_cols(out, {"avg_fare": 2, "avg_tip": 2, "avg_miles": 2,
                             "avg_minutes": 1})


def hourly_activity(frame):
    out = (_clean(frame).groupby("pickup_hour")
           .agg(trips=("fare_amount", "size"),
                avg_fare=("fare_amount", "mean"),
                avg_miles=("trip_distance", "mean"),
                revenue=("total_amount", "sum"))
           .reset_index()
           .rename(columns={"pickup_hour": "hour_of_day"}))
    return _round_cols(out, {"avg_fare": 2, "avg_miles": 2, "revenue": 0})


def weekday_activity(frame):
    out = (_clean(frame).groupby(["pickup_dow", "pickup_day_name"])
           .agg(trips=("fare_amount", "size"),
                avg_fare=("fare_amount", "mean"),
                avg_tip=("tip_amount", "mean"))
           .reset_index()
           .rename(columns={"pickup_dow": "dow",
                            "pickup_day_name": "day_of_week"}))
    return _round_cols(out, {"avg_fare": 2, "avg_tip": 2})


def monthly_activity(frame):
    # The 7 pickups dated outside 2023 are informational, not suspect, so they
    # are not filtered out here either — at 7 rows in 787k they cannot move a
    # monthly total, and hiding them would be the larger distortion.
    out = (_clean(frame).groupby(["pickup_month", "pickup_month_name"])
           .agg(trips=("fare_amount", "size"),
                avg_fare=("fare_amount", "mean"),
                revenue=("total_amount", "sum"))
           .reset_index()
           .rename(columns={"pickup_month": "month_no",
                            "pickup_month_name": "month_name"}))
    return _round_cols(out, {"avg_fare": 2, "revenue": 0})


def od_pairs(frame, top=15, cross_borough_only=False):
    clean = _clean(frame)
    if cross_borough_only:
        clean = clean[clean.pu_borough != clean.do_borough]
    out = (clean.groupby(["pu_zone", "pu_borough", "do_zone", "do_borough"],
                         dropna=False)
           .agg(trips=("fare_amount", "size"),
                avg_fare=("fare_amount", "mean"),
                avg_miles=("trip_distance", "mean"),
                avg_minutes=("duration_min", "mean"))
           .reset_index()
           .rename(columns={"pu_zone": "origin", "pu_borough": "origin_borough",
                            "do_zone": "destination",
                            "do_borough": "dest_borough"})
           .sort_values("trips", ascending=False)
           .head(top)
           .reset_index(drop=True))
    return _round_cols(out, {"avg_fare": 2, "avg_miles": 2, "avg_minutes": 1})


def borough_economics(frame):
    clean = _clean(frame)
    clean = clean[clean.trip_distance > 0]
    grouped = clean.groupby("pu_borough", dropna=False)
    out = (grouped.agg(trips=("fare_amount", "size"),
                       avg_miles=("trip_distance", "mean"),
                       avg_minutes=("duration_min", "mean"),
                       avg_fare=("fare_amount", "mean"),
                       fare_sum=("fare_amount", "sum"),
                       mile_sum=("trip_distance", "sum"),
                       revenue=("total_amount", "sum"))
           .reset_index()
           .rename(columns={"pu_borough": "borough"}))
    # Ratio of sums, not mean of ratios: the per-row ratio is dominated by short
    # trips, where the flag drop makes every mile look expensive.
    out["fare_per_mile"] = out.fare_sum / out.mile_sum
    out = out.drop(columns=["fare_sum", "mile_sum"])
    out = out[["borough", "trips", "avg_miles", "avg_minutes", "avg_fare",
               "fare_per_mile", "revenue"]]
    out = out.sort_values("trips", ascending=False).reset_index(drop=True)
    return _round_cols(out, {"avg_miles": 2, "avg_minutes": 1, "avg_fare": 2,
                             "fare_per_mile": 2, "revenue": 0})


def tipping_by_zone(frame, top=15, min_trips=1000):
    clean = _clean(frame)
    clean = clean[(clean.payment_type == 1) & (clean.fare_amount > 0)]
    out = (clean.groupby(["pu_zone", "pu_borough"], dropna=False)
           .agg(card_trips=("fare_amount", "size"),
                avg_fare=("fare_amount", "mean"),
                avg_tip=("tip_amount", "mean"),
                tip_sum=("tip_amount", "sum"),
                fare_sum=("fare_amount", "sum"))
           .reset_index()
           .rename(columns={"pu_zone": "zone", "pu_borough": "borough"}))
    out = out[out.card_trips >= min_trips]
    out["tip_pct"] = out.tip_sum / out.fare_sum * 100
    out = out.drop(columns=["tip_sum", "fare_sum"])
    out = (out.sort_values("tip_pct", ascending=False).head(top)
           .reset_index(drop=True))
    return _round_cols(out, {"avg_fare": 2, "avg_tip": 2, "tip_pct": 1})


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

WORKFLOWS = {
    "quality": lambda f: [("QUALITY RULE HIT COUNTS", quality_report(f))],
    "zones": lambda f: [
        ("BUSIEST PICKUP ZONES", busiest_zones(f, "pickup")),
        ("BUSIEST DROP-OFF ZONES", busiest_zones(f, "dropoff")),
    ],
    "activity": lambda f: [
        ("ACTIVITY BY HOUR OF DAY", hourly_activity(f)),
        ("ACTIVITY BY DAY OF WEEK", weekday_activity(f)),
        ("ACTIVITY BY MONTH", monthly_activity(f)),
    ],
    "pairs": lambda f: [
        ("MOST COMMON ORIGIN -> DESTINATION PAIRS", od_pairs(f)),
        ("MOST COMMON PAIRS THAT CROSS A BOROUGH LINE",
         od_pairs(f, cross_borough_only=True)),
    ],
    "money": lambda f: [
        ("FARE ECONOMICS BY PICKUP BOROUGH", borough_economics(f)),
        ("BEST-TIPPING ZONES (card payments, >= 1,000 trips)",
         tipping_by_zone(f)),
    ],
}


def quality_report(frame):
    """Per-rule hit counts and percentages — the pandas build_quality report."""
    flags = flag(frame)
    total = len(frame)
    return pd.DataFrame([
        {"rule": rule.code, "severity": rule.severity,
         "rows": int(flags[rule.code].sum()),
         "pct_of_file": round(float(flags[rule.code].sum()) / total * 100, 3)}
        for rule in RULES
    ])


def _show(title, frame):
    print()
    print("=" * 78)
    print(title)
    print("=" * 78)
    print(frame.to_string(index=False))


def main(argv):
    names = [a for a in argv[1:] if not a.startswith("-")] or list(WORKFLOWS)
    use_cache = "--cache" in argv[1:]
    unknown = [n for n in names if n not in WORKFLOWS]
    if unknown:
        print(f"unknown workflow(s): {', '.join(unknown)}")
        print(f"available: {', '.join(WORKFLOWS)}")
        return 1

    frame = build(use_cache=use_cache, verbose=True)
    problems = structural_report(frame)
    print("structural problems:",
          problems if problems else "none — every row typed cleanly")

    started = time.perf_counter()
    for name in names:
        for title, result in WORKFLOWS[name](frame):
            _show(title, result)
    print(f"\nanswered in {time.perf_counter() - started:.2f}s "
          f"(after the parse, which is the part a database does once)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
