#!/usr/bin/env python3
"""
Operational wrapper for snowpack pipeline.
Runs hourly via cron to:
1. Merge latest snow height data
2. Generate station configs with current time as ENDDATE
3. Run snowpack for all sensors
4. Copy output to archive
"""

import os
import sys
import subprocess
from datetime import datetime
from pathlib import Path

def log(msg):
    """Print log message with timestamp."""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{timestamp}] {msg}")

def run_command(cmd, description):
    """Run a shell command and return success status."""
    log(f"Running: {description}")
    try:
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
        if result.returncode != 0:
            log(f"ERROR: {description} failed")
            if result.stdout:
                log(f"STDOUT:\n{result.stdout}")
            if result.stderr:
                log(f"STDERR:\n{result.stderr}")
            return False
        log(f"✓ {description} completed")
        return True
    except Exception as e:
        log(f"ERROR: Exception in {description}: {e}")
        return False

def get_base_dir():
    """Get the snowpack project base directory."""
    script_dir = os.path.dirname(os.path.abspath(__file__))
    return os.path.dirname(script_dir)

def merge_snow_heights():
    """Run the merge_snow_heights.py script."""
    base_dir = get_base_dir()
    merge_script = os.path.join(base_dir, "scripts", "merge_snow_heights.py")
    return run_command(f"cd {base_dir} && python3 {merge_script}", "Merge snow heights")

def update_ini_end_dates():
    """Update ENDDATE in all INI files to current time."""
    base_dir = get_base_dir()
    config_dir = os.path.join(base_dir, "config")
    
    # Format: ISO 8601 without seconds
    current_time = datetime.now().strftime("%Y-%m-%dT%H:%M")
    
    log(f"Updating INI files with ENDDATE = {current_time}")
    
    ini_count = 0
    for ini_file in Path(config_dir).glob("*.ini"):
        try:
            with open(ini_file, 'r') as f:
                content = f.read()
            
            # Replace ENDDATE line
            import re
            new_content = re.sub(
                r'ENDDATE = \d{4}-\d{2}-\d{2}T\d{2}:\d{2}',
                f'ENDDATE = {current_time}',
                content
            )
            
            if new_content != content:
                with open(ini_file, 'w') as f:
                    f.write(new_content)
                ini_count += 1
        except Exception as e:
            log(f"ERROR updating {ini_file.name}: {e}")
            return False
    
    log(f"✓ Updated ENDDATE in {ini_count} INI files")
    return current_time

def run_snowpack(end_time):
    """Run snowpack for all sensors."""
    base_dir = get_base_dir()
    config_dir = os.path.join(base_dir, "config")
    
    cmd = f"cd {base_dir} && snowpack -c {config_dir}/*.ini -b 2025-09-01T00:00 -e {end_time} 2>&1"
    return run_command(cmd, "Snowpack simulation")

def copy_output_to_archive():
    """Copy output directory to archive location."""
    base_dir = get_base_dir()
    output_dir = os.path.join(base_dir, "output")
    archive_dir = os.path.join(os.path.dirname(base_dir), "vakt", "snowpack")
    
    # Create archive directory if it doesn't exist
    os.makedirs(archive_dir, exist_ok=True)
    
    cmd = f"cp -r {output_dir}/* {archive_dir}/"
    return run_command(cmd, f"Copy output to {archive_dir}")

def main():
    """Run the complete operational pipeline."""
    log("=" * 70)
    log("SNOWPACK Operational Pipeline")
    log("=" * 70)
    
    base_dir = get_base_dir()
    
    # Verify directories exist
    if not os.path.exists(base_dir):
        log(f"ERROR: Base directory not found: {base_dir}")
        sys.exit(1)
    
    config_dir = os.path.join(base_dir, "config")
    output_dir = os.path.join(base_dir, "output")
    
    os.makedirs(config_dir, exist_ok=True)
    os.makedirs(output_dir, exist_ok=True)
    
    # Step 1: Merge latest snow height data
    if not merge_snow_heights():
        log("ERROR: Merge failed, aborting pipeline")
        sys.exit(1)
    
    # Step 2: Update INI end dates to current time
    end_time = update_ini_end_dates()
    if not end_time:
        log("ERROR: INI update failed, aborting pipeline")
        sys.exit(1)
    
    # Step 3: Run snowpack
    if not run_snowpack(end_time):
        log("WARNING: Snowpack run completed with errors")
        # Continue to archive step anyway
    
    # Step 4: Copy output to archive
    if not copy_output_to_archive():
        log("WARNING: Archive copy failed")
    
    log("=" * 70)
    log("Pipeline completed")
    log("=" * 70)

if __name__ == "__main__":
    main()
