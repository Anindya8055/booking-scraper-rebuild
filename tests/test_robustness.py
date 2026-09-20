"""Cancellation, job-timeout and schema/storage tests for the orchestrator."""

import asyncio
import json
import time

import pytest

import src.orchestrator as orch_mod
from src.orchestrator import ScrapeOrchestrator, STATUS_CANCELLED, STATUS_FAILED

from conftest import FakeScraperClient, hotel_urls, make_hotel, make_params


@pytest.fixture
def orchestrator(tmp_path):
    return ScrapeOrchestrator(headless=True, output_dir=str(tmp_path))


def patch_client(monkeypatch, fake):
    monkeypatch.setattr(orch_mod, "BookingScraperClient", lambda headless=True: fake)


def test_cancellation_stops_work_and_keeps_completed(monkeypatch, orchestrator):
    prog = []
    urls = hotel_urls(2)
    ok = {u: make_hotel(u, f"Hotel {i}") for i, u in enumerate(urls)}
    cancel_event = asyncio.Event()

    async def detail(url, params):
        if url == urls[0]:  # slow hotel
            await asyncio.sleep(30)
        return ok[url]

    fake = FakeScraperClient(urls, detail)
    patch_client(monkeypatch, fake)

    async def set_later():
        await asyncio.sleep(0.1)
        cancel_event.set()

    async def scenario():
        canceller = asyncio.create_task(set_later())
        started = time.monotonic()
        try:
            path = await orchestrator.run(
                make_params(), max_hotels=2, progress=prog.append, cancel_event=cancel_event
            )
        finally:
            canceller.cancel()
        return path, time.monotonic() - started

    path, elapsed = asyncio.run(scenario())

    assert elapsed < 5.0  # did not wait out the 30s slow hotel
    meta = orchestrator.last_run_meta
    assert meta["status"] == STATUS_CANCELLED
    assert meta["cancelled"] is True
    assert meta["requested_hotels"] == 2
    assert meta["successful_hotels"] == 1  # fast hotel completed before cancel
    assert meta["failed_hotels"] == 1
    assert meta["failures"][0]["hotel_id"] == "hotel-0"
    assert meta["failures"][0]["reason"] == "cancelled before completion"
    assert path.endswith(".json")  # completed results preserved
    assert fake.opened == fake.closed  # nothing orphaned


def test_timeout_marks_job_failed(monkeypatch, orchestrator):
    prog = []
    urls = hotel_urls(2)

    async def detail(url, params):
        await asyncio.sleep(30)

    fake = FakeScraperClient(urls, detail)
    patch_client(monkeypatch, fake)

    # job_timeout_s is the smallest allowed (1.0s) via the run() clamp.
    path = asyncio.run(
        orchestrator.run(make_params(), max_hotels=2, progress=prog.append, job_timeout_s=0.1)
    )

    assert not path  # no hotel ever completed
    meta = orchestrator.last_run_meta
    assert meta["status"] == STATUS_FAILED
    assert meta["cancelled"] is False
    assert meta["timeout_s"] == 0.1  # explicit short value respected
    assert "timeout" in (meta["failure_reason"] or "").lower()
    assert fake.opened == fake.closed  # every started context was closed


def test_partial_failure_schema_and_metadata(monkeypatch, orchestrator):
    prog = []
    urls = hotel_urls(3)
    ok = {u: make_hotel(u, f"Hotel {i}") for i, u in enumerate(urls)}

    async def detail(url, params):
        if url == urls[0]:
            from src.errors import ExtractionError

            raise ExtractionError("no units found")
        return ok[url]

    fake = FakeScraperClient(urls, detail)
    patch_client(monkeypatch, fake)
    path = asyncio.run(orchestrator.run(make_params(), max_hotels=3, progress=prog.append))

    meta = orchestrator.last_run_meta
    assert meta["status"] == "completed_with_partial_failures"
    assert (meta["requested_hotels"], meta["successful_hotels"], meta["failed_hotels"]) == (3, 2, 1)

    data = json.load(open(path, encoding="utf-8"))
    assert len(data) == 2  # only successful hotels stored
    for rec in data:
        # Stable schema: no invented keys, unit missing values stay null.
        assert set(rec) == {"hotel", "units", "source_url", "collected_at", "raw_data"}
        assert rec["hotel"]["hotel_id"] != "hotel-0"
        for unit in rec["units"]:
            assert {"unit_id", "unit_name", "price_amount", "price_currency", "price_text", "unique_url"} <= set(unit)
            assert unit["plan_name"] is None  # not faked

    companion = json.load(open(orchestrator.last_meta_path, encoding="utf-8"))
    assert companion["successful_hotels"] == 2
    assert companion["units"] == sum(companion_matching_units(data))
    assert isinstance(companion["failures"], list)
    assert "params" in companion and "timing" in companion
    # Re-serialisable: must be valid JSON end-to-end.
    json.dumps(companion)


def companion_matching_units(data):
    return [len(rec["units"]) for rec in data]


def test_successful_save_uses_timestamp_style(monkeypatch, orchestrator):
    from pathlib import Path

    prog = []
    urls = hotel_urls(1)
    ok = {u: make_hotel(u, "Hotel A") for u in urls}

    async def detail(url, params):
        return ok[url]

    fake = FakeScraperClient(urls, detail)
    patch_client(monkeypatch, fake)
    path = asyncio.run(orchestrator.run(make_params(), max_hotels=1, progress=prog.append))

    stem = Path(path).stem  # booking_results_YYYYMMDD_HHMMSS
    assert len(stem) == len("booking_results_20260920_130453")
    meta_stem = Path(orchestrator.last_meta_path).stem
    assert meta_stem.startswith("booking_results_") and meta_stem.endswith(".meta")
    assert Path(path).parent == Path(str(orchestrator.storage.output_dir))