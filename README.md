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
