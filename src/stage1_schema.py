"""Stage 1 -- create the schema.

Three layers, and the reason for each:

  TripRaw   Every column VARCHAR. No types, no constraints, no NOT NULL. The load
            cannot fail on data *content* because there is no content it could
            violate -- only structural problems (wrong column count, unreadable
            file) can stop it, and those deserve to fail loudly. Landing raw is
            what makes "what failed to parse, and why" a queryable question
            instead of a lost log line. A row rejected at the door is a row you
            can never report on, and reporting on questionable rows is the
            deliverable.

  Trip      Typed. Populated by casting TripRaw. Keeps all 787,060 rows --
            suspect ones are flagged, never deleted, so the analytics workflow
            and the quality workflow read the same table one predicate apart.

  TripFlag  One row per (trip, violated rule). Normalised rather than relying on
            the flag_mask column, because a GROUP BY here uses a bitmap index
            while masking forces a full scan. Trip.flag_mask is kept as well, but
            for labelling combinations rather than for filtering.
"""

import iris

from db import RAW_COLUMNS, banner, exec_dml, exec_sql, log, scalar, try_exec
import rules

# Dependents first. Trip has reference columns into Zone, so Zone must go last.
DROP_ORDER = [
    "Taxi.TripFlag",
    "Taxi.TripReject",
    "Taxi.TripQuality",
    "Taxi.Trip",
    "Taxi.TripRaw",
    "Taxi.Zone",
]


def build():
    banner("STAGE 1 -- schema")

    # The view first: it depends on Trip, which would otherwise refuse to drop.
    try_exec("DROP VIEW Taxi.TripEnriched")
    for table in DROP_ORDER:
        try_exec(f"DROP TABLE {table}")

    # A DROP can fail silently for a non-obvious reason -- anything still holding
    # an object reference to Taxi.Zone pins it -- and the next CREATE then reports
    # "already exists", which points at the wrong problem. Check rather than hope.
    still_there = [
        table for table in DROP_ORDER
        if scalar(
            "SELECT COUNT(*) FROM INFORMATION_SCHEMA.TABLES "
            "WHERE TABLE_SCHEMA = ? AND TABLE_NAME = ?",
            *table.split("."),
        )
    ]
    if still_there:
        raise RuntimeError(
            f"could not drop {', '.join(still_there)} -- something still "
            f"references them (a leftover table with a Taxi.Zone reference column "
            f"will do it)"
        )
    log(f"dropped {len(DROP_ORDER)} tables (if present)")

    # Make a declared PRIMARY KEY the table's IDKEY for the duration of the build.
    # An object-reference column stores the *RowID* of its target, so
    # Trip.pu_location_id -> Zone is only correct if Zone's RowID is its
    # location_id. By default (DDLPKeyNotIDKey = 1) a DDL primary key is a separate
    # unique constraint and IRIS assigns its own RowID; the two coincide here only
    # because the lookup IDs run 1..265 with no gaps. Setting the option to 0 makes
    # location_id *be* the RowID, so the reference is correct by construction.
    util = iris.cls("%SYSTEM.SQL.Util")
    previous = util.GetOption("DDLPKeyNotIDKey")
    util.SetOption("DDLPKeyNotIDKey", 0)
    try:
        _build_tables()
    finally:
        # Global, instance-wide setting -- always put it back.
        util.SetOption("DDLPKeyNotIDKey", previous)
        log(f"restored DDLPKeyNotIDKey = {previous}")

    count = rules.seed(exec_dml)
    log(f"seeded {count} rules into Taxi.TripQuality")

    build_functions()
    build_views()


def _build_tables():
    # ---- zones ----------------------------------------------------------
    # 'Zone' is awkward as a column name, so it becomes zone_name. This table is
    # populated by Python rather than LOAD DATA, so no header matching applies.
    # Created first: Trip's reference columns point at it.
    exec_sql("""
        CREATE TABLE Taxi.Zone (
            location_id   INTEGER NOT NULL PRIMARY KEY,
            borough       VARCHAR(50),
            zone_name     VARCHAR(100),
            service_zone  VARCHAR(50)
        )
    """)
    log("created Taxi.Zone (location_id is the IDKEY)")

    # ---- raw landing ----------------------------------------------------
    # Column names must match the CSV header verbatim; LOAD DATA pairs them by
    # name and fails with "not defined in header" otherwise.
    raw_ddl = ",\n            ".join(f"{c} VARCHAR(64)" for c in RAW_COLUMNS)
    exec_sql(f"CREATE TABLE Taxi.TripRaw (\n            {raw_ddl}\n        )")
    log(f"created Taxi.TripRaw ({len(RAW_COLUMNS)} VARCHAR columns)")

    # ---- typed trips ----------------------------------------------------
    # duration_min and implied_mph are stored, not computed on read: the compound
    # rules depend on them, and storing keeps those predicates plain SQL.
    exec_sql("""
        CREATE TABLE Taxi.Trip (
            trip_id                BIGINT NOT NULL PRIMARY KEY,
            raw_id                 BIGINT,
            vendor_id              INTEGER,
            pickup_ts              TIMESTAMP,
            dropoff_ts             TIMESTAMP,
            store_and_fwd_flag     VARCHAR(1),
            ratecode_id            INTEGER,
            -- Typed as the Zone class, not INTEGER, so one column serves both
            -- roles: it filters as a plain integer id (bitmap-indexed below) and
            -- supports arrow traversal (pu_location_id->borough), which IRIS
            -- resolves as a left outer join -- no borough strings duplicated
            -- across 787k rows, no explicit JOIN in every query.
            pu_location_id         Taxi.Zone,
            do_location_id         Taxi.Zone,
            passenger_count        INTEGER,
            trip_distance          NUMERIC(12,2),
            fare_amount            NUMERIC(12,2),
            extra                  NUMERIC(12,2),
            mta_tax                NUMERIC(12,2),
            tip_amount             NUMERIC(12,2),
            tolls_amount           NUMERIC(12,2),
            ehail_fee              NUMERIC(12,2),
            improvement_surcharge  NUMERIC(12,2),
            total_amount           NUMERIC(12,2),
            payment_type           INTEGER,
            trip_type              INTEGER,
            congestion_surcharge   NUMERIC(12,2),
            duration_min           NUMERIC(12,2),
            implied_mph            NUMERIC(12,2),
            pickup_hour            INTEGER,
            pickup_month           INTEGER,
            pickup_dow             INTEGER,
            flag_count             INTEGER DEFAULT 0,
            flag_mask              BIGINT DEFAULT 0
        )
    """)
    log("created Taxi.Trip")

    # ---- rules, rejects, flags ------------------------------------------
    exec_sql("""
        CREATE TABLE Taxi.TripQuality (
            rule_id       INTEGER NOT NULL PRIMARY KEY,
            bit_position  INTEGER,
            rule_name     VARCHAR(64),
            phase         VARCHAR(10),
            severity      VARCHAR(10),
            predicate     VARCHAR(500),
            description   VARCHAR(400),
            enabled       BIT
        )
    """)
    exec_sql("""
        CREATE TABLE Taxi.TripReject (
            reject_id   BIGINT NOT NULL IDENTITY PRIMARY KEY,
            raw_id      BIGINT,
            field_name  VARCHAR(64),
            raw_value   VARCHAR(64),
            reason      VARCHAR(200)
        )
    """)
    exec_sql("""
        CREATE TABLE Taxi.TripFlag (
            trip_id  BIGINT NOT NULL,
            rule_id  INTEGER NOT NULL,
            PRIMARY KEY (trip_id, rule_id)
        )
    """)
    log("created Taxi.TripQuality, Taxi.TripReject, Taxi.TripFlag")

    # Bitmap indexes: cheap over a 787k-row extent and they make the clean-set
    # filter and the zone/time rollups fast.
    for stmt in [
        "CREATE BITMAP INDEX TripFlagRule ON Taxi.TripFlag (rule_id)",
        "CREATE BITMAP INDEX TripFlagCount ON Taxi.Trip (flag_count)",
        "CREATE INDEX TripPickupTs ON Taxi.Trip (pickup_ts)",
        "CREATE BITMAP INDEX TripPULoc ON Taxi.Trip (pu_location_id)",
        "CREATE BITMAP INDEX TripDOLoc ON Taxi.Trip (do_location_id)",
        "CREATE BITMAP INDEX TripHour ON Taxi.Trip (pickup_hour)",
        "CREATE BITMAP INDEX TripMonth ON Taxi.Trip (pickup_month)",
    ]:
        try_exec(stmt)
    log("created indexes")


def build_views():
    """A read-only, enriched projection of Trip.

    The enrichment the brief asks for: raw location IDs replaced with borough and
    zone names from the lookup file. A view rather than extra columns on Trip, so
    the 265-row lookup stays the single source of truth.

    %EXACT is required: string fields collate to uppercase by default, so
    GROUP BY borough yields 'MANHATTAN' without it.

    Explicit LEFT JOIN rather than arrow syntax is a bug workaround. Arrow
    traversal is correct on the base table (verified to agree on all 787,060
    rows), but two arrow hops into the same table *inside a view definition* make
    every aggregate over the view -- even a bare COUNT(*) -- fail with

        <UNDEFINED>isStatInvalid+2^%qTable ^||%sql.smd("rv","TAXI.ZONE")

    It is the view's cached metadata that goes bad, so it surfaces after unrelated
    later work rather than at CREATE VIEW time; only a different view definition
    clears it. _verify_view() in stage 4 runs an aggregate so a regression fails
    the build rather than the analytics.
    """
    try_exec("DROP VIEW Taxi.TripEnriched")
    exec_sql("""
        CREATE VIEW Taxi.TripEnriched AS
        SELECT
            t.trip_id,
            t.pickup_ts,
            t.dropoff_ts,
            t.pickup_hour,
            t.pickup_month,
            t.pickup_dow,
            t.pu_location_id,
            %EXACT(pz.borough)      AS pu_borough,
            %EXACT(pz.zone_name)    AS pu_zone,
            %EXACT(pz.service_zone) AS pu_service_zone,
            t.do_location_id,
            %EXACT(dz.borough)      AS do_borough,
            %EXACT(dz.zone_name)    AS do_zone,
            %EXACT(dz.service_zone) AS do_service_zone,
            t.passenger_count,
            t.trip_distance,
            t.duration_min,
            t.implied_mph,
            t.fare_amount,
            t.tip_amount,
            t.total_amount,
            t.payment_type,
            t.flag_count,
            t.flag_mask
        FROM Taxi.Trip t
        LEFT JOIN Taxi.Zone pz ON pz.location_id = t.pu_location_id
        LEFT JOIN Taxi.Zone dz ON dz.location_id = t.do_location_id
    """)
    log("created view Taxi.TripEnriched (zone/borough enrichment via LEFT JOIN)")


def build_functions():
    """Expose flag_mask decoding to SQL as a Python user-defined function.

    IRIS SQL has no bitwise operators ('&', '|' and BITAND all fail to parse, and
    the ObjectScript bit primitives are not callable from SQL), so
    CREATE FUNCTION ... LANGUAGE PYTHON is what makes Trip.flag_mask queryable.
    A UDF in a WHERE clause is opaque to the optimiser and forces a full scan, so
    this is for labelling combinations over an already-narrowed set, not for
    filtering -- that is what TripFlag and its bitmap index are for.
    """
    names = {bit: name for name, bit in rules.BITS.items()}

    # The text between the braces is compiled verbatim as Python, so every body
    # line starts at column 0 or earns "unexpected indent". Last element of each
    # tuple is a build-time self-test: (args, expected).
    definitions = [
        # Turns 34 into 'distance_reformatted,fare_negative' so query output reads
        # without joining back to the rules table.
        ("Taxi.mask_names", "mask BIGINT", "VARCHAR(500)", [
            f"names = {names!r}",
            "m = mask or 0",
            "return ','.join(n for b, n in sorted(names.items()) if m & (1 << b))",
        ], [((0,), ""), ((1,), "cast_failed")]),
    ]

    for name, signature, returns, body, _ in definitions:
        try_exec(f"DROP FUNCTION {name}")
        statement = (
            f"CREATE FUNCTION {name}({signature}) RETURNS {returns}\n"
            f"LANGUAGE PYTHON\n{{\n" + "\n".join(body) + "\n}"
        )
        exec_sql(statement)

    # Call each one: a UDF IRIS cannot invoke does not always raise -- applied
    # across a scan it can return zero rows, which reads as "no trips matched"
    # rather than "broken". Failing loudly at build time catches that.
    for name, _, _, _, cases in definitions:
        for args, expected in cases:
            literal = ", ".join(str(a) for a in args)
            got = exec_sql(f"SELECT {name}({literal})")[0][0]
            if str(got) != str(expected):
                raise RuntimeError(
                    f"{name}({literal}) returned {got!r}, expected {expected!r}"
                )
    log(f"created and self-tested {len(definitions)} Python SQL functions "
        f"({', '.join(n.split('.')[1] for n, _, _, _, _ in definitions)})")


if __name__ == "__main__":
    build()
