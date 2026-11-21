"""Validation and reconciliation for backtests recovered from exported CSVs.

When the database is gone but the two exported result files survive, the
``samples`` (per-sample detail) and ``summary`` (per-horizon aggregate) CSVs can
be used to rebuild a read-only record.  Recovery is deliberately strict:

* every ``(series_id, target_time)`` pair must be unique;
* a prediction may not have been issued after the cutoff or after its target;
* the exported ``lead_seconds``, ``horizon``, ``width`` and ``covered`` values
  must equal what the original export rules would produce;
* the summary is recomputed from the detail rows and compared at the original
  export precision (six decimal places for coverage and mean width).

Any mismatch rejects the whole upload.  The same comparison helpers are used
later, when a candidate prediction/actual pair is submitted: row order and
line-ending differences are ignored because both sides are parsed into
records before being compared.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from datetime import datetime

from .backtest import (
    ValidationError,
    _format_duration,
    _iso,
    _parse_cutoff,
    _parse_number,
    _parse_utc,
    _read_csv,
)

SAMPLE_COLUMNS = (
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
)
SUMMARY_COLUMNS = ("horizon", "sample_count", "covered_count", "coverage", "mean_width")
SUMMARY_PRECISION = 6

_INTEGER_RE = re.compile(r"[+-]?\d+")


@dataclass
class RecoveryResult:
    cutoff: datetime
    samples: list[dict]
    horizons: list[dict]
    misses: list[dict]
    summary_rows: list[dict]
    warnings: list[str]


def _parse_int(value: str, field_name: str, row_no: int) -> int:
    text = (value or "").strip()
    if not text:
        raise ValueError(f"row {row_no}: {field_name} is empty")
    if not _INTEGER_RE.fullmatch(text):
        raise ValueError(f"row {row_no}: {field_name} {value!r} is not an integer")
    return int(text)


def _parse_bool(value: str, field_name: str, row_no: int) -> bool:
    text = (value or "").strip().lower()
    if text == "true":
        return True
    if text == "false":
        return False
    raise ValueError(f"row {row_no}: {field_name} {value!r} must be true or false")


def _round_summary(value: float) -> str:
    return f"{value:.{SUMMARY_PRECISION}f}"


def _summary_signature(row: dict) -> tuple:
    return (
        row["horizon"],
        int(row["sample_count"]),
        int(row["covered_count"]),
        _round_summary(float(row["coverage"])),
        _round_summary(float(row["mean_width"])),
    )


def summarize_samples(samples: list[dict]) -> list[dict]:
    """Group samples by their actual lead time, exactly like the exporter."""
    grouped: dict[int, dict] = {}
    for sample in samples:
        lead = int(sample["lead_seconds"])
        bucket = grouped.setdefault(
            lead,
            {
                "horizon": sample["horizon"],
                "lead_seconds": lead,
                "sample_count": 0,
                "covered_count": 0,
                "width_total": 0.0,
            },
        )
        bucket["sample_count"] += 1
        bucket["covered_count"] += int(bool(sample["covered"]))
        bucket["width_total"] += float(sample["width"])

    rows: list[dict] = []
    for lead in sorted(grouped):
        bucket = grouped[lead]
        count = bucket["sample_count"]
        rows.append(
            {
                "horizon": bucket["horizon"],
                "lead_seconds": lead,
                "sample_count": count,
                "covered_count": bucket["covered_count"],
                "coverage": bucket["covered_count"] / count,
                "mean_width": bucket["width_total"] / count,
            }
        )
    return rows


def _parse_samples(content: bytes, cutoff: datetime) -> list[dict]:
    rows = _read_csv(content, "samples", SAMPLE_COLUMNS)
    if not rows:
        raise ValidationError(["samples CSV contains no data rows"])

    errors: list[str] = []
    samples: list[dict] = []
    seen: set[tuple[str, datetime]] = set()

    for row in rows:
        row_no = int(row["__row__"])
        try:
            series_id = (row.get("series_id") or "").strip()
            if not series_id:
                raise ValueError(f"row {row_no}: series_id is empty")
            issue_time = _parse_utc(row.get("issue_time", ""), "issue_time", row_no)
            target_time = _parse_utc(row.get("target_time", ""), "target_time", row_no)
            horizon = (row.get("horizon") or "").strip()
            if not horizon:
                raise ValueError(f"row {row_no}: horizon is empty")
            lead_seconds = _parse_int(row.get("lead_seconds", ""), "lead_seconds", row_no)
            lower = _parse_number(row.get("lower", ""), "lower", row_no)
            upper = _parse_number(row.get("upper", ""), "upper", row_no)
            width = _parse_number(row.get("width", ""), "width", row_no)
            actual = _parse_number(row.get("actual", ""), "actual", row_no)
            covered = _parse_bool(row.get("covered", ""), "covered", row_no)
        except ValueError as exc:
            errors.append(str(exc))
            continue

        row_errors: list[str] = []
        key = (series_id, target_time)
        if key in seen:
            row_errors.append(
                f"row {row_no}: duplicate sample for series {series_id!r} at target "
                f"{_iso(target_time)}"
            )
        else:
            seen.add(key)

        if issue_time > target_time:
            row_errors.append(
                f"row {row_no}: issue_time {_iso(issue_time)} is after target_time "
                f"{_iso(target_time)}"
            )
        if issue_time > cutoff:
            row_errors.append(
                f"row {row_no}: issue_time {_iso(issue_time)} is after the cutoff "
                f"{_iso(cutoff)}"
            )

        computed_lead = int((target_time - issue_time).total_seconds())
        if lead_seconds != computed_lead:
            row_errors.append(
                f"row {row_no}: lead_seconds is {lead_seconds}, expected "
                f"{computed_lead} from issue_time to target_time"
            )
        expected_horizon = _format_duration(computed_lead)
        if horizon != expected_horizon:
            row_errors.append(
                f"row {row_no}: horizon is {horizon!r}, expected "
                f"{expected_horizon!r} for {computed_lead} seconds"
            )

        if lower > upper:
            row_errors.append(
                f"row {row_no}: lower {lower} is greater than upper {upper}"
            )
        computed_width = upper - lower
        if width != computed_width:
            row_errors.append(
                f"row {row_no}: width is {width!r}, expected {computed_width!r} "
                "from upper - lower"
            )

        computed_covered = lower <= actual <= upper
        if covered != computed_covered:
            row_errors.append(
                f"row {row_no}: covered is {str(covered).lower()}, expected "
                f"{str(computed_covered).lower()} for actual {actual!r} against "
                f"[{lower!r}, {upper!r}]"
            )

        if row_errors:
            errors.extend(row_errors)
            continue

        samples.append(
            {
                "series_id": series_id,
                "issue_time": _iso(issue_time),
                "target_time": _iso(target_time),
                "horizon": expected_horizon,
                "lead_seconds": computed_lead,
                "lower": lower,
                "upper": upper,
                "width": computed_width,
                "actual": actual,
                "covered": computed_covered,
            }
        )

    if errors:
        raise ValidationError(errors)
    return samples


def _parse_summary(content: bytes) -> list[dict]:
    rows = _read_csv(content, "summary", SUMMARY_COLUMNS)
    if not rows:
        raise ValidationError(["summary CSV contains no data rows"])

    errors: list[str] = []
    parsed: list[dict] = []
    for row in rows:
        row_no = int(row["__row__"])
        try:
            horizon = (row.get("horizon") or "").strip()
            if not horizon:
                raise ValueError(f"row {row_no}: horizon is empty")
            sample_count = _parse_int(
                row.get("sample_count", ""), "sample_count", row_no
            )
            covered_count = _parse_int(
                row.get("covered_count", ""), "covered_count", row_no
            )
            coverage = _parse_number(row.get("coverage", ""), "coverage", row_no)
            mean_width = _parse_number(
                row.get("mean_width", ""), "mean_width", row_no
            )
        except ValueError as exc:
            errors.append(str(exc))
            continue

        if sample_count < 0:
            errors.append(f"row {row_no}: sample_count must not be negative")
            continue
        if covered_count < 0 or covered_count > sample_count:
            errors.append(
                f"row {row_no}: covered_count {covered_count} is outside "
                f"0..{sample_count}"
            )
            continue
        if coverage < 0 or coverage > 1:
            errors.append(f"row {row_no}: coverage must be between 0 and 1")
            continue

        parsed.append(
            {
                "horizon": horizon,
                "sample_count": sample_count,
                "covered_count": covered_count,
                "coverage": coverage,
                "mean_width": mean_width,
            }
        )

    if errors:
        raise ValidationError(errors)
    return parsed


def _summary_values(signature: tuple) -> str:
    return (
        f"sample_count={signature[1]}, covered_count={signature[2]}, "
        f"coverage={signature[3]}, mean_width={signature[4]}"
    )


def compare_summary_rows(
    reference_rows: list[dict],
    candidate_rows: list[dict],
    *,
    reference_label: str = "recovered summary",
    candidate_label: str = "recomputed summary",
) -> list[str]:
    """Compare summary rows, ignoring row order and rounding to export precision."""
    reference = Counter(_summary_signature(row) for row in reference_rows)
    candidate = Counter(_summary_signature(row) for row in candidate_rows)
    if reference == candidate:
        return []

    errors: list[str] = []
    horizons = sorted(
        {row["horizon"] for row in reference_rows}
        | {row["horizon"] for row in candidate_rows}
    )
    for horizon in horizons:
        expected = [
            row for row in reference_rows if row["horizon"] == horizon
        ]
        actual = [
            row for row in candidate_rows if row["horizon"] == horizon
        ]
        if Counter(map(_summary_signature, expected)) == Counter(
            map(_summary_signature, actual)
        ):
            continue
        if len(expected) != len(actual):
            errors.append(
                f"summary horizon {horizon!r}: {reference_label} has "
                f"{len(expected)} row(s), {candidate_label} has {len(actual)} row(s)"
            )
        if len(expected) == 1 and len(actual) == 1:
            # A single row per horizon is the normal case: report the exact
            # fields that disagree instead of a vague missing/extra row.
            ref, cand = expected[0], actual[0]
            for field in ("sample_count", "covered_count", "coverage", "mean_width"):
                if field in ("coverage", "mean_width"):
                    ref_value = _round_summary(float(ref[field]))
                    cand_value = _round_summary(float(cand[field]))
                else:
                    ref_value = int(ref[field])
                    cand_value = int(cand[field])
                if ref_value != cand_value:
                    errors.append(
                        f"summary horizon {horizon!r}: {field} is {cand_value} in "
                        f"{candidate_label}, expected {ref_value} from "
                        f"{reference_label}"
                    )
            continue

        expected_counter = Counter(map(_summary_signature, expected))
        actual_counter = Counter(map(_summary_signature, actual))
        for signature, count in (expected_counter - actual_counter).items():
            for _ in range(count):
                errors.append(
                    f"summary horizon {horizon!r}: {candidate_label} is missing "
                    f"a row present in {reference_label} "
                    f"({_summary_values(signature)})"
                )
        for signature, count in (actual_counter - expected_counter).items():
            for _ in range(count):
                errors.append(
                    f"summary horizon {horizon!r}: {candidate_label} has an extra "
                    f"row not present in {reference_label} "
                    f"({_summary_values(signature)})"
                )
    return errors


def compare_samples(
    reference_samples: list[dict],
    candidate_samples: list[dict],
    *,
    reference_label: str = "recovered detail",
    candidate_label: str = "recomputed detail",
) -> list[str]:
    """Compare per-sample detail rows, ignoring row order and line endings."""
    reference = {
        (sample["series_id"], sample["target_time"]): sample
        for sample in reference_samples
    }
    candidate = {
        (sample["series_id"], sample["target_time"]): sample
        for sample in candidate_samples
    }
    errors: list[str] = []

    for key in sorted(set(reference) | set(candidate)):
        series_id, target_time = key
        if key not in candidate:
            errors.append(
                f"detail for series {series_id!r} target {target_time}: missing from "
                f"{candidate_label} but present in {reference_label}"
            )
            continue
        if key not in reference:
            errors.append(
                f"detail for series {series_id!r} target {target_time}: extra row in "
                f"{candidate_label} not present in {reference_label}"
            )
            continue

        ref, cand = reference[key], candidate[key]
        for field in (
            "issue_time",
            "horizon",
            "lead_seconds",
            "lower",
            "upper",
            "width",
            "actual",
            "covered",
        ):
            if ref[field] != cand[field]:
                errors.append(
                    f"detail for series {series_id!r} target {target_time}: "
                    f"{field} is {cand[field]!r} in {candidate_label}, expected "
                    f"{ref[field]!r} from {reference_label}"
                )

    return errors


def run_recovery(
    *,
    samples_csv: bytes,
    summary_csv: bytes,
    cutoff_raw: str,
) -> RecoveryResult:
    cutoff = _parse_cutoff(cutoff_raw)
    samples = _parse_samples(samples_csv, cutoff)
    # Canonical order, matching the exporter, so the recovered run does not
    # depend on the row order of the uploaded file.
    samples.sort(key=lambda sample: (sample["series_id"], sample["target_time"]))
    horizons = summarize_samples(samples)
    summary_rows = _parse_summary(summary_csv)

    mismatches = compare_summary_rows(
        summary_rows,
        horizons,
        reference_label="summary CSV",
        candidate_label="detail recomputation",
    )
    if mismatches:
        raise ValidationError(
            ["summary CSV does not match the recomputed detail:"] + mismatches
        )

    misses = [sample for sample in samples if not sample["covered"]]
    return RecoveryResult(
        cutoff=cutoff,
        samples=samples,
        horizons=horizons,
        misses=misses,
        summary_rows=summary_rows,
        warnings=["该记录由导出的明细与汇总 CSV 恢复，仅保存结果，原始输入未知。"],
    )


def verify_against_recovered(
    *,
    recovered_samples: list[dict],
    recovered_summary_rows: list[dict],
    computed_samples: list[dict],
    computed_horizons: list[dict],
) -> list[str]:
    """Compare a freshly computed backtest with the preserved exports.

    Both the per-sample detail and the per-horizon summary must agree; row order
    is irrelevant and summary floats are compared at export precision.
    """
    errors = compare_samples(
        recovered_samples,
        computed_samples,
        reference_label="recovered detail",
        candidate_label="recomputed detail",
    )
    errors += compare_summary_rows(
        recovered_summary_rows,
        computed_horizons,
        reference_label="recovered summary",
        candidate_label="recomputed summary",
    )
    return errors
