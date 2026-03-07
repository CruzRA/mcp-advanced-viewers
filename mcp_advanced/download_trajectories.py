#!/usr/bin/env python3
"""Download all trajectory JSONs from signed S3 URLs found in a CSV.

The CSV should have at least a ``taskid`` column and one of:
    • trajectory_urls – JSON blob with pre-signed HTTPS download URLs
    • response        – task response JSON (agentRuns[].trajectoryS3Uri)

When ``trajectory_urls`` is present the script prefers it because it
normally contains pre-signed HTTPS download links.  If only ``response``
is available the script extracts s3:// URIs and resolves them via the
Scale upload-asset endpoint to get signed URLs.

Usage:
    python download_trajectories.py [path/to/input.csv]
"""

import csv, sys, json, os, time, urllib.request, urllib.error

csv.field_size_limit(sys.maxsize)

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(SCRIPT_DIR, "trajectories")
os.makedirs(OUT_DIR, exist_ok=True)


def _extract_urls_from_blob(blob: dict) -> list[tuple[str, str, int, str]]:
    """Return [(step_key, url, run_index, filename), ...] from a turns-style JSON.

    Checks three locations per run (first hit wins):
      1. agentRuns[].trajectoryS3Uri
      2. agentRuns[].taskStepContext.prompt_responses[].agent_trajectory_s3_uri
    """
    results = []
    turn = blob.get("turns", [{}])[0]
    for sk, sv in turn.items():
        if not isinstance(sv, dict) or sv.get("type") != "ExternalApp":
            continue
        items = sv.get("output", {}).get("items", [])
        if not items or not isinstance(items[0], dict):
            continue
        meta = items[0].get("metadata", {})
        for j, ar in enumerate(meta.get("agentRuns", [])):
            # Source 1: trajectoryS3Uri
            url = ar.get("trajectoryS3Uri", "")
            if url:
                fname = url.split("?")[0].split("/")[-1]
                results.append((sk, url, j, fname))
                continue
            # Source 2: taskStepContext.prompt_responses
            prs = ar.get("taskStepContext", {}).get("prompt_responses", [])
            for pr in prs:
                url2 = pr.get("agent_trajectory_s3_uri", "")
                if url2:
                    fname2 = url2.split("?")[0].split("/")[-1]
                    results.append((sk, url2, j, fname2))
                    break
    return results


def _resolve_s3_uris(s3_uris: list[str]) -> dict[str, str]:
    """Resolve s3:// URIs to pre-signed HTTPS URLs via the Scale upload-asset endpoint.

    Returns {s3_uri: https_url}.  Requires SCALE_JWT and SCALE_CSRF env vars.
    """
    jwt = os.environ.get("SCALE_JWT", "")
    csrf = os.environ.get("SCALE_CSRF", "")
    if not jwt or not csrf:
        env_path = os.path.join(os.path.dirname(SCRIPT_DIR), ".env")
        if os.path.exists(env_path):
            with open(env_path) as f:
                for line in f:
                    line = line.strip()
                    if line.startswith("SCALE_JWT="):
                        jwt = line.split("=", 1)[1].strip().strip('"').strip("'")
                    elif line.startswith("SCALE_CSRF="):
                        csrf = line.split("=", 1)[1].strip().strip('"').strip("'")

    if not jwt or not csrf:
        print("⚠  Cannot resolve s3:// URIs: SCALE_JWT / SCALE_CSRF not found.")
        return {}

    url = "https://dashboard.scale.com/corp-api/upload_static_asset_to_s3?pageLoadId=auto"
    payload = json.dumps({"inputObject": s3_uris, "expireMs": 36000000}).encode()
    req = urllib.request.Request(url, data=payload, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "*/*")
    req.add_header("Origin", "https://dashboard.scale.com")
    req.add_header("Referer", "https://dashboard.scale.com/")
    req.add_header("User-Agent", "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36")
    req.add_header("x-csrf-token", csrf)
    req.add_header("Cookie", f"_jwt={jwt}; _csrf={csrf}")

    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            body = json.loads(resp.read())
        # body is a list of signed URLs in the same order as input
        return dict(zip(s3_uris, body))
    except Exception as e:
        print(f"⚠  Failed to resolve s3:// URIs: {e}")
        return {}


def download(url: str, dest: str) -> bool:
    """Download url to dest. Returns True on success."""
    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = resp.read()
        with open(dest, "wb") as f:
            f.write(data)
        return True
    except Exception as e:
        print(f"    ✗ Failed: {e}")
        return False


def main():
    csv_path = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
        SCRIPT_DIR, "50ed9d8c-2cdb-4f81-9a99-31d0c9876829.csv"
    )
    print(f"CSV: {csv_path}")

    with open(csv_path) as f:
        reader = csv.DictReader(f)
        rows = list(reader)

    has_traj_col = "trajectory_urls" in (rows[0] if rows else {})
    print(f"trajectory_urls column: {'yes' if has_traj_col else 'no'}")

    # Collect all URLs per task
    tasks = []  # list of (task_id, {fname: (step, url, run_idx)})
    all_s3_uris = set()

    for row in rows:
        task_id = row.get("taskid", row.get("task", ""))
        if not task_id:
            continue

        seen = {}  # fname -> (step_key, url, run_idx)

        # 1. Try compact trajectory_urls format: {"filename": "url", ...}
        if has_traj_col and row.get("trajectory_urls"):
            raw = row["trajectory_urls"].strip()
            try:
                parsed = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                parsed = None

            if isinstance(parsed, dict) and parsed:
                # Check if it's the compact {filename: url} format
                first_key = next(iter(parsed))
                if first_key.endswith(".json") and not first_key.startswith("turns"):
                    # Compact format
                    for idx, (fname, url) in enumerate(parsed.items()):
                        if fname not in seen:
                            seen[fname] = ("compact", url, idx)
                            if url.startswith("s3://"):
                                all_s3_uris.add(url)
                else:
                    # Legacy full-blob format
                    urls = _extract_urls_from_blob(parsed)
                    for sk, url, run_idx, fname in urls:
                        if fname not in seen:
                            seen[fname] = (sk, url, run_idx)
                            if url.startswith("s3://"):
                                all_s3_uris.add(url)

        # 2. Fallback to response column (full blob)
        if not seen and row.get("response"):
            try:
                blob = json.loads(row["response"])
                urls = _extract_urls_from_blob(blob)
            except (json.JSONDecodeError, TypeError):
                urls = []
            for sk, url, run_idx, fname in urls:
                if fname not in seen:
                    seen[fname] = (sk, url, run_idx)
                    if url.startswith("s3://"):
                        all_s3_uris.add(url)

        tasks.append((task_id, seen))

    # Resolve any s3:// URIs in bulk
    s3_map = {}
    if all_s3_uris:
        print(f"\nResolving {len(all_s3_uris)} s3:// URIs to signed URLs...")
        s3_map = _resolve_s3_uris(sorted(all_s3_uris))
        print(f"  Resolved {len(s3_map)} URLs")

    total = 0
    ok = 0
    skipped = 0

    for task_id, seen in tasks:
        task_dir = os.path.join(OUT_DIR, task_id)
        os.makedirs(task_dir, exist_ok=True)

        print(f"\n{'='*60}")
        print(f"Task: {task_id}  —  {len(seen)} unique trajectories")
        print(f"{'='*60}")

        for fname, (sk, raw_url, run_idx) in sorted(seen.items()):
            total += 1
            dest = os.path.join(task_dir, fname)
            if os.path.exists(dest) and os.path.getsize(dest) > 100:
                print(f"  ✓ {fname} (already downloaded)")
                skipped += 1
                ok += 1
                continue

            # Resolve URL if it's s3://
            dl_url = raw_url
            if dl_url.startswith("s3://"):
                dl_url = s3_map.get(dl_url, "")
            if not dl_url or not dl_url.startswith("https://"):
                print(f"  ✗ {fname} (no downloadable URL)")
                continue

            print(f"  ↓ {fname} (run {run_idx} in {sk})...")
            if download(dl_url, dest):
                size = os.path.getsize(dest)
                print(f"    ✓ {size:,} bytes")
                ok += 1
            time.sleep(0.2)

    print(f"\n{'='*60}")
    print(f"Done: {ok}/{total} downloaded ({skipped} already existed)")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
