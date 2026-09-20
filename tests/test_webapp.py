"""Webapp tests (FastAPI TestClient) with a fake orchestrator.

No real Booking traffic. Validates request validation, one-at-a-time
concurrency, job lifecycle statuses, cancellation via the API, metadata and
download endpoints, and that errors surface as clean messages (not traces).
"""

import json
import os
import time

import pytest
from fastapi.testclient import TestClient

import src.webapp as webapp_mod
from src.webapp import app


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(webapp_mod, "OUTPUT_DIR", str(tmp_path))
    with TestClient(app) as c:
        yield c


class FakeFastOrchestrator:
    """Completes instantly and writes a small results + meta payload to disk."""

    def __init__(self, headless=True, output_dir="data"):
        self.output_dir = output_dir
        self.last_run_meta = {
            "status": "completed",
            "requested_hotels": 1,
            "successful_hotels": 1,
            "failed_hotels": 0,
            "units": 2,
            "retries_total": 0,
            "failure_reason": None,
            "failures": [],
            "params": {"destination": "Test"},
        }

    async def run(self, params, max_hotels=None, prefix="booking_results",
                  progress=None, cancel_event=None, job_timeout_s=None):
        if progress is not None:
            progress("Fake extracted 1 hotel")
        path = os.path.join(self.output_dir, "booking_results_20260101_000000.json")
        os.makedirs(self.output_dir, exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(
                [{"hotel": {"hotel_id": "x", "hotel_name": "X"},
                  "units": [], "source_url": "u", "collected_at": "t"}],
                fh,
            )
        self.last_meta_path = path.replace(".json", ".meta.json")
        with open(self.last_meta_path, "w", encoding="utf-8") as fh:
            json.dump(self.last_run_meta, fh)
        return path


class FakeSlowOrchestrator:
    """Blocks until cancelled/the event is set, then reports cancelled."""

    def __init__(self, headless=True, output_dir="data"):
        self.output_dir = output_dir
        self.last_run_meta = {
            "status": "cancelled",
            "cancelled": True,
            "requested_hotels": 1,
            "successful_hotels": 0,
            "failed_hotels": 1,
            "units": 0,
            "retries_total": 0,
            "failure_reason": None,
            "failures": [{"index": 0, "hotel_id": "h", "reason": "cancelled before completion"}],
        }

    async def run(self, params, max_hotels=None, prefix="booking_results",
                  progress=None, cancel_event=None, job_timeout_s=None):
        if progress is not None:
            progress("Searching...")
        if cancel_event is not None:
            await cancel_event.wait()
        if progress is not None:
            progress("Cancelled")
        path = os.path.join(self.output_dir, "booking_results_20260101_000001.json")
        os.makedirs(self.output_dir, exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump([], fh)
        return path


class FakeFailOrchestrator:
    def __init__(self, headless=True, output_dir="data"):
        self.output_dir = output_dir
        self.last_run_meta = None

    async def run(self, params, max_hotels=None, prefix="booking_results",
                  progress=None, cancel_event=None, job_timeout_s=None):
        raise RuntimeError("boom: caught, not a trace")


def wait_status(client, job_id, targets, timeout=8.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        data = client.get(f"/api/search/{job_id}").json()
        if data["status"] in targets:
            return data
        time.sleep(0.05)
    raise AssertionError(
        f"job {job_id} never reached {targets}; last={client.get(f'/api/search/{job_id}').json()['status']}"
    )


def test_index_and_health(client):
    html = client.get("/").text
    assert "Booking" in html
    assert client.get("/api/health").json()["status"] == "ok"


def test_validation_errors(client):
    r = client.post("/api/search", json={"destination": "  ", "max_hotels": 3})
    assert r.status_code == 422
    r = client.post(
        "/api/search",
        json={"destination": "Dhaka", "checkin": "2026-09-20", "checkout": "2026-09-20"},
    )
    assert r.status_code == 422
    r = client.post(
        "/api/search",
        json={"destination": "Dhaka", "checkin": "2020-01-01", "checkout": "2020-01-02"},
    )
    assert r.status_code == 422
    r = client.post("/api/search", json={"destination": "Dhaka", "adults": 0})
    assert r.status_code == 422


def test_lifecycle_and_download(client, monkeypatch):
    monkeypatch.setattr(webapp_mod, "ScrapeOrchestrator", FakeFastOrchestrator)
    r = client.post("/api/search", json={"destination": "Dhaka", "max_hotels": 3})
    assert r.status_code == 200
    job_id = r.json()["job_id"]

    data = wait_status(client, job_id, {"done"})
    assert data["status"] == "done"
    assert data["meta"]["status"] == "completed"
    assert data["summary"]["successful_hotels"] == 1
    assert data["summary"]["retries_total"] == 0
    assert data["download_url"] == f"/api/search/{job_id}/download"

    dl = client.get(data["download_url"])
    assert dl.status_code == 200
    assert dl.json()[0]["hotel"]["hotel_id"] == "x"

    meta = client.get(f"/api/search/{job_id}/meta")
    assert meta.status_code == 200
    assert meta.json()["status"] == "completed"


def test_one_search_at_a_time_returns_409(client, monkeypatch):
    monkeypatch.setattr(webapp_mod, "ScrapeOrchestrator", FakeSlowOrchestrator)
    r1 = client.post("/api/search", json={"destination": "Dhaka", "max_hotels": 3})
    assert r1.status_code == 200
    job_id = r1.json()["job_id"]
    wait_status(client, job_id, {"running"})

    r2 = client.post("/api/search", json={"destination": "Sylhet"})
    assert r2.status_code == 409

    client.post(f"/api/search/{job_id}/cancel")
    wait_status(client, job_id, {"cancelled"})


def test_cancellation_endpoint(client, monkeypatch):
    monkeypatch.setattr(webapp_mod, "ScrapeOrchestrator", FakeSlowOrchestrator)
    r = client.post("/api/search", json={"destination": "Dhaka", "max_hotels": 3})
    job_id = r.json()["job_id"]
    wait_status(client, job_id, {"running"})

    # Cancel while running -> explicit 'cancelling', then stable 'cancelled'.
    c = client.post(f"/api/search/{job_id}/cancel")
    assert c.status_code == 200
    assert c.json()["status"] == "cancelling"

    data = wait_status(client, job_id, {"cancelled"})
    assert data["status"] == "cancelled"
    assert data["summary"]["failed_hotels"] == 1
    assert data["meta"]["failures"][0]["reason"] == "cancelled before completion"
    assert data["download_url"] is not None  # partial results preserved

    # Cancelling a finished job is a no-op, not an error.
    c2 = client.post(f"/api/search/{job_id}/cancel")
    assert c2.status_code == 200
    assert c2.json()["status"] == "cancelled"


def test_unknown_job_and_no_meta(client):
    assert client.get("/api/search/nope").status_code == 404
    assert client.get("/api/search/nope/meta").status_code == 404
    assert client.post("/api/search/nope/cancel").status_code == 404


def test_failure_reports_clean_message(client, monkeypatch):
    monkeypatch.setattr(webapp_mod, "ScrapeOrchestrator", FakeFailOrchestrator)
    r = client.post("/api/search", json={"destination": "Dhaka", "max_hotels": 3})
    job_id = r.json()["job_id"]
    data = wait_status(client, job_id, {"error"})
    assert data["status"] == "error"
    assert data["error"] == "boom: caught, not a trace"  # clean, no traceback
    assert data.get("results") is None


def test_download_path_guard(client, monkeypatch):
    monkeypatch.setattr(webapp_mod, "ScrapeOrchestrator", FakeFastOrchestrator)
    r = client.post("/api/search", json={"destination": "Dhaka", "max_hotels": 3})
    job_id = r.json()["job_id"]
    wait_status(client, job_id, {"done"})

    # Force an out-of-tree path to prove the guard rejects it.
    job = webapp_mod._JOB_BY_ID[job_id]
    job.output_path = "/etc/passwd"
    r = client.get(f"/api/search/{job_id}/download")
    assert r.status_code == 403