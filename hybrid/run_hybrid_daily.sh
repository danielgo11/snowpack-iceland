#!/usr/bin/env bash
set -euo pipefail

SNOWPACK_ROOT="${SNOWPACK_ROOT:-/imo/vinnugogn/ofanflod/verk/hmat/snowpack}"
HYBRID_DIR="${HYBRID_DIR:-$SNOWPACK_ROOT/hybrid}"
HARMONIE_DIR="${HARMONIE_DIR:-$SNOWPACK_ROOT/harmonie}"

MAPPING_CSV="${MAPPING_CSV:-$HYBRID_DIR/fixed_station_mapping.csv}"
FORECAST_SITES_CSV="${FORECAST_SITES_CSV:-$HYBRID_DIR/forecast_sites.csv}"
FORECAST_SMET_DIR="${FORECAST_SMET_DIR:-$HARMONIE_DIR/data/smet}"
OBS_PROFILE_DIR="${OBS_PROFILE_DIR:-$SNOWPACK_ROOT/config}"
GENERATED_CONFIG_DIR="${GENERATED_CONFIG_DIR:-$HYBRID_DIR/generated_configs}"
HYBRID_OUTPUT_DIR="${HYBRID_OUTPUT_DIR:-$HYBRID_DIR/output}"
REPORT_CSV="${REPORT_CSV:-$HYBRID_DIR/hybrid_run_report.csv}"
LOG_DIR="${LOG_DIR:-$HYBRID_DIR/logs}"
LOG_FILE="${LOG_FILE:-$LOG_DIR/hybrid_cron.log}"
PYTHON_BIN="${PYTHON_BIN:-python3}"

mkdir -p "$LOG_DIR" "$GENERATED_CONFIG_DIR" "$HYBRID_OUTPUT_DIR"

{
  echo "[$(date -u +'%Y-%m-%dT%H:%M:%SZ')] Starting hybrid daily run"
  "$PYTHON_BIN" "$HYBRID_DIR/run_hybrid_forecast.py" \
    --mapping "$MAPPING_CSV" \
    --forecast-sites "$FORECAST_SITES_CSV" \
    --forecast-smet-dir "$FORECAST_SMET_DIR" \
    --obs-profile-dir "$OBS_PROFILE_DIR" \
    --generated-config-dir "$GENERATED_CONFIG_DIR" \
    --snowpack-output-dir "$HYBRID_OUTPUT_DIR" \
    --report-csv "$REPORT_CSV"
  rc=$?
  echo "[$(date -u +'%Y-%m-%dT%H:%M:%SZ')] Hybrid daily run finished (exit=$rc)"
  exit $rc
} >> "$LOG_FILE" 2>&1
