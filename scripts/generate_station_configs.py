#!/usr/bin/env python3
"""
Generate individual station INI files and empty .sno files from sensors_list.txt
"""

import os
import csv
import re
from datetime import datetime
from pathlib import Path

def clean_name(name):
    """Clean sensor name for use in filenames."""
    # Remove special characters and extra spaces
    name = re.sub(r'[^\w\s]', '', name)
    # Replace spaces with underscores
    name = re.sub(r'\s+', '_', name)
    # Convert to lowercase
    name = name.lower()
    return name

def extract_elevation(name):
    """Extract elevation (mh) from sensor name."""
    # Look for pattern like "420mh" or "420 mh"
    match = re.search(r'(\d+)\s*mh', name, re.IGNORECASE)
    if match:
        return float(match.group(1))
    return 0.0

def generate_ini(sensor_id, name, latitude, longitude, altitude, output_dir):
    """Generate an individual INI file for a sensor."""
    
    clean_sensor_name = clean_name(name)
    ini_filename = f"{sensor_id}_{clean_sensor_name}.ini"
    ini_path = os.path.join(output_dir, ini_filename)
    
    # Replace placeholders in template
    ini_content = f"""[General]
BUFFER_SIZE = 370
BUFF_BEFORE = 1.5
BUFF_GRIDS = 10

[Input]
COORDSYS = UTM
COORDPARAM = 27
TIME_ZONE = 0
METEO = SMET
METEOPATH = ./data/smet_files
METEOFILE = {sensor_id}_smet_merged.txt
SNOW = SMET
SNOWPATH = ./data/snow_heights
SNOWFILE = {sensor_id}_{clean_sensor_name}_hs.sno
STARTDATE = 2025-09-01T00:00
ENDDATE = 2026-07-01T00:00

[InputEditing]
ENABLE_TIMESERIES_EDITING = TRUE
{sensor_id}_{clean_sensor_name}_smet::EDIT1 = CREATE
{sensor_id}_{clean_sensor_name}_smet::ARG1::PARAM = TSG
{sensor_id}_{clean_sensor_name}_smet::ARG1::ALGORITHM = CST
{sensor_id}_{clean_sensor_name}_smet::ARG1::VALUE = 273.15

[Snowpack]
CALCULATION_STEP_LENGTH = 60
ROUGHNESS_LENGTH = 0.002
HEIGHT_OF_METEO_VALUES = 2
HEIGHT_OF_WIND_VALUE = 10
ENFORCE_MEASURED_SNOW_HEIGHTS = TRUE
INFLATE_ALLOW = TRUE
INFLATE_UPON_INIT = FALSE
SW_MODE = INCOMING
ATMOSPHERIC_STABILITY = MO_HOLTSLAG
CHANGE_BC = FALSE
SNP_SOIL = FALSE
CANOPY = FALSE
FORCE_SW_MODE = TRUE

[Filters]
ENABLE_METEO_FILTERS = TRUE
ENABLE_TIME_FILTERS = TRUE

[Output]
COORDSYS = UTM
COORDPARAM = 27
TIME_ZONE = 0
METEOPATH = ./output
WRITE_PROCESSED_METEO = FALSE
EXPERIMENT = {sensor_id}_{clean_sensor_name}
SNOW_WRITE = FALSE
PROF_WRITE = TRUE
PROF_FORMAT = PRO PRF
AGGREGATE_PRO = FALSE
AGGREGATE_PRF = FALSE
PROF_START = 0.041666
PROF_DAYS_BETWEEN = 0.041666
PROF_ID_OR_MK = ID
PROF_AGE_OR_DATE = AGE
HARDNESS_IN_NEWTON = FALSE
CLASSIFY_PROFILE = TRUE
TS_WRITE = TRUE
TS_FORMAT = SMET

[Interpolations1D]
MAX_GAP_SIZE = 86400
PSUM::ARG1::PERIOD = 3600
PSUM::ARG1::ALGORITHM = ACCUMULATE
PSUM::ARG1::MAX_GAP_SIZE = 8400
ENABLE_RESAMPLING = TRUE
"""
    
    with open(ini_path, 'w') as f:
        f.write(ini_content)
    
    print(f"Created: {ini_filename}")
    return ini_filename

def generate_sno(sensor_id, name, latitude, longitude, altitude, output_dir):
    """Generate an empty .sno file for a sensor."""
    
    clean_sensor_name = clean_name(name)
    sno_filename = f"{sensor_id}_{clean_sensor_name}_hs.sno"
    sno_path = os.path.join(output_dir, sno_filename)
    
    sno_content = f"""SMET 1.1 ASCII

[HEADER]
station_id       = {sensor_id}
station_name     = {name}
latitude         = {latitude}
longitude        = {longitude}
altitude         = {altitude}
SlopeAngle       = 0
SlopeAzi         = 0
ProfileDate      = 2025-09-01T00:00:00
tz               = 0
nodata           = -999
HS_Last          = 0
nSoilLayerData   = 0
nSnowLayerData   = 0
SoilAlbedo       = 0.15
BareSoil_z0      = 0.2
CanopyHeight     = 0
CanopyLeafAreaIndex = 0
CanopyDirectThroughfall = 1
WindScalingFactor = 1
ErosionLevel     = 0
TimeCountDeltaHS = 0
fields = timestamp Layer_Thick T Vol_Frac_I Vol_Frac_W Vol_Frac_V Vol_Frac_S Rho_S Conduc_S HeatCapac_S rg rb dd sp mk mass_hoar ne CDot metamo

[DATA]
"""
    
    with open(sno_path, 'w') as f:
        f.write(sno_content)
    
    print(f"Created: {sno_filename}")
    return sno_filename

def main():
    """Read sensors_list.txt and generate configs."""
    
    # Determine base directory
    script_dir = os.path.dirname(os.path.abspath(__file__))
    base_dir = os.path.dirname(script_dir)
    
    sensors_file = os.path.join(base_dir, 'sensors_list.txt')
    config_dir = os.path.join(base_dir, 'config')
    snow_heights_dir = os.path.join(base_dir, 'data', 'snow_heights')
    
    # Create directories if they don't exist
    os.makedirs(config_dir, exist_ok=True)
    os.makedirs(snow_heights_dir, exist_ok=True)
    
    if not os.path.exists(sensors_file):
        print(f"Error: {sensors_file} not found!")
        return
    
    print(f"Reading sensors from: {sensors_file}")
    print(f"Config output: {config_dir}")
    print(f"SNO files output: {snow_heights_dir}")
    print()
    
    ini_count = 0
    sno_count = 0
    
    with open(sensors_file, 'r') as f:
        reader = csv.DictReader(f)
        for row in reader:
            sensor_id = row['sensorId'].strip()
            name = row['name'].strip()
            latitude = float(row['latitude'])
            longitude = float(row['longitude'])
            
            # Skip sensors with bad coordinates (0, 0)
            if latitude == 0 and longitude == 0:
                print(f"Skipping {sensor_id} (bad coordinates)")
                continue
            
            altitude = extract_elevation(name)
            
            # Generate INI file
            generate_ini(sensor_id, name, latitude, longitude, altitude, config_dir)
            ini_count += 1
            
            # Generate empty SNO file
            generate_sno(sensor_id, name, latitude, longitude, altitude, snow_heights_dir)
            sno_count += 1
    
    print()
    print(f"Generated {ini_count} INI files in {config_dir}")
    print(f"Generated {sno_count} SNO files in {snow_heights_dir}")

if __name__ == "__main__":
    main()
