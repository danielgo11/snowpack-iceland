#!/usr/bin/env python3
"""Extract HARMONIE GRIB fields for fixed sites and write aligned SMET files."""

from __future__ import annotations

import argparse
import math
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple


MISSING_VALUE = -999.0
RETRY_ATTEMPTS = 5
RETRY_SLEEP_SECONDS = 10
# Operational requirement: before May 9 each year, force ISWR to zero outside daylight hours.
# From May 9 onward, use model ISWR values as-is.
DAYLIGHT_FORCE_CUTOFF = (5, 9)
pygrib = None
Observer = None
sun = None

TARGET_VARIABLES = {
    "2t": "2t",
    "2d": "2d",
    "2sh": "2sh",
    "10u": "10u",
    "10v": "10v",
    "i10fg": "i10fg",
    "max_i10fg": "i10fg",
    "rprate": "rprate",
    "tsrwe": "tsrwe",
    "pres": "pres",
    "dswrf": "dswrf",
    "grad": "dswrf",
    "ulwrf": "ulwrf",
    "nlwrf": "nlwrf",
    "z": "z",
}


@dataclass(frozen=True)
class Site:
    name: str
    station_id: int
    x: int
    y: int
    lat: float
    lon: float
    altitude: float
    easting: float
    northing: float
    epsg: int


SITES: Dict[str, Site] = {
    "vestfj": Site("vestfj", 1, 212, 85, 65.928064, -23.136375, 651.6, 130510.331, 7335899.118, 32628),
    "nord": Site("nord", 2, 241, 183, 65.995650, -18.648705, 730.4, 334444.510, 7324242.892, 32628),
    "austfj": Site("austfj", 3, 238, 295, 65.303028, -14.086696, 767.1, 542578.693, 7242535.463, 32628),
    "oddskard": Site("oddskard", 4, 228, 304, 65.0680046, -13.9022296, 613.2, 551633.508, 7216482.159, 32628),
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Extract GRIB data and convert directly to SMET.")
    parser.add_argument("--grib-dir", default="data/grib", help="Directory containing .grib2 files")
    parser.add_argument("--smet-dir", default="data/smet", help="Output directory for .smet files")
    return parser.parse_args()


def require_runtime_dependencies() -> None:
    global pygrib, Observer, sun
    try:
        import pygrib as _pygrib
    except ImportError as exc:
        raise SystemExit("pygrib is required. Install it before running this script.") from exc

    try:
        from astral import Observer as _Observer
        from astral.sun import sun as _sun
    except ImportError as exc:
        raise SystemExit("astral is required. Install it before running this script.") from exc

    pygrib = _pygrib
    Observer = _Observer
    sun = _sun


def _to_timestamp(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _complete_count(row: Dict[str, float]) -> int:
    return sum(1 for key in TARGET_VARIABLES.values() if key in row and row[key] is not None)


def _pick_best_row(existing: Dict[str, float], candidate: Dict[str, float]) -> Dict[str, float]:
    existing_count = _complete_count(existing)
    candidate_count = _complete_count(candidate)

    if candidate_count > existing_count:
        winner, loser = dict(candidate), existing
    else:
        winner, loser = dict(existing), candidate

    for key, value in loser.items():
        if key not in winner and value is not None:
            winner[key] = value
    return winner


def _safe_grid_value(values: Any, x: int, y: int) -> Optional[float]:
    if y < 0 or x < 0 or y >= values.shape[0] or x >= values.shape[1]:
        return None
    value = values[y, x]
    if value is None:
        return None
    try:
        fval = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(fval):
        return None
    return fval


def _read_grib_with_retries(filepath: Path):
    last_error = None
    for attempt in range(1, RETRY_ATTEMPTS + 1):
        try:
            return pygrib.open(str(filepath))
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            if attempt < RETRY_ATTEMPTS:
                print(f"Retry {attempt}/{RETRY_ATTEMPTS} for {filepath.name}: {exc}")
                time.sleep(RETRY_SLEEP_SECONDS)
    raise RuntimeError(str(last_error))


def extract_rows_from_file(filepath: Path) -> Dict[str, Dict[datetime, Dict[str, float]]]:
    per_file_rows: Dict[str, Dict[datetime, Dict[str, float]]] = defaultdict(dict)
    grbs = _read_grib_with_retries(filepath)
    try:
        for grb in grbs:
            short_name = getattr(grb, "shortName", None)
            if short_name not in TARGET_VARIABLES:
                continue

            var_name = TARGET_VARIABLES[short_name]
            valid_date = getattr(grb, "validDate", None)
            if valid_date is None:
                continue
            timestamp = _to_timestamp(valid_date)
            values = grb.values
            for site in SITES.values():
                site_rows = per_file_rows[site.name]
                row = dict(site_rows.get(timestamp, {}))
                row[var_name] = _safe_grid_value(values, site.x, site.y)
                site_rows[timestamp] = row
    finally:
        grbs.close()

    return per_file_rows


def apply_base_conversions(row: Dict[str, float]) -> Dict[str, float]:
    out = dict(row)

    if out.get("pres") is not None:
        out["pres"] = out["pres"] / 100.0

    for key in ("rprate", "tsrwe"):
        if out.get(key) is not None:
            out[key] = out[key] * 3600.0

    for key in ("dswrf", "ulwrf", "nlwrf"):
        if out.get(key) is not None and out[key] > 2000.0:
            out[key] = out[key] / 3600.0

    if out.get("z") is not None:
        out["z"] = out["z"] / 9.81

    return out


def compute_relative_humidity(temp_k: Optional[float], q: Optional[float], pressure_pa: Optional[float]) -> Optional[float]:
    if temp_k is None or q is None or pressure_pa is None:
        return None
    if q < 0:
        return None

    try:
        vapor_pressure = (q * pressure_pa) / (0.622 + 0.378 * q)
        saturation_vapor_pressure = 611.2 * math.exp((17.67 * (temp_k - 273.15)) / (temp_k - 29.65))
        rh = 100.0 * vapor_pressure / saturation_vapor_pressure
        return max(0.0, min(100.0, rh))
    except (OverflowError, ZeroDivisionError):
        return None


def compute_wind_speed_direction(u: Optional[float], v: Optional[float]) -> Tuple[Optional[float], Optional[float]]:
    if u is None or v is None:
        return None, None
    speed = math.hypot(u, v)
    direction = (270.0 - math.degrees(math.atan2(v, u))) % 360.0
    return speed, direction


def daylight_forced_iswr(site: Site, timestamp: datetime, iswr: Optional[float], daylight_cache: Dict[Tuple[str, date], Tuple[datetime, datetime]]) -> Optional[float]:
    if iswr is None:
        return None

    cutoff_dt = datetime(timestamp.year, DAYLIGHT_FORCE_CUTOFF[0], DAYLIGHT_FORCE_CUTOFF[1], tzinfo=timezone.utc)
    if timestamp >= cutoff_dt:
        return iswr

    cache_key = (site.name, timestamp.date())
    if cache_key not in daylight_cache:
        observer = Observer(latitude=site.lat, longitude=site.lon)
        try:
            sun_times = sun(observer, date=timestamp.date(), tzinfo=timezone.utc)
        except Exception:  # noqa: BLE001
            return 0.0
        sunrise = sun_times.get("sunrise") or sun_times.get("dawn")
        sunset = sun_times.get("sunset") or sun_times.get("dusk")
        if sunrise is None or sunset is None:
            return 0.0
        daylight_cache[cache_key] = (sunrise, sunset)

    sunrise, sunset = daylight_cache[cache_key]
    if timestamp < sunrise or timestamp > sunset:
        return 0.0
    return iswr


def derive_output_fields(site: Site, timestamp: datetime, row: Dict[str, float], daylight_cache: Dict[Tuple[str, date], Tuple[datetime, datetime]]) -> Dict[str, Optional[float]]:
    ta = row.get("2t")
    tsg = 273.15

    vw, dw = compute_wind_speed_direction(row.get("10u"), row.get("10v"))
    p_pa = row.get("pres") * 100.0 if row.get("pres") is not None else None
    rh = compute_relative_humidity(ta, row.get("2sh"), p_pa)

    psum = None
    if row.get("tsrwe") is not None and row.get("rprate") is not None:
        psum = row["tsrwe"] + row["rprate"]
    elif row.get("rprate") is not None:
        psum = row["rprate"]

    iswr = daylight_forced_iswr(site, timestamp, row.get("dswrf"), daylight_cache)
    ilwr = None
    if row.get("nlwrf") is not None and row.get("ulwrf") is not None:
        ilwr = row["nlwrf"] + row["ulwrf"]

    return {
        "TA": ta,
        "TSG": tsg,
        "VW": vw,
        "VW_MAX": row.get("i10fg"),
        "DW": dw,
        "RH": rh,
        "P": p_pa,
        "PSUM": psum,
        "ISWR": iswr,
        "ILWR": ilwr,
    }


def format_value(value: Optional[float]) -> str:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return "-999"
    return f"{value:.3f}"


def write_smet(site: Site, rows: Dict[datetime, Dict[str, Optional[float]]], outpath: Path) -> int:
    sorted_timestamps = sorted(rows.keys())
    outpath.parent.mkdir(parents=True, exist_ok=True)

    header = [
        "SMET 1.1 ASCII",
        "[HEADER]",
        f"station_id = {site.station_id}",
        f"station_name = {site.name}",
        f"latitude = {site.lat}",
        f"longitude = {site.lon}",
        f"altitude = {site.altitude}",
        f"easting = {site.easting}",
        f"northing = {site.northing}",
        f"epsg = {site.epsg}",
        "nodata = -999",
        "tz = 0",
        "fields = station_id timestamp TA TSG VW VW_MAX DW RH P PSUM ISWR ILWR",
        "units = station_id timestamp K K m/s m/s deg % Pa kg/m2/hr W/m2 W/m2",
        "[DATA]",
    ]

    with outpath.open("w", encoding="utf-8") as fh:
        fh.write("\n".join(header) + "\n")
        for timestamp in sorted_timestamps:
            data = rows[timestamp]
            fields = [
                str(site.station_id),
                timestamp.strftime("%Y-%m-%dT%H:%M:%S"),
                format_value(data.get("TA")),
                format_value(data.get("TSG")),
                format_value(data.get("VW")),
                format_value(data.get("VW_MAX")),
                format_value(data.get("DW")),
                format_value(data.get("RH")),
                format_value(data.get("P")),
                format_value(data.get("PSUM")),
                format_value(data.get("ISWR")),
                format_value(data.get("ILWR")),
            ]
            fh.write(" ".join(fields) + "\n")
    return len(sorted_timestamps)


def align_timestamps(site_rows: Dict[str, Dict[datetime, Dict[str, float]]]) -> Dict[str, Dict[datetime, Dict[str, float]]]:
    common_timestamps = None
    for site_name in SITES:
        timestamps = set(site_rows.get(site_name, {}).keys())
        if common_timestamps is None:
            common_timestamps = timestamps
        else:
            common_timestamps &= timestamps

    common_timestamps = common_timestamps or set()
    aligned: Dict[str, Dict[datetime, Dict[str, float]]] = {}
    for site_name in SITES:
        aligned[site_name] = {
            ts: site_rows[site_name][ts]
            for ts in site_rows.get(site_name, {})
            if ts in common_timestamps
        }
    return aligned


def process_grib_files(grib_files: Iterable[Path]) -> Tuple[Dict[str, Dict[datetime, Dict[str, float]]], List[Path], int]:
    site_rows: Dict[str, Dict[datetime, Dict[str, float]]] = defaultdict(dict)
    skipped_files: List[Path] = []
    processed_count = 0

    for filepath in sorted(grib_files):
        try:
            file_rows = extract_rows_from_file(filepath)
        except Exception as exc:  # noqa: BLE001
            skipped_files.append(filepath)
            print(f"Skipping unreadable file {filepath.name}: {exc}")
            continue

        processed_count += 1
        for site_name, rows in file_rows.items():
            for timestamp, row in rows.items():
                converted_row = apply_base_conversions(row)
                existing = site_rows[site_name].get(timestamp)
                if existing is None:
                    site_rows[site_name][timestamp] = converted_row
                else:
                    site_rows[site_name][timestamp] = _pick_best_row(existing, converted_row)

    return site_rows, skipped_files, processed_count


def main() -> None:
    args = parse_args()
    require_runtime_dependencies()
    grib_dir = Path(args.grib_dir)
    smet_dir = Path(args.smet_dir)

    if not grib_dir.exists():
        raise SystemExit(f"GRIB directory does not exist: {grib_dir}")

    grib_files = sorted(grib_dir.glob("*.grib2"))
    if not grib_files:
        raise SystemExit(f"No .grib2 files found in {grib_dir}")

    site_rows, skipped_files, processed_count = process_grib_files(grib_files)
    site_rows = align_timestamps(site_rows)

    daylight_cache: Dict[Tuple[str, date], Tuple[datetime, datetime]] = {}
    output_counts: Dict[str, int] = {}

    for site_name, site in SITES.items():
        derived_rows: Dict[datetime, Dict[str, Optional[float]]] = {}
        for timestamp, row in sorted(site_rows.get(site_name, {}).items()):
            derived_rows[timestamp] = derive_output_fields(site, timestamp, row, daylight_cache)

        outpath = smet_dir / f"{site_name}.smet"
        output_counts[site_name] = write_smet(site, derived_rows, outpath)

    print("\nSummary")
    print("-------")
    print(f"Files processed: {processed_count}")
    print(f"Files skipped: {len(skipped_files)}")
    if skipped_files:
        for path in skipped_files:
            print(f"  - {path.name}")

    for site_name in SITES:
        print(f"Rows written ({site_name}): {output_counts.get(site_name, 0)}")


if __name__ == "__main__":
    main()
