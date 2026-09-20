"""
Localhost browser interface for the Booking Scraper Rebuild.

A thin FastAPI app that runs the EXISTING application-layer scraper
(src.orchestrator.ScrapeOrchestrator) from a web form. It adds no second
scraper and no fake data: every search hits real Booking.com.

Each search runs in a dedicated background thread (with its own asyncio
event loop) so the browser stays responsive while the scraper works. The
UI polls a small status endpoint for progress; results come from the JSON
file written by the existing `src.storage.Storage` layer.
"""

from __future__ import annotations

import asyncio
import json
import threading
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from pydantic import BaseModel, Field, model_validator

import booking_scraper.logger as scraper_log
from .export import flatten_results, rows_to_psv, rows_to_xlsx_bytes
from .models import HotelSearchParams
from .orchestrator import (
    ScrapeOrchestrator,
    STATUS_CANCELLED,
    STATUS_FAILED,
)

scraper_log.set_print_enabled(False)

APP_DIR = Path(__file__).resolve().parent
INDEX_FILE = APP_DIR / "web" / "index.html"
OUTPUT_DIR = "data"
MAX_JOB_HISTORY = 20

# Job lifecycle statuses used by the web layer.
JS_RUNNING = "running"
JS_CANCELLING = "cancelling"
JS_DONE = "done"
JS_ERROR = "error"
JS_CANCELLED = "cancelled"


# ---------------------------------------------------------------------------
# Request/validation (mirrors the CLI rules)
# ---------------------------------------------------------------------------

def _default_checkin() -> date:
    return date.today() + timedelta(days=1)


def _default_checkout() -> date:
    return _default_checkin() + timedelta(days=1)


class SearchRequest(BaseModel):
    destination: str = Field(..., min_length=1)
    checkin: Optional[str] = None
    checkout: Optional[str] = None
    adults: int = Field(2, ge=1, le=10)
    children: int = Field(0, ge=0, le=10)
    rooms: int = Field(1, ge=1, le=5)
    currency: str = Field("USD", min_length=3, max_length=4)
    max_hotels: int = Field(5, ge=1, le=50)

    @model_validator(mode="after")
    def _resolve_dates(self) -> "SearchRequest":
        self.destination = self.destination.strip()
        if not self.destination:
            raise ValueError("Destination cannot be empty")
        checkin = (
            date.fromisoformat(self.checkin)
            if self.checkin
            else _default_checkin()
        )
        checkout = (
            date.fromisoformat(self.checkout)
            if self.checkout
            else _default_checkout()
        )
        if checkin >= checkout:
            raise ValueError("Check-in date must be before check-out date")
        if checkin < date.today():
            raise ValueError(
                f"Check-in date {checkin.isoformat()} is in the past. "
                f"Booking.com requires a valid future stay."
            )
        self.checkin = checkin.isoformat()
        self.checkout = checkout.isoformat()
        self.currency = self.currency.upper().strip()
        return self


@dataclass
class Job:
    id: str
    params: dict
    status: str = JS_RUNNING
    progress: List[str] = field(default_factory=list)
    error: Optional[str] = None
    output_path: Optional[str] = None
    results: Optional[list] = None
    meta: Optional[dict] = None
    started_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))
    finished_at: Optional[str] = None
    cancel_event: Optional[asyncio.Event] = None
    loop: Optional[asyncio.AbstractEventLoop] = None
    cancel_requested: bool = False

    def to_payload(self, include_results: bool = True) -> dict:
        payload = {
            "id": self.id,
            "status": self.status,
            "params": self.params,
            "progress": list(self.progress),
            "error": self.error,
            "output_path": self.output_path,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "download_url": (
                f"/api/search/{self.id}/download" if self.output_path else None
            ),
            "meta": self.meta,
            "summary": self.summary(),
        }
        if include_results and self.status in (JS_DONE, JS_CANCELLED) and self.results is not None:
            payload["results"] = self.results
        return payload

    def summary(self) -> dict:
        meta = self.meta or {}
        return {
            "requested_hotels": meta.get("requested_hotels"),
            "successful_hotels": meta.get("successful_hotels"),
            "failed_hotels": meta.get("failed_hotels"),
            "units": meta.get("units"),
            "retries_total": meta.get("retries_total", 0),
            "failure_reason": meta.get("failure_reason"),
        }


# ---------------------------------------------------------------------------
# Job store (single in-memory worker, one search at a time)
# ---------------------------------------------------------------------------

_BUSY_LOCK = threading.Lock()
_JOB_BY_ID: Dict[str, Job] = {}


def _register(job: Job) -> None:
    _JOB_BY_ID[job.id] = job
    if len(_JOB_BY_ID) > MAX_JOB_HISTORY:
        oldest = next(iter(_JOB_BY_ID))
        _JOB_BY_ID.pop(oldest)


def _job_for_status() -> Optional[Job]:
    """Return the currently running/cancelling job, if any."""
    for job in _JOB_BY_ID.values():
        if job.status in (JS_RUNNING, JS_CANCELLING):
            return job
    return None


def _emit(job: Job, line: str) -> None:
    with _BUSY_LOCK:
        job.progress.append(line)


def _run_search(job: Job, params: HotelSearchParams, max_hotels: int) -> None:
    async def _inner() -> None:
        job.loop = asyncio.get_running_loop()
        job.cancel_event = asyncio.Event()
        if job.cancel_requested:
            job.cancel_event.set()
        orchestrator = ScrapeOrchestrator(headless=True, output_dir=OUTPUT_DIR)
        try:
            path = await orchestrator.run(
                params,
                max_hotels=max_hotels,
                progress=lambda line: _emit(job, line),
                cancel_event=job.cancel_event,
            )
            meta = orchestrator.last_run_meta or {}
        except Exception as exc:  # noqa: BLE001 - surface any scraper failure to the UI
            with _BUSY_LOCK:
                job.status = JS_ERROR
                job.error = str(exc)
                job.finished_at = datetime.now().isoformat(timespec="seconds")
            return

        with _BUSY_LOCK:
            meta_status = meta.get("status")
            if meta_status == STATUS_CANCELLED:
                job.status = JS_CANCELLED
            elif meta_status == STATUS_FAILED:
                job.status = JS_ERROR
            else:
                job.status = JS_DONE
            job.meta = meta
            job.finished_at = datetime.now().isoformat(timespec="seconds")
            job.output_path = path or None
            if job.output_path:
                out = Path(job.output_path)
                if out.exists():
                    try:
                        job.results = json.loads(out.read_text(encoding="utf-8"))
                    except Exception:
                        job.results = None
            if job.status == JS_ERROR and meta.get("failure_reason"):
                job.error = meta.get("failure_reason")

    try:
        asyncio.run(_inner())
    except Exception as exc:  # noqa: BLE001
        with _BUSY_LOCK:
            job.status = JS_ERROR
            job.error = str(exc)
            job.finished_at = datetime.now().isoformat(timespec="seconds")


def _start_search(req: SearchRequest) -> Job:
    job = Job(id=uuid.uuid4().hex[:12], params=req.model_dump())
    _register(job)

    params = HotelSearchParams(
        destination=req.destination,
        checkin=date.fromisoformat(req.checkin),
        checkout=date.fromisoformat(req.checkout),
        adults=req.adults,
        children=req.children,
        rooms=req.rooms,
        currency=req.currency,
    )

    thread = threading.Thread(
        target=_run_search,
        args=(job, params, req.max_hotels),
        name=f"booking-search-{job.id}",
        daemon=True,
    )
    thread.start()
    return job


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(
    title="Booking Scraper Rebuild",
    description="Localhost UI that runs the existing Booking.com scraper.",
    version="0.2.0",
)


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return INDEX_FILE.read_text(encoding="utf-8")


@app.get("/api/health")
def health() -> dict:
    return {"status": "ok"}


@app.post("/api/search")
def create_search(req: SearchRequest) -> JSONResponse:
    running = _job_for_status()
    if running is not None:
        raise HTTPException(
            status_code=409,
            detail=(
                f"A search is already running ({running.id}). "
                "Wait for it to finish or cancel it first."
            ),
        )
    job = _start_search(req)
    return JSONResponse({"job_id": job.id, "status": job.status})


@app.get("/api/search/{job_id}")
def job_status(job_id: str) -> dict:
    job = _JOB_BY_ID.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Unknown job id")
    return job.to_payload()


@app.post("/api/search/{job_id}/cancel")
def cancel_job(job_id: str):
    """Request cancellation of a running search. The worker thread flips the
    orchestrator's cancel event; the job ends with an explicit final state."""
    job = _JOB_BY_ID.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Unknown job id")
    with _BUSY_LOCK:
        if job.status not in (JS_RUNNING, JS_CANCELLING):
            return JSONResponse(
                {
                    "job_id": job_id,
                    "status": job.status,
                    "message": "Job is not running; nothing to cancel.",
                }
            )
        was_cancelling = job.status == JS_CANCELLING
        job.cancel_requested = True
        if not was_cancelling:
            job.status = JS_CANCELLING
    if job.loop is not None and job.cancel_event is not None:
        job.loop.call_soon_threadsafe(job.cancel_event.set)
    return JSONResponse({"job_id": job_id, "status": JS_CANCELLING})


@app.get("/api/search/{job_id}/download")
def job_download(job_id: str):
    job = _JOB_BY_ID.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Unknown job id")
    if not job.output_path:
        raise HTTPException(status_code=404, detail="No output file for this job")
    out = Path(job.output_path).resolve()
    base = Path(OUTPUT_DIR).resolve()
    if base not in out.parents:
        raise HTTPException(status_code=403, detail="Output path outside data dir")
    if not out.exists():
        raise HTTPException(status_code=404, detail="Output file no longer exists")
    return FileResponse(
        out,
        media_type="application/json",
        filename=out.name,
    )


@app.get("/api/search/{job_id}/export.xlsx")
def job_export_xlsx(job_id: str):
    """Export the job's results as a real .xlsx workbook (openpyxl), one
    row per room/unit. Missing optional fields are blank cells, never
    fabricated placeholders."""
    job = _JOB_BY_ID.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Unknown job id")
    if not job.results:
        raise HTTPException(status_code=404, detail="No results for this job")
    rows = flatten_results(job.results)
    data = rows_to_xlsx_bytes(rows)
    filename = f"{Path(job.output_path).stem if job.output_path else job_id}.xlsx"
    return Response(
        content=data,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/api/search/{job_id}/export.psv")
def job_export_psv(job_id: str):
    """Export the job's results as pipe-separated values, one row per
    room/unit."""
    job = _JOB_BY_ID.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Unknown job id")
    if not job.results:
        raise HTTPException(status_code=404, detail="No results for this job")
    rows = flatten_results(job.results)
    text = rows_to_psv(rows)
    filename = f"{Path(job.output_path).stem if job.output_path else job_id}.psv"
    return Response(
        content=text.encode("utf-8"),
        media_type="text/plain; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/api/search/{job_id}/meta")
def job_meta(job_id: str):
    """Expose the companion metadata file for a finished job."""
    job = _JOB_BY_ID.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Unknown job id")
    if job.meta is None:
        raise HTTPException(status_code=404, detail="No metadata for this job yet")
    return JSONResponse(job.meta)