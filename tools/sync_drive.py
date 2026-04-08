import os
import subprocess
import sys
import argparse

# Configuration
RCLONE_EXE = os.path.join(os.path.dirname(__file__), "rclone", "rclone.exe")
LOCAL_DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data")
REMOTE_PATH = "gdrive:gojep_data"

def run_command(args, capture=False):
    """Run a command and handle errors."""
    try:
        if capture:
            result = subprocess.run(args, capture_output=True, text=True, check=True)
            return result.stdout.strip()
        else:
            subprocess.run(args, check=True)
            return True
    except subprocess.CalledProcessError as e:
        print(f"Error running command: {' '.join(args)}")
        if e.stderr:
            print(f"Stderr: {e.stderr}")
        return False

def sync(resync=False, dry_run=False):
    """Run rclone bisync between local data and Google Drive."""
    if not os.path.exists(RCLONE_EXE):
        print(f"Error: rclone.exe not found at {RCLONE_EXE}")
        return False

    if not os.path.exists(LOCAL_DATA_DIR):
        os.makedirs(LOCAL_DATA_DIR, exist_ok=True)

    cmd = [
        RCLONE_EXE, "bisync",
        LOCAL_DATA_DIR,
        REMOTE_PATH,
        "-v",
        "--compare", "size,modtime",
        "--slow-hash-sync-only",
        "--fix-case"
    ]

    if resync:
        print(">>> Performing initial resync (baseline setup)...")
        cmd.append("--resync")
    
    if dry_run:
        print(">>> Dry run enabled. No files will be moved.")
        cmd.append("--dry-run")

    print(f"Syncing {LOCAL_DATA_DIR} <-> {REMOTE_PATH}...")
    return run_command(cmd)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Sync local data with Google Drive via Rclone.")
    parser.add_argument("--resync", action="store_true", help="Perform initial resync (required once).")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be done without making changes.")
    parser.add_argument("--quiet", action="store_true", help="Suppress output (used for automation).")
    
    args = parser.parse_args()
    
    success = sync(resync=args.resync, dry_run=args.dry_run)
    if success:
        if not args.quiet:
            print("Sync completed successfully.")
    else:
        sys.exit(1)
