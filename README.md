# NYC Green Taxi 2023, on InterSystems IRIS

Project B of the new-hire discovery project, built with Python and IRIS Community
Edition. It loads the 2023 green-taxi trip file and the taxi-zone lookup into
IRIS, enriches every trip with borough and zone names, flags questionable trips
against fourteen rules, answers a set of analytical questions, and presents the
whole thing through a small dashboard.

**The dashboard is not the point.** The point of this exercise is to find out
where a new developer trips over IRIS, and to write it down. So the largest
section of this README is [the friction log](#the-friction-log) — twenty
things that cost time, what the error actually said, why it happened, and what
would have to change for the next cohort not to lose the same hours.

Short version of what hurt: **three of the four hardest problems produced error
messages that pointed nowhere near their cause**, and one produced no error at
all.

---

## Contents

- [Quick start](#quick-start)
- [What it does](#what-it-does)
- [Architecture: who does what](#architecture-who-does-what)
  - [The relevance model: which rows a figure may ignore](#the-relevance-model-which-rows-a-figure-may-ignore)
- [The pipeline, stage by stage](#the-pipeline-stage-by-stage)
- [The friction log](#the-friction-log)
- [What worked well](#what-worked-well)
- [Findings in the data](#findings-in-the-data)
- [The stretch goal: IRIS SQL vs pandas vs Embedded Python](#the-stretch-goal-iris-sql-vs-pandas-vs-embedded-python)
- [Recommendations for the next cohort](#recommendations-for-the-next-cohort)
- [Known limitations](#known-limitations)
- [File map](#file-map)

---

## Quick start

Requires Docker and Python 3.9+.

```bash
# 1. Data. Both CSVs go in ./data (git-ignored).
ls data/
#   2023_Green_Taxi_Trip_Data.csv
#   taxi_zone_lookup(in).csv

# 2. IRIS. The image is built, not just pulled -- see friction #2 for why.
docker compose up -d --build

# 3. Python.
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env

# 4. Check the connection before doing anything expensive.
PYTHONPATH=src .venv/bin/python -m taxi.cli info

# 5. Build everything: ~90 seconds from empty database to indexed tables.
PYTHONPATH=src .venv/bin/python -m taxi.cli pipeline

# 6. The dashboard.
PYTHONPATH=src .venv/bin/python -m taxi.web      # http://127.0.0.1:8000
```

Everything is also reachable from the CLI, which is what I actually used while
building:

```bash
python -m taxi.cli info       # connection check and row counts
python -m taxi.cli pipeline   # full rebuild
python -m taxi.cli profile    # what is wrong with the raw file
python -m taxi.cli quality    # re-apply the rules; --sample RULE, --derive-thresholds
python -m taxi.cli analyze    # every analytical workflow, as text
python -m taxi.cli bench      # IRIS SQL vs pandas vs Embedded Python
```

### Ports

`docker-compose.yml` maps **1973→1972** and **52774→52773**, not the defaults.
I already had another IRIS container holding 1972, and the failure mode when two
containers want the same port is a connection that appears to succeed against the
wrong instance. Offsetting deliberately was cheaper than debugging that twice.

---

## What it does

Against the project guide's requirements:

| Requirement | Where |
|---|---|
| Load the supplied CSVs into IRIS | `load.py`, server-side `LOAD DATA` |
| Enrich pickup/dropoff with borough and zone | `transform.py`, two `LEFT JOIN`s onto `Taxi.Zone` |
| Trip-quality workflow identifying questionable records | `quality.py`, 14 rules |
| ...and *using* those flags without biasing the answers | `quality.py`'s [relevance model](#the-relevance-model-which-rows-a-figure-may-ignore): each rule declares which measures it invalidates |
| Two or more additional user workflows | `analytics.py`: busiest zones, activity by hour/day/month, fare & tip comparison, OD pairs |
| Present through an interface | `web.py` + `static/`, a single-page dashboard |
| Show Python + IRIS beats raw files | `bench.py`, and the whole cleaning-impact story |
| **Stretch:** filter/aggregate in IRIS vs pull into Python | `bench.py`, three arms (SQL / host pandas / Embedded Python), measured with correctness assertions |

---

## Architecture: who does what

The brief was to lean on Python but make real use of IRIS. The line I drew, and
held everywhere:

> **IRIS does all the work over rows. Python decides what work to do, and
> presents the answer.**

Concretely:

- **No function in this project ever pulls a row-per-trip result set** for
  analysis. Every analytical query groups, filters and orders server-side and
  returns tens of rows. `bench.py` exists precisely to show what breaking that
  rule costs.
- **Bulk load is server-side.** IRIS reads the 100 MB file off a bind mount
  itself; Python issues one statement and waits.
- **The cast, the derivations and the enrichment join are one `INSERT ... SELECT`.**
  787,060 rows are typed, have seven columns derived, and are joined twice
  against the zone lookup, without a single row crossing the driver.
- **The quality rules are Python objects compiled into SQL.** The rule
  *definitions* — predicate, threshold, severity, description — live in a Python
  list, because that is what you want to read and review. Evaluating them is two
  `UPDATE` statements. Nothing is evaluated row-by-row in Python.
- **pandas only ever sees already-aggregated results.** It labels day numbers,
  maps payment-type codes, pivots 168 rows into a heatmap. Presentation work.

The one place this inverts is `bench.py`, deliberately.

### Why the rules live in Python

`quality.py` is the clearest expression of the split, so it is worth a moment:

```python
Rule(
    name="implausible_speed",
    column="QImplausibleSpeed",
    severity="error",
    description="Average speed above {max_mph} mph",
    predicate="AvgMph > {max_mph}",
)
```

Adding a rule is a one-line change. `schema.py` reads the same registry to
generate one `BIT DEFAULT 0` column per rule, and `apply_rules()` reads it to
generate the SQL. So the flag column, the DDL, the predicate, the human-readable
description and the dashboard row all come from one declaration — there is no
second place to forget to update.

Thresholds are named constants substituted into the predicates, so
`DEFAULT_THRESHOLDS` is the single place cutoffs are stated rather than having
numbers buried in fourteen SQL strings.

Each rule also declares **which measures it invalidates**, which is what makes the
analytics selective rather than blunt — see [the relevance
model](#the-relevance-model-which-rows-a-figure-may-ignore) below.

Flagging is a *separate, re-runnable stage*, not folded into the load. Tuning a
threshold and re-flagging takes 12 seconds and does not re-read the CSV.

### The relevance model: which rows a figure may ignore

The obvious way to wire the quality workflow into the analytics is
`WHERE QualityIssueCount = 0` — drop every flagged row from every figure. That is
what this project did first, and it is wrong in a way worth explaining, because it
looks careful.

A row flagged for a missing passenger count tells you nothing about its own fare.
Dropping it from an average fare discards good evidence for an unrelated reason.
Worse, it does not merely lose precision: **the rows a blunt filter drops are not a
random sample**, so it introduces bias. Measured on this file:

| Rows dropped because… | Rows | Their average fare |
|---|---|---|
| `total_mismatch` | 115,009 | **$19.43** |
| `missing_passenger_count` | 61,703 | **$24.44** |
| `non_positive_distance` | 38,621 | **$23.95** |
| `unknown_zone` | 9,504 | **$44.09** |
| *(rows that pass every rule)* | 598,358 | **$17.35** |

Every group the blunt filter throws out has a higher average fare than the group
it keeps. So `WHERE QualityIssueCount = 0` reports an average fare of **$17.35**
where the rows that actually have a usable fare say **$18.37** — a 5.9% error, in
a figure whose whole purpose is accuracy.

So instead, each rule names the measures it casts doubt on, and each query names
what it depends on. `quality.MEASURES` is a small closed vocabulary — the only
facts about a trip anything here reads:

```
trip_count  location  time  distance  duration  speed
fare  tip  total  fee_breakdown  passengers
```

and each rule maps onto it:

| Rule | Invalidates |
|---|---|
| `negative_amount` | `trip_count`, `fare`, `tip`, `total`, `fee_breakdown` |
| `total_mismatch` | `fee_breakdown` |
| `missing_passenger_count` | `passengers` |
| `unknown_zone` | `location` |
| `outside_2023` | `time` |
| `non_positive_duration` | `duration`, `speed`, `time` |
| `non_positive_distance` | `distance`, `speed` |
| `excessive_distance` | `distance`, `speed` |
| `implausible_speed` | `distance`, `duration`, `speed` |
| `implausibly_short` | `distance`, `duration`, `speed` |
| `excessive_duration` | `duration`, `speed` |
| `stalled_trip` | `duration`, `speed` |
| `excessive_fare` | `fare`, `total` |
| `disproportionate_tip` | `tip`, `total` |

Two mechanisms consume it, and the split between them matters:

**A row filter**, on the measures that define the *population* — that the trip
happened, plus whatever the query groups by. `trip_count` is deliberately
invalidated by exactly one rule, `negative_amount`, because a refund is an
accounting entry against a trip already counted elsewhere. Everything else leaves
the row countable.

**A per-column guard** (`quality.guard`) on each aggregate, so one query can report
a trip count over the widest defensible population while each average beside it is
drawn only from rows that can support *that number*:

```sql
SELECT COUNT(*) AS trips,
       ROUND(AVG(CASE WHEN QNonPositiveDistance = 0 AND QImplausiblyShort = 0
                       AND QExcessiveDistance = 0 AND QImplausibleSpeed = 0
                      THEN TripDistance END), 2) AS avg_miles,
       ROUND(AVG(CASE WHEN QNegativeAmount = 0 AND QExcessiveFare = 0
                      THEN FareAmount END), 2)   AS avg_fare
FROM Taxi.Trip
WHERE QNegativeAmount = 0 AND QUnknownZone = 0
```

`AVG` and `SUM` skip NULLs, so each guarded aggregate gets its own denominator
without a second query. The denominators therefore differ *by design*, which is
only honest if you show them — `quality.measure_coverage()` does, in one pass, and
it is on the Trip quality tab:

| Measure | Usable rows | % of file |
|---|---|---|
| `time` | 786,020 | 99.87% |
| `trip_count` | 784,808 | 99.71% |
| `fare` | 784,778 | 99.71% |
| `tip` | 784,196 | 99.64% |
| `total` | 784,166 | 99.63% |
| `location` | 777,462 | 98.78% |
| `duration` | 776,404 | 98.65% |
| `distance` | 741,533 | 94.22% |
| `speed` | 737,852 | 93.75% |
| `passengers` | 725,304 | 92.15% |
| `fee_breakdown` | 669,798 | 85.10% |
| **the blunt filter** | **598,358** | **76.02%** |

The heatmap is the cleanest illustration. It reports nothing but counts by weekday
and hour, so only three of the fourteen rules have any say in it — the ones that
make a row uncountable or untimeable. `total_mismatch` and
`missing_passenger_count`, 176,000 rows between them, have no opinion about when
someone hailed a cab.

Two places take the coarser tool deliberately, and both are commented as such:

- **`fare_comparison_across_zones`** computes `SUM(fare)/SUM(distance)`. A ratio
  must draw both inputs from the same rows or the numerator and denominator
  describe different populations, so fare and distance are filtered at row level
  rather than guarded independently.
- **`headline_numbers.clean_trips`** keeps the strict definition, because that
  particular headline genuinely is "how much of this file is unblemished".

### Layers

```
data/*.csv
    │  server-side LOAD DATA (IRIS reads the file itself)
    ▼
Taxi.TripRaw      all VARCHAR, 20 columns, zero constraints — nothing can fail
Taxi.ZoneRaw      the landing tables
    │  one INSERT ... SELECT: cast, derive, enrich
    ▼
Taxi.Trip         typed + 7 derived columns + 4 enrichment columns + 14 flags
Taxi.Zone         typed lookup, INTEGER primary key
    │  two UPDATE statements (flags, then issue count)
    ▼
Taxi.Trip         flagged and countable
    │  GROUP BY, in IRIS, always
    ▼
analytics.py  →  web.py (JSON)  →  static/app.js (SVG)
```

The all-VARCHAR landing table is the load answer to a file you have not profiled
yet: **nothing can fail to land**, so the load either works completely or not at
all. Values that are *unreadable* are then caught by the cast, and values that
are *wrong* by the rules. Those are different problems and they get different
stages. Every row is accounted for — `cast_and_enrich` raises if
`loaded + rejected != raw`.

---

## The pipeline, stage by stage

Measured on this laptop, IRIS Community in Docker, full 787,060-row file:

| Stage | Time | What happens |
|---|---|---|
| 1. schema | 0.50s | DDL generated from Python, including 14 flag columns |
| 2a. load zones | 0.17s | 265 rows, landed then typed |
| 2b. load trips | **0.72s** | 787,060 rows, server-side `LOAD DATA` |
| 3. cast + enrich | **71.11s** | typing, 7 derived columns, 2 enrichment joins |
| 4a. flag rows | 7.40s | 14 predicates, one pass |
| 4b. count issues | 4.65s | sum the flag columns |
| 5. indexes | 4.80s | 5 bitmap + 3 standard, built *after* the load |
| 6. tune tables | 0.58s | optimiser statistics |
| **Total** | **~90s** | empty database → indexed, flagged, queryable |

Two things worth noticing.

**The load is not the slow part.** 787,060 rows land in 0.72 seconds. The
expensive stage is the cast — see [limitation #1](#known-limitations) for why,
and it is my fault rather than IRIS's.

**Indexes are built after the load, not before.** Bitmap indexes on
`PickupHour`, `PickupMonth`, `PUBorough`, `PaymentType` and `QualityIssueCount`
are ideal for this workload (low cardinality, always in a `WHERE` or `GROUP BY`)
but maintaining them during a bulk insert is wasted work.

---

## The friction log

This is the substance of the exercise. Twenty items, grouped, each with the real
error text where there was one.

Ranked by how much time they cost, the worst four were **#4** (the brace
conflict), **#11** (collation), **#13** (thousands separators) and **#6** (the
silently-ingested header). Note what they have in common: three produced
misleading errors, and one produced none.

### Getting connected

#### 1. The package name is not the module name

```bash
pip install intersystems-irispython   # installs this
```
```python
import iris                            # imports as this
```

There is nothing in the install output that tells you this. `import
intersystems_irispython` fails, and searching for the failure finds nothing
useful because everyone who knows just writes `import iris`.

**Cost:** ten minutes and a lucky guess.
**Fix for the guide:** one line in the setup instructions.

#### 2. The stock image ships an expired password

The first DB-API connect against a fresh `intersystemsdc/iris-community` fails on
authentication. The cause is that `_SYSTEM`'s password is *pre-expired* — IRIS
wants you to change it on first login — but the driver reports it as an
authentication failure, so you spend your time double-checking the password you
just typed correctly.

The Native API additionally requires the `%Service_CallIn` service to be enabled,
which is off by default and produces a *different* failure.

Both are fixed at image build time rather than by hand, so the environment is
reproducible:

```objectscript
zn "%SYS"
do ##class(Security.Users).UnExpireUserPasswords("*")
set props("Enabled") = 1
do ##class(Security.Services).Modify("%Service_CallIn", .props)
halt
```

That is `iris.script`, run by the `Dockerfile` during build. It is the reason
step 2 of the quick start is `up -d --build` and not just `up -d`.

**Cost:** about 45 minutes, most of it doubting the credentials.
**Fix for the guide:** ship exactly this compose file. Nobody should meet
`Security.Users.UnExpireUserPasswords` on day one.

#### 3. Port collisions fail confusingly

Two IRIS containers both wanting 1972 do not produce a clean error; you get a
connection to something, and the something may not be what you think. Offsetting
to 1973/52774 in `docker-compose.yml` and putting the port in `.env` was the fix.

**Fix for the guide:** mention it, and suggest non-default ports up front.

### The driver

#### 4. `LOAD DATA ... USING {...}` cannot go through the DB-API driver

The single biggest time sink, and the most interesting bug.

`LOAD DATA` takes its options as a JSON object:

```sql
LOAD DATA FROM FILE '/data/trips.csv' INTO Taxi.TripRaw (...)
  VALUES (...) USING {"from":{"file":{"header":1}}}
```

Through `iris.connect()` (DB-API), that fails at prepare time with:

```
<PARAMETER ERROR>; Parameter Name error, First value cannot be a digit: 1
```

Which is not a message about JSON, or about `LOAD DATA`, or about braces. It is
a message about a *parameter*, and there is no parameter in the statement.

What is actually happening: the DB-API layer treats `{` as the start of an
**ODBC escape sequence** (the `{ts '...'}`, `{fn ...}`, `{call ...}` family), so
it tries to parse `{"from":...}` as one, chokes on the `1` in `"header":1`, and
reports it as a parameter error.

Two things confirmed the diagnosis rather than assuming it:

1. The **identical statement** run in `iris sql` inside the container works
   perfectly — 1000 rows, header correctly skipped. So the statement is valid and
   the server is fine; the client mangled it.
2. Passing the JSON as a bound parameter (`USING ?`) fails *differently*:
   ```
   <THROW>%FromJSON+30^%Library.DynamicAbstractObject
   ```
   because the `USING` clause is read at prepare time, when no parameter is bound
   yet. So parameterising is not a workaround either.

**The fix** is to send the statement as a *string argument* through the Native
API, where no ODBC escape parsing happens:

```python
irispy = iris.createIRIS(iris.createConnection(...))
result = irispy.classMethodObject("%SQL.Statement", "%ExecDirect", None, sql)
```

That is what `db.exec_direct()` is, and it is the only reason it exists. Its
docstring says so, at length, because the next person to read that function will
otherwise wonder why there are two ways to run a statement.

**Cost:** the better part of two hours.
**Fix:** this is a driver bug — brace handling should not apply to a clause the
server parses as JSON. Failing that, it needs to be documented loudly on the
`LOAD DATA` page, because `LOAD DATA` + Python is an obvious combination and
`header:1` is not an exotic option.

#### 5. One package, two connection APIs, no guidance

`intersystems-irispython` gives you:

- `iris.connect()` → DB-API 2.0. Cursors, `?` markers, `cursor.description`.
  Behaves like `sqlite3`. This is what you want for 95% of the work.
- `iris.createConnection()` + `iris.createIRIS()` → the Native API. Globals,
  class methods, `%ExecDirect`. Different object model entirely.

Nothing tells you that the first cannot do `LOAD DATA` options and the second
can. You need both, for one function each, and you find that out by hitting #4.

**Fix for the guide:** a short "which API for which job" table.

### `LOAD DATA`

#### 6. Without `header:1`, the header becomes a data row. Silently.

The failure that produced no error at all. A 1000-line test file gave **1001
rows**, one of which had `VendorID = 'VENDORID'`.

Because the landing table is all-VARCHAR by design (see
[architecture](#architecture-who-does-what)), there is nothing for that row to
violate. It loads clean. It survives the cast as a row of NULLs. It reaches the
analytics. On the full file it would be one bogus row in 787,060 — small enough
to never notice, large enough to be wrong.

This one is only caught by checking `COUNT(*)` against `wc -l`, which is now
what `load_trips` prints.

**Cost:** 20 minutes, and it was luck that the test file had a round number of
lines.
**Fix:** `LOAD DATA` should default to skipping a header when it can detect one,
or at minimum warn. A CSV with a header row is the common case, not the exception.

#### 7. With `header:1`, columns map by *name*, not position

Having added `header:1`, the load broke a different way:

```
LoaderException: Invalid VALUE column, 'zonename' is not defined in header
```

My column was `ZoneName`; the CSV's header says `Zone`. Once a header exists,
`LOAD DATA` matches source to target by header name, so positional assumptions
silently stop applying — and a *renamed* column is a hard error rather than a
mismatch.

The fix, and the better habit anyway, is to state the mapping explicitly:

```sql
INTO Taxi.Zone (LocationID, Borough, ZoneName, ServiceZone)
VALUES         (LocationID, Borough, Zone,     service_zone)
```

`load.py` uses this form even where both lists are identical, so the
correspondence between file and table lives in one readable place
(`schema.ZONE_COLUMN_MAP`, `schema.TRIP_COLUMN_MAP`) instead of being implied.

Related: **double-quoting identifiers in the DDL made this worse**, because
quoted identifiers are case-sensitive and the CSV headers are not consistently
cased. Dropping the quotes lets IRIS fold them and the mismatch disappears.

#### 8. Paths are resolved server-side

`LOAD DATA FROM FILE '/data/...'` is a path *inside the container*, not on your
laptop. Obvious in hindsight; not obvious at 5pm. This is why
`docker-compose.yml` bind-mounts `./data:/data:ro` and `.env` stores the
container path (`TRIPS_CSV_CONTAINER_PATH`) rather than a host path.

### The SQL dialect

#### 9. `HOUR` and `MONTH` are reserved words

```sql
SELECT DATEPART('hour', PickupDateTime) AS hour   -- no
```
```
IDENTIFIER expected, reserved word HOUR found
```

Fine, once you know. The problem is that IRIS's reserved-word list is longer than
the SQL standard's and I had no reason to expect two such ordinary words to be on
it. Aliases are now `pickup_hour` and `month_num`.

**Fix for the guide:** link the reserved-word list from anywhere a newcomer
writes their first `SELECT`.

#### 10. `TO_TIMESTAMP` needs the right format model for a 12-hour clock

The trip file stores `01/15/2023 06:23:41 PM`. The format model that reads it
correctly is `'MM/DD/YYYY HH:MI:SS AM'` — note that `AM` is the *literal token
for a meridian indicator*, not an assertion that the value is morning. Get the
model wrong and you get plausible-looking timestamps twelve hours out, which is
the worst kind of wrong.

#### 11. The default collation silently uppercases every grouped string

The one that felt most like my own bug, and it was not.

```sql
SELECT PUBorough FROM Taxi.Trip WHERE ...            -- 'Manhattan'
SELECT PUBorough FROM Taxi.Trip GROUP BY PUBorough   -- 'MANHATTAN'
```

Same column, same table, different case, no error, no warning. Every chart label
and every borough name arrived shouting.

The cause is that IRIS's default string collation is **`SQLUPPER`**, so the
grouped value you get back is the *collated* value. I confirmed it by comparing
`SELECT`, `GROUP BY` and `%EXACT()` forms of the same query, then tested three
DDL spellings (`COLLATE SQLSTRING`, `COLLATE EXACT`, `COLLATE %EXACT` — all
work). The fix is one clause per display column:

```sql
PUBorough   VARCHAR(50)  COLLATE SQLSTRING,
PUZoneName  VARCHAR(100) COLLATE SQLSTRING,
```

**Cost:** about an hour, most of it spent looking for a bug in my own pandas
code, because "my `GROUP BY` changes the data" is not a hypothesis you reach for.
**Fix for the guide:** this deserves a paragraph in any IRIS SQL introduction.
It is a correct, documented, defensible design decision that will surprise
literally every developer arriving from PostgreSQL or MySQL, and it surprises
them *silently*.

### Data traps

Not IRIS's fault, but they are friction, and they are where the real work was.

#### 12. Quoted thousands separators inside numeric columns

578 cells across three columns look like `"2,032.67"`. A blind
`CAST(trip_distance AS NUMERIC)` does not skip those rows — it **aborts the
statement**, which on a single `INSERT ... SELECT` over 787,060 rows means you
get nothing and a message about one cell.

The guarded repair:

```sql
CASE WHEN ISNUMERIC(REPLACE(trip_distance, ',', '')) = 1
     THEN CAST(REPLACE(trip_distance, ',', '') AS NUMERIC(10,2))
     ELSE NULL END
```

`ISNUMERIC` turns an abort into a NULL, which the quality rules then flag. And
before writing that I checked that the comma is *always* followed by exactly
three digits — otherwise it might be a decimal comma and stripping it would
multiply values by a thousand. It is a separator. Verified, not assumed.

Counts, from `cli profile`: 554 in `trip_distance`, 12 in `fare_amount`, 12 in
`total_amount`. After the fix, **zero unparseable cells remain and zero rows are
rejected**.

#### 13. `ehail_fee` is 100% empty

787,060 of 787,060 rows. Deliberately not carried into the typed table.

#### 14. A 55,613-row block with three fields systematically missing

`passenger_count`, `store_and_fwd_flag` and `congestion_surcharge` are missing
together, for exactly the same 7.07% of rows. The identical count across three
unrelated columns is the tell: this is one upstream system's output, not random
loss. It also explains the `+2.75` reconciliation cluster in #16.

#### 15. The zone lookup contains its own sentinels

`LocationID` 264 is Borough `Unknown`, and 265 is Borough `N/A`. My first
`unknown_zone` rule only checked for `'Unknown'` and NULL, and therefore
undercounted by 3,771 rows (5,827 → **9,598**). The lookup you are joining to in
order to clean your data needs cleaning too.

#### 16. 14.6% of rows do not reconcile — and it is the file, not the rule

`total_mismatch` flags 115,015 rows, far and away the largest flag. That is a
number that looks like a bug in my own predicate, so I went looking for one
rather than reporting it.

It is real. The discrepancies **do not scatter** — they land on three exact
values:

| Discrepancy | Rows | Explanation |
|---|---|---|
| **−1.00** | 73,169 | `mta_tax` recorded as 1.50 where reconciling rows record 0.50, but `total_amount` was computed with 0.50 |
| **−3.75** | 25,208 | that same dollar, *plus* a `congestion_surcharge` of 2.75 recorded but not included in the total |
| **+2.75** | 15,684 | `congestion_surcharge` is NULL while `total_amount` still includes it — these are the #14 rows |

A rounding bug scatters. Three sharp spikes at fee-sized values means the *fees
were recorded inconsistently*, not that the trips are fake. So the flag is a
reconciliation signal, and `analytics.total_reconciliation()` is the query that
demonstrates it — it is on the Trip quality tab of the dashboard.

This is the finding I would lead with, because the lesson generalises: **a flag
firing on 15% of your data is a question, not an answer.**

It also changed what the flag *does*. Because the mismatch is confined to how the
fee columns were recorded, `total_mismatch` invalidates only `fee_breakdown` — not
`fare`, not `tip`, not `trip_count`. The trip happened, the distance is real, and
`fare_amount` is fine; it is the sum of the components that cannot be trusted. That
single mapping puts 115,013 rows back into every other figure on the dashboard. See
[the relevance model](#the-relevance-model-which-rows-a-figure-may-ignore).

#### 17. Unfiltered averages are not merely imprecise, they are impossible

Before filtering, the "average green taxi trip" is:

- **19.02 miles** long
- at an average speed of **81.31 mph**
- with a longest trip of **278,990 miles** — eleven times around the planet

A few hundred rows do that. Filter and you get **3.09 miles at 11.87 mph** —
recognisably a city. Same data, same database, one `WHERE` clause.

The second half of this one took longer to see. Having built the filter, the
tempting thing is `WHERE QualityIssueCount = 0`, and it *looks* right — the numbers
come back plausible. But it answers a different question than the one asked, and
[the relevance model](#the-relevance-model-which-rows-a-figure-may-ignore) is what
came out of noticing. `cleaning_impact()` now reports all three columns side by
side, on the front page of the dashboard, rather than quietly showing the good
numbers:

| Metric | No filter | Blunt filter | Selective |
|---|---|---|---|
| trips | 787,060 | 598,358 | **784,808** |
| avg_miles | 19.02 | 2.88 | **3.09** |
| avg_minutes | 19.6 | 14.6 | **15.3** |
| avg_mph | 81.31 | 11.73 | **11.87** |
| max_miles | 278,990.28 | 96.26 | **111.66** |
| avg_fare | $18.33 | $17.35 | **$18.37** |
| avg_tip_pct | 14.60% | 14.49% | **13.88%** |

Two things to read off that table. The blunt filter's average fare, $17.35, is
*below* the unfiltered figure, which should be suspicious — the outliers it removed
were high-value, so how did the average fall? Because it also removed 180,000 rows
for reasons unrelated to fares, and those rows carry higher fares than average.

And `max_miles` makes it concrete. The blunt filter's longest trip is 96.26 miles.
The selective one finds a **111.66-mile, 203-minute, $400** trip — about 33 mph
average, an entirely ordinary long-haul run — whose *only* flag is `unknown_zone`.
It was excluded from a distance figure because its drop-off point is missing from
the lookup table.

### Performance

#### 18. My own slow stage

`cast_and_enrich` takes 71 of the pipeline's 90 seconds. The cause is mine, not
IRIS's: each row's two timestamps are re-parsed by `TO_TIMESTAMP` for *every*
derived column that needs them — `TripMinutes`, `AvgMph`, `PickupDate`,
`PickupHour`, `PickupDayOfWeek`, `PickupMonth` — roughly ten parses per row where
two would do. Staging the parsed timestamps first, then deriving from those,
should cut it substantially. Not done; see [limitations](#known-limitations).

### Tooling (my environment, not IRIS)

Included for completeness, and because they shaped how the UI got verified.

#### 19. No JavaScript runtime for the palette validator

The visualization guidance I followed requires *running* a colour-blindness
validator rather than eyeballing the palette. There is no `node`, `deno` or `bun`
on this machine.

Resolved rather than skipped: macOS ships JavaScriptCore, reachable as
`osascript -l JavaScript`. Stripping the ES-module `export` keywords lets the
validator run as a plain function body, and both its auto-run blocks are guarded
on `process`/`document` so neither fires. The palette (categorical slots 1–3 plus
the blue sequential ramp) **passes every hard gate in both light and dark mode**
under the strictest all-pairs setting — worst CVD ΔE 9.2 light / 9.4 dark against
a target of 8.0, worst normal-vision ΔE 24.0 light / 20.9 dark against a floor of
15.0.

Slot 3 (aqua `#1baf7a`) arrived with the bench panel's third arm and is the one
value that trips a WARN rather than a PASS: 2.74:1 against this light surface,
under the 3:1 line. That is a documented conditional relief, not a dismissable
one — it is legal only with visible labels or a table view. The single figure that
uses the slot has both (direct value labels on every column, and the figure's own
table underneath), so the condition is met rather than ignored. Re-running the
validator is one command:

```
$ osascript -l JavaScript /tmp/pal.js     # loads static/validate_palette.js, strips `export`
=== light / all  ok=true
   ["CVD separation","pass","worst all-pairs #1baf7a↔#eb6834 ΔE 9.2 (deutan)"]
   ["Contrast vs surface","relief","below 3:1 — relief required: [[\"#1baf7a\",2.74]]"]
```

#### 20. Headless Chrome is blocked, so the UI got a different kind of check

`--headless --screenshot` and `--dump-dom` both exit 0 with empty output on this
machine (corporate policy, almost certainly). So instead of looking at a
screenshot, `tools/render_check.js` builds a small DOM shim and **executes every
panel's render path** against real captured API payloads, asserting no runtime
errors, no non-finite SVG geometry, and no element ids that `index.html` does not
define.

```
$ osascript -l JavaScript tools/render_check.js
  overview    282 nodes   5 svg    18 marks    6 bars   0 tables
  quality     214 nodes   0 svg     0 marks   26 bars   0 tables
  zones       289 nodes   0 svg     0 marks   36 bars   1 tables
  time        421 nodes   5 svg   213 marks    0 bars   0 tables
  browse      379 nodes   0 svg     0 marks    0 bars   1 tables  103 tested cells
  bench       157 nodes   1 svg     9 marks    3 bars   2 tables
  pairs       215 nodes   0 svg     0 marks   15 bars   1 tables
PASSED — no runtime errors, no non-finite geometry, no missing ids.
```

The mark counts reconcile exactly, which is the point of printing them — `time`'s
213 = 24 hour bars + 1 speed line + 7 weekday bars + 12 month bars + 168 heatmap
cells + 1 hover marker, and `bench`'s 9 = 3 questions × 3 arms (it was 6 before
Embedded Python became the third arm, and drops back to 6 if the server has no
pandas — a path worth running the shim against, which is how I know the panel
degrades to two series instead of throwing).

**It earned its keep immediately**: it caught a real bug in `niceTicks()`, where
the top axis tick could land *below* the data maximum, so the tallest bar in
every chart would have been drawn past the top of its plot area. That is a defect
I would probably have seen in a screenshot and *definitely* would have shrugged
at as a styling quirk.

The honest caveat: a shim proves the code runs and the numbers are sane. It says
nothing about whether two axis labels overlap. That check is still outstanding —
see [limitations](#known-limitations).

---

## What worked well

The presentation asks what worked, and plenty did:

- **`LOAD DATA` is genuinely excellent.** 787,060 rows in 0.72 seconds, one
  statement, no chunking logic, no progress bar needed. Once past #4 and #6 it
  was the least troublesome part of the project.
- **DB-API compliance is real.** `iris.connect()` behaves like `sqlite3`. `?`
  markers, cursors, `description`, `rowcount`, context managers — all as you
  expect. Almost all of `db.py` is boilerplate I did not have to think about.
- **`INSERT ... SELECT` for the whole cast/derive/enrich stage** is a genuinely
  better pattern than pulling rows into Python, and it was easy to express.
- **Bitmap indexes** are a good fit for exactly this shape of data and made the
  low-cardinality `GROUP BY`s fast without any tuning on my part.
- **`%SQL_Diag.Result` / `%SQL_Diag.Message`** give real per-row load
  diagnostics. Not needed in the end — the all-VARCHAR landing table means
  nothing fails — but good to know they are there.
- **`DROP TABLE IF EXISTS`, `TUNE TABLE`, `ISNUMERIC`, `DATEDIFF`, `DATEPART`,
  `DAYOFWEEK`** all did what the name says on the first try.
- **The generated-DDL approach paid off.** Because `schema.py` reads the rule
  registry, adding a fourteenth rule required editing one list.

---

## Findings in the data

| | |
|---|---|
| Rows in file | 787,060 |
| Rows that failed to load | 0 |
| Rows that failed to cast | 0 |
| Trips passing all 14 rules | **598,358 (76.02%)** |
| Trips with 1 issue | 138,251 (17.57%) |
| Trips with 2 issues | 46,170 (5.87%) |
| Trips with 3+ issues | 4,281 (0.54%) |
| Pickup zones seen | 252 of 265 |
| Trips usable for a *fare* figure | **784,778 (99.71%)** |
| Trips usable for a *distance* figure | 741,533 (94.22%) |
| Trips usable for a *fee-breakdown* figure | 669,798 (85.10%) |

Those last three are the point of the [relevance
model](#the-relevance-model-which-rows-a-figure-may-ignore): 76.02% is the fraction
of rows with *nothing at all* wrong, and almost no figure needs that. The average
fare rests on 99.71% of the file, not 76%.

The average trip, on the rows that can speak to it: **3.09 miles**, **15.3
minutes**, **11.87 mph**, **$18.37**, tipping **13.88%** on card. Note that $18.37
is *higher* than the unfiltered $18.33 and much higher than the blunt filter's
$17.35 — see [#17](#17-unfiltered-averages-are-not-merely-imprecise-they-are-impossible).

Top flags: `total_mismatch` 115,015 (14.6%), `missing_passenger_count` 61,756
(7.8%), `non_positive_distance` 39,494 (5.0%), `unknown_zone` 9,598 (1.2%). At
the other end, `outside_2023` catches **7** rows — the earliest recorded pickup
in this 2023 file is dated 2008-12-31. The two largest flags are also the two
narrowest in effect: `total_mismatch` invalidates only `fee_breakdown` and
`missing_passenger_count` only `passengers`, so between them they remove 176,769
rows from two figures and none from the other twelve.

Substantive findings are in the friction log above, because for this project
finding them *was* the friction: [#16](#16-146-of-rows-do-not-reconcile--and-it-is-the-file-not-the-rule)
(the fee-recording defect), [#17](#17-unfiltered-averages-are-not-merely-imprecise-they-are-impossible)
(outliers make raw averages impossible), and
[#14](#14-a-55613-row-block-with-three-fields-systematically-missing) (one
upstream system's missing-field block).

One more worth stating: **cash tips are never metered**, so a cash row's
`tip_amount` is essentially always zero. Reading tip percentage without splitting
by payment type says the outer boroughs do not tip. They do; they pay cash. This
is why `tipping_by_borough()` groups by payment type — a data-collection artefact
masquerading as a behavioural finding.

---

## The stretch goal: IRIS SQL vs pandas vs Embedded Python

The guide asks how doing the filtering and aggregation in IRIS compares to
pulling everything into Python: how much data moves, which is easier to
maintain, which scales. `bench.py` answers it by measurement, and the dashboard's
last tab runs it live.

### Aggregation

Each question is answered three ways:

1. **IRIS SQL** — `GROUP BY` in the database; a few dozen rows come back.
2. **Host pandas** — `SELECT` the raw columns for all 787,060 rows and reduce
   them in pandas in the client process.
3. **Embedded Python** — that *same* pandas reducer, running inside the IRIS
   process, fed by `iris.sql.exec(...).dataframe()` with no driver and no wire.

**Every answer is asserted equal to the IRIS-side answer with
`pd.testing.assert_frame_equal` before any timing is reported**, because a faster
wrong answer is not a result. All three arms agree on all three questions.

The third arm is there to split a variable the first two confound. Host pandas
loses to `GROUP BY` for two reasons at once — 787k rows cross the driver, *and*
every row has to be materialised in Python — and the two-arm version of this
benchmark cannot say which reason dominates. Embedded Python removes only the
first.

| Question | IRIS SQL | Host pandas | Embedded Python | pandas ÷ IRIS | Rows moved | Ratio |
|---|---|---|---|---|---|---|
| Revenue by pickup borough | 0.030s | 0.704s | 0.903s | **23.7×** | 8 vs 787,060 | 98,382× |
| Trips & distance by hour | 0.023s | 0.644s | 0.786s | **28.0×** | 24 vs 787,060 | 32,794× |
| Distribution of issue counts | 0.007s | 0.310s | 0.603s | **42.0×** | 6 vs 787,060 | 131,177× |

*(median of 2 runs; the embedded arm returns the same 8/24/6 answer rows the IRIS
arm does, having scanned all 787,060 in-process)*

**Embedded Python is slower than host pandas here, and that is the finding.**
Being closer to the data bought nothing, because the wire was never the
bottleneck. Its own split of the time says so:

| Question | pandas fetch | pandas reduce | embedded fetch | embedded reduce |
|---|---|---|---|---|
| Revenue by pickup borough | 0.645s | 0.055s | 0.705s | 0.033s |
| Trips & distance by hour | 0.592s | 0.050s | 0.635s | 0.010s |
| Distribution of issue counts | 0.306s | 0.005s | 0.463s | 0.005s |

Both pandas arms spend 90–98% of their time turning 787,060 rows into a frame
and hundredths of a second reducing it. On this host the driver does that
marshalling slightly *faster* than `iris.sql`'s in-process DataFrame build, so
removing the network — which, with the container on the same machine, is loopback
and nearly free — leaves the embedded arm holding the same row-by-row cost with
no compensating win. The 23–42× that `GROUP BY` wins by is not a
transport number at all: it is the cost of materialising every row in Python,
wherever the interpreter happens to live.

Two things this does *not* say. Across a real network the ranking of arms 2 and 3
would shift, because the wire that is nearly free here would not be; the
measurement above is honest about one deployment, not all of them. And Embedded
Python is not thereby useless — it is the right tool when the work genuinely
cannot be expressed in SQL (a scikit-learn model, a bespoke parser) and you want
it next to the data. It is the wrong tool for an aggregation SQL already does.

The two pandas arms run the same code by construction, not by inspection:
`bench.py` builds the `LANGUAGE PYTHON` function body with
`inspect.getsource(comparison.reduce_in_pandas)`, so the reducer inside IRIS is
the source text of the function object the host arm calls. They cannot drift.

### Ingest

| Approach | Rows/second | vs `LOAD DATA` |
|---|---|---|
| IRIS `LOAD DATA` (server-side) | 237,160 | — |
| Python `executemany`, batches of 5,000 | 105,366 | 2.3× slower |
| Python, one `INSERT` per row | 6,620 | **36× slower** |

### What I conclude

**On speed:** IRIS-side wins, by 24–42× on aggregation, and it wins for a reason
worth naming precisely: not because the rows travel, but because they are
materialised one at a time in Python at all. Embedded Python demonstrates that by
removing the travel and still losing. But the ingest table is
the more useful result, because it shows the axis that actually matters is
**batched versus not**, more than Python versus SQL. Batched `executemany` is
only 2.3× behind a purpose-built parallel bulk loader — perfectly reasonable.
The row-at-a-time loop, which is the obvious first thing anyone writes, is 36×
behind. If you take one number away, take that one.

**On data movement:** this is the bigger deal, and it is the argument that keeps
working as the data grows. Answering "revenue by borough" needs **eight rows** of
answer. Moving 787,060 rows to compute it means the driver, the network and the
Python process all handle ~98,000× more data than the question requires. The
speed difference is a symptom; the transferred volume is the cause.

**On maintainability:** genuinely mixed, and I do not think the honest answer is
"IRIS wins".

- The SQL version is shorter and states the intent directly.
- The pandas version is easier to *debug*, because you can look at the
  intermediate frame.
- SQL in Python strings gets no syntax checking until it runs, and the four
  reserved-word and collation problems above (#9, #11) were all "valid Python,
  invalid or surprising SQL, discovered at runtime".
- The hybrid this project settled on — **rules and thresholds as Python data,
  compiled to SQL** — is the part I would actually defend. `quality.py` reads
  like a specification, and it executes as a single server-side pass. That is
  better than either pure alternative.

**On scaling:** the IRIS-side approach is bounded by the data on disk; the
Python-side approach is bounded by RAM. At 787k rows both fit comfortably. At
50× this file, one still works unchanged and the other needs chunking logic that
is itself a source of bugs. That is not a speed argument, it is a "does it work
at all" argument.

**One caveat I should flag rather than bury:** the ingest table's
*projected-full-file* column extrapolates a 50,000-row rate to 787,060 rows, and
that flatters nobody consistently — `LOAD DATA` pays a fixed setup cost and
parallelises better on bigger files, so its projection (3.3s) is over three times
worse than the *measured* full-file load (0.72s). The measured subset rates are
sound; the extrapolation is a guide, and the dashboard says so on the figure.

---

## Recommendations for the next cohort

Ordered by how much time they would save, most first.

1. **Ship a working `docker-compose.yml` + `Dockerfile` with the guide.** With
   passwords un-expired and `%Service_CallIn` enabled. This single artefact
   removes friction #1, #2 and #3 — over an hour, before anyone writes a line of
   interesting code. Nobody's first IRIS experience should be
   `Security.Users.UnExpireUserPasswords`.

2. **Document the `LOAD DATA` + DB-API brace conflict, or fix it.** Friction #4
   cost the most, and the error message actively misdirects. `LOAD DATA` from
   Python with `header:1` is not an exotic path — it is the obvious first thing
   to try. Ideally the driver should not apply ODBC escape parsing to a clause
   the server reads as JSON.

3. **Put the `SQLUPPER` collation default in the first SQL tutorial.** Friction
   #11 is silent, it corrupts presentation output, and it will surprise every
   developer arriving from another database. One paragraph and a `COLLATE
   SQLSTRING` example prevents it.

4. **Make `LOAD DATA` warn about an apparent header row.** Friction #6 produced
   no error at all, and the landing-table pattern that makes loads robust is
   exactly what stops you noticing. A one-line warning would do.

5. **Add a "which API for which job" table to the driver docs.** DB-API vs
   Native, and the specific things only the latter can do.

6. **Link the reserved-word list from the SQL getting-started page.** `HOUR` and
   `MONTH` are not words anyone expects to be reserved.

7. **Say `import iris` in the install instructions.** One line.

8. **Consider giving the sample data a deliberate flaw list.** The thousands
   separators, the empty `ehail_fee`, the 55,613-row missing-field block and the
   fee-reconciliation defect made this project *much* more interesting than clean
   data would have. I would keep them and not warn people — but a facilitator's
   note listing them would help whoever runs the debrief know what to look for.

9. **Ask the quality-workflow requirement to specify how the flags get used.**
   This one is about the guide, not about IRIS. "Identify questionable records" is
   naturally read as "and then exclude them," and that reading produces a *biased*
   dashboard that looks careful — [the relevance
   model](#the-relevance-model-which-rows-a-figure-may-ignore) is what it took to
   undo it here. One sentence in the brief — *a flag is a statement about one
   field, not a verdict on the row* — would put every team on the interesting side
   of that question from the start.

---

## Known limitations

Things I know are imperfect, stated rather than hidden.

1. **`cast_and_enrich` is ~4× slower than it needs to be** (friction #18). Each
   row's timestamps are parsed roughly ten times instead of two. The fix —
   staging parsed timestamps, then deriving from those — is clear and untried.

2. **The dashboard has not been looked at in a real browser by me.** Headless
   Chrome is blocked here (friction #20), so I verified it by executing every
   render path against real payloads with geometry assertions. That catches
   crashes, NaNs and bad scales — it caught one real bug — but it cannot catch
   overlapping axis labels or a chart that is technically correct and ugly.
   **Someone should open it and look before it is presented.**

3. **The dashboard caches every result in-process.** Correct while `Taxi.Trip` is
   static, wrong if you re-run the pipeline behind a running server. Use
   `POST /api/cache/clear`, or restart it.

4. **`web.py` is Flask's development server**, single-process, no auth, bound to
   localhost. Appropriate for a demo and nothing else.

5. **Credentials are dev-only and in the repo.** `_SYSTEM`/`SYS` appear in
   `Dockerfile` and `.env.example` deliberately, so setup is one command. `.env`
   is git-ignored. This is fine for a throwaway container and would not be fine
   for anything else.

6. **`derive_thresholds()` is offered, not adopted.** It measures cutoffs from
   the loaded data's upper 0.1% tail as an alternative to the hand-set
   thresholds. I kept the hand-set ones, because a percentile always flags
   exactly 0.1% of rows whether or not that 0.1% is wrong, whereas "no
   green-taxi trip is twelve hours long" is a falsifiable claim about taxis. Both
   are available so they can be compared: `cli quality --derive-thresholds`.

7. **Rejected-row handling is untested in anger.** `Taxi.TripReject` exists and
   the row-count reconciliation is enforced, but this file produces zero rejects,
   so that path has never actually carried a row.

---

## File map

```
docs/project_guide.md         the assignment

Dockerfile                    IRIS Community + the dev bootstrap (friction #2)
iris.script                   un-expire passwords, enable %Service_CallIn
docker-compose.yml            ports 1973/52774, ./data mounted read-only at /data
requirements.txt              4 dependencies
.env.example                  connection settings and container-side CSV paths

src/taxi/
  config.py      52   env loading, IrisConfig, table-name constants
  db.py         188   the only module that talks to IRIS; exec_direct lives here
  schema.py     230   DDL generated from Python, incl. one flag column per rule
  load.py       137   server-side LOAD DATA with explicit column mapping
  transform.py  245   cast + derive + enrich, as one INSERT ... SELECT
  quality.py    535   14 rules as Python data, compiled to two UPDATEs;
                      each rule declares the measures it invalidates
  analytics.py  572   the user-facing workflows; IRIS aggregates, pandas labels
  bench.py      375   the stretch goal, with correctness assertions
  cli.py        185   info / pipeline / profile / quality / analyze / bench
  web.py        328   JSON endpoints over analytics + quality; serves the page

src/taxi/static/
  index.html    107   the whole page skeleton
  app.js       1504   hand-rolled SVG charts; no framework, no build, no CDN
  styles.css    561   design tokens + layout; every colour is a named token
  validate_palette.js   the colour validator, vendored so it can run in-browser

tools/
  render_check.js  340   executes every panel headlessly (friction #20)
  fixtures/*.json        real captured API payloads it runs against
```

### Why the front end is hand-written

Three reasons, in order of how much they mattered:

1. **It has to work in a room, on a laptop, on demand.** A CDN `<script>` tag is
   a single point of failure five minutes before a presentation. There is no
   network dependency and no build step: `python -m taxi.web` and it works.
2. **The charts needed are a column chart, a line chart, a heatmap and a ranked
   bar list.** That is about 200 lines of SVG.
3. **Every mark specification the design guidance asks for** — thin bars, 4px
   rounded data-ends, 2px lines, a 2px surface gap between adjacent fills,
   recessive gridlines, a table view behind every figure, no dual-axis charts —
   is easier to satisfy directly than to talk a charting library out of its
   defaults.

Colour is never written as a hex in `app.js`; marks reference CSS custom
properties by role, so light and dark swap in one place and the palette stays
auditable in a single file.
