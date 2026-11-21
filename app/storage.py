"""SQLite persistence for validated backtest runs.

Two kinds of runs are stored:

``full``
    A normal upload.  The original prediction and actual CSVs are kept and can
    be downloaded again.
``recovered``
    A record rebuilt from exported *result* CSVs after the database was lost.
    It starts with nothing but its detail and summary and no inputs
    (``input_status = 'awaiting'``).  A matching prediction/actual pair can be
    registered atomically later, which fills in the inputs without changing the
    run id or any of the recovered results.
"""

from __future__ import annotations

import csv
import io
import json
import os
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterator

DB_PATH = os.path.abspath(
    os.environ.get("BACKTEST_DB_PATH", "data/backtest.db")
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS backtests (
    id                   TEXT PRIMARY KEY,
    created_at           TEXT NOT NULL,
    cutoff               TEXT NOT NULL,
    prediction_filename  TEXT NOT NULL,
    actual_filename      TEXT NOT NULL,
    prediction_csv       BLOB NOT NULL,
    actual_csv           BLOB NOT NULL,
    summary_json         TEXT NOT NULL,
    prediction_rows      INTEGER NOT NULL,
    actual_rows          INTEGER NOT NULL,
    selected_predictions INTEGER NOT NULL,
    evaluated_samples    INTEGER NOT NULL,
    kind                 TEXT NOT NULL DEFAULT 'full',
    input_status         TEXT NOT NULL DEFAULT 'complete',
    recovered_samples_csv BLOB,
    recovered_summary_csv BLOB
);
CREATE INDEX IF NOT EXISTS idx_backtests_created_at
    ON backtests (created_at DESC);
"""

# Columns introduced after the first release.  Existing databases are migrated
# in place so a volume created by an older build keeps working.
_ADDED_COLUMNS = (
    ("kind", "TEXT NOT NULL DEFAULT 'full'"),
    ("input_status", "TEXT NOT NULL DEFAULT 'complete'"),
    ("recovered_samples_csv", "BLOB"),
    ("recovered_summary_csv", "BLOB"),
)

_init_lock = threading.Lock()
_initialized_paths: set[str] = set()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _migrate(conn: sqlite3.Connection) -> None:
    existing = {
        row[1] for row in conn.execute("PRAGMA table_info(backtests)").fetchall()
    }
    for name, definition in _ADDED_COLUMNS:
        if name not in existing:
            conn.execute(f"ALTER TABLE backtests ADD COLUMN {name} {definition}")


@contextmanager
def _connect() -> Iterator[sqlite3.Connection]:
    directory = os.path.dirname(os.path.abspath(DB_PATH))
    os.makedirs(directory, exist_ok=True)
    with _init_lock:
        if DB_PATH not in _initialized_paths:
            with sqlite3.connect(DB_PATH) as init:
                init.executescript(_SCHEMA)
                _migrate(init)
                init.execute("PRAGMA journal_mode=WAL")
            _initialized_paths.add(DB_PATH)
    connection = sqlite3.connect(DB_PATH, timeout=15)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def save_backtest(
    *,
    cutoff: str,
    prediction_filename: str,
    actual_filename: str,
    prediction_csv: bytes,
    actual_csv: bytes,
    summary: dict,
    submitted_rows: dict,
) -> str:
    run_id = uuid.uuid4().hex
    summary_json = json.dumps(summary, ensure_ascii=False)
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO backtests (
                id, created_at, cutoff, prediction_filename, actual_filename,
                prediction_csv, actual_csv, summary_json,
                prediction_rows, actual_rows, selected_predictions,
                evaluated_samples, kind, input_status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'full', 'complete')
            """,
            (
                run_id,
                _now_iso(),
                cutoff,
                prediction_filename,
                actual_filename,
                prediction_csv,
                actual_csv,
                summary_json,
                submitted_rows["prediction_rows"],
                submitted_rows["actual_rows"],
                submitted_rows["selected_predictions"],
                submitted_rows["evaluated_samples"],
            ),
        )
    return run_id


def save_recovered_backtest(
    *,
    cutoff: str,
    samples_csv: bytes,
    summary_csv: bytes,
    summary: dict,
) -> str:
    """Store a results-only record; no source inputs exist yet."""
    run_id = uuid.uuid4().hex
    summary_json = json.dumps(summary, ensure_ascii=False)
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO backtests (
                id, created_at, cutoff, prediction_filename, actual_filename,
                prediction_csv, actual_csv, summary_json,
                prediction_rows, actual_rows, selected_predictions,
                evaluated_samples, kind, input_status,
                recovered_samples_csv, recovered_summary_csv
            ) VALUES (?, ?, ?, '', '', ?, ?, ?, 0, 0, ?, ?, 'recovered', 'awaiting', ?, ?)
            """,
            (
                run_id,
                _now_iso(),
                cutoff,
                b"",
                b"",
                summary_json,
                summary["submitted_rows"]["selected_predictions"],
                summary["submitted_rows"]["evaluated_samples"],
                samples_csv,
                summary_csv,
            ),
        )
    return run_id


def register_inputs(
    run_id: str,
    *,
    prediction_filename: str,
    actual_filename: str,
    prediction_csv: bytes,
    actual_csv: bytes,
) -> bool:
    """Attach a verified input pair to a recovered run.

    The conditional ``UPDATE`` makes the transition atomic: concurrent
    submissions race on the same row, and exactly one of them can move
    ``awaiting`` to ``complete``.  The loser changes nothing.
    """
    with _connect() as conn:
        cursor = conn.execute(
            """
            UPDATE backtests
               SET prediction_filename = ?, actual_filename = ?,
                   prediction_csv = ?, actual_csv = ?,
                   input_status = 'complete'
             WHERE id = ? AND kind = 'recovered' AND input_status = 'awaiting'
            """,
            (
                prediction_filename,
                actual_filename,
                prediction_csv,
                actual_csv,
                run_id,
            ),
        )
        return cursor.rowcount == 1


def _row_to_summary(row: sqlite3.Row) -> dict:
    summary = json.loads(row["summary_json"])
    kind = row["kind"]
    input_status = row["input_status"]
    return {
        "id": row["id"],
        "created_at": row["created_at"],
        "cutoff": row["cutoff"],
        "prediction_filename": row["prediction_filename"],
        "actual_filename": row["actual_filename"],
        "kind": kind,
        "input_status": input_status,
        "has_inputs": input_status == "complete",
        "input_label": "复算输入" if kind == "recovered" and input_status == "complete" else None,
        **summary,
    }


def list_backtests(limit: int = 20) -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM backtests ORDER BY created_at DESC, rowid DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [_row_to_summary(row) for row in rows]


def get_backtest(run_id: str) -> dict | None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM backtests WHERE id = ?", (run_id,)
        ).fetchone()
    return _row_to_summary(row) if row else None


def _csv_bytes(header: list[str], rows: list[list]) -> bytes:
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(header)
    writer.writerows(rows)
    return buffer.getvalue().encode("utf-8")


def render_samples_csv(run: dict) -> bytes:
    header = [
        "series_id",
        "issue_time",
        "target_time",
        "horizon",
        "lead_seconds",
        "lower",
        "upper",
        "width",
        "actual",
        "covered",
    ]
    rows = [
        [
            sample["series_id"],
            sample["issue_time"],
            sample["target_time"],
            sample["horizon"],
            sample["lead_seconds"],
            sample["lower"],
            sample["upper"],
            sample["width"],
            sample["actual"],
            "true" if sample["covered"] else "false",
        ]
        for sample in run["samples"]
    ]
    return _csv_bytes(header, rows)


def render_summary_csv(run: dict) -> bytes:
    header = ["horizon", "sample_count", "covered_count", "coverage", "mean_width"]
    rows = [
        [
            horizon["horizon"],
            horizon["sample_count"],
            horizon["covered_count"],
            f"{horizon['coverage']:.6f}",
            f"{horizon['mean_width']:.6f}",
        ]
        for horizon in run["horizons"]
    ]
    return _csv_bytes(header, rows)


def render_input_csv(run_id: str, kind: str) -> tuple[str, bytes] | None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT prediction_filename, actual_filename, prediction_csv, "
            "actual_csv, input_status FROM backtests WHERE id = ?",
            (run_id,),
        ).fetchone()
    if row is None or row["input_status"] != "complete":
        return None
    if kind == "prediction":
        return row["prediction_filename"], row["prediction_csv"]
    return row["actual_filename"], row["actual_csv"]


def render_result_csv(run_id: str, kind: str) -> bytes | None:
    """Return the exact exported result file a recovered run was built from.

    Recovery keeps the uploaded detail and summary bytes so a download returns
    the file the engineer verified, not a re-serialisation of it.  Normal runs
    have no stored export and fall back to rendering from the saved rows.
    """
    if kind not in ("samples", "summary"):
        return None
    with _connect() as conn:
        row = conn.execute(
            "SELECT kind, recovered_samples_csv, recovered_summary_csv "
            "FROM backtests WHERE id = ?",
            (run_id,),
        ).fetchone()
    if row is None or row["kind"] != "recovered":
        return None
    return row["recovered_samples_csv" if kind == "samples" else "recovered_summary_csv"]
