"""Regression test for nearby-city links leaking into property facilities.

Observed live on a Delhi property with no dedicated facilities section:
extract_amenities() fell back to sweeping every <li> on the page (see
_facility_roots), which picked up a "nearby cities" quick-link list
("Gurgaon", "Noida", "Sonipat", ...) alongside the real facilities. Live DOM
inspection of the actual page showed the distinguishing signal: every real
facility item (e.g. "Free Wifi", "Air conditioning") is plain text, while
each nearby-city item is wrapped in an <a> link. _BAD_SECTION_JS now
excludes any <li> whose content lives inside an <a>.

Uses a static local HTML fixture (set_content, no network), same pattern as
test_dom_extraction.py. No dedicated facilities root is present, so
extract_amenities() falls back to sweeping <li> across the page -- the same
fallback path that hit this live.
"""

import asyncio

import pytest

playwright_async = pytest.importorskip("playwright.async_api")

from booking_scraper.detail import extract_amenities  # noqa: E402


PAGE_WITH_NEARBY_CITY_LINKS = """
<main>
  <ul>
    <li>Free Wifi</li>
    <li>Air conditioning</li>
    <li>Private bathroom</li>
  </ul>
  <ul>
    <li><a href="/city/gurgaon.html"><span>Gurgaon</span></a></li>
    <li><a href="/city/noida.html"><span>Noida</span></a></li>
  </ul>
</main>
"""


async def _extract(html: str):
    async with playwright_async.async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.set_content(f"<html><body>{html}</body></html>")
        try:
            return await extract_amenities(page)
        finally:
            await browser.close()


def test_linked_nearby_city_names_excluded_from_amenities():
    amenities, _scarcity = asyncio.run(_extract(PAGE_WITH_NEARBY_CITY_LINKS))
    assert "Gurgaon" not in amenities
    assert "Noida" not in amenities


def test_real_plain_text_facilities_still_included():
    amenities, _scarcity = asyncio.run(_extract(PAGE_WITH_NEARBY_CITY_LINKS))
    assert "Free Wifi" in amenities
    assert "Air conditioning" in amenities
    assert "Private bathroom" in amenities
