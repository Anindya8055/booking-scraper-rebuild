"""Orchestrator robustness tests: retries, permanent failures and partial
recovery. All use the fake client from conftest (no real Booking traffic)."""

import asyncio
import json

import pytest

import src.orchestrator as orch_mod
from src.errors import ExtractionError, NavigationError, SearchPhaseError
from src.orchestrator import ScrapeOrchestrator, STATUS_FAILED

from conftest import FakeScraperClient, hotel_urls, make_hotel, make_params


@pytest.fixture
def orchestrator(tmp_path):
    return ScrapeOrchestrator(headless=True, output_dir=str(tmp_path))


def patch_client(monkeypatch, fake):
    monkeypatch.setattr(orch_mod, "BookingScraperClient", lambda headless=True: fake)


def test_basic_completion(monkeypatch, orchestrator):
    prog = []
    urls = hotel_urls(3)
    ok = {u: make_hotel(u, f"Hotel {i}", n_units=2) for i, u in enumerate(urls)}

    async def detail(url, params):
        return ok[url]

    fake = FakeScraperClient(urls, detail)
    patch_client(monkeypatch, fake)
    path = asyncio.run(orchestrator.run(make_params(), max_hotels=3, progress=prog.append))

    assert path.endswith(".json")
    assert orchestrator.last_run_meta["status"] == "completed"
    assert orchestrator.last_run_meta["successful_hotels"] == 3
    assert orchestrator.last_run_meta["requested_hotels"] == 3
    assert orchestrator.last_run_meta["failed_hotels"] == 0
    assert orchestrator.last_run_meta["units"] == 6

    data = json.load(open(path, encoding="utf-8"))
    assert isinstance(data, list)
    assert len(data) == 3
    for rec in data:
        assert {"hotel", "units", "source_url", "collected_at"} <= set(rec)
        assert rec["hotel"]["hotel_name"]
        assert rec["units"][0]["unit_name"]

    assert orchestrator.last_meta_path and orchestrator.last_meta_path.endswith(".meta.json")
    assert json.load(open(orchestrator.last_meta_path, encoding="utf-8"))["status"] == "completed"


def test_retry_on_transient_then_success(monkeypatch, orchestrator):
    monkeypatch.setattr(orch_mod, "DETAIL_MAX_RETRIES", 1)
    monkeypatch.setattr(orch_mod, "DETAIL_RETRY_BACKOFF_S", 0.001)
    prog = []
    urls = hotel_urls(2)
    ok = {u: make_hotel(u, f"Hotel {i}") for i, u in enumerate(urls)}
    attempts = {u: 0 for u in urls}

    async def detail(url, params):
        attempts[url] += 1
        if url == urls[0] and attempts[url] == 1:
            raise NavigationError("simulated navigation timeout")
        return ok[url]

    fake = FakeScraperClient(urls, detail)
    patch_client(monkeypatch, fake)
    path = asyncio.run(orchestrator.run(make_params(), max_hotels=2, progress=prog.append))

    assert path
    assert attempts[urls[0]] == 2  # one retry performed
    assert orchestrator.last_run_meta["status"] == "completed"
    assert orchestrator.last_run_meta["retries_total"] == 1
    assert any("Retrying hotel 1/2" in line for line in prog)


def test_permanent_failure_not_retried(monkeypatch, orchestrator):
    monkeypatch.setattr(orch_mod, "DETAIL_MAX_RETRIES", 5)
    monkeypatch.setattr(orch_mod, "DETAIL_RETRY_BACKOFF_S", 0.001)
    prog = []
    urls = hotel_urls(3)
    ok = {u: make_hotel(u, f"Hotel {i}") for i, u in enumerate(urls)}
    attempts = {u: 0 for u in urls}

    async def detail(url, params):
        attempts[url] += 1
        if url == urls[1]:
            raise ExtractionError("no units extracted")
        return ok[url]

    fake = FakeScraperClient(urls, detail)
    patch_client(monkeypatch, fake)
    path = asyncio.run(orchestrator.run(make_params(), max_hotels=3, progress=prog.append))

    assert path
    assert attempts[urls[1]] == 1  # permanent error is never retried
    meta = orchestrator.last_run_meta
    assert meta["status"] == "completed_with_partial_failures"
    assert meta["successful_hotels"] == 2
    assert meta["failed_hotels"] == 1
    assert meta["units"] == 4
    assert meta["failures"][0]["hotel_id"] == "hotel-1"
    assert meta["failures"][0]["retries"] == 0
    assert meta["failures"][0]["attempts"] == 1
    assert "Failed hotel 2/3" in " ".join(prog)


def test_retry_cap_is_bounded(monkeypatch, orchestrator):
    monkeypatch.setattr(orch_mod, "DETAIL_MAX_RETRIES", 1)
    monkeypatch.setattr(orch_mod, "DETAIL_RETRY_BACKOFF_S", 0.001)
    prog = []
    urls = hotel_urls(2)
    attempts = {u: 0 for u in urls}

    async def detail(url, params):
        attempts[url] += 1
        raise NavigationError("always transient")

    fake = FakeScraperClient(urls, detail)
    patch_client(monkeypatch, fake)
    path = asyncio.run(orchestrator.run(make_params(), max_hotels=2, progress=prog.append))

    assert not path  # nothing saved
    assert attempts[urls[0]] == 2  # initial call + 1 retry, then given up
    meta = orchestrator.last_run_meta
    assert meta["status"] == "completed_with_partial_failures"
    assert meta["failed_hotels"] == 2
    assert all(f["attempts"] == 2 for f in meta["failures"])


def test_search_phase_retry_then_success(monkeypatch, orchestrator):
    prog = []
    urls = hotel_urls(2)
    ok = {u: make_hotel(u, f"Hotel {i}") for i, u in enumerate(urls)}

    async def detail(url, params):
        return ok[url]

    fake = FakeScraperClient(urls, detail, fail_search_once=True)
    patch_client(monkeypatch, fake)
    path = asyncio.run(orchestrator.run(make_params(), max_hotels=2, progress=prog.append))

    assert path
    assert fake.search_calls == 2
    assert orchestrator.last_run_meta["status"] == "completed"
    assert any("Retrying search" in line for line in prog)


def test_search_phase_permanent_failure(monkeypatch, orchestrator):
    prog = []

    async def fail_search(params, max_hotels=None):
        raise SearchPhaseError("simulated search failure")

    fake = FakeScraperClient([], lambda url, params: None)
    fake.search_hotel_urls = fail_search
    patch_client(monkeypatch, fake)
    path = asyncio.run(orchestrator.run(make_params(), max_hotels=2, progress=prog.append))

    assert not path
    meta = orchestrator.last_run_meta
    assert meta["status"] == STATUS_FAILED
    assert "search" in (meta["failure_reason"] or "").lower()