"""A Python function that runs *inside* IRIS, and the tariff it encodes.

The rest of this project pushes work into IRIS as SQL. That covers aggregation
well and stops at the point where the work is no longer an aggregate. Asking
"does this trip's metered fare match the tariff it should have been charged on?"
is a per-row decision over a rate-code table, not a GROUP BY -- SQL can express
it, as a nested CASE, and the expression is worse to read than the rule it
encodes.

``CREATE OR REPLACE FUNCTION ... LANGUAGE PYTHON { ... }`` compiles a Python body
into a SQL-callable function that executes in the IRIS process. So the third
option, alongside "aggregate in SQL" and "pull it into pandas", is *send the
Python to the data*::

    SELECT AVG(ABS(FareAmount - Taxi.ExpectedFare(RatecodeID, TripDistance, TripMinutes)))
    FROM Taxi.Trip

787k Python calls happen server-side and one row comes back.

Two things make this honest rather than a demo:

**One definition, two runtimes.** ``build_source()`` renders the function body
once. ``create_sql()`` wraps that text for IRIS; ``python_callable()`` compiles
the same text in this process for bench.py's pandas arm. Neither is a
transcription of the other, so when bench.py asserts the two arms agree it is
testing the plumbing rather than two hand-maintained copies of one formula.

**The tariff is measured, not asserted.** Hard-coding a published rate card would
put a number in this file that nobody here can verify and that silently rots when
the fare schedule changes. ``calibrate()`` fits the metered rate from the loaded
trips by least squares and reads the flat fares straight off the data as the modal
fare for their rate code. On the 2023 file that lands within about $0.38 of the
median trip's actual fare; the guessed published rate card was six times worse.
The fitted numbers are printed whenever the function is deployed, so the model is
visible rather than buried.
"""

from dataclasses import dataclass, field
from textwrap import dedent, indent
from typing import Any, Callable, Dict, Optional

import numpy as np
import pandas as pd

from . import db
from .config import IrisConfig, SCHEMA, TRIPS

FUNCTION_NAME = f"{SCHEMA}.ExpectedFare"

# Rate codes the model claims to cover: 1 is the standard meter, 2 is the JFK
# flat fare. 3 (Newark), 4 (out of city), 5 (negotiated) and the stray 6/99 are
# deliberately out of scope -- a negotiated fare has no tariff to deviate from,
# so "expected" would be meaningless rather than merely inaccurate.
METERED_RATE_CODE = 1
FLAT_FARE_RATE_CODES = (2,)
MODELLED_RATE_CODES = (METERED_RATE_CODE,) + FLAT_FARE_RATE_CODES

# A trip whose metered fare misses the tariff by more than this is worth looking
# at. Chosen well above the fitted model's own p95 error (~$1.87) so that the
# count reports trips the tariff cannot explain, not trips the fit cannot.
OFF_TARIFF_DOLLARS = 5.00

# The rows both benchmark arms operate on. Stated once, because the comparison is
# only meaningful if the two arms see exactly the same population, and because
# every column the function reads must be non-NULL for it to return a number.
POPULATION = (
    f"RatecodeID IN ({', '.join(str(c) for c in MODELLED_RATE_CODES)}) "
    "AND TripDistance IS NOT NULL "
    "AND TripMinutes IS NOT NULL "
    "AND FareAmount IS NOT NULL"
)


@dataclass(frozen=True)
class Tariff:
    """The fare model's constants, all measured from the loaded data."""

    base: float
    per_mile: float
    per_minute: float
    # rate code -> the flat fare charged on it, read off the data as the mode.
    flat_fares: Dict[int, float] = field(default_factory=dict)
    # Reported so the fit can be judged rather than trusted.
    rows_fitted: int = 0
    median_error: float = 0.0
    p95_error: float = 0.0

    def as_dict(self) -> Dict[str, Any]:
        return {
            "base": round(self.base, 4),
            "per_mile": round(self.per_mile, 4),
            "per_minute": round(self.per_minute, 4),
            "flat_fares": {str(k): v for k, v in sorted(self.flat_fares.items())},
            "rows_fitted": self.rows_fitted,
            "median_error": round(self.median_error, 2),
            "p95_error": round(self.p95_error, 2),
        }


# --------------------------------------------------------------------------
# The function body: rendered once, executed in two places
# --------------------------------------------------------------------------

# Written against plain scalars and the stdlib only, because it has to compile in
# both runtimes: the IRIS server's Python interpreter (which has pandas and numpy
# but is a different environment from the host venv) and this process. No imports,
# so there is nothing to install server-side before the function will run.
#
# The {} placeholders are filled by build_source(), so the template itself carries
# no literal braces that would need doubling. The rendered text does: FLAT_FARES
# comes out as a dict literal, which puts `{2: 70.0}` inside the CREATE FUNCTION
# body's own `{ ... }`. IRIS matches those braces correctly rather than ending the
# body at the first `}`, so a Python dict or set literal in a UDF is safe -- worth
# knowing, because the DB-API driver's *statement* parser is much less forgiving
# about braces (see the exec_direct note in db.py).
_BODY_TEMPLATE = """\
# Generated by taxi/udf.py from a tariff fitted to the loaded trips.
# Do not edit here -- edit _BODY_TEMPLATE and redeploy.
FLAT_FARES = {flat_fares!r}
BASE = {base!r}
PER_MILE = {per_mile!r}
PER_MINUTE = {per_minute!r}

if rateCode is None:
    return None
code = int(rateCode)

# A flat fare ignores the meter entirely: JFK is JFK whatever the traffic did.
if code in FLAT_FARES:
    return FLAT_FARES[code]

# Anything else is either the standard meter or outside the model's scope.
if code != {metered_rate_code!r} or miles is None or minutes is None:
    return None

fare = BASE + PER_MILE * float(miles) + PER_MINUTE * float(minutes)

# The meter cannot run backwards, so neither can the model. Rounding here rather
# than in the caller means both runtimes see byte-identical per-row values and
# the two arms of the benchmark can be asserted equal.
return round(max(fare, 0.0), 2)
"""

# The signature is shared for the same reason the body is: so the SQL function and
# the local callable cannot drift apart in argument order.
#
# camelCase, not the snake_case used everywhere else in this project, because
# CREATE FUNCTION's formalspec parser rejects an underscore in a parameter name.
# `rate_code INTEGER` fails with "Invalid method formalspec format
# Taxi.funcExpectedFare.ExpectedFare, expected <identifier> [OFFSET=10]" -- offset
# 10 being the underscore, and the message naming a generated class rather than
# the parameter. `rateCode INTEGER` compiles. Underscores are fine in the
# function's own name and in ordinary column names; it is only the parameter list.
_SIGNATURE = ("rateCode", "miles", "minutes")
_SQL_SIGNATURE = "rateCode INTEGER, miles DOUBLE, minutes DOUBLE"


def build_source(tariff: Tariff) -> str:
    """The function body, as the text both runtimes will execute."""
    return _BODY_TEMPLATE.format(
        flat_fares={int(k): float(v) for k, v in tariff.flat_fares.items()},
        base=round(tariff.base, 6),
        per_mile=round(tariff.per_mile, 6),
        per_minute=round(tariff.per_minute, 6),
        metered_rate_code=METERED_RATE_CODE,
    )


def create_sql(tariff: Tariff) -> str:
    """The DDL that compiles the body into a SQL-callable function inside IRIS.

    ``CREATE OR REPLACE`` so deploying is idempotent and redeploying after a
    recalibration is the same call.
    """
    return (
        f"CREATE OR REPLACE FUNCTION {FUNCTION_NAME}({_SQL_SIGNATURE})\n"
        f"RETURNS DOUBLE\n"
        f"LANGUAGE PYTHON\n"
        "{\n" + indent(build_source(tariff), "    ") + "}"
    )


def python_callable(tariff: Tariff) -> Callable[[Any, Any, Any], Optional[float]]:
    """The same body, compiled in *this* process.

    ``exec`` rather than a hand-written twin: the point is that bench.py's pandas
    arm runs the identical text the server is running, so a discrepancy between
    the two arms can only come from where the code ran, never from what it says.
    """
    source = (
        f"def expected_fare({', '.join(_SIGNATURE)}):\n"
        + indent(build_source(tariff), "    ")
    )
    namespace: Dict[str, Any] = {}
    exec(compile(source, "<taxi.udf generated>", "exec"), namespace)  # noqa: S102
    return namespace["expected_fare"]


# --------------------------------------------------------------------------
# Calibration
# --------------------------------------------------------------------------

# Fitted on clean, plainly-metered trips only. The bounds are not outlier
# cosmetics: a 200-mile "trip" or a 12-hour one is a data error, and including it
# would drag the per-mile rate the other 630k trips actually paid.
_CALIBRATION_SQL = f"""
    SELECT TripDistance, TripMinutes, FareAmount
    FROM {TRIPS}
    WHERE RatecodeID = {METERED_RATE_CODE}
      AND QualityIssueCount = 0
      AND TripDistance BETWEEN 0.1 AND 30
      AND TripMinutes BETWEEN 1 AND 120
      AND FareAmount > 0
"""

# The modal fare for a flat-fare rate code *is* the flat fare: on the 2023 file
# 2,088 of the 2,163 rate-code-2 trips were charged the same number.
_FLAT_FARE_SQL = f"""
    SELECT TOP 1 FareAmount
    FROM {TRIPS}
    WHERE RatecodeID = ? AND FareAmount > 0
    GROUP BY FareAmount
    ORDER BY COUNT(*) DESC
"""


def calibrate(cfg: Optional[IrisConfig] = None) -> Tariff:
    """Fit the tariff to the loaded trips.

    ``fare ~ base + per_mile * miles + per_minute * minutes``, by least squares.
    The meter really charges distance while moving and time while stopped, which
    is a max() rather than a sum; the linear form is what the data supports and
    is honest about being a fit rather than the rate card.
    """
    frame = db.query_df(_CALIBRATION_SQL, (), cfg)
    if frame.empty:
        raise RuntimeError(
            f"Cannot calibrate: no rows matched in {TRIPS}. Run the pipeline first."
        )

    miles = pd.to_numeric(frame["TripDistance"], errors="coerce").astype(float)
    minutes = pd.to_numeric(frame["TripMinutes"], errors="coerce").astype(float)
    fare = pd.to_numeric(frame["FareAmount"], errors="coerce").astype(float)
    usable = miles.notna() & minutes.notna() & fare.notna()
    miles, minutes, fare = miles[usable], minutes[usable], fare[usable]

    miles_np, minutes_np = miles.to_numpy(), minutes.to_numpy()
    design = np.column_stack([np.ones(len(miles_np)), miles_np, minutes_np])
    observed = fare.to_numpy()
    (base, per_mile, per_minute), *_ = np.linalg.lstsq(design, observed, rcond=None)

    # Written out rather than as `design @ coef`: the matrix product raises
    # spurious divide-by-zero and overflow RuntimeWarnings from the BLAS on
    # Apple silicon, and the elementwise form is both quieter and closer to the
    # formula the deployed function actually evaluates.
    predicted = base + per_mile * miles_np + per_minute * minutes_np
    residuals = np.abs(observed - predicted)

    flat_fares: Dict[int, float] = {}
    for code in FLAT_FARE_RATE_CODES:
        modal = db.scalar(_FLAT_FARE_SQL, (code,), cfg)
        if modal is not None:
            flat_fares[code] = round(float(modal), 2)

    return Tariff(
        base=float(base),
        per_mile=float(per_mile),
        per_minute=float(per_minute),
        flat_fares=flat_fares,
        rows_fitted=int(len(miles)),
        median_error=float(np.median(residuals)),
        p95_error=float(np.percentile(residuals, 95)),
    )


# --------------------------------------------------------------------------
# Deployment
# --------------------------------------------------------------------------

# Cached per process so that calling tariff() in a loop -- which bench.py's pandas
# reducer does once per timed repeat -- neither recalibrates nor recompiles.
_cached: Optional[Tariff] = None


def deploy(cfg: Optional[IrisConfig] = None, quiet: bool = False) -> Tariff:
    """Calibrate, compile into IRIS, and return the tariff that was deployed.

    Always recalibrates and redeploys rather than checking whether the function
    already exists. A function left over from a previous load would encode a
    tariff fitted to data that is no longer there, and the pandas arm of the
    benchmark would then disagree with it for a reason that looks like a bug in
    the comparison. Redeploying is one compile, so the safety is close to free.
    """
    global _cached
    tariff = calibrate(cfg)
    db.execute(create_sql(tariff), (), cfg)
    _cached = tariff
    if not quiet:
        print(
            f"  {FUNCTION_NAME} deployed: "
            f"${tariff.base:.2f} + ${tariff.per_mile:.2f}/mile + "
            f"${tariff.per_minute:.2f}/minute, flat {tariff.flat_fares}"
        )
        print(
            f"  fitted on {tariff.rows_fitted:,} trips; "
            f"median error ${tariff.median_error:.2f}, p95 ${tariff.p95_error:.2f}"
        )
    return tariff


def tariff(cfg: Optional[IrisConfig] = None) -> Tariff:
    """The deployed tariff, deploying it first if this process has not yet."""
    return _cached if _cached is not None else deploy(cfg, quiet=True)


# --------------------------------------------------------------------------
# What the function is actually for
# --------------------------------------------------------------------------

# The gap is computed per row inside IRIS by the Python function, then aggregated
# in the same statement, so one row per rate code crosses the driver.
#
# Two details that are not stylistic:
#
# The per-row gap is isolated in a derived table so the function is called *once*
# per trip. Repeating the call in the AVG and in the CASE -- the obvious way to
# write this -- doubles the number of Python invocations and quietly makes the
# UDF look twice as expensive as it is in bench.py.
#
# ROUND(..., 2) before the threshold test, because IRIS evaluates this in
# fixed-point decimal and bench.py's pandas arm evaluates it in float64. Both are
# right; they disagree on the handful of trips whose gap is exactly the threshold,
# where a float64 gap of 4.999999999999999 falls on the other side of `> 5.00`.
# Rounding first puts both arms on values that are exact in either representation.
#
# dedent()ed, unlike the other statements here, because this one is displayed as
# well as executed: web.py sends it to the dashboard so the page can show the
# query that called the function. Without it the first line comes out flush and
# every later line keeps this file's indentation.
GAP_BY_RATE_SQL = dedent(f"""
    SELECT rate_code,
           COUNT(*)              AS trips,
           ROUND(AVG(gap), 2)    AS mean_gap,
           SUM(CASE WHEN gap > {OFF_TARIFF_DOLLARS} THEN 1 ELSE 0 END) AS off_tariff
    FROM (
        SELECT RatecodeID AS rate_code,
               ROUND(ABS(FareAmount - {FUNCTION_NAME}(
                   RatecodeID, TripDistance, TripMinutes)), 2) AS gap
        FROM {TRIPS}
        WHERE {POPULATION}
    ) AS per_trip
    GROUP BY rate_code
    ORDER BY rate_code
""").strip()


def gap_by_rate_code(cfg: Optional[IrisConfig] = None) -> pd.DataFrame:
    """Per rate code: how far the metered fare sits from the fitted tariff."""
    deploy(cfg, quiet=True)
    frame = db.query_df(GAP_BY_RATE_SQL, (), cfg)
    if frame.empty:
        return frame
    trips = pd.to_numeric(frame["trips"], errors="coerce")
    off = pd.to_numeric(frame["off_tariff"], errors="coerce")
    frame["pct_off_tariff"] = (100.0 * off / trips).round(2)
    return frame
