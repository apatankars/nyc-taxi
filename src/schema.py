"""The one declarative description of a trip row.

Imported by both src/validate_trips.py and src/load_trips.py on purpose. If the
validator and the loader each carried their own idea of what a valid row is they
would drift, and the failure mode is the worst kind: validation reports a clean
file, then the load dies halfway through and you cannot tell which of the two is
wrong.

Scope: this file describes **structure**, not plausibility. A row with
trip_distance = 0 and fare_amount = -500 is structurally perfect — both are
numbers in range — and it loads. Deciding that such a row is *questionable* is
the job of the quality view, which runs against the loaded table. Keeping the two
apart is what lets a threshold change without a reload.

Every fact below came from running src/profile_trips.py over the supplied file,
not from assumption. Nullability especially: 55,613 rows (7.07%) leave the same
five columns empty together, so those five must be nullable or 7% of the year
becomes unloadable.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Optional, Set

TS_FORMAT = "%m/%d/%Y %I:%M:%S %p"

# The file is billed as calendar 2023, and 7 pickups fall outside it (2008, 2009,
# 2022). Those load: a 2008 timestamp is structurally a valid timestamp, and
# "pickup outside the advertised year" is a finding for the quality layer, not a
# reason to refuse the row. These bounds exist only to catch real garbage.
MIN_YEAR, MAX_YEAR = 2000, 2030

# Taxi.Zone holds ids 1..265. A LocationID outside that cannot be joined to a
# borough at all, which makes the row structurally unusable for enrichment.
MIN_LOCATION_ID, MAX_LOCATION_ID = 1, 265


class FieldError(ValueError):
    """One field that cannot be typed, or falls outside its declared domain."""


def parse_timestamp(raw):
    ts = datetime.strptime(raw, TS_FORMAT)
    if not MIN_YEAR <= ts.year <= MAX_YEAR:
        raise FieldError(f"year {ts.year} outside {MIN_YEAR}..{MAX_YEAR}")
    return ts


def parse_number(raw, cast=float):
    """Type a numeric field, stripping thousands separators first.

    The comma strip is not cosmetic. 578 values in the supplied file are written
    '1,069.67' / '4,003', and because every CSV field is quoted the comma sits
    inside the field. Parse without stripping it and those rows fail — and they
    are the largest values in their columns. Refusing them would drop the
    trip_distance maximum from 278,990.28 to 976.84, quietly discarding the most
    extreme records in a data set whose stated purpose includes finding extreme
    records.

    (Those 554 distances are separately unrecoverable — no scaling of them
    correlates with fare, r=0.02 against r=0.63 for normal rows. They still load;
    the quality view flags them. Parsing and trusting are different questions.)
    """
    try:
        return cast(raw.replace(",", ""))
    except ValueError as exc:
        raise FieldError(str(exc)) from exc


def parse_int(raw):
    return parse_number(raw, int)


def parse_flag(raw):
    if raw not in ("Y", "N"):
        raise FieldError(f"expected Y or N, got {raw!r}")
    return raw


@dataclass(frozen=True)
class Column:
    csv_name: str
    sql_name: str
    sql_type: str
    parse: Callable[[str], object]
    # False for the five columns the 55,613-row block leaves empty together.
    required: bool = True
    # Closed domain for code columns, checked after parsing. None = open range.
    domain: Optional[Set[int]] = None

    def coerce(self, raw):
        """str -> typed value or None. Raises FieldError with a usable reason."""
        if raw is None or raw == "":
            if self.required:
                raise FieldError("required but empty")
            return None
        value = self.parse(raw)
        if self.domain is not None and value not in self.domain:
            raise FieldError(f"{value} not in allowed {sorted(self.domain)}")
        return value


def _location(csv_name, sql_name):
    def parse(raw):
        value = parse_int(raw)
        if not MIN_LOCATION_ID <= value <= MAX_LOCATION_ID:
            raise FieldError(
                f"{value} outside {MIN_LOCATION_ID}..{MAX_LOCATION_ID}, "
                "cannot join to Taxi.Zone")
        return value
    return Column(csv_name, sql_name, "INTEGER", parse)


def _money(csv_name, sql_name, required=True):
    # NUMERIC, not DOUBLE: these get SUM()'d over 787k rows and compared between
    # zones, and binary float error is visible at that scale.
    return Column(csv_name, sql_name, "NUMERIC(12,2)", parse_number, required)


# ehail_fee is absent by design: empty in all 787,060 rows, so a column for it
# would carry no information. Recorded here rather than silently omitted, because
# "we checked and it is entirely empty" is a different claim from "we forgot it".
COLUMNS = (
    Column("VendorID", "vendor_id", "SMALLINT", parse_int, domain={1, 2}),
    Column("lpep_pickup_datetime", "pickup_ts", "TIMESTAMP", parse_timestamp),
    Column("lpep_dropoff_datetime", "dropoff_ts", "TIMESTAMP", parse_timestamp),
    Column("store_and_fwd_flag", "store_and_fwd_flag", "VARCHAR(1)",
           parse_flag, required=False),
    # 99 is the TLC dictionary's own "unknown" rate code and appears 45 times. It
    # is a legitimate member of the domain, not a defect.
    Column("RatecodeID", "ratecode_id", "SMALLINT", parse_int,
           required=False, domain={1, 2, 3, 4, 5, 6, 99}),
    _location("PULocationID", "pu_location_id"),
    _location("DOLocationID", "do_location_id"),
    Column("passenger_count", "passenger_count", "SMALLINT", parse_int,
           required=False),
    Column("trip_distance", "trip_distance", "DOUBLE", parse_number),
    _money("fare_amount", "fare_amount"),
    _money("extra", "extra"),
    _money("mta_tax", "mta_tax"),
    _money("tip_amount", "tip_amount"),
    _money("tolls_amount", "tolls_amount"),
    _money("improvement_surcharge", "improvement_surcharge"),
    _money("total_amount", "total_amount"),
    Column("payment_type", "payment_type", "SMALLINT", parse_int,
           required=False, domain={1, 2, 3, 4, 5, 6}),
    Column("trip_type", "trip_type", "SMALLINT", parse_int,
           required=False, domain={1, 2}),
    _money("congestion_surcharge", "congestion_surcharge", required=False),
)

# Computed once at load instead of recomputed in every query. Signed on purpose:
# 1,033 rows drop off before they are picked up, and clamping that to zero would
# erase the evidence.
DURATION = Column("", "duration_min", "DOUBLE", parse_number, required=False)

ALL_COLUMNS = COLUMNS + (DURATION,)


def coerce_row(row):
    """Validate and type one csv.DictReader row.

    Returns (values, errors). `values` is a tuple in ALL_COLUMNS order; `errors`
    is a list of "csv_column: reason" strings, empty when the row is loadable.

    Every column is attempted even after the first failure, so a bad row reports
    all of its problems rather than only the earliest one. That is what makes the
    validation report worth reading instead of requiring N passes to converge.
    """
    values, errors = [], []
    for column in COLUMNS:
        try:
            values.append(column.coerce(row.get(column.csv_name)))
        except FieldError as exc:
            values.append(None)
            errors.append(f"{column.csv_name}: {exc}")

    pickup, dropoff = values[1], values[2]
    values.append(
        (dropoff - pickup).total_seconds() / 60.0
        if pickup is not None and dropoff is not None else None
    )
    return tuple(values), errors


def create_table_ddl(schema="Taxi", table="Trip"):
    """Build the CREATE TABLE from the column list, so the two cannot disagree."""
    body = ",\n".join(
        f"    {c.sql_name:<22} {c.sql_type}" for c in ALL_COLUMNS
    )
    return f"CREATE TABLE {schema}.{table} (\n{body}\n)"


def insert_sql(schema="Taxi", table="Trip"):
    names = ", ".join(c.sql_name for c in ALL_COLUMNS)
    marks = ",".join("?" * len(ALL_COLUMNS))
    return f"INSERT INTO {schema}.{table} ({names}) VALUES ({marks})"
