"""
Generate SMET files for SNOWPACK modeling in Iceland.
Reads all sensor locations from sensors_list.txt and generates SMET files
for each sensor location using nearest IMO meteorological stations.
"""

import os
import sys
import requests
import pandas as pd
import numpy as np
import csv
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional, List, Dict, Tuple
from math import radians, sin, cos, sqrt, atan2

try:
    from pyproj import Transformer
except ImportError:
    import subprocess
    subprocess.check_call([sys.executable, "-m", "pip", "install", "pyproj"])
    from pyproj import Transformer

OUTPUT_DIR = Path("data/smet_files")
OUTPUT_DIR.mkdir(exist_ok=True)

IMO_STATIONS_URL = "https://api.vedur.is/weather/stations"
IMO_AUTO_HOUR_URL = "https://api.vedur.is/weather/observations/aws/hour"
IMO_SYNOP_URL = "https://api.vedur.is/weather/observations/synop"

PRECIP_STATIONS = [253, 408, 626]
START_DATE = datetime(2026, 9, 1)
END_DATE = datetime(2027, 7, 1)

MAX_RADIUS_KM = 30.0
MAX_STATIONS = 9
MIN_DATA_COVERAGE = 0.5
MAX_DAYS_PER_QUERY = 30

def load_sensors_from_csv(csv_file: str) -> List[Tuple[int, str, float, float, float]]:
    """Load sensor locations from sensors_list.txt CSV file."""
    sensors = []
    
    try:
        with open(csv_file, 'r') as f:
            reader = csv.DictReader(f)
            for row in reader:
                try:
                    sensor_id = int(row['sensorId'].strip())
                    name = row['name'].strip()
                    lat = float(row['latitude'])
                    lon = float(row['longitude'])
                    
                    # Skip sensors with bad coordinates
                    if lat == 0 and lon == 0:
                        continue
                    
                    # Extract elevation from name (e.g., "420mh" or "420 mh")
                    import re
                    elev_match = re.search(r'(\d+)\s*mh', name, re.IGNORECASE)
                    elevation = float(elev_match.group(1)) if elev_match else 0.0
                    
                    sensors.append((sensor_id, name, lat, lon, elevation))
                except (ValueError, KeyError):
                    continue
    except FileNotFoundError:
        print("Error: sensors_list.txt not found")
        return []
    
    return sensors

def great_circle_distance(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Calculate distance between two points on Earth in km using Haversine formula."""
    R = 6371.0
    lat1_rad = radians(lat1)
    lon1_rad = radians(lon1)
    lat2_rad = radians(lat2)
    lon2_rad = radians(lon2)
    dlat = lat2_rad - lat1_rad
    dlon = lon2_rad - lon1_rad
    a = sin(dlat / 2)**2 + cos(lat1_rad) * cos(lat2_rad) * sin(dlon / 2)**2
    c = 2 * atan2(sqrt(a), sqrt(1 - a))
    return R * c

def get_json(url: str, params: dict, timeout: int = 10):
    """Fetch JSON from URL with error handling."""
    try:
        r = requests.get(url, params=params, timeout=timeout)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        return [] if "list" in url.lower() else {}

def load_imo_stations() -> pd.DataFrame:
    """Load all IMO stations from the API."""
    print("Fetching IMO station list from api.vedur.is...")
    try:
        data = get_json(IMO_STATIONS_URL, {})
        if not data:
            return pd.DataFrame()

        if isinstance(data, dict) and "results" in data:
            stations_list = data["results"]
        elif isinstance(data, list):
            stations_list = data
        else:
            return pd.DataFrame()

        if not stations_list:
            return pd.DataFrame()

        df = pd.DataFrame(stations_list)
        print("API returned %d stations" % len(df))

        df = df.rename(columns={"station": "id", "ele": "elev"})
        keep_cols = ["id", "lat", "lon", "name"]
        if "elev" in df.columns:
            keep_cols.append("elev")

        df = df[[c for c in keep_cols if c in df.columns]].copy()
        if "elev" not in df.columns:
            df["elev"] = np.nan

        df["id"] = pd.to_numeric(df["id"], errors="coerce")
        df["lat"] = pd.to_numeric(df["lat"], errors="coerce")
        df["lon"] = pd.to_numeric(df["lon"], errors="coerce")
        df["elev"] = pd.to_numeric(df["elev"], errors="coerce")

        df = df.dropna(subset=["lat", "lon", "id"])
        df = df[df["id"] != 0]

        print("Loaded %d valid stations" % len(df))
        return df
    except Exception as e:
        print("Error loading stations: %s" % str(e))
        return pd.DataFrame()

def find_nearest_stations(site_lat: float, site_lon: float, all_stations: pd.DataFrame,
                         max_km: float = MAX_RADIUS_KM, max_count: int = MAX_STATIONS) -> pd.DataFrame:
    """Find nearest IMO stations within max_km of a site."""
    df = all_stations.copy()
    df["dist_km"] = df.apply(
        lambda row: great_circle_distance(site_lat, site_lon, row["lat"], row["lon"]),
        axis=1
    )
    within = df[df["dist_km"] <= max_km].sort_values("dist_km")
    return within.head(max_count).reset_index(drop=True)

def fetch_auto_hour_data(station_id: int, day_from: datetime, day_to: datetime) -> List[Dict]:
    """Fetch hourly auto station data from IMO API."""
    params = {
        "station_id": int(station_id),
        "parameters": "all",
        "format": "json",
        "day_from": day_from.strftime("%Y-%m-%d"),
        "day_to": day_to.strftime("%Y-%m-%d"),
    }
    try:
        data = get_json(IMO_AUTO_HOUR_URL, params, timeout=15)
        if isinstance(data, dict) and "results" in data:
            return data["results"] if isinstance(data["results"], list) else []
        elif isinstance(data, list):
            return data
        else:
            return []
    except Exception:
        return []

def fetch_precip_chunked(station_id: int, day_from: datetime, day_to: datetime) -> List[Dict]:
    """Fetch SYNOP precipitation data in chunks."""
    all_entries = []
    current = day_from
    
    while current < day_to:
        chunk_end = min(current + timedelta(days=MAX_DAYS_PER_QUERY), day_to)
        
        params = {
            "station_id": int(station_id),
            "day_from": current.strftime("%Y-%m-%d"),
            "day_to": chunk_end.strftime("%Y-%m-%d"),
            "locale": "is",
            "format": "json",
            "parameters": "basic",
        }
        
        data = get_json(IMO_SYNOP_URL, params, timeout=15)
        
        if isinstance(data, list):
            all_entries.extend(data)
        elif isinstance(data, dict) and "results" in data:
            results = data["results"]
            if isinstance(results, list):
                all_entries.extend(results)
        
        current = chunk_end + timedelta(days=1)
    
    return all_entries

def load_precip_data() -> pd.DataFrame:
    """Load precipitation data from SYNOP endpoints."""
    print("\nFetching precipitation data from SYNOP stations...")
    precip_dfs = []
    
    for sid in PRECIP_STATIONS:
        print("  Station %d..." % sid)
        entries = fetch_precip_chunked(sid, START_DATE, END_DATE)
        
        if not entries:
            print("    No data")
            continue
        
        try:
            df = pd.DataFrame(entries)
            
            # Look for precipitation column (might be 'r', 'R', or 'rr')
            precip_col = None
            for col in ['r', 'R', 'rr', 'RR']:
                if col in df.columns:
                    precip_col = col
                    break
            
            if not precip_col:
                print("    No precipitation column found. Available: %s" % str(list(df.columns)))
                continue
            
            # Select needed columns
            keep_cols = [c for c in ["station", "name", "time", precip_col] if c in df.columns]
            df = df[keep_cols].copy()
            
            # Rename precipitation column to 'r' for consistency
            if precip_col != 'r':
                df = df.rename(columns={precip_col: 'r'})
            
            df["time"] = pd.to_datetime(df["time"])
            df = df.dropna(subset=["r"])
            
            # Filter out negative or obviously bad values
            df = df[df["r"] >= 0]
            
            precip_dfs.append(df)
            print("    Loaded %d records" % len(df))
        except Exception as e:
            print("    Error processing: %s" % str(e))
            continue
    
    if not precip_dfs:
        print("  No precipitation data found")
        return pd.DataFrame()
    
    merged = pd.concat(precip_dfs, ignore_index=True)
    merged = merged.sort_values("time")
    print("  Total precipitation records: %d" % len(merged))
    return merged

def build_hourly_series(entries: List[Dict], key: str) -> pd.Series:
    """Convert raw IMO entries to a pandas Series indexed by timestamp."""
    if not entries:
        return pd.Series(dtype=float)

    times = []
    values = []

    for entry in entries:
        try:
            ts = None
            for ts_field in ["timestamp", "timi", "time", "datetime"]:
                if ts_field in entry and entry[ts_field]:
                    ts = pd.to_datetime(entry[ts_field])
                    break
            if ts is None:
                continue

            val = entry.get(key)
            if val is None or val == "":
                val = np.nan
            else:
                val = float(val)

            times.append(ts)
            values.append(val)
        except Exception:
            continue

    if not times:
        return pd.Series(dtype=float)

    series = pd.Series(values, index=pd.DatetimeIndex(times))
    return series.sort_index()

def expand_daily_to_hourly(daily_series: pd.Series, target_index: pd.DatetimeIndex) -> pd.Series:
    """Expand daily data to hourly by spreading evenly across 24 hours."""
    hourly_values = np.full(len(target_index), np.nan)
    
    for i, ts in enumerate(target_index):
        day_key = ts.normalize()
        matching = daily_series[daily_series.index.normalize() == day_key]
        
        if not matching.empty:
            daily_val = matching.iloc[0]
            if pd.notna(daily_val) and daily_val >= 0:
                hourly_values[i] = daily_val / 24.0
    
    return pd.Series(hourly_values, index=target_index)

def latlon_to_utm27(lat: float, lon: float) -> Tuple[float, float]:
    """Convert lat/lon to UTM zone 27N (Iceland)."""
    transformer = Transformer.from_crs("EPSG:4326", "EPSG:32627", always_xy=True)
    easting, northing = transformer.transform(lon, lat)
    return easting, northing

def check_data_quality(t_series: pd.Series, f_series: pd.Series, min_coverage: float = MIN_DATA_COVERAGE) -> bool:
    """Check if the data has sufficient valid coverage."""
    if t_series.empty or f_series.empty:
        return False
    
    total_points = len(t_series)
    valid_t = pd.notna(t_series).sum()
    valid_f = pd.notna(f_series).sum()
    
    t_coverage = valid_t / total_points if total_points > 0 else 0
    f_coverage = valid_f / total_points if total_points > 0 else 0
    
    print("      Temperature coverage: %.1f%%" % (t_coverage * 100))
    print("      Wind coverage: %.1f%%" % (f_coverage * 100))
    
    return t_coverage >= min_coverage and f_coverage >= min_coverage

def get_last_timestamp(sensor_id: int) -> Optional[datetime]:
    """Get the last timestamp from existing SMET file for a sensor."""
    output_file = OUTPUT_DIR / ("%d_smet_merged.txt" % sensor_id)
    
    if not output_file.exists():
        return None
    
    try:
        with open(output_file, 'r') as f:
            lines = f.readlines()
        
        # Find [DATA] line
        data_start = -1
        for i, line in enumerate(lines):
            if line.strip() == "[DATA]":
                data_start = i + 1
                break
        
        if data_start == -1 or data_start >= len(lines):
            return None
        
        # Get last data line
        last_line = None
        for line in reversed(lines[data_start:]):
            line = line.strip()
            if line:
                last_line = line
                break
        
        if not last_line:
            return None
        
        # Parse timestamp (column 1)
        parts = last_line.split('\t')
        if len(parts) >= 2:
            timestamp_str = parts[1]
            return pd.to_datetime(timestamp_str)
    
    except Exception as e:
        print("    Error reading last timestamp: %s" % str(e))
        return None
    
    return None

def generate_smet_file(sensor_id: int, sensor_name: str, sensor_lat: float, sensor_lon: float,
                      sensor_elev: float, nearest_stations: pd.DataFrame,
                      all_stations: pd.DataFrame, precip_data: pd.DataFrame) -> Optional[str]:
    """Generate a complete SMET file for one sensor location."""
    print("\nProcessing sensor %d (%s)..." % (sensor_id, sensor_name))

    if nearest_stations.empty:
        print("  No nearby stations found")
        return None

    print("  Searching for temperature/wind data...")
    hourly_data_tw = None
    chosen_station_tw = None

    for _, station in nearest_stations.iterrows():
        sid = int(station["id"])
        station_name = station["name"]
        dist_km = station["dist_km"]

        print("    Trying station %d (%s, %.1f km away)..." % (sid, station_name, dist_km))

        entries = fetch_auto_hour_data(sid, START_DATE, END_DATE)

        if not entries:
            print("      No data returned")
            continue

        try:
            t_series = build_hourly_series(entries, "t")
            if t_series.empty:
                t_series = build_hourly_series(entries, "T")

            f_series = build_hourly_series(entries, "f")
            if f_series.empty:
                f_series = build_hourly_series(entries, "F")

            if t_series.empty or f_series.empty:
                print("      Missing temperature or wind data")
                continue

            if not check_data_quality(t_series, f_series):
                print("      Insufficient data coverage (need %.0f%%)" % (MIN_DATA_COVERAGE * 100))
                continue

            hourly_data_tw = entries
            chosen_station_tw = station
            print("      ACCEPTED")
            break

        except Exception as e:
            print("      Error processing: %s" % str(e))
            continue

    if hourly_data_tw is None or chosen_station_tw is None:
        print("  Could not fetch valid temp/wind data from any station")
        return None

    try:
        df = _build_smet_dataframe(sensor_id, hourly_data_tw, chosen_station_tw, precip_data)
        if df is None or df.empty:
            print("  Failed to build data frame")
            return None

        output_file = OUTPUT_DIR / ("%d_smet_merged.txt" % sensor_id)
        _append_smet_file(output_file, sensor_id, sensor_name, sensor_lat, sensor_lon, sensor_elev,
                        chosen_station_tw, df)

        print("  Wrote %s" % output_file)
        return str(output_file)

    except Exception as e:
        print("  Error building SMET: %s" % str(e))
        import traceback
        traceback.print_exc()
        return None

def _build_smet_dataframe(sensor_id: int, entries_tw: List[Dict], station_tw: pd.Series, 
                         precip_data: pd.DataFrame) -> Optional[pd.DataFrame]:
    """Build a DataFrame with SMET variables from IMO hourly data."""

    t_series = build_hourly_series(entries_tw, "t")
    if t_series.empty:
        t_series = build_hourly_series(entries_tw, "T")

    f_series = build_hourly_series(entries_tw, "f")
    if f_series.empty:
        f_series = build_hourly_series(entries_tw, "F")

    d_series = build_hourly_series(entries_tw, "d")
    if d_series.empty:
        d_series = build_hourly_series(entries_tw, "D")

    if t_series.empty:
        return None

    # Validate temperature ranges
    t_series = t_series[(t_series >= -60) & (t_series <= 50)]

    ts_index = pd.date_range(start=START_DATE, end=END_DATE, freq="1h")

    t = t_series.reindex(ts_index, method="nearest", tolerance=pd.Timedelta("30min"))
    f = f_series.reindex(ts_index, method="nearest", tolerance=pd.Timedelta("30min"))
    d = d_series.reindex(ts_index, method="nearest", tolerance=pd.Timedelta("30min"))

    # Handle precipitation from merged data
    r = pd.Series(index=ts_index, dtype=float)
    if not precip_data.empty:
        r_series = build_hourly_series(precip_data.to_dict('records'), "r")
        if not r_series.empty:
            r = expand_daily_to_hourly(r_series, ts_index)
            precip_coverage = pd.notna(r).sum() / len(r) * 100
            print("    Precipitation: %.1f%% coverage" % precip_coverage)

    ta = np.where(pd.notna(t.values), t.values + 273.15, -999.0)
    vw = np.where(pd.notna(f.values), f.values, -999.0)
    dw = np.where(pd.notna(d.values), d.values, -999.0)
    psum = np.where(pd.notna(r.values), r.values, -999.0)

    # Validate TA bounds after conversion
    ta = np.where((ta >= 230.0) & (ta <= 310.0), ta, -999.0)

    # Use average of TA for TSG (ground/soil temp)
    tsg = np.where(ta != -999.0, ta, -999.0)
    tsg = np.where(tsg == -999.0, 265.0, tsg)

    vw_max = np.where(vw != -999.0, vw * 1.3, -999.0)
    rh = np.full_like(ta, 0.9)
    p = np.full_like(ta, 101325.0)
    iswr = np.zeros_like(ta)
    ilwr = np.full_like(ta, 200.0)
    
    precip = np.copy(psum)
    precip_splitting = np.where(psum != -999.0, 1.0, -999.0)

    df = pd.DataFrame({
        "timestamp": ts_index,
        "station_id": sensor_id,
        "TA": ta,
        "TSG": tsg,
        "VW": vw,
        "VW_MAX": vw_max,
        "DW": dw,
        "RH": rh,
        "P": p,
        "PSUM": psum,
        "precipitation": precip,
        "precip_splitting": precip_splitting,
        "ISWR": iswr,
        "ILWR": ilwr,
    })

    return df

def _append_smet_file(output_path: Path, sensor_id: int, sensor_name: str, sensor_lat: float,
                    sensor_lon: float, sensor_elev: float, station: pd.Series, df: pd.DataFrame):
    """Append new data to existing SMET file, or create if doesn't exist."""

    easting, northing = latlon_to_utm27(sensor_lat, sensor_lon)
    
    # Read existing file if it exists
    existing_df = None
    if output_path.exists():
        try:
            with open(output_path, 'r') as f:
                lines = f.readlines()
            
            # Find [DATA] line
            data_start = -1
            for i, line in enumerate(lines):
                if line.strip() == "[DATA]":
                    data_start = i + 1
                    break
            
            if data_start > 0:
                # Parse existing data
                existing_times = []
                existing_rows = []
                for line in lines[data_start:]:
                    line = line.strip()
                    if line:
                        parts = line.split('\t')
                        if len(parts) >= 2:
                            try:
                                ts = pd.to_datetime(parts[1])
                                existing_times.append(ts)
                                existing_rows.append(parts)
                            except (ValueError, IndexError):
                                continue
                
                if existing_times:
                    existing_df = pd.DataFrame({
                        "timestamp": existing_times,
                    })
                    print("  Loaded %d existing records" % len(existing_df))
        
        except Exception as e:
            print("  Error reading existing file: %s" % str(e))
    
    # Merge new data with existing
    if existing_df is not None:
        df_combined = pd.concat([existing_df[["timestamp"]], df[["timestamp"]]], ignore_index=True)
        df_combined = df_combined.drop_duplicates(subset=["timestamp"], keep="last")
        df_combined = df_combined.sort_values("timestamp").reset_index(drop=True)
        
        # Keep only new rows not in existing
        df_new = df[~df["timestamp"].isin(existing_df["timestamp"])]
        df = pd.concat([existing_df.merge(df, on="timestamp", how="left"), df_new], ignore_index=True)
        df = df.sort_values("timestamp").reset_index(drop=True)
        print("  Combined: %d existing + %d new records = %d total unique" % (len(existing_df), len(df_new), len(df)))
    
    # Write complete file
    with open(output_path, "w") as f:
        f.write("SMET 1.1 ASCII\n")
        f.write("[HEADER]\n")
        f.write("station_id       = %d\n" % sensor_id)
        f.write("station_name     = %s\n" % sensor_name)
        f.write("latitude         = %.6f\n" % sensor_lat)
        f.write("longitude        = %.6f\n" % sensor_lon)
        f.write("easting          = %.3f\n" % easting)
        f.write("northing         = %.3f\n" % northing)
        f.write("epsg             = 32627\n")
        f.write("altitude         = %.1f\n" % sensor_elev)
        f.write("nodata           = -999\n")
        f.write("fields = station_id timestamp TA TSG VW VW_MAX DW RH P PSUM precipitation precip_splitting ISWR ILWR\n")
        f.write("[DATA]\n")

        for _, row in df.iterrows():
            timestamp = row["timestamp"].strftime("%Y-%m-%dT%H:%M:%S")
            # Use proper tab separation between all fields
            f.write("%d\t%s\t%.3f\t%.3f\t%.3f\t%.3f\t%.3f\t%.3f\t%.1f\t%.3f\t%.3f\t%.3f\t%.3f\t%.3f\n" % (
                int(row['station_id']),
                timestamp,
                row['TA'],
                row['TSG'],
                row['VW'],
                row['VW_MAX'],
                row['DW'],
                row['RH'],
                row['P'],
                row['PSUM'],
                row['precipitation'],
                row['precip_splitting'],
                row['ISWR'],
                row['ILWR']
            ))

def main():
    print("=" * 70)
    print("SMET Generator for SNOWPACK - Iceland Avalanche Sites")
    print("=" * 70)

    # Load sensors from CSV
    sensors = load_sensors_from_csv("sensors_list.txt")
    if not sensors:
        print("Error: Could not load sensors from sensors_list.txt")
        return
    
    print("Loaded %d sensors from sensors_list.txt\n" % len(sensors))

    all_stations = load_imo_stations()
    if all_stations.empty:
        print("Failed to load stations. Exiting.")
        return

    precip_data = load_precip_data()

    results = []
    for sensor_id, sensor_name, sensor_lat, sensor_lon, sensor_elev in sensors:
        nearest = find_nearest_stations(sensor_lat, sensor_lon, all_stations)

        if nearest.empty:
            print("\nNo stations within %.1f km of sensor %d" % (MAX_RADIUS_KM, sensor_id))
            continue

        print("\nNearest stations to sensor %d (%s):" % (sensor_id, sensor_name))
        for _, s in nearest.head(3).iterrows():
            print("  - %s (ID %d, %.1f km)" % (s['name'], int(s['id']), s['dist_km']))

        output_file = generate_smet_file(sensor_id, sensor_name, sensor_lat, sensor_lon,
                                        sensor_elev, nearest, all_stations, precip_data)

        if output_file:
            results.append((sensor_id, sensor_name, output_file))

    print("\n" + "=" * 70)
    print("SUMMARY")
    print("=" * 70)

    if results:
        print("Successfully generated %d SMET files:" % len(results))
        for sid, sname, path in results:
            print("  - %d (%s): %s" % (sid, sname, path))
    else:
        print("No SMET files were generated")

    print("\nOutput directory: %s" % OUTPUT_DIR.resolve())

if __name__ == "__main__":
    main()
