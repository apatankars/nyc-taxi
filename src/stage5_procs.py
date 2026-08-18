"""Stage 5a -- expose the re-flagging loop to SQL as a Python function.

The rules are rows, so changing a threshold is an UPDATE -- but re-applying it is
stage 4's work: one INSERT..SELECT per rule, then refresh flag_count/flag_mask,
roughly forty statements. Driven from the client that is forty round trips; wrapped in a
`CREATE FUNCTION ... LANGUAGE PYTHON` it is one call, running inside the IRIS
process and returning a one-line summary. The function imports stage4_flag rather
than restating it, so there is no second implementation to keep in step.

run.py calls this as part of `./run.sh`; on its own it just recreates the
function:

    docker exec -w /src nyc-taxi-iris /usr/irissys/bin/irispython stage5_procs.py

A function rather than a procedure: a scalar function is the only one both modes
can invoke and read a return value from with a plain `SELECT Taxi.apply_rules_now()`.
Procedures need CALL plus driver-specific output handling.
"""

from db import banner, exec_sql, log, scalar, try_exec

NAME = "Taxi.apply_rules_now"

# Compiled verbatim as the function body, so every line starts at column 0 --
# an indented first line earns "unexpected indent" from the SQL compiler.
#
# The sys.path insert is what lets the body reuse the pipeline's own modules:
# /src is the bind mount holding stage4_flag.py, and the embedded interpreter does
# not have it on the path by default.
BODY = [
    "import sys",
    "if '/src' not in sys.path:",
    "    sys.path.insert(0, '/src')",
    "import stage4_flag",
    "try:",
    "    results = stage4_flag.apply_rules()",
    "    updated = stage4_flag.refresh_summaries()",
    "    flagged = sum(n for _, _, n in results)",
    "    return ('applied %d rules, %d flags over %d trips'",
    "            % (len(results), flagged, updated))",
    "except Exception as exc:",
    "    return 'error: %s' % exc",
]


def build():
    banner("STAGE 5a -- SQL function for re-applying rules")

    try_exec(f"DROP FUNCTION {NAME}")
    exec_sql(f"CREATE FUNCTION {NAME}() RETURNS VARCHAR(200)\n"
             f"LANGUAGE PYTHON\n{{\n" + "\n".join(BODY) + "\n}")
    log(f"  created {NAME}()")

    # Call it once (as stage 1 self-tests its UDFs): a function IRIS cannot invoke
    # can return NULL rather than raising, reading as "nothing to do". A
    # successful call also proves the flags are current.
    summary = scalar(f"SELECT {NAME}()")
    if not summary or str(summary).lower().startswith("error"):
        raise RuntimeError(f"{NAME}() returned {summary!r}")
    log(f"  self-test: {summary}")
    return summary


if __name__ == "__main__":
    build()
