#!/bin/bash
# Fix all INI files to use simple sensor ID SMET filenames
# Changes METEOFILE from long names to just {sensor_id}_smet_merged.txt

CONFIG_DIR="./config"

echo "Fixing METEOFILE references in all INI files..."
echo ""

fixed=0
for ini_file in "$CONFIG_DIR"/*.ini; do
    if [ ! -f "$ini_file" ]; then
        continue
    fi
    
    ini_name=$(basename "$ini_file")
    # Extract sensor ID from filename (first part before underscore)
    sensor_id=$(echo "$ini_name" | cut -d'_' -f1)
    
    # Check if file has METEOFILE reference
    if grep -q "^METEOFILE" "$ini_file"; then
        # Replace METEOFILE line with simple format
        sed -i "s/^METEOFILE = .*/METEOFILE = ${sensor_id}_smet_merged.txt/" "$ini_file"
        echo "✓ Fixed $ini_name (sensor $sensor_id)"
        fixed=$((fixed + 1))
    fi
done

echo ""
echo "Fixed $fixed INI files"
echo ""
echo "Verifying changes (showing first 3 results):"
grep "^METEOFILE" "$CONFIG_DIR"/*.ini | head -3
