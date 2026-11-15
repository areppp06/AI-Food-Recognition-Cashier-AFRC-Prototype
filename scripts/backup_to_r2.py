#!/usr/bin/env python3
"""
AFRC Automated SQLite Backup to Cloudflare R2
- Uses existing env/cloudflare.env credentials (CLOUDFLARE_API_TOKEN & CLOUDFLARE_ACCOUNT_ID)
- Uses SQLite Online Backup API for 100% consistent, zero-lock point-in-time snapshots
- Compresses with gzip (level 9)
- Uploads directly to Cloudflare R2 under DB_backups/
- Automatically prunes backups older than RETENTION_DAYS (default: 90 days)
"""

import os
import sys
import gzip
import shutil
import sqlite3
import tempfile
import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path
import requests

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("afrc_backup")

def load_env_file(filepath: Path) -> dict:
    env_vars = {}
    if not filepath.is_file():
        return env_vars
    with open(filepath, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k = k.strip()
            v = v.strip().strip("'\"")
            if k and v:
                env_vars[k] = v
                if not os.environ.get(k):
                    os.environ[k] = v
    return env_vars

def main():
    script_dir = Path(__file__).resolve().parent
    repo_dir = script_dir.parent

    # 1. Load cloudflare.env and fallback configs
    cf_env_path = repo_dir / "env" / "cloudflare.env"
    load_env_file(cf_env_path)
    load_env_file(Path("/root/.r2_backup.env"))
    load_env_file(repo_dir / ".r2_backup.env")
    load_env_file(repo_dir / ".env")

    account_id = (os.getenv("CLOUDFLARE_ACCOUNT_ID") or os.getenv("R2_ACCOUNT_ID") or "").strip()
    api_token = (os.getenv("CLOUDFLARE_API_TOKEN") or "").strip()
    bucket_name = (os.getenv("R2_BUCKET_NAME") or "afrc-plates").strip()
    folder_prefix = (os.getenv("R2_FOLDER_PREFIX") or "DB_backups").strip().strip("/")
    db_path = Path(os.getenv("AFRC_DB_PATH", str(repo_dir / "afrc.db")))
    retention_days = int(os.getenv("R2_RETENTION_DAYS") or "90")

    if not account_id:
        logger.error("CLOUDFLARE_ACCOUNT_ID is missing from env/cloudflare.env")
        sys.exit(1)
    if not api_token:
        logger.error("CLOUDFLARE_API_TOKEN is missing from env/cloudflare.env")
        sys.exit(1)
    if not db_path.exists():
        logger.error(f"Database file not found at: {db_path}")
        sys.exit(1)

    logger.info(f"Target Bucket: {bucket_name} | Folder: {folder_prefix}/ | Retention: {retention_days} days")

    # 2. Online atomic snapshot
    temp_dir = tempfile.mkdtemp(prefix="afrc_backup_")
    now_utc = datetime.now(timezone.utc)
    now_sg = now_utc + timedelta(hours=8)
    ts_str = now_sg.strftime("%Y%m%d_%H%M%S")
    backup_filename = f"afrc_backup_{ts_str}.db"
    backup_gz_filename = f"{backup_filename}.gz"

    temp_raw = Path(temp_dir) / backup_filename
    temp_gz = Path(temp_dir) / backup_gz_filename

    try:
        logger.info(f"Taking live online snapshot of {db_path}...")
        src_conn = sqlite3.connect(str(db_path))
        dst_conn = sqlite3.connect(str(temp_raw))
        with dst_conn:
            src_conn.backup(dst_conn)
        dst_conn.close()
        src_conn.close()

        # Integrity check
        verify_conn = sqlite3.connect(str(temp_raw))
        check_val = verify_conn.execute("PRAGMA integrity_check;").fetchone()[0]
        verify_conn.close()
        if check_val != "ok":
            raise RuntimeError(f"Backup snapshot integrity check failed: {check_val}")

        raw_size = temp_raw.stat().st_size
        logger.info(f"Snapshot verified clean (size: {raw_size:,} bytes).")

        # 3. Compress snapshot
        logger.info("Compressing snapshot with gzip...")
        with open(temp_raw, "rb") as f_in, gzip.open(temp_gz, "wb", compresslevel=9) as f_out:
            shutil.copyfileobj(f_in, f_out)
        gz_size = temp_gz.stat().st_size
        logger.info(f"Compressed to {gz_size:,} bytes ({round((1 - gz_size / raw_size) * 100, 1)}% reduction).")

        # 4. Upload to Cloudflare R2 via REST API
        r2_key = f"{folder_prefix}/{backup_gz_filename}"
        upload_url = f"https://api.cloudflare.com/client/v4/accounts/{account_id}/r2/buckets/{bucket_name}/objects/{r2_key}"
        headers = {
            "Authorization": f"Bearer {api_token}",
            "Content-Type": "application/gzip",
        }

        logger.info(f"Uploading to Cloudflare R2: {bucket_name}/{r2_key}...")
        with open(temp_gz, "rb") as gz_file:
            resp = requests.put(upload_url, headers=headers, data=gz_file, timeout=60)

        if resp.status_code not in (200, 201):
            raise RuntimeError(f"R2 upload failed ({resp.status_code}): {resp.text}")

        logger.info(f"Successfully uploaded backup to R2: {r2_key}")

        # 5. Prune backups older than retention_days
        logger.info(f"Checking for backups in '{folder_prefix}/' older than {retention_days} days...")
        cutoff_date = now_utc - timedelta(days=retention_days)
        list_url = f"https://api.cloudflare.com/client/v4/accounts/{account_id}/r2/buckets/{bucket_name}/objects"
        list_resp = requests.get(
            list_url,
            headers={"Authorization": f"Bearer {api_token}"},
            params={"prefix": f"{folder_prefix}/"},
            timeout=30,
        )

        deleted_count = 0
        if list_resp.status_code == 200:
            objects = list_resp.json().get("result", [])
            for obj in objects:
                key = obj.get("key", "")
                if key == f"{folder_prefix}/" or not key:
                    continue
                
                # Check upload date
                uploaded_str = obj.get("uploaded")
                if uploaded_str:
                    try:
                        # Parse ISO 8601 string
                        uploaded_dt = datetime.fromisoformat(uploaded_str.replace("Z", "+00:00"))
                        if uploaded_dt < cutoff_date:
                            logger.info(f"Deleting expired backup (> {retention_days} days old): {key} (Uploaded: {uploaded_str})")
                            del_url = f"https://api.cloudflare.com/client/v4/accounts/{account_id}/r2/buckets/{bucket_name}/objects/{key}"
                            del_resp = requests.delete(del_url, headers={"Authorization": f"Bearer {api_token}"}, timeout=30)
                            if del_resp.status_code in (200, 204):
                                deleted_count += 1
                            else:
                                logger.warning(f"Failed to delete {key}: {del_resp.status_code} {del_resp.text}")
                    except Exception as pe:
                        logger.warning(f"Could not parse object date for {key}: {pe}")
            
            if deleted_count > 0:
                logger.info(f"Retention prune complete: removed {deleted_count} expired backup(s).")
            else:
                logger.info(f"Retention prune complete: no backups exceeded {retention_days} days.")
        else:
            logger.warning(f"Could not list objects for pruning ({list_resp.status_code}): {list_resp.text}")

        logger.info("AFRC DB backup completed successfully!")

    except Exception as e:
        logger.exception(f"Backup failed with error: {e}")
        sys.exit(1)
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)

if __name__ == "__main__":
    main()
