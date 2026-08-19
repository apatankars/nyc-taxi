"""Cast the landing table into typed, enriched rows -- one server-side statement.

Python builds the SQL; IRIS does the work. Nothing here iterates over rows.

What this stage has to cope with, all of it discovered by profiling the raw table
rather than assumed (see ``profile_raw`` at the bottom, which is what found it):

* **12-hour timestamps.** ``lpep_pickup_datetime`` is ``MM/DD/YYYY hh:mm:ss AM``.
  All 787,060 values are exactly 22 characters and match that shape, so
  ``TO_TIMESTAMP(x, 'MM/DD/YYYY HH:MI:SS AM')`` handles the file without
  exception. Note it gets the meridian right: ``12:26:10 AM`` becomes 00:26:10.

* **Thousands separators inside numbers.** 554 ``trip_distance`` values and 12
  ``fare_amount``/``total_amount`` values are written like ``"2,032.67"``. The
  quotes mean the CSV parses cleanly, but the *string* is not numeric, so a plain
  CAST fails on them. Stripping the comma first repairs every one of them: after
  ``REPLACE(x, ',', '')`` there are zero unparseable numeric cells in the file.
  These rows are also genuinely suspect -- a 2,032-mile green-taxi trip -- and
  the quality rules flag them once they are numbers we can compare.

* **A block of systematically missing fields.** 55,613 rows have no
  ``passenger_count``, ``RatecodeID``, ``store_and_fwd_flag`` or
  ``congestion_surcharge``. They are spread evenly across all 12 months and both
  vendors, so this is not one broken feed; it is about 7% of the file throughout.
  They are loaded, not rejected, and flagged by ``missing_passenger_count``.

* **An entirely empty column.** ``ehail_fee`` is NULL on all 787,060 rows, so it
  is not carried into the typed table at all.
"""

from typing import Dict, List, Optional

import pandas as pd

from . import db
from .config import IrisConfig, RAW_TRIPS, SCHEMA, TRIPS, ZONES

# The source timestamp format. IRIS's HH:MI:SS AM handles the 12-hour clock.
_TS_FORMAT = "MM/DD/YYYY HH:MI:SS AM"

# A row is loadable if both timestamps have the expected shape. Everything else
# that can go wrong in this file is repairable (commas) or representable as NULL
# (missing fields); an unparseable timestamp is neither, because the whole
# duration-based half of the analysis depends on it.
_VALID_ROW = (
    "lpep_pickup_datetime LIKE '__/__/____ __:__:__ _M' "
    "AND lpep_dropoff_datetime LIKE '__/__/____ __:__:__ _M'"
)


def _ts(column: str) -> str:
    return f"TO_TIMESTAMP({column}, '{_TS_FORMAT}')"


def _num(column: str, precision: str = "NUMERIC(12,2)") -> str:
    """Cast a raw VARCHAR column to a number, tolerating thousands separators.

    Returns NULL rather than raising if a cell still is not numeric after the
    comma is stripped. That keeps one malformed cell in a future file from
    aborting a 787k-row load, and the NULL is then visible to the quality rules
    instead of being silently absent.
    """
    cleaned = f"REPLACE({column}, ',', '')"
    return (
        f"CASE WHEN ISNUMERIC({cleaned}) = 1 "
        f"THEN CAST({cleaned} AS {precision}) ELSE NULL END"
    )


def _int(column: str) -> str:
    return _num(column, "INTEGER")


def _cast_select() -> str:
    """The SELECT that does the casting, deriving and enriching, all in one pass."""
    pickup = _ts("r.lpep_pickup_datetime")
    dropoff = _ts("r.lpep_dropoff_datetime")

    # Duration in minutes, from a whole-second difference. Computed here rather
    # than as a column expression so AvgMph can reuse it in the same SELECT.
    minutes = f"(DATEDIFF('second', {pickup}, {dropoff}) / 60.0)"
    distance = _num("r.trip_distance", "NUMERIC(10,2)")
    fare = _num("r.fare_amount")
    tip = _num("r.tip_amount")

    return f"""
    SELECT
        {_int('r.VendorID')},
        {pickup},
        {dropoff},
        r.store_and_fwd_flag,
        {_int('r.RatecodeID')},
        {_int('r.PULocationID')},
        {_int('r.DOLocationID')},
        {_int('r.passenger_count')},
        {distance},
        {fare},
        {_num('r.extra')},
        {_num('r.mta_tax')},
        {tip},
        {_num('r.tolls_amount')},
        {_num('r.improvement_surcharge')},
        {_num('r.total_amount')},
        {_int('r.payment_type')},
        {_int('r.trip_type')},
        {_num('r.congestion_surcharge')},

        -- Derived. Guarded so a zero or negative duration yields NULL instead of
        -- a divide-by-zero; the quality rules report those rows separately.
        {minutes},
        CASE WHEN {minutes} > 0
             THEN CAST({distance} / ({minutes} / 60.0) AS NUMERIC(10,2))
             ELSE NULL END,
        CASE WHEN {fare} > 0
             THEN CAST(100.0 * {tip} / {fare} AS NUMERIC(7,2))
             ELSE NULL END,
        CAST({pickup} AS DATE),
        DATEPART('hour', {pickup}),
        DAYOFWEEK({pickup}),
        MONTH({pickup}),

        -- Enrichment. LEFT JOIN, not INNER: a location id absent from the lookup
        -- must still produce a trip row, flagged as an unknown zone, rather than
        -- vanishing from the totals.
        COALESCE(pu.Borough,  'Unknown'),
        COALESCE(pu.ZoneName, 'Unknown'),
        COALESCE(dof.Borough,  'Unknown'),
        COALESCE(dof.ZoneName, 'Unknown')
    FROM {RAW_TRIPS} r
        LEFT JOIN {ZONES} pu  ON pu.LocationID  = {_int('r.PULocationID')}
        LEFT JOIN {ZONES} dof ON dof.LocationID = {_int('r.DOLocationID')}
    WHERE {_VALID_ROW}
    """


_TARGET_COLUMNS = """
    VendorID, PickupDateTime, DropoffDateTime, StoreAndFwdFlag, RatecodeID,
    PULocationID, DOLocationID, PassengerCount, TripDistance, FareAmount,
    Extra, MtaTax, TipAmount, TollsAmount, ImprovementSurcharge, TotalAmount,
    PaymentType, TripType, CongestionSurcharge,
    TripMinutes, AvgMph, TipPct, PickupDate, PickupHour, PickupDayOfWeek,
    PickupMonth, PUBorough, PUZoneName, DOBorough, DOZoneName
"""


def cast_and_enrich(cfg: Optional[IrisConfig] = None) -> Dict[str, int]:
    """Populate Taxi.Trip from Taxi.TripRaw. Returns loaded and rejected counts."""
    print(f"Casting and enriching {RAW_TRIPS} -> {TRIPS}")

    with db.timed("cast_and_enrich"):
        db.execute(f"DELETE FROM {TRIPS}", (), cfg)
        db.execute(
            f"INSERT INTO {TRIPS} ({_TARGET_COLUMNS}) {_cast_select()}", (), cfg
        )

    with db.timed("record_rejects"):
        db.execute(f"DELETE FROM {SCHEMA}.TripReject", (), cfg)
        db.execute(
            f"""
            INSERT INTO {SCHEMA}.TripReject (RawID, Reason, Pickup, Dropoff)
            SELECT r.%ID, 'Unparseable pickup or drop-off timestamp',
                   r.lpep_pickup_datetime, r.lpep_dropoff_datetime
            FROM {RAW_TRIPS} r
            WHERE NOT ({_VALID_ROW})
            """,
            (),
            cfg,
        )

    loaded = db.row_count(TRIPS, cfg)
    rejected = db.row_count(f"{SCHEMA}.TripReject", cfg)
    raw = db.row_count(RAW_TRIPS, cfg)
    print(f"  {loaded} typed trips, {rejected} rejected, from {raw} raw rows")
    if loaded + rejected != raw:
        raise RuntimeError(
            f"Row accounting does not balance: {loaded} + {rejected} != {raw}"
        )
    return {"loaded": loaded, "rejected": rejected, "raw": raw}


def rejects(limit: int = 20, cfg: Optional[IrisConfig] = None) -> pd.DataFrame:
    return db.query_df(
        f"SELECT TOP {int(limit)} RawID, Reason, Pickup, Dropoff FROM {SCHEMA}.TripReject",
        (),
        cfg,
    )


def profile_raw(cfg: Optional[IrisConfig] = None) -> pd.DataFrame:
    """Describe the landing table's data-quality problems, in SQL.

    This is the query that drove the design of the cast above, kept in the code
    so the reasoning is reproducible rather than a story told in a comment. Every
    count is computed by IRIS; a 12-row frame comes back.
    """
    numeric_columns: List[str] = [
        "trip_distance",
        "fare_amount",
        "total_amount",
        "tip_amount",
        "passenger_count",
        "congestion_surcharge",
    ]

    checks = [
        (
            "timestamps not matching MM/DD/YYYY hh:mm:ss XM",
            f"NOT ({_VALID_ROW})",
        ),
        ("ehail_fee empty", "ehail_fee IS NULL"),
        ("passenger_count missing", "passenger_count IS NULL"),
        ("store_and_fwd_flag missing", "store_and_fwd_flag IS NULL"),
        ("congestion_surcharge missing", "congestion_surcharge IS NULL"),
    ] + [
        (
            f"{col}: comma-separated thousands",
            f"{col} IS NOT NULL AND ISNUMERIC({col}) = 0 "
            f"AND ISNUMERIC(REPLACE({col}, ',', '')) = 1",
        )
        for col in numeric_columns
    ] + [
        (
            f"{col}: unparseable even after comma strip",
            f"{col} IS NOT NULL AND ISNUMERIC(REPLACE({col}, ',', '')) = 0",
        )
        for col in numeric_columns
    ]

    selects = ",\n    ".join(
        f"SUM(CASE WHEN {predicate} THEN 1 ELSE 0 END) AS c{i}"
        for i, (_, predicate) in enumerate(checks)
    )
    _, rows = db.query(f"SELECT\n    {selects}\nFROM {RAW_TRIPS}", (), cfg)
    total = db.row_count(RAW_TRIPS, cfg)

    frame = pd.DataFrame(
        {
            "issue": [label for label, _ in checks],
            "rows": [int(v or 0) for v in rows[0]] if rows else 0,
        }
    )
    frame["pct"] = (frame["rows"] / total * 100).round(3) if total else 0.0
    return frame[frame["rows"] > 0].sort_values("rows", ascending=False).reset_index(
        drop=True
    )
