from fastapi import FastAPI, UploadFile, File, HTTPException, Depends, Form, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from sqlalchemy import func
from sqlalchemy.orm import Session
from typing import Annotated, List
import asyncio
import math
import os
import uuid
import json
import hashlib
import httpx
import razorpay
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import quote

from .db import SessionLocal, init_db, Dish, Cashier, AuditLog, SessionBill, ClientCooldown, ScanAttempt, next_bill_number
from .schemas import Dish as DishSchema, BillResponse, BillItem, VerifyRequest, ErrorResponse
from .ai_service import recognize_food, RecognitionError

UPLOAD_DIR = Path(__file__).resolve().parent.parent / "uploads"
MAX_RETAKES = 4
COOLDOWN_MINUTES = int(os.getenv("COOLDOWN_MINUTES", "25"))
MAX_SCANS_PER_WINDOW = int(os.getenv("MAX_SCANS_PER_WINDOW", "5"))
MAX_SCANS_PER_IP = int(os.getenv("MAX_SCANS_PER_IP", "6"))
SCAN_WINDOW_MINUTES = int(os.getenv("SCAN_WINDOW_MINUTES", "15"))
BILL_EXPIRY_MINUTES = int(os.getenv("BILL_EXPIRY_MINUTES", "15"))
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
        async with httpx.AsyncClient(timeout=15.0) as client:
            for safe_chars in ("/", ""):
                url = f"https://api.cloudflare.com/client/v4/accounts/{account_id}/r2/buckets/{bucket}/objects/{quote(key, safe=safe_chars)}"
                resp = await client.put(url, content=image_bytes, headers=headers)
                if 200 <= resp.status_code < 300:
                    _register_r2_key(key)
                    return True
    except Exception:
        return False
    return False

async def _fetch_plate_from_r2(key: str):
    cfg = _r2_config()
    if not cfg or not key:
        return None
    account_id, api_token, bucket = cfg
    headers = {"Authorization": f"Bearer {api_token}"}
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            for safe_chars in ("/", ""):
                url = f"https://api.cloudflare.com/client/v4/accounts/{account_id}/r2/buckets/{bucket}/objects/{quote(key, safe=safe_chars)}"
                resp = await client.get(url, headers=headers)
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
        .filter(SessionBill.status.in_(("open", "pending", "retaking", "needs_cashier")))
        .all()
    )
    changed = False
    for bill in stale:
        if bill.status == "needs_cashier":
            bill.status = "void"
            bill.void_reason = bill.void_reason or "Needed Cashier Assistance"
            bill.updated_at = datetime.utcnow()
            changed = True
            continue
        last_touch = bill.updated_at or bill.created_at
        if last_touch and last_touch < cutoff:
            bill.status = "void"
            bill.void_reason = f"Ghosted / Stale Bill ({BILL_EXPIRY_MINUTES}m Timeout)"
            bill.updated_at = datetime.utcnow()
            changed = True
    if changed:
        db.commit()

def _scan_usage_status(
    request: Request,
    client_token: str,
    db: Session,
    session_id: str = "",
    device_id: str = "",
) -> dict:
    window_start = datetime.utcnow() - timedelta(minutes=SCAN_WINDOW_MINUTES)
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

    used = max(device_scans, session_scans)
    remaining = max(0, MAX_SCANS_PER_WINDOW - used)
    if ip_scans >= MAX_SCANS_PER_IP:
        remaining = 0
    return {
        "device_scans": device_scans,
        "session_scans": session_scans,
        "ip_scans": ip_scans,
        "scans_used": used,
        "scans_limit": MAX_SCANS_PER_WINDOW,
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
    until = datetime.utcnow() + timedelta(minutes=COOLDOWN_MINUTES)
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
    usage = _scan_usage_status(
        request,
        client_token=client_token,
        db=db,
        session_id=session_id,
        device_id=device_id,
    )
    reason = ""
    if usage["session_scans"] >= MAX_SCANS_PER_WINDOW:
        reason = f"Too many photo scan attempts in this session (max {MAX_SCANS_PER_WINDOW} per {SCAN_WINDOW_MINUTES} min)."
    elif usage["device_scans"] >= MAX_SCANS_PER_WINDOW:
        reason = f"Too many photo scan attempts from this device (max {MAX_SCANS_PER_WINDOW} per {SCAN_WINDOW_MINUTES} min)."
    elif usage["ip_scans"] >= MAX_SCANS_PER_IP:
        reason = f"Too many photo scan attempts from your network IP (max {MAX_SCANS_PER_IP} per {SCAN_WINDOW_MINUTES} min)."

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
                message=f"Your rate limit hit, try again after {COOLDOWN_MINUTES}min.",
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

def _sync_r2_objects() -> None:
    cfg = _r2_config()
    if not cfg:
        return
    account_id, api_token, bucket = cfg
    headers = {"Authorization": f"Bearer {api_token}", "Content-Type": "application/json"}
    base = f"https://api.cloudflare.com/client/v4/accounts/{account_id}/r2/buckets"
    try:
        check = httpx.get(f"{base}/{quote(bucket, safe='')}", headers=headers, timeout=10.0)
        if check.status_code == 404:
            httpx.post(
                base,
                headers=headers,
                json={"name": bucket, "locationHint": "apac"},
                timeout=10.0,
            )
        objs_resp = httpx.get(
            f"{base}/{quote(bucket, safe='')}/objects",
            headers=headers,
            timeout=10.0,
        )
        if 200 <= objs_resp.status_code < 300:
            data = objs_resp.json()
            for obj in (data.get("result") or []):
                key = str(obj.get("key") or "").strip()
                if key:
                    _register_r2_key(key)
            if _R2_SESSION_KEYS or _R2_BILL_KEYS:
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
    except Exception:
        pass

async def _stale_bills_watcher():
    while True:
        await asyncio.sleep(60)
        db = SessionLocal()
        try:
            _expire_stale_bills(db)
        except Exception:
            pass
        finally:
            db.close()

@app.on_event("startup")
async def startup():
    init_db()
    _sync_r2_objects()
    db = SessionLocal()
    try:
        _expire_stale_bills(db)
    finally:
        db.close()
    asyncio.create_task(_stale_bills_watcher())

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

@app.post("/api/clear-site-cache")
async def clear_site_cache():
    headers = dict(NO_CACHE_HEADERS)
    headers["Clear-Site-Data"] = '"cache"'
    return JSONResponse(content={"status": "cleared"}, headers=headers)

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
    _require_turnstile(cf_turnstile_response, "recognize", request)
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

    if target_bill and (target_bill.retake_count or 0) >= MAX_RETAKES:
        target_bill.status = "void"
        target_bill.void_reason = "Exceeded 4 Retries Limit"
        target_bill.updated_at = datetime.utcnow()
        db.commit()
        _set_cooldown(
            request,
            c_token or target_bill.client_token or "",
            db,
            session_id=target_bill.id,
            device_id=d_id,
            reason="Exceeded 4 photo retries limit.",
        )
        raise HTTPException(status_code=429, detail=ErrorResponse(
            error_code="RATE_LIMIT",
            message=f"Your rate limit hit, try again after {COOLDOWN_MINUTES}min."
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

def _bill_payload(session: SessionBill) -> dict:
    items = json.loads(session.final_bill or "[]")
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
    if status in ("void", "cancelled") and not void_reason:
        void_reason = (
            "Exceeded 4 Retries Limit"
            if retakes_used >= MAX_RETAKES
            else f"Ghosted / Stale Bill ({BILL_EXPIRY_MINUTES}m Timeout)"
        )
    return {
        "session_id": session.id,
        "bill_number": session.bill_number or "AAA-0001",
        "items": items,
        "total": total,
        "status": status,
        "void_reason": void_reason,
        "retake_count": retakes_used,
        "retakes_left": max(0, MAX_RETAKES - retakes_used),
        "max_retakes": MAX_RETAKES,
        "receipt_email": session.receipt_email or "",
        "curlec_order_id": session.curlec_order_id or "",
        "curlec_payment_id": session.curlec_payment_id or "",
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
    if (bill.retake_count or 0) >= MAX_RETAKES:
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
            "retakes_left": max(0, MAX_RETAKES - (bill.retake_count or 0)),
        },
        headers=NO_CACHE_HEADERS,
    )
    resp.set_cookie("afrc_client_id", c_token, max_age=86400 * 30, path="/", samesite="lax")
    resp.set_cookie("afrc_active_session", bill.id, max_age=3600, path="/", samesite="lax")
    return resp

@app.get("/api/sessions/{session_id}/image")
async def get_session_image(session_id: str, db: Session = Depends(get_db)):
    bill = db.query(SessionBill).filter(SessionBill.id == session_id).first()
    if not bill:
        raise HTTPException(status_code=404, detail="Bill not found")
    img_path = UPLOAD_DIR / f"{bill.id}.jpg"
    if img_path.is_file() and img_path.stat().st_size > 0:
        return FileResponse(img_path, media_type="image/jpeg", headers={"Cache-Control": "no-cache"})

    bill_no_upper = (bill.bill_number or "").upper()
    if bill.id not in _R2_SESSION_KEYS and bill_no_upper not in _R2_BILL_KEYS:
        _sync_r2_objects()

    candidate_keys = []
    for k in (
        bill.image_key,
        _R2_SESSION_KEYS.get(bill.id),
        f"plates/{bill.bill_number}_{bill.id}.jpg" if bill.bill_number else "",
        _R2_BILL_KEYS.get(bill_no_upper),
    ):
        if k and k not in candidate_keys:
            candidate_keys.append(k)

    for key in candidate_keys:
        r2_bytes = await _fetch_plate_from_r2(key)
        if r2_bytes:
            try:
                UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
                img_path.write_bytes(r2_bytes)
            except OSError:
                pass
            if not bill.image_key:
                bill.image_key = key
                db.commit()
            return Response(
                content=r2_bytes,
                media_type="image/jpeg",
                headers={"Cache-Control": "no-cache"},
            )
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
    body = payload or {}
    c_token = _get_client_token(request, body.get("client_token", "")) or bill.client_token or ""
    d_id = _get_device_id(request, explicit_device=body.get("device_id", ""), explicit_client=c_token)
    bill.status = "void"
    bill.void_reason = "Exceeded 4 Retries Limit"
    bill.updated_at = datetime.utcnow()
    db.commit()
    _set_cooldown(
        request,
        c_token,
        db,
        session_id=session_id,
        device_id=d_id,
        reason="Exceeded 4 photo retries limit.",
    )
    resp = JSONResponse(
        content={
            "status": "void",
            "void_reason": bill.void_reason,
            "cooldown_minutes": COOLDOWN_MINUTES,
            "message": f"Your rate limit hit, try again after {COOLDOWN_MINUTES}min.",
        },
        headers=NO_CACHE_HEADERS,
    )
    resp.delete_cookie("afrc_active_session", path="/")
    return resp

def _curlec_client():
    key_id = os.getenv("CURLEC_KEY_ID", "").strip()
    key_secret = os.getenv("CURLEC_KEY_SECRET", "").strip()
    if not key_id or not key_secret:
        raise HTTPException(status_code=503, detail="Payment gateway is not configured")
    return razorpay.Client(auth=(key_id, key_secret)), key_id

def _amount_sen(bill: SessionBill) -> int:
    return int(round(float(_bill_payload(bill)["total"]) * 100))

def _checkout_payload(bill: SessionBill, key_id: str) -> dict:
    return {
        "key_id": key_id,
        "order_id": bill.curlec_order_id,
        "amount": _amount_sen(bill),
        "currency": "MYR",
        "session_id": bill.id,
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
    email = str(payload.get("email") or "").strip()
    if email and ("@" not in email or "." not in email.split("@")[-1]):
        raise HTTPException(status_code=400, detail="Enter a valid email or leave it blank")
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

@app.get("/api/admin/bills")
def list_admin_bills(db: Session = Depends(get_db)):
    _expire_stale_bills(db)
    sessions = db.query(SessionBill).order_by(SessionBill.created_at.desc()).all()
    bills = [_bill_payload(s) for s in sessions]
    today_str = datetime.utcnow().strftime("%Y-%m-%d")
    total_revenue = round(sum(b["total"] for b in bills if b["status"] == "paid"), 2)
    today_revenue = round(
        sum(
            b["total"]
            for b in bills
            if b["status"] == "paid" and (b["paid_at"] or b["created_at"]).startswith(today_str)
        ),
        2,
    )
    return {
        "summary": {
            "total_revenue": total_revenue,
            "today_revenue": today_revenue,
            "total_bills": len(bills),
            "paid_count": sum(1 for b in bills if b["status"] == "paid"),
            "pending_count": sum(1 for b in bills if b["status"] in ("open", "pending")),
            "needs_cashier_count": sum(1 for b in bills if b["status"] == "needs_cashier"),
            "void_count": sum(1 for b in bills if b["status"] in ("void", "cancelled")),
        },
        "bills": bills,
    }

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
