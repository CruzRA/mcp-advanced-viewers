"""
Modal deployment for MCP Advanced Viewer Pipeline.

Runs every 10 minutes on Modal's infrastructure:
  1. Pull task data from Google Sheets
  2. Download trajectory JSONs
  3. Generate HTML viewers + homepage
  4. Commit & push to GitHub

Setup (one-time):
  1. pip install modal
  2. modal setup                          # authenticate (opens browser)
  3. modal secret create gsheet-sa-key \\
       SA_KEY_JSON="$(cat credentials/your-key.json)"
  4. modal secret create github-creds \\
       GIT_USER="CruzRA" \\
       GIT_EMAIL="cruzra914@gmail.com" \\
       GIT_TOKEN="ghp_xxxxx" \\
       GIT_REPO="https://github.com/CruzRA/mcp-advanced-viewers.git"

Deploy:
  modal deploy pipeline/modal_app.py

Test single run:
  modal run pipeline/modal_app.py
"""

from __future__ import annotations

import os
from pathlib import Path

import modal

# ── Modal App ─────────────────────────────────────────────────
app = modal.App("mcp-advanced-viewers")

# Container image with deps + git
image = (
    modal.Image.debian_slim(python_version="3.12")
    .pip_install("gspread", "google-auth", "pandas")
    .apt_install("git")
    # Copy pipeline scripts into image (writable /app/pipeline)
    .add_local_dir(
        str(Path(__file__).parent),
        remote_path="/app/pipeline",
        copy=True,
        ignore=[
            "modal_app.py",
            "output/",
            "credentials/",
            "__pycache__/",
            "*.pyc",
            ".env",
        ],
    )
)

SHEET_ID = "1PT1eA-YhTLs-DSV4BtNyFs5MkpqOhM9zxJwzyMJGTYk"
GID = 0


@app.function(
    image=image,
    secrets=[
        modal.Secret.from_name("gsheet-sa-key"),
        modal.Secret.from_name("github-creds"),
    ],
    schedule=modal.Period(minutes=50),
    timeout=600,
)
def run_pipeline():
    """Full pipeline cycle — scheduled every 60 minutes."""
    import csv
    import json
    import shutil
    import subprocess
    import sys
    from datetime import datetime

    csv.field_size_limit(sys.maxsize)

    SCRIPTS = "/app/pipeline"
    OUTPUT  = "/app/pipeline/output"
    CREDS   = "/app/pipeline/credentials"

    os.makedirs(OUTPUT, exist_ok=True)
    os.makedirs(CREDS, exist_ok=True)

    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"\n{'='*60}")
    print(f"MCP Advanced Viewer Pipeline — {ts}")
    print(f"{'='*60}")

    # ── Write service account key from Modal secret ──
    sa_json = os.environ.get("SA_KEY_JSON", "")
    if not sa_json:
        raise RuntimeError("SA_KEY_JSON secret is empty")
    sa_path = os.path.join(CREDS, "sa_key.json")
    with open(sa_path, "w") as f:
        f.write(sa_json)
    print("  ✓ SA key written")

    # ── Step 1: Pull Google Sheet ──
    _banner("STEP 1: Pull data from Google Sheets")

    import gspread
    from google.oauth2.service_account import Credentials

    creds = Credentials.from_service_account_file(sa_path, scopes=[
        "https://www.googleapis.com/auth/spreadsheets.readonly",
        "https://www.googleapis.com/auth/drive.readonly",
    ])
    gc = gspread.authorize(creds)
    sh = gc.open_by_key(SHEET_ID)

    ws = next((w for w in sh.worksheets() if w.id == GID), sh.sheet1)
    print(f"  Worksheet: {ws.title}")

    rows = ws.get_all_records()
    print(f"  Rows: {len(rows)}")
    if not rows:
        print("  ✗ Empty sheet — nothing to do")
        return

    rows = [{k.lower().strip(): v for k, v in r.items()} for r in rows]
    cols = set(rows[0].keys())
    if not {"taskid", "response"}.issubset(cols):
        raise RuntimeError(f"Missing required columns. Found: {sorted(cols)}")

    csv_path = os.path.join(OUTPUT, "sheet_data.csv")
    with open(csv_path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    print(f"  → {csv_path} ({len(rows)} rows)")

    # ── Step 2: Download trajectories ──
    has_traj = "trajectory_urls" in cols
    if has_traj:
        traj_dir = os.path.join(OUTPUT, "trajectories")
        if os.path.isdir(traj_dir):
            shutil.rmtree(traj_dir)
        _run_script(SCRIPTS, "download_trajectories.py", csv_path)
    else:
        print("\n⏭ No trajectory_urls column — skipping download")

    # ── Step 3: Generate viewers ──
    _run_script(SCRIPTS, "generate_viewers.py", csv_path)

    # ── Step 4: Push to GitHub ──
    _banner("STEP 4: Push to GitHub")
    _git_push(OUTPUT, ts)

    print(f"\n{'='*60}")
    print("✓ PIPELINE COMPLETE")
    print(f"{'='*60}")


# ── Helpers ───────────────────────────────────────────────────

def _banner(title: str):
    print(f"\n{'='*60}")
    print(title)
    print(f"{'='*60}")


def _run_script(scripts_dir: str, script: str, *args: str):
    import subprocess, sys
    _banner(f"Running {script}")
    r = subprocess.run(
        [sys.executable, os.path.join(scripts_dir, script), *args],
        cwd=scripts_dir,
        env={**os.environ, "PYTHONUNBUFFERED": "1"},
    )
    if r.returncode != 0:
        print(f"  ⚠ {script} exited with code {r.returncode}")
    else:
        print(f"  ✓ {script} completed")


def _git_push(output_dir: str, ts: str):
    import shutil, subprocess

    git_user  = os.environ.get("GIT_USER", "")
    git_email = os.environ.get("GIT_EMAIL", "")
    git_token = os.environ.get("GIT_TOKEN", "")
    git_repo  = os.environ.get("GIT_REPO", "")

    if not all([git_user, git_token, git_repo]):
        print("  ⚠ GitHub credentials not configured — skipping push")
        print("    Create secret: modal secret create github-creds ...")
        return

    repo_dir = "/tmp/repo"
    if os.path.isdir(repo_dir):
        shutil.rmtree(repo_dir)

    auth_url = git_repo.replace("https://", f"https://{git_user}:{git_token}@")
    subprocess.run(["git", "clone", "--depth=1", auth_url, repo_dir],
                   check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", git_user], cwd=repo_dir)
    subprocess.run(["git", "config", "user.email", git_email or f"{git_user}@users.noreply.github.com"],
                   cwd=repo_dir)

    # Copy generated HTML viewers into repo
    repo_output = os.path.join(repo_dir, "pipeline", "output")
    os.makedirs(repo_output, exist_ok=True)
    copied = 0
    for fname in os.listdir(output_dir):
        if fname.endswith(".html"):
            shutil.copy2(os.path.join(output_dir, fname),
                         os.path.join(repo_output, fname))
            copied += 1
    print(f"  Copied {copied} HTML file(s)")

    r = subprocess.run(["git", "status", "--porcelain"],
                       cwd=repo_dir, capture_output=True, text=True)
    if not r.stdout.strip():
        print("  ✓ Nothing to commit — viewers are up to date")
        return

    subprocess.run(["git", "add", "-A"], cwd=repo_dir)
    msg = f"pipeline: update viewers ({ts})"
    subprocess.run(["git", "commit", "-m", msg], cwd=repo_dir, check=True,
                   capture_output=True)
    subprocess.run(["git", "push", "origin", "main"], cwd=repo_dir, check=True,
                   capture_output=True)
    print(f"  ✓ Pushed: {msg}")


# ── Entrypoints ──────────────────────────────────────────────

@app.local_entrypoint()
def main():
    """Test: `modal run pipeline/modal_app.py`"""
    run_pipeline.remote()
