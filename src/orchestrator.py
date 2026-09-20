"""
Orchestrator for the Booking Scraper Rebuild.

Application-level robustness lives here: bounded retries, partial-failure
recovery, user cancellation, job timeout protection and structured result
metadata. The proven real-Booking retrieval path (booking_scraper/* +
src.client) is left untouched; this module only decides how to drive it.
"""

import asyncio
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

import booking_scraper.timing as bl_timing
from booking_scraper.config import (
    DETAIL_MAX_RETRIES,
    DETAIL_RETRY_BACKOFF_S,
    DETAIL_WORKERS,
    JOB_TIMEOUT_S,
)

from .client import BookingScraperClient
from .errors import PermanentError, RetryableError, SearchPhaseError
from .models import HotelSearchParams, ScrapedHotel
from .storage import Storage

ProgressCallback = Optional[Callable[[str], None]]

# Statuses exposed to the UI / result metadata.
STATUS_COMPLETED = "completed"
STATUS_PARTIAL = "completed_with_partial_failures"
STATUS_CANCELLED = "cancelled"
STATUS_FAILED = "failed"

# Cap detailed workers so we never hammer Booking.com too hard. The engine's
# default (3) is already within this range.
MAX_DETAIL_WORKERS = 4

# The search (SERP) phase is retried once; a second failure is permanent.
SEARCH_MAX_ATTEMPTS = 2


def derive_detail_workers() -> int:
    try:
        return max(1, min(int(DETAIL_WORKERS), MAX_DETAIL_WORKERS))
    except (TypeError, ValueError):
        return 3


def _retry_policy() -> Tuple[int, float]:
    """Return (max_retries, base_backoff_s) for hotel detail attempts."""
    try:
        retries = max(0, int(DETAIL_MAX_RETRIES))
    except (TypeError, ValueError):
        retries = 2
    try:
        backoff = max(0.0, float(DETAIL_RETRY_BACKOFF_S))
    except (TypeError, ValueError):
        backoff = 4.0
    return retries, backoff


def _url_slug(url: str) -> str:
    try:
        from booking_scraper.utils import hotel_key

        return hotel_key(url)
    except Exception:
        parts = [p for p in url.split("/") if p]
        return parts[-1] if parts else url


def _timing_totals() -> Dict[str, dict]:
    """Compact timing summary dict for the result metadata file."""
    totals: Dict[str, dict] = {}
    for label in bl_timing.labels():
        total, n = bl_timing.totals(label)
        totals[label] = {"total_s": round(total, 3), "count": n}
    return totals


class GuardResult:
    __slots__ = ("status", "value")

    def __init__(self, status: str, value):
        self.status = status
        self.value = value


class _RetryCounter:
    """Shared across concurrent fetch tasks so retries count across the job."""

    __slots__ = ("count",)

    def __init__(self):
        self.count = 0


class ScrapeOrchestrator:
    def __init__(self, headless: bool = True, output_dir: str = "data"):
        self.headless = headless
        self.storage = Storage(output_dir=Path(output_dir))
        self.last_run_meta: Optional[dict] = None
        # Set by save_results so callers can locate the companion meta file.
        self.last_meta_path: Optional[str] = None

    @staticmethod
    def _emit(progress: ProgressCallback, msg: str) -> None:
        """Deliver a status line to the progress callback when present,
        otherwise mirror the current CLI behaviour of printing it."""
        if progress is not None:
            progress(msg)
        else:
            print(msg)

    def _print_header(
        self,
        params: HotelSearchParams,
        max_hotels: Optional[int],
        progress: ProgressCallback,
    ):
        rooms = params.rooms
        self._emit(progress, "Searching Booking.com...")
        self._emit(progress, f"Destination: {params.destination}")
        self._emit(progress, f"Dates: {params.checkin} -> {params.checkout}")
        guests = f"{params.adults} adult{'s' if params.adults != 1 else ''}"
        if params.children:
            guests += f", {params.children} child{'ren' if params.children != 1 else ''}"
        guests += f", {rooms} room{'s' if rooms != 1 else ''}"
        self._emit(progress, f"Guests: {guests}")
        self._emit(progress, f"Currency: {params.currency}")
        self._emit(progress, f"Target hotels: {max_hotels if max_hotels else 'no limit'}")

    # ------------------------------------------------------------------
    # Main entry point
    # ------------------------------------------------------------------
    async def run(
        self,
        params: HotelSearchParams,
        max_hotels: Optional[int] = None,
        prefix: str = "booking_results",
        progress: ProgressCallback = None,
        cancel_event: Optional[asyncio.Event] = None,
        job_timeout_s: Optional[float] = None,
    ) -> str:
        """Run the full scrape (search -> details -> storage) and return the
        data-file path (empty string if nothing could be saved).

        `progress` is an optional callback receiving one status line at a time
        (printed when omitted). `cancel_event`, when set, gracefully stops
        pending work and saves whatever already completed. `job_timeout_s`
        caps the whole job (defaulting to the JOB_TIMEOUT_S setting).
        """
        self._print_header(params, max_hotels, progress)
        if job_timeout_s is None:
            try:
                job_timeout_s = max(1.0, float(JOB_TIMEOUT_S))
            except (TypeError, ValueError):
                job_timeout_s = 900.0
        deadline = time.monotonic() + job_timeout_s

        hotel_urls: List[str] = []
        hotels: List[ScrapedHotel] = []
        failures: List[dict] = []
        cancelled = False
        timed_out = False
        failure_reason: Optional[str] = None
        retry_counter = _RetryCounter()

        try:
            async with BookingScraperClient(headless=self.headless) as client:
                # ---- Search phase (bounded retry, cancel/timeout aware) ----
                guard = await self._run_guarded(
                    self._search_phase(
                        client, params, max_hotels, progress, cancel_event, retry_counter
                    ),
                    cancel_event,
                    deadline,
                )
                if guard.status == "ok":
                    hotel_urls, resolved = guard.value
                    self._emit_search_lines(params, max_hotels, hotel_urls, resolved, progress)
                elif guard.status == "cancelled":
                    cancelled = True
                else:
                    timed_out = True
                    failure_reason = f"Job exceeded {job_timeout_s:.0f}s timeout during search"

                # ---- Detail phase ----
                if not cancelled and not timed_out:
                    if len(hotel_urls) == 0:
                        failure_reason = "Search returned no hotels"
                    else:
                        hotels, failures, cancelled, timed_out, failure_reason = (
                            await self._detail_phase(
                                client,
                                params,
                                hotel_urls,
                                progress,
                                cancel_event,
                                deadline,
                                retry_counter,
                            )
                        )
        except asyncio.CancelledError:
            cancelled = True
        except SearchPhaseError as exc:
            failure_reason = str(exc)
        except Exception as exc:  # unexpected application-level failure
            import booking_scraper.logger as bl_logger

            bl_logger.log("Run failed", error=str(exc))
            failure_reason = f"unexpected failure: {exc}"

        # ------------------------------------------------------------------
        # Status derivation
        # ------------------------------------------------------------------
        if cancelled:
            status = STATUS_CANCELLED
        elif timed_out:
            status = STATUS_FAILED
            if failure_reason is None:
                failure_reason = f"Job exceeded {job_timeout_s:.0f}s timeout"
        elif len(failures) > 0:
            status = STATUS_PARTIAL
        elif len(hotels) == 0:
            status = STATUS_FAILED
            if failure_reason is None:
                failure_reason = "No hotels were extracted"
        else:
            status = STATUS_COMPLETED

        retries_total = retry_counter.count or max(
            0, sum(f.get("attempts", 0) for f in failures) - len(failures)
        )
        meta: dict = {
            "status": status,
            "requested_hotels": len(hotel_urls),
            "successful_hotels": len(hotels),
            "failed_hotels": len(failures),
            "units": sum(len(h.units) for h in hotels),
            "retries_total": retries_total,
            "cancelled": cancelled,
            "timeout_s": job_timeout_s if timed_out else None,
            "failure_reason": failure_reason,
            "params": {
                "destination": params.destination,
                "checkin": params.checkin.isoformat(),
                "checkout": params.checkout.isoformat(),
                "adults": params.adults,
                "children": params.children,
                "rooms": params.rooms,
                "currency": params.currency,
                "lang": params.lang,
            },
            "max_hotels": max_hotels,
            "destination": self._resolved_meta(client) if hotel_urls else None,
            "failures": failures,
            "timing": _timing_totals(),
        }
        self.last_run_meta = meta

        path = self.save_results(hotels, prefix=prefix, progress=progress, meta=meta)
        self._print_result_summary(hotels, failures, retries_total, path, status, progress)
        return path

    def _emit_search_lines(
        self,
        params: HotelSearchParams,
        max_hotels: Optional[int],
        hotel_urls: List[str],
        resolved: Optional[dict],
        progress: ProgressCallback,
    ):
        if resolved:
            label = resolved.get("label", "")
            country = resolved.get("country_code") or ""
            self._emit(
                progress,
                f"Destination resolved: {label}"
                + (f", {country}" if country else "")
                + f" (id {resolved.get('dest_id')}, type {resolved.get('dest_type')}, {resolved.get('source')})",
            )
        self._emit(
            progress,
            f"Search phase: {len(hotel_urls)} unique hotels "
            f"{'(max_hotels=%s reached)' % max_hotels if max_hotels and len(hotel_urls) >= max_hotels else ''}".rstrip(),
        )

    def _resolved_meta(self, client: BookingScraperClient) -> Optional[dict]:
        from .client import BookingScraperClient

        resolved = getattr(client, "last_resolved", None)
        return dict(resolved) if resolved else None

    # ------------------------------------------------------------------
    # Search phase
    # ------------------------------------------------------------------
    async def _search_phase(
        self,
        client: BookingScraperClient,
        params: HotelSearchParams,
        max_hotels: Optional[int],
        progress: ProgressCallback,
        cancel_event: Optional[asyncio.Event],
        retry_counter: "_RetryCounter",
    ) -> Tuple[List[str], Optional[dict]]:
        last_error: Optional[Exception] = None
        for attempt in range(1, SEARCH_MAX_ATTEMPTS + 1):
            if cancel_event is not None and cancel_event.is_set():
                raise asyncio.CancelledError
            try:
                urls = await client.search_hotel_urls(params, max_hotels=max_hotels)
                resolved = dict(client.last_resolved) if client.last_resolved else None
                return urls, resolved
            except (asyncio.CancelledError, asyncio.exceptions.TimeoutError):
                raise
            except SearchPhaseError as exc:
                last_error = exc
                if attempt < SEARCH_MAX_ATTEMPTS:
                    retry_counter.count += 1
                    self._emit(
                        progress,
                        f"Retrying search (attempt {attempt + 1}/{SEARCH_MAX_ATTEMPTS}): {exc}",
                    )
                    await _wait_backoff(attempt, cancel_event)
        raise SearchPhaseError(
            f"search failed after {SEARCH_MAX_ATTEMPTS} attempts: {last_error}"
        )

    # ------------------------------------------------------------------
    # Detail phase
    # ------------------------------------------------------------------
    async def _detail_phase(
        self,
        client: BookingScraperClient,
        params: HotelSearchParams,
        hotel_urls: List[str],
        progress: ProgressCallback,
        cancel_event: Optional[asyncio.Event],
        deadline: float,
        retry_counter: "_RetryCounter",
    ) -> Tuple[
        List[ScrapedHotel],
        List[dict],
        bool,
        bool,
        Optional[str],
    ]:
        workers = derive_detail_workers()
        self._emit(progress, f"Detail workers: {workers} (concurrent)")
        total = len(hotel_urls)
        max_retries, base = _retry_policy()

        semaphore = asyncio.Semaphore(workers)

        async def fetch_one(index: int, url: str):
            attempts = 0
            while True:
                if cancel_event is not None and cancel_event.is_set():
                    raise asyncio.CancelledError
                attempts += 1
                try:
                    async with semaphore:
                        detail = await client.get_hotel_detail(url, params)
                    return index, detail, None
                except (asyncio.CancelledError, asyncio.exceptions.TimeoutError):
                    raise
                except RetryableError as exc:
                    if attempts - 1 >= max_retries:
                        failure = _failure_payload(url, exc, attempts)
                        return index, None, failure
                    retry_counter.count += 1
                    self._emit(
                        progress,
                        f"Retrying hotel {index + 1}/{total} "
                        f"(attempt {attempts + 1}/{max_retries + 2}, "
                        f"wait {base * (2 ** (attempts - 1)):.0f}s): {exc}",
                    )
                    await _wait_backoff(attempts, cancel_event)
                except PermanentError as exc:
                    return index, None, _failure_payload(url, exc, attempts)
                except Exception as exc:
                    return index, None, _failure_payload(url, exc, attempts)

        tasks = [asyncio.create_task(fetch_one(i, url)) for i, url in enumerate(hotel_urls)]
        guard = await _await_guarded(tasks, cancel_event, deadline)

        out: List[Optional[tuple]] = []
        for task in tasks:
            try:
                out.append(task.result())
            except (asyncio.CancelledError, Exception):
                out.append(None)

        hotels: List[ScrapedHotel] = []
        failures: List[dict] = []
        for idx, entry in enumerate(out):
            url = hotel_urls[idx]
            slug = _url_slug(url)
            if entry is None:
                if guard == "cancelled" or guard == "timeout":
                    reason = (
                        "cancelled before completion"
                        if guard == "cancelled"
                        else "job timed out before completion"
                    )
                    failures.append(
                        {
                            "index": idx,
                            "hotel_id": slug,
                            "hotel_name": None,
                            "reason": reason,
                            "retries": 0,
                            "attempts": 0,
                        }
                    )
                    self._emit(
                        progress,
                        f"Failed hotel {idx + 1}/{total}: {slug} ({reason})",
                    )
                continue
            index, detail, fail_info = entry
            if detail is not None:
                hotels.append(detail)
                self._emit(
                    progress,
                    f"Found hotel {index + 1}/{total}: {detail.hotel.hotel_name}",
                )
            else:
                reason = fail_info.get("reason", "unknown") if fail_info else "unknown"
                failures.append(
                    {
                        "index": index,
                        "hotel_id": slug,
                        "hotel_name": None,
                        "reason": reason,
                        "retries": (fail_info.get("retries", 0) if fail_info else 0),
                        "attempts": (fail_info.get("attempts", 0) if fail_info else 0),
                    }
                )
                self._emit(
                    progress,
                    f"Failed hotel {index + 1}/{total}: {slug} ({reason})",
                )

        cancelled = guard == "cancelled"
        timed_out = guard == "timeout"
        return hotels, failures, cancelled, timed_out, None

    # ------------------------------------------------------------------
    # Output
    # ------------------------------------------------------------------
    def save_results(
        self,
        hotels: List[ScrapedHotel],
        prefix: str = "booking_results",
        progress: ProgressCallback = None,
        meta: Optional[dict] = None,
    ) -> str:
        """Save the scraped hotels and their run metadata.

        The hotel file keeps the proven top-level-list schema (unchanged). A
        companion `<prefix>_<timestamp>.meta.json` carries status, counts,
        failures and timing. Returns the data-file path ("" if nothing saved).
        """
        self.last_meta_path = None
        if not hotels:
            self._emit(progress, "No hotels were saved.")
            return ""
        with bl_timing.timed("storage.save"):
            path = self.storage.save(hotels, prefix=prefix)
        if meta is not None:
            with bl_timing.timed("storage.save_meta"):
                self.last_meta_path = str(self.storage.save_metadata(meta, prefix=prefix))
        return str(path)

    def _print_result_summary(
        self,
        hotels: List[ScrapedHotel],
        failures: List[dict],
        retries_total: int,
        path: str,
        status: str,
        progress: ProgressCallback,
    ):
        unit_count = sum(len(h.units) for h in hotels)
        self._emit(progress, "")
        status_lines = {
            STATUS_COMPLETED: "Completed.",
            STATUS_PARTIAL: "Completed with partial failures.",
            STATUS_CANCELLED: "Cancelled.",
            STATUS_FAILED: "Failed.",
        }
        self._emit(progress, status_lines.get(status, status))
        if status == STATUS_CANCELLED:
            self._emit(
                progress,
                "The search was cancelled; already-completed hotels were kept.",
            )
        self._emit(progress, f"Hotels: {len(hotels)}")
        self._emit(progress, f"Rooms/units: {unit_count}")
        if failures:
            self._emit(progress, f"Failed hotels: {len(failures)}")
            for f in failures:
                extra = f" (retries: {f['retries']})" if f.get("retries") else ""
                self._emit(
                    progress,
                    f"  - {f.get('hotel_id')}: {f.get('reason')}{extra}",
                )
        if retries_total:
            self._emit(progress, f"Retries used: {retries_total}")
        if path:
            self._emit(progress, f"Output: {path}")
            if self.last_meta_path:
                self._emit(progress, f"Metadata: {self.last_meta_path}")
        for line in bl_timing.summary():
            self._emit(progress, line)

    # ------------------------------------------------------------------
    # Guards shared by search and detail phases
    # ------------------------------------------------------------------
    async def _run_guarded(
        self,
        coro,
        cancel_event: Optional[asyncio.Event],
        deadline: float,
    ) -> GuardResult:
        """Await `coro` (a single search-phase coroutine); abort early if the
        cancel event is set or the deadline passes."""
        main = asyncio.create_task(coro)
        controls = self._controls(cancel_event, deadline)

        monitor = asyncio.create_task(
            asyncio.wait(controls, return_when=asyncio.FIRST_COMPLETED)
        )
        done, _ = await asyncio.wait(
            {main, monitor}, return_when=asyncio.FIRST_COMPLETED
        )
        if main in done:
            await self._cleanup_controls(monitor, controls)
            return GuardResult("ok", main.result())
        outcome = "cancelled" if (cancel_event is not None and cancel_event.is_set()) else "timeout"
        main.cancel()
        await asyncio.gather(main, return_exceptions=True)
        await self._cleanup_controls(monitor, controls)
        return GuardResult(outcome, None)

    @staticmethod
    def _controls(
        cancel_event: Optional[asyncio.Event], deadline: float
    ) -> List[asyncio.Task]:
        controls: List[asyncio.Task] = []
        if cancel_event is not None:
            controls.append(asyncio.create_task(cancel_event.wait()))
        controls.append(
            asyncio.create_task(asyncio.sleep(max(0.0, deadline - time.monotonic())))
        )
        return controls

    @staticmethod
    async def _cleanup_controls(monitor: asyncio.Task, controls: List[asyncio.Task]):
        monitor.cancel()
        for c in controls:
            c.cancel()
        await asyncio.gather(monitor, *controls, return_exceptions=True)


async def _await_guarded(
    tasks: List[asyncio.Task],
    cancel_event: Optional[asyncio.Event],
    deadline: float,
) -> str:
    """Wait for ALL `tasks`; abort early on cancel/timeout and stop the rest.

    Returns "ok" | "cancelled" | "timeout". On abort, unfinished tasks are
    cancelled so each attempt's finally-blocks close its own browser context
    (nothing is orphaned). Already-finished tasks keep their results."""
    if not tasks:
        return "ok"
    controls: List[asyncio.Task] = []
    if cancel_event is not None:
        controls.append(asyncio.create_task(cancel_event.wait()))
    controls.append(
        asyncio.create_task(asyncio.sleep(max(0.0, deadline - time.monotonic())))
    )

    async def _all_work() -> None:
        await asyncio.wait(tasks, return_when=asyncio.ALL_COMPLETED)

    work = asyncio.create_task(_all_work())
    monitor = asyncio.create_task(
        asyncio.wait(controls, return_when=asyncio.FIRST_COMPLETED)
    )
    done, _ = await asyncio.wait(
        {work, monitor}, return_when=asyncio.FIRST_COMPLETED
    )

    if work in done:
        outcome = "ok"
    else:
        outcome = "cancelled" if (cancel_event is not None and cancel_event.is_set()) else "timeout"
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    work.cancel()
    monitor.cancel()
    for c in controls:
        c.cancel()
    await asyncio.gather(work, monitor, *controls, return_exceptions=True)
    return outcome


async def _wait_backoff(attempt: int, cancel_event: Optional[asyncio.Event]) -> None:
    """Sleep with exponential backoff; wakes early if cancellation is set."""
    _max_retries, base = _retry_policy()
    delay = base * (2 ** (attempt - 1))
    if cancel_event is not None:
        try:
            await asyncio.wait_for(cancel_event.wait(), timeout=delay)
        except asyncio.TimeoutError:
            return
    else:
        await asyncio.sleep(delay)


def _failure_payload(url: str, exc: Exception, attempts: int) -> dict:
    return {
        "reason": str(exc),
        "retries": max(0, attempts - 1),
        "attempts": attempts,
    }