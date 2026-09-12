#!/usr/bin/env python3
"""Build fixed nearest observational-station mapping for forecast sites."""

from __future__ import annotations

import argparse
import csv
import math
import re
from pathlib import Path


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    radius_km = 6371.0
    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlon / 2) ** 2
    return 2 * radius_km * math.asin(math.sqrt(a))


def extract_altitude_m(name: str) -> float:
    match = re.search(r"(\d+)\s*mh", name, flags=re.IGNORECASE)
    return float(match.group(1)) if match else 0.0


def load_forecast_sites(path: Path) -> list[dict]:
    with path.open(newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    required = {"site_name", "station_id", "latitude", "longitude", "altitude_m"}
    if not rows or not required.issubset(rows[0]):
        raise ValueError(f"Missing required forecast columns in {path}")
    out = []
    for row in rows:
        out.append(
            {
                "site_name": row["site_name"].strip(),
                "station_id": int(row["station_id"]),
                "latitude": float(row["latitude"]),
                "longitude": float(row["longitude"]),
                "altitude_m": float(row["altitude_m"]),
            }
        )
    return out


def load_obs_sites(path: Path) -> list[dict]:
    with path.open(newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    required = {"sensorId", "name", "latitude", "longitude"}
    if not rows or not required.issubset(rows[0]):
        raise ValueError(f"Missing required observational columns in {path}")
    out = []
    for row in rows:
        lat = float(row["latitude"])
        lon = float(row["longitude"])
        if lat == 0.0 and lon == 0.0:
            continue
        out.append(
            {
                "sensor_id": int(row["sensorId"]),
                "name": row["name"].strip(),
                "latitude": lat,
                "longitude": lon,
                "altitude_m": extract_altitude_m(row["name"]),
            }
        )
    if not out:
        raise ValueError("No valid observational stations after filtering")
    return out


def build_mapping(forecast_sites: list[dict], obs_sites: list[dict]) -> list[dict]:
    mapped = []
    for forecast in forecast_sites:
        ranked = sorted(
            obs_sites,
            key=lambda obs: haversine_km(
                forecast["latitude"], forecast["longitude"], obs["latitude"], obs["longitude"]
            ),
        )
        nearest = ranked[0]
        mapped.append(
            {
                "forecast_site": forecast["site_name"],
                "forecast_station_id": forecast["station_id"],
                "forecast_latitude": f"{forecast['latitude']:.6f}",
                "forecast_longitude": f"{forecast['longitude']:.6f}",
                "forecast_altitude_m": f"{forecast['altitude_m']:.1f}",
                "obs_sensor_id": nearest["sensor_id"],
                "obs_name": nearest["name"],
                "obs_latitude": f"{nearest['latitude']:.6f}",
                "obs_longitude": f"{nearest['longitude']:.6f}",
                "obs_altitude_m": f"{nearest['altitude_m']:.1f}",
                "distance_km": f"{haversine_km(forecast['latitude'], forecast['longitude'], nearest['latitude'], nearest['longitude']):.3f}",
            }
        )
    return mapped


def write_mapping(path: Path, rows: list[dict]) -> None:
    headers = [
        "forecast_site",
        "forecast_station_id",
        "forecast_latitude",
        "forecast_longitude",
        "forecast_altitude_m",
        "obs_sensor_id",
        "obs_name",
        "obs_latitude",
        "obs_longitude",
        "obs_altitude_m",
        "distance_km",
    ]
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=headers)
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--forecast-sites",
        type=Path,
        default=Path("hybrid/forecast_sites.csv"),
        help="CSV with forecast site metadata",
    )
    parser.add_argument(
        "--observations",
        type=Path,
        default=Path("hybrid/observational_stations.csv"),
        help="CSV with observational station metadata",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("hybrid/fixed_station_mapping.csv"),
        help="Output CSV path",
    )
    args = parser.parse_args()

    forecast_sites = load_forecast_sites(args.forecast_sites)
    obs_sites = load_obs_sites(args.observations)
    mapped = build_mapping(forecast_sites, obs_sites)
    write_mapping(args.output, mapped)

    print(f"Wrote mapping: {args.output}")
    for row in mapped:
        print(
            f"{row['forecast_site']}: sensor {row['obs_sensor_id']} "
            f"({row['obs_name']}) at {row['distance_km']} km"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
