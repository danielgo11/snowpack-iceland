#!/usr/bin/env python3
"""Run 48h hybrid SNOWPACK forecasts from fixed mapping + forecast SMET forcing."""

from __future__ import annotations

import argparse
import csv
import re
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional


INI_TEMPLATE = """[General]
BUFFER_SIZE = 370
BUFF_BEFORE = 1.5
BUFF_GRIDS = 10

[Input]
COORDSYS = UTM
COORDPARAM = 28N
TIME_ZONE = 0
METEO = SMET
METEOPATH = {meteo_path}
METEOFILE1 = {meteo_file}
SNOW = SMET
SNOWPATH = {snow_path}
SNOWFILE1 = {snow_file}
STARTDATE = {start_date}
ENDDATE = {end_date}

[Snowpack]
CALCULATION_STEP_LENGTH = 60
ROUGHNESS_LENGTH = 0.002
HEIGHT_OF_METEO_VALUES = 2
HEIGHT_OF_WIND_VALUE = 10
SW_MODE = INCOMING
ATMOSPHERIC_STABILITY = MO_HOLTSLAG
CHANGE_BC = FALSE
SNP_SOIL = FALSE
CANOPY = FALSE
FORCE_SW_MODE = TRUE

[Generators]
ILWR::generator = ALLSKY

[Output]
COORDSYS = UTM
COORDPARAM = 28N
TIME_ZONE = 0
METEOPATH = {output_path}
WRITE_PROCESSED_METEO = FALSE
EXPERIMENT = {experiment}
SNOW_WRITE = FALSE
PROF_WRITE = TRUE
PROF_FORMAT = PRO PRF
AGGREGATE_PRO = FALSE
AGGREGATE_PRF = FALSE
PROF_START = 0.041666
PROF_DAYS_BETWEEN = 0.041666
PROF_ID_OR_MK = ID
PROF_AGE_OR_DATE = AGE
HARDNESS_IN_NEWTON = FALSE
CLASSIFY_PROFILE = TRUE
TS_WRITE = TRUE
TS_FORMAT = SMET
"""


@dataclass
class Site:
    name: str
    station_id: int
    latitude: float
    longitude: float
    altitude_m: float


@dataclass
class Mapping:
    forecast_site: str
    obs_sensor_id: int
    obs_name: str
    distance_km: float


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mapping", type=Path, default=Path("hybrid/fixed_station_mapping.csv"))
    parser.add_argument("--forecast-sites", type=Path, default=Path("hybrid/forecast_sites.csv"))
    parser.add_argument("--forecast-smet-dir", type=Path, default=Path("data/smet"))
    parser.add_argument("--obs-profile-dir", type=Path, required=True, help="Directory with observational .sno files")
    parser.add_argument("--generated-config-dir", type=Path, default=Path("hybrid/generated_configs"))
    parser.add_argument("--snowpack-output-dir", type=Path, default=Path("output/hybrid"))
    parser.add_argument("--report-csv", type=Path, default=Path("hybrid/hybrid_run_report.csv"))
    parser.add_argument("--horizon-hours", type=int, default=48)
    parser.add_argument("--snowpack-bin", default="snowpack")
    parser.add_argument("--skip-run", action="store_true", help="Only prepare/validate; do not execute snowpack")
    return parser.parse_args()


def read_sites(path: Path) -> dict[str, Site]:
    with path.open(newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    sites: dict[str, Site] = {}
    for row in rows:
        site = Site(
            name=row["site_name"].strip(),
            station_id=int(row["station_id"]),
            latitude=float(row["latitude"]),
            longitude=float(row["longitude"]),
            altitude_m=float(row["altitude_m"]),
        )
        sites[site.name] = site
    if not sites:
        raise ValueError(f"No sites found in {path}")
    return sites


def read_mapping(path: Path) -> list[Mapping]:
    with path.open(newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    mapped: list[Mapping] = []
    for row in rows:
        mapped.append(
            Mapping(
                forecast_site=row["forecast_site"].strip(),
                obs_sensor_id=int(row["obs_sensor_id"]),
                obs_name=row["obs_name"].strip(),
                distance_km=float(row["distance_km"]),
            )
        )
    if not mapped:
        raise ValueError(f"No mappings found in {path}")
    return mapped


def parse_smet_time_bounds(smet_path: Path) -> tuple[datetime, datetime]:
    with smet_path.open(encoding="utf-8") as f:
        lines = f.readlines()
    data_idx = next((i for i, line in enumerate(lines) if line.strip() == "[DATA]"), None)
    if data_idx is None:
        raise ValueError(f"No [DATA] section in {smet_path}")
    timestamps: list[datetime] = []
    for line in lines[data_idx + 1 :]:
        line = line.strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        timestamps.append(datetime.strptime(parts[1], "%Y-%m-%dT%H:%M:%S"))
    if not timestamps:
        raise ValueError(f"No timestamp rows in {smet_path}")
    return min(timestamps), max(timestamps)


def extract_latest_timestamp_from_profile(path: Path) -> Optional[str]:
    text = path.read_text(encoding="utf-8", errors="ignore")
    matches = re.findall(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2})?", text)
    if not matches:
        return None
    latest = max(matches)
    return latest if len(latest) == 19 else f"{latest}:00"


def find_latest_profile(profile_dir: Path, sensor_id: int) -> Path:
    candidates = []
    patterns = [f"{sensor_id}.sno", f"{sensor_id}_*.sno", f"{sensor_id}*.sno"]
    for pattern in patterns:
        candidates.extend(profile_dir.glob(pattern))
    files = [p for p in candidates if p.is_file()]
    if not files:
        raise FileNotFoundError(f"No .sno profile found for sensor {sensor_id} in {profile_dir}")
    return sorted(files, key=lambda p: p.stat().st_mtime)[-1]


def render_ini(
    site: Site,
    smet_path: Path,
    profile_path: Path,
    output_dir: Path,
    run_start: datetime,
    run_end: datetime,
) -> str:
    return INI_TEMPLATE.format(
        meteo_path=str(smet_path.parent.resolve()),
        meteo_file=smet_path.name,
        snow_path=str(profile_path.parent.resolve()),
        snow_file=profile_path.name,
        start_date=run_start.strftime("%Y-%m-%dT%H:%M"),
        end_date=run_end.strftime("%Y-%m-%dT%H:%M"),
        output_path=str(output_dir.resolve()),
        experiment=f"hybrid_{site.name}",
    )


def run_snowpack(bin_name: str, ini_path: Path, run_start: datetime, run_end: datetime) -> tuple[bool, str]:
    cmd = [
        bin_name,
        "-c",
        str(ini_path),
        "-b",
        run_start.strftime("%Y-%m-%dT%H:%M"),
        "-e",
        run_end.strftime("%Y-%m-%dT%H:%M"),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    ok = result.returncode == 0
    output = (result.stdout or "") + ("\n" + result.stderr if result.stderr else "")
    return ok, output.strip()


def main() -> int:
    args = parse_args()
    args.generated_config_dir.mkdir(parents=True, exist_ok=True)
    args.snowpack_output_dir.mkdir(parents=True, exist_ok=True)
    args.report_csv.parent.mkdir(parents=True, exist_ok=True)

    if not args.skip_run and shutil.which(args.snowpack_bin) is None:
        raise SystemExit(f"Snowpack binary not found: {args.snowpack_bin}")

    sites = read_sites(args.forecast_sites)
    mappings = read_mapping(args.mapping)

    report_rows: list[dict[str, str]] = []
    failures = 0

    for item in mappings:
        site = sites.get(item.forecast_site)
        if site is None:
            failures += 1
            report_rows.append(
                {
                    "forecast_site": item.forecast_site,
                    "obs_sensor_id": str(item.obs_sensor_id),
                    "obs_name": item.obs_name,
                    "distance_km": f"{item.distance_km:.3f}",
                    "profile_path": "",
                    "profile_timestamp": "",
                    "forcing_start": "",
                    "forcing_end_available": "",
                    "forcing_end_used": "",
                    "forcing_window_ok": "false",
                    "run_success": "false",
                    "note": "forecast site missing from forecast_sites.csv",
                }
            )
            continue

        smet_path = args.forecast_smet_dir / f"{site.name}.smet"
        if not smet_path.exists():
            failures += 1
            report_rows.append(
                {
                    "forecast_site": site.name,
                    "obs_sensor_id": str(item.obs_sensor_id),
                    "obs_name": item.obs_name,
                    "distance_km": f"{item.distance_km:.3f}",
                    "profile_path": "",
                    "profile_timestamp": "",
                    "forcing_start": "",
                    "forcing_end_available": "",
                    "forcing_end_used": "",
                    "forcing_window_ok": "false",
                    "run_success": "false",
                    "note": f"missing forecast forcing: {smet_path}",
                }
            )
            continue

        try:
            profile_path = find_latest_profile(args.obs_profile_dir, item.obs_sensor_id)
            profile_timestamp = extract_latest_timestamp_from_profile(profile_path) or ""
            forcing_start, forcing_end_available = parse_smet_time_bounds(smet_path)
            forcing_end_used = forcing_start + timedelta(hours=args.horizon_hours)
            forcing_window_ok = forcing_end_available >= forcing_end_used
            if not forcing_window_ok:
                raise ValueError(
                    f"forcing window too short for {site.name}: "
                    f"{forcing_start} -> {forcing_end_available} (need through {forcing_end_used})"
                )

            ini_text = render_ini(
                site=site,
                smet_path=smet_path,
                profile_path=profile_path,
                output_dir=args.snowpack_output_dir,
                run_start=forcing_start,
                run_end=forcing_end_used,
            )
            ini_path = args.generated_config_dir / f"hybrid_{site.name}.ini"
            ini_path.write_text(ini_text, encoding="utf-8")

            if args.skip_run:
                run_success = True
                run_note = "prepared only (--skip-run)"
            else:
                run_success, run_output = run_snowpack(args.snowpack_bin, ini_path, forcing_start, forcing_end_used)
                run_note = "ok" if run_success else (run_output.splitlines()[-1] if run_output else "snowpack failed")

            if not run_success:
                failures += 1

            report_rows.append(
                {
                    "forecast_site": site.name,
                    "obs_sensor_id": str(item.obs_sensor_id),
                    "obs_name": item.obs_name,
                    "distance_km": f"{item.distance_km:.3f}",
                    "profile_path": str(profile_path.resolve()),
                    "profile_timestamp": profile_timestamp,
                    "forcing_start": forcing_start.strftime("%Y-%m-%dT%H:%M:%S"),
                    "forcing_end_available": forcing_end_available.strftime("%Y-%m-%dT%H:%M:%S"),
                    "forcing_end_used": forcing_end_used.strftime("%Y-%m-%dT%H:%M:%S"),
                    "forcing_window_ok": str(forcing_window_ok).lower(),
                    "run_success": str(run_success).lower(),
                    "note": run_note,
                }
            )
        except Exception as exc:  # noqa: BLE001
            failures += 1
            report_rows.append(
                {
                    "forecast_site": site.name,
                    "obs_sensor_id": str(item.obs_sensor_id),
                    "obs_name": item.obs_name,
                    "distance_km": f"{item.distance_km:.3f}",
                    "profile_path": "",
                    "profile_timestamp": "",
                    "forcing_start": "",
                    "forcing_end_available": "",
                    "forcing_end_used": "",
                    "forcing_window_ok": "false",
                    "run_success": "false",
                    "note": str(exc),
                }
            )

    headers = [
        "forecast_site",
        "obs_sensor_id",
        "obs_name",
        "distance_km",
        "profile_path",
        "profile_timestamp",
        "forcing_start",
        "forcing_end_available",
        "forcing_end_used",
        "forcing_window_ok",
        "run_success",
        "note",
    ]
    with args.report_csv.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=headers)
        writer.writeheader()
        writer.writerows(report_rows)

    total = len(report_rows)
    success = total - failures
    print(f"Hybrid summary: total={total}, success={success}, failed={failures}")
    print(f"Report: {args.report_csv}")
    for row in report_rows:
        print(
            f"{row['forecast_site']}: obs={row['obs_sensor_id']} "
            f"window_ok={row['forcing_window_ok']} run_success={row['run_success']} note={row['note']}"
        )

    return 0 if failures == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
