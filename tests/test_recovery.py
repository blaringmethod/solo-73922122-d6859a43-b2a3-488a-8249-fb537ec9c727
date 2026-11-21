import io
import os
import tempfile
import threading
import uuid

import pytest

_TMP = tempfile.mkdtemp(prefix="backtest-recovery-tests-")
os.environ["BACKTEST_DB_PATH"] = os.path.join(_TMP, "test.db")

from fastapi.testclient import TestClient  # noqa: E402

from app import storage  # noqa: E402
from app.backtest import run_backtest  # noqa: E402
from app.main import app  # noqa: E402
from app.recovery import (  # noqa: E402
    compare_summary_rows,
    run_recovery,
    summarize_samples,
)
from app.backtest import ValidationError  # noqa: E402

CUTOFF = "2026-11-20T12:00:00Z"

SAMPLES_HEADER = (
    "series_id,issue_time,target_time,horizon,lead_seconds,lower,upper,width,actual,covered\n"
)
SUMMARY_HEADER = "horizon,sample_count,covered_count,coverage,mean_width\n"

SAMPLES_ROWS = [
    "alpha,2026-11-20T12:00:00Z,2026-11-20T13:00:00Z,1h,3600,10.0,11.0,1.0,11.3,false",
    "alpha,2026-11-20T11:00:00Z,2026-11-20T15:00:00Z,4h,14400,9.0,10.5,1.5,10.2,true",
    "beta,2026-11-20T11:00:00Z,2026-11-20T13:00:00Z,2h,7200,20.0,22.0,2.0,21.1,true",
    "beta,2026-11-20T12:00:00Z,2026-11-20T16:00:00Z,4h,14400,19.5,21.5,2.0,22.4,false",
]
SUMMARY_ROWS = [
    "1h,1,0,0.000000,1.000000",
    "2h,1,1,1.000000,2.000000",
    "4h,2,1,0.500000,1.750000",
]

# The original inputs that produced the exported result above.
PRED_HEADER = "series_id,issue_time,target_time,lower,upper\n"
ACT_HEADER = "series_id,target_time,actual\n"
PRED_ROWS = [
    "alpha,2026-11-20T09:00:00Z,2026-11-20T13:00:00Z,9.0,11.0",
    "alpha,2026-11-20T10:30:00Z,2026-11-20T13:00:00Z,9.5,11.5",
    "alpha,2026-11-20T12:00:00Z,2026-11-20T13:00:00Z,10.0,11.0",
    "alpha,2026-11-20T11:00:00Z,2026-11-20T15:00:00Z,9.0,10.5",
    "beta,2026-11-20T11:00:00Z,2026-11-20T13:00:00Z,20.0,22.0",
    "beta,2026-11-20T12:00:00Z,2026-11-20T16:00:00Z,19.5,21.5",
]
ACT_ROWS = [
    "alpha,2026-11-20T13:00:00Z,11.3",
    "alpha,2026-11-20T15:00:00Z,10.2",
    "beta,2026-11-20T13:00:00Z,21.1",
    "beta,2026-11-20T16:00:00Z,22.4",
]


def samples_csv(rows=SAMPLES_ROWS) -> bytes:
    return (SAMPLES_HEADER + "\n".join(rows) + "\n").encode()


def summary_csv(rows=SUMMARY_ROWS) -> bytes:
    return (SUMMARY_HEADER + "\n".join(rows) + "\n").encode()


def pred_csv(rows=PRED_ROWS) -> bytes:
    return (PRED_HEADER + "\n".join(rows) + "\n").encode()


def act_csv(rows=ACT_ROWS) -> bytes:
    return (ACT_HEADER + "\n".join(rows) + "\n").encode()


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as test_client:
        yield test_client


def _recover(client, samples=None, summary=None, cutoff=CUTOFF):
    return client.post(
        "/api/backtests/recover",
        files={
            "samples_csv": ("samples.csv", samples or samples_csv(), "text/csv"),
            "summary_csv": ("summary.csv", summary or summary_csv(), "text/csv"),
        },
        data={"cutoff": cutoff},
    )


def _recovered_run(client) -> dict:
    response = _recover(client)
    assert response.status_code == 201, response.text
    return response.json()


# ------------------------------------------------------------------ recovery


def test_recovery_saves_results_only_run(client):
    payload = _recovered_run(client)
    run = payload["run"]
    run_id = payload["id"]
    assert run["kind"] == "recovered"
    assert run["input_status"] == "awaiting"
    assert run["has_inputs"] is False
    assert run["cutoff"] == CUTOFF
    assert len(run["samples"]) == 4
    assert [h["horizon"] for h in run["horizons"]] == ["1h", "2h", "4h"]
    assert [m["series_id"] for m in run["misses"]] == ["alpha", "beta"]
    assert run["warnings"]

    samples = client.get(f"/api/backtests/{run_id}/download/samples.csv")
    summary = client.get(f"/api/backtests/{run_id}/download/summary.csv")
    assert samples.status_code == 200 and summary.status_code == 200
    # The verified export is returned byte-for-byte, not re-rendered.
    assert samples.content == samples_csv()
    assert summary.content == summary_csv()
    assert summary.text.splitlines()[0].startswith("horizon,")

    # No original inputs exist yet.
    assert client.get(f"/api/backtests/{run_id}/download/input/prediction.csv").status_code == 404
    assert client.get(f"/api/backtests/{run_id}/download/input/actual.csv").status_code == 404

    listing = client.get("/api/backtests").json()["runs"]
    listed = next(item for item in listing if item["id"] == run_id)
    assert listed["kind"] == "recovered"
    assert listed["input_status"] == "awaiting"


def test_recovery_ignores_row_order_and_line_endings(client):
    shuffled = list(reversed(SAMPLES_ROWS))
    base = run_recovery(
        samples_csv=samples_csv(),
        summary_csv=summary_csv(),
        cutoff_raw=CUTOFF,
    )
    reordered = run_recovery(
        samples_csv=samples_csv(shuffled).replace(b"\n", b"\r\n"),
        summary_csv=summary_csv(list(reversed(SUMMARY_ROWS))).replace(
            b"\n", b"\r\n"
        ),
        cutoff_raw=CUTOFF,
    )
    assert reordered.samples == base.samples
    assert reordered.horizons == base.horizons

    response = _recover(
        client,
        samples=samples_csv(shuffled).replace(b"\n", b"\r\n"),
        summary=summary_csv(list(reversed(SUMMARY_ROWS))),
    )
    assert response.status_code == 201, response.text


@pytest.mark.parametrize(
    "rows, needle",
    [
        # wrong lead time
        (SAMPLES_ROWS[:1] + [SAMPLES_ROWS[1].replace(",14400,", ",14000,")] + SAMPLES_ROWS[2:], "lead_seconds"),
        # wrong horizon label
        (SAMPLES_ROWS[:1] + [SAMPLES_ROWS[1].replace(",4h,", ",3h,")] + SAMPLES_ROWS[2:], "horizon"),
        # wrong width
        (SAMPLES_ROWS[:1] + [SAMPLES_ROWS[1].replace(",1.5,10.2", ",1.6,10.2")] + SAMPLES_ROWS[2:], "width"),
        # wrong covered flag
        (SAMPLES_ROWS[:1] + [SAMPLES_ROWS[1].replace(",10.2,true", ",10.2,false")] + SAMPLES_ROWS[2:], "covered"),
        # duplicate (series, target)
        (SAMPLES_ROWS + [SAMPLES_ROWS[0]], "duplicate sample"),
        # prediction issued after its target
        (SAMPLES_ROWS[:1] + ["alpha,2026-11-20T16:00:00Z,2026-11-20T15:00:00Z,0s,0,1,2,1,1,true"] + SAMPLES_ROWS[1:], "after target_time"),
        # prediction issued after the cutoff
        (SAMPLES_ROWS[:1] + ["alpha,2026-11-20T13:00:00Z,2026-11-20T15:00:00Z,2h,7200,9,10.5,1.5,10.2,true"] + SAMPLES_ROWS[1:], "after the cutoff"),
    ],
)
def test_recovery_rejects_tampered_detail(rows, needle):
    with pytest.raises(ValidationError) as excinfo:
        run_recovery(
            samples_csv=samples_csv(rows),
            summary_csv=summary_csv(),
            cutoff_raw=CUTOFF,
        )
    assert needle in str(excinfo.value)


@pytest.mark.parametrize(
    "rows, needle",
    [
        (["1h,1,0,0.000000,1.000000", "2h,1,1,1.000000,2.000000", "4h,2,2,1.000000,1.750000"], "covered_count"),
        (["1h,1,0,0.000000,1.000000", "2h,1,1,1.000000,2.000000"], "4h"),
        (SUMMARY_ROWS + ["6h,1,0,0.000000,1.000000"], "6h"),
        (["1h,1,0,0.000000,1.000000", "2h,1,1,1.000000,2.000000", "4h,2,1,0.500000,1.700000"], "mean_width"),
        (["1h,1,0,0.000000,1.000000", "2h,1,1,1.000000,2.000000", "4h,2,1,0.500001,1.750000"], "coverage"),
    ],
)
def test_recovery_rejects_summary_mismatch(rows, needle):
    with pytest.raises(ValidationError) as excinfo:
        run_recovery(
            samples_csv=samples_csv(),
            summary_csv=summary_csv(rows),
            cutoff_raw=CUTOFF,
        )
    assert needle in str(excinfo.value)


def test_summary_comparison_is_order_insensitive_at_export_precision():
    recomputed = summarize_samples(
        [
            {
                "horizon": "1h",
                "lead_seconds": 3600,
                "width": 1.0,
                "covered": False,
            },
            {
                "horizon": "1h",
                "lead_seconds": 3600,
                "width": 2.0,
                "covered": True,
            },
        ]
    )
    expected = [
        {
            "horizon": "1h",
            "sample_count": 2,
            "covered_count": 1,
            "coverage": 0.5,
            "mean_width": 1.5,
        }
    ]
    assert compare_summary_rows(expected, recomputed) == []
    almost = [{**expected[0], "coverage": 0.5000004}]
    assert compare_summary_rows(expected, almost) == []
    off = [{**expected[0], "coverage": 0.5000006}]
    assert compare_summary_rows(expected, off)


def test_recovery_rejects_non_recoverable_headers(client):
    bad = "series_id,target_time,actual\nx,2026-11-20T13:00:00Z,1\n"
    response = _recover(client, samples=bad.encode())
    assert response.status_code == 422
    assert "missing required column" in response.text


def test_recovery_requires_cutoff_and_non_empty_files(client):
    no_cutoff = _recover(client, cutoff="")
    assert no_cutoff.status_code == 422
    assert "cutoff time is required" in no_cutoff.json()["errors"][0]

    empty = client.post(
        "/api/backtests/recover",
        files={
            "samples_csv": ("samples.csv", b"", "text/csv"),
            "summary_csv": ("summary.csv", summary_csv(), "text/csv"),
        },
        data={"cutoff": CUTOFF},
    )
    assert empty.status_code == 400
    assert empty.json()["errors"]


def test_recovery_rejects_cutoff_before_some_issue_time(client):
    response = _recover(client, cutoff="2026-11-20T09:00:00Z")
    assert response.status_code == 422
    assert "after the cutoff" in response.text


# ------------------------------------------------------------------ backfill


def test_backfill_registers_first_matching_pair_and_keeps_result(client):
    payload = _recovered_run(client)
    run_id = payload["id"]
    before = client.get(f"/api/backtests/{run_id}").json()

    # Different row order and CRLF line endings must not matter.
    prediction = pred_csv(list(reversed(PRED_ROWS))).replace(b"\n", b"\r\n")
    actual = act_csv(list(reversed(ACT_ROWS)))
    response = client.post(
        f"/api/backtests/{run_id}/inputs",
        files={
            "prediction_csv": ("history-predictions.csv", prediction, "text/csv"),
            "actual_csv": ("history-actuals.csv", actual, "text/csv"),
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["id"] == run_id

    after = client.get(f"/api/backtests/{run_id}").json()
    assert after["kind"] == "recovered"
    assert after["input_status"] == "complete"
    assert after["has_inputs"] is True
    # The recovered results are untouched, down to their values.
    assert after["samples"] == before["samples"]
    assert after["horizons"] == before["horizons"]
    assert after["cutoff"] == before["cutoff"]

    pred_download = client.get(f"/api/backtests/{run_id}/download/input/prediction.csv")
    act_download = client.get(f"/api/backtests/{run_id}/download/input/actual.csv")
    assert pred_download.status_code == 200
    assert pred_download.content == prediction
    assert act_download.content == actual

    # A second submission cannot replace the registered pair.
    second = client.post(
        f"/api/backtests/{run_id}/inputs",
        files={
            "prediction_csv": ("other.csv", pred_csv(), "text/csv"),
            "actual_csv": ("other.csv", act_csv(), "text/csv"),
        },
    )
    assert second.status_code == 409
    assert client.get(f"/api/backtests/{run_id}/download/input/prediction.csv").content == prediction


def test_backfill_mismatch_is_rejected_and_keeps_run_awaiting(client):
    run_id = _recovered_run(client)["id"]
    before = client.get(f"/api/backtests/{run_id}").json()

    wrong_actual = act_csv([*ACT_ROWS[:3], "beta,2026-11-20T16:00:00Z,99.0"])
    response = client.post(
        f"/api/backtests/{run_id}/inputs",
        files={
            "prediction_csv": ("p.csv", pred_csv(), "text/csv"),
            "actual_csv": ("a.csv", wrong_actual, "text/csv"),
        },
    )
    assert response.status_code == 422
    assert response.json()["errors"]
    after = client.get(f"/api/backtests/{run_id}").json()
    assert after["input_status"] == "awaiting"
    assert after["samples"] == before["samples"]
    assert client.get(f"/api/backtests/{run_id}/download/input/prediction.csv").status_code == 404


def test_backfill_detects_detail_only_and_summary_only_differences(client, monkeypatch):
    """Detail and summary must both agree; each alone is not enough."""
    from app import recovery

    run_id = _recovered_run(client)["id"]
    real_compare_samples = recovery.compare_samples
    real_compare_summary = recovery.compare_summary_rows

    # 1) Summary disagrees while the detail matches.
    monkeypatch.setattr(recovery, "compare_samples", lambda *a, **k: [])
    monkeypatch.setattr(
        recovery,
        "compare_summary_rows",
        lambda *a, **k: ["summary horizon '4h': mean_width is 9.0, expected 1.75"],
    )
    summary_only = client.post(
        f"/api/backtests/{run_id}/inputs",
        files={
            "prediction_csv": ("p.csv", pred_csv(), "text/csv"),
            "actual_csv": ("a.csv", act_csv(), "text/csv"),
        },
    )
    assert summary_only.status_code == 422
    assert "mean_width" in summary_only.text
    assert client.get(f"/api/backtests/{run_id}").json()["input_status"] == "awaiting"

    # 2) Detail disagrees while the summary matches.
    monkeypatch.setattr(recovery, "compare_samples", real_compare_samples)
    monkeypatch.setattr(
        recovery,
        "compare_summary_rows",
        lambda *a, **k: ["detail for series 'alpha' target x: actual is 0, expected 11.3"],
    )
    detail_only = client.post(
        f"/api/backtests/{run_id}/inputs",
        files={
            "prediction_csv": ("p.csv", pred_csv(), "text/csv"),
            "actual_csv": ("a.csv", act_csv(), "text/csv"),
        },
    )
    assert detail_only.status_code == 422
    assert "actual is" in detail_only.text
    assert client.get(f"/api/backtests/{run_id}").json()["input_status"] == "awaiting"

    # Restore both and the same pair is accepted.
    monkeypatch.setattr(recovery, "compare_summary_rows", real_compare_summary)
    accepted = client.post(
        f"/api/backtests/{run_id}/inputs",
        files={
            "prediction_csv": ("p.csv", pred_csv(), "text/csv"),
            "actual_csv": ("a.csv", act_csv(), "text/csv"),
        },
    )
    assert accepted.status_code == 200, accepted.text
    assert client.get(f"/api/backtests/{run_id}").json()["input_status"] == "complete"


def test_backfill_accepts_extra_post_cutoff_vintage(client):
    """An irrelevant later vintage does not change the selected rows, so it matches."""
    run_id = _recovered_run(client)["id"]
    extra = [*PRED_ROWS, "alpha,2026-11-20T12:30:00Z,2026-11-20T13:00:00Z,50.0,60.0"]
    response = client.post(
        f"/api/backtests/{run_id}/inputs",
        files={
            "prediction_csv": ("p.csv", pred_csv(extra), "text/csv"),
            "actual_csv": ("a.csv", act_csv(), "text/csv"),
        },
    )
    assert response.status_code == 200, response.text
    stored = client.get(f"/api/backtests/{run_id}/download/input/prediction.csv")
    assert stored.content == pred_csv(extra)


def test_backfill_rejects_invalid_inputs_without_saving(client):
    run_id = _recovered_run(client)["id"]
    response = client.post(
        f"/api/backtests/{run_id}/inputs",
        files={
            "prediction_csv": ("p.csv", pred_csv(["a,2026-11-20T10:00:00Z,2026-11-20T13:00:00Z,2,1"]), "text/csv"),
            "actual_csv": ("a.csv", act_csv(), "text/csv"),
        },
    )
    assert response.status_code == 422
    assert client.get(f"/api/backtests/{run_id}").json()["input_status"] == "awaiting"


def test_backfill_only_applies_to_recovered_runs(client):
    normal = client.post(
        "/api/backtests",
        files={
            "prediction_csv": ("p.csv", pred_csv(), "text/csv"),
            "actual_csv": ("a.csv", act_csv(), "text/csv"),
        },
        data={"cutoff": CUTOFF},
    )
    assert normal.status_code == 201
    response = client.post(
        f"/api/backtests/{normal.json()['id']}/inputs",
        files={
            "prediction_csv": ("p.csv", pred_csv(), "text/csv"),
            "actual_csv": ("a.csv", act_csv(), "text/csv"),
        },
    )
    assert response.status_code == 409


def test_backfill_unknown_run_is_404(client):
    response = client.post(
        "/api/backtests/does-not-exist/inputs",
        files={
            "prediction_csv": ("p.csv", pred_csv(), "text/csv"),
            "actual_csv": ("a.csv", act_csv(), "text/csv"),
        },
    )
    assert response.status_code == 404


# --------------------------------------------------- concurrency & persistence


def test_concurrent_backfill_has_exactly_one_winner(client):
    run_id = _recovered_run(client)["id"]
    first_pred = pred_csv(list(reversed(PRED_ROWS)))
    second_pred = pred_csv(PRED_ROWS + ["alpha,2026-11-20T12:30:00Z,2026-11-20T13:00:00Z,50.0,60.0"])
    barrier = threading.Barrier(2)
    results: list[bool] = []
    lock = threading.Lock()

    def submit(prediction: bytes):
        barrier.wait()
        won = storage.register_inputs(
            run_id,
            prediction_filename="p.csv",
            actual_filename="a.csv",
            prediction_csv=prediction,
            actual_csv=act_csv(),
        )
        with lock:
            results.append(won)
        return won

    threads = [
        threading.Thread(target=submit, args=(first_pred,)),
        threading.Thread(target=submit, args=(second_pred,)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(results) == [False, True]
    stored = client.get(f"/api/backtests/{run_id}/download/input/prediction.csv")
    assert stored.content in (first_pred, second_pred)
    assert client.get(f"/api/backtests/{run_id}").json()["input_status"] == "complete"


def test_recovered_run_survives_a_fresh_connection(client):
    run_id = _recovered_run(client)["id"]
    client.post(
        f"/api/backtests/{run_id}/inputs",
        files={
            "prediction_csv": ("p.csv", pred_csv(), "text/csv"),
            "actual_csv": ("a.csv", act_csv(), "text/csv"),
        },
    )
    reopened = storage.get_backtest(run_id)
    assert reopened is not None
    assert reopened["input_status"] == "complete"
    assert reopened["kind"] == "recovered"
    assert len(reopened["samples"]) == 4
