"""
Fetch and process snow height (HS) data from Snowsense SM4 sensors.

Pulls hourly data from dev.snowsense.is/v1/sensors API, validates,
handles gaps, and writes SMET 1.1 ASCII files compatible with SNOWPACK forcing.

Usage:
    python fetch_snow_heights.py
"""

import requests
import pandas as pd
import numpy as np
import csv
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional, List, Dict, Tuple

OUTPUT_DIR = Path("data/snow_heights")
OUTPUT_DIR.mkdir(exist_ok=True)

SNOWSENSE_DATA_URL = "https://dev.snowsense.is/v1/sensors/{sensor_id}/data"

START_DATE = datetime(2026, 9, 1, 0, 0, 0, tzinfo=timezone.utc)

MAX_HS_CM = 500
MAX_POINTS_PER_FETCH = 5000
REALISTIC_JUMP_CM = 100
SMOOTH_WINDOW_DAYS = 7
INTERPOLATE_GAP_HOURS = 12
FORWARD_FILL_GAP_DAYS = 3

def load_sensors_from_csv(csv_file: str) -> List[Tuple[int, str, float, float, float]]:
    """Load sensor list from sensors_list.txt CSV file."""
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

def get_json(url: str, params: dict, timeout: int = 30):
    """Fetch JSON from URL with error handling."""
    try:
        r = requests.get(url, params=params, timeout=timeout)
        r.raise_for_status()
        return r.json()
    except requests.RequestException as e:
        print("HTTP error fetching %s: %s" % (url, str(e)))
        return None
    except Exception as e:
        print("Error parsing JSON: %s" % str(e))
        return None

def get_last_timestamp(sensor_id: int) -> Optional[datetime]:
    """Get the last timestamp from existing HS file for a sensor."""
    output_file = OUTPUT_DIR / ("%d_hs.txt" % sensor_id)
    
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
            ts = pd.to_datetime(timestamp_str)
            # Make timezone-aware to UTC if naive
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            return ts
    
    except Exception as e:
        print("    Error reading last timestamp: %s" % str(e))
        return None
    
    return None

def fetch_sensor_data(sensor_id: int, from_dt: datetime, to_dt: datetime) -> List[Dict]:
    """
    Fetch snow height data for a sensor between dates.
    Handles pagination (max 5000 points per fetch).
    """
    all_data = []
    current_from = from_dt
    
    print("    Fetching data from %s to %s..." % (from_dt.isoformat(), to_dt.isoformat()))
    
    while current_from < to_dt:
        params = {
            "from": current_from.isoformat(),
            "to": to_dt.isoformat(),
            "take": MAX_POINTS_PER_FETCH,
            "order": "asc",
            "period": "TenMinute",
        }
        
        url = SNOWSENSE_DATA_URL.format(sensor_id=sensor_id)
        data = get_json(url, params)
        
        if not data:
            print("    No data returned for period starting %s" % current_from.isoformat())
            break
        
        if isinstance(data, list):
            data_list = data
        elif isinstance(data, dict) and "data" in data:
            data_list = data["data"]
        else:
            print("    Unexpected response format")
            break
        
        if not data_list:
            break
        
        all_data.extend(data_list)
        print("    Got %d points (total: %d)" % (len(data_list), len(all_data)))
        
        if len(data_list) < MAX_POINTS_PER_FETCH:
            break
        
        last_time = pd.to_datetime(data_list[-1].get("createdAt"))
        current_from = last_time + timedelta(minutes=10)
    
    print("    Total %d points fetched" % len(all_data))
    return all_data

def process_sensor_data(sensor_id: int, sensor_name: str, sensor_lat: float, 
                       sensor_lon: float, sensor_elev: float, raw_data: List[Dict],
                       start_date: datetime, end_date: datetime) -> Tuple[Optional[pd.DataFrame], Dict]:
    """
    Process raw sensor data:
    - Extract HS values (laserSnowdepthCm)
    - Extract top temperature (first value in temperature array)
    - Validate (remove > 500 cm)
    - Resample to hourly
    - Handle gaps
    - Apply smoothing if needed
    
    Returns: (dataframe, report_dict)
    """
    report = {
        "sensor_id": sensor_id,
        "sensor_name": sensor_name,
        "total_raw_points": len(raw_data),
        "filtered_points": 0,
        "start_date": None,
        "end_date": None,
        "gaps_interpolated": 0,
        "gaps_forward_filled": 0,
        "gaps_offline": 0,
        "points_smoothed": 0,
    }
    
    if not raw_data:
        print("  No data for sensor %d" % sensor_id)
        return None, report
    
    times = []
    values = []
    temps = []
    
    for entry in raw_data:
        try:
            ts_str = entry.get("createdAt")
            if not ts_str:
                continue
            
            ts = pd.to_datetime(ts_str)
            
            hs = entry.get("laserSnowdepthCm")
            if hs is None:
                continue
            
            hs_val = float(hs)
            
            if hs_val > MAX_HS_CM:
                report["filtered_points"] += 1
                continue
            
            # Extract top temperature (first value in temperature array)
            temp_array = entry.get("temperature")
            top_temp = None
            if temp_array and isinstance(temp_array, list) and len(temp_array) > 0:
                try:
                    top_temp = float(temp_array[0])
                except (ValueError, TypeError):
                    top_temp = None
            
            times.append(ts)
            values.append(hs_val)
            temps.append(top_temp)
        
        except Exception:
            continue
    
    if not times:
        print("  No valid data points for sensor %d" % sensor_id)
        return None, report
    
    print("  Valid points: %d" % len(times))
    
    df_raw = pd.DataFrame({
        "time": times,
        "hs": values,
        "temp": temps,
    })
    df_raw = df_raw.sort_values("time").reset_index(drop=True)
    
    if df_raw["time"].duplicated().any():
        print("  Found duplicate timestamps, aggregating...")
        df_raw = df_raw.groupby("time").agg({"hs": "mean", "temp": "mean"}).reset_index()
        print("  Aggregated to %d unique timestamps" % len(df_raw))
    
    report["start_date"] = df_raw["time"].min()
    report["end_date"] = df_raw["time"].max()
    
    print("  Data range: %s to %s" % (report["start_date"], report["end_date"]))
    
    hourly_index = pd.date_range(start=start_date, end=end_date, freq="1h", tz=timezone.utc)
    
    df_hourly = df_raw.set_index("time").reindex(hourly_index, method="nearest", tolerance=pd.Timedelta("5min"))
    df_hourly = df_hourly.reset_index()
    df_hourly.columns = ["time", "hs", "temp"]
    
    print("  Resampled to hourly: %d values" % df_hourly["hs"].notna().sum())
    
    df_hourly["status"] = "raw"
    df_hourly["hs"] = df_hourly["hs"].fillna(-999.0)
    
    sensor_start_idx = df_hourly[df_hourly["hs"] != -999.0].index
    if len(sensor_start_idx) == 0:
        print("  No valid data points after processing")
        return None, report
    
    sensor_start_idx = sensor_start_idx[0]
    print("  Sensor data starts at row %d (%s)" % (sensor_start_idx, df_hourly.loc[sensor_start_idx, "time"]))
    
    smooth_end_idx = min(sensor_start_idx + (SMOOTH_WINDOW_DAYS * 24), len(df_hourly))
    
    if smooth_end_idx > sensor_start_idx + 1:
        first_val = df_hourly.loc[sensor_start_idx, "hs"]
        second_val = df_hourly.loc[sensor_start_idx + 1, "hs"] if sensor_start_idx + 1 < len(df_hourly) and df_hourly.loc[sensor_start_idx + 1, "hs"] != -999.0 else first_val
        
        initial_jump = abs(second_val - first_val)
        
        if initial_jump > REALISTIC_JUMP_CM:
            print("  Initial jump detected: %.1f cm > %.1f cm threshold" % (initial_jump, REALISTIC_JUMP_CM))
            print("  Applying 7-day smoothing...")
            
            smooth_data = df_hourly.loc[sensor_start_idx:smooth_end_idx, "hs"].copy()
            smooth_data = smooth_data.replace(-999.0, np.nan)
            
            smooth_data = smooth_data.rolling(window=24*7, center=True, min_periods=1).mean()
            
            mask = (df_hourly.index >= sensor_start_idx) & (df_hourly.index < smooth_end_idx) & (df_hourly["hs"] != -999.0)
            df_hourly.loc[mask, "hs"] = smooth_data[mask]
            df_hourly.loc[mask, "status"] = "smoothed"
            
            report["points_smoothed"] = mask.sum()
            print("  Smoothed %d points" % report["points_smoothed"])
    
    df_hourly = fill_gaps(df_hourly, report)
    
    return df_hourly, report

def fill_gaps(df: pd.DataFrame, report: Dict) -> pd.DataFrame:
    """
    Handle gaps in snow height data:
    - < 12 hrs: interpolate
    - 12 hrs - 3 days: forward fill
    - > 3 days: set to -999 (offline)
    """
    df = df.copy()
    
    i = 0
    while i < len(df):
        if df.loc[i, "hs"] == -999.0:
            gap_start = i
            gap_length = 0
            
            while i < len(df) and df.loc[i, "hs"] == -999.0:
                gap_length += 1
                i += 1
            
            gap_end = i
            gap_hours = gap_length
            
            if gap_hours <= INTERPOLATE_GAP_HOURS:
                df.loc[gap_start:gap_end-1, "hs"] = np.nan
                df.loc[gap_start:gap_end-1, "hs"] = df.loc[gap_start:gap_end-1, "hs"].interpolate()
                df.loc[gap_start:gap_end-1, "status"] = "interpolated"
                report["gaps_interpolated"] += 1
            
            elif gap_hours <= (FORWARD_FILL_GAP_DAYS * 24):
                if gap_start > 0:
                    last_val = df.loc[gap_start - 1, "hs"]
                    df.loc[gap_start:gap_end-1, "hs"] = last_val
                    df.loc[gap_start:gap_end-1, "status"] = "forward_fill"
                    report["gaps_forward_filled"] += 1
                else:
                    df.loc[gap_start:gap_end-1, "hs"] = -999.0
                    df.loc[gap_start:gap_end-1, "status"] = "offline"
                    report["gaps_offline"] += 1
            
            else:
                df.loc[gap_start:gap_end-1, "hs"] = -999.0
                df.loc[gap_start:gap_end-1, "status"] = "offline"
                report["gaps_offline"] += 1
        
        else:
            i += 1
    
    return df

def append_smet_file(df: pd.DataFrame, sensor_id: int, sensor_name: str, 
                    sensor_lat: float, sensor_lon: float, sensor_elev: float):
    """Append new data to existing SMET file, or create if doesn't exist."""
    
    output_file = OUTPUT_DIR / ("%d_hs.txt" % sensor_id)
    
    # Read existing file if it exists
    existing_df = None
    if output_file.exists():
        try:
            with open(output_file, 'r') as f:
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
                existing_values = []
                for line in lines[data_start:]:
                    line = line.strip()
                    if line:
                        parts = line.split('\t')
                        if len(parts) >= 3:
                            try:
                                ts = pd.to_datetime(parts[1])
                                hs_val = float(parts[2])
                                existing_times.append(ts)
                                existing_values.append(hs_val)
                            except (ValueError, IndexError):
                                continue
                
                if existing_times:
                    existing_df = pd.DataFrame({
                        "time": existing_times,
                        "hs": existing_values,
                    })
                    print("  Loaded %d existing records" % len(existing_df))
        
        except Exception as e:
            print("  Error reading existing file: %s" % str(e))
    
    # Merge new data with existing
    if existing_df is not None:
        df_combined = pd.concat([existing_df, df[["time", "hs"]]], ignore_index=True)
        df_combined = df_combined.drop_duplicates(subset=["time"], keep="last")
        df_combined = df_combined.sort_values("time").reset_index(drop=True)
        print("  Combined: %d existing + new records = %d total unique" % (len(existing_df), len(df_combined)))
        df = df_combined
    
    # Write complete file
    with open(output_file, "w") as f:
        f.write("SMET 1.1 ASCII\n")
        f.write("[HEADER]\n")
        f.write("station_id       = %d\n" % sensor_id)
        f.write("station_name     = %s\n" % sensor_name)
        f.write("latitude         = %.6f\n" % sensor_lat)
        f.write("longitude        = %.6f\n" % sensor_lon)
        f.write("altitude         = %.1f\n" % sensor_elev)
        f.write("nodata           = -999\n")
        f.write("fields = station_id timestamp HS\n")
        f.write("[DATA]\n")
        
        for _, row in df.iterrows():
            timestamp = row["time"].strftime("%Y-%m-%dT%H:%M:%S")
            hs_val = row["hs"]
            f.write("%d\t%s\t%.1f\n" % (sensor_id, timestamp, hs_val))
    
    print("  Wrote %s" % output_file)
    return output_file

def write_report(sensor_id: int, sensor_name: str, report: Dict, output_file: str):
    """Write processing report for sensor."""
    
    report_file = OUTPUT_DIR / ("%d_report.txt" % sensor_id)
    
    with open(report_file, "w") as f:
        f.write("=" * 70 + "\n")
        f.write("Snow Height Data Processing Report\n")
        f.write("=" * 70 + "\n")
        f.write("Sensor ID: %d\n" % sensor_id)
        f.write("Sensor Name: %s\n" % sensor_name)
        f.write("\n")
        f.write("Data Summary:\n")
        f.write("  Total raw points: %d\n" % report["total_raw_points"])
        f.write("  Filtered (> %d cm): %d\n" % (MAX_HS_CM, report["filtered_points"]))
        f.write("  Valid data range: %s to %s\n" % (report["start_date"], report["end_date"]))
        f.write("\n")
        f.write("Processing:\n")
        f.write("  Points smoothed (initial jump): %d\n" % report["points_smoothed"])
        f.write("  Gap periods interpolated (< %d hrs): %d\n" % (INTERPOLATE_GAP_HOURS, report["gaps_interpolated"]))
        f.write("  Gap periods forward-filled (< %d days): %d\n" % (FORWARD_FILL_GAP_DAYS, report["gaps_forward_filled"]))
        f.write("  Gap periods offline (> %d days): %d\n" % (FORWARD_FILL_GAP_DAYS, report["gaps_offline"]))
        f.write("\n")
        f.write("Output file: %s\n" % output_file)
        f.write("=" * 70 + "\n")
    
    print("  Wrote %s" % report_file)

def main():
    print("=" * 70)
    print("Fetch and Process Snow Heights from Snowsense SM4 Sensors")
    print("=" * 70)
    
    # Use current time as end date
    now = datetime.now(timezone.utc)
    print("Date range: %s to %s" % (START_DATE, now))
    print("Output directory: %s" % OUTPUT_DIR.resolve())
    print("")
    
    # Load sensors from CSV
    sensors = load_sensors_from_csv("sensors_list.txt")
    if not sensors:
        print("Error: Could not load sensors from sensors_list.txt")
        return
    
    print("Loaded %d sensors from sensors_list.txt\n" % len(sensors))
    
    results = []
    
    for sensor_id, sensor_name, sensor_lat, sensor_lon, sensor_elev in sensors:
        print("\nProcessing sensor %d (%s)..." % (sensor_id, sensor_name))
        
        # Determine fetch range: from last timestamp to now
        last_ts = get_last_timestamp(sensor_id)
        if last_ts:
            fetch_from = last_ts + timedelta(hours=1)
            print("  Last existing timestamp: %s" % last_ts)
            print("  Fetching new data from: %s" % fetch_from)
        else:
            fetch_from = START_DATE
            print("  No existing data, fetching from start: %s" % fetch_from)
        
        # Always fetch to current time
        raw_data = fetch_sensor_data(sensor_id, fetch_from, now)
        
        if not raw_data:
            print("  No new data to process")
            continue
        
        df, report = process_sensor_data(sensor_id, sensor_name, sensor_lat, sensor_lon, sensor_elev, raw_data, START_DATE, now)
        
        if df is None:
            print("  Skipping sensor - processing failed")
            continue
        
        output_file = append_smet_file(df, sensor_id, sensor_name, sensor_lat, sensor_lon, sensor_elev)
        write_report(sensor_id, sensor_name, report, str(output_file))
        
        results.append((sensor_id, sensor_name, output_file))
    
    print("\n" + "=" * 70)
    print("SUMMARY")
    print("=" * 70)
    
    if results:
        print("Successfully processed %d sensors:" % len(results))
        for sid, sname, sfile in results:
            print("  - %d (%s): %s" % (sid, sname, sfile))
    else:
        print("No new data processed")
    
    print("\nOutput directory: %s" % OUTPUT_DIR.resolve())

if __name__ == "__main__":
    main()
