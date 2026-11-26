"""Cloudflare R2 plate image storage, failed scan archiving, and background sync."""
from __future__ import annotations

import asyncio
import logging
import os
import uuid
from datetime import datetime
from urllib.parse import quote

import httpx
from sqlalchemy.orm import Session

from ..db import FailedScan, SessionBill, SessionLocal
from .config_service import UPLOAD_DIR

logger = logging.getLogger("afrc")

_R2_SESSION_KEYS: dict[str, str] = {}
_R2_BILL_KEYS: dict[str, str] = {}
_R2_MISSING_UNTIL: dict[str, float] = {}
_R2_MISSING_TTL_SEC: float = 120.0

_HTTP_CLIENT: httpx.AsyncClient | None = None
_R2_FETCH_SEM: asyncio.Semaphore | None = None
_R2_SYNC_LOCK: asyncio.Lock | None = None
_R2_LAST_SYNC_AT: float = 0.0
_R2_SYNC_COOLDOWN_SEC: float = 60.0


def _r2_config():
    account_id = os.getenv("CLOUDFLARE_ACCOUNT_ID", "").strip()
    api_token = os.getenv("CLOUDFLARE_API_TOKEN", "").strip()
    bucket = os.getenv("R2_BUCKET_NAME", "afrc-plates").strip() or "afrc-plates"
    if (
        not account_id
        or not api_token
        or account_id.startswith("your-")
        or api_token.startswith("your-")
    ):
        return None
    return account_id, api_token, bucket


def _register_r2_key(key: str) -> None:
    if not key:
        return
    name = key.rsplit("/", 1)[-1]
    stem = name[:-4] if name.lower().endswith(".jpg") else name
    if "_" in stem:
        bill_no, sid = stem.split("_", 1)
        if bill_no:
            _R2_BILL_KEYS[bill_no.upper()] = key
        if sid:
            _R2_SESSION_KEYS[sid] = key
    else:
        _R2_SESSION_KEYS[stem] = key


def _get_http_client() -> httpx.AsyncClient:
    global _HTTP_CLIENT
    if _HTTP_CLIENT is None or _HTTP_CLIENT.is_closed:
        _HTTP_CLIENT = httpx.AsyncClient(
            timeout=8.0,
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
        )
    return _HTTP_CLIENT


async def close_http_client() -> None:
    global _HTTP_CLIENT
    if _HTTP_CLIENT is not None and not _HTTP_CLIENT.is_closed:
        try:
            await asyncio.wait_for(_HTTP_CLIENT.aclose(), timeout=1.5)
        except Exception:
            pass
        _HTTP_CLIENT = None


async def _upload_plate_to_r2(key: str, image_bytes: bytes) -> bool:
    cfg = _r2_config()
    if not cfg:
        logger.warning(f"[R2] Upload skipped for {key}: R2 configuration missing (_r2_config is None)")
        return False
    account_id, api_token, bucket = cfg
    headers = {
        "Authorization": f"Bearer {api_token}",
        "Content-Type": "image/jpeg",
    }
    try:
        client = _get_http_client()
        for safe_chars in ("/", ""):
            url = f"https://api.cloudflare.com/client/v4/accounts/{account_id}/r2/buckets/{bucket}/objects/{quote(key, safe=safe_chars)}"
            resp = await client.put(url, content=image_bytes, headers=headers, timeout=15.0)
            if 200 <= resp.status_code < 300:
                _register_r2_key(key)
                logger.info(f"[R2] Uploaded {key} to bucket '{bucket}' ({len(image_bytes)} bytes)")
                return True
            else:
                logger.warning(f"[R2] Upload failed (HTTP {resp.status_code}) for '{key}' in bucket '{bucket}': {resp.text[:160]}")
    except Exception as e:
        logger.exception(f"[R2] Exception during upload for '{key}' in bucket '{bucket}': {e}")
        return False
    return False


async def _fetch_plate_from_r2(key: str):
    global _R2_FETCH_SEM
    cfg = _r2_config()
    if not cfg or not key:
        return None
    if _R2_FETCH_SEM is None:
        _R2_FETCH_SEM = asyncio.Semaphore(4)
    account_id, api_token, bucket = cfg
    headers = {"Authorization": f"Bearer {api_token}"}
    try:
        async with _R2_FETCH_SEM:
            client = _get_http_client()
            for safe_chars in ("/", ""):
                url = f"https://api.cloudflare.com/client/v4/accounts/{account_id}/r2/buckets/{bucket}/objects/{quote(key, safe=safe_chars)}"
                resp = await client.get(url, headers=headers, timeout=8.0)
                if 200 <= resp.status_code < 300 and resp.content:
                    return resp.content
    except Exception:
        pass
    return None


async def _record_failed_scan(
    db: Session,
    image_bytes: bytes,
    rejection_code: str,
    rejection_message: str,
    raw_ai: str = "",
    session_id: str = "",
    bill_number: str = "",
    is_retry: bool = False,
    ip: str = "",
    device_id: str = "",
) -> FailedScan:
    scan_uid = str(uuid.uuid4())
    failed_dir = UPLOAD_DIR / "failed_scans"
    try:
        failed_dir.mkdir(parents=True, exist_ok=True)
        if image_bytes and len(image_bytes) > 0:
            (failed_dir / f"{scan_uid}.jpg").write_bytes(image_bytes)
            (failed_dir / f"{rejection_code}_{scan_uid}.jpg").write_bytes(image_bytes)
    except OSError:
        pass

    r2_key = f"failed_scans/{rejection_code}_{scan_uid}.jpg" if (image_bytes and len(image_bytes) > 0) else ""
    if r2_key:
        try:
            uploaded = await _upload_plate_to_r2(r2_key, image_bytes)
            if not uploaded:
                r2_key = f"failed_scans/{scan_uid}.jpg"
        except Exception:
            r2_key = f"failed_scans/{scan_uid}.jpg"

    rec = FailedScan(
        session_id=session_id or "",
        bill_number=bill_number or "",
        is_retry=bool(is_retry),
        ip=ip or "",
        device_id=device_id or "",
        rejection_code=rejection_code or "UNKNOWN",
        rejection_message=rejection_message or "",
        image_key=r2_key,
        raw_ai_response=raw_ai or "",
        created_at=datetime.utcnow(),
    )
    db.add(rec)
    db.commit()
    db.refresh(rec)
    return rec


def _backfill_r2_keys_sync() -> None:
    if not _R2_SESSION_KEYS and not _R2_BILL_KEYS:
        return
    db = SessionLocal()
    try:
        rows = db.query(SessionBill).filter((SessionBill.image_key == None) | (SessionBill.image_key == "")).all()
        changed = False
        for s in rows:
            matched = _R2_SESSION_KEYS.get(s.id) or _R2_BILL_KEYS.get((s.bill_number or "").upper())
            if matched:
                s.image_key = matched
                changed = True
        if changed:
            db.commit()
    finally:
        db.close()


async def _sync_r2_objects_async(force: bool = False) -> None:
    global _R2_SYNC_LOCK, _R2_LAST_SYNC_AT
    cfg = _r2_config()
    if not cfg:
        return
    if _R2_SYNC_LOCK is None:
        _R2_SYNC_LOCK = asyncio.Lock()
    async with _R2_SYNC_LOCK:
        now_mono = asyncio.get_running_loop().time()
        if not force and _R2_LAST_SYNC_AT and (now_mono - _R2_LAST_SYNC_AT) < _R2_SYNC_COOLDOWN_SEC:
            return
        _R2_LAST_SYNC_AT = now_mono
        account_id, api_token, bucket = cfg
        headers = {"Authorization": f"Bearer {api_token}", "Content-Type": "application/json"}
        base = f"https://api.cloudflare.com/client/v4/accounts/{account_id}/r2/buckets"
        try:
            async with httpx.AsyncClient(timeout=8.0) as client:
                check = await client.get(f"{base}/{quote(bucket, safe='')}", headers=headers)
                if check.status_code == 404:
                    await client.post(
                        base,
                        headers=headers,
                        json={"name": bucket, "locationHint": "apac"},
                    )
                objs_resp = await client.get(
                    f"{base}/{quote(bucket, safe='')}/objects",
                    headers=headers,
                )
            if 200 <= objs_resp.status_code < 300:
                data = objs_resp.json()
                for obj in (data.get("result") or []):
                    key = str(obj.get("key") or "").strip()
                    if key:
                        _register_r2_key(key)
                await asyncio.to_thread(_backfill_r2_keys_sync)
        except asyncio.CancelledError:
            raise
        except Exception:
            pass


def _get_bill_image_meta(session_id: str):
    db = SessionLocal()
    try:
        bill = db.query(SessionBill).filter(SessionBill.id == session_id).first()
        if not bill:
            return None
        return {
            "id": bill.id,
            "bill_number": bill.bill_number or "",
            "image_key": bill.image_key or "",
        }
    finally:
        db.close()


def _save_bill_image_key(session_id: str, key: str) -> None:
    db = SessionLocal()
    try:
        bill = db.query(SessionBill).filter(SessionBill.id == session_id).first()
        if bill and not bill.image_key:
            bill.image_key = key
            db.commit()
    finally:
        db.close()
