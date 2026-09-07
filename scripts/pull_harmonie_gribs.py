#!/usr/bin/env python3
"""Pull HARMONIE GRIB files into a local staging directory."""

from __future__ import annotations

import argparse
import shutil
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple


FILE_PREFIX = "ig-is_"


@dataclass(frozen=True)
class GribFile:
    source: Path
    cycle: datetime
    lead_hour: int
    valid_time: datetime


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Copy HARMONIE GRIB files without extraction.")
    parser.add_argument("--source-root", default="/imo/frumgogn/harmonie/ig-is/grib", help="Root with yearly source folders")
    parser.add_argument("--dest-dir", default="/imo/verk/hmat/snowpack/harmonie/data/grib", help="Destination GRIB folder")
    parser.add_argument("--run-hour", default="00", choices=["00", "03"], help="Cycle hour to select")
    parser.add_argument("--horizon-hours", type=int, default=48, help="Maximum forecast lead to keep")
    parser.add_argument("--mode", choices=["daily", "backfill"], default="daily", help="Pull mode")
    parser.add_argument("--date", help="Daily mode cycle date (YYYY-MM-DD). Default: today UTC")
    parser.add_argument("--start-date", default="2026-09-01", help="Backfill start date (YYYY-MM-DD)")
    parser.add_argument("--end-date", default="2027-07-01", help="Backfill end date (YYYY-MM-DD)")
    parser.add_argument("--years", default="2026,2027", help="Comma-separated year folders under source root")
    parser.add_argument("--dry-run", action="store_true", help="Report selections without copying")
    return parser.parse_args()


def parse_source_name(filename: str) -> Optional[Tuple[datetime, int]]:
    if not filename.startswith(FILE_PREFIX):
        return None
    core = filename[len(FILE_PREFIX):]
    if core.endswith(".grib2"):
        core = core[:-6]
    parts = core.split(".")
    if len(parts) != 2:
        return None
    cycle_raw, lead_raw = parts
    if len(cycle_raw) != 10 or len(lead_raw) != 2:
        return None
    if not (cycle_raw.isdigit() and lead_raw.isdigit()):
        return None
    cycle = datetime.strptime(cycle_raw, "%Y%m%d%H").replace(tzinfo=timezone.utc)
    lead = int(lead_raw)
    return cycle, lead


def year_paths(source_root: Path, years_csv: str) -> List[Path]:
    years = [token.strip() for token in years_csv.split(",") if token.strip()]
    paths = [source_root / year for year in years]
    return [path for path in paths if path.exists()]


def gather_candidates(
    source_root: Path,
    years_csv: str,
    run_hour: str,
    horizon: int,
    start_date: date,
    end_date: date,
) -> List[GribFile]:
    candidates: List[GribFile] = []
    for year_dir in year_paths(source_root, years_csv):
        for path in year_dir.iterdir():
            if not path.is_file():
                continue
            parsed = parse_source_name(path.name)
            if not parsed:
                continue
            cycle, lead = parsed
            if cycle.strftime("%H") != run_hour:
                continue
            if lead > horizon:
                continue
            cycle_date = cycle.date()
            if cycle_date < start_date or cycle_date > end_date:
                continue
            valid_time = cycle + timedelta(hours=lead)
            candidates.append(GribFile(path, cycle, lead, valid_time))
    return candidates


def select_latest_by_valid_time(files: Iterable[GribFile]) -> List[GribFile]:
    selected: Dict[datetime, GribFile] = {}
    for item in files:
        current = selected.get(item.valid_time)
        if current is None or item.cycle > current.cycle:
            selected[item.valid_time] = item
    return sorted(selected.values(), key=lambda item: item.valid_time)


def clean_destination(dest_dir: Path) -> None:
    dest_dir.mkdir(parents=True, exist_ok=True)
    for existing in dest_dir.glob(f"{FILE_PREFIX}*.grib2"):
        existing.unlink()


def destination_name(item: GribFile) -> str:
    return f"{FILE_PREFIX}{item.cycle.strftime('%Y%m%d%H')}.{item.lead_hour:02d}.grib2"


def copy_selected(items: Iterable[GribFile], dest_dir: Path, dry_run: bool) -> int:
    count = 0
    for item in items:
        count += 1
        outpath = dest_dir / destination_name(item)
        if dry_run:
            print(f"DRY RUN: {item.source} -> {outpath} (valid {item.valid_time.isoformat()})")
            continue
        shutil.copy2(item.source, outpath)
    return count


def parse_ymd(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


def mode_date_range(args: argparse.Namespace) -> Tuple[date, date]:
    if args.mode == "daily":
        cycle_date = parse_ymd(args.date) if args.date else datetime.now(timezone.utc).date()
        return cycle_date, cycle_date
    return parse_ymd(args.start_date), parse_ymd(args.end_date)


def main() -> None:
    args = parse_args()
    source_root = Path(args.source_root)
    dest_dir = Path(args.dest_dir)

    if not source_root.exists():
        raise SystemExit(f"Source root does not exist: {source_root}")
    if args.horizon_hours < 0:
        raise SystemExit("--horizon-hours must be >= 0")

    start_date, end_date = mode_date_range(args)
    if end_date < start_date:
        raise SystemExit("end date must be >= start date")

    if source_root.resolve() == dest_dir.resolve():
        raise SystemExit("source-root and dest-dir must be different paths")

    candidates = gather_candidates(
        source_root=source_root,
        years_csv=args.years,
        run_hour=args.run_hour,
        horizon=args.horizon_hours,
        start_date=start_date,
        end_date=end_date,
    )
    if not candidates:
        raise SystemExit("No matching GRIB files found for selection criteria.")

    selected = select_latest_by_valid_time(candidates)
    if not args.dry_run:
        clean_destination(dest_dir)
    copied = copy_selected(selected, dest_dir, args.dry_run)

    print("Summary")
    print("-------")
    print(f"Mode: {args.mode}")
    print(f"Date window: {start_date} to {end_date}")
    print(f"Run hour: {args.run_hour}")
    print(f"Horizon hours: {args.horizon_hours}")
    print(f"Candidates found: {len(candidates)}")
    print(f"Files selected (latest by valid timestamp): {len(selected)}")
    print(f"Files copied: {copied}")
    print(f"Destination: {dest_dir}")


if __name__ == "__main__":
    main()
