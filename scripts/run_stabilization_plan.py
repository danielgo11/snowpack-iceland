#!/usr/bin/env python3
"""Structured SNOWPACK stabilization workflow runner for forecast forcing."""

from __future__ import annotations

import argparse
import csv
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run structured SNOWPACK stabilization experiments.")
    parser.add_argument("--raw-dir", default="data/raw", help="Directory containing *_raw.csv files")
    parser.add_argument("--ini-template", required=True, help="Path to base SNOWPACK ini file for the target station")
    parser.add_argument("--snowpack-bin", default="snowpack", help="SNOWPACK executable")
    parser.add_argument("--station", default="vestfj", help="Station name (smet file prefix)")
    parser.add_argument("--start", required=True, help="Simulation start timestamp (YYYY-MM-DDTHH:MM)")
    parser.add_argument("--end", required=True, help="Simulation end timestamp (YYYY-MM-DDTHH:MM)")
    parser.add_argument("--work-dir", default="data/stabilization", help="Directory where experiment artifacts are written")
    parser.add_argument("--baseline-rain-factor", type=float, default=3600.0)
    parser.add_argument("--baseline-snow-factor", type=float, default=3600.0)
    parser.add_argument("--stable-empty-snow-factor", type=float, default=5.10)
    parser.add_argument("--unstable-snow-factor", type=float, default=5.20)
    parser.add_argument("--baseline-step", type=float, default=15.0)
    parser.add_argument("--baseline-stability", default="MO_HOLTSLAG")
    parser.add_argument("--baseline-smoothing-radius", type=int, default=2)
    parser.add_argument("--conditional-cap", type=float, default=2.5, help="PSUM cap for conditional-cap experiments")
    parser.add_argument("--conditional-cap-start", default=None, help="Optional ISO start timestamp for PSUM cap window")
    parser.add_argument("--conditional-cap-end", default=None, help="Optional ISO end timestamp for PSUM cap window")
    parser.add_argument("--timestep-values", default="60,30,15", help="Comma-separated step-length matrix values")
    parser.add_argument("--stability-values", default="MO_HOLTSLAG,NEUTRAL", help="Comma-separated ATMOSPHERIC_STABILITY values")
    parser.add_argument("--smoothing-radius-values", default="1,2,3", help="Comma-separated PSUM smoothing radii")
    parser.add_argument(
        "--psum-handling-values",
        default="baseline,disable_smoothing,conditional_cap",
        help="Comma-separated PSUM handling modes",
    )
    parser.add_argument("--tag", default=None, help="Optional run tag for output directory naming")
    return parser.parse_args()


def run_command(cmd: List[str], log_path: Path) -> Tuple[int, str]:
    result = subprocess.run(cmd, capture_output=True, text=True)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    output = (result.stdout or "") + ("\n" + result.stderr if result.stderr else "")
    log_path.write_text(output, encoding="utf-8")
    return result.returncode, output


def parse_csv_values(value: str) -> List[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def parse_csv_floats(value: str) -> List[float]:
    out: List[float] = []
    for item in parse_csv_values(value):
        out.append(float(item))
    return out


def set_ini_value(lines: List[str], section: str, key: str, value: str) -> List[str]:
    section_header = f"[{section}]"
    in_section = False
    section_start = None
    section_end = len(lines)
    key_pattern = re.compile(rf"^\s*{re.escape(key)}\s*=")

    for idx, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            if stripped == section_header:
                in_section = True
                section_start = idx
                continue
            if in_section:
                section_end = idx
                break
        if in_section and key_pattern.match(stripped):
            lines[idx] = f"{key} = {value}"
            return lines

    if section_start is None:
        if lines and lines[-1].strip() != "":
            lines.append("")
        lines.extend([section_header, f"{key} = {value}"])
        return lines

    lines.insert(section_end, f"{key} = {value}")
    return lines


def apply_expert_overrides(
    ini_template: Path,
    output_ini: Path,
    smet_dir: Path,
    output_dir: Path,
    station: str,
    calculation_step_length: float,
    atmospheric_stability: str,
) -> None:
    lines = ini_template.read_text(encoding="utf-8").splitlines()
    smet_file = f"{station}.smet"

    updates = [
        ("Input", "COORDSYS", "UTM"),
        ("Input", "METEOPATH", str(smet_dir)),
        ("Input", "METEOFILE1", smet_file),
        ("InputEditing", "ENABLE_TIMESERIES_EDITING", "TRUE"),
        ("InputEditing", "*::EDIT1", "EXCLUDE"),
        ("InputEditing", "*::ARG1::PARAMS", "station_id ISWR ILWR"),
        ("InputEditing", "*::EDIT2", "CREATE"),
        ("InputEditing", "*::ARG2::PARAM", "TSG"),
        ("InputEditing", "*::ARG2::ALGORITHM", "CST"),
        ("InputEditing", "*::ARG2::VALUE", "273.15"),
        ("Snowpack", "CALCULATION_STEP_LENGTH", f"{calculation_step_length:.3f}"),
        ("Snowpack", "SW_MODE", "INCOMING"),
        ("Snowpack", "ATMOSPHERIC_STABILITY", atmospheric_stability),
        ("Snowpack", "CANOPY", "FALSE"),
        ("Snowpack", "CHANGE_BC", "TRUE"),
        ("Snowpack", "THRESH_CHANGE_BC", "-1.0"),
        ("Filters", "ENABLE_METEO_FILTERS", "TRUE"),
        ("Filters", "P::FILTER1", "MULT"),
        ("Filters", "P::ARG1::TYPE", "Cst"),
        ("Filters", "P::ARG1::CST", "100.0"),
        ("Interpolations1D", "ENABLE_RESAMPLING", "TRUE"),
        ("Interpolations1D", "PSUM::RESAMPLE1", "ACCUMULATE"),
        ("Interpolations1D", "PSUM::ARG1::PERIOD", str(int(round(calculation_step_length * 60)))),
        ("Generators", "ISWR::GENERATOR1", "CLEARSKY_SW"),
        ("Generators", "ILWR::GENERATOR1", "CLEARSKY_LW"),
        ("Generators", "ILWR::ARG1::TYPE", "Dilley"),
        ("Output", "COORDSYS", "UTM"),
        ("Output", "METEOPATH", str(output_dir)),
        ("Output", "WRITE_PROCESSED_METEO", "TRUE"),
        ("Output", "METEO", "SMET"),
        ("Output", "SNOW_WRITE", "FALSE"),
        ("Output", "PROF_WRITE", "TRUE"),
        ("Output", "PROF_FORMAT", "PRO"),
        ("Output", "TS_WRITE", "TRUE"),
        ("Output", "TS_FORMAT", "SMET"),
        ("Output", "OUT_HEAT", "TRUE"),
        ("Output", "OUT_LW", "TRUE"),
        ("Output", "OUT_SW", "TRUE"),
        ("Output", "OUT_MASS", "TRUE"),
        ("Output", "OUT_METEO", "TRUE"),
        ("Output", "OUT_STAB", "TRUE"),
    ]

    for section, key, value in updates:
        lines = set_ini_value(lines, section, key, value)

    output_ini.parent.mkdir(parents=True, exist_ok=True)
    output_ini.write_text("\n".join(lines) + "\n", encoding="utf-8")


def smet_psum_metrics(path: Path) -> Tuple[float, float]:
    lines = path.read_text(encoding="utf-8").splitlines()
    fields_line = next((ln for ln in lines if ln.startswith("fields = ")), None)
    if fields_line is None:
        return 0.0, 0.0
    fields = fields_line.split("=", 1)[1].strip().split()
    if "timestamp" not in fields or "PSUM" not in fields:
        return 0.0, 0.0
    ta_idx = fields.index("TA") if "TA" in fields else None
    ts_idx = fields.index("timestamp")
    psum_idx = fields.index("PSUM")
    data_idx = next(i for i, ln in enumerate(lines) if ln.strip() == "[DATA]")
    total_psum = 0.0
    cold_psum = 0.0
    for ln in lines[data_idx + 1 :]:
        if not ln.strip():
            continue
        cols = ln.split()
        if len(cols) <= psum_idx or cols[psum_idx] == "-999":
            continue
        psum = float(cols[psum_idx])
        if psum > 0:
            total_psum += psum
            if ta_idx is not None and len(cols) > ta_idx and cols[ta_idx] != "-999":
                if float(cols[ta_idx]) < 273.15:
                    cold_psum += psum
    return total_psum, cold_psum


def parse_failure_metrics(log_text: str) -> Tuple[Optional[str], Optional[float]]:
    ts_match = re.search(r"\[E\]\s+\[(?P<ts>[0-9:\-T]+)\]\s+\[Snowpack\.cc:1043\]\s+Temperature out of bound", log_text)
    fail_ts = ts_match.group("ts") if ts_match else None

    top_temp_patterns = [
        r"top node temperature[^-0-9]*(?P<t>-?\d+(?:\.\d+)?)",
        r"T\[0\]\s*=\s*(?P<t>-?\d+(?:\.\d+)?)",
        r"surface temperature[^-0-9]*(?P<t>-?\d+(?:\.\d+)?)",
    ]
    top_temp = None
    for pattern in top_temp_patterns:
        matches = list(re.finditer(pattern, log_text, flags=re.IGNORECASE))
        if matches:
            top_temp = float(matches[-1].group("t"))
            break
    return fail_ts, top_temp


def snow_in_pro(output_dir: Path) -> bool:
    for pro_path in output_dir.rglob("*.pro"):
        text = pro_path.read_text(encoding="utf-8", errors="ignore")
        for match in re.finditer(r"nSnowLayerData\s*=\s*(\d+)", text):
            if int(match.group(1)) > 0:
                return True
    return False


def seasonal_shape_ok(output_dir: Path) -> Optional[bool]:
    ts_candidates = [p for p in output_dir.rglob("*.smet") if p.is_file()]
    for path in ts_candidates:
        lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
        fields_line = next((ln for ln in lines if ln.startswith("fields = ")), None)
        if not fields_line:
            continue
        fields = fields_line.split("=", 1)[1].strip().split()
        if "timestamp" not in fields or "HS" not in fields:
            continue
        ts_idx = fields.index("timestamp")
        hs_idx = fields.index("HS")
        data_idx = next((i for i, ln in enumerate(lines) if ln.strip() == "[DATA]"), None)
        if data_idx is None:
            continue
        series: List[Tuple[datetime, float]] = []
        for ln in lines[data_idx + 1 :]:
            if not ln.strip():
                continue
            cols = ln.split()
            if len(cols) <= hs_idx or cols[hs_idx] == "-999":
                continue
            try:
                ts = datetime.fromisoformat(cols[ts_idx]).replace(tzinfo=timezone.utc)
                hs = float(cols[hs_idx])
            except ValueError:
                continue
            series.append((ts, hs))
        if not series:
            continue
        peak_ts, peak_hs = max(series, key=lambda item: item[1])
        last_hs = series[-1][1]
        peak_ok = peak_ts.month in {3, 4, 5}
        melt_ok = peak_hs > 0 and last_hs <= 0.1 * peak_hs
        return peak_ok and melt_ok
    return None


def build_converter_command(
    repo_root: Path,
    raw_dir: Path,
    smet_dir: Path,
    rain_factor: float,
    snow_factor: float,
    smoothing_radius: int,
    psum_mode: str,
    station: str,
    cap: float,
    cap_start: Optional[str],
    cap_end: Optional[str],
) -> List[str]:
    cmd = [
        "python3",
        str(repo_root / "convert_raw_to_smet.py"),
        "--raw-dir",
        str(raw_dir),
        "--smet-dir",
        str(smet_dir),
        "--rain-factor",
        f"{rain_factor}",
        "--snow-factor",
        f"{snow_factor}",
        "--psum-smoothing-window-radius",
        str(max(0, smoothing_radius)),
    ]
    if psum_mode == "disable_smoothing":
        cmd.append("--disable-psum-smoothing")
    elif psum_mode == "conditional_cap":
        cmd.extend(
            [
                "--conditional-psum-cap",
                f"{cap}",
                "--conditional-psum-cap-sites",
                station,
            ]
        )
        if cap_start:
            cmd.extend(["--conditional-psum-start", cap_start])
        if cap_end:
            cmd.extend(["--conditional-psum-end", cap_end])
    return cmd


def run_case(
    *,
    name: str,
    axis: str,
    axis_value: str,
    repo_root: Path,
    raw_dir: Path,
    run_root: Path,
    ini_template: Path,
    snowpack_bin: str,
    station: str,
    start: str,
    end: str,
    rain_factor: float,
    snow_factor: float,
    step_length: float,
    atmospheric_stability: str,
    smoothing_radius: int,
    psum_mode: str,
    conditional_cap: float,
    conditional_cap_start: Optional[str],
    conditional_cap_end: Optional[str],
) -> Dict[str, str]:
    smet_dir = run_root / "smet" / name
    output_dir = run_root / "output" / name
    logs_dir = run_root / "logs"
    ini_path = run_root / "config" / f"{name}.ini"

    convert_cmd = build_converter_command(
        repo_root=repo_root,
        raw_dir=raw_dir,
        smet_dir=smet_dir,
        rain_factor=rain_factor,
        snow_factor=snow_factor,
        smoothing_radius=smoothing_radius,
        psum_mode=psum_mode,
        station=station,
        cap=conditional_cap,
        cap_start=conditional_cap_start,
        cap_end=conditional_cap_end,
    )
    convert_rc, _ = run_command(convert_cmd, logs_dir / f"{name}_convert.log")
    if convert_rc != 0:
        return {
            "case": name,
            "axis": axis,
            "axis_value": axis_value,
            "status": "convert_failed",
            "first_failure_timestamp": "",
            "top_node_temperature": "",
            "total_psum": "",
            "cold_psum": "",
            "snow_in_pro": "",
            "seasonal_shape_ok": "",
            "acceptance_met": "false",
        }

    apply_expert_overrides(
        ini_template=ini_template,
        output_ini=ini_path,
        smet_dir=smet_dir,
        output_dir=output_dir,
        station=station,
        calculation_step_length=step_length,
        atmospheric_stability=atmospheric_stability,
    )

    output_dir.mkdir(parents=True, exist_ok=True)
    snowpack_cmd = [snowpack_bin, "-c", str(ini_path), "-b", start, "-e", end]
    snowpack_rc, snowpack_log = run_command(snowpack_cmd, logs_dir / f"{name}_snowpack.log")
    failure_ts, top_node_temp = parse_failure_metrics(snowpack_log)

    forcing_path = smet_dir / f"{station}.smet"
    total_psum, cold_psum = smet_psum_metrics(forcing_path) if forcing_path.exists() else (0.0, 0.0)
    has_snow = snow_in_pro(output_dir)
    shape_ok = seasonal_shape_ok(output_dir)

    complete_ok = snowpack_rc == 0
    shape_flag = bool(shape_ok)
    acceptance_met = complete_ok and has_snow and shape_flag

    return {
        "case": name,
        "axis": axis,
        "axis_value": axis_value,
        "status": "completed" if snowpack_rc == 0 else "failed",
        "first_failure_timestamp": failure_ts or "",
        "top_node_temperature": "" if top_node_temp is None else f"{top_node_temp:.3f}",
        "total_psum": f"{total_psum:.3f}",
        "cold_psum": f"{cold_psum:.3f}",
        "snow_in_pro": str(has_snow).lower(),
        "seasonal_shape_ok": "unknown" if shape_ok is None else str(shape_ok).lower(),
        "acceptance_met": str(acceptance_met).lower(),
    }


def main() -> None:
    args = parse_args()
    repo_root = Path(__file__).resolve().parents[1]
    raw_dir = Path(args.raw_dir).resolve()
    ini_template = Path(args.ini_template).resolve()
    if not raw_dir.exists():
        raise SystemExit(f"Raw directory not found: {raw_dir}")
    if not ini_template.exists():
        raise SystemExit(f"INI template not found: {ini_template}")

    timestamp = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    run_tag = args.tag or f"stabilization_{timestamp}"
    run_root = Path(args.work_dir).resolve() / run_tag
    run_root.mkdir(parents=True, exist_ok=True)

    cases: List[Dict[str, object]] = []
    cases.append(
        {
            "name": "baseline_canonical",
            "axis": "reference",
            "axis_value": "baseline",
            "snow_factor": args.baseline_snow_factor,
            "step": args.baseline_step,
            "stability": args.baseline_stability,
            "radius": args.baseline_smoothing_radius,
            "psum_mode": "baseline",
        }
    )
    cases.append(
        {
            "name": "stable_empty_reference",
            "axis": "reference",
            "axis_value": f"snow_factor={args.stable_empty_snow_factor}",
            "snow_factor": args.stable_empty_snow_factor,
            "step": args.baseline_step,
            "stability": args.baseline_stability,
            "radius": args.baseline_smoothing_radius,
            "psum_mode": "baseline",
        }
    )
    cases.append(
        {
            "name": "unstable_with_snow_reference",
            "axis": "reference",
            "axis_value": f"snow_factor={args.unstable_snow_factor}",
            "snow_factor": args.unstable_snow_factor,
            "step": args.baseline_step,
            "stability": args.baseline_stability,
            "radius": args.baseline_smoothing_radius,
            "psum_mode": "baseline",
        }
    )

    for step in parse_csv_floats(args.timestep_values):
        if abs(step - args.baseline_step) < 1e-9:
            continue
        cases.append(
            {
                "name": f"axis_timestep_{int(step)}min",
                "axis": "timestep",
                "axis_value": f"{step}",
                "snow_factor": args.baseline_snow_factor,
                "step": step,
                "stability": args.baseline_stability,
                "radius": args.baseline_smoothing_radius,
                "psum_mode": "baseline",
            }
        )

    for stability in parse_csv_values(args.stability_values):
        if stability == args.baseline_stability:
            continue
        cases.append(
            {
                "name": f"axis_stability_{stability.lower()}",
                "axis": "stability",
                "axis_value": stability,
                "snow_factor": args.baseline_snow_factor,
                "step": args.baseline_step,
                "stability": stability,
                "radius": args.baseline_smoothing_radius,
                "psum_mode": "baseline",
            }
        )

    for mode in parse_csv_values(args.psum_handling_values):
        if mode == "baseline":
            continue
        cases.append(
            {
                "name": f"axis_psum_{mode}",
                "axis": "psum_handling",
                "axis_value": mode,
                "snow_factor": args.baseline_snow_factor,
                "step": args.baseline_step,
                "stability": args.baseline_stability,
                "radius": args.baseline_smoothing_radius,
                "psum_mode": mode,
            }
        )

    for radius in parse_csv_floats(args.smoothing_radius_values):
        if int(round(radius)) == int(round(args.baseline_smoothing_radius)):
            continue
        cases.append(
            {
                "name": f"axis_smoothing_radius_{int(round(radius))}",
                "axis": "smoothing_radius",
                "axis_value": f"{int(round(radius))}",
                "snow_factor": args.baseline_snow_factor,
                "step": args.baseline_step,
                "stability": args.baseline_stability,
                "radius": int(round(radius)),
                "psum_mode": "baseline",
            }
        )

    rows: List[Dict[str, str]] = []
    for case in cases:
        print(f"Running case: {case['name']}")
        row = run_case(
            name=str(case["name"]),
            axis=str(case["axis"]),
            axis_value=str(case["axis_value"]),
            repo_root=repo_root,
            raw_dir=raw_dir,
            run_root=run_root,
            ini_template=ini_template,
            snowpack_bin=args.snowpack_bin,
            station=args.station,
            start=args.start,
            end=args.end,
            rain_factor=args.baseline_rain_factor,
            snow_factor=float(case["snow_factor"]),
            step_length=float(case["step"]),
            atmospheric_stability=str(case["stability"]),
            smoothing_radius=int(case["radius"]),
            psum_mode=str(case["psum_mode"]),
            conditional_cap=float(args.conditional_cap),
            conditional_cap_start=args.conditional_cap_start,
            conditional_cap_end=args.conditional_cap_end,
        )
        rows.append(row)

    report_path = run_root / "stabilization_report.csv"
    with report_path.open("w", newline="", encoding="utf-8") as fh:
        fieldnames = [
            "case",
            "axis",
            "axis_value",
            "status",
            "first_failure_timestamp",
            "top_node_temperature",
            "total_psum",
            "cold_psum",
            "snow_in_pro",
            "seasonal_shape_ok",
            "acceptance_met",
        ]
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    print(f"Run directory: {run_root}")
    print(f"Report: {report_path}")


if __name__ == "__main__":
    main()
