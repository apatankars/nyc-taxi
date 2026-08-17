# Setup & Working With IRIS — Runbook

Companion to [IRIS_primer.md](IRIS_primer.md). The primer explains *what IRIS is*; this explains
*how you operate it* on this project.

---

## Part 1 — The virtual environment

### Why you need one

The `iris` Python driver is a compiled wheel that must be importable by whatever Python runs your
code. You have three Pythons on this machine and they are not interchangeable:

| Python | Where | Has `iris`? |
|---|---|---|
| 3.9.6 | `/usr/bin/python3`, Apple Command Line Tools | No — and shouldn't |
| 3.12.3 | inside the container | No |
| `irispython` | inside the container, `/usr/irissys/bin/` | **Yes, always** |

The host's 3.9.6 is the one your project code will run under, so it's the one that needs the driver.
A venv matters here for reasons beyond general tidiness:

1. **The system Python is Apple's.** Installing into it needs `sudo`, can be clobbered by an Xcode
   update, and pollutes every other project on the machine.
2. **Its bundled pip is 21.2.4** — four years stale. A venv gets its own upgradeable pip.
3. **Reproducibility.** Your teammates and the presentation need `pip install -r requirements.txt` to
   just work. Without a venv, "works on my machine" is unfalsifiable.

### Creating it

```bashç
cd /Users/apatanka/Desktop/nyc_taxi
python3 -m venv .venv
source .venv/bin/activate          # prompt gains a (.venv) prefix
python -m pip install --upgrade pip
pip install -r requirements.txt
```

Re-activate with `source .venv/bin/activate` in each new shell. In VS Code, run
**Python: Select Interpreter** and choose `.venv` so the editor and notebooks use it too.

### What's in `requirements.txt` and why

- **`intersystems-irispython==5.4.0`** — the driver. Note the trap: the *package* is
  `intersystems-irispython`, the *import* is `iris`. Pinned because it's the one dependency where a
  surprise version change would break everything at once.
- **`pandas`** — `pd.read_sql(sql, conn)` works directly against an IRIS DB-API connection, which is
  what makes the stretch-goal comparison easy to write. 2.3.3 verified on Python 3.9.6.
- **`matplotlib`** — charts for the presentation.
- **`jupyterlab`** — the lowest-risk demo interface of the ones the brief lists.
- **`python-dotenv`** — keeps host/port/credentials in `.env` instead of hardcoded in source.

### One trap that already bit us

**Never name a file `iris.py`, and never create one anywhere on your import path.** Python resolves
the script's own directory first, so a file at `src/iris.py` shadows the driver entirely. The symptom
is baffling:

```
AttributeError: module 'iris' has no attribute 'connect'
```

The import "succeeds" — it just imported your file. An empty `src/iris.py` existed in this project and
produced exactly this. Same applies to `queue.py`, `types.py`, etc. When an import behaves
impossibly, check `print(iris.__file__)` first.

---

## Part 2 — Replacing the container

### Why replace it at all

The container currently running was built from `~/Desktop/objectscript-docker-template-master`. Two
concrete problems for this project, both covered in the primer's §2: its host ports are unpinned so
they change on every recreate, and it mounts only the template directory, so **IRIS cannot see your
CSVs** — which blocks server-side loading entirely.

### Stopping the old one

It has `restart: always`, so `docker stop` alone isn't enough — Docker will bring it back. Take it
down through Compose, from the directory that owns it:

```bash
cd /Users/apatanka/Desktop/objectscript-docker-template-master
docker compose down
```

This stops and removes the container. It does **not** delete the built image or that directory, so
`docker compose up -d` there restores it whenever you want it back. Nothing is lost.

Confirm nothing is holding the ports:

```bash
docker ps
```

### Starting the new one

```bash
cd /Users/apatanka/Desktop/nyc_taxi
docker compose up -d --build
```

First run pulls `intersystemsdc/iris-community:latest` (a few GB) and then runs the build's
config step, so expect several minutes. Subsequent starts are seconds.

Watch it come up:

```bash
docker compose logs -f iris      # Ctrl-C to detach; wait for the startup banner
```

Then verify end to end:

```bash
source .venv/bin/activate
python src/iris_conn.py
```

Expected:

```
Connected to localhost:1972/USER
version: IRIS for UNIX (...) 2026.1 (Build 234U) ...
```

### What the new setup gives you

| | Old (template) | New |
|---|---|---|
| Superserver port | random (52792 today) | **1972**, pinned |
| Portal port | random (52794 today) | **52773**, pinned |
| Sees your CSVs | no | yes, at `/data/` |
| Can write files back | no | yes, `iris_out/` → `/output` |
| Extra baggage | ObjectScript demo classes, ZPM | none |

Portal after the swap: <http://localhost:52773/csp/sys/UtilHome.csp>

### Day-to-day

```bash
docker compose up -d          # start
docker compose stop           # pause, keeps data
docker compose down           # remove container, keeps data (in the image's volume)
docker compose down -v        # remove container AND data — full reset
docker compose exec iris bash # shell inside
```

`docker compose down -v` is your "I've wedged the database" escape hatch: it discards everything and
you reload from CSV. Given that your data is a reproducible CSV load, don't be precious about it —
being able to rebuild from scratch in one command is a genuine advantage of the container approach,
and worth saying so in the presentation.

---

## Part 3 — How data gets into IRIS

Three routes. The difference between them is **where the file must physically be**, and that's the
thing that trips everyone up.

### Route A — Server-side `LOAD DATA` (fastest)

IRIS reads the file itself. No row ever crosses the network.

```sql
LOAD DATA FROM FILE '/data/2023_Green_Taxi_Trip_Data.csv'
INTO Taxi.TripRaw
USING {"from": {"file": {"header": true}}}
```

**The path is inside the container, not on your Mac.** That is what `./data:/data:ro` in the compose
file buys you: `data/2023_Green_Taxi_Trip_Data.csv` on your Mac is `/data/2023_Green_Taxi_Trip_Data.csv`
to IRIS. Get this wrong and you get `FileNotFoundException` for a path you can plainly see in Finder.

Trade-off: it's a fairly literal load. The taxi CSV's `MM/DD/YYYY hh:mm:ss AM/PM` timestamps won't
land in a `TIMESTAMP` column cleanly, so land them in a staging table with `VARCHAR` timestamps and
convert with SQL, or use Route B.

### Route B — Client-side `executemany` (most control)

Python reads and transforms; IRIS receives finished rows. Every row crosses the wire, so batch it.

```python
import csv
from datetime import datetime
from iris_conn import connect

def parse_ts(s):
    return datetime.strptime(s, "%m/%d/%Y %I:%M:%S %p")

with connect() as conn:
    cur = conn.cursor()
    sql = "INSERT INTO Taxi.Trip (pickup_ts, dropoff_ts, pu_location, trip_distance) VALUES (?,?,?,?)"
    with open("data/2023_Green_Taxi_Trip_Data.csv", newline="") as f:
        batch = []
        for row in csv.DictReader(f):
            batch.append((parse_ts(row["lpep_pickup_datetime"]),
                          parse_ts(row["lpep_dropoff_datetime"]),
                          int(row["PULocationID"]),
                          float(row["trip_distance"])))
            if len(batch) >= 10_000:
                cur.executemany(sql, batch); conn.commit(); batch = []
        if batch:
            cur.executemany(sql, batch); conn.commit()
```

This is where the brief's "Python as the primary implementation language" is most naturally
satisfied: parsing, type coercion and quality flagging all happen here, in code you can explain.

Remember `encoding="utf-8-sig"` for `taxi_zone_lookup(in).csv` — it has a byte-order mark that will
otherwise corrupt the `LocationID` column name.

### Route C — pandas round-trip (quickest to write)

`pandas.to_sql` needs SQLAlchemy, which is an extra dependency and slow for 787k rows. Fine for the
265-row zone lookup, wrong tool for the trips. Mentioned so you can rule it out deliberately.

### Recommended for this project

**Load the zone lookup with Route B** (tiny, and the BOM needs handling). **Load the trips both
ways** — Route A into a staging table and Route B with Python-side cleaning — and time them. You need
a load story for the presentation anyway, and "we measured both" is a much better answer than "we
picked one."

---

## Part 4 — How code gets into IRIS

You have to decide whether your logic runs *beside* the database or *inside* it. Both are legitimate;
knowing that the choice exists is the point.

### Beside it: Python on the host (your default)

Ordinary `.py` files in `src/`, talking over port 1972. Normal tooling — debugger, git, tests. IRIS
is "just the database." **Do this for most of the project.**

### Inside it: SQL objects

Views, computed columns, indexes and constraints are code that lives in the database, created from
Python via DDL:

```python
execute("""
CREATE VIEW Taxi.SuspectTrip AS
SELECT %ID AS trip_id, pickup_ts, trip_distance, fare_amount,
       DATEDIFF('minute', pickup_ts, dropoff_ts) AS duration_min
FROM Taxi.Trip
WHERE trip_distance <= 0 OR fare_amount < 0
""")
```

A view is the cheapest possible way to make the trip-quality workflow reusable: define the rules
once, and every consumer — notebook, dashboard, Portal — sees the same definition.

### Inside it: classes with Embedded Python methods

The distinctive option. A persistent class is simultaneously a SQL table (primer §4), and its methods
can be written in Python running *in* the database process:

```objectscript
Class Taxi.Trip Extends %Persistent
{
Property tripDistance As %Double;
Property pickupTs As %TimeStamp;

ClassMethod FlagOutliers() As %Integer [ Language = python ]
{
    import iris
    rs = iris.sql.exec("SELECT %ID, trip_distance FROM Taxi.Trip WHERE trip_distance > 100")
    # ... no network hop, no serialization
}
}
```

Load a class file with `docker compose exec iris iris session IRIS -U USER` then
`do $System.OBJ.Load("/data/../MyClass.cls","ck")`, or use the VS Code ObjectScript extension.

**Cost/benefit:** you'd be learning ObjectScript class syntax mid-project. Worth it for *one* method,
as a deliberate experiment for the stretch goal, so you can speak to it. Not worth it for the bulk of
the application.

### Inside it: interactive Embedded Python

No class file needed — just a Python REPL inside the database:

```bash
docker compose exec iris /usr/irissys/bin/irispython
```

```python
import iris
rs = iris.sql.exec("SELECT COUNT(*) FROM Taxi.Trip")
print(rs.dataframe())
```

The cleanest demonstration of the push-down idea: identical Python, zero data transfer. Ten minutes
here will give you a better answer to "how much data needs to move?" than an hour of reading.

---

## Part 5 — How Python and IRIS interact, concretely

Four patterns, in the order you'll reach for them.

**1. Query into pandas** — the everyday case.

```python
import pandas as pd
from iris_conn import connect

with connect() as conn:
    df = pd.read_sql("""
        SELECT z.borough, z.zone, COUNT(*) AS trips, AVG(t.fare_amount) AS avg_fare
        FROM Taxi.Trip t JOIN Taxi.Zone z ON t.pu_location = z.location_id
        GROUP BY z.borough, z.zone
        ORDER BY trips DESC
    """, conn)
```

**2. Parameterised statements** — `?` placeholders, not string formatting.

```python
cols, rows = query("SELECT COUNT(*) FROM Taxi.Trip WHERE pu_location = ?", [166])
```

**3. Native API for class methods** — when logic lives in IRIS.

```python
from iris_conn import native

with native() as n:
    n.classMethodValue("Taxi.Trip", "FlagOutliers")
```

**4. Embedded Python** — Python running inside IRIS, as in Part 4.

### The two failure modes worth memorising

- **Rows aren't tuples.** `cur.fetchall()` yields `iris.dbapi.DataRow`. Index or convert them;
  `src/iris_conn.py::query` does the conversion for you.
- **`fetchall()` after DDL raises `<USAGE ERROR>`.** The statement succeeded; there's just no result
  set. This is why `iris_conn.py` splits `query()` from `execute()`.

---

## Part 6 — How this fits the project

```
  data/*.csv
      │
      │  Route A: LOAD DATA (server reads /data directly)
      │  Route B: Python parse + executemany  ◄── most of the "Python as primary language" credit
      ▼
┌─────────────────────────────────────────────┐
│  IRIS  (localhost:1972, namespace USER)     │
│                                             │
│  Taxi.Trip     787k rows                    │
│  Taxi.Zone     265 rows   ──► enrichment join
│  Taxi.SuspectTrip (view)  ──► quality workflow
│  bitmap / columnar        ──► stretch goal  │
└─────────────────────────────────────────────┘
      │
      │  DB-API + pandas (aggregate in IRIS, return small results)
      ▼
  Notebook / script / dashboard  ──► presentation
```

Requirement-by-requirement:

| Brief requirement | What you use |
|---|---|
| Load data into an IRIS instance you set up | `docker-compose.yml` + `Dockerfile` here; Route A and/or B |
| Python as primary implementation language | `src/` — parsing, validation, enrichment, analysis |
| IRIS as the data platform | `Taxi.*` tables; SQL does the aggregation |
| Enrich with zone/borough names | `Taxi.Zone` join, or denormalise and compare |
| Trip-quality workflow | `Taxi.SuspectTrip` view + Python rules; `DATEDIFF` verified working |
| Two more user workflows | busiest zones, hourly profile, O/D pairs — plain `GROUP BY` |
| Interface of your choice | JupyterLab (already in `requirements.txt`) |
| Show Python + IRIS beat raw files | indexed/aggregated queries vs. re-parsing 100 MB of CSV each time |
| **Stretch:** push-down vs. pull-up | same analysis both ways; measure rows moved, wall-clock, LOC |

### Suggested order of work

1. Swap the container (Part 2), confirm `python src/iris_conn.py`.
2. Load `Taxi.Zone` — 265 rows, exercises the whole path cheaply, BOM lesson included.
3. Profile the raw CSV in pandas *before* designing the schema. Let the data's actual mess pick your
   column types and quality rules.
4. Create `Taxi.Trip`, load it, time it.
5. Build the quality view and the two analytics workflows.
6. Stretch goal: pick one workflow, do it both ways, measure.
7. Notebook that tells the story, plus your friction log.

Steps 1–2 are the ones where a whole afternoon can disappear on environment issues. That's not
wasted time — it *is* the deliverable the brief is actually asking about. Log it as you go.
