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
