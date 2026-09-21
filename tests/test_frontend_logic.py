"""Regression tests for pure client-side JS logic embedded in
src/web/index.html: facility grouping and Booking CDN image-size selection.

These functions have no DOM dependency (no `document`/`window` access), so
they're extracted verbatim from the page's <script> block by marker comments
and executed under Node -- this exercises the actual shipped code rather
than a re-implementation that could drift from it. If the markers below stop
matching, the extraction (not the logic) is what broke; update the markers
to match wherever the blocks moved to in index.html.
"""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

INDEX_HTML = Path(__file__).resolve().parents[1] / "src" / "web" / "index.html"

node = shutil.which("node")
pytestmark = pytest.mark.skipif(node is None, reason="node is not installed")


def _extract_block(start_marker: str, end_marker: str) -> str:
    text = INDEX_HTML.read_text(encoding="utf-8")
    start = text.index(start_marker)
    end = text.index(end_marker, start)
    return text[start:end]


IMAGE_SIZING_JS = _extract_block(
    "// ---- Booking CDN resolution selection",
    "function amenityChips(",
)
FACILITY_GROUPING_JS = _extract_block(
    "// ---- Facility grouping",
    "function renderFacilities(",
)


def _run_node(js_body: str, harness: str):
    script = js_body + "\n" + harness
    result = subprocess.run([node, "--input-type=commonjs"], input=script,
                             capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, f"node failed: {result.stderr}\n---\n{script}"
    return json.loads(result.stdout)


# ---------------------------------------------------------------------------
# Facility grouping
# ---------------------------------------------------------------------------

def _group(amenities):
    harness = f"""
    const result = groupFacilities({json.dumps(amenities)});
    console.log(JSON.stringify(result));
    """
    return _run_node(FACILITY_GROUPING_JS, harness)


def test_spanish_language_not_grouped_under_wellness_pool():
    # Regression: "Spanish" (Booking's "Languages spoken" list) used to land
    # in "Wellness & pool" because a plain substring check on the short key
    # "spa" also matches inside "Spanish".
    groups = _group(["Spanish", "Swimming pool", "Spa"])
    by_title = {g["title"]: g["items"] for g in groups}
    assert "Spanish" not in by_title.get("Wellness & pool", [])
    assert "Swimming pool" in by_title["Wellness & pool"]
    assert "Spa" in by_title["Wellness & pool"]
    assert "Spanish" in by_title["General"]


def test_other_known_languages_land_in_general_not_a_category():
    groups = _group(["French", "German", "Free Wifi"])
    by_title = {g["title"]: g["items"] for g in groups}
    assert set(by_title["General"]) == {"French", "German"}
    assert "Free Wifi" in by_title["Internet & workspace"]


def test_common_facilities_grouped_sensibly():
    groups = _group([
        "Free Wifi", "Outdoor swimming pool", "Restaurant", "Free parking",
        "Family rooms", "24-hour front desk",
    ])
    by_title = {g["title"]: g["items"] for g in groups}
    assert "Free Wifi" in by_title["Internet & workspace"]
    assert "Outdoor swimming pool" in by_title["Wellness & pool"]
    assert "Restaurant" in by_title["Food & drink"]
    assert "Free parking" in by_title["Parking & transport"]
    assert "Family rooms" in by_title["Family & comfort"]
    assert "24-hour front desk" in by_title["Safety & services"]


def test_front_desk_not_misfiled_under_workspace_via_desk_substring():
    # "desk" used to be a bare Internet & workspace key, so "24-hour front
    # desk" (a reception/safety fact) would substring-match "desk" and be
    # filed under Internet & workspace instead of Safety & services.
    groups = _group(["24-hour front desk"])
    by_title = {g["title"]: g["items"] for g in groups}
    assert "24-hour front desk" not in by_title.get("Internet & workspace", [])
    assert "24-hour front desk" in by_title["Safety & services"]


def test_unclassifiable_item_goes_to_general_not_invented_category():
    groups = _group(["Some totally novel amenity nobody has seen before"])
    titles = [g["title"] for g in groups]
    assert titles == ["General"]


def test_no_more_facilities_group_title_emitted():
    groups = _group(["Spanish", "Some obscure facility"])
    titles = [g["title"] for g in groups]
    assert "More facilities" not in titles
    assert "General" in titles


# ---------------------------------------------------------------------------
# Booking CDN image resolution selection
# ---------------------------------------------------------------------------

def _resize(url, size):
    harness = f"""
    console.log(JSON.stringify(bookingImageAtSize({json.dumps(url)}, {json.dumps(size)})));
    """
    return _run_node(IMAGE_SIZING_JS, harness)


def test_upgrades_low_res_thumbnail_to_requested_size():
    url = "https://cf.bstatic.com/xdata/images/hotel/square60/810347557.jpg?k=abc&o="
    assert _resize(url, "max1024x768") == (
        "https://cf.bstatic.com/xdata/images/hotel/max1024x768/810347557.jpg?k=abc&o="
    )


def test_preserves_query_string_and_id():
    url = "https://cf.bstatic.com/xdata/images/hotel/max300/999.jpg?k=xyz&o="
    assert _resize(url, "max1024x768") == (
        "https://cf.bstatic.com/xdata/images/hotel/max1024x768/999.jpg?k=xyz&o="
    )


def test_non_booking_cdn_url_passes_through_unchanged():
    url = "https://example.com/some/other/image.jpg"
    assert _resize(url, "max1024x768") == url


def test_empty_url_passes_through_unchanged():
    assert _resize("", "max1024x768") == ""
