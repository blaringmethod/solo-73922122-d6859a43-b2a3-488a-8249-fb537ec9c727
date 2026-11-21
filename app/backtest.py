"""Core validation and aggregation logic for the prediction-interval backtest.

Semantics
---------
* Every timestamp is interpreted as UTC. Offsets are normalised to UTC and
  naive timestamps are treated as UTC, never as the host's local time.
* For each ``(series_id, target_time)`` the *latest prediction issued at or
  before the evaluation cutoff* is selected. Later vintages are ignored, so a
  past forecast stays usable even when newer forecasts exist.
* A horizon is the *actual* elapsed time from ``issue_time`` to
  ``target_time`` (never a nominal value taken from a filename or extra
  column). Samples with the identical lead time share one horizon, labelled
  with a compact duration such as ``30m``, ``1h``, ``1h 30m`` or ``1d 2h``.
* Validation is all-or-nothing. Duplicate rows, inverted bounds
  (``lower > upper``), inverted timing (``issue_time > target_time``), and any
  target time that lacks either a cutoff-available prediction or an actual
  value reject the entire batch.
"""

from __future__ import annotations

import csv
import io
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone

PREDICTION_COLUMNS = ("series_id", "issue_time", "target_time", "lower", "upper")
ACTUAL_COLUMNS = ("series_id", "target_time", "actual")
MAX_REPORTED_ERRORS = 100


class ValidationError(Exception):
    """Raised when an upload cannot be turned into a trustworthy backtest."""

    def __init__(self, errors: list[str]):
        self.errors = _cap_errors(errors)
        self.total_errors = len(errors)
        super().__init__("; ".join(self.errors))


def _cap_errors(errors: list[str]) -> list[str]:
    if len(errors) <= MAX_REPORTED_ERRORS:
        return list(errors)
    shown = errors[:MAX_REPORTED_ERRORS]
    shown.append(f"... and {len(errors) - MAX_REPORTED_ERRORS} more error(s)")
    return shown


@dataclass(frozen=True)
class Interval:
    series_id: str
    issue_time: datetime
    target_time: datetime
    lower: float
    upper: float


@dataclass
class HorizonStats:
    horizon: str
    sample_count: int = 0
    covered_count: int = 0
    width_total: float = 0.0

    @property
    def coverage(self) -> float:
        return self.covered_count / self.sample_count if self.sample_count else 0.0

    @property
    def mean_width(self) -> float:
        return self.width_total / self.sample_count if self.sample_count else 0.0


@dataclass
class BacktestResult:
    cutoff: datetime
    intervals: list[dict] = field(default_factory=list)
    horizons: list[dict] = field(default_factory=list)
    misses: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    submitted_rows: dict[str, int] = field(default_factory=dict)


def _parse_utc(value: str, field_name: str, row_no: int) -> datetime:
    text = (value or "").strip()
    if not text:
        raise ValueError(f"row {row_no}: {field_name} is empty")
    normalized = text[:-1] + "+00:00" if text.endswith(("Z", "z")) else text
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValueError(
            f"row {row_no}: {field_name} {value!r} is not a valid ISO-8601 timestamp"
        ) from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _parse_cutoff(value: str) -> datetime:
    if not value or not value.strip():
        raise ValidationError(["evaluation cutoff time is required"])
    try:
        return _parse_utc(value, "cutoff", 0)
    except ValueError as exc:
        raise ValidationError([f"invalid evaluation cutoff: {exc}"]) from exc


def _parse_number(value: str, field_name: str, row_no: int) -> float:
    text = (value or "").strip()
    if not text:
        raise ValueError(f"row {row_no}: {field_name} is empty")
    try:
        number = float(text)
    except ValueError as exc:
        raise ValueError(
            f"row {row_no}: {field_name} {value!r} is not a number"
        ) from exc
    if not math.isfinite(number):
        raise ValueError(f"row {row_no}: {field_name} must be finite")
    return number


def _read_csv(content: bytes, kind: str, required: tuple[str, ...]) -> list[dict]:
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValidationError([f"{kind} CSV must be UTF-8 encoded"]) from exc
    if not text.strip():
        raise ValidationError([f"{kind} CSV is empty"])
    reader = csv.DictReader(io.StringIO(text, newline=""))
    if reader.fieldnames is None:
        raise ValidationError([f"{kind} CSV has no header row"])
    normalized_fields = [(name or "").strip() for name in reader.fieldnames]
    missing = [name for name in required if name not in normalized_fields]
    if missing:
        raise ValidationError(
            [f"{kind} CSV is missing required column(s): {', '.join(missing)}"]
        )
    rows: list[dict] = []
    for row_no, raw in enumerate(reader, start=2):
        normalized = {
            (key or "").strip(): (value if value is not None else "")
            for key, value in raw.items()
        }
        if not any(str(value).strip() for value in normalized.values()):
            continue  # tolerate blank lines
        rows.append({"__row__": row_no, **normalized})
    return rows


def _format_duration(seconds: int) -> str:
    days, rem = divmod(max(0, seconds), 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    parts: list[str] = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    if secs and not days:
        parts.append(f"{secs}s")
    return " ".join(parts) or "0s"


def _iso(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def run_backtest(
    *,
    prediction_csv: bytes,
    actual_csv: bytes,
    cutoff_raw: str,
) -> BacktestResult:
    cutoff = _parse_cutoff(cutoff_raw)
    pred_rows = _read_csv(prediction_csv, "prediction", PREDICTION_COLUMNS)
    actual_rows = _read_csv(actual_csv, "actual", ACTUAL_COLUMNS)

    errors: list[str] = []
    warnings: list[str] = []
    if not pred_rows:
        errors.append("prediction CSV contains no data rows")
    if not actual_rows:
        errors.append("actual CSV contains no data rows")

    # ---- parse and validate predictions -----------------------------------
    selected: dict[tuple[str, datetime], Interval] = {}
    seen_predictions: dict[tuple[str, datetime, datetime], Interval] = {}
    all_prediction_keys: set[tuple[str, datetime]] = set()
    ignored_after_cutoff = 0

    for row in pred_rows:
        row_no = int(row["__row__"])
        try:
            series_id = (row.get("series_id") or "").strip()
            if not series_id:
                raise ValueError(f"row {row_no}: series_id is empty")
            issue_time = _parse_utc(row.get("issue_time", ""), "issue_time", row_no)
            target_time = _parse_utc(row.get("target_time", ""), "target_time", row_no)
            lower = _parse_number(row.get("lower", ""), "lower", row_no)
            upper = _parse_number(row.get("upper", ""), "upper", row_no)
        except ValueError as exc:
            errors.append(str(exc))
            continue

        if issue_time > target_time:
            errors.append(
                f"row {row_no}: issue_time {_iso(issue_time)} is after target_time "
                f"{_iso(target_time)} (reversed interval)"
            )
            continue
        if lower > upper:
            errors.append(
                f"row {row_no}: lower {lower} is greater than upper {upper} "
                "(reversed interval)"
            )
            continue

        interval = Interval(series_id, issue_time, target_time, lower, upper)
        identity = (series_id, issue_time, target_time)
        previous = seen_predictions.get(identity)
        if previous is not None:
            if (previous.lower, previous.upper) == (lower, upper):
                errors.append(
                    f"row {row_no}: duplicate prediction for series {series_id!r} "
                    f"issued {_iso(issue_time)} for target {_iso(target_time)}"
                )
            else:
                errors.append(
                    f"row {row_no}: conflicting duplicate prediction for series "
                    f"{series_id!r} issued {_iso(issue_time)} for target "
                    f"{_iso(target_time)}"
                )
            continue
        seen_predictions[identity] = interval
        all_prediction_keys.add((series_id, target_time))

        if issue_time > cutoff:
            ignored_after_cutoff += 1
            continue
        key = (series_id, target_time)
        current = selected.get(key)
        if current is None or issue_time > current.issue_time:
            selected[key] = interval

    # ---- parse and validate actuals ---------------------------------------
    actuals: dict[tuple[str, datetime], float] = {}
    for row in actual_rows:
        row_no = int(row["__row__"])
        try:
            series_id = (row.get("series_id") or "").strip()
            if not series_id:
                raise ValueError(f"row {row_no}: series_id is empty")
            target_time = _parse_utc(row.get("target_time", ""), "target_time", row_no)
            actual = _parse_number(row.get("actual", ""), "actual", row_no)
        except ValueError as exc:
            errors.append(str(exc))
            continue
        key = (series_id, target_time)
        if key in actuals:
            errors.append(
                f"row {row_no}: duplicate actual value for series {series_id!r} "
                f"at target {_iso(target_time)}"
            )
            continue
        actuals[key] = actual

    # Row-level problems make the coverage check meaningless; report them first.
    if errors:
        raise ValidationError(errors)

    # ---- atomic completeness check over every target in the batch ---------
    all_targets = all_prediction_keys | set(actuals)
    for series_id, target_time in sorted(all_targets, key=lambda k: (k[0], k[1])):
        key = (series_id, target_time)
        if key not in selected:
            errors.append(
                f"series {series_id!r} target {_iso(target_time)} has no prediction "
                f"issued at or before the cutoff {_iso(cutoff)}"
            )
        if key not in actuals:
            errors.append(
                f"series {series_id!r} target {_iso(target_time)} has no actual value"
            )
    if errors:
        raise ValidationError(errors)

    if ignored_after_cutoff:
        warnings.append(
            f"{ignored_after_cutoff} prediction row(s) were issued after the cutoff "
            "and were not eligible for selection"
        )

    # ---- aggregate ---------------------------------------------------------
    by_horizon: dict[int, HorizonStats] = {}
    samples: list[dict] = []
    misses: list[dict] = []

    for key in sorted(selected, key=lambda k: (k[0], k[1])):
        interval = selected[key]
        actual = actuals[key]
        lead_seconds = int((interval.target_time - interval.issue_time).total_seconds())
        horizon = _format_duration(lead_seconds)
        width = interval.upper - interval.lower
        covered = interval.lower <= actual <= interval.upper

        stats = by_horizon.setdefault(lead_seconds, HorizonStats(horizon))
        stats.sample_count += 1
        stats.covered_count += int(covered)
        stats.width_total += width

        row = {
            "series_id": interval.series_id,
            "issue_time": _iso(interval.issue_time),
            "target_time": _iso(interval.target_time),
            "horizon": horizon,
            "lead_seconds": lead_seconds,
            "lower": interval.lower,
            "upper": interval.upper,
            "width": width,
            "actual": actual,
            "covered": covered,
        }
        samples.append(row)
        if not covered:
            misses.append(row)

    horizon_rows = []
    for lead_seconds in sorted(by_horizon):
        stats = by_horizon[lead_seconds]
        horizon_rows.append(
            {
                "horizon": stats.horizon,
                "lead_seconds": lead_seconds,
                "sample_count": stats.sample_count,
                "covered_count": stats.covered_count,
                "coverage": stats.coverage,
                "mean_width": stats.mean_width,
            }
        )

    return BacktestResult(
        cutoff=cutoff,
        intervals=samples,
        horizons=horizon_rows,
        misses=misses,
        warnings=warnings,
        submitted_rows={
            "prediction_rows": len(pred_rows),
            "actual_rows": len(actual_rows),
            "selected_predictions": len(selected),
            "evaluated_samples": len(samples),
        },
    )
