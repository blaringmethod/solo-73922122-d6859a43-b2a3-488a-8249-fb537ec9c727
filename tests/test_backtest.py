import os
import tempfile

import pytest

# Isolate the SQLite file before importing the application.
_TMP = tempfile.mkdtemp(prefix="backtest-tests-")
os.environ["BACKTEST_DB_PATH"] = os.path.join(_TMP, "test.db")

from fastapi.testclient import TestClient  # noqa: E402

from app.backtest import ValidationError, run_backtest  # noqa: E402
from app.main import app  # noqa: E402


PRED_HEADER = "series_id,issue_time,target_time,lower,upper\n"
ACT_HEADER = "series_id,target_time,actual\n"


def pred(*rows: str) -> bytes:
    return (PRED_HEADER + "\n".join(rows) + "\n").encode()


def act(*rows: str) -> bytes:
    return (ACT_HEADER + "\n".join(rows) + "\n").encode()


CUTOFF = "2026-11-20T12:00:00Z"


# ---------------------------------------------------------------- core logic


def test_selects_latest_prediction_available_at_cutoff():
    result = run_backtest(
        prediction_csv=pred(
            "a,2026-11-20T10:00:00Z,2026-11-20T13:00:00Z,1,2",
            "a,2026-11-20T11:30:00Z,2026-11-20T13:00:00Z,1.5,2.5",
            "a,2026-11-20T12:30:00Z,2026-11-20T13:00:00Z,9,10",  # after cutoff
        ),
        actual_csv=act("a,2026-11-20T13:00:00Z,2.0"),
        cutoff_raw=CUTOFF,
    )
    assert len(result.intervals) == 1
    sample = result.intervals[0]
    assert sample["issue_time"] == "2026-11-20T11:30:00Z"
    assert sample["lower"] == 1.5 and sample["upper"] == 2.5
    assert sample["covered"] is True
    assert sample["horizon"] == "1h 30m"
    assert sample["lead_seconds"] == 5400
    assert result.warnings and "after the cutoff" in result.warnings[0]


def test_prediction_exactly_at_cutoff_is_available():
    result = run_backtest(
        prediction_csv=pred("a,2026-11-20T12:00:00Z,2026-11-20T12:30:00Z,1,2"),
        actual_csv=act("a,2026-11-20T12:30:00Z,2"),
        cutoff_raw=CUTOFF,
    )
    assert result.intervals[0]["issue_time"] == "2026-11-20T12:00:00Z"


def test_latest_prediction_after_cutoff_falls_back_to_earlier_one():
    result = run_backtest(
        prediction_csv=pred(
            "a,2026-11-20T09:00:00Z,2026-11-20T13:00:00Z,1,2",
            "a,2026-11-20T12:30:00Z,2026-11-20T13:00:00Z,5,6",
        ),
        actual_csv=act("a,2026-11-20T13:00:00Z,1.5"),
        cutoff_raw=CUTOFF,
    )
    assert result.intervals[0]["issue_time"] == "2026-11-20T09:00:00Z"


def test_horizons_group_by_actual_lead_time_and_report_coverage_and_width():
    result = run_backtest(
        prediction_csv=pred(
            "a,2026-11-20T11:00:00Z,2026-11-20T12:00:00Z,0,2",  # 1h, covered
            "b,2026-11-20T11:00:00Z,2026-11-20T12:00:00Z,0,1",  # 1h, miss
            "a,2026-11-20T09:00:00Z,2026-11-20T13:00:00Z,0,4",  # 4h, covered
        ),
        actual_csv=act(
            "a,2026-11-20T12:00:00Z,1",
            "b,2026-11-20T12:00:00Z,3",
            "a,2026-11-20T13:00:00Z,2",
        ),
        cutoff_raw=CUTOFF,
    )
    by_horizon = {h["horizon"]: h for h in result.horizons}
    assert set(by_horizon) == {"1h", "4h"}
    assert by_horizon["1h"]["sample_count"] == 2
    assert by_horizon["1h"]["covered_count"] == 1
    assert by_horizon["1h"]["coverage"] == 0.5
    assert by_horizon["1h"]["mean_width"] == 1.5  # (2 + 1) / 2
    assert by_horizon["4h"]["coverage"] == 1.0
    assert by_horizon["4h"]["mean_width"] == 4.0
    assert [m["series_id"] for m in result.misses] == ["b"]
    assert by_horizon["1h"]["lead_seconds"] == 3600


@pytest.mark.parametrize(
    "rows, needle",
    [
        (["a,2026-11-20T10:00:00Z,2026-11-20T13:00:00Z,2,1"], "reversed interval"),
        (["a,2026-11-20T14:00:00Z,2026-11-20T13:00:00Z,1,2"], "reversed interval"),
    ],
)
def test_rejects_inverted_rows(rows, needle):
    with pytest.raises(ValidationError) as excinfo:
        run_backtest(
            prediction_csv=pred(*rows),
            actual_csv=act("a,2026-11-20T13:00:00Z,1.5"),
            cutoff_raw=CUTOFF,
        )
    assert needle in str(excinfo.value)


def test_rejects_duplicate_prediction_rows():
    duplicate = "a,2026-11-20T10:00:00Z,2026-11-20T13:00:00Z,1,2"
    with pytest.raises(ValidationError) as excinfo:
        run_backtest(
            prediction_csv=pred(duplicate, duplicate),
            actual_csv=act("a,2026-11-20T13:00:00Z,1.5"),
            cutoff_raw=CUTOFF,
        )
    assert "duplicate prediction" in str(excinfo.value)


def test_rejects_duplicate_actual_rows():
    with pytest.raises(ValidationError) as excinfo:
        run_backtest(
            prediction_csv=pred("a,2026-11-20T10:00:00Z,2026-11-20T13:00:00Z,1,2"),
            actual_csv=act(
                "a,2026-11-20T13:00:00Z,1.5", "a,2026-11-20T13:00:00Z,1.6"
            ),
            cutoff_raw=CUTOFF,
        )
    assert "duplicate actual" in str(excinfo.value)


def test_rejects_batch_when_actual_missing_for_some_target():
    with pytest.raises(ValidationError) as excinfo:
        run_backtest(
            prediction_csv=pred(
                "a,2026-11-20T10:00:00Z,2026-11-20T13:00:00Z,1,2",
                "b,2026-11-20T10:00:00Z,2026-11-20T13:00:00Z,1,2",
            ),
            actual_csv=act("a,2026-11-20T13:00:00Z,1.5"),
            cutoff_raw=CUTOFF,
        )
    assert "no actual value" in str(excinfo.value)
    assert "'b'" in str(excinfo.value)


def test_rejects_batch_when_prediction_only_exists_after_cutoff():
    with pytest.raises(ValidationError) as excinfo:
        run_backtest(
            prediction_csv=pred("a,2026-11-20T12:30:00Z,2026-11-20T13:00:00Z,1,2"),
            actual_csv=act("a,2026-11-20T13:00:00Z,1.5"),
            cutoff_raw=CUTOFF,
        )
    assert "no prediction issued at or before the cutoff" in str(excinfo.value)


def test_rejects_series_only_present_in_actuals():
    with pytest.raises(ValidationError) as excinfo:
        run_backtest(
            prediction_csv=pred("a,2026-11-20T10:00:00Z,2026-11-20T13:00:00Z,1,2"),
            actual_csv=act(
                "a,2026-11-20T13:00:00Z,1.5", "c,2026-11-20T13:00:00Z,9"
            ),
            cutoff_raw=CUTOFF,
        )
    assert "'c'" in str(excinfo.value)


def test_naive_timestamps_are_treated_as_utc():
    result = run_backtest(
        prediction_csv=pred("a,2026-11-20 10:00:00,2026-11-20 13:00:00,1,2"),
        actual_csv=act("a,2026-11-20 13:00:00,1.5"),
        cutoff_raw="2026-11-20T12:00:00",
    )
    assert result.cutoff.isoformat() == "2026-11-20T12:00:00+00:00"
    assert result.intervals[0]["issue_time"] == "2026-11-20T10:00:00Z"


def test_offsets_are_normalised_to_utc_and_short_horizon_buckets():
    result = run_backtest(
        prediction_csv=pred("a,2026-11-20T20:00:00+08:00,2026-11-20T12:30:00Z,1,2"),
        actual_csv=act("a,2026-11-20T12:30:00Z,2"),
        cutoff_raw="2026-11-20T12:00:00Z",
    )
    sample = result.intervals[0]
    assert sample["issue_time"] == "2026-11-20T12:00:00Z"
    assert sample["target_time"] == "2026-11-20T12:30:00Z"
    assert sample["horizon"] == "30m"
    assert sample["covered"] is True  # upper bound is inclusive


def test_equal_bounds_are_a_valid_degenerate_interval():
    result = run_backtest(
        prediction_csv=pred("a,2026-11-20T10:00:00Z,2026-11-20T13:00:00Z,2,2"),
        actual_csv=act("a,2026-11-20T13:00:00Z,2"),
        cutoff_raw=CUTOFF,
    )
    assert result.intervals[0]["covered"] is True
    assert result.intervals[0]["width"] == 0.0


def test_rejects_missing_columns():
    with pytest.raises(ValidationError) as excinfo:
        run_backtest(
            prediction_csv=b"series_id,issue_time,target_time,lower\n",
            actual_csv=act("a,2026-11-20T13:00:00Z,1.5"),
            cutoff_raw=CUTOFF,
        )
    assert "missing required column" in str(excinfo.value)


def test_rejects_non_finite_numbers():
    with pytest.raises(ValidationError) as excinfo:
        run_backtest(
            prediction_csv=pred("a,2026-11-20T10:00:00Z,2026-11-20T13:00:00Z,nan,2"),
            actual_csv=act("a,2026-11-20T13:00:00Z,1.5"),
            cutoff_raw=CUTOFF,
        )
    assert "finite" in str(excinfo.value)


# ---------------------------------------------------------------- HTTP layer


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as test_client:
        yield test_client


def _files(predictions: bytes, actuals: bytes):
    return {
        "prediction_csv": ("predictions.csv", predictions, "text/csv"),
        "actual_csv": ("actuals.csv", actuals, "text/csv"),
    }


def test_api_roundtrip_saves_and_downloads(client):
    response = client.post(
        "/api/backtests",
        files=_files(
            pred(
                "a,2026-11-20T11:00:00Z,2026-11-20T12:00:00Z,0,2",
                "b,2026-11-20T10:00:00Z,2026-11-20T13:00:00Z,0,1",
            ),
            act("a,2026-11-20T12:00:00Z,1", "b,2026-11-20T13:00:00Z,5"),
        ),
        data={"cutoff": CUTOFF},
    )
    assert response.status_code == 201, response.text
    payload = response.json()
    run_id = payload["id"]
    assert payload["run"]["misses"][0]["series_id"] == "b"

    listing = client.get("/api/backtests")
    assert listing.status_code == 200
    assert any(run["id"] == run_id for run in listing.json()["runs"])

    detail = client.get(f"/api/backtests/{run_id}")
    assert detail.status_code == 200
    assert detail.json()["cutoff"] == CUTOFF

    samples = client.get(payload["download_url"])
    assert samples.status_code == 200
    assert "attachment" in samples.headers["content-disposition"]
    lines = samples.text.strip().splitlines()
    assert lines[0].startswith("series_id,issue_time,target_time,horizon")
    assert len(lines) == 3

    summary = client.get(payload["summary_url"])
    assert summary.status_code == 200
    assert summary.text.splitlines()[0].split(",")[0] == "horizon"

    original = client.get(f"/api/backtests/{run_id}/download/input/prediction.csv")
    assert original.status_code == 200
    assert b"series_id,issue_time" in original.content


def test_api_rejects_batch_and_does_not_save(client):
    before = len(client.get("/api/backtests").json()["runs"])
    response = client.post(
        "/api/backtests",
        files=_files(
            pred("a,2026-11-20T11:00:00Z,2026-11-20T12:00:00Z,2,1"),
            act("a,2026-11-20T12:00:00Z,1"),
        ),
        data={"cutoff": CUTOFF},
    )
    assert response.status_code == 422
    assert response.json()["errors"]
    after = len(client.get("/api/backtests").json()["runs"])
    assert before == after


def test_api_health_and_missing_run(client):
    assert client.get("/healthz").json()["status"] == "ok"
    assert client.get("/api/backtests/does-not-exist").status_code == 404


def test_api_reports_empty_upload_and_bad_cutoff(client):
    empty = client.post(
        "/api/backtests",
        files=_files(b"", act("a,2026-11-20T13:00:00Z,1")),
        data={"cutoff": CUTOFF},
    )
    assert empty.status_code == 400
    assert empty.json()["errors"]

    bad_cutoff = client.post(
        "/api/backtests",
        files=_files(
            pred("a,2026-11-20T10:00:00Z,2026-11-20T13:00:00Z,1,2"),
            act("a,2026-11-20T13:00:00Z,1"),
        ),
        data={"cutoff": "not-a-time"},
    )
    assert bad_cutoff.status_code == 422
    assert "invalid evaluation cutoff" in bad_cutoff.json()["errors"][0]

    missing_cutoff = client.post(
        "/api/backtests",
        files=_files(
            pred("a,2026-11-20T10:00:00Z,2026-11-20T13:00:00Z,1,2"),
            act("a,2026-11-20T13:00:00Z,1"),
        ),
    )
    assert missing_cutoff.status_code == 422
    assert "cutoff time is required" in missing_cutoff.json()["errors"][0]


def test_index_page_served(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "预测区间回测台" in response.text
