"""Stage 3 -- cast TripRaw into the typed Trip table.

Only *schema* validation happens here: can this text become the declared type? A
failure means the value is unstorable, so it becomes NULL and the reason is
recorded in TripReject. No business opinion is applied -- -8.50 is a valid NUMERIC
and passes through, to be judged by stage 4.

Three observations can only be made here, because they depend on the original
text, which stops existing once it is typed:

  distance_reformatted    '1,571.97' arrived formatted for display. We strip the
                          separator to store it, but record that we did --
                          normalising silently would destroy the evidence linking
                          the formatting to the absurd magnitude.
  meter_metadata_missing  The five-field block is absent as a unit. Recorded once
                          per row, since "this came from a feed without meter
                          data" is a testable hypothesis; five NULLs say nothing.
  cast_failed             Something could not be coerced at all.

Nothing is imputed: filling in a median would manufacture data and bury the most
interesting structural fact in the file.
"""

import sys
from datetime import datetime

import iris
from db import banner, exec_dml, exec_sql, log, prepare, scalar, sql_params
from rules import INGEST_RULES

# The trip CSV's own format: month/day/year on a 12-hour clock with AM/PM.
PICKUP_FORMAT = "%m/%d/%Y %I:%M:%S %p"

CHUNK = 50_000

RAW_SELECT = """
    SELECT ID, VendorID, lpep_pickup_datetime, lpep_dropoff_datetime,
           store_and_fwd_flag, RatecodeID, PULocationID, DOLocationID,
           passenger_count, trip_distance, fare_amount, extra, mta_tax,
           tip_amount, tolls_amount, ehail_fee, improvement_surcharge,
           total_amount, payment_type, trip_type, congestion_surcharge
    FROM Taxi.TripRaw
    WHERE ID > ? AND ID <= ?
"""

TRIP_INSERT = """
    INSERT INTO Taxi.Trip (
        trip_id, raw_id, vendor_id, pickup_ts, dropoff_ts, store_and_fwd_flag,
        ratecode_id, pu_location_id, do_location_id, passenger_count,
        trip_distance, fare_amount, extra, mta_tax, tip_amount, tolls_amount,
        ehail_fee, improvement_surcharge, total_amount, payment_type, trip_type,
        congestion_surcharge, duration_min, implied_mph, pickup_hour,
        pickup_month, pickup_dow, flag_count
    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
"""

# The five fields that go missing as a unit.
METER_FIELDS = (4, 5, 8, 18, 20)  # sfw_flag, ratecode, passenger_count, payment, congestion


class RowCast:
    """Casts one raw row, accumulating rejects and ingest observations."""

    def __init__(self, raw_id):
        self.raw_id = raw_id
        self.rejects = []
        self.reformatted = False
        self.failed = False

    def _blank(self, value):
        return value is None or not str(value).strip()

    def number(self, value, field):
        """NUMERIC, tolerating display formatting like '1,571.97'."""
        if self._blank(value):
            return None
        text = str(value).strip()
        cleaned = text.replace(",", "")
        if cleaned != text:
            self.reformatted = True
        try:
            return float(cleaned)
        except ValueError:
            self.reject(field, text, "not a number")
            return None

    def integer(self, value, field):
        if self._blank(value):
            return None
        text = str(value).strip()
        try:
            return int(float(text.replace(",", "")))
        except ValueError:
            self.reject(field, text, "not an integer")
            return None

    def flag(self, value, field):
        if self._blank(value):
            return None
        text = str(value).strip()
        if text not in ("Y", "N"):
            self.reject(field, text, "expected Y or N")
            return None
        return text

    def timestamp(self, value, field):
        """Parse MM/DD/YYYY hh:mm:ss AM/PM into an ODBC-format string.

        Done in Python rather than SQL TO_TIMESTAMP: the AM/PM half is the one
        silent-corruption risk in the file -- get it wrong and every timestamp is
        still valid and storable, with half off by twelve hours, wrecking the
        hour-of-day analysis. Python's %I/%p is explicit and testable, verified by
        self_test() below.
        """
        if self._blank(value):
            return None, None
        text = str(value).strip()
        try:
            parsed = datetime.strptime(text, PICKUP_FORMAT)
        except ValueError:
            self.reject(field, text, f"does not match {PICKUP_FORMAT}")
            return None, None
        return parsed.strftime("%Y-%m-%d %H:%M:%S"), parsed

    def reject(self, field, value, reason):
        self.failed = True
        self.rejects.append((self.raw_id, field, value[:64], reason))


def cast_row(raw):
    """Return (insert_params, ingest_rule_ids, rejects) for one raw row."""
    raw_id = int(raw[0])
    c = RowCast(raw_id)

    pickup_text, pickup = c.timestamp(raw[2], "lpep_pickup_datetime")
    dropoff_text, dropoff = c.timestamp(raw[3], "lpep_dropoff_datetime")

    vendor_id = c.integer(raw[1], "VendorID")
    sfw = c.flag(raw[4], "store_and_fwd_flag")
    ratecode = c.integer(raw[5], "RatecodeID")
    pu_loc = c.integer(raw[6], "PULocationID")
    do_loc = c.integer(raw[7], "DOLocationID")
    passengers = c.integer(raw[8], "passenger_count")
    distance = c.number(raw[9], "trip_distance")
    fare = c.number(raw[10], "fare_amount")
    extra = c.number(raw[11], "extra")
    mta = c.number(raw[12], "mta_tax")
    tip = c.number(raw[13], "tip_amount")
    tolls = c.number(raw[14], "tolls_amount")
    ehail = c.number(raw[15], "ehail_fee")
    improvement = c.number(raw[16], "improvement_surcharge")
    total = c.number(raw[17], "total_amount")
    payment = c.integer(raw[18], "payment_type")
    trip_type = c.integer(raw[19], "trip_type")
    congestion = c.number(raw[20], "congestion_surcharge")

    # ---- derived ---------------------------------------------------------
    # Stored rather than computed on read, so the compound rules in stage 4 stay
    # plain SQL predicates.
    duration_min = None
    implied_mph = None
    if pickup is not None and dropoff is not None:
        duration_min = round((dropoff - pickup).total_seconds() / 60.0, 2)
        if duration_min > 0 and distance is not None:
            implied_mph = round(distance / (duration_min / 60.0), 2)

    pickup_hour = pickup.hour if pickup else None
    pickup_month = pickup.month if pickup else None
    pickup_dow = pickup.isoweekday() if pickup else None

    # ---- ingest-phase observations ---------------------------------------
    ingest = set()
    if c.failed:
        ingest.add(INGEST_RULES["cast_failed"])
    if c.reformatted:
        ingest.add(INGEST_RULES["distance_reformatted"])
    if all(raw[i] is None or not str(raw[i]).strip() for i in METER_FIELDS):
        ingest.add(INGEST_RULES["meter_metadata_missing"])

    params = (
        raw_id, raw_id, vendor_id, pickup_text, dropoff_text, sfw, ratecode,
        pu_loc, do_loc, passengers, distance, fare, extra, mta, tip, tolls,
        ehail, improvement, total, payment, trip_type, congestion,
        duration_min, implied_mph, pickup_hour, pickup_month, pickup_dow,
        len(ingest),
    )
    return params, ingest, c.rejects


def self_test():
    """Guard the one failure mode that is silent rather than loud."""
    midnight = datetime.strptime("01/01/2023 12:26:10 AM", PICKUP_FORMAT)
    noon = datetime.strptime("01/01/2023 12:26:10 PM", PICKUP_FORMAT)
    evening = datetime.strptime("01/17/2023 06:40:38 PM", PICKUP_FORMAT)
    assert midnight.hour == 0, f"12:26 AM parsed to hour {midnight.hour}, expected 0"
    assert noon.hour == 12, f"12:26 PM parsed to hour {noon.hour}, expected 12"
    assert evening.hour == 18, f"06:40 PM parsed to hour {evening.hour}, expected 18"

    probe = RowCast(0)
    assert probe.number("1,571.97", "trip_distance") == 1571.97
    assert probe.reformatted is True
    clean = RowCast(0)
    assert clean.number("2.58", "trip_distance") == 2.58
    assert clean.reformatted is False
    assert clean.number(None, "x") is None
    log("  self-test passed (AM/PM parsing, separator handling)")


def run(limit=None):
    banner("STAGE 3 -- cast to typed")
    self_test()

    exec_dml("DELETE FROM Taxi.TripFlag")
    exec_dml("DELETE FROM Taxi.TripReject")
    exec_dml("DELETE FROM Taxi.Trip")

    max_id = scalar("SELECT MAX(ID) FROM Taxi.TripRaw") or 0
    if limit:
        max_id = min(max_id, limit)
    log(f"  casting raw IDs 1..{max_id:,} in chunks of {CHUNK:,}")

    trip_stmt = prepare(TRIP_INSERT)
    flag_stmt = prepare("INSERT INTO Taxi.TripFlag (trip_id, rule_id) VALUES (?,?)")
    reject_stmt = prepare(
        "INSERT INTO Taxi.TripReject (raw_id, field_name, raw_value, reason) "
        "VALUES (?,?,?,?)"
    )

    inserted = flags_written = rejects_written = 0
    lo = 0
    while lo < max_id:
        hi = min(lo + CHUNK, max_id)
        rows = exec_sql(RAW_SELECT, lo, hi)

        # One transaction per chunk: without this, every INSERT commits on its own
        # and the journal write dominates the runtime.
        iris.tstart()
        try:
            for raw in rows:
                params, ingest, rejects = cast_row(raw)
                trip_stmt.execute(*sql_params(params))
                inserted += 1
                for rule_id in ingest:
                    flag_stmt.execute(params[0], rule_id)
                    flags_written += 1
                for reject in rejects:
                    reject_stmt.execute(*sql_params(reject))
                    rejects_written += 1
            iris.tcommit()
        except Exception:
            iris.trollback()
            raise

        log(f"    {hi:>9,} / {max_id:,}  trips={inserted:,} "
            f"ingest_flags={flags_written:,} rejects={rejects_written:,}")
        lo = hi

    log(f"  Taxi.Trip:       {inserted:,} rows")
    log(f"  ingest flags:    {flags_written:,}")
    log(f"  Taxi.TripReject: {rejects_written:,}")
    return inserted


if __name__ == "__main__":
    cap = int(sys.argv[1]) if len(sys.argv) > 1 else None
    run(limit=cap)
