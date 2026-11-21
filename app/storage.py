"""SQLite persistence for validated backtest runs."""

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
    evaluated_samples    INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_backtests_created_at
    ON backtests (created_at DESC);
"""

_init_lock = threading.Lock()
_initialized_paths: set[str] = set()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


@contextmanager
def _connect() -> Iterator[sqlite3.Connection]:
    directory = os.path.dirname(os.path.abspath(DB_PATH))
    os.makedirs(directory, exist_ok=True)
    with _init_lock:
        if DB_PATH not in _initialized_paths:
            with sqlite3.connect(DB_PATH) as init:
                init.executescript(_SCHEMA)
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
                evaluated_samples
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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


def _row_to_summary(row: sqlite3.Row) -> dict:
    summary = json.loads(row["summary_json"])
    return {
        "id": row["id"],
        "created_at": row["created_at"],
        "cutoff": row["cutoff"],
        "prediction_filename": row["prediction_filename"],
        "actual_filename": row["actual_filename"],
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
            "actual_csv FROM backtests WHERE id = ?",
            (run_id,),
        ).fetchone()
    if row is None:
        return None
    if kind == "prediction":
        return row["prediction_filename"], row["prediction_csv"]
    return row["actual_filename"], row["actual_csv"]
