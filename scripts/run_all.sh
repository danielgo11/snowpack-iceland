#!/bin/bash
# Master run script for SNOWPACK modeling in Iceland
# Orchestrates: fetch_snow_heights -> generate_smet -> fetch_precip -> merge_snow_heights -> run_snowpack for all 32 sensors

set -e

echo "========================================================================"
echo "SNOWPACK Iceland - Master Run Script"
echo "========================================================================"
echo "Date: $(date)"
echo ""

# Configuration
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BASE_DIR="$(dirname "$SCRIPT_DIR")"
cd "$BASE_DIR"

echo "Base directory: $BASE_DIR"
echo "Script directory: $SCRIPT_DIR"
echo ""

# Step 1: Fetch snow height data from sensors
echo "========================================================================"
echo "STEP 1: Fetching snow height data from Snowsense sensors..."
echo "========================================================================"
python3 "$SCRIPT_DIR/fetch_snow_heights.py"
if [ $? -ne 0 ]; then
    echo "ERROR: fetch_snow_heights.py failed"
    exit 1
fi
echo ""

# Step 2: Generate SMET files from IMO meteorological data
echo "========================================================================"
echo "STEP 2: Generating SMET files from IMO meteorological stations..."
echo "========================================================================"
python3 "$SCRIPT_DIR/generate_smet.py"
if [ $? -ne 0 ]; then
    echo "ERROR: generate_smet.py failed"
    exit 1
fi
echo ""

# Step 3: Fetch precipitation data
echo "========================================================================"
echo "STEP 3: Fetching precipitation data from SYNOP stations..."
echo "========================================================================"
python3 "$SCRIPT_DIR/fetch_precip.py"
if [ $? -ne 0 ]; then
    echo "WARNING: fetch_precip.py had issues (may not be critical)"
fi
echo ""

# Step 4: Merge snow heights into SMET files
echo "========================================================================"
echo "STEP 4: Merging snow height data into SMET files..."
echo "========================================================================"
python3 "$SCRIPT_DIR/merge_snow_heights.py"
if [ $? -ne 0 ]; then
    echo "ERROR: merge_snow_heights.py failed"
    exit 1
fi
echo ""

# Step 5: Run SNOWPACK for all sensors
echo "========================================================================"
echo "STEP 5: Running SNOWPACK for all 32 sensors..."
echo "========================================================================"

CONFIG_DIR="$BASE_DIR/config"
SNOWPACK_CMD="snowpack"

if ! command -v $SNOWPACK_CMD &> /dev/null; then
    echo "ERROR: snowpack command not found. Make sure SNOWPACK is installed and in PATH"
    exit 1
fi

# Count INI files
INI_FILES=$(find "$CONFIG_DIR" -name "*.ini" -type f | sort)
INI_COUNT=$(echo "$INI_FILES" | wc -l)

echo "Found $INI_COUNT INI configuration files in $CONFIG_DIR"
echo ""

# Track results
TOTAL=0
SUCCESS=0
FAILED=0
FAILED_SENSORS=()

for ini_file in $INI_FILES; do
    TOTAL=$((TOTAL + 1))
    INI_NAME=$(basename "$ini_file")
    SENSOR_ID=$(echo "$INI_NAME" | cut -d'_' -f1)
    
    echo "[$TOTAL/$INI_COUNT] Running SNOWPACK for sensor $SENSOR_ID..."
    echo "  Config: $INI_NAME"
    
    # Run snowpack from the base directory so relative paths work correctly
    if $SNOWPACK_CMD "$ini_file" > /dev/null 2>&1; then
        SUCCESS=$((SUCCESS + 1))
        echo "  ✓ SUCCESS"
    else
        FAILED=$((FAILED + 1))
        FAILED_SENSORS+=("$SENSOR_ID")
        echo "  ✗ FAILED"
    fi
done

echo ""
echo "========================================================================"
echo "SNOWPACK Run Summary"
echo "========================================================================"
echo "Total sensors: $TOTAL"
echo "Successful:    $SUCCESS"
echo "Failed:        $FAILED"

if [ $FAILED -gt 0 ]; then
    echo ""
    echo "Failed sensors:"
    for sensor_id in "${FAILED_SENSORS[@]}"; do
        echo "  - $sensor_id"
    done
fi

echo ""
echo "Output directory: $BASE_DIR/output/"
echo ""
echo "Completed at: $(date)"
echo "========================================================================"

if [ $FAILED -eq 0 ]; then
    echo "All sensors completed successfully!"
    exit 0
else
    echo "Some sensors failed. Check logs above."
    exit 1
fi
