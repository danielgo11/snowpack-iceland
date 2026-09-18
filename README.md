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
- `--disable-seasonal-summary`

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
