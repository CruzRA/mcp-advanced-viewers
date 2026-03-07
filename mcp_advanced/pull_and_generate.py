#!/usr/bin/env python3
"""
End-to-end pipeline: Pull data from Google Sheets → Download trajectories → Generate viewers.

Usage:
    python pull_and_generate.py [--sheet-id SHEET_ID] [--gid GID] [--skip-download]

Defaults to the configured sheet ID. Requires the service account key at
the project root (rafaelcruzpydrive-*.json) and the sheet shared with
scripts@rafaelcruzpydrive.iam.gserviceaccount.com as Viewer.
"""

import argparse
import csv
import glob
import json
import os
import subprocess
import sys
import time

csv.field_size_limit(sys.maxsize)

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(SCRIPT_DIR)

# Default Google Sheet
DEFAULT_SHEET_ID = "1PT1eA-YhTLs-DSV4BtNyFs5MkpqOhM9zxJwzyMJGTYk"
DEFAULT_GID = 0

# Service account key (glob pattern)
SA_KEY_GLOB = os.path.join(PROJECT_ROOT, "rafaelcruzpydrive-*.json")


def find_sa_key():
    """Find the service account JSON key file."""
    matches = glob.glob(SA_KEY_GLOB)
    if not matches:
        print(f"✗ No service account key found matching {SA_KEY_GLOB}")
        sys.exit(1)
    return matches[0]


def pull_sheet(sheet_id, gid=0):
    """Pull data from Google Sheets and return as list of dicts."""
    import gspread
    from google.oauth2.service_account import Credentials

    sa_key = find_sa_key()
    print(f"Service account key: {os.path.basename(sa_key)}")

    scopes = [
        "https://www.googleapis.com/auth/spreadsheets.readonly",
        "https://www.googleapis.com/auth/drive.readonly",
    ]
    creds = Credentials.from_service_account_file(sa_key, scopes=scopes)
    gc = gspread.authorize(creds)

    print(f"Opening sheet {sheet_id} (gid={gid})...")
    sh = gc.open_by_key(sheet_id)

    # Find worksheet by gid
    ws = None
    for w in sh.worksheets():
        if w.id == gid:
            ws = w
            break
    if ws is None:
        ws = sh.sheet1
        print(f"  ⚠ gid={gid} not found, using first sheet: {ws.title}")
    else:
        print(f"  Sheet: {ws.title}")

    rows = ws.get_all_records()
    print(f"  Rows: {len(rows)}")

    # Normalize column names to lowercase
    normalized = []
    for row in rows:
        normalized.append({k.lower().strip(): v for k, v in row.items()})

    return normalized


def write_csv(rows, out_path):
    """Write list of dicts to CSV."""
    if not rows:
        print("✗ No rows to write")
        return
    fieldnames = list(rows[0].keys())
    with open(out_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    size_kb = os.path.getsize(out_path) // 1024
    print(f"  Wrote {out_path} ({len(rows)} rows, {size_kb}KB)")


def main():
    parser = argparse.ArgumentParser(description="Pull from Google Sheets and generate viewers")
    parser.add_argument("--sheet-id", default=DEFAULT_SHEET_ID, help="Google Sheet ID")
    parser.add_argument("--gid", type=int, default=DEFAULT_GID, help="Worksheet gid")
    parser.add_argument("--skip-download", action="store_true", help="Skip trajectory download")
    parser.add_argument("--skip-generate", action="store_true", help="Skip viewer generation")
    args = parser.parse_args()

    # Step 1: Pull from Google Sheets
    print("=" * 60)
    print("STEP 1: Pull data from Google Sheets")
    print("=" * 60)
    rows = pull_sheet(args.sheet_id, args.gid)

    # Check required columns
    if not rows:
        print("✗ No data in sheet")
        sys.exit(1)

    cols = set(rows[0].keys())
    if "taskid" not in cols:
        print(f"✗ Missing 'taskid' column. Found: {sorted(cols)}")
        sys.exit(1)
    if "response" not in cols:
        print(f"✗ Missing 'response' column. Found: {sorted(cols)}")
        sys.exit(1)

    has_traj = "trajectory_urls" in cols
    print(f"  Columns: {sorted(cols)}")
    print(f"  trajectory_urls: {'yes' if has_traj else 'no'}")

    # Check trajectory URL freshness
    if has_traj:
        sample_traj = rows[0].get("trajectory_urls", "")
        if sample_traj:
            try:
                parsed = json.loads(sample_traj) if isinstance(sample_traj, str) else sample_traj
                if isinstance(parsed, dict):
                    first_url = next(iter(parsed.values()), "")
                    if "X-Amz-Date" in first_url:
                        import urllib.parse
                        params = urllib.parse.parse_qs(urllib.parse.urlparse(first_url).query)
                        amz_date = params.get("X-Amz-Date", [""])[0]
                        if amz_date:
                            from datetime import datetime
                            created = datetime.strptime(amz_date, "%Y%m%dT%H%M%SZ")
                            age_min = (datetime.utcnow() - created).total_seconds() / 60
                            print(f"  URL age: {age_min:.0f} minutes")
                            if age_min > 45:
                                print(f"  ⚠ URLs are {age_min:.0f}min old — tokens may expire soon!")
            except Exception:
                pass

    # Write CSV
    csv_path = os.path.join(SCRIPT_DIR, "sheet_data.csv")
    write_csv(rows, csv_path)

    # Step 2: Download trajectories
    if not args.skip_download and has_traj:
        print()
        print("=" * 60)
        print("STEP 2: Download trajectories")
        print("=" * 60)

        # Clear existing trajectories to force fresh download
        traj_dir = os.path.join(SCRIPT_DIR, "trajectories")
        if os.path.isdir(traj_dir):
            import shutil
            shutil.rmtree(traj_dir)
            print(f"  Cleared {traj_dir}")
        os.makedirs(traj_dir, exist_ok=True)

        dl_script = os.path.join(SCRIPT_DIR, "download_trajectories.py")
        t0 = time.time()
        result = subprocess.run(
            [sys.executable, dl_script, csv_path],
            cwd=SCRIPT_DIR,
        )
        elapsed = time.time() - t0
        print(f"  Download completed in {elapsed:.0f}s (exit code {result.returncode})")
    elif args.skip_download:
        print("\n⏭ Skipping trajectory download (--skip-download)")
    else:
        print("\n⏭ No trajectory_urls column — skipping download")

    # Step 3: Generate viewers
    if not args.skip_generate:
        print()
        print("=" * 60)
        print("STEP 3: Generate viewers")
        print("=" * 60)
        gen_script = os.path.join(SCRIPT_DIR, "generate_csv_viewers.py")
        t0 = time.time()
        result = subprocess.run(
            [sys.executable, gen_script, csv_path],
            cwd=SCRIPT_DIR,
        )
        elapsed = time.time() - t0
        print(f"  Generation completed in {elapsed:.0f}s (exit code {result.returncode})")
    else:
        print("\n⏭ Skipping viewer generation (--skip-generate)")

    print()
    print("=" * 60)
    print("DONE")
    print("=" * 60)


if __name__ == "__main__":
    main()
