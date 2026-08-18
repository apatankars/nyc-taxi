"""Profile the raw trip CSV before designing the schema or the quality rules.

Run:  python src/profile_trips.py

This runs entirely in pandas, against the file, with no IRIS involvement. That
is the point: decide what the columns *are* and what "questionable" *means* from
the data, rather than picking thresholds first and discovering afterwards that
they match nothing.

Every column is read as a string. Letting pandas infer types would silently
coerce the messy values this script exists to find — a non-numeric fare becomes
NaN and is indistinguishable from an empty one.
"""

import os

import pandas as pd

CSV_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data", "2023_Green_Taxi_Trip_Data.csv",
)

TS_FORMAT = "%m/%d/%Y %I:%M:%S %p"

NUMERIC = [
    "trip_distance", "fare_amount", "extra", "mta_tax", "tip_amount",
    "tolls_amount", "ehail_fee", "improvement_surcharge", "total_amount",
    "congestion_surcharge", "passenger_count",
]
CODES = ["VendorID", "RatecodeID", "payment_type", "trip_type", "store_and_fwd_flag"]


def to_num(series):
    """Coerce a string column to numeric, stripping thousands separators first.

    This matters more than it looks. Large values in this file are written with
    a comma — '1,069.67', '4,003' — and the comma survives because every field
    is quoted. Convert without stripping it and those values become NaN, which
    means the rows silently lost are *the largest ones in the column*: the
    reported max for trip_distance drops from 1,630.86 to 976.84 and for
    fare_amount from 4,003 to 824.10.

    So the naive parse does not merely lose 578 rows out of 787,060 — it loses
    precisely the outliers a data-quality workflow exists to surface.
    """
    return pd.to_numeric(series.str.replace(",", "", regex=False), errors="coerce")


def rule(label, mask, total):
    """Print one candidate quality rule with its hit count and share."""
    n = int(mask.sum())
    print(f"  {label:<46} {n:>7,}  {n / total * 100:>6.3f}%")
    return n


def main():
    print(f"reading {os.path.basename(CSV_PATH)} "
          f"({os.path.getsize(CSV_PATH) / 1e6:.0f} MB) as strings ...")
    df = pd.read_csv(CSV_PATH, dtype=str, keep_default_na=False, na_values=[""])
    total = len(df)
    print(f"{total:,} rows x {len(df.columns)} columns\n")

    print("=" * 72)
    print("MISSING VALUES")
    print("=" * 72)
    missing = df.isna().sum()
    for col in df.columns:
        n = int(missing[col])
        flag = "  <-- entirely empty" if n == total else ""
        print(f"  {col:<26} {n:>7,}  {n / total * 100:>6.2f}%{flag}")

    print()
    print("=" * 72)
    print("TIMESTAMPS")
    print("=" * 72)
    pu = pd.to_datetime(df["lpep_pickup_datetime"], format=TS_FORMAT, errors="coerce")
    do = pd.to_datetime(df["lpep_dropoff_datetime"], format=TS_FORMAT, errors="coerce")
    print(f"  pickup unparseable  {int(pu.isna().sum()):>7,}")
    print(f"  dropoff unparseable {int(do.isna().sum()):>7,}")
    print(f"  pickup range   {pu.min()}  ..  {pu.max()}")
    print(f"  dropoff range  {do.min()}  ..  {do.max()}")
    outside = ((pu < "2023-01-01") | (pu >= "2024-01-01")).sum()
    print(f"  pickups outside 2023  {int(outside):,}   <-- data set is billed as 2023 only")

    duration = (do - pu).dt.total_seconds() / 60.0

    print()
    print("=" * 72)
    print("NUMERIC COLUMNS  (commas stripped; 'commas' = values written 1,234.5)")
    print("=" * 72)
    num = {}
    for col in NUMERIC:
        s = to_num(df[col])
        num[col] = s
        commas = int(df[col].fillna("").str.contains(",").sum())
        bad = int((df[col].notna() & s.isna()).sum())
        print(f"  {col:<24} min {s.min():>12.2f}  max {s.max():>12.2f}  "
              f"mean {s.mean():>9.2f}  commas {commas:>4}  bad {bad}")

    print()
    print("=" * 72)
    print("CODE COLUMNS  (value -> count)")
    print("=" * 72)
    for col in CODES:
        counts = df[col].value_counts(dropna=False).head(8).to_dict()
        print(f"  {col:<22} {counts}")

    print()
    print("=" * 72)
    print("LOCATION IDS  (valid range is 1..265, per Taxi.Zone)")
    print("=" * 72)
    for col in ("PULocationID", "DOLocationID"):
        s = to_num(df[col])
        bad = int(((s < 1) | (s > 265) | s.isna()).sum())
        print(f"  {col:<16} out of range/unparseable: {bad:,}")

    print()
    print("=" * 72)
    print("CANDIDATE QUALITY RULES  (this is what drives Taxi.SuspectTrip)")
    print("=" * 72)
    dist = num["trip_distance"]
    fare = num["fare_amount"]
    totl = num["total_amount"]
    pax = num["passenger_count"]
    # Implied average speed. Guard the divide: a zero-minute trip is caught by
    # its own rule below, and inf would poison the comparison.
    speed = dist / (duration / 60.0).where(duration > 0)

    rule("trip_distance == 0", dist == 0, total)
    rule("trip_distance < 0", dist < 0, total)
    rule("trip_distance > 100 miles", dist > 100, total)
    rule("duration <= 0 min (dropoff before pickup)", duration <= 0, total)
    rule("duration > 24 h", duration > 24 * 60, total)
    rule("duration < 1 min", duration < 1, total)
    rule("fare_amount < 0", fare < 0, total)
    rule("fare_amount == 0", fare == 0, total)
    rule("fare_amount > 500", fare > 500, total)
    rule("total_amount < 0", totl < 0, total)
    rule("passenger_count == 0", pax == 0, total)
    rule("passenger_count > 6", pax > 6, total)
    rule("implied speed > 80 mph", speed > 80, total)
    rule("distance > 0 but fare == 0", (dist > 0) & (fare == 0), total)
    rule("fare > 0 but distance == 0", (fare > 0) & (dist == 0), total)

    any_flag = (
        (dist <= 0) | (dist > 100) | (duration <= 0) | (duration > 24 * 60)
        | (fare < 0) | (fare > 500) | (totl < 0) | (pax == 0) | (speed > 80)
    )
    n = int(any_flag.sum())
    print(f"\n  {'ROWS MATCHING AT LEAST ONE RULE':<46} {n:>7,}  {n / total * 100:>6.3f}%")

    print()
    print("=" * 72)
    print("PERCENTILES  (for picking thresholds that are not arbitrary)")
    print("=" * 72)
    qs = [0.001, 0.01, 0.5, 0.99, 0.999, 0.9999]
    frame = pd.DataFrame({
        "trip_distance": dist, "fare_amount": fare,
        "total_amount": totl, "duration_min": duration,
    })
    print(frame.quantile(qs).round(2).to_string())


if __name__ == "__main__":
    main()
