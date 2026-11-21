"""FastAPI application for the prediction-interval backtest workbench."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from urllib.parse import quote

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from . import storage
from .backtest import ValidationError, run_backtest

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
MAX_UPLOAD_BYTES = int(os.environ.get("BACKTEST_MAX_UPLOAD_BYTES", 20 * 1024 * 1024))

app = FastAPI(title="Interval Backtest Workbench", version="1.0.0")
app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")


@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    with open(os.path.join(BASE_DIR, "static", "index.html"), encoding="utf-8") as handle:
        return HTMLResponse(handle.read())


@app.get("/healthz")
def healthz() -> dict:
    return {"status": "ok", "time": datetime.now(timezone.utc).isoformat()}


async def _read_upload(upload: UploadFile, label: str) -> bytes:
    content = await upload.read(MAX_UPLOAD_BYTES + 1)
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"{label} file exceeds the {MAX_UPLOAD_BYTES} byte limit",
        )
    if not content.strip():
        raise HTTPException(status_code=400, detail=f"{label} file is empty")
    return content


def _validation_response(exc: ValidationError) -> JSONResponse:
    return JSONResponse(
        status_code=422,
        content={
            "detail": "Upload rejected",
            "errors": exc.errors,
            "total_errors": exc.total_errors,
        },
    )


@app.post("/api/backtests")
async def create_backtest(
    prediction_csv: UploadFile = File(...),
    actual_csv: UploadFile = File(...),
    cutoff: str = Form(""),
) -> JSONResponse:
    try:
        prediction_bytes = await _read_upload(prediction_csv, "prediction")
        actual_bytes = await _read_upload(actual_csv, "actual")
    except HTTPException as exc:
        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": exc.detail, "errors": [exc.detail]},
        )

    try:
        result = run_backtest(
            prediction_csv=prediction_bytes,
            actual_csv=actual_bytes,
            cutoff_raw=cutoff,
        )
    except ValidationError as exc:
        return _validation_response(exc)

    summary = {
        "cutoff": result.cutoff.isoformat().replace("+00:00", "Z"),
        "warnings": result.warnings,
        "submitted_rows": result.submitted_rows,
        "horizons": result.horizons,
        "misses": result.misses,
        "samples": result.intervals,
    }
    run_id = storage.save_backtest(
        cutoff=summary["cutoff"],
        prediction_filename=prediction_csv.filename or "predictions.csv",
        actual_filename=actual_csv.filename or "actuals.csv",
        prediction_csv=prediction_bytes,
        actual_csv=actual_bytes,
        summary=summary,
        submitted_rows=summary["submitted_rows"],
    )
    run = storage.get_backtest(run_id)
    assert run is not None
    return JSONResponse(
        status_code=201,
        content={
            "id": run_id,
            "download_url": f"/api/backtests/{run_id}/download/samples.csv",
            "summary_url": f"/api/backtests/{run_id}/download/summary.csv",
            "run": run,
        },
    )


@app.get("/api/backtests")
def list_backtests(limit: int = 20) -> dict:
    limit = max(1, min(limit, 100))
    runs = storage.list_backtests(limit)
    for run in runs:
        # Keep the list payload small; full rows are fetched on demand.
        run.pop("samples", None)
        run.pop("misses", None)
    return {"runs": runs}


@app.get("/api/backtests/{run_id}")
def get_backtest(run_id: str) -> dict:
    run = storage.get_backtest(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="backtest not found")
    return run


def _download(content: bytes, filename: str, media_type: str) -> Response:
    ascii_name = filename.encode("ascii", "replace").decode("ascii").replace('"', "'")
    return Response(
        content=content,
        media_type=media_type,
        headers={
            "Content-Disposition": (
                f'attachment; filename="{ascii_name}"; '
                f"filename*=UTF-8''{quote(filename)}"
            )
        },
    )


@app.get("/api/backtests/{run_id}/download/samples.csv")
def download_samples(run_id: str) -> Response:
    run = storage.get_backtest(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="backtest not found")
    return _download(
        storage.render_samples_csv(run), f"backtest-{run_id}-samples.csv", "text/csv"
    )


@app.get("/api/backtests/{run_id}/download/summary.csv")
def download_summary(run_id: str) -> Response:
    run = storage.get_backtest(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="backtest not found")
    return _download(
        storage.render_summary_csv(run), f"backtest-{run_id}-summary.csv", "text/csv"
    )


@app.get("/api/backtests/{run_id}/download/input/{kind}.csv")
def download_input(run_id: str, kind: str) -> Response:
    if kind not in ("prediction", "actual"):
        raise HTTPException(status_code=404, detail="unknown input kind")
    found = storage.render_input_csv(run_id, kind)
    if found is None:
        raise HTTPException(status_code=404, detail="backtest not found")
    filename, content = found
    return _download(content, filename, "text/csv")
