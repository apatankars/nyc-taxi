"""Bulk ingest, done by IRIS rather than by Python.

The 100 MB trip file is read by the *server*, via ``LOAD DATA``, straight off the
bind-mounted ``/data`` directory. Python issues one statement and waits.

The alternatives were measured rather than assumed, in bench.py, and the result is
more nuanced than "the database is faster":

* ``LOAD DATA`` is the quickest, and reads the full 787k-row file in about a
  second flat -- IRIS parallelises it across a pool of writer processes.
* ``executemany`` in batches of 5,000 is only about 2x behind it. Batched Python
  inserts are a perfectly reasonable way to load this file; anyone claiming
  otherwise has not measured it.
* One ``execute`` per row is the approach that actually hurts: ~6,800 rows/sec
  against ``LOAD DATA``'s ~244,000, a factor of about 36, because each of 787k
  rows pays for its own round trip.

So the real lesson is about *batching*, not about Python versus SQL. ``LOAD DATA``
is still what this project uses: it is the fastest option, it is one statement
rather than a loop, and it needs no chunking logic to get right.

Two practical notes:

* ``LOAD DATA`` paths are resolved *inside the container*. ``/data/...``, never
  ``./data/...``.
* Loading is done through db.exec_direct (Native API) rather than the DB-API,
  because the ``USING {...}`` clause cannot survive the DB-API's ODBC escape
  handling. See db.exec_direct for the details.
"""

from typing import List, Optional, Tuple

import pandas as pd

from . import db
from .config import IrisConfig, RAW_TRIPS, SCHEMA, TRIPS_CSV, ZONES, ZONES_CSV
from .schema import TRIP_COLUMN_MAP, ZONE_COLUMN_MAP

# Tells LOAD DATA that line 1 is a header rather than a row. Without it the header
# is ingested as data -- verified: it lands as a row whose VendorID is the literal
# string "VENDORID", which then survives all the way into the analytics.
_SKIP_HEADER = '{"from":{"file":{"header":1}}}'


def _load_file(
    csv_path: str,
    table: str,
    column_map: List[Tuple[str, str]],
    cfg: Optional[IrisConfig] = None,
) -> int:
    """LOAD DATA with an explicit target-column / source-header mapping.

    The ``INTO tbl (targets) VALUES (headers)`` form is used even where the two
    lists are identical. Relying on implicit matching means a renamed CSV column
    fails at load time with a message about headers; stating the mapping keeps
    that correspondence in one readable place instead.
    """
    targets = ", ".join(target for target, _ in column_map)
    headers = ", ".join(header for _, header in column_map)
    sql = (
        f"LOAD DATA FROM FILE '{csv_path}' INTO {table} ({targets}) "
        f"VALUES ({headers}) USING {_SKIP_HEADER}"
    )
    info = db.exec_direct(sql, cfg)
    return int(info["rowcount"] or 0)


def load_zones(cfg: Optional[IrisConfig] = None) -> int:
    """Load the 265-row zone lookup, then type it.

    Small enough that the staging step costs nothing, and it keeps the lookup
    table's LocationID a real INTEGER primary key for the enrichment join.
    """
    print(f"Loading zone lookup from {ZONES_CSV}")
    with db.timed("load_zones"):
        db.execute(f"DELETE FROM {SCHEMA}.ZoneRaw", (), cfg)
        rows = _load_file(ZONES_CSV, f"{SCHEMA}.ZoneRaw", ZONE_COLUMN_MAP, cfg)

    with db.timed("cast_zones"):
        db.execute(f"DELETE FROM {ZONES}", (), cfg)
        db.execute(
            f"""
            INSERT INTO {ZONES} (LocationID, Borough, ZoneName, ServiceZone)
            SELECT CAST(LocationID AS INTEGER), Borough, ZoneName, ServiceZone
            FROM {SCHEMA}.ZoneRaw
            WHERE LocationID IS NOT NULL
            """,
            (),
            cfg,
        )

    loaded = db.row_count(ZONES, cfg)
    print(f"  {rows} rows read, {loaded} zones typed")
    return loaded


def load_trips(cfg: Optional[IrisConfig] = None) -> int:
    """Load the full trip file into the all-VARCHAR landing table."""
    print(f"Loading trips from {TRIPS_CSV} (server-side LOAD DATA)")
    with db.timed("load_trips"):
        db.execute(f"DELETE FROM {RAW_TRIPS}", (), cfg)
        rows = _load_file(TRIPS_CSV, RAW_TRIPS, TRIP_COLUMN_MAP, cfg)

    loaded = db.row_count(RAW_TRIPS, cfg)
    print(f"  {loaded} raw rows loaded (statement reported {rows})")
    return loaded


def load_diagnostics(cfg: Optional[IrisConfig] = None) -> pd.DataFrame:
    """Whatever the most recent LOAD DATA could not read.

    IRIS writes per-row load errors to %SQL_Diag.Message, keyed by a
    %SQL_Diag.Result id. An empty frame here means every line of the file was
    accepted into the landing table -- which is the expected outcome, because the
    landing table is all VARCHAR and imposes no constraints to violate. Values
    that are *wrong* rather than unreadable are caught later, by the cast
    (transform.py) and the rules (quality.py).
    """
    latest = db.scalar("SELECT MAX(ID) FROM %SQL_Diag.Result", (), cfg)
    if latest is None:
        return pd.DataFrame(columns=["diag_result", "severity", "message"])
    return db.query_df(
        """
        SELECT TOP 100 diagResult AS diag_result, severity, message
        FROM %SQL_Diag.Message
        WHERE diagResult = ?
        ORDER BY ID
        """,
        (latest,),
        cfg,
    )


def load_all(cfg: Optional[IrisConfig] = None) -> dict:
    zones = load_zones(cfg)
    trips = load_trips(cfg)
    return {"zones": zones, "raw_trips": trips}
