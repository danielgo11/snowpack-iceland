#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="/imo/vinnugogn/ofanflod/verk/hmat/snowpack/harmonie"
SOURCE_GRIB_DIR=""
STAGED_GRIB_DIR=""
SMET_DIR=""
CONFIG_DIR=""
OUTPUT_DIR=""
COPY_DIR="/imo/vinnugogn/ofanflod/verk/vakt/snowpack"
RUN_HOUR="00"
HORIZON_HOURS=48
SEASON_START=""

usage() {
  cat <<'EOF'
Usage: run_harmonie_daily.sh [options]

Options:
  --root-dir PATH          HARMONIE workspace root
  --source-grib-dir PATH   Source folder with ig-is_YYYYMMDDHH.FF files
  --grib-dir PATH          Staging GRIB folder for .grib2 files
  --smet-dir PATH          SMET output folder
  --config-dir PATH        SNOWPACK config folder
  --output-dir PATH        SNOWPACK output folder
  --copy-dir PATH          Final copy target folder
  --run-hour HH            Forecast cycle hour (default: 00)
  --horizon-hours N        Forecast horizon in hours (default: 48)
  --season-start YYYY-MM-DD  Season start date (default: current season Sep 1)
  -h, --help               Show help
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --root-dir) ROOT_DIR="$2"; shift 2 ;;
    --source-grib-dir) SOURCE_GRIB_DIR="$2"; shift 2 ;;
    --grib-dir) STAGED_GRIB_DIR="$2"; shift 2 ;;
    --smet-dir) SMET_DIR="$2"; shift 2 ;;
    --config-dir) CONFIG_DIR="$2"; shift 2 ;;
    --output-dir) OUTPUT_DIR="$2"; shift 2 ;;
    --copy-dir) COPY_DIR="$2"; shift 2 ;;
    --run-hour) RUN_HOUR="$2"; shift 2 ;;
    --horizon-hours) HORIZON_HOURS="$2"; shift 2 ;;
    --season-start) SEASON_START="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage; exit 1 ;;
  esac
done

SOURCE_GRIB_DIR="${SOURCE_GRIB_DIR:-$ROOT_DIR/gribs}"
STAGED_GRIB_DIR="${STAGED_GRIB_DIR:-$ROOT_DIR/data/grib}"
SMET_DIR="${SMET_DIR:-$ROOT_DIR/data/smet}"
CONFIG_DIR="${CONFIG_DIR:-$ROOT_DIR/config}"
OUTPUT_DIR="${OUTPUT_DIR:-$ROOT_DIR/output}"

if [[ -z "$SEASON_START" ]]; then
  year_now=$(date -u +%Y)
  month_now=$(date -u +%m)
  if [[ "$month_now" -ge 9 ]]; then
    SEASON_START="${year_now}-09-01"
  else
    SEASON_START="$((year_now - 1))-09-01"
  fi
fi
SEASON_START_COMPACT="${SEASON_START//-/}"

if [[ ! -d "$SOURCE_GRIB_DIR" ]]; then
  echo "Source GRIB directory not found: $SOURCE_GRIB_DIR" >&2
  exit 1
fi

if ! command -v snowpack >/dev/null 2>&1; then
  echo "snowpack command not found in PATH" >&2
  exit 1
fi

if [[ -f "$ROOT_DIR/extract_and_convert.py" ]]; then
  EXTRACT_SCRIPT="$ROOT_DIR/extract_and_convert.py"
elif [[ -f "$ROOT_DIR/scripts/extract_and_convert.py" ]]; then
  EXTRACT_SCRIPT="$ROOT_DIR/scripts/extract_and_convert.py"
else
  echo "extract_and_convert.py not found in $ROOT_DIR or $ROOT_DIR/scripts" >&2
  exit 1
fi

echo "[1/6] Selecting latest ${RUN_HOUR}Z run (<= ${HORIZON_HOURS}h) from $SOURCE_GRIB_DIR"
selection=$(python3 - "$SOURCE_GRIB_DIR" "$RUN_HOUR" "$HORIZON_HOURS" "$SEASON_START_COMPACT" <<'PY'
import re
import sys
from collections import defaultdict
from pathlib import Path

source_dir = Path(sys.argv[1])
run_hour = sys.argv[2]
horizon = int(sys.argv[3])
season_start = sys.argv[4]
pattern = re.compile(r"^ig-is_(\d{10})\.(\d{2})(?:\.grib2)?$")
required = set(range(horizon + 1))
leads_by_cycle = defaultdict(set)

for p in source_dir.glob("ig-is_*"):
    m = pattern.match(p.name)
    if not m:
        continue
    ymdh, ff_text = m.groups()
    ymd, hour = ymdh[:8], ymdh[8:10]
    if hour != run_hour or ymd < season_start:
        continue
    ff = int(ff_text)
    if ff <= horizon:
        leads_by_cycle[ymdh].add(ff)

complete = sorted(ymdh for ymdh, leads in leads_by_cycle.items() if required.issubset(leads))
if not complete:
    sys.exit(1)
selected = complete[-1]
for ff in sorted(required):
    print(f"{selected}.{ff:02d}")
PY
) || {
  echo "No complete eligible cycle found for ${RUN_HOUR}Z from $SEASON_START onward." >&2
  exit 1
}
readarray -t selected_members <<<"$selection"
selected_cycle="${selected_members[0]%%.*}"
echo "Selected complete cycle: $selected_cycle"

echo "[2/6] Full rebuild: clearing staged GRIB + SMET"
mkdir -p "$STAGED_GRIB_DIR" "$SMET_DIR" "$OUTPUT_DIR"
find "$STAGED_GRIB_DIR" -maxdepth 1 -type f -name '*.grib2' -delete
find "$SMET_DIR" -maxdepth 1 -type f -name '*.smet' -delete
find "$OUTPUT_DIR" -mindepth 1 -delete

echo "[3/6] Staging GRIB files"
copied=0
for member in "${selected_members[@]}"; do
  src_plain="$SOURCE_GRIB_DIR/ig-is_${member}"
  src_grib2="${src_plain}.grib2"
  ff="${member##*.}"
  if [[ -f "$src_plain" ]]; then
    cp -f "$src_plain" "$STAGED_GRIB_DIR/${selected_cycle}.${ff}.grib2"
    copied=$((copied + 1))
  elif [[ -f "$src_grib2" ]]; then
    cp -f "$src_grib2" "$STAGED_GRIB_DIR/${selected_cycle}.${ff}.grib2"
    copied=$((copied + 1))
  else
    echo "Missing required lead file for cycle $selected_cycle: ig-is_${member}[.grib2]" >&2
    exit 1
  fi
done
if [[ "$copied" -ne $((HORIZON_HOURS + 1)) ]]; then
  echo "Expected $((HORIZON_HOURS + 1)) leads, copied $copied" >&2
  exit 1
fi
echo "Staged files: $copied"

echo "[4/6] Building SMET files"
python3 "$EXTRACT_SCRIPT" --grib-dir "$STAGED_GRIB_DIR" --smet-dir "$SMET_DIR"

echo "[5/6] Applying operational SMET post-processing (ISWR fill + nord TA fallback)"
python3 - "$SMET_DIR" <<'PY'
import sys
from pathlib import Path

smet_dir = Path(sys.argv[1])

def read_smet(path: Path):
    lines = path.read_text(encoding="utf-8").splitlines()
    field_line_idx = next(i for i, ln in enumerate(lines) if ln.startswith("fields = "))
    data_idx = next(i for i, ln in enumerate(lines) if ln.strip() == "[DATA]")
    fields = lines[field_line_idx].split("=", 1)[1].strip().split()
    rows = []
    for ln in lines[data_idx + 1:]:
        if not ln.strip():
            continue
        rows.append(ln.split())
    return lines, data_idx, fields, rows

vest_path = smet_dir / "vestfj.smet"
nord_path = smet_dir / "nord.smet"
vest_ta = {}
if vest_path.exists():
    _, _, vest_fields, vest_rows = read_smet(vest_path)
    ts_idx = vest_fields.index("timestamp")
    ta_idx = vest_fields.index("TA")
    for r in vest_rows:
        vest_ta[r[ts_idx]] = r[ta_idx]

for path in smet_dir.glob("*.smet"):
    lines, data_idx, fields, rows = read_smet(path)
    iswr_idx = fields.index("ISWR")
    ts_idx = fields.index("timestamp")
    ta_idx = fields.index("TA")
    for r in rows:
        if r[iswr_idx] == "-999":
            r[iswr_idx] = "0.000"
        if path.name == "nord.smet":
            vt = vest_ta.get(r[ts_idx])
            if vt and vt != "-999":
                r[ta_idx] = f"{float(vt) - 0.512:.3f}"
    out = lines[:data_idx + 1] + [" ".join(r) for r in rows]
    path.write_text("\n".join(out) + "\n", encoding="utf-8")
PY

echo "[6/6] Running SNOWPACK and copying outputs"
run_start="${selected_cycle:0:4}-${selected_cycle:4:2}-${selected_cycle:6:2}T${selected_cycle:8:2}:00"
run_end=$(python3 - "$run_start" "$HORIZON_HOURS" <<'PY'
import sys
from datetime import datetime, timedelta
start = datetime.strptime(sys.argv[1], "%Y-%m-%dT%H:%M")
hours = int(sys.argv[2])
print((start + timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M"))
PY
)

failed=0
for smet_file in "$SMET_DIR"/*.smet; do
  [[ -e "$smet_file" ]] || continue
  station=$(basename "$smet_file" .smet)
  ini="$CONFIG_DIR/${station}.ini"
  if [[ -f "$ini" ]]; then
    echo "Running $station"
    if ! snowpack -c "$ini" -b "$run_start" -e "$run_end"; then
      echo "SNOWPACK failed for $station" >&2
      failed=1
    fi
  fi
done

if [[ "$failed" -ne 0 ]]; then
  exit 1
fi

resolved_output=$(python3 -c 'import os,sys; print(os.path.realpath(sys.argv[1]))' "$OUTPUT_DIR")
resolved_copy=$(python3 -c 'import os,sys; print(os.path.realpath(sys.argv[1]))' "$COPY_DIR")
if [[ "$resolved_output" == "$resolved_copy" ]]; then
  echo "Refusing to copy: output and destination paths are the same ($resolved_output)" >&2
  exit 1
fi

if ! find "$OUTPUT_DIR" -mindepth 1 -type f -print -quit | grep -q .; then
  echo "No output files found in $OUTPUT_DIR; refusing to publish empty results." >&2
  exit 1
fi
publish_parent=$(dirname "$COPY_DIR")
mkdir -p "$publish_parent"
tmp_publish_dir=$(mktemp -d "$publish_parent/.snowpack_publish.XXXXXX")
cp -a "$OUTPUT_DIR"/. "$tmp_publish_dir"/

backup_dir=""
if [[ -e "$COPY_DIR" ]]; then
  backup_dir="${COPY_DIR}.bak.$$"
  rm -rf "$backup_dir"
  mv "$COPY_DIR" "$backup_dir"
fi
mv "$tmp_publish_dir" "$COPY_DIR"
if [[ -n "$backup_dir" ]]; then
  rm -rf "$backup_dir"
fi
echo "Copied outputs to $COPY_DIR"
