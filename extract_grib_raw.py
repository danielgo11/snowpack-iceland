#!/usr/bin/env python3
"""Extract raw HARMONIE GRIB values for fixed sites into CSV files (no conversions)."""

from __future__ import annotations

import argparse
import csv
import math
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple


RETRY_ATTEMPTS = 5
RETRY_SLEEP_SECONDS = 10
pygrib = None

SHORTNAME_MAP = {
    "2t": "2t",
    "2d": "2d",
    "2sh": "2sh",
    "10u": "10u",
    "10v": "10v",
    "i10fg": "i10fg",
    "max_i10fg": "i10fg",
    "rprate": "rprate",
    "pres": "pres",
    "dswrf": "dswrf",
    "grad": "dswrf",
    "ulwrf": "ulwrf",
    "nlwrf": "nlwrf",
    "z": "z",
}

PARAMETERNAME_EXACT_MAP = {
    "Total snowfall rate water equivalent": "tsrwe",
    "Rain precipitation rate": "rprate",
    "Graupel (snow pellets) precipitation rate": "graupel_rate",
    "Downward short-wave radiation flux": "dswrf",
    "Upward long-wave radiation flux": "ulwrf",
    "Net long-wave radiation flux": "nlwrf",
}

RAW_FIELDS = [
    "2t",
    "2d",
    "2sh",
    "10u",
    "10v",
    "i10fg",
    "rprate",
    "tsrwe",
    "graupel_rate",
    "pres",
    "dswrf",
    "ulwrf",
    "nlwrf",
    "z",
]


@dataclass(frozen=True)
class Site:
    name: str
    station_id: int
    x: int
    y: int
    lat: float
    lon: float
    altitude: float


SITES: Dict[str, Site] = {
    "vestfj": Site("vestfj", 1, 212, 85, 65.928064, -23.136375, 651.6),
    "nord": Site("nord", 2, 241, 183, 65.995650, -18.648705, 730.4),
    "austfj": Site("austfj", 3, 238, 295, 65.303028, -14.086696, 767.1),
    "oddskard": Site("oddskard", 4, 228, 304, 65.0680046, -13.9022296, 613.2),
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Extract raw GRIB values to CSV (no unit conversion).")
    parser.add_argument("--grib-dir", default="data/grib", help="Directory containing .grib2 files")
    parser.add_argument("--out-dir", default="data/raw", help="Directory for raw CSV output")
    return parser.parse_args()


def require_pygrib() -> None:
    global pygrib
    try:
        import pygrib as _pygrib
    except ImportError as exc:
        raise SystemExit("pygrib is required. Install it before running this script.") from exc
    pygrib = _pygrib


def _to_timestamp(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


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


def _parse_run_and_lead(filepath: Path) -> Tuple[Optional[str], Optional[int]]:
    stem = filepath.name
    try:
        token = stem.split("_", 1)[1].replace(".grib2", "")
        run_id, lead_str = token.split(".", 1)
        return run_id, int(lead_str)
    except (IndexError, ValueError):
        return None, None


def _resolve_var_name(grb: Any) -> Optional[str]:
    short_name = getattr(grb, "shortName", None)
    if short_name in SHORTNAME_MAP:
        return SHORTNAME_MAP[short_name]

    parameter_name = str(getattr(grb, "parameterName", "") or "")
    return PARAMETERNAME_EXACT_MAP.get(parameter_name)


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


def _complete_count(row: Dict[str, Any]) -> int:
    return sum(1 for key in RAW_FIELDS if row.get(key) is not None)


def _pick_best_row(existing: Dict[str, Any], candidate: Dict[str, Any]) -> Dict[str, Any]:
    existing_count = _complete_count(existing)
    candidate_count = _complete_count(candidate)
    if candidate_count > existing_count:
        winner, loser = dict(candidate), existing
    else:
        winner, loser = dict(existing), candidate

    for key, value in loser.items():
        if key not in winner or winner[key] is None:
            winner[key] = value
    return winner


def write_site_csv(out_dir: Path, site_name: str, rows: Dict[datetime, Dict[str, Any]]) -> int:
    out_path = out_dir / f"{site_name}_raw.csv"
    headers = ["timestamp", "run_id", "lead_hour", "source_file", *RAW_FIELDS]
    out_path.parent.mkdir(parents=True, exist_ok=True)

    count = 0
    with out_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=headers)
        writer.writeheader()
        for timestamp in sorted(rows.keys()):
            row = rows[timestamp]
            out = {
                "timestamp": timestamp.strftime("%Y-%m-%dT%H:%M:%S"),
                "run_id": row.get("run_id"),
                "lead_hour": row.get("lead_hour"),
                "source_file": row.get("source_file"),
            }
            for field in RAW_FIELDS:
                out[field] = row.get(field)
            writer.writerow(out)
            count += 1
    return count


def process_files(grib_files: Iterable[Path], out_dir: Path) -> Tuple[Dict[str, Dict[datetime, Dict[str, Any]]], int, List[Path]]:
    site_rows: Dict[str, Dict[datetime, Dict[str, Any]]] = defaultdict(dict)
    skipped_files: List[Path] = []
    processed_files = 0

    message_csv = out_dir / "raw_messages_long.csv"
    message_csv.parent.mkdir(parents=True, exist_ok=True)
    with message_csv.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(
            fh,
            fieldnames=[
                "source_file",
                "run_id",
                "lead_hour",
                "timestamp",
                "site",
                "var_name",
                "value",
                "short_name",
                "parameter_name",
                "step_type",
                "units",
                "type_of_level",
                "level",
            ],
        )
        writer.writeheader()

        for filepath in sorted(grib_files):
            try:
                grbs = _read_grib_with_retries(filepath)
            except Exception as exc:  # noqa: BLE001
                skipped_files.append(filepath)
                print(f"Skipping unreadable file {filepath.name}: {exc}")
                continue

            run_id, lead_hour = _parse_run_and_lead(filepath)
            processed_files += 1

            try:
                for grb in grbs:
                    var_name = _resolve_var_name(grb)
                    if var_name is None:
                        continue

                    valid_date = getattr(grb, "validDate", None)
                    if valid_date is None:
                        continue
                    timestamp = _to_timestamp(valid_date)

                    short_name = str(getattr(grb, "shortName", "") or "")
                    parameter_name = str(getattr(grb, "parameterName", "") or "")
                    step_type = str(getattr(grb, "stepType", "") or "")
                    units = str(getattr(grb, "units", "") or "")
                    type_of_level = str(getattr(grb, "typeOfLevel", "") or "")
                    level = str(getattr(grb, "level", "") or "")
                    values = grb.values

                    for site in SITES.values():
                        value = _safe_grid_value(values, site.x, site.y)
                        writer.writerow(
                            {
                                "source_file": filepath.name,
                                "run_id": run_id,
                                "lead_hour": lead_hour,
                                "timestamp": timestamp.strftime("%Y-%m-%dT%H:%M:%S"),
                                "site": site.name,
                                "var_name": var_name,
                                "value": value,
                                "short_name": short_name,
                                "parameter_name": parameter_name,
                                "step_type": step_type,
                                "units": units,
                                "type_of_level": type_of_level,
                                "level": level,
                            }
                        )

                        rows = site_rows[site.name]
                        candidate = dict(rows.get(timestamp, {}))
                        candidate["source_file"] = filepath.name
                        candidate["run_id"] = run_id
                        candidate["lead_hour"] = lead_hour
                        candidate[var_name] = value
                        existing = rows.get(timestamp)
                        if existing is None:
                            rows[timestamp] = candidate
                        else:
                            rows[timestamp] = _pick_best_row(existing, candidate)
            finally:
                grbs.close()

    return site_rows, processed_files, skipped_files


def main() -> None:
    args = parse_args()
    require_pygrib()

    grib_dir = Path(args.grib_dir)
    out_dir = Path(args.out_dir)

    if not grib_dir.exists():
        raise SystemExit(f"GRIB directory does not exist: {grib_dir}")

    grib_files = sorted(grib_dir.glob("*.grib2"))
    if not grib_files:
        raise SystemExit(f"No .grib2 files found in {grib_dir}")

    site_rows, processed_files, skipped_files = process_files(grib_files, out_dir)

    print("\nSummary")
    print("-------")
    print(f"Files processed: {processed_files}")
    print(f"Files skipped: {len(skipped_files)}")
    if skipped_files:
        for path in skipped_files:
            print(f"  - {path.name}")

    for site_name in SITES:
        row_count = write_site_csv(out_dir, site_name, site_rows.get(site_name, {}))
        print(f"Rows written ({site_name}): {row_count}")

    print(f"Long-format messages written: {out_dir / 'raw_messages_long.csv'}")


if __name__ == "__main__":
    main()
