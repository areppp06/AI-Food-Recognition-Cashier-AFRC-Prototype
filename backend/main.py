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
import uuid
import json
import hashlib
import httpx
import razorpay
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import quote
from dotenv import dotenv_values

from .db import ROOT, ENV_DIR, SessionLocal, init_db, Dish, Cashier, AuditLog, SessionBill, ClientCooldown, ScanAttempt, BillRefund, next_bill_number
from .schemas import Dish as DishSchema, BillResponse, BillItem, VerifyRequest, ErrorResponse
from .ai_service import recognize_food, RecognitionError

UPLOAD_DIR = Path(__file__).resolve().parent.parent / "uploads"
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
app = FastAPI(title="AFRC Prototype", version="1.0")

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
                return True
    except Exception:
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
        "price": dish.price,
        "available": dish.available,
        "unit": dish.unit,
        "description": dish.description or "",
        "pieces_per_portion": dish.pieces_per_portion or 1,
    }

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

def _client_ip(request: Request) -> str:
    cf_ip = request.headers.get("cf-connecting-ip", "").strip()
    if cf_ip:
        return cf_ip
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
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
        hostname.strip()
        for hostname in os.getenv("TURNSTILE_HOSTNAMES", "").split(",")
        if hostname.strip()
    }
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
    if (
        result.get("success") is not True
        or result.get("action") != expected_action
        or result.get("hostname") not in hostnames
    ):
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

def _track_task(coro) -> asyncio.Task:
    task = asyncio.create_task(coro)
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_TASKS.discard)
    return task

@app.on_event("startup")
async def startup():
    init_db()
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
}

@app.get("/")
async def root():
    return FileResponse("frontend/index.html", headers=NO_CACHE_HEADERS)

@app.get("/admin")
async def admin():
    return FileResponse("frontend/admin.html", headers=NO_CACHE_HEADERS)

@app.get("/cashier")
async def cashier():
    return FileResponse("frontend/cashier.html", headers=NO_CACHE_HEADERS)

@app.get("/pay")
async def pay_page():
    return FileResponse("frontend/pay.html", headers=NO_CACHE_HEADERS)

@app.get("/payment/success")
async def payment_success_page():
    return FileResponse("frontend/success.html", headers=NO_CACHE_HEADERS)

@app.get("/api/menu")
def get_menu(db: Session = Depends(get_db)):
    dishes = db.query(Dish).all()
    return [_dish_json(d) for d in dishes]

@app.post("/api/menu")
def update_menu(dishes: List[dict], db: Session = Depends(get_db)):
    for d in dishes:
        dish = db.query(Dish).filter(Dish.id == d["id"]).first()
        if dish:
            dish.available = d.get("available", dish.available)
            dish.price = d.get("price", dish.price)
            if "description" in d:
                dish.description = _clean_description(d.get("description"))
            if "pieces_per_portion" in d:
                dish.pieces_per_portion = _clean_pieces(d.get("pieces_per_portion"))
    db.commit()
    return {"status": "updated"}

@app.post("/api/menu/item")
def add_menu_item(payload: dict, db: Session = Depends(get_db)):
    name = str(payload.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="Dish name is required")
    try:
        price = float(payload.get("price"))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="A valid price is required")
    if price < 0:
        raise HTTPException(status_code=400, detail="Price cannot be negative")
    existing = db.query(Dish).filter(func.lower(Dish.name) == name.casefold()).first()
    if existing:
        raise HTTPException(status_code=409, detail="A dish with this name already exists")
    unit = str(payload.get("unit") or "portion").strip() or "portion"
    dish = Dish(
        name=name,
        price=price,
        available=bool(payload.get("available", True)),
        unit=unit,
        description=_clean_description(payload.get("description")),
        pieces_per_portion=_clean_pieces(payload.get("pieces_per_portion")),
    )
    db.add(dish)
    db.commit()
    db.refresh(dish)
    return _dish_json(dish)

@app.delete("/api/menu/{dish_id}")
def delete_menu_item(dish_id: int, db: Session = Depends(get_db)):
    dish = db.query(Dish).filter(Dish.id == dish_id).first()
    if not dish:
        raise HTTPException(status_code=404, detail="Dish not found")
    db.delete(dish)
    db.commit()
    return {"status": "deleted", "id": dish_id}

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
            "company_phone": prof["company_phone"],
            "company_email": prof["company_email"],
            "active_bill": _bill_payload(active_bill) if active_bill else None,
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
    available = [_dish_json(d) for d in db.query(Dish).filter(Dish.available == True).all()]
    # The menu is now a plain list. End the read so Grok runs without holding a transaction.
    db.rollback()

    try:
        ai_items = await recognize_food(image_bytes, available)
    except RecognitionError as e:
        raise HTTPException(status_code=422, detail=ErrorResponse(
            error_code=e.code,
            message=e.message,
            retake_required=True
        ).dict())
    except Exception as e:
        raise HTTPException(status_code=503, detail=ErrorResponse(
            error_code="AI_SERVICE_ERROR",
            message="AI service is temporarily down. Please try again in a moment."
        ).dict())

    if not ai_items:
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
        bill_items.append(BillItem(
            name=dish.name,
            quantity=billed_qty,
            unit_price=dish.price,
            subtotal=round(sub, 2),
            piece_count=piece_count
        ))

    if not bill_items:
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

def _bill_payload(session: SessionBill, refunded_amount: float = 0.0) -> dict:
    prof = _restaurant_profile()
    try:
        items = json.loads(session.final_bill or "[]")
    except Exception:
        items = []
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
        "receipt_email": session.receipt_email or "",
        "receipt_phone": session.receipt_phone or "",
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
    resp = JSONResponse(content=_bill_payload(bill), headers=NO_CACHE_HEADERS)
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
    max_retakes = _rate_limit_config()["max_retakes"]
    if (bill.retake_count or 0) >= max_retakes:
        raise HTTPException(status_code=409, detail="No retries remaining")
    c_token = _get_client_token(request, (payload or {}).get("client_token", ""))
    bill.status = "retaking"
    bill.updated_at = datetime.utcnow()
    if c_token:
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
async def get_session_image(session_id: str):
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
def need_cashier(session_id: str, db: Session = Depends(get_db)):
    bill = db.query(SessionBill).filter(SessionBill.id == session_id).first()
    if not bill:
        raise HTTPException(status_code=404, detail="Bill not found")
    if bill.status == "paid":
        raise HTTPException(status_code=409, detail="This bill is already paid")
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
def cancel_bill(session_id: str, db: Session = Depends(get_db)):
    bill = db.query(SessionBill).filter(SessionBill.id == session_id).first()
    if not bill:
        raise HTTPException(status_code=404, detail="Bill not found")
    if bill.status == "paid":
        raise HTTPException(status_code=409, detail="This bill is already paid")
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
    return {
        "restaurant_name": rest_name,
        "company_name": comp_name,
        "company_phone": comp_phone,
        "company_email": comp_email,
    }

def _restaurant_name() -> str:
    return _restaurant_profile()["restaurant_name"]

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
    if amount < 1:
        raise HTTPException(status_code=400, detail="This bill has nothing to pay")
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
    except Exception:
        raise HTTPException(status_code=502, detail="Could not start the payment. Please try again.")
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
    if bill.status == "paid":
        return {"status": "paid", "session_id": bill.id}
    phone = _normalize_my_phone(payload.get("phone") or "")
    email = str(payload.get("email") or "").strip()
    if email and ("@" not in email or "." not in email.split("@")[-1]):
        raise HTTPException(status_code=400, detail="Enter a valid email or leave it blank")
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

    if event.get("event") in ("payment.captured", "order.paid"):
        entity = ((event.get("payload") or {}).get("payment") or {}).get("entity") or {}
        payment_id = str(entity.get("id") or "").strip()
        order_id = str(entity.get("order_id") or "").strip()
        payment = entity
        if not verified_by_sig:
            if not payment_id:
                return {"status": "ignored"}
            try:
                payment = client.payment.fetch(payment_id)
                order_id = str(payment.get("order_id") or "").strip()
            except Exception:
                return {"status": "ignored"}
        bill = db.query(SessionBill).filter(SessionBill.curlec_order_id == order_id).first()
        if bill and bill.status != "paid" and str(payment.get("status")) == "captured":
            _record_captured_payment(bill, payment, db)
    return {"status": "ok"}

@app.post("/api/verify")
def verify(req: VerifyRequest, request: Request, db: Session = Depends(get_db)):
    _require_turnstile(req.cf_turnstile_response, "verify", request)
    cashier = db.query(Cashier).filter(Cashier.pin == req.pin, Cashier.active == True).first()
    if not cashier:
        raise HTTPException(401, "Invalid PIN")

    # Recalculate from corrections
    dish_map = {d.name: d for d in db.query(Dish).all()}
    bill_items = []
    total = 0.0
    for corr in req.corrections:
        name = corr["name"]
        qty = float(corr["quantity"])
        dish = dish_map.get(name)
        if dish and dish.available:
            sub = dish.price * qty
            total += sub
            bill_items.append({
                "name": name, "quantity": qty, "unit_price": dish.price,
                "subtotal": round(sub, 2)
            })

    target_bill = (
        db.query(SessionBill)
        .filter((SessionBill.id == req.session_id) | (SessionBill.bill_number == req.session_id.strip().upper()))
        .first()
    )
    if target_bill:
        target_bill.final_bill = json.dumps(bill_items)
        if target_bill.status == "needs_cashier":
            target_bill.status = "open"

    # Log
    log = AuditLog(cashier_id=cashier.id, action="verify_bill",
                   details=f"session={req.session_id} total={total:.2f}")
    db.add(log)
    db.commit()

    return {"items": bill_items, "total": round(total, 2), "verified": True}

@app.get("/api/cashiers")
def list_cashiers(db: Session = Depends(get_db)):
    return [{"id": c.id, "name": c.name, "active": c.active} for c in db.query(Cashier).all()]

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

    # Available months (last 12) and quarters (last 8) for selectors
    available_months = []
    for delta in range(0, -12, -1):
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
            }

    if p_norm == "today":
        days_int = 1
        start_date = today_date
        end_date = today_date
        granularity = "hour"
        period_label = "Today (Hourly)"
        file_tag = "today"
        for h in range(24):
            lbl = f"{h:02d}:00"
            _add_bucket(lbl, lbl)
    elif p_norm == "7d":
        days_int = 7
        start_date = today_date - timedelta(days=6)
        end_date = today_date
        granularity = "day"
        period_label = "Last 7 Days"
        file_tag = "last-7d"
        for i in range(6, -1, -1):
            d = today_date - timedelta(days=i)
            _add_bucket(d.isoformat(), d.strftime("%d %b"))
    elif p_norm == "30d":
        days_int = 30
        start_date = today_date - timedelta(days=29)
        end_date = today_date
        granularity = "day"
        period_label = "Last 30 Days"
        file_tag = "last-30d"
        for i in range(29, -1, -1):
            d = today_date - timedelta(days=i)
            _add_bucket(d.isoformat(), d.strftime("%d %b"))
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
                _add_bucket(d.isoformat(), d.strftime("%d %b"))
        else:
            oy, om = _shift_year_month(today_date.year, today_date.month, -11)
            start_date = date(oy, om, 1)
            end_date = today_date
            granularity = "month"
            period_label = "Monthly (Last 12 Months)"
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
            period_label = "Quarterly (Last 4 Quarters)"
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
        period_label = "All Time"
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
        return d_val.isoformat(), d_val.strftime("%d %b")

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
                d_name = str(item.get("name") or "Unknown").strip() or "Unknown"
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
                f"{i.get('name', '')} x{i.get('quantity', 0)}"
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
        "all_time_revenue": round(all_time_revenue, 2),
        "total_revenue": round(all_time_revenue, 2),
        "total_bills": len(rows),
        "paid_count": paid_count,
        "pending_count": pending_count,
        "void_count": void_count,
    }
    analytics = {
        "period": p_norm,
        "month": selected_month,
        "quarter": selected_quarter,
        "days": days_int,
        "period_label": period_label,
        "file_tag": file_tag,
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
    bills = [_bill_payload(s, refunded_amount=refunded_map.get(s.id, 0.0)) for s in page_rows]

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
    days: int = 7,
    period: str = "",
    month: str = "",
    quarter: str = "",
    db: Session = Depends(get_db),
):
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
    days: int = 7,
    period: str = "",
    month: str = "",
    quarter: str = "",
    db: Session = Depends(get_db),
):
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

    writer.writerow(["AFRC SALES & DASHBOARD OVERVIEW REPORT"])
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
    writer.writerow(["All-Time Revenue (RM)", f"{summary['all_time_revenue']:.2f}"])
    writer.writerow(["All-Time Total Bills", summary["total_bills"]])
    writer.writerow([])

    writer.writerow(["SALES TIMELINE BREAKDOWN"])
    time_col = "Hour" if analytics["granularity"] == "hour" else ("Month" if analytics["granularity"] == "month" else ("Quarter" if analytics["granularity"] == "quarter" else "Date"))
    writer.writerow([time_col, "Paid Revenue (RM)", "Paid Bills", "Pending Bills", "Voided Bills", "Total Bills"])
    for row in analytics["series"]:
        writer.writerow([
            row["label"] if analytics["granularity"] == "hour" else row["key"],
            f"{row['revenue']:.2f}",
            row["paid_count"],
            row["open_count"],
            row["void_count"],
            row["total_count"],
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
            b["bill_number"],
            b["created_at_local"],
            b["paid_at_local"],
            b["status"].upper(),
            b["void_reason"],
            f"{b['total']:.2f}",
            b["items"],
            b.get("receipt_phone", ""),
            b["receipt_email"],
            b["curlec_payment_id"],
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
def update_admin_bill_status(session_id: str, payload: dict, db: Session = Depends(get_db)):
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

@app.get("/api/admin/bills/{session_id}/refund-info")
def get_admin_bill_refund_info(session_id: str, db: Session = Depends(get_db)):
    bill = db.query(SessionBill).filter(SessionBill.id == session_id).first()
    if not bill:
        raise HTTPException(status_code=404, detail="Bill not found")
    return _build_refund_info(bill, db, sync_curlec_contact=True)

@app.post("/api/admin/bills/{session_id}/refund")
def refund_admin_bill(session_id: str, payload: dict, db: Session = Depends(get_db)):
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
        cashier_id=0,
        action="REFUND_ISSUED",
        details=(
            f"Bill #{bill.bill_number or bill.id}: RM {total_refund:.2f} "
            f"({', '.join(f'{i['quantity']}x {i['name']}' for i in refund_items_payload)})"
            + (f" [Curlec {curlec_refund_id}]" if curlec_refund_id else " [Manual]")
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

@app.get("/api/admin/audit-logs")
def list_audit_logs(db: Session = Depends(get_db)):
    cashier_map = {c.id: c.name for c in db.query(Cashier).all()}
    logs = db.query(AuditLog).order_by(AuditLog.timestamp.desc()).limit(200).all()
    return [
        {
            "id": l.id,
            "cashier_id": l.cashier_id,
            "cashier_name": cashier_map.get(l.cashier_id, f"Cashier #{l.cashier_id}"),
            "action": l.action,
            "details": l.details or "",
            "timestamp": (l.timestamp.isoformat() + "Z") if l.timestamp else "",
        }
        for l in logs
    ]
