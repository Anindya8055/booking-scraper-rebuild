"""Regression test for the safe_first_text() hidden-text leak.

Booking.com sometimes renders a badge/promo element (certification badge,
Genius discount banner, rating stars) as a sibling inside the same heading
container used for the hotel name (#hp_hotel_name). A prior implementation
read raw DOM textContent, which also picks up text from elements a browser
would never actually show (e.g. an inline SVG <style> block), corrupting
hotel_name with leaked CSS/badge text. This was observed on live Booking.com
data (a "Travel Proud" SVG badge's CSS ending up inside hotel_name).

This test uses a static local HTML fixture (set_content, no network) to pin
the fix deterministically -- it does not claim to prove real Booking.com
retrieval; that is verified separately against live production data.
"""

import asyncio

import pytest

playwright_async = pytest.importorskip("playwright.async_api")

from booking_scraper.detail import safe_first_text  # noqa: E402


BADGE_HTML = """
<div id="hp_hotel_name">
  <span class="badges">
    <style>.cls-2{fill:#ff8000}.cls-3{fill:#febb02}</style>
    <svg><style>.cls-5{fill:#c00}</style></svg>
    Travel Proud
  </span>
  <div>Hotel 81 Geylang</div>
</div>
"""

PLAIN_HTML = """
<div id="hp_hotel_name">Bogota Kings 300</div>
"""


async def _extract(html: str) -> str:
    async with playwright_async.async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.set_content(f"<html><body>{html}</body></html>")
        try:
            return await safe_first_text(page, "#hp_hotel_name")
        finally:
            await browser.close()


def test_hidden_style_text_excluded_from_heading():
    name = asyncio.run(_extract(BADGE_HTML))
    assert name is not None
    assert "cls-2" not in name
    assert "fill:" not in name


def test_plain_heading_still_extracted():
    name = asyncio.run(_extract(PLAIN_HTML))
    assert name == "Bogota Kings 300"
