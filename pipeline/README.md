# MCP Advanced Viewer Pipeline

Automated pipeline that pulls task data from Google Sheets, downloads agent
trajectory files, and generates interactive HTML trace viewers.

## Directory Structure

```
pipeline/
├── run.py                     # Main entry point — runs full pipeline
├── download_trajectories.py   # Step 2: download trajectory JSONs from S3
├── generate_viewers.py        # Step 3: generate HTML viewers + homepage
├── requirements.txt
├── credentials/               # ← put your service account key here
│   └── your-key.json
└── output/                    # ← generated at runtime
    ├── sheet_data.csv
    ├── trajectories/
    │   └── <taskid>/
    │       └── prompt-trajectory-XXXX.json
    ├── <taskid>_viewer.html
    └── index.html             # homepage with all tasks
```

## Quick Start

### 1. Install Dependencies

```bash
pip install -r requirements.txt
```

### 2. Set Up Google Service Account

1. Go to [Google Cloud Console](https://console.cloud.google.com/)
2. Create a project (or use existing)
3. Enable **Google Sheets API** and **Google Drive API**
4. Create a **Service Account** → download the JSON key
5. Place the key in `credentials/`:
   ```bash
   cp ~/Downloads/your-project-XXXXXX.json credentials/
   ```

### 3. Share the Google Sheet

Open the Google Sheet and share it with your service account email as **Viewer**:

```
scripts@your-project.iam.gserviceaccount.com
```

The email is in the `client_email` field of your JSON key.

### 4. Configure Sheet ID

Edit the defaults in `run.py` (near the top):

```python
DEFAULT_SHEET_ID = "1PT1eA-YhTLs-DSV4BtNyFs5MkpqOhM9zxJwzyMJGTYk"
DEFAULT_GID = 0
```

Or pass them at runtime:
```bash
python run.py --sheet-id YOUR_SHEET_ID --gid 0
```

### 5. Run the Pipeline

```bash
python run.py
```

This will:
1. Pull all rows from the Google Sheet
2. Download trajectory JSONs from pre-signed S3 URLs
3. Generate HTML viewers for each task + an `index.html` homepage
4. Commit & push changes to the git repository

Output goes to `output/`.

## CLI Options

```
python run.py [OPTIONS]

Options:
  --sheet-id ID      Google Sheet ID (default: configured in script)
  --gid N            Worksheet gid (default: 0)
  --skip-download    Reuse cached trajectories (faster re-runs)
  --skip-generate    Only pull sheet + download trajectories
  --skip-push        Skip git commit/push
  --fresh            Clear all output before running
```

## Setting Up a Cron Job

### Option A: Simple crontab

```bash
crontab -e
```

Add a line to run every hour (adjust as needed):

```cron
0 * * * * cd /path/to/pipeline && /usr/bin/python3 run.py >> output/cron.log 2>&1
```

### Option B: With virtual environment

```bash
# Create venv once
python3 -m venv /path/to/pipeline/.venv
source /path/to/pipeline/.venv/bin/activate
pip install -r /path/to/pipeline/requirements.txt
```

Crontab:
```cron
0 * * * * cd /path/to/pipeline && .venv/bin/python run.py >> output/cron.log 2>&1
```

### Option C: launchd (macOS native scheduler)

Create `~/Library/LaunchAgents/com.mcp.viewer-pipeline.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.mcp.viewer-pipeline</string>
    <key>ProgramArguments</key>
    <array>
        <string>/usr/bin/python3</string>
        <string>/path/to/pipeline/run.py</string>
    </array>
    <key>WorkingDirectory</key>
    <string>/path/to/pipeline</string>
    <key>StartInterval</key>
    <integer>3600</integer>
    <key>StandardOutPath</key>
    <string>/path/to/pipeline/output/cron.log</string>
    <key>StandardErrorPath</key>
    <string>/path/to/pipeline/output/cron.log</string>
</dict>
</plist>
```

Load it:
```bash
launchctl load ~/Library/LaunchAgents/com.mcp.viewer-pipeline.plist
```

## Important Notes

### Pre-signed URL Expiration

The `trajectory_urls` column in the Google Sheet contains pre-signed S3 URLs
with embedded AWS Cognito tokens. These tokens expire in **~1 hour**. The
pipeline checks URL freshness and warns you if they're stale.

**For cron jobs:** Make sure the upstream process that populates the Google
Sheet refreshes the URLs frequently. If downloads fail with `400 Bad Request`,
the URLs have expired.

### Serving the Viewers

The generated HTML files are fully self-contained (all CSS/JS inline). You can:

- Open them directly in a browser (`file://`)
- Serve them with any static file server:
  ```bash
  cd output && python3 -m http.server 8080
  ```
- Upload to S3, GitHub Pages, or any CDN

### Standalone Scripts

Each script can be run independently:

```bash
# Download trajectories from a CSV
python download_trajectories.py path/to/tasks.csv

# Generate viewers from a CSV (trajectories must already be downloaded)
python generate_viewers.py path/to/tasks.csv
```

## Google Sheet Format

| Column | Required | Description |
|--------|----------|-------------|
| `taskid` | ✓ | Unique task identifier |
| `response` | ✓ | JSON string with task response (turns, steps) |
| `trajectory_urls` | | JSON `{filename: signed_url}` for trajectory downloads |
| `email` | | Annotator email (shown in viewer) |
| `annotator` | | Annotator name (shown in viewer) |
