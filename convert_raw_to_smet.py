#!/usr/bin/env python3
"""Convert extracted raw HARMONIE CSV data to SMET files."""

from __future__ import annotations

import argparse
import bisect
import csv
import math
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from statistics import median
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple


MISSING_VALUE = -999.0
Observer = None
sun = None


@dataclass(frozen=True)
class Site:
    name: str
    station_id: int
    lat: float
    lon: float
    altitude: float
    easting: float
    northing: float
    epsg: int


SITES: Dict[str, Site] = {
    "vestfj": Site("vestfj", 1, 65.928064, -23.136375, 651.6, 130510.331, 7335899.118, 32628),
    "nord": Site("nord", 2, 65.995650, -18.648705, 730.4, 334444.510, 7324242.892, 32628),
    "austfj": Site("austfj", 3, 65.303028, -14.086696, 767.1, 542578.693, 7242535.463, 32628),
    "oddskard": Site("oddskard", 4, 65.0680046, -13.9022296, 613.2, 551633.508, 7216482.159, 32628),
}

RAW_FIELDS = (
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
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Convert raw GRIB-extracted CSV data to SMET.")
    parser.add_argument("--raw-dir", default="data/raw", help="Directory with *_raw.csv files")
    parser.add_argument("--smet-dir", default="data/smet", help="Directory to write SMET files")
    parser.add_argument("--rain-factor", type=float, default=3600.0, help="Multiplier for rprate")
    parser.add_argument("--snow-factor", type=float, default=3600.0, help="Multiplier for tsrwe")
    parser.add_argument("--include-graupel", action="store_true", help="Include graupel_rate in PSUM")
    parser.add_argument("--graupel-factor", type=float, default=3600.0, help="Multiplier for graupel_rate")
    parser.add_argument("--disable-daylight-forcing", action="store_true", help="Disable ISWR daylight zeroing")
    parser.add_argument("--daylight-season-start", default="09-01", help="MM-DD daylight-forcing season start")
    parser.add_argument("--daylight-season-cutoff", default="05-09", help="MM-DD daylight-forcing season cutoff")
    parser.add_argument("--disable-psum-smoothing", action="store_true", help="Disable PSUM smoothing")
    parser.add_argument(
        "--smooth-psum-sites",
        default="vestfj",
        help="Comma-separated site names to smooth PSUM for (ignored if disabled)",
    )
    parser.add_argument("--disable-radiation-deaccum", action="store_true", help="Use raw radiation values directly")
    parser.add_argument("--radiation-scale-threshold", type=float, default=2000.0, help="If diff exceeds this, divide by 3600")
    parser.add_argument("--no-align", action="store_true", help="Do not enforce cross-site timestamp alignment")
    parser.add_argument("--disable-seasonal-summary", action="store_true", help="Disable Oct-Feb / Mar-May summary printout")
    return parser.parse_args()


def require_astral() -> None:
    global Observer, sun
    if Observer is not None and sun is not None:
        return
    try:
        from astral import Observer as _Observer
        from astral.sun import sun as _sun
    except ImportError as exc:
        raise SystemExit("astral is required unless --disable-daylight-forcing is set.") from exc
    Observer = _Observer
    sun = _sun


def parse_month_day(value: str) -> Tuple[int, int]:
    try:
        month_str, day_str = value.split("-", 1)
        month, day = int(month_str), int(day_str)
    except ValueError as exc:
        raise SystemExit(f"Invalid MM-DD value: {value}") from exc
    if not (1 <= month <= 12 and 1 <= day <= 31):
        raise SystemExit(f"Invalid MM-DD value: {value}")
    try:
        datetime(2001, month, day)
    except ValueError as exc:
        raise SystemExit(f"Invalid MM-DD value: {value}") from exc
    return month, day


def parse_optional_float(value: Optional[str]) -> Optional[float]:
    if value is None:
        return None
    text = str(value).strip()
    if text == "" or text.lower() in {"none", "nan", "-999"}:
        return None
    try:
        fval = float(text)
    except ValueError:
        return None
    if math.isnan(fval):
        return None
    return fval


def parse_optional_int(value: Optional[str]) -> Optional[int]:
    if value is None:
        return None
    text = str(value).strip()
    if text == "" or text.lower() == "none":
        return None
    try:
        return int(float(text))
    except ValueError:
        return None


def _complete_count(row: Dict[str, Any]) -> int:
    return sum(1 for key in RAW_FIELDS if row.get(key) is not None)


def _pick_best_row(existing: Dict[str, Any], candidate: Dict[str, Any]) -> Dict[str, Any]:
    existing_count = _complete_count(existing)
    candidate_count = _complete_count(candidate)
    winner = dict(candidate) if candidate_count > existing_count else dict(existing)
    loser = existing if candidate_count > existing_count else candidate
    for key, value in loser.items():
        if key not in winner or winner[key] is None:
            winner[key] = value
    return winner


def load_raw_csv(path: Path) -> Dict[datetime, Dict[str, Any]]:
    rows: Dict[datetime, Dict[str, Any]] = {}
    with path.open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        for record in reader:
            timestamp = datetime.fromisoformat(record["timestamp"])
            if timestamp.tzinfo is None:
                timestamp = timestamp.replace(tzinfo=timezone.utc)
            else:
                timestamp = timestamp.astimezone(timezone.utc)

            row: Dict[str, Any] = {
                field: parse_optional_float(record.get(field))
                for field in RAW_FIELDS
            }
            row["run_id"] = record.get("run_id")
            row["lead_hour"] = parse_optional_int(record.get("lead_hour"))

            existing = rows.get(timestamp)
            if existing is None:
                rows[timestamp] = row
            else:
                rows[timestamp] = _pick_best_row(existing, row)
    return rows


def align_timestamps(site_rows: Dict[str, Dict[datetime, Dict[str, Any]]]) -> Dict[str, Dict[datetime, Dict[str, Any]]]:
    common_timestamps: Optional[Set[datetime]] = None
    for site_name in SITES:
        timestamps = set(site_rows.get(site_name, {}).keys())
        common_timestamps = timestamps if common_timestamps is None else (common_timestamps & timestamps)
    common_timestamps = common_timestamps or set()

    aligned: Dict[str, Dict[datetime, Dict[str, Any]]] = {}
    for site_name in SITES:
        aligned[site_name] = {
            ts: row
            for ts, row in site_rows.get(site_name, {}).items()
            if ts in common_timestamps
        }
    return aligned


def deaccumulate_radiation(site_rows: Dict[str, Dict[datetime, Dict[str, Any]]], threshold: float) -> Dict[str, Dict[datetime, Dict[str, Any]]]:
    radiation_keys = ("dswrf", "ulwrf", "nlwrf")
    for rows in site_rows.values():
        grouped: Dict[str, List[Tuple[int, datetime, Dict[str, Any]]]] = {}
        for timestamp, row in rows.items():
            run_id = row.get("run_id")
            lead = row.get("lead_hour")
            key = str(run_id) if run_id is not None else f"timestamp:{timestamp.isoformat()}"
            sort_lead = int(lead) if lead is not None else 10**9
            grouped.setdefault(key, []).append((sort_lead, timestamp, row))

        for entries in grouped.values():
            entries.sort(key=lambda item: (item[0], item[1]))
            for rad_key in radiation_keys:
                prev_raw: Optional[float] = None
                for _, _, row in entries:
                    current_raw = row.get(rad_key)
                    if current_raw is None:
                        prev_raw = None
                        continue
                    value = current_raw if prev_raw is None else current_raw - prev_raw
                    prev_raw = current_raw
                    if value < 0.0:
                        value = 0.0
                    if value > threshold:
                        value = value / 3600.0
                    row[rad_key] = value
    return site_rows


def compute_relative_humidity(temp_k: Optional[float], q: Optional[float], pressure_pa: Optional[float]) -> Optional[float]:
    if temp_k is None or q is None or pressure_pa is None or q < 0:
        return None
    try:
        vapor_pressure = (q * pressure_pa) / (0.622 + 0.378 * q)
        saturation_vapor_pressure = 611.2 * math.exp((17.67 * (temp_k - 273.15)) / (temp_k - 29.65))
        rh = 100.0 * vapor_pressure / saturation_vapor_pressure
    except (OverflowError, ZeroDivisionError):
        return None
    return max(0.0, min(100.0, rh))


def compute_wind_speed_direction(u: Optional[float], v: Optional[float]) -> Tuple[Optional[float], Optional[float]]:
    if u is None or v is None:
        return None, None
    speed = math.hypot(u, v)
    direction = (270.0 - math.degrees(math.atan2(v, u))) % 360.0
    return speed, direction


def is_daylight_forcing_season(timestamp: datetime, start: Tuple[int, int], cutoff: Tuple[int, int]) -> bool:
    month_day = (timestamp.month, timestamp.day)
    if start <= cutoff:
        return start <= month_day < cutoff
    return month_day >= start or month_day < cutoff


def daylight_forced_iswr(
    site: Site,
    timestamp: datetime,
    iswr: Optional[float],
    daylight_cache: Dict[Tuple[str, date], Tuple[datetime, datetime]],
    season_start: Tuple[int, int],
    season_cutoff: Tuple[int, int],
) -> Optional[float]:
    if iswr is None:
        return None
    if not is_daylight_forcing_season(timestamp, season_start, season_cutoff):
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
    if timestamp < sunrise or timestamp >= sunset:
        return 0.0
    return iswr


def derive_output_fields(
    site: Site,
    timestamp: datetime,
    row: Dict[str, Any],
    args: argparse.Namespace,
    daylight_cache: Dict[Tuple[str, date], Tuple[datetime, datetime]],
    season_start: Tuple[int, int],
    season_cutoff: Tuple[int, int],
) -> Dict[str, Optional[float]]:
    ta = row.get("2t")
    tsg = 273.15

    vw, dw = compute_wind_speed_direction(row.get("10u"), row.get("10v"))
    p_hpa = row.get("pres") / 100.0 if row.get("pres") is not None else None
    p_pa = p_hpa * 100.0 if p_hpa is not None else None
    rh = compute_relative_humidity(ta, row.get("2sh"), p_pa)

    rain = row.get("rprate")
    snow = row.get("tsrwe")
    graupel = row.get("graupel_rate")
    rain_scaled = rain * args.rain_factor if rain is not None else None
    snow_scaled = snow * args.snow_factor if snow is not None else None
    graupel_scaled = graupel * args.graupel_factor if (args.include_graupel and graupel is not None) else None
    precip_terms = [v for v in (rain_scaled, snow_scaled, graupel_scaled) if v is not None]
    psum = sum(precip_terms) if precip_terms else None

    iswr_raw = row.get("dswrf")
    iswr = (
        daylight_forced_iswr(site, timestamp, iswr_raw, daylight_cache, season_start, season_cutoff)
        if not args.disable_daylight_forcing
        else iswr_raw
    )
    ilwr = None
    if row.get("nlwrf") is not None and row.get("ulwrf") is not None:
        ilwr = row["nlwrf"] + row["ulwrf"]  # type: ignore[operator]

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


def smooth_psum_timeseries(rows: Dict[datetime, Dict[str, Optional[float]]], window_radius: int = 2) -> Dict[datetime, Dict[str, Optional[float]]]:
    sorted_timestamps = sorted(rows.keys())
    if len(sorted_timestamps) > 1:
        step = timedelta(hours=1)
        if any((sorted_timestamps[i] - sorted_timestamps[i - 1]) != step for i in range(1, len(sorted_timestamps))):
            return rows

    raw_values: List[Optional[float]] = []
    for ts in sorted_timestamps:
        value = rows[ts].get("PSUM")
        raw_values.append(None if value is None else max(0.0, value))

    smoothed_values: List[Optional[float]] = []
    for i, value in enumerate(raw_values):
        if value is None:
            smoothed_values.append(None)
            continue
        window = [x for x in raw_values[max(0, i - window_radius) : min(len(raw_values), i + window_radius + 1)] if x is not None]
        smoothed_values.append(median(window) if window else value)

    original_sum = sum(v for v in raw_values if v is not None)
    smoothed_sum = sum(v for v in smoothed_values if v is not None)
    if smoothed_sum > 0.0:
        scale = original_sum / smoothed_sum
        smoothed_values = [None if v is None else v * scale for v in smoothed_values]

    for ts, value in zip(sorted_timestamps, smoothed_values):
        rows[ts]["PSUM"] = value
    return rows


def fill_missing_ilwr_nearest(rows: Dict[datetime, Dict[str, Optional[float]]]) -> Dict[datetime, Dict[str, Optional[float]]]:
    sorted_timestamps = sorted(rows.keys())
    valid_timestamps = [ts for ts in sorted_timestamps if rows[ts].get("ILWR") is not None]
    if not valid_timestamps:
        return rows
    for timestamp in sorted_timestamps:
        if rows[timestamp].get("ILWR") is not None:
            continue
        idx = bisect.bisect_left(valid_timestamps, timestamp)
        candidates: List[Tuple[float, datetime]] = []
        if idx > 0:
            prev_ts = valid_timestamps[idx - 1]
            candidates.append((abs((timestamp - prev_ts).total_seconds()), prev_ts))
        if idx < len(valid_timestamps):
            next_ts = valid_timestamps[idx]
            candidates.append((abs((next_ts - timestamp).total_seconds()), next_ts))
        if not candidates:
            continue
        _, best_ts = min(candidates, key=lambda item: item[0])
        rows[timestamp]["ILWR"] = rows[best_ts].get("ILWR")
    return rows


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


def seasonal_summary(rows: Dict[datetime, Dict[str, Optional[float]]]) -> Dict[str, Tuple[int, int, float, int, float]]:
    out: Dict[str, Tuple[int, int, float, int, float]] = {}
    for label, selector in (
        ("Oct-Feb", lambda t: t.month >= 10 or t.month <= 2),
        ("Mar-May", lambda t: 3 <= t.month <= 5),
    ):
        count = precip_h = cold_h = 0
        psum_sum = cold_psum = 0.0
        for ts, row in rows.items():
            if not selector(ts):
                continue
            count += 1
            ta = row.get("TA")
            psum = row.get("PSUM")
            if psum is not None and psum > 0:
                precip_h += 1
                psum_sum += psum
            if ta is not None and ta < 273.15:
                cold_h += 1
                if psum is not None and psum > 0:
                    cold_psum += psum
        out[label] = (count, precip_h, psum_sum, cold_h, cold_psum)
    return out


def load_all_sites(raw_dir: Path) -> Dict[str, Dict[datetime, Dict[str, Any]]]:
    site_rows: Dict[str, Dict[datetime, Dict[str, Any]]] = {}
    for site_name in SITES:
        csv_path = raw_dir / f"{site_name}_raw.csv"
        if not csv_path.exists():
            raise SystemExit(f"Missing raw CSV: {csv_path}")
        site_rows[site_name] = load_raw_csv(csv_path)
    return site_rows


def parse_site_set(value: str) -> Set[str]:
    names = {item.strip() for item in value.split(",") if item.strip()}
    invalid = names - set(SITES.keys())
    if invalid:
        raise SystemExit(f"Unknown site(s) in --smooth-psum-sites: {', '.join(sorted(invalid))}")
    return names


def main() -> None:
    args = parse_args()
    raw_dir = Path(args.raw_dir)
    smet_dir = Path(args.smet_dir)
    if not raw_dir.exists():
        raise SystemExit(f"Raw directory does not exist: {raw_dir}")

    season_start = parse_month_day(args.daylight_season_start)
    season_cutoff = parse_month_day(args.daylight_season_cutoff)
    smooth_sites = parse_site_set(args.smooth_psum_sites) if not args.disable_psum_smoothing else set()

    if not args.disable_daylight_forcing:
        require_astral()

    site_rows = load_all_sites(raw_dir)
    if not args.disable_radiation_deaccum:
        site_rows = deaccumulate_radiation(site_rows, threshold=args.radiation_scale_threshold)
    if not args.no_align:
        site_rows = align_timestamps(site_rows)

    daylight_cache: Dict[Tuple[str, date], Tuple[datetime, datetime]] = {}
    output_counts: Dict[str, int] = {}

    print("\nSummary")
    print("-------")
    for site_name, site in SITES.items():
        derived_rows: Dict[datetime, Dict[str, Optional[float]]] = {}
        for timestamp, row in sorted(site_rows.get(site_name, {}).items()):
            derived_rows[timestamp] = derive_output_fields(
                site=site,
                timestamp=timestamp,
                row=row,
                args=args,
                daylight_cache=daylight_cache,
                season_start=season_start,
                season_cutoff=season_cutoff,
            )

        derived_rows = fill_missing_ilwr_nearest(derived_rows)
        if site_name in smooth_sites:
            derived_rows = smooth_psum_timeseries(derived_rows)

        outpath = smet_dir / f"{site_name}.smet"
        output_counts[site_name] = write_smet(site, derived_rows, outpath)
        print(f"Rows written ({site_name}): {output_counts[site_name]}")

        if not args.disable_seasonal_summary:
            season = seasonal_summary(derived_rows)
            for label in ("Oct-Feb", "Mar-May"):
                rows_n, precip_h, psum_sum, cold_h, cold_psum = season[label]
                print(
                    f"{site_name} {label}: rows={rows_n} precip_h={precip_h} "
                    f"PSUM={psum_sum:.3f} cold_h={cold_h} cold_PSUM={cold_psum:.3f}"
                )


if __name__ == "__main__":
    main()
