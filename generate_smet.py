"""
Generate SMET files for SNOWPACK modeling in Iceland.

Pulls meteorological data from IMO (Icelandic Met Office) stations near avalanche sites,
then writes SMET 1.1 ASCII format files suitable for SNOWPACK input.

Uses the api.vedur.is/weather/ endpoints.

Usage:
    python generate_smet.py
"""

import os
import sys
import requests
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, List, Dict, Tuple
from math import radians, sin, cos, sqrt, atan2

# =============================================================================
# CONFIG
# =============================================================================

OUTPUT_DIR = Path("smet_files")
OUTPUT_DIR.mkdir(exist_ok=True)

IMO_STATIONS_URL = "https://api.vedur.is/weather/stations"
IMO_AUTO_HOUR_URL = "https://api.vedur.is/weather/observations/aws/hour"

AVALANCHE_SITES = [
    (1, "vestfj", 66.076302, -23.158573, 420, "Ísafjörður, Steiniðjugil"),
    (2, "nord", 66.061745, -23.485704, 610, "Flateyri, Miðhryggsgil"),
    (3, "austfj", 65.718079, -17.678465, 562, "Ljósavatnsskarð"),
    (4, "tindaöxl", 66.063942, -18.633444, 380, "Ólafsfjörður, Tindaöxl"),
]

START_DATE = datetime(2025, 10, 21)
END_DATE = datetime(2026, 8, 13)

MAX_RADIUS_KM = 30.0
MAX_STATIONS = 9

# =============================================================================
# DISTANCE CALCULATION
# =============================================================================

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

# =============================================================================
# HTTP HELPERS
# =============================================================================

def get_json(url: str, params: dict, timeout: int = 10):
    """Fetch JSON from URL with error handling."""
    try:
        r = requests.get(url, params=params, timeout=timeout)
        r.raise_for_status()
        return r.json()
    except requests.RequestException as e:
        print("HTTP error fetching %s: %s" % (url, str(e)))
        return [] if "list" in url.lower() else {}
    except Exception as e:
        print("Error parsing JSON: %s" % str(e))
        return [] if "list" in url.lower() else {}

# =============================================================================
# STATION LOADING
# =============================================================================

def load_imo_stations() -> pd.DataFrame:
    """Load all IMO stations from the API."""
    print("Fetching IMO station list from api.vedur.is...")
    
    try:
        data = get_json(IMO_STATIONS_URL, {})
        
        if not data:
            print("No station data returned")
            return pd.DataFrame()
        
        if isinstance(data, dict) and "results" in data:
            stations_list = data["results"]
        elif isinstance(data, list):
            stations_list = data
        else:
            print("Unexpected API response format: %s" % str(type(data)))
            return pd.DataFrame()
        
        if not stations_list:
            print("Empty station list returned")
            return pd.DataFrame()
        
        df = pd.DataFrame(stations_list)
        print("API returned %d stations" % len(df))
        print("Fields: %s" % str(list(df.columns)))
        
        # Map the actual API field names
        # API uses: station (ID), name, lat, lon, ele (elevation)
        df = df.rename(columns={
            "station": "id",
            "ele": "elev",
        })
        
        # Keep only essential columns
        keep_cols = ["id", "lat", "lon", "name"]
        if "elev" in df.columns:
            keep_cols.append("elev")
        
        df = df[[c for c in keep_cols if c in df.columns]].copy()
        
        # Add elev if missing
        if "elev" not in df.columns:
            df["elev"] = np.nan
        
        # Convert to numeric
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
        import traceback
        traceback.print_exc()
        return pd.DataFrame()

# =============================================================================
# STATION SELECTION
# =============================================================================

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

# =============================================================================
# DATA FETCHING
# =============================================================================

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
        
        if isinstance(data, dict):
            if "results" in data:
                return data["results"] if isinstance(data["results"], list) else []
            else:
                return []
        elif isinstance(data, list):
            return data
        else:
            return []
    
    except Exception as e:
        print("Failed to fetch station %d: %s" % (station_id, str(e)))
        return []

# =============================================================================
# DATA PROCESSING
# =============================================================================

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

def celsius_to_kelvin(temp_c: float) -> float:
    """Convert Celsius to Kelvin."""
    if np.isnan(temp_c):
        return np.nan
    return temp_c + 273.15

# =============================================================================
# SMET FILE GENERATION
# =============================================================================

def latlon_to_utm28(lat: float, lon: float) -> Tuple[float, float]:
    """Approximate conversion from lat/lon to UTM zone 28N."""
    easting = (lon + 180) * 110567
    northing = (lat + 90) * 110575
    return easting, northing

def generate_smet_file(site_id: int, site_name: str, site_lat: float, site_lon: float, 
                      site_elev: float, site_desc: str, nearest_stations: pd.DataFrame,
                      all_stations: pd.DataFrame) -> Optional[str]:
    """Generate a complete SMET file for one site."""
    print("\nProcessing %s (%s)..." % (site_name, site_desc))
    
    if nearest_stations.empty:
        print("  No nearby stations found")
        return None
    
    hourly_data = None
    chosen_station = None
    
    for _, station in nearest_stations.iterrows():
        sid = int(station["id"])
        station_name = station["name"]
        dist_km = station["dist_km"]
        
        print("  Trying station %d (%s, %.1f km away)..." % (sid, station_name, dist_km))
        
        entries = fetch_auto_hour_data(sid, START_DATE, END_DATE)
        
        if not entries:
            print("    No data returned")
            continue
        
        try:
            t_series = build_hourly_series(entries, "T")
            if t_series.empty:
                t_series = build_hourly_series(entries, "t")
            
            f_series = build_hourly_series(entries, "F")
            if f_series.empty:
                f_series = build_hourly_series(entries, "f")
            
            if t_series.empty or f_series.empty:
                print("    Missing temperature or wind data")
                continue
            
            hourly_data = entries
            chosen_station = station
            print("    Got %d hourly records" % len(entries))
            break
        
        except Exception as e:
            print("    Error processing: %s" % str(e))
            continue
    
    if hourly_data is None or chosen_station is None:
        print("  Could not fetch valid data from any station")
        return None
    
    try:
        df = _build_smet_dataframe(site_id, hourly_data, chosen_station)
        if df is None or df.empty:
            print("  Failed to build data frame")
            return None
        
        output_file = OUTPUT_DIR / ("%s_smet.txt" % site_name)
        _write_smet_file(output_file, site_id, site_name, site_lat, site_lon, site_elev,
                        chosen_station, df)
        
        print("  Wrote %s" % output_file)
        return str(output_file)
    
    except Exception as e:
        print("  Error building SMET: %s" % str(e))
        import traceback
        traceback.print_exc()
        return None

def _build_smet_dataframe(site_id: int, entries: List[Dict], station: pd.Series) -> Optional[pd.DataFrame]:
    """Build a DataFrame with SMET variables from IMO hourly data."""
    
    t_series = build_hourly_series(entries, "T")
    if t_series.empty:
        t_series = build_hourly_series(entries, "t")
    
    f_series = build_hourly_series(entries, "F")
    if f_series.empty:
        f_series = build_hourly_series(entries, "f")
    
    d_series = build_hourly_series(entries, "D")
    if d_series.empty:
        d_series = build_hourly_series(entries, "d")
    
    r_series = build_hourly_series(entries, "R")
    if r_series.empty:
        r_series = build_hourly_series(entries, "r")
    
    if t_series.empty:
        return None
    
    ts_index = pd.date_range(start=START_DATE, end=END_DATE, freq="1h")
    
    t = t_series.reindex(ts_index, method="nearest", tolerance=pd.Timedelta("30min"))
    f = f_series.reindex(ts_index, method="nearest", tolerance=pd.Timedelta("30min"))
    d = d_series.reindex(ts_index, method="nearest", tolerance=pd.Timedelta("30min"))
    r = r_series.reindex(ts_index, method="nearest", tolerance=pd.Timedelta("30min"))
    
    ta = np.where(t.notna(), celsius_to_kelvin(t.values), -999.0)
    vw = np.where(f.notna(), f.values, -999.0)
    dw = np.where(d.notna(), d.values, -999.0)
    psum = np.where(r.notna(), r.values, -999.0)
    
    tsg = np.full_like(ta, 273.15)
    vw_max = np.where(vw != -999.0, vw * 1.3, -999.0)
    rh = np.full_like(ta, 0.9)
    p = np.full_like(ta, 101325.0)
    iswr = np.zeros_like(ta)
    ilwr = np.full_like(ta, 200.0)
    
    df = pd.DataFrame({
        "timestamp": ts_index,
        "station_id": site_id,
        "TA": ta,
        "TSG": tsg,
        "VW": vw,
        "VW_MAX": vw_max,
        "DW": dw,
        "RH": rh,
        "P": p,
        "PSUM": psum,
        "ISWR": iswr,
        "ILWR": ilwr,
    })
    
    return df

def _write_smet_file(output_path: Path, site_id: int, site_name: str, site_lat: float,
                    site_lon: float, site_elev: float, station: pd.Series, df: pd.DataFrame):
    """Write a SMET 1.1 ASCII file."""
    
    easting, northing = latlon_to_utm28(site_lat, site_lon)
    
    with open(output_path, "w") as f:
        f.write("SMET 1.1 ASCII\n")
        f.write("[HEADER]\n")
        f.write("station_id       = %d\n" % site_id)
        f.write("station_name     = %s\n" % site_name)
        f.write("easting          = %.3f\n" % easting)
        f.write("northing         = %.3f\n" % northing)
        f.write("epsg             = 32628\n")
        f.write("altitude         = %.1f\n" % site_elev)
        f.write("latitude         = %.6f\n" % site_lat)
        f.write("longitude        = %.6f\n" % site_lon)
        f.write("nodata           = -999\n")
        f.write("fields = station_id timestamp TA TSG VW VW_MAX DW RH P PSUM ISWR ILWR\n")
        f.write("[DATA]\n")
        
        for _, row in df.iterrows():
            timestamp = row["timestamp"].strftime("%Y-%m-%dT%H:%M:%S")
            f.write("%d\t%s\t%.3f\t%.3f\t%.3f\t%.3f\t%.3f\t%.3f\t%.1f\t%.3f\t%.3f\t%.3f\n" % (
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
                row['ISWR'],
                row['ILWR']
            ))

# =============================================================================
# MAIN
# =============================================================================

def main():
    print("=" * 70)
    print("SMET Generator for SNOWPACK - Iceland Avalanche Sites")
    print("=" * 70)
    
    all_stations = load_imo_stations()
    if all_stations.empty:
        print("Failed to load stations. Exiting.")
        return
    
    results = []
    for site_id, site_name, site_lat, site_lon, site_elev, site_desc in AVALANCHE_SITES:
        nearest = find_nearest_stations(site_lat, site_lon, all_stations)
        
        if nearest.empty:
            print("\nNo stations within %.1f km of %s" % (MAX_RADIUS_KM, site_name))
            continue
        
        print("\nNearest stations to %s:" % site_name)
        for _, s in nearest.head(3).iterrows():
            print("  - %s (ID %d, %.1f km)" % (s['name'], int(s['id']), s['dist_km']))
        
        output_file = generate_smet_file(site_id, site_name, site_lat, site_lon,
                                        site_elev, site_desc, nearest, all_stations)
        
        if output_file:
            results.append((site_name, output_file))
    
    print("\n" + "=" * 70)
    print("SUMMARY")
    print("=" * 70)
    
    if results:
        print("Successfully generated %d SMET files:" % len(results))
        for name, path in results:
            print("  - %s: %s" % (name, path))
    else:
        print("No SMET files were generated")
    
    print("\nOutput directory: %s" % OUTPUT_DIR.resolve())

if __name__ == "__main__":
    main()
