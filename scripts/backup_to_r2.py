#!/usr/bin/env python3
"""
AFRC Automated SQLite Backup to Cloudflare R2
- Uses SQLite Online Backup API for 100% consistent point-in-time snapshots
- Compresses with gzip (level 9)
- Uploads to Cloudflare R2 under folder DB_backups/
- Automatically prunes backups older than RETENTION_DAYS (default: 90)
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

try:
    import boto3
    from botocore.config import Config
    from botocore.exceptions import ClientError
except ImportError:
    print("Error: boto3 is required. Run 'pip install boto3'")
    sys.exit(1)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("afrc_backup")

def load_env_file(filepath: Path):
    if not filepath.is_file():
        return
    with open(filepath, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k = k.strip()
            v = v.strip().strip("'\"")
            if k and k not in os.environ:
                os.environ[k] = v

def main():
    script_dir = Path(__file__).resolve().parent
    repo_dir = script_dir.parent

    # Search for backup env config
    possible_envs = [
        Path("/root/.r2_backup.env"),
        repo_dir / ".r2_backup.env",
        repo_dir / ".env",
    ]
    for p in possible_envs:
        if p.exists():
            load_env_file(p)

    account_id = os.getenv("R2_ACCOUNT_ID", "").strip()
    bucket_name = os.getenv("R2_BUCKET_NAME", "").strip()
    access_key_id = os.getenv("R2_ACCESS_KEY_ID", "").strip()
    secret_access_key = os.getenv("R2_SECRET_ACCESS_KEY", "").strip()
    folder_prefix = os.getenv("R2_FOLDER_PREFIX", "DB_backups").strip().strip("/")
    db_path = Path(os.getenv("AFRC_DB_PATH", str(repo_dir / "afrc.db")))
    retention_days = int(os.getenv("R2_RETENTION_DAYS", "90"))

    missing = []
    if not account_id: missing.append("R2_ACCOUNT_ID")
    if not bucket_name: missing.append("R2_BUCKET_NAME")
    if not access_key_id: missing.append("R2_ACCESS_KEY_ID")
    if not secret_access_key: missing.append("R2_SECRET_ACCESS_KEY")

    if missing:
        logger.error(f"Missing required Cloudflare R2 credentials: {', '.join(missing)}")
        logger.info("Please set them in /root/.r2_backup.env")
        sys.exit(1)

    if not db_path.exists():
        logger.error(f"Database file not found at: {db_path}")
        sys.exit(1)

    endpoint_url = f"https://{account_id}.r2.cloudflarestorage.com"
    s3_client = boto3.client(
        "s3",
        endpoint_url=endpoint_url,
        aws_access_key_id=access_key_id,
        aws_secret_access_key=secret_access_key,
        region_name="auto",
        config=Config(retries={"max_attempts": 3, "mode": "standard"}),
    )

    # 1. Create temporary directory
    temp_dir = tempfile.mkdtemp(prefix="afrc_backup_")
    now_utc = datetime.now(timezone.utc)
    # Format filename with Singapore date/time for easy readability (UTC+8)
    now_sg = now_utc + timedelta(hours=8)
    ts_str = now_sg.strftime("%Y%m%d_%H%M%S")
    backup_filename = f"afrc_backup_{ts_str}.db"
    backup_gz_filename = f"{backup_filename}.gz"

    temp_raw = Path(temp_dir) / backup_filename
    temp_gz = Path(temp_dir) / backup_gz_filename

    try:
        # 2. Perform SQLite online backup
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

        # 4. Upload to Cloudflare R2
        r2_key = f"{folder_prefix}/{backup_gz_filename}"
        logger.info(f"Uploading to Cloudflare R2: s3://{bucket_name}/{r2_key}...")
        s3_client.upload_file(
            Filename=str(temp_gz),
            Bucket=bucket_name,
            Key=r2_key,
            ExtraArgs={"ContentType": "application/gzip"},
        )
        logger.info(f"Successfully uploaded backup to R2: {r2_key}")

        # 5. Prune backups older than retention_days
        logger.info(f"Checking for backups in '{folder_prefix}/' older than {retention_days} days...")
        cutoff_date = now_utc - timedelta(days=retention_days)
        paginator = s3_client.get_paginator("list_objects_v2")
        deleted_count = 0

        for page in paginator.paginate(Bucket=bucket_name, Prefix=f"{folder_prefix}/"):
            for obj in page.get("Contents", []):
                key = obj["Key"]
                if key == f"{folder_prefix}/":
                    continue
                last_modified = obj["LastModified"]
                if last_modified < cutoff_date:
                    logger.info(f"Deleting expired backup (> {retention_days} days old): {key} (Modified: {last_modified})")
                    s3_client.delete_object(Bucket=bucket_name, Key=key)
                    deleted_count += 1

        if deleted_count > 0:
            logger.info(f"Retention prune complete: removed {deleted_count} expired backup(s).")
        else:
            logger.info("Retention prune complete: no backups exceeded 90 days.")

        logger.info("AFRC DB backup completed successfully!")

    except Exception as e:
        logger.exception(f"Backup failed with error: {e}")
        sys.exit(1)
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)

if __name__ == "__main__":
    main()
