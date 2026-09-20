"""Regression test for the quick_dismiss_overlays() latency bug.

quick_dismiss_overlays() used to call `.first.click(force=True)` on a
close-button locator with no visibility check and no explicit timeout. On a
page with no matching overlay (the common case), Playwright's default 30s
actionability timeout applied on every call -- and the function is called at
least twice per page navigation (dest resolution, search, and each hotel
detail page), turning a normal search into a multi-minute one. This was
found by comparing live Booking.com timing-instrumentation output against
the code and confirmed with a live before/after benchmark.

This test pins the fix on a static page with no overlay at all: the call
must return well under Playwright's 30s default action timeout.
"""

import asyncio
import time

import pytest

playwright_async = pytest.importorskip("playwright.async_api")

from booking_scraper.browser import quick_dismiss_overlays  # noqa: E402


async def _run() -> float:
    async with playwright_async.async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.set_content("<html><body><h1>No overlay here</h1></body></html>")
        try:
            t0 = time.monotonic()
            await quick_dismiss_overlays(page, rounds=2)
            return time.monotonic() - t0
        finally:
            await browser.close()


def test_quick_dismiss_overlays_is_fast_when_nothing_to_dismiss():
    elapsed = asyncio.run(_run())
    # The old bug made this take ~30s per round (up to 60s for rounds=2).
    # A generous ceiling well below that catches a regression without being
    # flaky on a loaded CI box.
    assert elapsed < 5.0, f"quick_dismiss_overlays took {elapsed:.1f}s -- likely the unguarded-click regression"
