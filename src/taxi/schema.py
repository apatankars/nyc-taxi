"""Table definitions, generated from Python and executed as DDL in IRIS.

The shape of the pipeline is:

    CSV --LOAD DATA--> Taxi.TripRaw --INSERT..SELECT--> Taxi.Trip --UPDATE--> flags
         (server-side)  all VARCHAR   (cast + enrich,     typed +   (quality.py)
                                       server-side JOIN)  enriched

Taxi.TripRaw exists because the source data is not clean enough to load straight
into typed columns. Timestamps are ``MM/DD/YYYY HH:MM:SS AM`` (a 12-hour clock,
which no IRIS default recognises), and several columns are empty on a large
minority of rows. Landing everything as VARCHAR means a bad value produces a row
we can *inspect and count*, rather than a load that aborts partway through.

The derived columns on Taxi.Trip (TripMinutes, AvgMph, PickupHour, ...) are
computed once, in IRIS, during the cast. Every later query groups or filters on
them, so paying for them once at load time keeps the analytics queries cheap.
"""

from typing import List, Optional, Tuple

from . import db
from .config import IrisConfig, RAW_TRIPS, SCHEMA, TRIPS, ZONES
from .quality import RULES, invalidating_columns

# The trip CSV's header names, in file order.
#
# When LOAD DATA is told the file has a header it matches columns *by name*, not
# by position -- a mismatch fails with "Invalid VALUE column, '<name>' is not
# defined in header" rather than by silently loading into the wrong column. These
# names therefore double as both the landing table's columns and the source
# header names, which is why they keep the CSV's own spelling and casing rather
# than being tidied up.
RAW_COLUMNS: List[str] = [
    "VendorID",
    "lpep_pickup_datetime",
    "lpep_dropoff_datetime",
    "store_and_fwd_flag",
    "RatecodeID",
    "PULocationID",
    "DOLocationID",
    "passenger_count",
    "trip_distance",
    "fare_amount",
    "extra",
    "mta_tax",
    "tip_amount",
    "tolls_amount",
    "ehail_fee",
    "improvement_surcharge",
    "total_amount",
    "payment_type",
    "trip_type",
    "congestion_surcharge",
]

# (landing column, CSV header) pairs, passed to LOAD DATA as an explicit
# INTO (...) VALUES (...) mapping. The trip file's headers are usable as column
# names as-is, so that mapping is the identity.
TRIP_COLUMN_MAP: List[Tuple[str, str]] = [(c, c) for c in RAW_COLUMNS]

# The zone file's headers are not: "Zone" would be a confusing column name inside
# a table called Taxi.Zone, and "service_zone" does not match the project's
# casing. So this mapping is where the rename happens, once, explicitly.
ZONE_COLUMN_MAP: List[Tuple[str, str]] = [
    ("LocationID", "LocationID"),
    ("Borough", "Borough"),
    ("ZoneName", "Zone"),
    ("ServiceZone", "service_zone"),
]

# Money and distance columns, so the cast and the analytics agree on precision.
_MONEY = "NUMERIC(12,2)"


def _raw_ddl() -> str:
    # Unquoted identifiers: IRIS folds them case-insensitively, which is what
    # LOAD DATA's header matching and the cast's SELECT both rely on.
    cols = ",\n    ".join(f"{c} VARCHAR(64)" for c in RAW_COLUMNS)
    return f"CREATE TABLE {RAW_TRIPS} (\n    {cols}\n)"


def _zones_ddl() -> str:
    # ZoneName rather than Zone: the source header says "Zone", but a column of
    # that name inside a table called Taxi.Zone is needlessly confusing to read
    # in a JOIN.
    # COLLATE SQLSTRING for the same reason as on Taxi.Trip: these values are
    # display labels and must survive a GROUP BY with their casing intact.
    return f"""CREATE TABLE {ZONES} (
    LocationID  INTEGER NOT NULL PRIMARY KEY,
    Borough     VARCHAR(50)  COLLATE SQLSTRING,
    ZoneName    VARCHAR(100) COLLATE SQLSTRING,
    ServiceZone VARCHAR(50)  COLLATE SQLSTRING
)"""


def _zones_raw_ddl() -> str:
    cols = ",\n    ".join(f"{target} VARCHAR(128)" for target, _ in ZONE_COLUMN_MAP)
    return f"CREATE TABLE {SCHEMA}.ZoneRaw (\n    {cols}\n)"


def _trips_ddl() -> str:
    # One BIT column per quality rule, named by the rule registry in quality.py.
    # Generating them here means adding a rule is a one-line change in one file.
    flag_cols = ",\n    ".join(f"{r.column} BIT DEFAULT 0" for r in RULES)
    return f"""CREATE TABLE {TRIPS} (
    -- Source fields, typed.
    VendorID              SMALLINT,
    PickupDateTime        TIMESTAMP,
    DropoffDateTime       TIMESTAMP,
    StoreAndFwdFlag       VARCHAR(1),
    RatecodeID            SMALLINT,
    PULocationID          INTEGER,
    DOLocationID          INTEGER,
    PassengerCount        SMALLINT,
    TripDistance          NUMERIC(10,2),
    FareAmount            {_MONEY},
    Extra                 {_MONEY},
    MtaTax                {_MONEY},
    TipAmount             {_MONEY},
    TollsAmount           {_MONEY},
    ImprovementSurcharge  {_MONEY},
    TotalAmount           {_MONEY},
    PaymentType           SMALLINT,
    TripType              SMALLINT,
    CongestionSurcharge   {_MONEY},

    -- Derived once during the cast, in IRIS. Every analytics query uses these.
    TripMinutes           NUMERIC(10,2),
    AvgMph                NUMERIC(10,2),
    TipPct                NUMERIC(7,2),
    PickupDate            DATE,
    PickupHour            SMALLINT,
    PickupDayOfWeek       SMALLINT,
    PickupMonth           SMALLINT,

    -- Enrichment, denormalised from Taxi.Zone by a JOIN during the cast so that
    -- grouping by borough or zone name needs no join at query time.
    --
    -- COLLATE SQLSTRING is not decoration. IRIS collates string columns as
    -- SQLUPPER by default, and a GROUP BY returns the *collated* value -- so
    -- grouping by an uncollated PUBorough yields 'MANHATTAN' while selecting the
    -- same column directly yields 'Manhattan'. Every query here groups by these
    -- columns, so without this the entire report shouts. SQLSTRING preserves
    -- case; the trade-off is that comparisons against them become
    -- case-sensitive, which is why the 'Unknown' sentinel below is written to
    -- match the lookup file's own casing exactly.
    PUBorough             VARCHAR(50)  COLLATE SQLSTRING,
    PUZoneName            VARCHAR(100) COLLATE SQLSTRING,
    DOBorough             VARCHAR(50)  COLLATE SQLSTRING,
    DOZoneName            VARCHAR(100) COLLATE SQLSTRING,

    -- Quality, written by quality.apply_rules() as a separate re-runnable pass.
    QualityIssueCount     SMALLINT DEFAULT 0,
    {flag_cols}
)"""


def _reject_ddl() -> str:
    """Rows that could not be cast out of TripRaw, kept with a reason.

    A prototype that silently drops unparseable rows cannot answer "how much of
    the file did you actually load?", which is the first question anyone asks.
    """
    return f"""CREATE TABLE {SCHEMA}.TripReject (
    RawID   INTEGER,
    Reason  VARCHAR(200),
    Pickup  VARCHAR(64),
    Dropoff VARCHAR(64)
)"""


# Indexes are created *after* the data is loaded and flagged -- see
# create_indexes(). Building them first would mean maintaining them across
# 787k inserts for no benefit.
def _index_ddl() -> List[str]:
    return [
        # Bitmap indexes suit low-cardinality columns, which is what most of the
        # grouping keys here are (24 hours, 12 months, 7 boroughs).
        f"CREATE BITMAP INDEX TripPickupHour ON {TRIPS} (PickupHour)",
        f"CREATE BITMAP INDEX TripPickupMonth ON {TRIPS} (PickupMonth)",
        f"CREATE BITMAP INDEX TripPUBorough ON {TRIPS} (PUBorough)",
        f"CREATE BITMAP INDEX TripPaymentType ON {TRIPS} (PaymentType)",
        f"CREATE BITMAP INDEX TripIssueCount ON {TRIPS} (QualityIssueCount)",
        # The individual flag columns analytics.py filters *rows* on. Derived from
        # the measure map rather than listed, so a rule that starts invalidating
        # trip_count/location/time gets an index without anyone remembering to add
        # one. The other flag columns are only ever read inside a CASE guard,
        # where an index cannot help, so they do not get one.
        *(
            f"CREATE BITMAP INDEX Trip{column} ON {TRIPS} ({column})"
            for column in invalidating_columns("trip_count", "location", "time")
        ),
        # Standard indexes for the higher-cardinality grouping keys.
        f"CREATE INDEX TripPUZone ON {TRIPS} (PUZoneName)",
        f"CREATE INDEX TripODPair ON {TRIPS} (PULocationID, DOLocationID)",
        f"CREATE INDEX TripPickupDate ON {TRIPS} (PickupDate)",
    ]


def create_all(cfg: Optional[IrisConfig] = None, drop_existing: bool = True) -> None:
    """(Re)create every table. Safe to run repeatedly."""
    statements: List[str] = []
    if drop_existing:
        for table in (RAW_TRIPS, TRIPS, ZONES, f"{SCHEMA}.ZoneRaw", f"{SCHEMA}.TripReject"):
            statements.append(f"DROP TABLE IF EXISTS {table}")
    statements += [_zones_raw_ddl(), _zones_ddl(), _raw_ddl(), _trips_ddl(), _reject_ddl()]

    print(f"Creating schema {SCHEMA} ({len(RULES)} quality flag columns)")
    with db.timed("create_all"):
        db.execute_script(statements, cfg)


def create_indexes(cfg: Optional[IrisConfig] = None) -> None:
    """Build the analytical indexes. Run after loading, not before."""
    print("Building indexes on the typed table")
    with db.timed("create_indexes"):
        db.execute_script(_index_ddl(), cfg)


def tune_tables(cfg: Optional[IrisConfig] = None) -> None:
    """Collect table statistics so the IRIS query optimiser has real numbers.

    Without this the optimiser plans against defaults, which on a 787k-row table
    is the difference between a bitmap-index plan and a full scan. It matters
    for the IRIS-side half of the bench.py comparison.
    """
    print("Tuning table statistics")
    with db.timed("tune_tables"):
        db.execute(f"TUNE TABLE {TRIPS}", (), cfg)
