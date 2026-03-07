#!/usr/bin/env python3
"""
MCP Advanced Viewer Pipeline
=============================
End-to-end pipeline:  Google Sheets → Download trajectories → Generate HTML viewers → Push to GitHub

Steps:
    1. Pull task data from a Google Sheet (requires service account credentials)
    2. Download trajectory JSONs from pre-signed S3 URLs in the sheet
    3. Generate interactive HTML trace viewers + homepage
    4. Commit & push generated viewers to the git repository

Usage:
    python run.py                          # full pipeline
    python run.py --skip-download          # reuse cached trajectories
    python run.py --skip-generate          # only pull + download
    python run.py --skip-push              # skip git commit/push
    python run.py --sheet-id <ID> --gid 0  # custom sheet

Prerequisites:
    - Service account JSON key in credentials/ directory
    - Google Sheet shared with the service account email as Viewer
    - pip install -r requirements.txt
    - Git remote configured (run `git remote add origin <url>` once)
"""

import argparse
import csv
import glob
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.parse
from datetime import datetime, timezone

csv.field_size_limit(sys.maxsize)

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
OUTPUT_DIR = os.path.join(SCRIPT_DIR, "output")
CREDS_DIR = os.path.join(SCRIPT_DIR, "credentials")

# ── Defaults ──────────────────────────────────────────────────────
DEFAULT_SHEET_ID = "1PT1eA-YhTLs-DSV4BtNyFs5MkpqOhM9zxJwzyMJGTYk"
DEFAULT_GID = 0


# ── Helpers ───────────────────────────────────────────────────────

def find_sa_key():
    """Find the first service account JSON key in credentials/."""
    patterns = [
        os.path.join(CREDS_DIR, "*.json"),
        os.path.join(SCRIPT_DIR, "*.json"),     # fallback: pipeline root
    ]
    for pat in patterns:
        matches = [f for f in glob.glob(pat) if "package" not in f.lower()]
        if matches:
            return matches[0]
    print("✗ No service account key found.")
    print(f"  Place your Google service account JSON key in: {CREDS_DIR}/")
    sys.exit(1)


def pull_sheet(sheet_id, gid=0):
    """Pull all rows from a Google Sheet. Returns list of dicts."""
    import gspread
    from google.oauth2.service_account import Credentials

    sa_key = find_sa_key()
    print(f"  Service account: {os.path.basename(sa_key)}")

    creds = Credentials.from_service_account_file(sa_key, scopes=[
        "https://www.googleapis.com/auth/spreadsheets.readonly",
        "https://www.googleapis.com/auth/drive.readonly",
    ])
    gc = gspread.authorize(creds)

    print(f"  Opening sheet {sheet_id} (gid={gid})...")
    sh = gc.open_by_key(sheet_id)

    ws = None
    for w in sh.worksheets():
        if w.id == gid:
            ws = w
            break
    if ws is None:
        ws = sh.sheet1
        print(f"  ⚠ gid={gid} not found — using first sheet: {ws.title}")
    else:
        print(f"  Worksheet: {ws.title}")

    rows = ws.get_all_records()
    print(f"  Rows: {len(rows)}")

    # Normalize column names to lowercase
    return [{k.lower().strip(): v for k, v in row.items()} for row in rows]


def write_csv(rows, path):
    """Write list of dicts to CSV."""
    if not rows:
        print("  ✗ No rows to write")
        return
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    print(f"  → {path} ({len(rows)} rows, {os.path.getsize(path) // 1024}KB)")


def check_url_freshness(rows):
    """Check if pre-signed URLs are still valid (they expire in ~1 hour)."""
    for row in rows:
        traj = row.get("trajectory_urls", "")
        if not traj:
            continue
        try:
            parsed = json.loads(traj) if isinstance(traj, str) else traj
            if not isinstance(parsed, dict):
                continue
            url = next(iter(parsed.values()), "")
            if "X-Amz-Date" not in url:
                continue
            params = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
            amz_date = params.get("X-Amz-Date", [""])[0]
            if amz_date:
                created = datetime.strptime(amz_date, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
                age_min = (datetime.now(timezone.utc) - created).total_seconds() / 60
                print(f"  URL age: {age_min:.0f} minutes")
                if age_min > 45:
                    print(f"  ⚠ URLs are {age_min:.0f}min old — Cognito tokens may have expired!")
                    print(f"    If downloads fail, refresh the sheet and re-run.")
                return
        except Exception:
            continue


def run_step(label, script, *args):
    """Run a Python script as a subprocess."""
    print(f"\n{'='*60}")
    print(f"STEP: {label}")
    print(f"{'='*60}")
    t0 = time.time()
    result = subprocess.run(
        [sys.executable, script, *args],
        cwd=SCRIPT_DIR,
    )
    elapsed = time.time() - t0
    status = "✓" if result.returncode == 0 else "✗"
    print(f"  {status} Completed in {elapsed:.0f}s (exit code {result.returncode})")
    return result.returncode


def git_push():
    """Commit and push generated viewers to the git repository.

    Only commits files tracked by git (respects .gitignore).
    Uses the repo root (parent of pipeline/) as the working directory.
    """
    repo_root = os.path.dirname(SCRIPT_DIR)  # birds_eye_view/

    def _git(*cmd):
        result = subprocess.run(
            ["git"] + list(cmd),
            cwd=repo_root,
            capture_output=True,
            text=True,
        )
        return result

    # Check if git repo exists and has a remote
    r = _git("remote", "-v")
    if r.returncode != 0:
        print("  ✗ Not a git repository. Run: git init && git remote add origin <url>")
        return False
    if not r.stdout.strip():
        print("  ✗ No git remote configured. Run: git remote add origin <url>")
        return False

    remote_line = r.stdout.strip().split("\n")[0]
    print(f"  Remote: {remote_line}")

    # Get current branch
    r = _git("branch", "--show-current")
    branch = r.stdout.strip() or "main"
    print(f"  Branch: {branch}")

    # Stage all changes (respects .gitignore)
    _git("add", "-A")

    # Check if there's anything to commit
    r = _git("status", "--porcelain")
    if not r.stdout.strip():
        print("  ✓ Nothing to commit — viewers are up to date")
        return True

    n_changes = len([l for l in r.stdout.strip().split("\n") if l.strip()])
    print(f"  Staging {n_changes} change(s)")

    # Commit with timestamp
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    commit_msg = f"pipeline: update viewers ({ts})"
    r = _git("commit", "-m", commit_msg)
    if r.returncode != 0:
        print(f"  ✗ Commit failed: {r.stderr.strip()}")
        return False
    print(f"  ✓ Committed: {commit_msg}")

    # Push
    r = _git("push", "origin", branch)
    if r.returncode != 0:
        # Try with --set-upstream for first push
        r = _git("push", "--set-upstream", "origin", branch)
        if r.returncode != 0:
            print(f"  ✗ Push failed: {r.stderr.strip()}")
            return False

    print(f"  ✓ Pushed to origin/{branch}")
    return True


# ── Main ──────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="MCP Advanced Viewer Pipeline",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--sheet-id", default=DEFAULT_SHEET_ID,
                        help="Google Sheet ID (default: %(default)s)")
    parser.add_argument("--gid", type=int, default=DEFAULT_GID,
                        help="Worksheet gid (default: %(default)s)")
    parser.add_argument("--skip-download", action="store_true",
                        help="Skip trajectory download (reuse cached files)")
    parser.add_argument("--skip-generate", action="store_true",
                        help="Skip viewer generation")
    parser.add_argument("--skip-push", action="store_true",
                        help="Skip git commit/push")
    parser.add_argument("--fresh", action="store_true",
                        help="Clear all cached data before running")
    args = parser.parse_args()

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    os.makedirs(CREDS_DIR, exist_ok=True)

    print("=" * 60)
    print("MCP Advanced Viewer Pipeline")
    print("=" * 60)
    print(f"  Sheet:  {args.sheet_id}")
    print(f"  Output: {OUTPUT_DIR}")

    # Fresh start?
    if args.fresh and os.path.isdir(OUTPUT_DIR):
        shutil.rmtree(OUTPUT_DIR)
        os.makedirs(OUTPUT_DIR, exist_ok=True)
        print("  Cleared output directory")

    # ── Step 1: Pull from Google Sheets ──
    print(f"\n{'='*60}")
    print("STEP 1: Pull data from Google Sheets")
    print("=" * 60)

    rows = pull_sheet(args.sheet_id, args.gid)
    if not rows:
        print("  ✗ No data in sheet")
        sys.exit(1)

    cols = set(rows[0].keys())
    required = {"taskid", "response"}
    missing = required - cols
    if missing:
        print(f"  ✗ Missing columns: {missing}. Found: {sorted(cols)}")
        sys.exit(1)

    has_traj = "trajectory_urls" in cols
    print(f"  Columns: {sorted(cols)}")
    print(f"  trajectory_urls: {'yes' if has_traj else 'no'}")

    if has_traj:
        check_url_freshness(rows)

    csv_path = os.path.join(OUTPUT_DIR, "sheet_data.csv")
    write_csv(rows, csv_path)

    # ── Step 2: Download trajectories ──
    if not args.skip_download and has_traj:
        traj_dir = os.path.join(OUTPUT_DIR, "trajectories")
        if os.path.isdir(traj_dir):
            shutil.rmtree(traj_dir)
            print(f"\n  Cleared {traj_dir}")
        run_step(
            "Download trajectories",
            os.path.join(SCRIPT_DIR, "download_trajectories.py"),
            csv_path,
        )
    elif args.skip_download:
        print(f"\n⏭ Skipping trajectory download (--skip-download)")
    else:
        print(f"\n⏭ No trajectory_urls column — skipping download")

    # ── Step 3: Generate viewers ──
    if not args.skip_generate:
        run_step(
            "Generate HTML viewers",
            os.path.join(SCRIPT_DIR, "generate_viewers.py"),
            csv_path,
        )
    else:
        print(f"\n⏭ Skipping viewer generation (--skip-generate)")

    # ── Step 4: Push to GitHub ──
    if not args.skip_push:
        print(f"\n{'='*60}")
        print("STEP 4: Push to GitHub")
        print("=" * 60)
        git_push()
    else:
        print(f"\n⏭ Skipping git push (--skip-push)")

    # ── Done ──
    print(f"\n{'='*60}")
    print("✓ PIPELINE COMPLETE")
    print(f"{'='*60}")
    print(f"  Viewers: {OUTPUT_DIR}/<taskid>_viewer.html")
    print(f"  Homepage: {os.path.join(OUTPUT_DIR, 'index.html')}")


if __name__ == "__main__":
    main()
