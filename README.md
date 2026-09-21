# snowpack-iceland
SNOWPACK avalanche modeling for Iceland with IMO station data and snowpack forcing

## HARMONIE daily forecast run

Requirements:
- `snowpack` available on `PATH`
- Python with `pygrib` and `astral` installed

Use:

```bash
scripts/run_harmonie_daily.sh
```

Defaults:
- full SMET rebuild each run
- latest 00Z cycle
- 48h horizon
- output copy to `/imo/vinnugogn/ofanflod/verk/vakt/snowpack`

## Pull GRIBs only (separate from extraction)

Use:

```bash
python3 scripts/pull_harmonie_gribs.py --mode daily
```

Backfill example:

```bash
python3 scripts/pull_harmonie_gribs.py --mode backfill --start-date 2026-09-01 --end-date 2027-07-01
```

## Two-stage forecast forcing workflow (fast iteration)

1) One-time raw extraction from GRIB:

```bash
python3 extract_grib_raw.py --grib-dir data/grib --out-dir data/raw
```

2) Fast conversion/tuning from raw CSV to SMET:

```bash
python3 convert_raw_to_smet.py --raw-dir data/raw --smet-dir data/smet
```

Useful toggles for iteration:
- `--rain-factor`, `--snow-factor`
- `--include-graupel` with `--graupel-factor`
- `--disable-daylight-forcing`
- `--disable-psum-smoothing` or `--smooth-psum-sites`
- `--psum-smoothing-window-radius`
- `--conditional-psum-cap` with optional `--conditional-psum-start` / `--conditional-psum-end`
- `--disable-seasonal-summary`

## Structured stabilization workflow (vestfj / expert INI strategy)

Run the stabilization matrix with frozen baseline/reference cases and one-axis A/B tests:

```bash
python3 scripts/run_stabilization_plan.py \
  --raw-dir data/raw \
  --ini-template /absolute/path/to/vestfj.ini \
  --start 2025-09-01T00:00 \
  --end 2026-07-01T00:00
```

What it does:
- rebuilds canonical baseline forcing from raw data (no manual SMET edits)
- creates two reference cases (`stable_empty_reference`, `unstable_with_snow_reference`)
- applies expert debugging defaults in per-case INI files (15 min step, pressure scaling filter, processed meteo + flux outputs, generator/input-editing settings)
- runs one-axis matrix tests (timestep, stability mode, PSUM handling, smoothing radius)
- writes a summary CSV with:
  - first failure timestamp
  - top-node temperature (if present in logs)
  - total PSUM and cold-season PSUM
  - whether snow appears in PRO
  - acceptance criteria flag

## Hybrid daily operational run

Hybrid runner script:

```bash
hybrid/run_hybrid_daily.sh
```

Default runtime paths used by the wrapper:
- snowpack root: `/imo/vinnugogn/ofanflod/verk/hmat/snowpack`
- forecast SMET: `/imo/vinnugogn/ofanflod/verk/hmat/snowpack/harmonie/data/smet`
- observational profiles: `/imo/vinnugogn/ofanflod/verk/hmat/snowpack/config`
- report: `/imo/vinnugogn/ofanflod/verk/hmat/snowpack/hybrid/hybrid_run_report.csv`
- log: `/imo/vinnugogn/ofanflod/verk/hmat/snowpack/hybrid/logs/hybrid_cron.log`

Example cron line (after forecast pipeline):

```cron
40 6 * * * /bin/bash /imo/vinnugogn/ofanflod/verk/hmat/snowpack/hybrid/run_hybrid_daily.sh
```

One-time manual test:

```bash
/bin/bash /imo/vinnugogn/ofanflod/verk/hmat/snowpack/hybrid/run_hybrid_daily.sh
echo $?
tail -n 80 /imo/vinnugogn/ofanflod/verk/hmat/snowpack/hybrid/logs/hybrid_cron.log
```
