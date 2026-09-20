"""
Destination resolution for Booking.com searches.

A user asks for a destination as a plain string (e.g. "Bogota", "Medellin").
The scraper needs Booking's internal ``dest_id`` / ``dest_type`` pair to build
a real search URL.

Strategy:

1. Live resolution (primary): query Booking.com's own `AutoComplete` GraphQL
   endpoint exactly like the production website's search box does, from the
   same browser context. Booking answers with real destinations, including
   ``dest_id``, ``destType`` and ``countryCode``.
2. Local config fallback: if the live call is unavailable, reuse an entry in
   the repository's ``cities.booking.json`` when the name matches.

This keeps the normal CLI free of any manual city-database maintenance.
"""

from __future__ import annotations

import json
import re
import secrets
from dataclasses import dataclass
from typing import List, Optional
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

from playwright.async_api import Page

from booking_scraper.models import CityCfg
from booking_scraper.utils import load_cities

from .models import HotelSearchParams

# GraphQL operation used by Booking.com's own destination search box.
AUTOCOMPLETE_QUERY = """query AutoComplete($input: AutoCompleteRequestInput!) {
  autoCompleteSuggestions(input: $input) {
    autocompleteResponseMetadata {
      querySuggestionsEligible
      __typename
    }
    results {
      advancedSearchOutput {
        accommodations {
          adultsTotal
          autocompleteResult {
            destination {
              destId
              destType
              countryCode
              latitude
              longitude
              __typename
            }
            displayInfo {
              title
              subTitle
              showEntireHomesCheckbox
              __typename
            }
            metaData {
              webFilters
              langCode
              eligiblePages
              __typename
            }
            __typename
          }
          __typename
        }
        __typename
      }
      destination {
        destId
        destType
        countryCode
        latitude
        longitude
        __typename
      }
      displayInfo {
        title
        subTitle
        showEntireHomesCheckbox
        __typename
      }
      metaData {
        webFilters
        langCode
        eligiblePages
        __typename
      }
      __typename
    }
  }
}
"""

# Booking.com destination types (normalized to lowercase for the search URL).
_AUTOCOMPLETE_URL_PARAMS = ("aid", "label", "chal_t", "force_referer", "lang")


@dataclass
class ResolvedDestination:
    dest_id: str
    dest_type: str
    label: str
    country_code: Optional[str] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    source: str = "live"


def _graphql_endpoint(page_url: str, default_lang: str = "es") -> str:
    """Build the AutoComplete GraphQL URL reusing params from the loaded page."""
    qs = parse_qs(urlparse(page_url).query)
    keep = {}
    for k in _AUTOCOMPLETE_URL_PARAMS:
        if qs.get(k):
            keep[k] = qs[k][-1]
    keep.setdefault("lang", default_lang)
    return urlunparse(
        ("https", "www.booking.com", "/dml/graphql", "", urlencode(keep), "")
    )


def _pick_result(results: List[dict]) -> Optional[dict]:
    """Pick the best destination from Booking's AutoComplete response."""
    if not results:
        return None
    for r in results:
        dest = r.get("destination") or {}
        if dest.get("destType") == "CITY" and dest.get("destId"):
            return r
    for r in results:
        dest = r.get("destination") or {}
        if r.get("displayInfo", {}).get("title"):
            return r
    return None


def _result_to_resolved(result: dict) -> ResolvedDestination:
    dest = result.get("destination") or {}
    info = result.get("displayInfo") or {}
    return ResolvedDestination(
        dest_id=str(dest.get("destId") or "").strip(),
        dest_type=str(dest.get("destType") or "").strip().lower(),
        label=(info.get("title") or "").strip(),
        country_code=dest.get("countryCode"),
        latitude=dest.get("latitude"),
        longitude=dest.get("longitude"),
        source="live",
    )


class DestinationResolver:
    """Resolves a destination string to Booking's dest_id/dest_type."""

    def __init__(self) -> None:
        self._cache: dict[str, ResolvedDestination] = {}

    def from_config(self, query: str) -> Optional[ResolvedDestination]:
        """Fallback lookup in the local cities.booking.json config."""
        for c in load_cities():
            if c.name.strip().lower() == query.strip().lower():
                return ResolvedDestination(
                    dest_id=c.dest_id,
                    dest_type=c.dest_type,
                    label=c.name,
                    source="config",
                )
        return None

    async def resolve_live(self, page: Page, query: str, lang: str = "es") -> Optional[ResolvedDestination]:
        """Resolve using Booking.com's own AutoComplete endpoint (live data)."""
        payload = {
            "operationName": "AutoComplete",
            "variables": {
                "input": {
                    "prefixQuery": query,
                    "requestConfig": {"enableRequestContextBoost": True},
                    "requestContext": {
                        "pageviewId": secrets.token_hex(8),
                        "location": None,
                        "page": "INDEX",
                    },
                    "nbSuggestions": 5,
                    "fallbackConfig": {
                        "mergeResults": True,
                        "nbMaxMergedResults": 6,
                        "nbMaxThirdPartyResults": 3,
                        "sources": ["GOOGLE", "HERE"],
                    },
                }
            },
            "extensions": {},
            "query": AUTOCOMPLETE_QUERY,
        }
        url = _graphql_endpoint(page.url, lang)
        resp = await page.request.post(
            url,
            data=json.dumps(payload),
            headers={"content-type": "application/json"},
        )
        if resp.status != 200:
            return None
        try:
            body = await resp.json()
        except Exception:
            return None
        results = (
            (body.get("data") or {})
            .get("autoCompleteSuggestions", {})
            .get("results")
            or []
        )
        picked = _pick_result(results)
        if picked is None:
            return None
        resolved = _result_to_resolved(picked)
        if not resolved.dest_id or not resolved.dest_type:
            return None
        return resolved

    async def resolve(self, page: Page, params: HotelSearchParams) -> ResolvedDestination:
        """Return a resolved destination, caching by destination string."""
        key = params.destination.strip().lower()
        cached = self._cache.get(key)
        if cached:
            return cached
        resolved = await self.resolve_live(page, params.destination, params.lang)
        if resolved is None:
            resolved = self.from_config(params.destination)
            if resolved is None:
                raise ValueError(
                    f"Could not resolve destination '{params.destination}' on Booking.com "
                    f"and no local city config fallback was found."
                )
        self._cache[key] = resolved
        return resolved

    def cached(self, params: HotelSearchParams) -> Optional[ResolvedDestination]:
        """Return a previously resolved destination, if any (no network)."""
        return self._cache.get(params.destination.strip().lower())

    def apply_params(self, resolved: ResolvedDestination, params: HotelSearchParams) -> CityCfg:
        """Build a CityCfg for the scraper from the resolved destination."""
        city = CityCfg(
            name=resolved.label or params.destination,
            dest_id=resolved.dest_id,
            dest_type=resolved.dest_type,
            checkin=params.checkin.isoformat(),
            checkout=params.checkout.isoformat(),
            adults=params.adults,
            children=params.children,
            rooms=params.rooms,
            currency=params.currency,
            lang=params.lang,
        )
        if query := re.sub(r"\s+", " ", params.destination).strip():
            city.name = city.name or query
        return city