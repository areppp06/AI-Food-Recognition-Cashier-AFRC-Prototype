import secrets
from fastapi import FastAPI, UploadFile, File, HTTPException, Depends, Form, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from sqlalchemy import func, or_
from sqlalchemy.orm import Session
from typing import Annotated, List
import asyncio
import calendar
import csv
import io
import math
import os
import logging
logger = logging.getLogger("afrc")
import re
import shutil
import subprocess
import uuid
import json
import hashlib
import httpx
import razorpay
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import quote
from dotenv import dotenv_values

from .db import ROOT, ENV_DIR, SessionLocal, init_db, Dish, Cashier, AuditLog, SessionBill, ClientCooldown, ScanAttempt, FailedScan, BillRefund, Feedback, AdminUser, AdminSession, next_bill_number
from .auth import hash_password, verify_password, create_admin_session, get_current_admin, require_admin_api, revoke_session, SESSION_COOKIE_NAME, SESSION_DURATION_DAYS, get_or_create_setup_token, burn_setup_token
from .schemas import Dish as DishSchema, BillResponse, BillItem, ErrorResponse
from .ai_service import recognize_food, RecognitionError

import threading
_IN_FLIGHT_REFUNDS: set[str] = set()
_REFUND_LOCK = threading.Lock()

UPLOAD_DIR = Path(__file__).resolve().parent.parent / "uploads"

FORBIDDEN_SESSION_HTML = """<!DOCTYPE html>
<html>
<head>
    <title>403 Forbidden</title>
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
</head>
<body style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0;background:#f8fafc;padding:1rem;">
    <div style="text-align:center;padding:2.5rem 2rem;background:#fff;border-radius:16px;box-shadow:0 4px 20px rgba(0,0,0,0.06);max-width:420px;border:1px solid #e2e8f0;">
        <div style="font-size:3rem;margin-bottom:1rem;">🔒</div>
        <h1 style="color:#0f172a;font-size:1.5rem;margin:0 0 0.5rem 0;font-weight:700;">403 Forbidden</h1>
        <p style="color:#64748b;font-size:0.95rem;line-height:1.5;margin:0;">You do not have permission to view or pay for this bill session.</p>
    </div>
</body>
</html>"""

MAX_RETAKES = int(os.getenv("MAX_RETAKES", "4"))
COOLDOWN_MINUTES = int(os.getenv("COOLDOWN_MINUTES", "25"))
MAX_SCANS_PER_WINDOW = int(os.getenv("MAX_SCANS_PER_WINDOW", "5"))
MAX_SCANS_PER_SESSION = int(os.getenv("MAX_SCANS_PER_SESSION", str(MAX_SCANS_PER_WINDOW)))
MAX_SCANS_PER_DEVICE = int(os.getenv("MAX_SCANS_PER_DEVICE", str(MAX_SCANS_PER_WINDOW)))
MAX_SCANS_PER_IP = int(os.getenv("MAX_SCANS_PER_IP", "6"))
SCAN_WINDOW_MINUTES = int(os.getenv("SCAN_WINDOW_MINUTES", "15"))
BILL_EXPIRY_MINUTES = int(os.getenv("BILL_EXPIRY_MINUTES", "15"))

_RL_FILE_CACHE: dict[str, tuple[tuple[int, int] | None, dict]] = {}

def _read_env_file_cached(path: Path) -> dict:
    key = str(path)
    try:
        st = path.stat()
        sig = (st.st_mtime_ns, st.st_size)
    except OSError:
        _RL_FILE_CACHE.pop(key, None)
        return {}
    cached = _RL_FILE_CACHE.get(key)
    if cached and cached[0] == sig:
        return cached[1]
    data = {k: v for k, v in dotenv_values(path).items() if v is not None and str(v).strip() != ""}
    _RL_FILE_CACHE[key] = (sig, data)
    return data

def _rl_int(merged_file: dict, key: str, default: int, minimum: int = 1) -> int:
    raw = merged_file.get(key)
    if raw is None:
        raw = os.getenv(key)
    if raw is None or str(raw).strip() == "":
        return max(minimum, int(default))
    try:
        return max(minimum, int(str(raw).strip()))
    except (TypeError, ValueError):
        return max(minimum, int(default))

def _rate_limit_config() -> dict:
    merged: dict = {}
    merged.update(_read_env_file_cached(ENV_DIR / "rate-limit.env"))
    merged.update(_read_env_file_cached(ROOT / "rate-limit.env"))
    fallback_window_max = _rl_int(merged, "MAX_SCANS_PER_WINDOW", MAX_SCANS_PER_WINDOW)
    return {
        "max_scans_per_session": _rl_int(merged, "MAX_SCANS_PER_SESSION", fallback_window_max),
        "max_scans_per_device": _rl_int(merged, "MAX_SCANS_PER_DEVICE", fallback_window_max),
        "max_scans_per_ip": _rl_int(merged, "MAX_SCANS_PER_IP", MAX_SCANS_PER_IP),
        "scan_window_minutes": _rl_int(merged, "SCAN_WINDOW_MINUTES", SCAN_WINDOW_MINUTES),
        "cooldown_minutes": _rl_int(merged, "COOLDOWN_MINUTES", COOLDOWN_MINUTES),
        "max_retakes": _rl_int(merged, "MAX_RETAKES", MAX_RETAKES),
    }
app = FastAPI(title="AFRC Prototype", version="1.0", docs_url=None, redoc_url=None, openapi_url=None)

@app.middleware("http")
async def add_anti_indexing_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Robots-Tag"] = "noindex, nofollow, noarchive, nosnippet"
    return response

_R2_SESSION_KEYS: dict[str, str] = {}
_R2_BILL_KEYS: dict[str, str] = {}

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
    if name.lower().endswith(".jpg"):
        stem = name[:-4]
    else:
        stem = name
    if "_" in stem:
        bill_no, sid = stem.split("_", 1)
        if bill_no:
            _R2_BILL_KEYS[bill_no.upper()] = key
        if sid:
            _R2_SESSION_KEYS[sid] = key
    else:
        _R2_SESSION_KEYS[stem] = key

_HTTP_CLIENT: httpx.AsyncClient | None = None
_R2_FETCH_SEM: asyncio.Semaphore | None = None
_R2_MISSING_UNTIL: dict[str, float] = {}
_R2_MISSING_TTL_SEC: float = 120.0

def _get_http_client() -> httpx.AsyncClient:
    global _HTTP_CLIENT
    if _HTTP_CLIENT is None or _HTTP_CLIENT.is_closed:
        _HTTP_CLIENT = httpx.AsyncClient(
            timeout=8.0,
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
        )
    return _HTTP_CLIENT

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

# Serve frontend
app.mount("/static", StaticFiles(directory="frontend"), name="static")

def _clean_description(value) -> str:
    text = str(value or "").strip()
    if len(text) > 500:
        raise HTTPException(status_code=400, detail="Description must be 500 characters or less")
    return text

def _clean_pieces(value):
    if value is None or value == "":
        return 1
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="Pieces per portion must be a whole number")
    if number < 1 or number > 100:
        raise HTTPException(status_code=400, detail="Pieces per portion must be from 1 to 100")
    return number

def _bill_quantity(raw_quantity: float, pieces_per_portion):
    pieces = int(round(float(raw_quantity)))
    if pieces < 1:
        return 0, None
    group = pieces_per_portion or 0
    if group <= 1:
        return pieces, None
    return math.ceil(pieces / group), pieces

def _dish_json(dish: Dish) -> dict:
    return {
        "id": dish.id,
        "name": dish.name,
        "alias": dish.alias or "",
        "display_name": (dish.alias or "").strip() or dish.name,
        "price": dish.price,
        "available": dish.available,
        "unit": dish.unit,
        "description": dish.description or "",
        "pieces_per_portion": dish.pieces_per_portion or 1,
    }

def _dish_alias_map(db: Session = None) -> dict[str, str]:
    close_after = False
    if db is None:
        db = SessionLocal()
        close_after = True
    try:
        dishes = db.query(Dish.name, Dish.alias).all()
        mapping = {}
        for name, alias in dishes:
            if name:
                disp = (alias or "").strip() or name
                mapping[name] = disp
                mapping[name.casefold()] = disp
                mapping[" ".join(name.strip().casefold().split())] = disp
        return mapping
    finally:
        if close_after:
            db.close()

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

def _mask_phone(phone: str) -> str:
    p = str(phone or "").strip()
    if not p or len(p) < 6:
        return p
    if p.startswith("+"):
        prefix = p[:4]
        suffix = p[-3:]
        stars = "•" * max(3, len(p) - 7)
        return f"{prefix}{stars}{suffix}"
    prefix = p[:3]
    suffix = p[-3:]
    stars = "•" * max(3, len(p) - 6)
    return f"{prefix}{stars}{suffix}"

def _mask_email(email: str) -> str:
    e = str(email or "").strip()
    if not e or "@" not in e:
        return e
    parts = e.split("@", 1)
    user, domain = parts[0], parts[1]
    if len(user) <= 2:
        masked_user = user[0] + "•"
    else:
        masked_user = user[0] + ("•" * max(2, len(user) - 2)) + user[-1]
    return f"{masked_user}@{domain}"

def _verify_bill_ownership(bill, request: Request, db, explicit_token: str = "") -> None:
    current_admin = get_current_admin(request, db)
    if current_admin:
        return
    if not bill.client_token:
        return
    c_token = _get_client_token(request, explicit_token)
    if not c_token or c_token != bill.client_token:
        raise HTTPException(
            status_code=403,
            detail="You do not have permission to modify this bill."
        )

def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        parts = [p.strip() for p in forwarded.split(",") if p.strip()]
        if parts:
            return parts[0]
    if request.client and request.client.host:
        return request.client.host
    return ""

def _device_fingerprint(request: Request) -> str:
    ip = _client_ip(request)
    ua = request.headers.get("user-agent", "").strip()
    raw = f"{ip}|{ua}"
    return "fp-" + hashlib.sha256(raw.encode("utf-8", errors="ignore")).hexdigest()[:28]

def _get_device_id(request: Request, explicit_device: str = "", explicit_client: str = "") -> str:
    dev = (
        (explicit_device or "").strip()
        or request.cookies.get("afrc_device_id", "").strip()
        or request.headers.get("x-afrc-device", "").strip()
        or (explicit_client or "").strip()
        or request.cookies.get("afrc_client_id", "").strip()
        or request.headers.get("x-afrc-client", "").strip()
    )
    if dev and len(dev) <= 128:
        return dev
    return _device_fingerprint(request)

def _get_client_token(request: Request, explicit: str = "") -> str:
    token = (
        (explicit or "").strip()
        or request.cookies.get("afrc_client_id", "").strip()
        or request.headers.get("x-afrc-client", "").strip()
    )
    if token and len(token) <= 128:
        return token
    return _device_fingerprint(request)

def _all_client_tokens(request: Request, explicit: str = "", explicit_device: str = "") -> list:
    tokens = []
    for candidate in (
        (explicit or "").strip(),
        (explicit_device or "").strip(),
        request.cookies.get("afrc_client_id", "").strip(),
        request.cookies.get("afrc_device_id", "").strip(),
        request.headers.get("x-afrc-client", "").strip(),
        request.headers.get("x-afrc-device", "").strip(),
        _device_fingerprint(request),
    ):
        if candidate and len(candidate) <= 128 and candidate not in tokens:
            tokens.append(candidate)
    return tokens

def _all_session_ids(request: Request, explicit_session: str = "") -> list:
    sids = []
    for candidate in (
        (explicit_session or "").strip(),
        request.headers.get("x-afrc-session", "").strip(),
        request.cookies.get("afrc_browser_session", "").strip(),
        request.cookies.get("afrc_active_session", "").strip(),
    ):
        if candidate and len(candidate) <= 128 and candidate not in sids:
            sids.append(candidate)
    return sids

def _get_scan_session_id(request: Request, explicit_session: str = "") -> str:
    sid = (
        request.headers.get("x-afrc-session", "").strip()
        or request.cookies.get("afrc_browser_session", "").strip()
        or (explicit_session or "").strip()
        or request.cookies.get("afrc_active_session", "").strip()
    )
    if sid and len(sid) <= 128:
        return sid
    return ""

def _rate_limit_keys(
    request: Request,
    client_token: str = "",
    session_id: str = "",
    device_id: str = "",
) -> list:
    keys = []
    for t in _all_client_tokens(request, client_token, device_id):
        for prefix in ("token:", "device:"):
            k = f"{prefix}{t}"
            if k not in keys:
                keys.append(k)
    ip = _client_ip(request)
    if ip:
        keys.append(f"ip:{ip}")
    for sid in _all_session_ids(request, session_id):
        k = f"session:{sid}"
        if k not in keys:
            keys.append(k)
    return keys

def _find_active_bill(
    request: Request,
    db: Session,
    session_id: str = "",
    explicit_token: str = "",
):
    active_statuses = ("open", "pending", "retaking")
    for sid in (
        (session_id or "").strip(),
        request.cookies.get("afrc_active_session", "").strip(),
    ):
        if sid:
            b = db.query(SessionBill).filter(SessionBill.id == sid).first()
            if b and b.status in active_statuses:
                return b
    tokens = _all_client_tokens(request, explicit_token)
    if tokens:
        b = (
            db.query(SessionBill)
            .filter(
                SessionBill.client_token.in_(tokens),
                SessionBill.status.in_(active_statuses),
            )
            .order_by(SessionBill.created_at.desc())
            .first()
        )
        if b:
            return b
    return None

def _expire_stale_bills(db: Session) -> None:
    cutoff = datetime.utcnow() - timedelta(minutes=BILL_EXPIRY_MINUTES)
    stale = (
        db.query(SessionBill)
        .filter(
            or_(
                SessionBill.status == "needs_cashier",
                (
                    SessionBill.status.in_(("open", "pending", "retaking"))
                    & (func.coalesce(SessionBill.updated_at, SessionBill.created_at) < cutoff)
                ),
            )
        )
        .all()
    )
    if not stale:
        return
    now = datetime.utcnow()
    for bill in stale:
        if bill.status == "needs_cashier":
            bill.status = "void"
            bill.void_reason = bill.void_reason or "Needed Cashier Assistance"
            bill.updated_at = now
        else:
            bill.status = "void"
            bill.void_reason = "Bill timed out"
            bill.updated_at = now
    db.commit()

def _scan_usage_status(
    request: Request,
    client_token: str,
    db: Session,
    session_id: str = "",
    device_id: str = "",
) -> dict:
    cfg = _rate_limit_config()
    window_start = datetime.utcnow() - timedelta(minutes=cfg["scan_window_minutes"])
    dev_ids = _all_client_tokens(request, client_token, device_id)
    sids = _all_session_ids(request, session_id)
    ip = _client_ip(request)

    device_scans = (
        db.query(ScanAttempt)
        .filter(
            ScanAttempt.created_at >= window_start,
            ScanAttempt.device_id.in_(dev_ids),
        )
        .count()
        if dev_ids
        else 0
    )
    session_scans = (
        db.query(ScanAttempt)
        .filter(
            ScanAttempt.created_at >= window_start,
            ScanAttempt.session_id.in_(sids),
        )
        .count()
        if sids
        else 0
    )
    ip_scans = (
        db.query(ScanAttempt)
        .filter(
            ScanAttempt.created_at >= window_start,
            ScanAttempt.ip == ip,
        )
        .count()
        if ip
        else 0
    )

    sess_limit = cfg["max_scans_per_session"]
    dev_limit = cfg["max_scans_per_device"]
    ip_limit = cfg["max_scans_per_ip"]
    used = max(device_scans, session_scans)
    remaining = min(
        max(0, sess_limit - session_scans),
        max(0, dev_limit - device_scans),
        max(0, ip_limit - ip_scans),
    )
    return {
        "device_scans": device_scans,
        "session_scans": session_scans,
        "ip_scans": ip_scans,
        "scans_used": used,
        "scans_limit": min(sess_limit, dev_limit),
        "scans_remaining": remaining,
    }

def _check_cooldown(
    request: Request,
    client_token: str,
    db: Session,
    session_id: str = "",
    device_id: str = "",
) -> tuple[int, str]:
    now = datetime.utcnow()
    keys = _rate_limit_keys(
        request,
        client_token=client_token,
        session_id=session_id,
        device_id=device_id,
    )
    if not keys:
        return 0, ""
    rows = db.query(ClientCooldown).filter(ClientCooldown.client_key.in_(keys)).all()
    max_remaining = 0
    reason = ""
    for r in rows:
        if r.cooldown_until and r.cooldown_until > now:
            rem = math.ceil((r.cooldown_until - now).total_seconds() / 60.0)
            if rem > max_remaining:
                max_remaining = rem
                reason = (r.reason or "").strip()
    return max_remaining, reason

def _set_cooldown(
    request: Request,
    client_token: str,
    db: Session,
    session_id: str = "",
    device_id: str = "",
    reason: str = "",
) -> None:
    cfg = _rate_limit_config()
    until = datetime.utcnow() + timedelta(minutes=cfg["cooldown_minutes"])
    keys = _rate_limit_keys(
        request,
        client_token=client_token,
        session_id=session_id,
        device_id=device_id,
    )
    for k in keys:
        row = db.query(ClientCooldown).filter(ClientCooldown.client_key == k).first()
        if row:
            row.cooldown_until = until
            if reason:
                row.reason = reason
        else:
            db.add(ClientCooldown(client_key=k, cooldown_until=until, reason=reason))
    db.commit()

def _record_and_enforce_scan_limit(
    request: Request,
    client_token: str,
    db: Session,
    session_id: str = "",
    device_id: str = "",
) -> dict:
    cfg = _rate_limit_config()
    usage = _scan_usage_status(
        request,
        client_token=client_token,
        db=db,
        session_id=session_id,
        device_id=device_id,
    )
    reason = ""
    if usage["session_scans"] >= cfg["max_scans_per_session"]:
        reason = f"Too many photo scan attempts in this session (max {cfg['max_scans_per_session']} per {cfg['scan_window_minutes']} min)."
    elif usage["device_scans"] >= cfg["max_scans_per_device"]:
        reason = f"Too many photo scan attempts from this device (max {cfg['max_scans_per_device']} per {cfg['scan_window_minutes']} min)."
    elif usage["ip_scans"] >= cfg["max_scans_per_ip"]:
        reason = f"Too many photo scan attempts from your network IP (max {cfg['max_scans_per_ip']} per {cfg['scan_window_minutes']} min)."

    if reason:
        _set_cooldown(
            request,
            client_token=client_token,
            db=db,
            session_id=session_id,
            device_id=device_id,
            reason=reason,
        )
        raise HTTPException(
            status_code=429,
            detail=ErrorResponse(
                error_code="RATE_LIMIT",
                message=f"Your rate limit hit, try again after {cfg['cooldown_minutes']}min.",
            ).dict(),
        )

    dev_id = _get_device_id(request, explicit_device=device_id, explicit_client=client_token)
    sid = _get_scan_session_id(request, explicit_session=session_id)
    ip = _client_ip(request)
    db.add(
        ScanAttempt(
            ip=ip,
            device_id=dev_id,
            session_id=sid,
            created_at=datetime.utcnow(),
        )
    )
    db.commit()
    return _scan_usage_status(
        request,
        client_token=client_token,
        db=db,
        session_id=session_id,
        device_id=device_id,
    )

def _require_turnstile(token: str, expected_action: str, request: Request) -> None:
    hostnames = {
        hostname.strip().lower()
        for hostname in os.getenv("TURNSTILE_HOSTNAMES", "").split(",")
        if hostname.strip()
    }
    # Auto-allow DOMAIN and its www. variant if configured in env
    domain_env = (os.getenv("DOMAIN") or "").strip().lower()
    if domain_env:
        hostnames.add(domain_env)
        if not domain_env.startswith("www."):
            hostnames.add(f"www.{domain_env}")
        else:
            hostnames.add(domain_env[4:])
    # Also allow localhost for local testing
    hostnames.update({"localhost", "127.0.0.1"})

    secret = os.getenv("TURNSTILE_SECRET", "")
    if not isinstance(token, str) or not token or len(token) > 2048 or not hostnames or not secret:
        raise HTTPException(status_code=403, detail="forbidden")
    try:
        response = httpx.post(
            "https://challenges.cloudflare.com/turnstile/v0/siteverify",
            data={"secret": secret, "response": token, "remoteip": _client_ip(request)},
            timeout=10.0,
        )
        if response.status_code < 200 or response.status_code >= 300:
            raise HTTPException(status_code=403, detail="forbidden")
        result = response.json()
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=403, detail="forbidden")

    result_hostname = str(result.get("hostname") or "").strip().lower()
    result_base = result_hostname[4:] if result_hostname.startswith("www.") else result_hostname
    hostname_matches = any(
        result_hostname == h or result_base == (h[4:] if h.startswith("www.") else h)
        for h in hostnames
    )

    if (
        result.get("success") is not True
        or result.get("action") != expected_action
        or not hostname_matches
    ):
        logger.warning(
            f"Turnstile siteverify rejected: success={result.get('success')}, action={result.get('action')} "
            f"(expected {expected_action}), hostname={result_hostname} (allowed: {hostnames}), "
            f"error-codes={result.get('error-codes')}"
        )
        raise HTTPException(status_code=403, detail="forbidden")

_R2_SYNC_LOCK: asyncio.Lock | None = None
_R2_LAST_SYNC_AT: float = 0.0
_R2_SYNC_COOLDOWN_SEC: float = 60.0
_BACKGROUND_TASKS: set[asyncio.Task] = set()

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

def _run_stale_bills_cleanup_sync() -> None:
    db = SessionLocal()
    try:
        _expire_stale_bills(db)
    except Exception:
        pass
    finally:
        db.close()

async def _stale_bills_watcher():
    try:
        while True:
            await asyncio.sleep(60)
            await asyncio.to_thread(_run_stale_bills_cleanup_sync)
    except asyncio.CancelledError:
        return

def _sync_legacy_scans_sync():
    db = SessionLocal()
    try:
        existing_sids = set(
            r[0] for r in db.query(ScanAttempt.session_id).filter(ScanAttempt.session_id != "").all()
        )
        sessions = db.query(SessionBill).all()
        to_add = []
        for s in sessions:
            if s.id not in existing_sids:
                dt = s.created_at or s.updated_at or datetime.utcnow()
                to_add.append(ScanAttempt(session_id=s.id, ip="127.0.0.1", device_id="legacy", created_at=dt))
                for i in range(int(s.retake_count or 0)):
                    to_add.append(ScanAttempt(session_id=s.id, ip="127.0.0.1", device_id="legacy", created_at=dt))
        if to_add:
            db.add_all(to_add)
            db.commit()
    except Exception:
        db.rollback()
    finally:
        db.close()

def _track_task(coro) -> asyncio.Task:
    task = asyncio.create_task(coro)
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_TASKS.discard)
    return task

def _sync_caddy_domain_sync() -> None:
    raw_domain = os.getenv("DOMAIN", "").strip()
    if not raw_domain:
        return
    caddy_file = Path("/etc/caddy/Caddyfile")
    if not caddy_file.parent.exists():
        return
    caddy_bin = shutil.which("caddy")
    if not caddy_bin:
        return

    domains = [d.strip() for d in re.split(r"[,;\s]+", raw_domain) if d.strip()]
    if not domains:
        return
    domain_header = ", ".join(domains)
    port = os.getenv("PORT", "8000").strip() or "8000"

    expected_content = f"""{domain_header} {{
    header {{
        X-Frame-Options "SAMEORIGIN"
        X-Content-Type-Options "nosniff"
        Referrer-Policy "strict-origin-when-cross-origin"
        X-Robots-Tag "noindex, nofollow, noarchive, nosnippet"
        -Server
    }}
    request_body {{
        max_size 2MB
    }}
    reverse_proxy 127.0.0.1:{port} {{
        header_up -CF-Connecting-IP
        trusted_proxies 173.245.48.0/20 103.21.244.0/22 103.22.200.0/22 103.31.4.0/22 141.101.64.0/18 108.162.192.0/18 190.93.240.0/20 188.114.96.0/20 197.234.240.0/22 198.41.128.0/17 162.158.0.0/15 104.16.0.0/13 104.24.0.0/14 172.64.0.0/13 131.0.72.0/22 2400:cb00::/32 2606:4700::/32 2803:f800::/32 2405:b500::/32 2405:8100::/32 2a06:98c0::/29 2c0f:f248::/32
    }}
}}
"""
    try:
        current_content = caddy_file.read_text(encoding="utf-8") if caddy_file.exists() else ""
        if current_content.strip() != expected_content.strip():
            caddy_file.write_text(expected_content, encoding="utf-8")
            subprocess.run(
                ["caddy", "reload", "--config", str(caddy_file)],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
            )
    except Exception:
        pass

@app.on_event("startup")
async def startup():
    init_db()
    await asyncio.to_thread(_sync_caddy_domain_sync)
    await asyncio.to_thread(_sync_legacy_scans_sync)
    await asyncio.to_thread(_run_stale_bills_cleanup_sync)
    _track_task(_sync_r2_objects_async(force=True))
    _track_task(_stale_bills_watcher())

@app.on_event("shutdown")
async def shutdown():
    global _HTTP_CLIENT
    tasks = list(_BACKGROUND_TASKS)
    for task in tasks:
        task.cancel()
    if tasks:
        try:
            await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=2.0)
        except (asyncio.TimeoutError, Exception):
            pass
    if _HTTP_CLIENT is not None and not _HTTP_CLIENT.is_closed:
        try:
            await asyncio.wait_for(_HTTP_CLIENT.aclose(), timeout=1.5)
        except Exception:
            pass
        _HTTP_CLIENT = None

NO_CACHE_HEADERS = {
    "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
    "Pragma": "no-cache",
    "Expires": "0",
    "X-Robots-Tag": "noindex, nofollow, noarchive, nosnippet",
}

@app.get("/robots.txt", include_in_schema=False)
def robots_txt():
    return Response(
        content="User-agent: *\nDisallow: /\n",
        media_type="text/plain",
        headers={
            "Cache-Control": "public, max-age=86400",
            "X-Robots-Tag": "noindex, nofollow, noarchive, nosnippet",
        },
    )

@app.get("/")
async def root():
    settings = _restaurant_settings()
    if settings["maintenance_mode"]:
        return RedirectResponse("/maintenance", status_code=307)
    if settings["coming_soon"]:
        return RedirectResponse("/coming-soon", status_code=307)
    return FileResponse("frontend/index.html", headers=NO_CACHE_HEADERS)

@app.get("/coming-soon")
async def coming_soon_page():
    return FileResponse("frontend/coming_soon.html", headers=NO_CACHE_HEADERS)

@app.get("/maintenance")
async def maintenance_page():
    return FileResponse("frontend/maintenance.html", headers=NO_CACHE_HEADERS)

@app.get("/privacy")
@app.get("/privacy-policy")
async def privacy_page():
    return FileResponse("frontend/privacy.html", headers=NO_CACHE_HEADERS)

@app.get("/terms")
@app.get("/terms-and-conditions")
async def terms_page():
    return FileResponse("frontend/terms.html", headers=NO_CACHE_HEADERS)

@app.get("/payment-policy")
@app.get("/refund-policy")
@app.get("/payment-and-refund-policy")
async def payment_policy_page():
    return FileResponse("frontend/payment-policy.html", headers=NO_CACHE_HEADERS)

@app.get("/contact")
@app.get("/contact-us")
async def contact_page():
    return FileResponse("frontend/contact.html", headers=NO_CACHE_HEADERS)

@app.get("/about")
@app.get("/about-us")
async def about_page():
    return FileResponse("frontend/about.html", headers=NO_CACHE_HEADERS)

@app.get("/pricelist")
@app.get("/prices")
async def pricelist_page():
    return FileResponse("frontend/pricelist.html", headers=NO_CACHE_HEADERS)

@app.get("/guide")
@app.get("/how-to-use")
@app.get("/learn")
async def guide_page():
    return FileResponse("frontend/guide.html", headers=NO_CACHE_HEADERS)

@app.get("/api/public-profile")
def get_public_profile():
    return _restaurant_profile()

@app.get("/api/pricelist")
def get_public_pricelist(db: Session = Depends(get_db)):
    dishes = db.query(Dish).order_by(Dish.name.asc()).all()
    latest_audit = (
        db.query(AuditLog)
        .filter(AuditLog.action.like("MENU_%"))
        .order_by(AuditLog.timestamp.desc())
        .first()
    )
    if latest_audit and latest_audit.timestamp:
        last_updated = latest_audit.timestamp.strftime("%Y-%m-%d %H:%M:%S")
    else:
        db_path = ROOT / "afrc.db"
        if db_path.exists():
            last_updated = datetime.fromtimestamp(db_path.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")
        else:
            last_updated = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")

    items = []
    for d in dishes:
        pieces = d.pieces_per_portion or 1
        unit = d.unit or "portion"
        price = round(float(d.price or 0.0), 2)
        price_per_piece = round(price / pieces, 2) if pieces > 0 else price
        display_name = (d.alias or "").strip() or d.name
        items.append({
            "id": d.id,
            "name": display_name,
            "raw_name": d.name,
            "alias": d.alias or "",
            "price": price,
            "unit": unit,
            "pieces_per_portion": pieces,
            "price_per_piece": price_per_piece,
            "available": bool(d.available),
            "description": d.description or "",
        })

    prof = _restaurant_profile()
    return {
        "restaurant_name": prof["restaurant_name"],
        "last_updated": last_updated,
        "items": items,
    }

@app.get("/login")
async def login_page(request: Request, db: Session = Depends(get_db)):
    current_admin = get_current_admin(request, db)
    if current_admin:
        return RedirectResponse(url="/admin", status_code=303)
    return FileResponse("frontend/login.html", headers=NO_CACHE_HEADERS)

@app.get("/admin")
async def admin(request: Request, db: Session = Depends(get_db)):
    admin_count = db.query(AdminUser).count()
    if admin_count == 0:
        return RedirectResponse(url="/login?setup=1", status_code=303)
    current_admin = get_current_admin(request, db)
    if not current_admin:
        return RedirectResponse(url="/login?next=/admin", status_code=303)
    return FileResponse("frontend/admin.html", headers=NO_CACHE_HEADERS)


_FAILED_LOGINS_IP: dict[str, list[float]] = {}
_FAILED_LOGINS_USER: dict[str, list[float]] = {}
MAX_LOGIN_FAILURES = 5
LOGIN_LOCKOUT_SECONDS = 15 * 60  # 15 minutes

def _check_login_rate_limit(request: Request, username: str) -> None:
    now = datetime.utcnow().timestamp()
    ip = _client_ip(request)
    cutoff = now - LOGIN_LOCKOUT_SECONDS
    for bucket, key, label in [
        (_FAILED_LOGINS_IP, ip, "network IP"),
        (_FAILED_LOGINS_USER, username.strip().lower(), f"account '{username}'")
    ]:
        if not key:
            continue
        attempts = [t for t in bucket.get(key, []) if t > cutoff]
        bucket[key] = attempts
        if len(attempts) >= MAX_LOGIN_FAILURES:
            oldest = attempts[0]
            remaining_mins = max(1, math.ceil((oldest + LOGIN_LOCKOUT_SECONDS - now) / 60))
            raise HTTPException(
                status_code=429,
                detail=f"Too many failed login attempts for this {label}. Locked for {remaining_mins} more minute(s)."
            )

def _record_login_failure(request: Request, username: str, db: Session) -> None:
    now = datetime.utcnow().timestamp()
    ip = _client_ip(request)
    u = username.strip().lower()
    if ip:
        _FAILED_LOGINS_IP.setdefault(ip, []).append(now)
    if u:
        _FAILED_LOGINS_USER.setdefault(u, []).append(now)
    try:
        db.add(AuditLog(
            cashier_id=0,
            admin_username=username[:100],
            action="LOGIN_FAILED",
            details=f"Failed login attempt for '{username}' from IP {ip}"
        ))
        db.commit()
    except Exception:
        pass

def _record_login_success(request: Request, username: str, user_id: int, db: Session) -> None:
    ip = _client_ip(request)
    u = username.strip().lower()
    _FAILED_LOGINS_USER.pop(u, None)
    if ip and ip in _FAILED_LOGINS_IP:
        _FAILED_LOGINS_IP.pop(ip, None)
    try:
        db.add(AuditLog(
            cashier_id=user_id,
            admin_username=username[:100],
            action="LOGIN_SUCCESS",
            details=f"Admin '{username}' logged in successfully from IP {ip}"
        ))
        db.commit()
    except Exception:
        pass

def _csv_escape(val) -> str:
    s = str(val if val is not None else "")
    if s and s[0] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + s
    return s

@app.get("/api/auth/status")
def auth_status(request: Request, db: Session = Depends(get_db)):
    admin_count = db.query(AdminUser).count()
    current_admin = get_current_admin(request, db)
    return {
        "needs_setup": admin_count == 0,
        "is_authenticated": current_admin is not None,
        "username": current_admin.username if current_admin else None,
        "restaurant_name": _restaurant_name() or "AFRC",
    }

@app.post("/api/auth/setup")
def auth_setup(payload: dict, request: Request, response: Response, db: Session = Depends(get_db)):
    if db.query(AdminUser).count() > 0:
        raise HTTPException(status_code=400, detail="Setup already completed. Please log in.")

    req_token = str(payload.get("setup_token") or payload.get("token") or "").strip()
    expected_token = get_or_create_setup_token()
    if not req_token or not secrets.compare_digest(req_token, expected_token):
        raise HTTPException(status_code=403, detail="Invalid or missing one-time setup token. Check your VPS terminal for the link or QR.")

    username = str(payload.get("username") or "").strip()
    password = str(payload.get("password") or "")
    if len(username) < 3:
        raise HTTPException(status_code=400, detail="Username must be at least 3 characters.")
    if len(password) < 6:
        raise HTTPException(status_code=400, detail="Password must be at least 6 characters.")
    
    user = AdminUser(
        username=username,
        password_hash=hash_password(password),
        created_at=datetime.utcnow(),
        last_login_at=datetime.utcnow(),
    )
    db.add(user)
    db.add(AuditLog(
        cashier_id=0,
        admin_username=username,
        action="ADMIN_INITIAL_SETUP",
        details=f"Initial admin '{username}' created from {request.client.host if request.client else 'local'}"
    ))
    db.commit()
    db.refresh(user)

    burn_setup_token()

    ip = request.client.host if request.client else ""
    ua = request.headers.get("user-agent", "")
    token = create_admin_session(db, user.id, ip, ua)
    
    is_https = request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=token,
        max_age=86400 * SESSION_DURATION_DAYS,
        httponly=True,
        samesite="lax",
        secure=is_https,
    )
    return {"ok": True, "username": user.username}

@app.post("/api/auth/login")
def auth_login(payload: dict, request: Request, response: Response, db: Session = Depends(get_db)):
    username = str(payload.get("username") or "").strip()
    password = str(payload.get("password") or "")

    _check_login_rate_limit(request, username)

    user = db.query(AdminUser).filter(AdminUser.username == username).first()
    if not user or not verify_password(password, user.password_hash):
        _record_login_failure(request, username, db)
        raise HTTPException(status_code=401, detail="Invalid username or password.")
    
    user.last_login_at = datetime.utcnow()
    db.commit()

    _record_login_success(request, user.username, user.id, db)

    ip = request.client.host if request.client else ""
    ua = request.headers.get("user-agent", "")
    token = create_admin_session(db, user.id, ip, ua)
    
    is_https = request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=token,
        max_age=86400 * SESSION_DURATION_DAYS,
        httponly=True,
        samesite="lax",
        secure=is_https,
    )
    return {"ok": True, "username": user.username}

@app.post("/api/auth/logout")
def auth_logout(request: Request, response: Response, db: Session = Depends(get_db)):
    token = request.cookies.get(SESSION_COOKIE_NAME)
    if token:
        revoke_session(db, token)
    response.delete_cookie(key=SESSION_COOKIE_NAME)
    return {"ok": True}

@app.post("/api/auth/change-password")
def auth_change_password(payload: dict, request: Request, db: Session = Depends(get_db)):
    current_admin = require_admin_api(request, db)
    old_password = str(payload.get("old_password") or "")
    new_password = str(payload.get("new_password") or "")
    
    if not verify_password(old_password, current_admin.password_hash):
        raise HTTPException(status_code=400, detail="Current password incorrect.")
    if len(new_password) < 6:
        raise HTTPException(status_code=400, detail="New password must be at least 6 characters.")
    
    current_admin.password_hash = hash_password(new_password)

    # Revoke all other active sessions for this admin (keep current session active)
    current_token = request.cookies.get(SESSION_COOKIE_NAME) or ""
    if not current_token:
        auth_h = request.headers.get("Authorization", "")
        if auth_h.startswith("Bearer "):
            current_token = auth_h[7:].strip()
    db.query(AdminSession).filter(
        AdminSession.user_id == current_admin.id,
        AdminSession.token != current_token
    ).delete()

    db.add(AuditLog(
        cashier_id=current_admin.id,
        admin_username=current_admin.username,
        action="PASSWORD_CHANGE",
        details=f"Admin '{current_admin.username}' updated password (all other sessions revoked)"
    ))
    db.commit()
    return {"ok": True, "message": "Password updated successfully. Other active sessions revoked."}

@app.get("/pay")
async def pay_page(request: Request, session: str = "", db: Session = Depends(get_db)):
    if session:
        bill = db.query(SessionBill).filter(SessionBill.id == session).first()
        if bill and bill.client_token:
            current_admin = get_current_admin(request, db)
            if not current_admin:
                c_token = _get_client_token(request)
                if not c_token or c_token != bill.client_token:
                    return Response(
                        content=FORBIDDEN_SESSION_HTML,
                        status_code=403,
                        media_type="text/html",
                    )
    return FileResponse("frontend/pay.html", headers=NO_CACHE_HEADERS)

@app.get("/payment/success")
async def payment_success_page(request: Request, session: str = "", db: Session = Depends(get_db)):
    if session:
        bill = db.query(SessionBill).filter(SessionBill.id == session).first()
        if bill and bill.client_token:
            current_admin = get_current_admin(request, db)
            if not current_admin:
                c_token = _get_client_token(request)
                if not c_token or c_token != bill.client_token:
                    return Response(
                        content=FORBIDDEN_SESSION_HTML,
                        status_code=403,
                        media_type="text/html",
                    )
    return FileResponse("frontend/success.html", headers=NO_CACHE_HEADERS)

@app.get("/api/menu")
def get_menu(db: Session = Depends(get_db)):
    dishes = db.query(Dish).all()
    return [_dish_json(d) for d in dishes]

@app.post("/api/menu")
def update_menu(dishes: List[dict], request: Request, db: Session = Depends(get_db)):
    current_admin = require_admin_api(request, db)
    for d in dishes:
        dish = db.query(Dish).filter(Dish.id == d["id"]).first()
        if dish:
            dish.available = d.get("available", dish.available)
            if "alias" in d:
                dish.alias = str(d.get("alias") or "").strip()
            if "price" in d:
                price_str = str(d["price"]).strip()
                try:
                    p = float(price_str)
                except (TypeError, ValueError):
                    raise HTTPException(status_code=400, detail=f"Invalid price for dish '{dish.name}'")
                if p < 0.10 or p > 10000:
                    raise HTTPException(status_code=400, detail=f"Price must be at least RM 0.10 and cannot exceed RM 10,000 for dish '{dish.name}'")
                if not re.match(r"^\d+(\.\d{1,2})?$", price_str) and round(p, 2) != round(p, 4):
                    raise HTTPException(status_code=400, detail=f"Price can have at most 2 decimal places for dish '{dish.name}'")
                dish.price = round(p, 2)
            if "description" in d:
                dish.description = _clean_description(d.get("description"))
            if "pieces_per_portion" in d:
                dish.pieces_per_portion = _clean_pieces(d.get("pieces_per_portion"))
    db.add(AuditLog(
        cashier_id=current_admin.id,
        admin_username=current_admin.username,
        action="MENU_UPDATE",
        details=f"Admin '{current_admin.username}' updated menu ({len(dishes)} item(s) modified)"
    ))
    db.commit()
    return {"status": "updated"}

@app.post("/api/menu/item")
def add_menu_item(payload: dict, request: Request, db: Session = Depends(get_db)):
    current_admin = require_admin_api(request, db)
    name = str(payload.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="Dish name is required")
    price_str = str(payload.get("price") or "").strip()
    try:
        price = float(price_str)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="A valid price is required")
    if price < 0.10 or price > 10000:
        raise HTTPException(status_code=400, detail="Price must be at least RM 0.10 and cannot exceed RM 10,000")
    if not re.match(r"^\d+(\.\d{1,2})?$", price_str) and round(price, 2) != round(price, 4):
        raise HTTPException(status_code=400, detail="Price can have at most 2 decimal places")
    existing = db.query(Dish).filter(func.lower(Dish.name) == name.casefold()).first()
    if existing:
        raise HTTPException(status_code=409, detail="A dish with this name already exists")
    unit = str(payload.get("unit") or "portion").strip() or "portion"
    alias = str(payload.get("alias") or "").strip()
    dish = Dish(
        name=name,
        alias=alias,
        price=round(price, 2),
        available=bool(payload.get("available", True)),
        unit=unit,
        description=_clean_description(payload.get("description")),
        pieces_per_portion=_clean_pieces(payload.get("pieces_per_portion")),
    )
    db.add(dish)
    db.add(AuditLog(
        cashier_id=current_admin.id,
        admin_username=current_admin.username,
        action="MENU_ADD",
        details=f"Admin '{current_admin.username}' added dish '{dish.name}' (RM {dish.price:.2f})"
    ))
    db.commit()
    db.refresh(dish)
    return _dish_json(dish)

@app.delete("/api/menu/{dish_id}")
def delete_menu_item(dish_id: int, request: Request, db: Session = Depends(get_db)):
    current_admin = require_admin_api(request, db)
    dish = db.query(Dish).filter(Dish.id == dish_id).first()
    if not dish:
        raise HTTPException(status_code=404, detail="Dish not found")
    d_name = dish.name
    db.delete(dish)
    db.add(AuditLog(
        cashier_id=current_admin.id,
        admin_username=current_admin.username,
        action="MENU_DELETE",
        details=f"Admin '{current_admin.username}' deleted dish '{d_name}'"
    ))
    db.commit()
    return {"status": "deleted", "id": dish_id}

@app.get("/api/admin/menu/export")
def export_admin_menu_csv(request: Request, db: Session = Depends(get_db)):
    require_admin_api(request, db)
    dishes = db.query(Dish).order_by(Dish.name.asc()).all()

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["Name", "Alias", "Price", "Pieces", "Available", "Description"])
    for d in dishes:
        p_val = f"{d.price:.2f}" if d.price is not None else "0.00"
        avail_str = "Yes" if d.available else "No"
        pieces_val = d.pieces_per_portion if d.pieces_per_portion is not None else 1
        writer.writerow([
            d.name or "",
            d.alias or "",
            p_val,
            pieces_val,
            avail_str,
            d.description or "",
        ])

    csv_bytes = ("\ufeff" + buf.getvalue()).encode("utf-8")
    date_tag = (datetime.utcnow() + _local_tz_offset()).strftime("%Y%m%d_%H%M%S")
    filename = f"afrc-menu-{date_tag}.csv"
    return Response(
        content=csv_bytes,
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            **NO_CACHE_HEADERS,
        },
    )

@app.post("/api/admin/menu/import")
async def import_admin_menu_csv(
    request: Request,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    current_admin = require_admin_api(request, db)

    fname = (file.filename or "").lower()
    if not fname.endswith(".csv"):
        raise HTTPException(status_code=400, detail="Only CSV files (.csv) are supported")

    content_bytes = await file.read()
    if not content_bytes:
        raise HTTPException(status_code=400, detail="Uploaded CSV file is empty")

    if len(content_bytes) > 5 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="File too large (maximum 5MB allowed)")

    text = None
    for enc in ("utf-8-sig", "utf-8", "latin-1", "cp1252"):
        try:
            text = content_bytes.decode(enc)
            break
        except (UnicodeDecodeError, LookupError):
            continue

    if text is None:
        raise HTTPException(status_code=400, detail="Unable to decode CSV file. Please upload a UTF-8 encoded CSV.")

    first_line = text.splitlines()[0] if text.splitlines() else ""
    delimiter = ";" if (";" in first_line and "," not in first_line) else ","

    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    rows = list(reader)
    if not rows:
        raise HTTPException(status_code=400, detail="CSV file contains no rows")

    raw_headers = rows[0]
    normalized_headers = [h.strip().lower() for h in raw_headers]

    col_map = {}
    for idx, h in enumerate(normalized_headers):
        if h in ("name", "dish name", "dish", "ai dish name", "item", "item name") and "name" not in col_map:
            col_map["name"] = idx
        elif h in ("alias", "customer alias", "display name") and "alias" not in col_map:
            col_map["alias"] = idx
        elif h in ("price", "price (rm)", "price rm", "rm", "cost") and "price" not in col_map:
            col_map["price"] = idx
        elif h in ("pieces", "piece", "pieces per portion", "pieces / portion", "portion", "qty", "count") and "pieces" not in col_map:
            col_map["pieces"] = idx
        elif h in ("available", "available today", "is available", "active", "status", "enabled") and "available" not in col_map:
            col_map["available"] = idx
        elif h in ("description", "desc", "ai description", "visual description", "ai visual description") and "description" not in col_map:
            col_map["description"] = idx

    if "name" not in col_map:
        raise HTTPException(
            status_code=400,
            detail="CSV is missing the 'Name' column header. Required column: Name (optional: Alias, Price, Pieces, Available, Description)."
        )

    new_dishes = []
    seen_names = set()

    for row_idx, row in enumerate(rows[1:], start=2):
        if not row or not any(field.strip() for field in row):
            continue

        def get_val(key, default=""):
            idx = col_map.get(key)
            if idx is not None and idx < len(row):
                return row[idx].strip()
            return default

        name = get_val("name")
        if not name:
            raise HTTPException(status_code=400, detail=f"Row {row_idx}: Dish Name cannot be empty")

        name_lower = name.casefold()
        if name_lower in seen_names:
            raise HTTPException(status_code=400, detail=f"Row {row_idx}: Duplicate dish name '{name}' found in CSV")
        seen_names.add(name_lower)

        alias = get_val("alias")

        price_raw = get_val("price", "0")
        price_clean = re.sub(r"[^0-9.]", "", price_raw.replace(",", "."))
        try:
            price_val = float(price_clean) if price_clean else 0.0
        except ValueError:
            raise HTTPException(status_code=400, detail=f"Row {row_idx} ('{name}'): Invalid price '{price_raw}'")

        if price_val < 0.10 or price_val > 10000:
            raise HTTPException(status_code=400, detail=f"Row {row_idx} ('{name}'): Price must be between RM 0.10 and RM 10,000")

        pieces_raw = get_val("pieces", "1")
        try:
            pieces_val = int(re.sub(r"[^0-9]", "", pieces_raw)) if pieces_raw else 1
            if pieces_val < 1:
                pieces_val = 1
            elif pieces_val > 100:
                pieces_val = 100
        except ValueError:
            pieces_val = 1

        avail_raw = get_val("available", "true").lower()
        if avail_raw in ("false", "no", "0", "n", "f", "unavailable", "off", "disable", "disabled"):
            avail_val = False
        else:
            avail_val = True

        desc_raw = get_val("description")
        if len(desc_raw) > 500:
            desc_raw = desc_raw[:500]

        dish = Dish(
            name=name,
            alias=alias,
            price=round(price_val, 2),
            available=avail_val,
            unit="portion",
            description=desc_raw,
            pieces_per_portion=pieces_val,
        )
        new_dishes.append(dish)

    if not new_dishes:
        raise HTTPException(status_code=400, detail="No valid menu items found in CSV")

    try:
        db.query(Dish).delete()
        for d in new_dishes:
            db.add(d)
        db.add(AuditLog(
            cashier_id=current_admin.id,
            admin_username=current_admin.username,
            action="MENU_IMPORT",
            details=f"Admin '{current_admin.username}' imported menu CSV overwriting with {len(new_dishes)} item(s)"
        ))
        db.commit()
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Failed to overwrite menu: {str(e)}")

    return {
        "ok": True,
        "count": len(new_dishes),
        "message": f"Successfully imported and overwrote menu with {len(new_dishes)} items."
    }

@app.get("/api/client-state")
def get_client_state(
    request: Request,
    session_id: str = "",
    client_token: str = "",
    device_id: str = "",
    browser_session_id: str = "",
    db: Session = Depends(get_db),
):
    prof = _restaurant_profile()
    _expire_stale_bills(db)
    c_token = _get_client_token(request, client_token)
    d_id = _get_device_id(request, explicit_device=device_id, explicit_client=c_token)
    b_sid = _get_scan_session_id(request, explicit_session=browser_session_id or session_id)
    effective_sid = session_id or b_sid
    rem_mins, cooldown_reason = _check_cooldown(
        request, c_token, db, session_id=effective_sid, device_id=d_id
    )
    usage = _scan_usage_status(
        request, c_token, db, session_id=effective_sid, device_id=d_id
    )
    active_bill = _find_active_bill(request, db, session_id=session_id, explicit_token=c_token)

    resp = JSONResponse(
        content={
            "client_token": c_token,
            "device_id": d_id,
            "cooldown_active": rem_mins > 0,
            "cooldown_minutes": rem_mins,
            "cooldown_reason": cooldown_reason,
            "scans_used": usage["scans_used"],
            "scans_limit": usage["scans_limit"],
            "scans_remaining": usage["scans_remaining"],
            "restaurant_name": prof["restaurant_name"],
            "company_name": prof["company_name"],
            "business_reg_number": prof.get("business_reg_number", ""),
            "company_phone": prof["company_phone"],
            "company_email": prof["company_email"],
            "restaurant_address": prof.get("restaurant_address", ""),
            "google_maps_url": prof.get("google_maps_url", ""),
            "google_maps_embed_url": prof.get("google_maps_embed_url", ""),
            "active_bill": _bill_payload(active_bill, alias_map=_dish_alias_map(db)) if active_bill else None,
            "operation_hours": {
                "start": _restaurant_settings()["operation_hours_start"],
                "end": _restaurant_settings()["operation_hours_end"],
                "is_open": _restaurant_settings()["is_open"],
                "text": _restaurant_settings()["operation_hour_text"],
                "current_server_time": _restaurant_settings()["current_server_time"],
            },
            "coming_soon": _restaurant_settings()["coming_soon"],
            "maintenance_mode": _restaurant_settings()["maintenance_mode"],
        },
        headers=NO_CACHE_HEADERS,
    )
    resp.set_cookie("afrc_client_id", c_token, max_age=86400 * 30, path="/", samesite="lax")
    resp.set_cookie("afrc_device_id", d_id, max_age=86400 * 365, path="/", samesite="lax")
    if b_sid:
        resp.set_cookie("afrc_browser_session", b_sid, max_age=86400, path="/", samesite="lax")
    if active_bill and rem_mins <= 0:
        resp.set_cookie("afrc_active_session", active_bill.id, max_age=3600, path="/", samesite="lax")
    else:
        resp.delete_cookie("afrc_active_session", path="/")
    return resp

@app.post("/api/recognize")
async def recognize(
    request: Request,
    file: UploadFile = File(...),
    cf_turnstile_response: Annotated[str, Form(alias="cf-turnstile-response")] = "",
    retake_session_id: Annotated[str, Form()] = "",
    client_token: Annotated[str, Form()] = "",
    device_id: Annotated[str, Form()] = "",
    browser_session_id: Annotated[str, Form()] = "",
    db: Session = Depends(get_db),
):
    settings = _restaurant_settings()
    if settings["maintenance_mode"]:
        raise HTTPException(
            status_code=503,
            detail="System is currently under maintenance. Please try again later."
        )
    if settings["coming_soon"]:
        raise HTTPException(
            status_code=503,
            detail="System is not yet launched. Coming soon!"
        )
    if not settings["is_open"]:
        op_text = settings["operation_hour_text"]
        raise HTTPException(
            status_code=403,
            detail=f"You are outside our operation hour. Our operation hour is {op_text} please comeback later."
        )

    await asyncio.to_thread(_require_turnstile, cf_turnstile_response, "recognize", request)
    _expire_stale_bills(db)

    c_token = _get_client_token(request, client_token)
    d_id = _get_device_id(request, explicit_device=device_id, explicit_client=c_token)
    retake_sid = retake_session_id.strip()
    b_sid = _get_scan_session_id(request, explicit_session=browser_session_id or retake_sid)
    effective_sid = retake_sid or b_sid

    rem_mins, _ = _check_cooldown(
        request, c_token, db, session_id=effective_sid, device_id=d_id
    )
    if rem_mins > 0:
        raise HTTPException(status_code=429, detail=ErrorResponse(
            error_code="RATE_LIMIT",
            message=f"Your rate limit hit, try again after {rem_mins}min."
        ).dict())

    existing_open = _find_active_bill(request, db, session_id=retake_sid, explicit_token=c_token)

    target_bill = None
    if existing_open:
        if existing_open.status == "retaking" or (retake_sid and existing_open.id == retake_sid and existing_open.status == "retaking"):
            target_bill = existing_open
        else:
            raise HTTPException(status_code=409, detail={
                "error_code": "PENDING_BILL",
                "message": f"You have a pending bill (#{existing_open.bill_number}). Please pay your pending bill first.",
                "session_id": existing_open.id,
                "bill_number": existing_open.bill_number,
            })

    cfg = _rate_limit_config()
    if target_bill and (target_bill.retake_count or 0) >= cfg["max_retakes"]:
        target_bill.status = "void"
        target_bill.void_reason = f"Exceeded {cfg['max_retakes']} Retries Limit"
        target_bill.updated_at = datetime.utcnow()
        db.commit()
        _set_cooldown(
            request,
            c_token or target_bill.client_token or "",
            db,
            session_id=target_bill.id,
            device_id=d_id,
            reason=f"Exceeded {cfg['max_retakes']} photo retries limit.",
        )
        raise HTTPException(status_code=429, detail=ErrorResponse(
            error_code="RATE_LIMIT",
            message=f"Your rate limit hit, try again after {cfg['cooldown_minutes']}min."
        ).dict())

    if not file.content_type or not file.content_type.startswith("image/"):
        raise HTTPException(status_code=400, detail=ErrorResponse(
            error_code="INVALID_IMAGE",
            message="Please capture a photo using the camera. Gallery uploads are not allowed."
        ).dict())

    try:
        image_bytes = await file.read()
        if len(image_bytes) < 1000:
            raise HTTPException(status_code=400, detail=ErrorResponse(
                error_code="UPLOAD_FAILED",
                message="Photo is too small or empty. Please retake."
            ).dict())
        if len(image_bytes) > 1 * 1024 * 1024:
            raise HTTPException(status_code=413, detail=ErrorResponse(
                error_code="IMAGE_TOO_LARGE",
                message="Photo exceeds 1MB limit. Please retake using camera."
            ).dict())
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=400, detail=ErrorResponse(
            error_code="UPLOAD_FAILED",
            message="Failed to receive photo. Please try again."
        ).dict())

    _record_and_enforce_scan_limit(
        request,
        c_token,
        db,
        session_id=effective_sid,
        device_id=d_id,
    )

    target_bill_id = target_bill.id if target_bill else ""
    is_retry_scan = bool(target_bill or retake_sid)
    scan_bill_no = (target_bill.bill_number or "") if target_bill else ""
    if not scan_bill_no and retake_sid:
        b_prev = db.query(SessionBill.bill_number).filter(SessionBill.id == retake_sid).first()
        if b_prev and b_prev[0]:
            scan_bill_no = b_prev[0]
    scan_sid_val = target_bill_id or retake_sid or effective_sid
    client_ip_val = _client_ip(request)

    available = [_dish_json(d) for d in db.query(Dish).filter(Dish.available == True).all()]
    # The menu is now a plain list. End the read so Grok runs without holding a transaction.
    db.rollback()

    try:
        ai_items = await recognize_food(image_bytes, available)
    except RecognitionError as e:
        await _record_failed_scan(
            db=db,
            image_bytes=image_bytes,
            rejection_code=e.code,
            rejection_message=e.message,
            raw_ai=getattr(e, "raw_response", ""),
            session_id=scan_sid_val,
            bill_number=scan_bill_no,
            is_retry=is_retry_scan,
            ip=client_ip_val,
            device_id=d_id,
        )
        raise HTTPException(status_code=422, detail=ErrorResponse(
            error_code=e.code,
            message=e.message,
            retake_required=True
        ).dict())
    except Exception as e:
        await _record_failed_scan(
            db=db,
            image_bytes=image_bytes,
            rejection_code="AI_SERVICE_ERROR",
            rejection_message="AI service is temporarily down. Please try again in a moment.",
            raw_ai=str(e)[:500],
            session_id=scan_sid_val,
            bill_number=scan_bill_no,
            is_retry=is_retry_scan,
            ip=client_ip_val,
            device_id=d_id,
        )
        raise HTTPException(status_code=503, detail=ErrorResponse(
            error_code="AI_SERVICE_ERROR",
            message="AI service is temporarily down. Please try again in a moment."
        ).dict())

    if not ai_items:
        await _record_failed_scan(
            db=db,
            image_bytes=image_bytes,
            rejection_code="NO_FOOD_DETECTED",
            rejection_message="No matching dishes found on the plate. Please retake or check today's menu.",
            raw_ai="",
            session_id=scan_sid_val,
            bill_number=scan_bill_no,
            is_retry=is_retry_scan,
            ip=client_ip_val,
            device_id=d_id,
        )
        raise HTTPException(status_code=422, detail=ErrorResponse(
            error_code="NO_FOOD_DETECTED",
            message="No matching dishes found on the plate. Please retake or check today's menu."
        ).dict())

    # Backend pricing - never trust AI for prices
    bill_items = []
    total = 0.0
    dishes = db.query(Dish).all()
    dish_map = {d.name: d for d in dishes}
    dish_map.update({" ".join(d.name.strip().casefold().split()): d for d in dishes})

    for item in ai_items:
        dish = dish_map.get(item.name) or dish_map.get(" ".join(item.name.strip().casefold().split()))
        if not dish or not dish.available:
            continue
        billed_qty, piece_count = _bill_quantity(item.quantity, dish.pieces_per_portion)
        if billed_qty < 1:
            continue
        sub = dish.price * billed_qty
        total += sub
        display_name = (dish.alias or "").strip() or dish.name
        bill_items.append(BillItem(
            name=display_name,
            quantity=billed_qty,
            unit_price=dish.price,
            subtotal=round(sub, 2),
            piece_count=piece_count
        ))

    if not bill_items:
        await _record_failed_scan(
            db=db,
            image_bytes=image_bytes,
            rejection_code="NO_MATCHING_DISHES",
            rejection_message="Recognized items are not on today's menu. Please retake or ask staff.",
            raw_ai=json.dumps([i.dict() for i in ai_items]),
            session_id=scan_sid_val,
            bill_number=scan_bill_no,
            is_retry=is_retry_scan,
            ip=client_ip_val,
            device_id=d_id,
        )
        raise HTTPException(status_code=422, detail=ErrorResponse(
            error_code="NO_MATCHING_DISHES",
            message="Recognized items are not on today's menu. Please retake or ask staff."
        ).dict())

    # Re-fetch target_bill after rollback if this is a retake (Option B: reuse same bill)
    target_bill = None
    if target_bill_id:
        target_bill = db.query(SessionBill).filter(SessionBill.id == target_bill_id).first()
        if target_bill and target_bill.status in ("paid", "void", "cancelled"):
            target_bill = None

    if target_bill:
        session_id = target_bill.id
        bill_number = target_bill.bill_number or next_bill_number(db)
        r2_key = f"plates/{bill_number}_{session_id}.jpg"
        try:
            UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
            (UPLOAD_DIR / f"{session_id}.jpg").write_bytes(image_bytes)
        except OSError:
            pass
        uploaded_to_r2 = await _upload_plate_to_r2(r2_key, image_bytes)
        if not uploaded_to_r2:
            r2_key = target_bill.image_key or f"plates/{bill_number}_{session_id}.jpg"
        target_bill.bill_number = bill_number
        target_bill.image_key = r2_key
        target_bill.raw_ai = json.dumps([i.dict() for i in ai_items])
        target_bill.final_bill = json.dumps([b.dict() for b in bill_items])
        target_bill.retake_count = (target_bill.retake_count or 0) + 1
        target_bill.status = "open"
        target_bill.curlec_order_id = ""
        target_bill.updated_at = datetime.utcnow()
        if c_token:
            target_bill.client_token = c_token
        db.commit()
    else:
        session_id = str(uuid.uuid4())
        bill_number = next_bill_number(db)
        r2_key = f"plates/{bill_number}_{session_id}.jpg"
        try:
            UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
            (UPLOAD_DIR / f"{session_id}.jpg").write_bytes(image_bytes)
        except OSError:
            pass
        uploaded_to_r2 = await _upload_plate_to_r2(r2_key, image_bytes)
        if not uploaded_to_r2:
            r2_key = f"plates/{bill_number}_{session_id}.jpg"
        session = SessionBill(
            id=session_id,
            bill_number=bill_number,
            image_key=r2_key,
            raw_ai=json.dumps([i.dict() for i in ai_items]),
            final_bill=json.dumps([b.dict() for b in bill_items]),
            retake_count=0,
            client_token=c_token,
            updated_at=datetime.utcnow(),
        )
        db.add(session)
        db.commit()

    payload = BillResponse(
        items=bill_items,
        total=round(total, 2),
        session_id=session_id,
        bill_number=bill_number,
    ).dict()
    resp = JSONResponse(content=payload, headers=NO_CACHE_HEADERS)
    resp.set_cookie("afrc_client_id", c_token, max_age=86400 * 30, path="/", samesite="lax")
    resp.set_cookie("afrc_device_id", d_id, max_age=86400 * 365, path="/", samesite="lax")
    if b_sid:
        resp.set_cookie("afrc_browser_session", b_sid, max_age=86400, path="/", samesite="lax")
    resp.set_cookie("afrc_active_session", session_id, max_age=3600, path="/", samesite="lax")
    return resp

def _bill_payload(session: SessionBill, refunded_amount: float = 0.0, is_owner_or_admin: bool = True, alias_map: dict = None) -> dict:
    prof = _restaurant_profile()
    try:
        items = json.loads(session.final_bill or "[]")
    except Exception:
        items = []
    if alias_map is None:
        alias_map = _dish_alias_map()
    if alias_map:
        for item in items:
            raw_n = str(item.get("name") or "").strip()
            if raw_n in alias_map:
                item["name"] = alias_map[raw_n]
            elif raw_n.casefold() in alias_map:
                item["name"] = alias_map[raw_n.casefold()]
    total = round(sum(float(item.get("subtotal") or 0) for item in items), 2)
    img_path = UPLOAD_DIR / f"{session.id}.jpg"
    retakes_used = int(session.retake_count or 0)
    bill_no_upper = (session.bill_number or "").upper()
    has_r2 = (
        bool(session.image_key)
        or (session.id in _R2_SESSION_KEYS)
        or (bill_no_upper in _R2_BILL_KEYS)
    )
    status = session.status or "open"
    void_reason = session.void_reason or ""
    max_retakes = _rate_limit_config()["max_retakes"]
    if status in ("void", "cancelled") and not void_reason:
        void_reason = (
            f"Exceeded {max_retakes} Retries Limit"
            if retakes_used >= max_retakes
            else "Bill timed out"
        )
    email_val = session.receipt_email or ""
    phone_val = session.receipt_phone or ""
    if not is_owner_or_admin:
        email_val = _mask_email(email_val)
        phone_val = _mask_phone(phone_val)
    return {
        "session_id": session.id,
        "bill_number": session.bill_number or "AAA-0001",
        "items": items,
        "total": total,
        "status": status,
        "void_reason": void_reason,
        "retake_count": retakes_used,
        "retakes_left": max(0, max_retakes - retakes_used),
        "max_retakes": max_retakes,
        "receipt_email": email_val,
        "receipt_phone": phone_val,
        "curlec_order_id": session.curlec_order_id or "",
        "curlec_payment_id": session.curlec_payment_id or "",
        "restaurant_name": prof["restaurant_name"],
        "company_name": prof["company_name"],
        "company_phone": prof["company_phone"],
        "company_email": prof["company_email"],
        "refunded_amount": round(float(refunded_amount or 0.0), 2),
        "created_at": (session.created_at.isoformat() + "Z") if session.created_at else "",
        "updated_at": (session.updated_at.isoformat() + "Z") if session.updated_at else "",
        "paid_at": (session.paid_at.isoformat() + "Z") if session.paid_at else "",
        "has_image": has_r2 or img_path.is_file(),
    }

@app.get("/api/sessions/{session_id}")
def get_session(session_id: str, request: Request, db: Session = Depends(get_db)):
    _expire_stale_bills(db)
    bill = db.query(SessionBill).filter(SessionBill.id == session_id).first()
    if not bill:
        raise HTTPException(status_code=404, detail="Bill not found")
    _verify_bill_ownership(bill, request, db)
    c_token = _get_client_token(request)
    changed = False
    if bill.status == "retaking":
        bill.status = "open"
        changed = True
    if bill.status in ("open", "pending") and not bill.client_token and c_token:
        bill.client_token = c_token
        changed = True
    if changed:
        db.commit()
    resp = JSONResponse(content=_bill_payload(bill, alias_map=_dish_alias_map(db)), headers=NO_CACHE_HEADERS)
    resp.set_cookie("afrc_client_id", c_token, max_age=86400 * 30, path="/", samesite="lax")
    if bill.status in ("open", "pending"):
        resp.set_cookie("afrc_active_session", bill.id, max_age=3600, path="/", samesite="lax")
    elif bill.status in ("paid", "void", "cancelled"):
        resp.delete_cookie("afrc_active_session", path="/")
    return resp

@app.post("/api/sessions/{session_id}/start-retake")
def start_retake(session_id: str, request: Request, payload: dict = None, db: Session = Depends(get_db)):
    _expire_stale_bills(db)
    bill = db.query(SessionBill).filter(SessionBill.id == session_id).first()
    if not bill:
        raise HTTPException(status_code=404, detail="Bill not found")
    if bill.status == "paid":
        raise HTTPException(status_code=409, detail="This bill is already paid")
    if bill.status in ("void", "cancelled"):
        raise HTTPException(status_code=409, detail="This bill has been voided. Please pay at the counter.")
    _verify_bill_ownership(bill, request, db, (payload or {}).get("client_token", ""))
    max_retakes = _rate_limit_config()["max_retakes"]
    if (bill.retake_count or 0) >= max_retakes:
        raise HTTPException(status_code=409, detail="No retries remaining")
    c_token = _get_client_token(request, (payload or {}).get("client_token", ""))
    bill.status = "retaking"
    bill.updated_at = datetime.utcnow()
    if c_token and not bill.client_token:
        bill.client_token = c_token
    db.commit()
    resp = JSONResponse(
        content={
            "status": "retaking",
            "session_id": bill.id,
            "bill_number": bill.bill_number,
            "retakes_left": max(0, max_retakes - (bill.retake_count or 0)),
        },
        headers=NO_CACHE_HEADERS,
    )
    resp.set_cookie("afrc_client_id", c_token, max_age=86400 * 30, path="/", samesite="lax")
    resp.set_cookie("afrc_active_session", bill.id, max_age=3600, path="/", samesite="lax")
    return resp

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

@app.get("/api/sessions/{session_id}/image")
async def get_session_image(session_id: str, request: Request, db: Session = Depends(get_db)):
    bill = db.query(SessionBill).filter(SessionBill.id == session_id).first()
    if not bill:
        raise HTTPException(status_code=404, detail="Bill not found")
    _verify_bill_ownership(bill, request, db)
    meta = await asyncio.to_thread(_get_bill_image_meta, session_id)
    if not meta:
        raise HTTPException(status_code=404, detail="Bill not found")
    img_path = UPLOAD_DIR / f"{meta['id']}.jpg"
    if img_path.is_file() and img_path.stat().st_size > 0:
        return FileResponse(img_path, media_type="image/jpeg", headers={"Cache-Control": "no-cache"})

    now_mono = asyncio.get_running_loop().time()
    miss_until = _R2_MISSING_UNTIL.get(meta["id"], 0.0)
    if miss_until > now_mono:
        raise HTTPException(status_code=404, detail="Image not found")

    bill_no_upper = meta["bill_number"].upper()
    candidate_keys = []
    for k in (
        meta["image_key"],
        _R2_SESSION_KEYS.get(meta["id"]),
        f"plates/{meta['bill_number']}_{meta['id']}.jpg" if meta["bill_number"] else "",
        _R2_BILL_KEYS.get(bill_no_upper),
    ):
        if k and k not in candidate_keys:
            candidate_keys.append(k)

    for key in candidate_keys:
        r2_bytes = await _fetch_plate_from_r2(key)
        if r2_bytes:
            _R2_MISSING_UNTIL.pop(meta["id"], None)
            try:
                UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
                img_path.write_bytes(r2_bytes)
            except OSError:
                pass
            if not meta["image_key"]:
                await asyncio.to_thread(_save_bill_image_key, meta["id"], key)
            return Response(
                content=r2_bytes,
                media_type="image/jpeg",
                headers={"Cache-Control": "no-cache"},
            )
    _R2_MISSING_UNTIL[meta["id"]] = now_mono + _R2_MISSING_TTL_SEC
    raise HTTPException(status_code=404, detail="Image not found")

@app.post("/api/sessions/{session_id}/need-cashier")
def need_cashier(session_id: str, request: Request, db: Session = Depends(get_db)):
    bill = db.query(SessionBill).filter(SessionBill.id == session_id).first()
    if not bill:
        raise HTTPException(status_code=404, detail="Bill not found")
    if bill.status == "paid":
        raise HTTPException(status_code=409, detail="This bill is already paid")
    _verify_bill_ownership(bill, request, db)
    bill.status = "void"
    bill.void_reason = "Needed Cashier Assistance"
    bill.updated_at = datetime.utcnow()
    db.commit()
    resp = JSONResponse(
        content={
            "status": "void",
            "void_reason": bill.void_reason,
            "bill_number": bill.bill_number,
            "message": f"Bill #{bill.bill_number} has been voided. Please head to the cashier counter for assistance.",
        },
        headers=NO_CACHE_HEADERS,
    )
    resp.delete_cookie("afrc_active_session", path="/")
    return resp

@app.post("/api/sessions/{session_id}/cancel")
def cancel_bill(session_id: str, request: Request, db: Session = Depends(get_db)):
    bill = db.query(SessionBill).filter(SessionBill.id == session_id).first()
    if not bill:
        raise HTTPException(status_code=404, detail="Bill not found")
    if bill.status == "paid":
        raise HTTPException(status_code=409, detail="This bill is already paid")
    _verify_bill_ownership(bill, request, db)
    bill.status = "void"
    bill.void_reason = "Customer Cancelled"
    bill.updated_at = datetime.utcnow()
    db.commit()
    resp = JSONResponse(
        content={
            "status": "void",
            "void_reason": bill.void_reason,
            "bill_number": bill.bill_number,
            "message": f"Bill #{bill.bill_number} has been cancelled and voided.",
        },
        headers=NO_CACHE_HEADERS,
    )
    resp.delete_cookie("afrc_active_session", path="/")
    return resp

@app.post("/api/sessions/{session_id}/void-retake")
def void_retake(session_id: str, request: Request, payload: dict = None, db: Session = Depends(get_db)):
    bill = db.query(SessionBill).filter(SessionBill.id == session_id).first()
    if not bill:
        raise HTTPException(status_code=404, detail="Bill not found")
    if bill.status == "paid":
        raise HTTPException(status_code=409, detail="This bill is already paid")
    _verify_bill_ownership(bill, request, db, (payload or {}).get("client_token", ""))
    cfg = _rate_limit_config()
    body = payload or {}
    c_token = _get_client_token(request, body.get("client_token", "")) or bill.client_token or ""
    d_id = _get_device_id(request, explicit_device=body.get("device_id", ""), explicit_client=c_token)
    bill.status = "void"
    bill.void_reason = f"Exceeded {cfg['max_retakes']} Retries Limit"
    bill.updated_at = datetime.utcnow()
    db.commit()
    _set_cooldown(
        request,
        c_token,
        db,
        session_id=session_id,
        device_id=d_id,
        reason=f"Exceeded {cfg['max_retakes']} photo retries limit.",
    )
    resp = JSONResponse(
        content={
            "status": "void",
            "void_reason": bill.void_reason,
            "cooldown_minutes": cfg["cooldown_minutes"],
            "message": f"Your rate limit hit, try again after {cfg['cooldown_minutes']}min.",
        },
        headers=NO_CACHE_HEADERS,
    )
    resp.delete_cookie("afrc_active_session", path="/")
    return resp

def _build_maps_embed_url(maps_url: str, rest_name: str) -> str:
    url = (maps_url or "").strip()
    if not url:
        return f"https://maps.google.com/maps?q={rest_name}&output=embed" if rest_name else ""
    if "<iframe" in url.lower():
        m = re.search(r'src=["\']([^"\']+)["\']', url, re.IGNORECASE)
        if m:
            return m.group(1)
    if "maps/embed" in url or "pb=" in url:
        return url
    if "Zemkra7AmRtoGj6Z9" in url or "TakCakap" in rest_name or "Warisan Ibunda" in rest_name:
        return "https://www.google.com/maps/embed?pb=!1m18!1m12!1m3!1d87976.50620304717!2d100.38085804863282!3d5.325134800000003!2m3!1f0!2f0!3f0!3m2!1i1024!2i768!4f13.1!3m3!1m2!1s0x304ac814ae9d8007%3A0x4ba659682bb01a8d!2sTakCakap%20Nasi%20Campur%20(De%20Warisan%20Ibunda)!5e1!3m2!1sen!2smy!4v1794758235590!5m2!1sen!2smy"
    return f"https://maps.google.com/maps?q={rest_name}&output=embed"

def _restaurant_profile() -> dict:
    merged = {
        **_read_env_file_cached(ENV_DIR / ".env"),
        **_read_env_file_cached(ROOT / ".env"),
        **_read_env_file_cached(ENV_DIR / "restaurant.env"),
        **_read_env_file_cached(ROOT / "restaurant.env"),
    }
    rest_name = (
        merged.get("RESTAURANT_NAME")
        or merged.get("STORE_NAME")
        or os.getenv("RESTAURANT_NAME")
        or os.getenv("STORE_NAME")
        or "AFRC"
    ).strip() or "AFRC"
    comp_name = (
        merged.get("COMPANY_NAME")
        or os.getenv("COMPANY_NAME")
        or ""
    ).strip()
    bus_reg = (
        merged.get("BUSINESS_REG_NUMBER")
        or merged.get("COMPANY_REG_NUMBER")
        or os.getenv("BUSINESS_REG_NUMBER")
        or os.getenv("COMPANY_REG_NUMBER")
        or ""
    ).strip()
    comp_phone = (
        merged.get("COMPANY_PHONE")
        or os.getenv("COMPANY_PHONE")
        or ""
    ).strip()
    comp_email = (
        merged.get("COMPANY_EMAIL")
        or os.getenv("COMPANY_EMAIL")
        or ""
    ).strip()
    rest_addr = (
        merged.get("RESTAURANT_ADDRESS")
        or os.getenv("RESTAURANT_ADDRESS")
        or ""
    ).strip()
    g_url = (
        merged.get("GOOGLE_MAPS_URL")
        or os.getenv("GOOGLE_MAPS_URL")
        or (f"https://maps.google.com/?q={rest_name}" if rest_name else "https://maps.google.com")
    ).strip()
    g_embed = _build_maps_embed_url(g_url, rest_name)
    return {
        "restaurant_name": rest_name,
        "company_name": comp_name,
        "business_reg_number": bus_reg,
        "company_phone": comp_phone,
        "company_email": comp_email,
        "restaurant_address": rest_addr,
        "google_maps_url": g_url,
        "google_maps_embed_url": g_embed,
    }

def _restaurant_name() -> str:
    return _restaurant_profile()["restaurant_name"]

def _is_within_hours(current_str: str, start_str: str, end_str: str) -> bool:
    try:
        cur_h, cur_m = map(int, current_str.split(":"))
        st_h, st_m = map(int, start_str.split(":"))
        en_h, en_m = map(int, end_str.split(":"))
        cur = cur_h * 60 + cur_m
        st = st_h * 60 + st_m
        en = en_h * 60 + en_m
        if st <= en:
            return st <= cur <= en
        else:
            return cur >= st or cur <= en
    except Exception:
        return True

def _restaurant_settings() -> dict:
    env_data = {
        **_read_env_file_cached(ENV_DIR / ".env"),
        **_read_env_file_cached(ROOT / ".env"),
        **_read_env_file_cached(ENV_DIR / "restaurant.env"),
        **_read_env_file_cached(ROOT / "restaurant.env"),
    }
    coming_soon = env_data.get("COMING_SOON", "false").strip().lower() in ("true", "1", "yes")
    maintenance = env_data.get("MAINTENANCE_MODE", "false").strip().lower() in ("true", "1", "yes")
    op_start = env_data.get("OPERATION_HOURS_START", "08:00").strip() or "08:00"
    op_end = env_data.get("OPERATION_HOURS_END", "17:00").strip() or "17:00"
    
    now_local = datetime.utcnow() + _local_tz_offset()
    current_time_str = now_local.strftime("%H:%M")
    is_open = _is_within_hours(current_time_str, op_start, op_end)
    op_text = f"{op_start} - {op_end}"
    
    return {
        "coming_soon": coming_soon,
        "maintenance_mode": maintenance,
        "operation_hours_start": op_start,
        "operation_hours_end": op_end,
        "is_open": is_open,
        "operation_hour_text": op_text,
        "current_server_time": current_time_str,
    }

def _save_restaurant_settings(updates: dict) -> dict:
    env_path = ENV_DIR / "restaurant.env"
    existing_lines = []
    if env_path.exists():
        with open(env_path, "r", encoding="utf-8") as f:
            existing_lines = f.readlines()
            
    keys_map = {}
    if "coming_soon" in updates:
        keys_map["COMING_SOON"] = "true" if updates["coming_soon"] else "false"
    if "maintenance_mode" in updates:
        keys_map["MAINTENANCE_MODE"] = "true" if updates["maintenance_mode"] else "false"
    if "operation_hours_start" in updates and updates["operation_hours_start"]:
        keys_map["OPERATION_HOURS_START"] = str(updates["operation_hours_start"]).strip()
    if "operation_hours_end" in updates and updates["operation_hours_end"]:
        keys_map["OPERATION_HOURS_END"] = str(updates["operation_hours_end"]).strip()
        
    new_lines = []
    handled_keys = set()
    for line in existing_lines:
        s_line = line.strip()
        if s_line and not s_line.startswith("#") and "=" in s_line:
            k, _ = s_line.split("=", 1)
            k = k.strip()
            if k in keys_map:
                new_lines.append(f"{k}={keys_map[k]}\n")
                handled_keys.add(k)
                continue
        new_lines.append(line)
        
    for k, v in keys_map.items():
        if k not in handled_keys:
            if new_lines and not new_lines[-1].endswith("\n"):
                new_lines.append("\n")
            new_lines.append(f"{k}={v}\n")
            
    with open(env_path, "w", encoding="utf-8") as f:
        f.writelines(new_lines)
        
    _RL_FILE_CACHE.pop(str(env_path), None)
    return _restaurant_settings()


def _curlec_client():
    key_id = os.getenv("CURLEC_KEY_ID", "").strip()
    key_secret = os.getenv("CURLEC_KEY_SECRET", "").strip()
    if not key_id or not key_secret:
        raise HTTPException(status_code=503, detail="Payment gateway is not configured")
    return razorpay.Client(auth=(key_id, key_secret)), key_id

def _amount_sen(bill: SessionBill) -> int:
    return int(round(float(_bill_payload(bill)["total"]) * 100))

def _checkout_payload(bill: SessionBill, key_id: str) -> dict:
    prof = _restaurant_profile()
    return {
        "key_id": key_id,
        "order_id": bill.curlec_order_id,
        "amount": _amount_sen(bill),
        "currency": "MYR",
        "session_id": bill.id,
        "restaurant_name": prof["restaurant_name"],
        "company_name": prof["company_name"],
        "company_phone": prof["company_phone"],
        "company_email": prof["company_email"],
    }

def _create_curlec_order(bill: SessionBill, db: Session) -> dict:
    client, key_id = _curlec_client()
    amount = _amount_sen(bill)
    if amount < 10:
        raise HTTPException(
            status_code=400,
            detail=f"Payment amount (RM {amount / 100:.2f}) is below the minimum allowed payment of RM 0.10. Please adjust dish price or items.",
        )
    if bill.curlec_order_id and bill.status == "pending":
        try:
            existing = client.order.fetch(bill.curlec_order_id)
        except Exception:
            existing = None
        if (
            existing
            and int(existing.get("amount") or 0) == amount
            and existing.get("currency") == "MYR"
            and existing.get("status") in ("created", "attempted")
        ):
            return _checkout_payload(bill, key_id)
    try:
        order = client.order.create({
            "amount": amount,
            "currency": "MYR",
            "receipt": (bill.bill_number or bill.id)[:40],
            "notes": {"session_id": bill.id, "bill_number": bill.bill_number or ""},
        })
    except Exception as e:
        logger.exception(f"Curlec order creation failed for bill {bill.id} (amount {amount} sen): {e}")
        err_str = str(e)
        if "minimum amount" in err_str.lower():
            raise HTTPException(
                status_code=400,
                detail=f"Payment amount (RM {amount / 100:.2f}) is below the minimum allowed payment of RM 0.10.",
            )
        raise HTTPException(status_code=502, detail=f"Could not start the payment: {err_str[:120]}")
    bill.curlec_order_id = order["id"]
    bill.status = "pending"
    db.commit()
    return _checkout_payload(bill, key_id)

def _record_captured_payment(bill: SessionBill, payment: dict, db: Session) -> bool:
    if (
        str(payment.get("status")) != "captured"
        or str(payment.get("currency")) != "MYR"
        or int(payment.get("amount") or 0) != _amount_sen(bill)
        or str(payment.get("order_id") or "") != (bill.curlec_order_id or "")
    ):
        return False
    bill.curlec_payment_id = str(payment.get("id") or "")
    contact = str(payment.get("contact") or "").strip()
    if contact:
        bill.receipt_phone = contact
    p_email = str(payment.get("email") or "").strip()
    if p_email and not (bill.receipt_email or "").strip():
        bill.receipt_email = p_email
    bill.status = "paid"
    bill.paid_at = datetime.utcnow()
    db.commit()
    return True

def _verify_and_capture(order_id: str, payment_id: str, signature: str, db: Session):
    order_id = str(order_id or "").strip()
    payment_id = str(payment_id or "").strip()
    signature = str(signature or "").strip()
    bill = db.query(SessionBill).filter(SessionBill.curlec_order_id == order_id).first()
    if not bill or order_id != (bill.curlec_order_id or ""):
        return None, "unpaid"
    if bill.status == "paid":
        return bill, "paid"
    client, _key_id = _curlec_client()
    try:
        client.utility.verify_payment_signature({
            "razorpay_order_id": bill.curlec_order_id,
            "razorpay_payment_id": payment_id,
            "razorpay_signature": signature,
        })
        payment = client.payment.fetch(payment_id)
    except Exception:
        return bill, "unpaid"
    if _record_captured_payment(bill, payment, db):
        return bill, "paid"
    return bill, "unpaid"

def _normalize_my_phone(raw_phone: str) -> str:
    raw = str(raw_phone or "").strip()
    if not raw:
        raise HTTPException(status_code=400, detail="Phone number is required")
    digits = "".join(ch for ch in raw if ch.isdigit())
    if digits.startswith("60"):
        digits = digits[2:]
    elif digits.startswith("0"):
        digits = digits[1:]
    if len(digits) < 9 or len(digits) > 10:
        raise HTTPException(
            status_code=400,
            detail="Enter a valid Malaysian phone number (9 to 10 digits after +60)",
        )
    return f"+60{digits}"

@app.post("/api/pay")
def pay(payload: dict, request: Request, db: Session = Depends(get_db)):
    _require_turnstile(str(payload.get("cf-turnstile-response") or ""), "pay", request)
    _expire_stale_bills(db)
    session_id = str(payload.get("session_id") or "").strip()
    bill = db.query(SessionBill).filter(SessionBill.id == session_id).first()
    if not bill:
        raise HTTPException(status_code=404, detail="Bill not found")
    if bill.status in ("void", "cancelled"):
        raise HTTPException(status_code=409, detail="This bill has been voided. Please pay at the counter.")
    if bill.status == "needs_cashier":
        raise HTTPException(status_code=409, detail="A cashier is checking this bill")
    _verify_bill_ownership(bill, request, db, (payload or {}).get("client_token", ""))
    if bill.status == "paid":
        return {"status": "paid", "session_id": bill.id}
    phone = _normalize_my_phone(payload.get("phone") or "")
    email = str(payload.get("email") or "").strip()
    if email:
        if email[0] in ("=", "+", "-", "@"):
            raise HTTPException(status_code=400, detail="Invalid email format")
        import re
        if not re.match(r"^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+$", email) or len(email) > 100:
            raise HTTPException(status_code=400, detail="Enter a valid email address or leave it blank")
    bill.receipt_phone = phone
    bill.receipt_email = email
    bill.updated_at = datetime.utcnow()
    db.commit()
    checkout = _create_curlec_order(bill, db)
    checkout["status"] = "pending"
    return checkout

@app.post("/api/pay/confirm")
def pay_confirm(payload: dict, db: Session = Depends(get_db)):
    bill, state = _verify_and_capture(
        payload.get("razorpay_order_id"),
        payload.get("razorpay_payment_id"),
        payload.get("razorpay_signature"),
        db,
    )
    if state != "paid" or not bill:
        raise HTTPException(status_code=402, detail="Payment was not completed")
    return {"status": "paid", "session_id": bill.id}

@app.post("/api/pay/callback")
async def pay_callback(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    bill, state = _verify_and_capture(
        form.get("razorpay_order_id"),
        form.get("razorpay_payment_id"),
        form.get("razorpay_signature"),
        db,
    )
    session_id = bill.id if bill else ""
    if state == "paid" and session_id:
        return RedirectResponse(f"/payment/success?session={quote(session_id)}", status_code=303)
    target = "/pay?payment=failed"
    if session_id:
        target = f"/pay?session={quote(session_id)}&payment=failed"
    return RedirectResponse(target, status_code=303)

@app.post("/api/curlec/webhook")
async def curlec_webhook(request: Request, db: Session = Depends(get_db)):
    body = await request.body()
    signature = request.headers.get("x-razorpay-signature", "")
    webhook_secret = os.getenv("CURLEC_WEBHOOK_SECRET", "").strip()
    client, _key_id = _curlec_client()
    try:
        event = json.loads(body)
    except Exception:
        raise HTTPException(status_code=400, detail="invalid payload")

    verified_by_sig = False
    if webhook_secret and not webhook_secret.startswith("http"):
        try:
            client.utility.verify_webhook_signature(body.decode("utf-8"), signature, webhook_secret)
            verified_by_sig = True
        except Exception:
            verified_by_sig = False

    if not verified_by_sig:
        raise HTTPException(
            status_code=400,
            detail="Invalid webhook signature"
        )

    if event.get("event") in ("payment.captured", "order.paid"):
        entity = ((event.get("payload") or {}).get("payment") or {}).get("entity") or {}
        order_id = str(entity.get("order_id") or "").strip()
        payment = entity
        bill = db.query(SessionBill).filter(SessionBill.curlec_order_id == order_id).first()
        if bill and bill.status != "paid" and str(payment.get("status")) == "captured":
            _record_captured_payment(bill, payment, db)
    return {"status": "ok"}


def _local_tz_offset() -> timedelta:
    try:
        hours = float(os.getenv("TZ_OFFSET_HOURS", "8"))
    except (TypeError, ValueError):
        hours = 8.0
    return timedelta(hours=hours)

def _shift_year_month(year: int, month: int, delta_months: int) -> tuple[int, int]:
    total = year * 12 + (month - 1) + delta_months
    y, m_idx = divmod(total, 12)
    return y, m_idx + 1

def _compute_overview_data(
    db: Session,
    days: int = 7,
    period: str = "",
    month: str = "",
    quarter: str = "",
    include_export_bills: bool = False,
) -> dict:
    offset = _local_tz_offset()
    now_local = datetime.utcnow() + offset
    today_date = now_local.date()
    week_start_date = today_date - timedelta(days=6)
    max_retakes = _rate_limit_config()["max_retakes"]

    rows = (
        db.query(
            SessionBill.id,
            SessionBill.bill_number,
            SessionBill.status,
            SessionBill.void_reason,
            SessionBill.final_bill,
            SessionBill.receipt_email,
            SessionBill.receipt_phone,
            SessionBill.curlec_payment_id,
            SessionBill.retake_count,
            SessionBill.created_at,
            SessionBill.updated_at,
            SessionBill.paid_at,
        )
        .order_by(SessionBill.created_at.desc())
        .all()
    )

    try:
        days_int = int(days)
    except (TypeError, ValueError):
        days_int = 7

    p_norm = (period or "").strip().lower()
    if p_norm not in ("today", "7d", "30d", "monthly", "quarterly", "all"):
        if days_int == 1:
            p_norm = "today"
        elif days_int == 30:
            p_norm = "30d"
        elif days_int == 0:
            p_norm = "all"
        else:
            p_norm = "7d"

    month_str = (month or "").strip()
    quarter_str = (quarter or "").strip().upper()

    # Available months (last 24) and quarters (last 8) for selectors
    available_months = []
    for delta in range(0, -24, -1):
        ym_y, ym_m = _shift_year_month(today_date.year, today_date.month, delta)
        available_months.append({
            "value": f"{ym_y:04d}-{ym_m:02d}",
            "label": date(ym_y, ym_m, 1).strftime("%B %Y"),
        })

    cur_q = (today_date.month - 1) // 3 + 1
    available_quarters = []
    q_month_span = {1: "Jan-Mar", 2: "Apr-Jun", 3: "Jul-Sep", 4: "Oct-Dec"}
    for q_idx in range(8):
        total_q = today_date.year * 4 + (cur_q - 1) - q_idx
        qy, qm_idx = divmod(total_q, 4)
        qn = qm_idx + 1
        available_quarters.append({
            "value": f"{qy:04d}-Q{qn}",
            "label": f"Q{qn} {qy} ({q_month_span[qn]})",
        })

    start_date: date | None = None
    end_date: date | None = None
    granularity = "day"
    period_label = "Last 7 Days"
    file_tag = "last-7d"
    selected_month = ""
    selected_quarter = ""

    buckets: dict[str, dict] = {}
    bucket_order: list[str] = []

    def _add_bucket(key: str, label: str) -> None:
        if key not in buckets:
            bucket_order.append(key)
            buckets[key] = {
                "key": key,
                "label": label,
                "revenue": 0.0,
                "paid_count": 0,
                "void_count": 0,
                "open_count": 0,
                "total_count": 0,
                "scan_count": 0,
            }

    if p_norm == "today":
        days_int = 1
        start_date = today_date
        end_date = today_date
        granularity = "hour"
        period_label = f"Today, {today_date.strftime('%d %b %Y')} (Hourly)"
        file_tag = "today"
        for h in range(24):
            lbl = f"{h:02d}:00"
            _add_bucket(lbl, lbl)
    elif p_norm == "7d":
        days_int = 7
        start_date = today_date - timedelta(days=6)
        end_date = today_date
        granularity = "day"
        period_label = f"Last 7 Days ({start_date.strftime('%d %b %Y')} - {end_date.strftime('%d %b %Y')})"
        file_tag = "last-7d"
        for i in range(6, -1, -1):
            d = today_date - timedelta(days=i)
            _add_bucket(d.isoformat(), d.strftime("%d %b %Y"))
    elif p_norm == "30d":
        days_int = 30
        start_date = today_date - timedelta(days=29)
        end_date = today_date
        granularity = "day"
        period_label = f"Last 30 Days ({start_date.strftime('%d %b %Y')} - {end_date.strftime('%d %b %Y')})"
        file_tag = "last-30d"
        for i in range(29, -1, -1):
            d = today_date - timedelta(days=i)
            _add_bucket(d.isoformat(), d.strftime("%d %b %Y"))
    elif p_norm == "monthly":
        days_int = 30
        parsed_m = None
        if len(month_str) == 7 and month_str[4] == "-":
            try:
                my = int(month_str[:4])
                mm = int(month_str[5:7])
                if 2000 <= my <= 2100 and 1 <= mm <= 12:
                    parsed_m = (my, mm)
            except ValueError:
                parsed_m = None
        if parsed_m:
            my, mm = parsed_m
            selected_month = f"{my:04d}-{mm:02d}"
            last_day = calendar.monthrange(my, mm)[1]
            start_date = date(my, mm, 1)
            end_date = date(my, mm, last_day)
            granularity = "day"
            period_label = start_date.strftime("%B %Y")
            file_tag = f"month-{selected_month}"
            for day_num in range(1, last_day + 1):
                d = date(my, mm, day_num)
                _add_bucket(d.isoformat(), d.strftime("%d %b %Y"))
        else:
            oy, om = _shift_year_month(today_date.year, today_date.month, -11)
            start_date = date(oy, om, 1)
            end_date = today_date
            granularity = "month"
            period_label = f"Last 12 Months ({start_date.strftime('%b %Y')} - {end_date.strftime('%b %Y')})"
            file_tag = "monthly-12m"
            for delta in range(-11, 1):
                ym_y, ym_m = _shift_year_month(today_date.year, today_date.month, delta)
                m_key = f"{ym_y:04d}-{ym_m:02d}"
                m_lbl = date(ym_y, ym_m, 1).strftime("%b %Y")
                _add_bucket(m_key, m_lbl)
    elif p_norm == "quarterly":
        days_int = 90
        parsed_q = None
        if len(quarter_str) == 7 and quarter_str[4:6] == "-Q":
            try:
                qy = int(quarter_str[:4])
                qn = int(quarter_str[6])
                if 2000 <= qy <= 2100 and 1 <= qn <= 4:
                    parsed_q = (qy, qn)
            except ValueError:
                parsed_q = None
        if parsed_q:
            qy, qn = parsed_q
            selected_quarter = f"{qy:04d}-Q{qn}"
            m_start = (qn - 1) * 3 + 1
            m_end = qn * 3
            start_date = date(qy, m_start, 1)
            end_date = date(qy, m_end, calendar.monthrange(qy, m_end)[1])
            granularity = "month"
            period_label = f"Q{qn} {qy} ({q_month_span[qn]})"
            file_tag = f"quarter-{selected_quarter}"
            for mm in range(m_start, m_end + 1):
                m_key = f"{qy:04d}-{mm:02d}"
                m_lbl = date(qy, mm, 1).strftime("%b %Y")
                _add_bucket(m_key, m_lbl)
        else:
            start_total_q = today_date.year * 4 + (cur_q - 1) - 3
            sy, sqm_idx = divmod(start_total_q, 4)
            sq = sqm_idx + 1
            start_date = date(sy, (sq - 1) * 3 + 1, 1)
            end_date = today_date
            granularity = "quarter"
            period_label = f"Last 4 Quarters ({start_date.strftime('%b %Y')} - {end_date.strftime('%b %Y')})"
            file_tag = "quarterly-4q"
            for q_idx in range(3, -1, -1):
                tq = today_date.year * 4 + (cur_q - 1) - q_idx
                by, bqm = divmod(tq, 4)
                bn = bqm + 1
                _add_bucket(f"{by:04d}-Q{bn}", f"Q{bn} {by:04d}")
    else:
        days_int = 0
        start_date = None
        end_date = None
        granularity = "day"
        period_label = f"All Time (up to {today_date.strftime('%d %b %Y')})"
        file_tag = "all-time"

    def _bucket_info(dt_local: datetime) -> tuple[str, str]:
        if granularity == "hour":
            lbl = f"{dt_local.hour:02d}:00"
            return lbl, lbl
        if granularity == "month":
            return f"{dt_local.year:04d}-{dt_local.month:02d}", dt_local.strftime("%b %Y")
        if granularity == "quarter":
            qq = (dt_local.month - 1) // 3 + 1
            return f"{dt_local.year:04d}-Q{qq}", f"Q{qq} {dt_local.year:04d}"
        d_val = dt_local.date()
        return d_val.isoformat(), d_val.strftime("%d %b %Y")

    def _in_date_range(d_val: date | None) -> bool:
        if d_val is None:
            return False
        if start_date is not None and d_val < start_date:
            return False
        if end_date is not None and d_val > end_date:
            return False
        return True

    all_time_revenue = 0.0
    today_revenue = 0.0
    weekly_revenue = 0.0
    today_paid_count = 0
    weekly_paid_count = 0
    today_bills = 0
    today_pending_count = 0
    today_void_count = 0
    paid_count = 0
    pending_count = 0
    void_count = 0

    period_revenue = 0.0
    period_paid = 0
    period_open = 0
    period_void = 0
    period_void_value = 0.0
    dish_agg: dict[str, dict] = {}
    void_agg: dict[str, dict] = {}
    export_bills: list[dict] = []
    alias_map = _dish_alias_map(db)

    for row in rows:
        (
            sid,
            bill_no,
            raw_status,
            raw_void_reason,
            final_bill,
            receipt_email,
            receipt_phone,
            curlec_payment_id,
            retake_count,
            created_at,
            updated_at,
            paid_at,
        ) = row

        st = (raw_status or "open").lower()
        try:
            items = json.loads(final_bill or "[]")
            bill_total = round(sum(float(i.get("subtotal") or 0) for i in items), 2)
        except Exception:
            items = []
            bill_total = 0.0

        created_utc = created_at or updated_at or datetime.utcnow()
        created_local = created_utc + offset
        created_d = created_local.date()

        paid_local = ((paid_at or created_utc) + offset) if st == "paid" else None
        paid_d = paid_local.date() if paid_local else None

        is_void = st in ("void", "cancelled")
        is_open = st in ("open", "pending", "retaking")
        void_reason = (raw_void_reason or "").strip()
        if is_void and not void_reason:
            void_reason = (
                f"Exceeded {max_retakes} Retries Limit"
                if int(retake_count or 0) >= max_retakes
                else "Bill timed out"
            )

        if created_d == today_date:
            today_bills += 1
            if is_open:
                today_pending_count += 1
            elif is_void:
                today_void_count += 1

        if st == "paid":
            paid_count += 1
            all_time_revenue += bill_total
            if paid_d == today_date:
                today_revenue += bill_total
                today_paid_count += 1
            if paid_d and week_start_date <= paid_d <= today_date:
                weekly_revenue += bill_total
                weekly_paid_count += 1
        elif is_open:
            pending_count += 1
        elif is_void:
            void_count += 1

        in_period_created = _in_date_range(created_d)
        in_period_paid = (st == "paid") and _in_date_range(paid_d)

        if in_period_paid:
            period_revenue += bill_total
            period_paid += 1
            pkey, plbl = _bucket_info(paid_local)
            _add_bucket(pkey, plbl)
            buckets[pkey]["revenue"] = round(buckets[pkey]["revenue"] + bill_total, 2)
            buckets[pkey]["paid_count"] += 1
            buckets[pkey]["total_count"] += 1
            for item in items:
                raw_d = str(item.get("name") or "Unknown").strip() or "Unknown"
                d_name = alias_map.get(raw_d) or alias_map.get(raw_d.casefold()) or raw_d
                qty = float(item.get("quantity") or 0)
                sub = float(item.get("subtotal") or 0)
                entry = dish_agg.setdefault(d_name, {"name": d_name, "quantity": 0.0, "revenue": 0.0})
                entry["quantity"] += qty
                entry["revenue"] += sub

        if in_period_created:
            ckey, clbl = _bucket_info(created_local)
            _add_bucket(ckey, clbl)
            if is_void:
                period_void += 1
                period_void_value += bill_total
                buckets[ckey]["void_count"] += 1
                buckets[ckey]["total_count"] += 1
                ventry = void_agg.setdefault(void_reason, {"reason": void_reason, "count": 0, "lost_value": 0.0})
                ventry["count"] += 1
                ventry["lost_value"] += bill_total
            elif is_open:
                period_open += 1
                buckets[ckey]["open_count"] += 1
                buckets[ckey]["total_count"] += 1

        if include_export_bills and (in_period_created or in_period_paid):
            item_desc = "; ".join(
                f"{(alias_map.get(i.get('name', '')) or alias_map.get(str(i.get('name', '')).casefold()) or i.get('name', ''))} x{i.get('quantity', 0)}"
                for i in items
            )
            export_bills.append({
                "bill_number": bill_no or "AAA-0001",
                "created_at_local": created_local.strftime("%Y-%m-%d %H:%M:%S"),
                "paid_at_local": paid_local.strftime("%Y-%m-%d %H:%M:%S") if paid_local else "",
                "status": "void" if is_void else st,
                "void_reason": void_reason if is_void else "",
                "total": bill_total,
                "items": item_desc,
                "receipt_email": receipt_email or "",
                "receipt_phone": receipt_phone or "",
                "curlec_payment_id": curlec_payment_id or "",
            })

    scan_rows = (
        db.query(
            ScanAttempt.created_at,
        )
        .all()
    )

    today_scans = 0
    period_scans = 0
    total_scans = len(scan_rows)

    for (s_created,) in scan_rows:
        s_utc = s_created or datetime.utcnow()
        s_local = s_utc + offset
        s_d = s_local.date()
        if s_d == today_date:
            today_scans += 1
        if _in_date_range(s_d):
            period_scans += 1
            skey, slbl = _bucket_info(s_local)
            _add_bucket(skey, slbl)
            buckets[skey]["scan_count"] = buckets[skey].get("scan_count", 0) + 1

    if p_norm == "all":
        bucket_order = sorted(buckets.keys())

    series = [buckets[k] for k in bucket_order]
    top_dishes = sorted(
        (
            {
                "name": v["name"],
                "quantity": round(v["quantity"], 2),
                "revenue": round(v["revenue"], 2),
            }
            for v in dish_agg.values()
        ),
        key=lambda x: (-x["quantity"], -x["revenue"], x["name"]),
    )[:10]
    void_causes = sorted(
        (
            {
                "reason": v["reason"],
                "count": v["count"],
                "lost_value": round(v["lost_value"], 2),
            }
            for v in void_agg.values()
        ),
        key=lambda x: (-x["count"], -x["lost_value"], x["reason"]),
    )

    summary = {
        "today_revenue": round(today_revenue, 2),
        "today_paid_count": today_paid_count,
        "weekly_revenue": round(weekly_revenue, 2),
        "weekly_paid_count": weekly_paid_count,
        "today_bills": today_bills,
        "today_pending_count": today_pending_count,
        "today_void_count": today_void_count,
        "today_scans": today_scans,
        "total_scans": total_scans,
        "all_time_revenue": round(all_time_revenue, 2),
        "total_revenue": round(all_time_revenue, 2),
        "total_bills": len(rows),
        "paid_count": paid_count,
        "pending_count": pending_count,
        "void_count": void_count,
    }
    profile = _restaurant_profile()
    summary["restaurant_name"] = profile.get("restaurant_name", "AFRC")
    summary["company_name"] = profile.get("company_name", "")
    analytics = {
        "period": p_norm,
        "month": selected_month,
        "quarter": selected_quarter,
        "days": days_int,
        "period_label": period_label,
        "file_tag": file_tag,
        "restaurant_name": profile.get("restaurant_name", "AFRC"),
        "company_name": profile.get("company_name", ""),
        "generated_at_local": now_local.strftime("%Y-%m-%d %H:%M:%S MYT"),
        "granularity": granularity,
        "available_months": available_months,
        "available_quarters": available_quarters,
        "period_totals": {
            "revenue": round(period_revenue, 2),
            "paid_count": period_paid,
            "open_count": period_open,
            "void_count": period_void,
            "void_value": round(period_void_value, 2),
            "total_bills": period_paid + period_open + period_void,
            "scan_count": period_scans,
        },
        "series": series,
        "top_dishes": top_dishes,
        "void_causes": void_causes,
    }
    res = {"summary": summary, "analytics": analytics}
    if include_export_bills:
        res["export_bills"] = export_bills
    return res

@app.get("/api/admin/bills")
def list_admin_bills(
    request: Request,
    page: int = 1,
    page_size: int = 20,
    status: str = "all",
    q: str = "",
    include_summary: bool = True,
    analytics_days: int = 7,
    period: str = "",
    month: str = "",
    quarter: str = "",
    db: Session = Depends(get_db),
):
    require_admin_api(request, db)
    _expire_stale_bills(db)

    page = max(1, int(page or 1))
    page_size = max(1, min(100, int(page_size or 20)))
    st_filter = (status or "all").strip().lower()
    search_q = (q or "").strip()

    query = db.query(SessionBill)
    if st_filter == "paid":
        query = query.filter(SessionBill.status == "paid")
    elif st_filter == "open":
        query = query.filter(SessionBill.status.in_(("open", "pending", "retaking")))
    elif st_filter == "void":
        query = query.filter(SessionBill.status.in_(("void", "cancelled")))

    if search_q:
        pat = f"%{search_q}%"
        query = query.filter(
            or_(
                SessionBill.bill_number.ilike(pat),
                SessionBill.id.ilike(pat),
                SessionBill.receipt_email.ilike(pat),
                SessionBill.receipt_phone.ilike(pat),
                SessionBill.void_reason.ilike(pat),
                SessionBill.final_bill.ilike(pat),
            )
        )

    total_items = query.count()
    total_pages = max(1, math.ceil(total_items / page_size))
    if page > total_pages:
        page = total_pages

    offset = (page - 1) * page_size
    page_rows = (
        query.order_by(SessionBill.created_at.desc())
        .offset(offset)
        .limit(page_size)
        .all()
    )
    page_sids = [s.id for s in page_rows]
    refunded_map: dict[str, float] = {}
    if page_sids:
        refund_rows = (
            db.query(
                BillRefund.session_id,
                func.coalesce(func.sum(BillRefund.refund_amount), 0.0),
            )
            .filter(BillRefund.session_id.in_(page_sids))
            .group_by(BillRefund.session_id)
            .all()
        )
        refunded_map = {sid: float(amt or 0.0) for sid, amt in refund_rows}
    alias_map = _dish_alias_map(db)
    bills = [_bill_payload(s, refunded_amount=refunded_map.get(s.id, 0.0), alias_map=alias_map) for s in page_rows]

    payload = {
        "bills": bills,
        "pagination": {
            "page": page,
            "page_size": page_size,
            "total_items": total_items,
            "total_pages": total_pages,
            "has_prev": page > 1,
            "has_next": page < total_pages,
        },
    }
    if include_summary:
        overview = _compute_overview_data(
            db,
            days=analytics_days,
            period=period,
            month=month,
            quarter=quarter,
        )
        payload["summary"] = overview["summary"]
        payload["analytics"] = overview["analytics"]
    return JSONResponse(content=payload, headers=NO_CACHE_HEADERS)

@app.get("/api/admin/analytics")
def get_admin_analytics(
    request: Request,
    days: int = 7,
    period: str = "",
    month: str = "",
    quarter: str = "",
    db: Session = Depends(get_db),
):
    require_admin_api(request, db)
    _expire_stale_bills(db)
    data = _compute_overview_data(
        db,
        days=days,
        period=period,
        month=month,
        quarter=quarter,
    )
    return JSONResponse(content=data, headers=NO_CACHE_HEADERS)

@app.get("/api/admin/export")
def export_admin_sales_csv(
    request: Request,
    days: int = 7,
    period: str = "",
    month: str = "",
    quarter: str = "",
    db: Session = Depends(get_db),
):
    require_admin_api(request, db)
    _expire_stale_bills(db)
    data = _compute_overview_data(
        db,
        days=days,
        period=period,
        month=month,
        quarter=quarter,
        include_export_bills=True,
    )
    summary = data["summary"]
    analytics = data["analytics"]
    pt = analytics["period_totals"]

    buf = io.StringIO()
    writer = csv.writer(buf)

    rest_name = analytics.get("restaurant_name") or "AFRC"
    comp_name = analytics.get("company_name") or ""
    host_name = request.headers.get("host", "").split(":")[0] or "e-cashier"

    writer.writerow([rest_name])
    if comp_name:
        writer.writerow([comp_name])
    writer.writerow([f"{host_name} comprehensive data reports"])
    writer.writerow(["Period", analytics["period_label"]])
    writer.writerow(["Generated At", analytics["generated_at_local"]])
    writer.writerow([])

    writer.writerow(["DASHBOARD OVERVIEW KPIs"])
    writer.writerow(["Metric", "Value"])
    writer.writerow(["Today's Paid Revenue (RM)", f"{summary['today_revenue']:.2f}"])
    writer.writerow(["Today's Paid Bills", summary["today_paid_count"]])
    writer.writerow(["Weekly Paid Revenue - Last 7 Days (RM)", f"{summary['weekly_revenue']:.2f}"])
    writer.writerow(["Weekly Paid Bills - Last 7 Days", summary["weekly_paid_count"]])
    writer.writerow(["Today's Bills Logged", summary["today_bills"]])
    writer.writerow(["Today's Voided Bills", summary["today_void_count"]])
    writer.writerow(["Today's Pending Bills", summary["today_pending_count"]])
    writer.writerow([f"Period Revenue - {analytics['period_label']} (RM)", f"{pt['revenue']:.2f}"])
    writer.writerow([f"Period Paid Bills - {analytics['period_label']}", pt["paid_count"]])
    writer.writerow([f"Period Voided Bills - {analytics['period_label']}", pt["void_count"]])
    writer.writerow([f"Period Voided Value - {analytics['period_label']} (RM)", f"{pt['void_value']:.2f}"])
    writer.writerow([f"Period Scanned Images - {analytics['period_label']}", pt.get("scan_count", 0)])
    writer.writerow(["Today's Scanned Images", summary.get("today_scans", 0)])
    writer.writerow(["All-Time Scanned Images", summary.get("total_scans", 0)])
    writer.writerow(["All-Time Revenue (RM)", f"{summary['all_time_revenue']:.2f}"])
    writer.writerow(["All-Time Total Bills", summary["total_bills"]])
    writer.writerow([])

    writer.writerow(["SALES TIMELINE BREAKDOWN"])
    time_col = "Hour" if analytics["granularity"] == "hour" else ("Month" if analytics["granularity"] == "month" else ("Quarter" if analytics["granularity"] == "quarter" else "Date"))
    writer.writerow([time_col, "Paid Revenue (RM)", "Paid Bills", "Pending Bills", "Voided Bills", "Total Bills", "Scanned Images"])
    for row in analytics["series"]:
        writer.writerow([
            row["label"] if analytics["granularity"] == "hour" else row["key"],
            f"{row['revenue']:.2f}",
            row["paid_count"],
            row["open_count"],
            row["void_count"],
            row["total_count"],
            row.get("scan_count", 0),
        ])
    writer.writerow([])

    writer.writerow(["TOP SELLING DISHES (PAID BILLS)"])
    writer.writerow(["Rank", "Dish Name", "Quantity Sold", "Revenue (RM)"])
    for idx, d in enumerate(analytics["top_dishes"], start=1):
        writer.writerow([idx, d["name"], d["quantity"], f"{d['revenue']:.2f}"])
    writer.writerow([])

    writer.writerow(["VOID CAUSE BREAKDOWN"])
    writer.writerow(["Void Cause", "Bill Count", "Voided Amount (RM)"])
    for vc in analytics["void_causes"]:
        writer.writerow([vc["reason"], vc["count"], f"{vc['lost_value']:.2f}"])
    writer.writerow([])

    writer.writerow(["ITEMIZED BILLS LOG"])
    writer.writerow(["Bill #", "Created At (MYT)", "Paid At (MYT)", "Status", "Void Cause", "Total (RM)", "Items", "Phone Number", "Customer Email", "Payment ID"])
    for b in data["export_bills"]:
        writer.writerow([
            _csv_escape(b["bill_number"]),
            _csv_escape(b["created_at_local"]),
            _csv_escape(b["paid_at_local"]),
            _csv_escape(b["status"].upper()),
            _csv_escape(b["void_reason"]),
            f"{b['total']:.2f}",
            _csv_escape(b["items"]),
            _csv_escape(b.get("receipt_phone", "")),
            _csv_escape(b["receipt_email"]),
            _csv_escape(b["curlec_payment_id"]),
        ])

    csv_bytes = ("\ufeff" + buf.getvalue()).encode("utf-8")
    day_tag = analytics.get("file_tag") or "report"
    date_tag = (datetime.utcnow() + _local_tz_offset()).strftime("%Y-%m-%d")
    filename = f"afrc-sales-report-{day_tag}-{date_tag}.csv"
    return Response(
        content=csv_bytes,
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            **NO_CACHE_HEADERS,
        },
    )

@app.post("/api/admin/bills/{session_id}/status")
def update_admin_bill_status(session_id: str, payload: dict, request: Request, db: Session = Depends(get_db)):
    require_admin_api(request, db)
    bill = db.query(SessionBill).filter(SessionBill.id == session_id).first()
    if not bill:
        raise HTTPException(status_code=404, detail="Bill not found")
    new_status = str(payload.get("status") or "").strip().lower()
    if new_status not in ("open", "pending", "paid", "needs_cashier", "void", "cancelled"):
        raise HTTPException(status_code=400, detail="Invalid status")
    if new_status in ("needs_cashier", "cancelled"):
        bill.status = "void"
        bill.void_reason = (
            "Needed Cashier Assistance" if new_status == "needs_cashier" else "Customer Cancelled"
        )
    else:
        bill.status = new_status
        if new_status == "void":
            bill.void_reason = str(payload.get("void_reason") or "").strip() or bill.void_reason or "Voided by Admin"
    bill.updated_at = datetime.utcnow()
    if bill.status == "paid" and not bill.paid_at:
        bill.paid_at = datetime.utcnow()
    db.add(AuditLog(
        cashier_id=current_admin.id,
        admin_username=current_admin.username,
        action=f"BILL_{new_status.upper()}",
        details=f"Bill #{bill.bill_number}: status set to {new_status} by admin '{current_admin.username}'"
    ))
    db.commit()
    return _bill_payload(bill)

def _build_refund_info(bill: SessionBill, db: Session, sync_curlec_contact: bool = False) -> dict:
    if sync_curlec_contact and bill.curlec_payment_id and (
        not (bill.receipt_phone or "").strip() or not (bill.receipt_email or "").strip()
    ):
        try:
            client, _key_id = _curlec_client()
            payment = client.payment.fetch(bill.curlec_payment_id, timeout=6)
            contact = str(payment.get("contact") or "").strip()
            email = str(payment.get("email") or "").strip()
            changed = False
            if contact and not (bill.receipt_phone or "").strip():
                bill.receipt_phone = contact
                changed = True
            if email and not (bill.receipt_email or "").strip():
                bill.receipt_email = email
                changed = True
            if changed:
                db.commit()
        except Exception:
            pass

    refund_rows = (
        db.query(BillRefund)
        .filter(BillRefund.session_id == bill.id)
        .order_by(BillRefund.created_at.desc())
        .all()
    )
    refunded_by_name: dict[str, float] = {}
    refunded_total = 0.0
    refunds_history = []
    for rf in refund_rows:
        amt = float(rf.refund_amount or 0.0)
        refunded_total += amt
        try:
            rf_items = json.loads(rf.refunded_items or "[]")
        except Exception:
            rf_items = []
        for it in rf_items:
            nm = str(it.get("name") or "").strip().lower()
            if nm:
                refunded_by_name[nm] = refunded_by_name.get(nm, 0.0) + float(it.get("quantity") or 0.0)
        refunds_history.append({
            "id": rf.id,
            "curlec_refund_id": rf.curlec_refund_id or "",
            "refund_amount": round(amt, 2),
            "reason": rf.reason or "",
            "status": rf.status or "processed",
            "items": rf_items,
            "created_at": (rf.created_at.isoformat() + "Z") if rf.created_at else "",
        })

    base = _bill_payload(bill, refunded_amount=refunded_total)
    item_rows = []
    for idx, it in enumerate(base["items"]):
        name = str(it.get("name") or f"Item {idx + 1}")
        qty = float(it.get("quantity") or 0.0)
        subtotal = float(it.get("subtotal") or 0.0)
        unit_price = float(it.get("unit_price") or (subtotal / qty if qty > 0 else 0.0))
        already_qty = refunded_by_name.get(name.strip().lower(), 0.0)
        rem_qty = max(0.0, round(qty - already_qty, 4))
        item_rows.append({
            "index": idx,
            "name": name,
            "quantity": qty,
            "unit_price": round(unit_price, 2),
            "subtotal": round(subtotal, 2),
            "refunded_qty": round(already_qty, 4),
            "remaining_qty": rem_qty,
        })

    bill_total = float(base["total"] or 0.0)
    remaining_refundable = max(0.0, round(bill_total - refunded_total, 2))
    return {
        "session_id": bill.id,
        "bill_number": base["bill_number"],
        "status": base["status"],
        "total": bill_total,
        "receipt_email": base["receipt_email"],
        "receipt_phone": base["receipt_phone"],
        "curlec_order_id": base["curlec_order_id"],
        "curlec_payment_id": base["curlec_payment_id"],
        "has_image": base["has_image"],
        "image_url": (
            f"/api/sessions/{quote(bill.id)}/image?v={int(bill.retake_count or 0)}"
            if base["has_image"]
            else ""
        ),
        "refunded_amount": round(refunded_total, 2),
        "remaining_refundable": remaining_refundable,
        "items": item_rows,
        "refunds": refunds_history,
    }

@app.get("/api/admin/bills/by-number/{bill_number}")
def get_admin_bill_by_number(bill_number: str, request: Request, db: Session = Depends(get_db)):
    require_admin_api(request, db)
    _expire_stale_bills(db)
    clean_no = bill_number.strip().lstrip("#")
    bill = db.query(SessionBill).filter(
        or_(
            func.lower(SessionBill.bill_number) == clean_no.lower(),
            func.lower(SessionBill.bill_number) == bill_number.strip().lower()
        )
    ).first()
    if not bill:
        raise HTTPException(status_code=404, detail="Bill not found")
    refunded_amt = db.query(func.coalesce(func.sum(BillRefund.refund_amount), 0.0)).filter(
        BillRefund.session_id == bill.id,
        BillRefund.status == "completed"
    ).scalar() or 0.0
    return JSONResponse(content=_bill_payload(bill, float(refunded_amt), is_owner_or_admin=True, alias_map=_dish_alias_map(db)), headers=NO_CACHE_HEADERS)

@app.get("/api/admin/bills/{session_id}/refund-info")
def get_admin_bill_refund_info(session_id: str, request: Request, db: Session = Depends(get_db)):
    require_admin_api(request, db)
    bill = db.query(SessionBill).filter(SessionBill.id == session_id).first()
    if not bill:
        raise HTTPException(status_code=404, detail="Bill not found")
    return _build_refund_info(bill, db, sync_curlec_contact=True)

@app.post("/api/admin/bills/{session_id}/refund")
def refund_admin_bill(session_id: str, payload: dict, request: Request, db: Session = Depends(get_db)):
    current_admin = require_admin_api(request, db)
    with _REFUND_LOCK:
        if session_id in _IN_FLIGHT_REFUNDS:
            raise HTTPException(
                status_code=409,
                detail="A refund is currently in progress for this bill. Please wait a moment."
            )
        _IN_FLIGHT_REFUNDS.add(session_id)

    try:
        bill = db.query(SessionBill).filter(SessionBill.id == session_id).first()
        if not bill:
            raise HTTPException(status_code=404, detail="Bill not found")
        if (bill.status or "").lower() != "paid":
            raise HTTPException(status_code=409, detail="Only paid bills can be refunded")

        info = _build_refund_info(bill, db, sync_curlec_contact=False)
        remaining_refundable = float(info["remaining_refundable"] or 0.0)
        if remaining_refundable <= 0:
            raise HTTPException(status_code=409, detail="This bill has already been fully refunded")

        req_items = payload.get("items") or []
        if not isinstance(req_items, list) or not req_items:
            raise HTTPException(status_code=400, detail="Select at least one item to refund")

        by_name = {str(it["name"]).strip().lower(): it for it in info["items"]}
        refund_items_payload = []
        total_refund = 0.0
        for req_it in req_items:
            if not isinstance(req_it, dict):
                continue
            name = str(req_it.get("name") or "").strip()
            try:
                qty = float(req_it.get("quantity") or 0)
            except (TypeError, ValueError):
                qty = 0.0
            if not name or qty <= 0:
                continue
            bill_item = by_name.get(name.lower())
            if not bill_item:
                raise HTTPException(status_code=400, detail=f"Item '{name}' is not on this bill")
            rem_qty = float(bill_item["remaining_qty"] or 0.0)
            if qty > rem_qty + 1e-6:
                raise HTTPException(
                    status_code=400,
                    detail=f"Refund quantity for {bill_item['name']} exceeds remaining bill quantity ({rem_qty})",
                )
            qty_clean = int(qty) if abs(qty - round(qty)) < 1e-6 else round(qty, 2)
            unit_price = float(bill_item["unit_price"] or 0.0)
            line_sub = round(unit_price * qty_clean, 2)
            total_refund += line_sub
            refund_items_payload.append({
                "name": bill_item["name"],
                "quantity": qty_clean,
                "unit_price": unit_price,
                "subtotal": line_sub,
            })

        if not refund_items_payload:
            raise HTTPException(status_code=400, detail="Select at least one item with quantity greater than 0")

        total_refund = round(total_refund, 2)
        if total_refund > remaining_refundable + 0.01:
            raise HTTPException(
                status_code=400,
                detail=f"Refund amount RM {total_refund:.2f} exceeds remaining refundable RM {remaining_refundable:.2f}",
            )
        total_refund = min(total_refund, remaining_refundable)
        amount_sen = int(round(total_refund * 100))
        if amount_sen <= 0:
            raise HTTPException(status_code=400, detail="Refund amount must be greater than RM 0.00")

        reason = str(payload.get("reason") or "").strip()
        curlec_refund_id = ""
        refund_status = "manual"
        if (bill.curlec_payment_id or "").strip():
            client, _key_id = _curlec_client()
            receipt_ref = f"rfnd-{bill.bill_number or bill.id}-{int(datetime.utcnow().timestamp())}"[:40]
            try:
                rf_resp = client.payment.refund(
                    bill.curlec_payment_id.strip(),
                    {
                        "amount": amount_sen,
                        "speed": "normal",
                        "notes": {
                            "bill_number": (bill.bill_number or "")[:255],
                            "session_id": bill.id[:255],
                            "reason": (reason or "Admin refund")[:255],
                        },
                        "receipt": receipt_ref,
                    },
                    timeout=15,
                )
                curlec_refund_id = str(rf_resp.get("id") or "")
                refund_status = str(rf_resp.get("status") or "processed")
            except Exception as exc:
                err_msg = str(exc)
                raise HTTPException(status_code=400, detail=f"Curlec refund failed: {err_msg}")

        rf_row = BillRefund(
            session_id=bill.id,
            bill_number=bill.bill_number or "",
            curlec_payment_id=bill.curlec_payment_id or "",
            curlec_refund_id=curlec_refund_id,
            refund_amount=round(total_refund, 2),
            refunded_items=json.dumps(refund_items_payload),
            reason=reason,
            status=refund_status,
        )
        db.add(rf_row)
        db.add(AuditLog(
            cashier_id=current_admin.id,
            admin_username=current_admin.username,
            action="REFUND_ISSUED",
            details=(
                f"Bill #{bill.bill_number or bill.id}: RM {total_refund:.2f} "
                f"({', '.join(str(i['quantity']) + 'x ' + str(i['name']) for i in refund_items_payload)})"
                + (f" [Curlec {curlec_refund_id}]" if curlec_refund_id else " [Manual]")
                + f" by admin '{current_admin.username}'"
            ),
        ))
        bill.updated_at = datetime.utcnow()
        db.commit()
        return {
            "status": "ok",
            "refund": {
                "id": rf_row.id,
                "curlec_refund_id": curlec_refund_id,
                "refund_amount": round(total_refund, 2),
                "status": refund_status,
                "items": refund_items_payload,
                "reason": reason,
            },
            "refund_info": _build_refund_info(bill, db, sync_curlec_contact=False),
        }
    finally:
        with _REFUND_LOCK:
            _IN_FLIGHT_REFUNDS.discard(session_id)

@app.get("/api/admin/audit-logs")
def list_audit_logs(
    request: Request,
    page: int = 1,
    page_size: int = 20,
    db: Session = Depends(get_db),
):
    require_admin_api(request, db)
    page = max(1, int(page or 1))
    page_size = max(1, min(100, int(page_size or 20)))

    query = db.query(AuditLog)
    total_items = query.count()
    total_pages = max(1, math.ceil(total_items / page_size))
    if page > total_pages:
        page = total_pages

    offset = (page - 1) * page_size
    logs = query.order_by(AuditLog.timestamp.desc()).offset(offset).limit(page_size).all()
    cashier_map = {c.id: c.name for c in db.query(Cashier).all()}

    logs_payload = [
        {
            "id": l.id,
            "cashier_id": l.cashier_id,
            "cashier_name": l.admin_username if getattr(l, "admin_username", None) else cashier_map.get(
                l.cashier_id,
                "System / Admin" if l.cashier_id == 0 else f"Cashier #{l.cashier_id}"
            ),
            "action": l.action,
            "details": l.details or "",
            "timestamp": (l.timestamp.isoformat() + "Z") if l.timestamp else "",
        }
        for l in logs
    ]

    return JSONResponse(
        content={
            "logs": logs_payload,
            "pagination": {
                "page": page,
                "page_size": page_size,
                "total_items": total_items,
                "total_pages": total_pages,
                "has_prev": page > 1,
                "has_next": page < total_pages,
            },
        },
        headers=NO_CACHE_HEADERS,
    )


@app.get("/api/admin/restaurant/settings")
def get_admin_restaurant_settings(request: Request, db: Session = Depends(get_db)):
    require_admin_api(request, db)
    prof = _restaurant_profile()
    settings = _restaurant_settings()
    return {
        **settings,
        "restaurant_name": prof["restaurant_name"],
        "company_name": prof["company_name"],
    }

@app.post("/api/admin/restaurant/settings")
async def update_admin_restaurant_settings(request: Request, db: Session = Depends(get_db)):
    current_admin = require_admin_api(request, db)
    data = await request.json()
    if not isinstance(data, dict):
        raise HTTPException(status_code=400, detail="Invalid request body")
    
    op_start = data.get("operation_hours_start")
    op_end = data.get("operation_hours_end")
    time_re = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")
    if op_start is not None and not time_re.match(str(op_start).strip()):
        raise HTTPException(status_code=400, detail="Invalid operation_hours_start (format HH:MM required)")
    if op_end is not None and not time_re.match(str(op_end).strip()):
        raise HTTPException(status_code=400, detail="Invalid operation_hours_end (format HH:MM required)")
    
    updated = _save_restaurant_settings(data)
    
    changes = []
    if "coming_soon" in data:
        changes.append(f"coming_soon={'ENABLED' if updated['coming_soon'] else 'DISABLED'}")
    if "maintenance_mode" in data:
        changes.append(f"maintenance_mode={'ENABLED' if updated['maintenance_mode'] else 'DISABLED'}")
    if op_start is not None or op_end is not None:
        changes.append(f"operation_hours={updated['operation_hours_start']}-{updated['operation_hours_end']}")
        
    change_desc = ", ".join(changes) or "no changes"
    db.add(AuditLog(
        cashier_id=current_admin.id,
        admin_username=current_admin.username,
        action="RESTAURANT_SETTINGS_UPDATE",
        details=f"Admin '{current_admin.username}' updated restaurant settings: {change_desc}"
    ))
    db.commit()
    
    prof = _restaurant_profile()
    return {
        **updated,
        "restaurant_name": prof["restaurant_name"],
        "company_name": prof["company_name"],
    }


@app.get("/api/admin/failed-scans")
def list_failed_scans(
    request: Request,
    period: str = "7d",
    rejection_code: str = "all",
    scan_type: str = "all",
    search: str = "",
    page: int = 1,
    page_size: int = 20,
    db: Session = Depends(get_db),
):
    current_admin = require_admin_api(request, db)
    offset_hours = timedelta(hours=8)
    now_local = datetime.utcnow() + offset_hours
    today_date = now_local.date()

    query = db.query(FailedScan)

    p_norm = (period or "7d").lower()
    if p_norm == "today":
        start_utc = datetime.combine(today_date, datetime.min.time()) - offset_hours
        query = query.filter(FailedScan.created_at >= start_utc)
    elif p_norm == "7d":
        start_date = today_date - timedelta(days=6)
        start_utc = datetime.combine(start_date, datetime.min.time()) - offset_hours
        query = query.filter(FailedScan.created_at >= start_utc)
    elif p_norm == "30d":
        start_date = today_date - timedelta(days=29)
        start_utc = datetime.combine(start_date, datetime.min.time()) - offset_hours
        query = query.filter(FailedScan.created_at >= start_utc)

    if rejection_code and rejection_code != "all":
        query = query.filter(FailedScan.rejection_code == rejection_code.strip())

    if scan_type == "retries":
        query = query.filter(FailedScan.is_retry == True)
    elif scan_type == "initial":
        query = query.filter(FailedScan.is_retry == False)

    s_term = (search or "").strip()
    if s_term:
        query = query.filter(
            or_(
                FailedScan.bill_number.ilike(f"%{s_term}%"),
                FailedScan.session_id.ilike(f"%{s_term}%"),
                FailedScan.rejection_message.ilike(f"%{s_term}%"),
                FailedScan.rejection_code.ilike(f"%{s_term}%"),
                FailedScan.ip.ilike(f"%{s_term}%"),
            )
        )

    total_items = query.count()
    total_pages = max(1, math.ceil(total_items / page_size))
    cur_page = max(1, min(page, total_pages))
    offset_idx = (cur_page - 1) * page_size

    rows = query.order_by(FailedScan.created_at.desc()).offset(offset_idx).limit(page_size).all()

    total_all = db.query(FailedScan).count()
    start_today_utc = datetime.combine(today_date, datetime.min.time()) - offset_hours
    today_count = db.query(FailedScan).filter(FailedScan.created_at >= start_today_utc).count()
    retries_count = db.query(FailedScan).filter(FailedScan.is_retry == True).count()

    code_counts = {}
    for (c,) in db.query(FailedScan.rejection_code).all():
        code_counts[c] = code_counts.get(c, 0) + 1

    top_rejection = max(code_counts.items(), key=lambda x: x[1])[0] if code_counts else "None"

    items = []
    for r in rows:
        dt_local = (r.created_at or datetime.utcnow()) + offset_hours
        items.append({
            "id": r.id,
            "session_id": r.session_id,
            "bill_number": r.bill_number,
            "is_retry": bool(r.is_retry),
            "rejection_code": r.rejection_code,
            "rejection_message": r.rejection_message,
            "has_image": bool(r.image_key),
            "image_url": f"/api/admin/failed-scans/{r.id}/image" if r.image_key else "",
            "raw_ai_response": r.raw_ai_response or "",
            "ip": r.ip or "",
            "device_id": r.device_id or "",
            "created_at_local": dt_local.strftime("%Y-%m-%d %H:%M:%S"),
        })

    return JSONResponse(
        content={
            "items": items,
            "pagination": {
                "page": cur_page,
                "page_size": page_size,
                "total_items": total_items,
                "total_pages": total_pages,
                "has_prev": cur_page > 1,
                "has_next": cur_page < total_pages,
            },
            "summary": {
                "total_failed": total_all,
                "today_failed": today_count,
                "retries_failed": retries_count,
                "top_rejection": top_rejection,
                "code_counts": code_counts,
            },
        },
        headers=NO_CACHE_HEADERS,
    )


@app.get("/api/admin/failed-scans/{scan_id}/image")
async def get_failed_scan_image(
    scan_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    require_admin_api(request, db)
    scan = db.query(FailedScan).filter(FailedScan.id == scan_id).first()
    if not scan or not scan.image_key:
        raise HTTPException(status_code=404, detail="Image not found")

    filename = Path(scan.image_key).name
    failed_dir = UPLOAD_DIR / "failed_scans"
    local_path = failed_dir / filename
    if local_path.is_file() and local_path.stat().st_size > 0:
        return FileResponse(local_path, media_type="image/jpeg", headers=NO_CACHE_HEADERS)

    if "_" in filename:
        uuid_part = filename.rsplit("_", 1)[-1]
        alt_path = failed_dir / uuid_part
        if alt_path.is_file() and alt_path.stat().st_size > 0:
            return FileResponse(alt_path, media_type="image/jpeg", headers=NO_CACHE_HEADERS)

    r2_bytes = await _fetch_plate_from_r2(scan.image_key)
    if r2_bytes:
        try:
            failed_dir.mkdir(parents=True, exist_ok=True)
            local_path.write_bytes(r2_bytes)
        except OSError:
            pass
        return Response(content=r2_bytes, media_type="image/jpeg", headers=NO_CACHE_HEADERS)

    raise HTTPException(status_code=404, detail="Image could not be retrieved")

@app.post("/api/feedback")
def submit_customer_feedback(payload: dict, request: Request, db: Session = Depends(get_db)):
    session_id = str(payload.get("session_id") or "").strip()
    try:
        rating = int(payload.get("rating") or 5)
    except (ValueError, TypeError):
        rating = 5

    if rating < 1:
        rating = 1
    elif rating > 5:
        rating = 5

    prefix = str(payload.get("prefix_comment") or "").strip()
    comment = str(payload.get("comment") or "").strip()
    if len(comment) > 80:
        comment = comment[:80]

    bill_number = ""
    phone = ""
    if session_id:
        bill = db.query(SessionBill).filter(SessionBill.id == session_id).first()
        if bill:
            bill_number = bill.bill_number or ""
            phone = bill.receipt_phone or ""

    existing = None
    if session_id:
        existing = db.query(Feedback).filter(Feedback.session_id == session_id).first()

    if existing:
        existing.rating = rating
        existing.prefix_comment = prefix
        existing.comment = comment
        if bill_number:
            existing.bill_number = bill_number
        if phone:
            existing.phone = phone
        db.commit()
        return {"ok": True, "message": "Feedback updated", "id": existing.id}

    fb = Feedback(
        session_id=session_id,
        bill_number=bill_number,
        phone=phone,
        rating=rating,
        prefix_comment=prefix,
        comment=comment,
        created_at=datetime.utcnow(),
    )
    db.add(fb)
    db.commit()
    db.refresh(fb)
    return {"ok": True, "message": "Feedback submitted successfully", "id": fb.id}


@app.get("/api/admin/feedbacks")
def get_admin_feedbacks(request: Request, db: Session = Depends(get_db)):
    require_admin_api(request, db)
    rows = db.query(Feedback).order_by(Feedback.created_at.desc()).all()

    tz_offset = _local_tz_offset()

    session_ids = [r.session_id for r in rows if r.session_id]
    bills_map = {}
    if session_ids:
        bills = db.query(SessionBill).filter(SessionBill.id.in_(session_ids)).all()
        for b in bills:
            bills_map[b.id] = b

    feedbacks_data = []
    distribution = {1: 0, 2: 0, 3: 0, 4: 0, 5: 0}
    total_rating_sum = 0

    for fb in rows:
        rating = fb.rating if (fb.rating and 1 <= fb.rating <= 5) else 5
        distribution[rating] = distribution.get(rating, 0) + 1
        total_rating_sum += rating

        bill = bills_map.get(fb.session_id)
        bill_no = fb.bill_number or (bill.bill_number if bill else "") or "N/A"
        phone = fb.phone or (bill.receipt_phone if bill else "") or "N/A"

        has_image = False
        image_url = ""
        bill_total = 0.0
        if bill:
            has_image = bool(bill.image_key or (UPLOAD_DIR / f"{bill.id}.jpg").is_file())
            if has_image or bill.id:
                image_url = f"/api/sessions/{bill.id}/image"
            try:
                items = json.loads(bill.final_bill or "[]")
                bill_total = round(sum(float(it.get("subtotal") or 0) for it in items), 2)
            except Exception:
                bill_total = 0.0

        dt_local = fb.created_at + tz_offset if fb.created_at else datetime.utcnow() + tz_offset

        feedbacks_data.append({
            "id": fb.id,
            "session_id": fb.session_id,
            "bill_number": bill_no,
            "phone": phone,
            "rating": rating,
            "prefix_comment": fb.prefix_comment or "",
            "comment": fb.comment or "",
            "has_image": has_image,
            "image_url": image_url,
            "bill_total": bill_total,
            "created_at_local": dt_local.strftime("%Y-%m-%d %H:%M:%S"),
        })

    count = len(feedbacks_data)
    avg_rating = round(total_rating_sum / count, 1) if count > 0 else 0.0

    return {
        "feedbacks": feedbacks_data,
        "summary": {
            "total": count,
            "avg_rating": avg_rating,
            "distribution": distribution,
            "positive_count": distribution[4] + distribution[5],
            "improvement_count": distribution[1] + distribution[2] + distribution[3],
        }
    }
