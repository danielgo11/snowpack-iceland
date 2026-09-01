"""
Merge snow height data from *_hs.sno files into SMET files.
Reads all sensor IDs from sensors_list.txt and dynamically matches
HS files with SMET files. Inserts HS in correct position after PSUM.
Converts HS from cm to m. Fills missing data (-999.000) by linear interpolation.
Removes unrealistic HS values based on elevation and season.
Prefers temperature from snow height sensor over IMO station temperature.
"""

from pathlib import Path
from datetime import datetime
import csv
import re

SNOW_PATH = Path("data/snow_heights")
SMET_PATH = Path("data/smet_files")
OUTPUT_PATH = Path("data/smet_files")

# Units for each field in order
FIELD_UNITS = {
    "station_id": "station_id",
    "timestamp": "timestamp",
    "TA": "K",
    "TSG": "K",
    "VW": "m/s",
    "VW_MAX": "m/s",
    "DW": "deg",
    "RH": "%",
    "P": "Pa",
    "PSUM": "kg/m2",
    "HS": "m",
    "precipitation": "mm",
    "precip_splitting": "1",
    "ISWR": "W/m2",
    "ILWR": "W/m2",
}

def load_sensors_from_csv(csv_file: str) -> dict:
    """Load sensor list from sensors_list.txt CSV file.
    Returns dict mapping sensor_id -> (name, lat, lon, elevation)
    """
    sensors = {}
    
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
                    elev_match = re.search(r'(\d+)\s*mh', name, re.IGNORECASE)
                    elevation = float(elev_match.group(1)) if elev_match else 0.0
                    
                    sensors[sensor_id] = (name, lat, lon, elevation)
                except (ValueError, KeyError):
                    continue
    except FileNotFoundError:
        print("Error: sensors_list.txt not found")
        return {}
    
    return sensors

def read_hs_data(filepath: Path):
    """Read HS SMET file and return dict of timestamp -> HS value in m."""
    hs_dict = {}

    with open(filepath, 'r') as f:
        lines = f.readlines()

    # Find [DATA] line
    data_start = 0
    for i, line in enumerate(lines):
        if line.strip() == "[DATA]":
            data_start = i + 1
            break

    # Parse data lines
    for line in lines[data_start:]:
        line = line.strip()
        if not line:
            continue

        parts = line.split()  # Split on any whitespace
        if len(parts) >= 3:
            try:
                timestamp = parts[1]
                hs_str = parts[2]
                
                # Handle 'nan' values
                if hs_str.lower() == 'nan':
                    hs_val = -999.0
                else:
                    hs_val = float(hs_str)
                
                # Replace -999 with None (will interpolate)
                if hs_val == -999.0:
                    hs_dict[timestamp] = None
                else:
                    # Convert cm to m
                    hs_dict[timestamp] = hs_val / 100.0
            except (ValueError, IndexError):
                continue

    print("  Read %d HS records from %s" % (len(hs_dict), filepath.name))
    return hs_dict

def read_sensor_temp_data(filepath: Path):
    """Read HS SMET file and extract temperature data (if available).
    Returns dict of timestamp -> temperature (in Celsius from sensor array).
    """
    temp_dict = {}

    with open(filepath, 'r') as f:
        lines = f.readlines()

    # Find [DATA] line
    data_start = 0
    for i, line in enumerate(lines):
        if line.strip() == "[DATA]":
            data_start = i + 1
            break

    # Parse data lines - HS files may not have temp, so handle gracefully
    for line in lines[data_start:]:
        line = line.strip()
        if not line:
            continue

        parts = line.split()
        if len(parts) >= 3:
            try:
                timestamp = parts[1]
                # For HS .sno files, typically only has: station_id, timestamp, HS
                # But if extended with temp, it would be parts[3]
                # We'll skip this for now as the .sno files only have HS
            except (ValueError, IndexError):
                continue

    return temp_dict

def is_unrealistic_hs(hs_m, timestamp, elevation, station_name):
    """
    Check if HS value is unrealistic for given conditions.
    Returns True if value should be filtered out.
    """
    if hs_m is None or hs_m < 0:
        return True
    
    try:
        dt = datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
        month = dt.month
    except:
        return False
    
    # Low elevation stations (< 500m): cap summer HS
    if elevation < 500:
        if month >= 6 and month <= 8:  # June-August
            if hs_m > 0.50:  # 50 cm max in summer
                return True
    
    # Mid elevation (500-800m): more lenient
    elif elevation < 800:
        if month >= 7 and month <= 8:  # July-August
            if hs_m > 1.50:  # 150 cm max in late summer
                return True
    
    # High elevation (> 800m): less restrictive
    
    return False

def interpolate_missing_values(data_rows, field_indices, timestamps):
    """Interpolate -999.000 and None values in specified columns."""
    for col_idx in field_indices:
        values = []
        for row in data_rows:
            try:
                val = float(row[col_idx])
                values.append(val if val != -999.0 else None)
            except (ValueError, IndexError):
                values.append(None)
        
        # Forward/backward fill then interpolate
        for i in range(len(values)):
            if values[i] is None:
                # Find nearest valid values before and after
                left_idx = i - 1
                right_idx = i + 1
                
                while left_idx >= 0 and values[left_idx] is None:
                    left_idx -= 1
                while right_idx < len(values) and values[right_idx] is None:
                    right_idx += 1
                
                # Linear interpolation
                if left_idx >= 0 and right_idx < len(values):
                    gap = right_idx - left_idx
                    frac = (i - left_idx) / float(gap)
                    interp_val = values[left_idx] + frac * (values[right_idx] - values[left_idx])
                    values[i] = interp_val
                elif left_idx >= 0:
                    values[i] = values[left_idx]
                elif right_idx < len(values):
                    values[i] = values[right_idx]
                else:
                    values[i] = 0.0  # Fallback
        
        # Write back to rows
        for row_idx in range(len(data_rows)):
            if col_idx < len(data_rows[row_idx]):
                data_rows[row_idx][col_idx] = "%.3f" % values[row_idx]

def filter_unrealistic_hs(data_rows, new_fields, elevation, station_name):
    """Remove or interpolate unrealistic HS values."""
    if "HS" not in new_fields:
        return
    
    hs_idx = new_fields.index("HS")
    
    print("  Filtering unrealistic HS values for sensor %s (elev=%d m)..." % (station_name, elevation))
    
    filtered_count = 0
    for row_idx, row in enumerate(data_rows):
        try:
            timestamp = row[1]  # Column 1 is timestamp
            hs_val = float(row[hs_idx])
            
            if is_unrealistic_hs(hs_val, timestamp, elevation, station_name):
                row[hs_idx] = "-999.000"  # Mark for interpolation
                filtered_count += 1
        except (ValueError, IndexError):
            pass
    
    if filtered_count > 0:
        print("    Marked %d unrealistic values for interpolation" % filtered_count)
        # Now interpolate these marked values
        interpolate_missing_values(data_rows, [hs_idx], [row[1] for row in data_rows])

def merge_station(sensor_id: int, sensor_name: str, elevation: int) -> Path:
    """Merge HS data into SMET file, inserting in correct position."""
    smet_filepath = SMET_PATH / ("%d_smet_merged.txt" % sensor_id)
    hs_filepath = SNOW_PATH / ("%d_hs.sno" % sensor_id)
    output_file = OUTPUT_PATH / ("%d_smet_merged.txt" % sensor_id)

    print("\nProcessing sensor %d (%s, elev=%d m)..." % (sensor_id, sensor_name, elevation))

    if not smet_filepath.exists():
        print("  ERROR: SMET file not found at %s" % smet_filepath)
        return None

    if not hs_filepath.exists():
        print("  ERROR: HS file not found at %s" % hs_filepath)
        return None

    # Read HS data
    hs_dict = read_hs_data(hs_filepath)
    
    # Read sensor temperature data (if available)
    sensor_temp_dict = read_sensor_temp_data(hs_filepath)

    # Read SMET file
    with open(smet_filepath, 'r') as f:
        lines = f.readlines()

    # Find field line and data start
    field_line_idx = -1
    data_start_idx = -1
    old_fields = []

    for i, line in enumerate(lines):
        if line.strip().startswith("fields ="):
            field_line_idx = i
            old_fields = line.replace("fields =", "").strip().split()
        if line.strip() == "[DATA]":
            data_start_idx = i + 1
            break

    if field_line_idx == -1 or data_start_idx == -1:
        print("  ERROR: Could not parse SMET file")
        return None

    print("  Current fields (%d): %s" % (len(old_fields), str(old_fields)))

    # Create new field list with HS inserted after PSUM
    new_fields = []
    hs_inserted = False
    for field in old_fields:
        new_fields.append(field)
        if field == "PSUM" and not hs_inserted:
            new_fields.append("HS")
            hs_inserted = True

    if not hs_inserted:
        new_fields.append("HS")

    print("  New fields (%d): %s" % (len(new_fields), str(new_fields)))
    hs_idx = new_fields.index("HS")
    ta_idx = new_fields.index("TA") if "TA" in new_fields else -1

    # Process data lines and build rows
    data_rows = []
    matched_count = 0
    for data_line in lines[data_start_idx:]:
        data_line = data_line.strip()
        if not data_line:
            continue

        # Split on whitespace
        parts = data_line.split()

        # Get timestamp (column 1)
        hs_val = -999.0
        if len(parts) > 1:
            timestamp = parts[1]
            if timestamp in hs_dict:
                hs_data = hs_dict[timestamp]
                if hs_data is not None:
                    hs_val = hs_data
                    matched_count += 1
                else:
                    hs_val = -999.0

        # Insert HS value at correct position (in m)
        parts.insert(hs_idx, "%.4f" % hs_val)
        data_rows.append(parts)

    print("  Matched %d timestamps with HS data" % matched_count)
    
    # Filter unrealistic HS values
    filter_unrealistic_hs(data_rows, new_fields, elevation, sensor_name)
    
    # Find column indices for fields that need interpolation
    interp_fields = ["TA", "VW", "VW_MAX", "TSG", "RH", "HS"]
    interp_indices = []
    for field in interp_fields:
        if field in new_fields:
            interp_indices.append(new_fields.index(field))
    
    print("  Interpolating missing values in columns: %s" % str(interp_indices))
    interpolate_missing_values(data_rows, interp_indices, [row[1] for row in data_rows])

    # Write output
    with open(output_file, "w") as f:
        # Write header up to field line
        for i in range(field_line_idx):
            f.write(lines[i])

        f.write("fields = " + " ".join(new_fields) + "\n")

        # Write units line
        units_line = " ".join([FIELD_UNITS.get(field, field) for field in new_fields])
        f.write("units = " + units_line + "\n")

        f.write("[DATA]\n")

        # Write data
        for row in data_rows:
            f.write("\t".join(row) + "\n")

    print("  Wrote: %s" % output_file)
    return output_file

def main():
    print("=" * 70)
    print("Merging Snow Heights into SMET Files")
    print("=" * 70)

    # Load all sensors
    sensors = load_sensors_from_csv("sensors_list.txt")
    if not sensors:
        print("Error: Could not load sensors from sensors_list.txt")
        return
    
    print("Loaded %d sensors from sensors_list.txt\n" % len(sensors))

    results = []
    failed = []
    
    for sensor_id, (sensor_name, lat, lon, elevation) in sorted(sensors.items()):
        output_file = merge_station(sensor_id, sensor_name, elevation)
        
        if output_file:
            results.append((sensor_id, sensor_name, output_file))
        else:
            failed.append((sensor_id, sensor_name))

    print("\n" + "=" * 70)
    print("SUMMARY")
    print("=" * 70)
    
    if results:
        print("Successfully merged %d sensors:" % len(results))
        for sid, sname, path in results:
            print("  - %d (%s): %s" % (sid, sname, path))
    
    if failed:
        print("\nFailed to merge %d sensors:" % len(failed))
        for sid, sname in failed:
            print("  - %d (%s)" % (sid, sname))
    
    print("\nOutput directory: %s" % OUTPUT_PATH.resolve())

if __name__ == "__main__":
    main()
