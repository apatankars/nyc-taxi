"""The single place this project talks to IRIS.

Two things worth knowing about the driver:

* The package installs as ``intersystems-irispython`` but imports as ``iris``.
* ``iris.connect()`` is DB-API 2.0, so cursors, ``?`` parameter markers and
  ``cursor.description`` all behave the way they do for sqlite3 or psycopg.

``query_df`` is deliberately the main read path. Every analytical query in this
project aggregates *inside* IRIS and returns a handful of rows, so building a
DataFrame from the cursor is cheap and no large result set crosses the wire.
"""

import time
from contextlib import contextmanager
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import iris
import pandas as pd

from .config import IrisConfig


class LoadDataError(RuntimeError):
    """A LOAD DATA statement returned a non-zero %SQLCODE."""


@contextmanager
def connect(cfg: Optional[IrisConfig] = None):
    """Yield a DB-API connection and always close it.

    Commits on clean exit, rolls back on exception. IRIS autocommits DDL, so the
    rollback matters for the data-moving statements, not for CREATE TABLE.
    """
    cfg = cfg or IrisConfig.from_env()
    conn = iris.connect(
        hostname=cfg.host,
        port=cfg.port,
        namespace=cfg.namespace,
        username=cfg.user,
        password=cfg.password,
    )
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def execute(sql: str, params: Sequence[Any] = (), cfg: Optional[IrisConfig] = None) -> int:
    """Run one statement and return its affected-row count (-1 if unreported)."""
    with connect(cfg) as conn:
        cur = conn.cursor()
        try:
            cur.execute(sql, list(params))
            return cur.rowcount
        finally:
            cur.close()


def execute_script(statements: Iterable[str], cfg: Optional[IrisConfig] = None) -> None:
    """Run several statements on one connection, in order.

    Used for DDL batches, where reconnecting per statement would be wasteful and
    where a failure partway through should stop the rest.
    """
    with connect(cfg) as conn:
        cur = conn.cursor()
        try:
            for sql in statements:
                stripped = sql.strip()
                if stripped:
                    cur.execute(stripped)
        finally:
            cur.close()


def query(
    sql: str, params: Sequence[Any] = (), cfg: Optional[IrisConfig] = None
) -> Tuple[List[str], List[tuple]]:
    """Return (column_names, rows) for a SELECT."""
    with connect(cfg) as conn:
        cur = conn.cursor()
        try:
            cur.execute(sql, list(params))
            columns = [d[0] for d in cur.description]
            return columns, cur.fetchall()
        finally:
            cur.close()


def query_df(
    sql: str, params: Sequence[Any] = (), cfg: Optional[IrisConfig] = None
) -> pd.DataFrame:
    """Return a SELECT's result as a DataFrame."""
    columns, rows = query(sql, params, cfg)
    return pd.DataFrame.from_records(rows, columns=columns)


def scalar(sql: str, params: Sequence[Any] = (), cfg: Optional[IrisConfig] = None) -> Any:
    """Return the first column of the first row, or None for an empty result."""
    _, rows = query(sql, params, cfg)
    return rows[0][0] if rows else None


def table_exists(qualified_name: str, cfg: Optional[IrisConfig] = None) -> bool:
    schema, _, table = qualified_name.partition(".")
    found = scalar(
        "SELECT COUNT(*) FROM INFORMATION_SCHEMA.TABLES "
        "WHERE TABLE_SCHEMA = ? AND TABLE_NAME = ?",
        (schema, table),
        cfg,
    )
    return bool(found)


def row_count(qualified_name: str, cfg: Optional[IrisConfig] = None) -> int:
    return int(scalar(f"SELECT COUNT(*) FROM {qualified_name}", (), cfg) or 0)


@contextmanager
def timed(label: str, sink: Optional[Dict[str, float]] = None):
    """Time a block, print the elapsed seconds, and optionally record them.

    Used throughout the pipeline so every stage reports its own cost, and by
    bench.py to collect the IRIS-side vs Python-side numbers.
    """
    start = time.perf_counter()
    try:
        yield
    finally:
        elapsed = time.perf_counter() - start
        print(f"  {label}: {elapsed:.2f}s")
        if sink is not None:
            sink[label] = elapsed


def server_info(cfg: Optional[IrisConfig] = None) -> str:
    """One-line description of what we are connected to. Also a connection test."""
    return str(scalar("SELECT $ZVERSION", (), cfg))


def exec_direct(sql: str, cfg: Optional[IrisConfig] = None) -> Dict[str, Any]:
    """Run one statement through the Native API instead of DB-API.

    This exists for exactly one reason: LOAD DATA's ``USING {...}`` clause.

    The DB-API layer parses ``{`` in statement text as the start of an ODBC
    escape sequence, so ``USING {"from":{"file":{"header":1}}}`` never reaches
    the server intact -- it fails at prepare time with a confusing
    ``Parameter Name error, First value cannot be a digit``. Passing the same
    JSON as a ``?`` parameter fails differently (``%FromJSON`` throws), because
    the clause is read at prepare time and a parameter is not bound yet.

    The Native API sends the statement as a *string argument* to
    ``%SQL.Statement.%ExecDirect``, so no ODBC escape parsing happens and the
    braces survive. Verified identical to running the statement in the IRIS SQL
    shell.

    Returns the statement's %SQLCODE, %ROWCOUNT and %Message. LOAD DATA also
    writes per-row diagnostics to the %SQL_Diag.Result / %SQL_Diag.Message
    tables; load.py reads those separately rather than through this call.
    """
    cfg = cfg or IrisConfig.from_env()
    native = iris.createConnection(
        hostname=cfg.host,
        port=cfg.port,
        namespace=cfg.namespace,
        username=cfg.user,
        password=cfg.password,
    )
    try:
        irispy = iris.createIRIS(native)
        result = irispy.classMethodObject("%SQL.Statement", "%ExecDirect", None, sql)
        info = {
            "sqlcode": result.get("%SQLCODE"),
            "rowcount": result.get("%ROWCOUNT"),
            "message": result.get("%Message"),
        }
    finally:
        native.close()

    if info["sqlcode"] not in (0, 100):
        raise LoadDataError(f"%SQLCODE={info['sqlcode']}: {info['message']}\nSQL: {sql}")
    return info
