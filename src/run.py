"""The one entry point: build whatever is missing, then serve the dashboard.

    docker exec -w /src nyc-taxi-iris /usr/irissys/bin/irispython run.py

Or, from the host, `./run.sh` -- which starts the container first and then runs
exactly this.

Everything runs inside the IRIS process under Embedded Python. When it finishes,
the dashboard is registered and answering on

    http://localhost:52773/taxi/     (_SYSTEM / SYS on this dev image)

Re-running is the normal case and is cheap: stages 1-3 are skipped when the typed
Trip table is already populated, because reloading 787,060 rows to look at a
dashboard is a waste of several minutes. The stages that *are* always re-run are
the ones whose inputs live in code you might have just edited -- the views and
UDFs, the quality rules, the SQL function, the web application registration.

    run.py                 build if needed, then serve
    run.py --reload        drop and rebuild everything from the CSVs
    run.py --reload 50000  same, but only cast the first 50,000 raw rows
    run.py --flags         re-apply the quality rules only (after editing rules.py)

Stage modules stay separately runnable for working on one of them in isolation:

    irispython stage4_flag.py
"""

import sys

import stage1_schema
import stage2_load_raw
import stage3_cast
import stage4_flag
import stage5_procs
import stage5_web
from db import banner, log, scalar, try_exec

# Below this, the typed table is treated as not built -- covers both an empty
# table and a run that was interrupted part way through the cast.
MIN_TRIPS = 1000


def trip_count():
    """Rows in Taxi.Trip, or None if the table does not exist yet.

    try_exec rather than a catch around scalar(): on the first run the table is
    genuinely absent, which is an answer, not an error.
    """
    if not try_exec("SELECT COUNT(*) FROM Taxi.Trip"):
        return None
    return int(scalar("SELECT COUNT(*) FROM Taxi.Trip") or 0)


def load(limit=None):
    """Stages 1-3: schema, raw CSV land, typed cast."""
    stage1_schema.build()
    stage2_load_raw.run()
    stage3_cast.run(limit=limit)


def serve():
    """Stages 4-5: apply the rules, expose them to SQL, register the web app."""
    stage4_flag.run()
    stage5_procs.build()
    return stage5_web.register()


def main(argv):
    args = [a for a in argv if a != "--"]
    reload_all = "--reload" in args
    flags_only = "--flags" in args
    limit = next((int(a) for a in args if a.isdigit()), None)

    unknown = [a for a in args
               if a not in ("--reload", "--flags") and not a.isdigit()]
    if unknown:
        log(f"unrecognised argument(s): {' '.join(unknown)}")
        log(__doc__)
        return 2

    if flags_only:
        # The whole point of stage 4 being re-runnable: change a threshold in
        # rules.py, re-apply, refresh the page. No reload, no re-cast.
        stage4_flag.run()
        banner("FLAGS RE-APPLIED")
        return 0

    existing = trip_count()
    if reload_all or existing is None or existing < MIN_TRIPS:
        if existing:
            log(f"\nrebuilding from the CSVs (Taxi.Trip currently holds "
                f"{existing:,} rows)")
        load(limit=limit)
    else:
        banner("STAGES 1-3 -- skipped")
        log(f"  Taxi.Trip already holds {existing:,} rows. Pass --reload to "
            f"rebuild from the CSVs.")

    path = serve()

    banner("READY")
    log(f"  dashboard:  http://localhost:52773{path}/")
    log(f"  sign in as: _SYSTEM / SYS")
    log("")
    log("  Edits to /src are picked up on the next request -- no restart, and no")
    log("  need to re-run this script unless the schema or the web app changed.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
