"""
Generate SMET files for SNOWPACK modeling in Iceland.

Pulls meteorological data from IMO (Icelandic Met Office) stations near avalanche sites,
then writes SMET 1.1 ASCII format files suitable for SNOWPACK input.

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
from haversine import haversine

# =============================================================================
# CONFIG
# =============================================================================

# Output directory
OUTPUT_DIR = Path("smet_files")
OUTPUT_DIR.mkdir(exist_ok=True)

# IMO API endpoints
IMO_STATIONS_URL = "https://vi-api.vedur.is/observations/list_stations"
IMO_AUTO_HOUR_URL = "https://vi-api.vedur.is/observations/auto_hour"

# Avalanche sites (station_id, name, latitude, longitude, altitude)
AVALANCHE_SITES = [
    (1, "vestfj", 66.076302, -23.158573, 420, "Ísafjörður, Steiniðjugil"),
    (2, "nord", 66.061745, -23.485704, 610, "Flateyri, Miðhryggsgil"),
    (3, "austfj", 65.718079, -17.678465, 562, "Ljósavatnsskarð"),
    (4, "tindaöxl", 66.063942, -18.633444, 380, "Ólafsfjörður, Tindaöxl"),
]

# Date range
START_DATE = datetime(2025, 10, 21)
END_DATE = datetime(2026, 8, 13)

# Search parameters
MAX_RADIUS_KM = 30.0
MAX_STATIONS = 9

# EPSG:32628 (UTM zone 28N) conversion - simplified approximation
def latlon_to_utm28(lat: float, lon: float) -> Tuple[float, float]:
    """Approximate conversion from lat/lon to UTM zone 28N (false easting/northing)."""
    # This is a rough approximation; for production, use pyproj
    easting = (lon + 180) * 110567  # meters per degree at equator
    northing = (lat + 90) * 110575   # meters per degree
    return easting, northing

# =============================================================================
# HTTP HELPERS
# =============================================================================

def get_json(url: str, params: dict, timeout: int = 10) -> List | Dict:
    """Fetch JSON from URL with error handling."""
    try:
        r = requests.get(url, params=params, timeout=timeout)
        r.raise_for_status()
        return r.json()
    except requests.RequestException as e:
        print(f"❌ HTTP error fetching {url}: {e}")
        return [] if isinstance(r.json(), list) else {}

# =============================================================================
# STATION LOADING
# =============================================================================

def load_imo_stations() -> pd.DataFrame:
    """Load all IMO stations from the API."""
    print("📡 Fetching IMO station list...")
    params = {"limit": 10000, "station_type": "all"}
    try:
        data = get_json(IMO_STATIONS_URL, params)
        if not data:
            print("❌ No station data returned")
            return pd.DataFrame()
        
        df = pd.DataFrame(data)
        
        # Rename columns to match expected format
        rename_map = {
            "stod": "id",
            "breidd_y": "lat",
            "lengd_x": "lon",
            "h_stod": "elev",
            "nafn": "name",
        }
        df = df.rename(columns=rename_map)
        
        # Keep only essential columns
        df = df[["id", "lat", "lon", "elev", "name"]].copy()
        
        # Convert to numeric and drop rows with missing coordinates
        df["id"] = pd.to_numeric(df["id"], errors="coerce")
        df["lat"] = pd.to_numeric(df["lat"], errors="coerce")
        df["lon"] = pd.to_numeric(df["lon"], errors="coerce")
        df["elev"] = pd.to_numeric(df["elev"], errors="coerce")
        
        df = df.dropna(subset=["lat", "lon", "id"])
        df = df[df["id"] != 0]  # Remove dummy entries
        
        print(f"✅ Loaded {len(df)} stations")
        return df
    
    except Exception as e:
        print(f"❌ Error loading stations: {e}")
        return pd.DataFrame()

# =============================================================================
# STATION SELECTION
# =============================================================================

def find_nearest_stations(site_lat: float, site_lon: float, all_stations: pd.DataFrame,
                         max_km: float = MAX_RADIUS_KM, max_count: int = MAX_STATIONS) -> pd.DataFrame:
    """Find nearest IMO stations within max_km of a site."""
    df = all_stations.copy()
    
    # Calculate distance
    df["dist_km"] = df.apply(
        lambda row: haversine((site_lat, site_lon), (row["lat"], row["lon"])),
        axis=1
    )
    
    # Filter by distance and sort
    within = df[df["dist_km"] <= max_km].sort_values("dist_km")
    
    return within.head(max_count).reset_index(drop=True)

# =============================================================================
# DATA FETCHING
# =============================================================================

def fetch_auto_hour_data(station_id: int, day_from: datetime, day_to: datetime) -> List[Dict]:
    """Fetch hourly auto station data from IMO API."""
    params = {
        "station_id": int(station_id),
        "day_from": day_from.strftime("%Y-%m-%d"),
        "day_to": day_to.strftime("%Y-%m-%d"),
        "locale": "en",
        "format": "json"
    }
    try:
        data = get_json(IMO_AUTO_HOUR_URL, params, timeout=15)
        return data if isinstance(data, list) else []
    except Exception as e:
        print(f"   ⚠️ Failed to fetch station {station_id}: {e}")
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
            ts = pd.to_datetime(entry.get("timi"))
            val = entry.get(key)
            
            # Convert to float, handle missing/empty
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

def kelvin_to_celsius(temp_k: float) -> float:
    """Convert Kelvin to Celsius."""
    if np.isnan(temp_k):
        return np.nan
    return temp_k - 273.15

def compute_rh_from_dewpoint(t_k: float, td_k: float) -> float:
    """Compute relative humidity from temperature and dew point (both in Kelvin)."""
    if np.isnan(t_k) or np.isnan(td_k):
        return np.nan
    
    t_c = kelvin_to_celsius(t_k)
    td_c = kelvin_to_celsius(td_k)
    
    # Magnus formula approximation
    try:
        alpha = ((17.27 * td_c) / (237.7 + td_c)) - ((17.27 * t_c) / (237.7 + t_c))
        rh = 100 * np.exp(alpha)
        return np.clip(rh / 100.0, 0.0, 1.0)  # Return as fraction 0-1
    except Exception:
        return np.nan

# =============================================================================
# SMET FILE GENERATION
# =============================================================================

def generate_smet_file(site_id: int, site_name: str, site_lat: float, site_lon: float, 
                      site_elev: float, site_desc: str, nearest_stations: pd.DataFrame,
                      all_stations: pd.DataFrame) -> Optional[str]:
    """
    Generate a complete SMET file for one site.
    
    Returns the output filename on success, None on failure.
    """
    print(f"\n🎯 Processing {site_name} ({site_desc})...")
    
    if nearest_stations.empty:
        print(f"   ❌ No nearby stations found")
        return None
    
    # Try to fetch data from the nearest station(s)
    hourly_data = None
    chosen_station = None
    
    for _, station in nearest_stations.iterrows():
        sid = int(station["id"])
        station_name = station["name"]
        dist_km = station["dist_km"]
        
        print(f"   📍 Trying station {sid} ({station_name}, {dist_km:.1f} km away)...")
        
        entries = fetch_auto_hour_data(sid, START_DATE, END_DATE)
        
        if not entries:
            print(f"      ⚠️ No data returned")
            continue
        
        # Try to build series from this station
        try:
            t_series = build_hourly_series(entries, "t")
            f_series = build_hourly_series(entries, "f")
            
            if t_series.empty or f_series.empty:
                print(f"      ⚠️ Missing temperature or wind data")
                continue
            
            # We have usable data
            hourly_data = entries
            chosen_station = station
            print(f"      ✅ Got {len(entries)} hourly records")
            break
        
        except Exception as e:
            print(f"      ❌ Error processing: {e}")
            continue
    
    if hourly_data is None or chosen_station is None:
        print(f"   ❌ Could not fetch valid data from any station")
        return None
    
    # Build the data frame
    try:
        df = _build_smet_dataframe(site_id, hourly_data, chosen_station)
        if df is None or df.empty:
            print(f"   ❌ Failed to build data frame")
            return None
        
        # Write SMET file
        output_file = OUTPUT_DIR / f"{site_name}_smet.txt"
        _write_smet_file(output_file, site_id, site_name, site_lat, site_lon, site_elev,
                        chosen_station, df)
        
        print(f"   💾 Wrote {output_file}")
        return str(output_file)
    
    except Exception as e:
        print(f"   ❌ Error building SMET: {e}")
        return None

def _build_smet_dataframe(site_id: int, entries: List[Dict], station: pd.Series) -> Optional[pd.DataFrame]:
    """Build a DataFrame with SMET variables from IMO hourly data."""
    
    # Build series for each variable
    t_series = build_hourly_series(entries, "t")  # Temperature in Celsius from IMO
    f_series = build_hourly_series(entries, "f")  # Wind speed m/s
    d_series = build_hourly_series(entries, "d")  # Wind direction degrees
    r_series = build_hourly_series(entries, "r")  # Precipitation mm
    
    if t_series.empty:
        return None
    
    # Create hourly index
    ts_index = pd.date_range(start=START_DATE, end=END_DATE, freq="1h")
    
    # Reindex all series to hourly grid with forward fill
    t = t_series.reindex(ts_index, method="nearest", tolerance=pd.Timedelta("30min"))
    f = f_series.reindex(ts_index, method="nearest", tolerance=pd.Timedelta("30min"))
    d = d_series.reindex(ts_index, method="nearest", tolerance=pd.Timedelta("30min"))
    r = r_series.reindex(ts_index, method="nearest", tolerance=pd.Timedelta("30min"))
    
    # Convert to numpy arrays and handle nodata
    ta = np.where(t.notna(), celsius_to_kelvin(t.values), -999.0)
    vw = np.where(f.notna(), f.values, -999.0)
    dw = np.where(d.notna(), d.values, -999.0)
    psum = np.where(r.notna(), r.values, -999.0)
    
    # Derived/fallback variables
    tsg = np.full_like(ta, 273.15)  # Ground temp = constant (or use TA)
    vw_max = vw * 1.3  # Rough gust estimate
    rh = np.full_like(ta, 0.9)  # Placeholder relative humidity
    p = np.full_like(ta, 101325.0)  # Placeholder pressure (Pa)
    iswr = np.zeros_like(ta)  # Shortwave radiation (to be filled)
    ilwr = np.full_like(ta, 200.0)  # Longwave radiation placeholder
    
    # Build DataFrame
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
    
    # Compute UTM coordinates (approximate)
    easting, northing = latlon_to_utm28(site_lat, site_lon)
    
    with open(output_path, "w") as f:
        # Header
        f.write("SMET 1.1 ASCII\n")
        f.write("[HEADER]\n")
        f.write(f"station_id       = {site_id}\n")
        f.write(f"station_name     = {site_name}\n")
        f.write(f"easting          = {easting:.3f}\n")
        f.write(f"northing         = {northing:.3f}\n")
        f.write(f"epsg             = 32628\n")
        f.write(f"altitude         = {site_elev:.1f}\n")
        f.write(f"latitude         = {site_lat:.6f}\n")
        f.write(f"longitude        = {site_lon:.6f}\n")
        f.write(f"nodata           = -999\n")
        f.write(f"fields = station_id timestamp TA TSG VW VW_MAX DW RH P PSUM ISWR ILWR\n")
        f.write("[DATA]\n")
        
        # Data rows
        for _, row in df.iterrows():
            timestamp = row["timestamp"].strftime("%Y-%m-%dT%H:%M:%S")
            f.write(
                f"{row['station_id']:.1f}\t"
                f"{timestamp}\t"
                f"{row['TA']:.3f}\t"
                f"{row['TSG']:.3f}\t"
                f"{row['VW']:.3f}\t"
                f"{row['VW_MAX']:.3f}\t"
                f"{row['DW']:.3f}\t"
                f"{row['RH']:.3f}\t"
                f"{row['P']:.1f}\t"
                f"{row['PSUM']:.3f}\t"
                f"{row['ISWR']:.3f}\t"
                f"{row['ILWR']:.3f}\n"
            )

# =============================================================================
# MAIN
# =============================================================================

def main():
    print("=" * 70)
    print("SMET Generator for SNOWPACK - Iceland Avalanche Sites")
    print("=" * 70)
    
    # Load IMO stations
    all_stations = load_imo_stations()
    if all_stations.empty:
        print("❌ Failed to load stations. Exiting.")
        return
    
    # Process each avalanche site
    results = []
    for site_id, site_name, site_lat, site_lon, site_elev, site_desc in AVALANCHE_SITES:
        # Find nearest stations
        nearest = find_nearest_stations(site_lat, site_lon, all_stations)
        
        if nearest.empty:
            print(f"\n❌ No stations within {MAX_RADIUS_KM} km of {site_name}")
            continue
        
        print(f"\n📍 Nearest stations to {site_name}:")
        for _, s in nearest.head(3).iterrows():
            print(f"   - {s['name']} (ID {s['id']}, {s['dist_km']:.1f} km)")
        
        # Generate SMET file
        output_file = generate_smet_file(site_id, site_name, site_lat, site_lon,
                                        site_elev, site_desc, nearest, all_stations)
        
        if output_file:
            results.append((site_name, output_file))
    
    # Summary
    print("\n" + "=" * 70)
    print("SUMMARY")
    print("=" * 70)
    
    if results:
        print(f"✅ Successfully generated {len(results)} SMET files:")
        for name, path in results:
            print(f"   - {name}: {path}")
    else:
        print("❌ No SMET files were generated")
    
    print(f"\nOutput directory: {OUTPUT_DIR.resolve()}")

if __name__ == "__main__":
    main()
