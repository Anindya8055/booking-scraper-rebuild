"""
QA validator for scraped Booking.com result files and their companion metadata.

Standalone, no scraper imports. Parses one or more `data/*.json` files (plus
their `<prefix>_<timestamp>.meta.json` companions) and checks:

  * file is valid JSON and a top-level list of hotel records
  * stable result schema (required keys, no surprises, correct types)
  * hotel -> units grouping and provenance sanity
  * no accidentally-missing REQUIRED fields (only genuinely-optional fields may
    be null, and those are reported separately, not fabricated values)

Usage:

    .venv/bin/python scripts/validate_results.py data/booking_results_*.json
    .venv/bin/python scripts/validate_results.py data/booking_results_1.json data/booking_results_2.json --meta data/booking_results_2.meta.json

Exit code 0 when all files are schema-valid; 1 otherwise. A summary is printed
either way.
"""

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path


HOTEL_REQUIRED = {
    "hotel_id": str,
    "hotel_name": str,
    "booking_url": str,
    "city": str,
    "amenities": list,
    "amenity_flags": dict,
    "images": list,
    "latitude": (float, type(None)),
    "longitude": (float, type(None)),
    "address": (str, type(None)),
    "country": (str, type(None)),
    "property_type": (str, type(None)),
    "description": (str, type(None)),
    "listing_status": (str, type(None)),
}
HOTEL_OPTIONAL_NULL = {"address", "country", "property_type", "description", "latitude", "longitude", "listing_status"}
HOTEL_OPTIONAL_MISSING = {"listing_status", "property_type", "description", "country"}

REQUIRED_TOP = {"hotel", "units", "source_url", "collected_at", "raw_data"}

UNIT_REQUIRED = {
    "unit_id": str,
    "unit_name": (str, type(None)),
    "price_amount": (float, type(None)),
    "price_currency": (str, type(None)),
    "price_text": (str, type(None)),
    "plan_name": (str, type(None)),
    "cancellation": (str, type(None)),
    "beds_text": (str, type(None)),
    "size_m2": (float, type(None)),
    "comfort_score": (float, str, type(None)),
    "amenities": list,
    "images": list,
    "price_row_text": (str, type(None)),
    "header_chips": (list, type(None)),
    "sections": (list, dict, type(None)),
    "long_desc": (str, type(None)),
    "unique_url": str,
}
# Fields Booking does not always expose; null here is NOT an extraction failure.
UNIT_OPTIONAL_NULL = {
    "unit_name", "size_m2", "beds_text", "plan_name", "cancellation", "comfort_score",
    "price_amount", "price_currency", "price_text", "long_desc", "header_chips", "sections",
}


def _check_type(name, value, expected):
    if isinstance(expected, tuple):
        return isinstance(value, expected)
    return isinstance(value, expected)


def validate_hotel(rec, path_label):
    """Return list of error strings for one hotel record; also quality notes."""
    errors = []
    missing = {}
    for key, expected in HOTEL_REQUIRED.items():
        if key not in rec:
            errors.append(f"{path_label}: hotel missing '{key}'")
            continue
        if not _check_type(key, rec[key], expected):
            errors.append(f"{path_label}: hotel['{key}'] wrong type {type(rec[key]).__name__}")
            continue
        if rec[key] is None and key not in HOTEL_OPTIONAL_NULL:
            errors.append(f"{path_label}: hotel['{key}'] is null but field is required")
    if not rec.get("hotel_id"):
        errors.append(f"{path_label}: hotel_id empty")
    name = rec.get("hotel_name") or ""
    if len(name.strip()) < 2:
        errors.append(f"{path_label}: hotel_name implausible ({name!r})")
    url = rec.get("booking_url") or ""
    if not url.startswith("https://www.booking.com/hotel/"):
        errors.append(f"{path_label}: booking_url not a booking.com hotel URL: {url!r}")
    if not (isinstance(rec.get("latitude"), (int, float)) or rec.get("latitude") is None):
        errors.append(f"{path_label}: bad latitude")
    if not (isinstance(rec.get("longitude"), (int, float)) or rec.get("longitude") is None):
        errors.append(f"{path_label}: bad longitude")
    for key in HOTEL_OPTIONAL_NULL:
        if rec.get(key) is None:
            missing.setdefault("hotel", []).append(key)
    return errors, missing


_URL_RE = re.compile(r"https://www\.booking\.com/hotel/")


def validate_unit(unit, hotel_url, path_label):
    errors = []
    missing = []
    for key, expected in UNIT_REQUIRED.items():
        if key not in unit:
            errors.append(f"{path_label}: unit missing '{key}'")
            continue
        if not _check_type(key, unit[key], expected):
            errors.append(f"{path_label}: unit['{key}'] wrong type {type(unit[key]).__name__}")
    unit_id = unit.get("unit_id") or ""
    unit_name = unit.get("unit_name")
    if not unit_id:
        errors.append(f"{path_label}: unit_id empty")
    if unit_name is None:
        missing.append("unit_name")  # genuinely absent on some rate rows
    elif len(str(unit_name).strip()) < 2:
        errors.append(f"{path_label}: unit_name implausible ({unit_name!r})")
    price_amount = unit.get("price_amount")
    price_text = unit.get("price_text")
    if price_amount is None and not price_text:
        errors.append(f"{path_label}: unit has neither price_amount nor price_text")
    currency = unit.get("price_currency")
    if price_amount is not None and not currency:
        errors.append(f"{path_label}: unit with numeric price lacks price_currency")
    if price_amount is not None and (not isinstance(price_amount, (int, float)) or price_amount < 0):
        errors.append(f"{path_label}: unit price_amount out of range ({price_amount!r})")
    uniq = unit.get("unique_url") or ""
    if not uniq.startswith(f"{hotel_url}#unit-"):
        errors.append(f"{path_label}: unique_url not anchored to hotel url ({uniq!r})")
    if "amenities" in unit and not isinstance(unit.get("amenities"), list):
        errors.append(f"{path_label}: unit amenities not a list")
    if "images" in unit and not isinstance(unit.get("images"), list):
        errors.append(f"{path_label}: unit images not a list")
    for key in UNIT_OPTIONAL_NULL:
        if unit.get(key) is None:
            missing.append(key)
    return errors, missing


def validate_file(data_path, meta_path=None):
    errors = []
    collected_missing = {"hotel": {}, "unit": {}}
    try:
        data = json.loads(Path(data_path).read_text(encoding="utf-8"))
    except Exception as exc:
        return [f"{data_path}: not valid JSON: {exc}"], None
    if not isinstance(data, list):
        return [f"{data_path}: top level is {type(data).__name__}, expected list"], None

    hotels = 0
    units = 0
    for i, rec in enumerate(data):
        label = f"{data_path}: record[{i}]"
        if not isinstance(rec, dict):
            errors.append(f"{label}: not an object")
            continue
        unexpected = set(rec) - REQUIRED_TOP
        if unexpected:
            errors.append(f"{label}: unexpected top-level keys {sorted(unexpected)}")
        for key in REQUIRED_TOP:
            if key not in rec:
                errors.append(f"{label}: missing top-level '{key}'")
        hotels += 1
        h_errors, h_missing = validate_hotel(rec.get("hotel", {}), label + ".hotel")
        errors.extend(h_errors)
        for k in h_missing.get("hotel", []):
            collected_missing["hotel"].setdefault(k, 0)
            collected_missing["hotel"][k] += 1

        src = rec.get("source_url") or ""
        if not src or not _URL_RE.match(src):
            errors.append(f"{label}: source_url not a booking.com URL")
        else:
            for p in ("checkin=", "checkout="):
                if p not in src:
                    errors.append(f"{label}: source_url lacks '{p}'")
        collected = rec.get("collected_at") or ""
        try:
            datetime.fromisoformat(collected.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            errors.append(f"{label}: collected_at not parseable ({collected!r})")

        hotel_url = (rec.get("hotel") or {}).get("booking_url") or ""
        for j, unit in enumerate(rec.get("units") or []):
            units += 1
            u_errors, u_missing = validate_unit(unit, hotel_url, f"{label}.units[{j}]")
            errors.extend(u_errors)
            for k in u_missing:
                collected_missing["unit"].setdefault(k, 0)
                collected_missing["unit"][k] += 1

    meta_info = None
    if meta_path and Path(meta_path).exists():
        meta_info = validate_meta(meta_path, errors, hotels, units)
    return errors, {"hotels": hotels, "units": units, "missing": collected_missing, "meta": meta_info}


def validate_meta(meta_path, errors, hotels, units):
    try:
        meta = json.loads(Path(meta_path).read_text(encoding="utf-8"))
    except Exception as exc:
        errors.append(f"{meta_path}: not valid JSON: {exc}")
        return None
    if not isinstance(meta, dict):
        errors.append(f"{meta_path}: not an object")
        return None
    for key in ("status", "requested_hotels", "successful_hotels", "failed_hotels", "units",
                "retries_total", "failures", "params", "timing"):
        if key not in meta:
            errors.append(f"{meta_path}: missing metadata field '{key}'")
    if meta.get("status") not in ("completed", "completed_with_partial_failures", "cancelled", "failed"):
        errors.append(f"{meta_path}: unexpected status {meta.get('status')!r}")
    for key in ("requested_hotels", "successful_hotels", "failed_hotels", "units", "retries_total"):
        if not isinstance(meta.get(key), int) or meta.get(key) < 0:
            errors.append(f"{meta_path}: {key} not a sane int")
    if not isinstance(meta.get("failures"), list):
        errors.append(f"{meta_path}: failures not a list")
    else:
        for f in meta["failures"]:
            for key in ("index", "hotel_id", "reason", "retries", "attempts"):
                if key not in f:
                    errors.append(f"{meta_path}: failure entry missing '{key}'")
    if hotels != meta.get("successful_hotels"):
        pass  # informational only (dynamic availability); reported in summary
    return {
        "status": meta.get("status"),
        "requested": meta.get("requested_hotels"),
        "successful": meta.get("successful_hotels"),
        "failed": meta.get("failed_hotels"),
        "units": meta.get("units"),
        "retries": meta.get("retries_total"),
        "n_failures": len(meta.get("failures") or []),
    }


def infer_meta(data_path: Path) -> Path | None:
    stem = data_path.stem  # prefix_timestamp
    for suf in ("meta", "metadata"):
        candidate = data_path.with_name(f"{stem}.{suf}.json")
        if candidate.exists():
            return candidate
    return None


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("files", nargs="+", type=Path)
    ap.add_argument("--meta", type=Path, action="append", default=[], help="explicit meta files")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    total_errors = []
    totals = {"files": 0, "hotels": 0, "units": 0}
    missing_totals = {"hotel": {}, "unit": {}}
    meta_rows = []
    distinct = set()

    for f in args.files:
        totals["files"] += 1
        meta_paths = [m for m in args.meta if m.exists()]
        inferred = infer_meta(f)
        if inferred is not None:
            meta_paths.append(inferred)
        errors, info = validate_file(f, meta_paths[0] if meta_paths else None)
        total_errors.extend(errors)
        if info:
            totals["hotels"] += info["hotels"]
            totals["units"] += info["units"]
            for grp, counts in info["missing"].items():
                for k, n in counts.items():
                    missing_totals[grp].setdefault(k, 0)
                    missing_totals[grp][k] += n
            if info["meta"]:
                meta_rows.append((str(f), info["meta"]))
            distinct.add(f)

    if not args.quiet:
        print(f"checked files: {totals['files']}, hotels: {totals['hotels']}, units: {totals['units']}")
        print("structural errors:", len(total_errors))
        for e in total_errors[:40]:
            print("  -", e)
        print()
        print("missing OPTIONAL fields (genuinely absent from pages, not defects):")
        for grp in ("hotel", "unit"):
            if missing_totals[grp]:
                print(f"  {grp}: " + ", ".join(f"{k}={v}" for k, v in sorted(missing_totals[grp].items())))
        if meta_rows:
            print("\nmetadata:")
            for name, m in meta_rows:
                print(f"  {name}: {m}")

    if total_errors:
        print("\nVALIDATION FAILED")
        sys.exit(1)
    print("\nVALIDATION OK")
    sys.exit(0)


if __name__ == "__main__":
    main()