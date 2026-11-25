"""
FastAPI Router for Customer Account (Google Auth), Guest Unclaimed Points,
Loyalty Rewards & Vouchers, and Admin Loyalty/Customer Management.
"""
from __future__ import annotations

import csv
import io
import math
import os
import re
from datetime import datetime, timedelta
from typing import Optional

from dotenv import dotenv_values
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from google.auth.transport import requests as google_requests
from google.oauth2 import id_token as google_id_token
from sqlalchemy import func, or_, text
from sqlalchemy.orm import Session

from backend.auth import require_admin_api
from backend.db import (
    AuditLog,
    BillRefund,
    CustomerUser,
    ENV_DIR,
    GuestUnclaimedSession,
    LoyaltySettings,
    PointRedemptionTier,
    PointsTransaction,
    ROOT,
    SessionBill,
    SessionLocal,
    Voucher,
)
from backend.loyalty import (
    CUSTOMER_SESSION_COOKIE,
    POINTS_PER_RM,
    POINTS_PER_RM_REDEMPTION_VALUE,
    _LOYALTY_LOCK,
    _begin_immediate_if_sqlite,
    claim_guest_unclaimed_points_for_user,
    clear_customer_session_cookie,
    clear_guest_unclaimed_session,
    clear_unclaimed_points_cookie,
    compute_voucher_discount,
    create_customer_session,
    format_debt_message,
    get_authoritative_guest_unclaimed,
    get_current_customer,
    get_loyalty_settings,
    get_or_create_guest_session_id,
    map_customer_profile,
    redeem_points_for_voucher,
    require_customer_api,
    revoke_all_customer_sessions,
    revoke_customer_session,
    set_customer_session_cookie,
    set_guest_loyalty_cookie,
    set_unclaimed_points_cookie,
    validate_voucher_for_user,
)

router = APIRouter()

NO_CACHE_HEADERS = {
    "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
    "Pragma": "no-cache",
    "Expires": "0",
    "X-Robots-Tag": "noindex, nofollow, noarchive, nosnippet",
}

# Rate limiters (in-memory per IP)
_GOOGLE_AUTH_ATTEMPTS: dict[str, list[float]] = {}
_VOUCHER_FAIL_ATTEMPTS: dict[str, dict] = {}


def _get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        parts = [p.strip() for p in forwarded.split(",") if p.strip()]
        if parts:
            return parts[0]
    real_ip = request.headers.get("x-real-ip", "").strip()
    if real_ip:
        return real_ip
    return request.client.host if request.client else ""


def _extract_client_token(request: Request, explicit: str = "") -> str:
    return (
        (explicit or "").strip()
        or request.cookies.get("afrc_client_id", "").strip()
        or request.headers.get("x-afrc-client", "").strip()
    )[:128]


def get_google_client_id() -> str:
    val = os.getenv("GOOGLE_CLIENT_ID", "").strip()
    if val and not val.startswith("your_google_"):
        return val
    for candidate in (
        ENV_DIR / "loyalty.env",
        ROOT / "loyalty.env",
        ENV_DIR / ".env",
        ROOT / ".env",
    ):
        if candidate.is_file():
            try:
                parsed = dotenv_values(candidate)
                cid = str(parsed.get("GOOGLE_CLIENT_ID") or "").strip()
                if cid and not cid.startswith("your_google_"):
                    return cid
            except Exception:
                pass
    return ""


def _check_google_auth_rate_limit(request: Request) -> None:
    ip = _client_ip(request)
    if not ip:
        return
    now = datetime.utcnow().timestamp()
    window = 15 * 60
    recent = [t for t in _GOOGLE_AUTH_ATTEMPTS.get(ip, []) if t > now - window]
    _GOOGLE_AUTH_ATTEMPTS[ip] = recent
    if len(recent) >= 20:
        raise HTTPException(
            status_code=429,
            detail="Too many authentication attempts. Please try again in a few minutes.",
        )
    recent.append(now)


def _check_voucher_rate_limit(ip: str) -> bool:
    if not ip:
        return True
    now = datetime.utcnow().timestamp()
    window = 90 * 60  # 1h 30m (matches takcakap.com-v2)
    rec = _VOUCHER_FAIL_ATTEMPTS.get(ip)
    if not rec or now > rec["reset"]:
        _VOUCHER_FAIL_ATTEMPTS[ip] = {"count": 0, "reset": now + window}
        return True
    return rec["count"] < 11


def _record_voucher_failure(ip: str) -> None:
    if not ip:
        return
    rec = _VOUCHER_FAIL_ATTEMPTS.get(ip)
    if rec:
        rec["count"] += 1


def _normalize_optional_my_phone(raw: str) -> str:
    s = (raw or "").strip()
    if not s:
        return ""
    digits = "".join(ch for ch in s if ch.isdigit())
    if digits.startswith("60"):
        digits = digits[2:]
    elif digits.startswith("0"):
        digits = digits[1:]
    if len(digits) < 9 or len(digits) > 10:
        raise HTTPException(
            status_code=400,
            detail="Enter a valid Malaysian phone number (e.g. +60123456789) or leave blank.",
        )
    return f"+60{digits}"


def _csv_safe(val) -> str:
    s = str(val if val is not None else "")
    if s and s[0] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + s
    return s


# ==============================================================================
# Frontend Page Route (/account and /loyalty)
# ==============================================================================

@router.get("/account")
@router.get("/loyalty")
async def account_page(request: Request):
    gsid, is_new = get_or_create_guest_session_id(request)
    resp = FileResponse("frontend/account.html", headers=NO_CACHE_HEADERS)
    if is_new:
        set_guest_loyalty_cookie(resp, request, gsid)
    return resp


# ==============================================================================
# Public Config & Unified Loyalty / Guest Unclaimed Status
# ==============================================================================

@router.get("/api/config/google")
def get_google_config():
    cid = get_google_client_id()
    return {
        "googleClientId": cid,
        "enabled": bool(cid),
    }


@router.get("/api/loyalty/status")
def get_loyalty_status(
    request: Request,
    session_id: str = "",
    db: Session = Depends(_get_db),
):
    gsid, is_new_guest = get_or_create_guest_session_id(request)
    ctok = _extract_client_token(request)
    cid = get_google_client_id()
    user = get_current_customer(request, db)

    claimed_now = None
    if user:
        # If user is already signed in and has any lingering unclaimed guest bill for this browser, auto-claim it!
        auto_claim = claim_guest_unclaimed_points_for_user(
            db=db,
            user_id=user.id,
            guest_session_id=gsid,
            client_token=ctok,
            explicit_session_id=session_id,
        )
        if auto_claim.get("claimed"):
            db.refresh(user)
            claimed_now = auto_claim

        payload = {
            "authenticated": True,
            "google_client_id": cid,
            "points_per_rm": POINTS_PER_RM,
            "user": map_customer_profile(user),
            "unclaimed": {
                "has_unclaimed": False,
                "points": 0,
                "bill_id": "",
                "bill_number": "",
                "paid_at": "",
            },
            "auto_claimed": claimed_now,
        }
        resp = JSONResponse(content=payload, headers=NO_CACHE_HEADERS)
        if is_new_guest:
            set_guest_loyalty_cookie(resp, request, gsid)
        if request.cookies.get("afrc_unclaimed_pts"):
            clear_unclaimed_points_cookie(resp)
        return resp

    unclaimed = get_authoritative_guest_unclaimed(db, gsid, client_token=ctok)
    payload = {
        "authenticated": False,
        "google_client_id": cid,
        "points_per_rm": POINTS_PER_RM,
        "user": None,
        "unclaimed": unclaimed,
        "auto_claimed": None,
    }
    resp = JSONResponse(content=payload, headers=NO_CACHE_HEADERS)
    set_guest_loyalty_cookie(resp, request, gsid)
    if unclaimed["has_unclaimed"]:
        set_unclaimed_points_cookie(
            resp,
            request,
            gsid,
            unclaimed["bill_id"],
            unclaimed["points"],
        )
    elif request.cookies.get("afrc_unclaimed_pts"):
        clear_unclaimed_points_cookie(resp)
    return resp


@router.post("/api/loyalty/guest/clear")
def reset_guest_unclaimed_session(
    request: Request,
    db: Session = Depends(_get_db),
):
    gsid, _ = get_or_create_guest_session_id(request)
    ctok = _extract_client_token(request)
    cleared_count = clear_guest_unclaimed_session(db, gsid, client_token=ctok)
    # Rotate guest session ID so browser starts completely fresh
    fresh_gsid, _ = get_or_create_guest_session_id(Request({"type": "http", "headers": [], "scheme": request.url.scheme}))
    resp = JSONResponse(
        content={"ok": True, "cleared_count": cleared_count},
        headers=NO_CACHE_HEADERS,
    )
    set_guest_loyalty_cookie(resp, request, fresh_gsid)
    clear_unclaimed_points_cookie(resp)
    return resp


@router.get("/api/loyalty/tiers")
@router.get("/api/user/points/tiers")
def list_active_redemption_tiers(db: Session = Depends(_get_db)):
    tiers = (
        db.query(PointRedemptionTier)
        .filter(PointRedemptionTier.is_active == True)
        .order_by(PointRedemptionTier.points_required.asc())
        .all()
    )
    return [
        {
            "id": t.id,
            "points_required": int(t.points_required),
            "discount_amount": round(float(t.discount_amount), 2),
        }
        for t in tiers
    ]


# ==============================================================================
# Customer Google Sign-In / Registration & Profile Management
# ==============================================================================

def verify_google_credential_token(credential: str, expected_client_id: str) -> dict:
    """
    Verifies a Google Identity Services ID token against GOOGLE_CLIENT_ID.
    Separated as a helper so unit/integration tests can also test token edge cases cleanly.
    """
    if not expected_client_id:
        raise HTTPException(
            status_code=503,
            detail="Google Sign-In is not configured on this server (missing GOOGLE_CLIENT_ID).",
        )
    if not credential:
        raise HTTPException(status_code=400, detail="Missing Google credential token.")

    try:
        payload = google_id_token.verify_oauth2_token(
            credential,
            google_requests.Request(),
            audience=expected_client_id,
            clock_skew_in_seconds=15,
        )
    except Exception as exc:
        raise HTTPException(status_code=401, detail=f"Google authentication failed: invalid token ({exc})")

    sub = str(payload.get("sub") or "").strip()
    email = str(payload.get("email") or "").strip().lower()
    if not sub or not email:
        raise HTTPException(status_code=400, detail="Invalid Google account payload.")
    if payload.get("email_verified") is False:
        raise HTTPException(status_code=400, detail="Google account email is not verified.")

    return {
        "google_id": sub,
        "email": email,
        "name": str(payload.get("name") or email.split("@")[0] or "Customer").strip()[:255],
        "avatar_url": str(payload.get("picture") or "").strip()[:512],
    }


@router.post("/api/customer/auth/google")
@router.post("/api/auth/google")
def customer_google_sign_in(
    payload: dict,
    request: Request,
    db: Session = Depends(_get_db),
):
    _check_google_auth_rate_limit(request)
    credential = str((payload or {}).get("credential") or "").strip()
    explicit_session_id = str((payload or {}).get("session_id") or "").strip()
    client_id = get_google_client_id()

    g_info = verify_google_credential_token(credential, client_id)
    google_id = g_info["google_id"]
    email = g_info["email"]
    name = g_info["name"]
    avatar_url = g_info["avatar_url"]

    now = datetime.utcnow()
    with _LOYALTY_LOCK:
        user = db.query(CustomerUser).filter(CustomerUser.google_id == google_id).first()
        if not user:
            user = db.query(CustomerUser).filter(CustomerUser.email == email).first()

        if user and user.is_suspended:
            raise HTTPException(
                status_code=403,
                detail="Your account is suspended. Please contact support.",
            )

        is_new_account = False
        if user:
            user.google_id = google_id
            user.email = email
            if name and (not user.name or user.name == "Customer"):
                user.name = name
            if avatar_url:
                user.avatar_url = avatar_url
            user.last_login_at = now
            user.updated_at = now
            db.commit()
            db.refresh(user)
        else:
            is_new_account = True
            user = CustomerUser(
                google_id=google_id,
                email=email,
                name=name or "Customer",
                avatar_url=avatar_url,
                phone_number="",
                points_balance=0,
                is_suspended=False,
                created_at=now,
                updated_at=now,
                last_login_at=now,
            )
            db.add(user)
            db.commit()
            db.refresh(user)

    # Create authenticated customer session
    ip = _client_ip(request)
    ua = request.headers.get("user-agent", "")
    sess_token = create_customer_session(db, user.id, ip=ip, user_agent=ua)

    # Automatically synchronize & transfer eligible unclaimed guest points into this account
    gsid, _ = get_or_create_guest_session_id(request)
    ctok = _extract_client_token(request, str((payload or {}).get("client_token") or ""))
    claim_result = claim_guest_unclaimed_points_for_user(
        db=db,
        user_id=user.id,
        guest_session_id=gsid,
        client_token=ctok,
        explicit_session_id=explicit_session_id,
    )
    db.refresh(user)

    resp = JSONResponse(
        content={
            "ok": True,
            "is_new_account": is_new_account,
            "token": sess_token,
            "user": map_customer_profile(user),
            "claimed": claim_result,
        },
        headers=NO_CACHE_HEADERS,
    )
    set_customer_session_cookie(resp, request, sess_token)
    set_guest_loyalty_cookie(resp, request, gsid)
    clear_unclaimed_points_cookie(resp)
    return resp


@router.post("/api/customer/auth/logout")
def customer_logout(request: Request, db: Session = Depends(_get_db)):
    token = request.cookies.get(CUSTOMER_SESSION_COOKIE, "").strip()
    if token:
        revoke_customer_session(db, token)
    resp = JSONResponse(content={"ok": True}, headers=NO_CACHE_HEADERS)
    clear_customer_session_cookie(resp)
    return resp


@router.get("/api/customer/me")
@router.get("/api/auth/me")
def get_customer_me(request: Request, db: Session = Depends(_get_db)):
    user = require_customer_api(request, db)
    return {"user": map_customer_profile(user)}


@router.put("/api/customer/profile")
@router.put("/api/account")
def update_customer_profile(
    payload: dict,
    request: Request,
    db: Session = Depends(_get_db),
):
    user = require_customer_api(request, db)
    if "name" in (payload or {}):
        new_name = str(payload.get("name") or "").strip()
        if not new_name or len(new_name) < 2:
            raise HTTPException(status_code=400, detail="Display name must be at least 2 characters.")
        if len(new_name) > 80:
            raise HTTPException(status_code=400, detail="Display name must be 80 characters or fewer.")
        user.name = new_name

    phone_key = "phone_number" if "phone_number" in (payload or {}) else ("phoneNumber" if "phoneNumber" in (payload or {}) else None)
    if phone_key is not None:
        user.phone_number = _normalize_optional_my_phone(str(payload.get(phone_key) or ""))

    user.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(user)
    return {"success": True, "user": map_customer_profile(user)}


# ==============================================================================
# Customer Loyalty Points, Redemption, Vouchers & Order History
# ==============================================================================

@router.get("/api/customer/points")
@router.get("/api/user/points")
def get_customer_points(
    request: Request,
    page: int = 1,
    limit: int = 15,
    db: Session = Depends(_get_db),
):
    user = require_customer_api(request, db)
    safe_page = max(1, int(page or 1))
    safe_limit = min(50, max(1, int(limit or 15)))
    offset = (safe_page - 1) * safe_limit

    balance = int(user.points_balance or 0)
    debt = abs(balance) if balance < 0 else 0

    total_txns = (
        db.query(func.count(PointsTransaction.id))
        .filter(PointsTransaction.user_id == user.id)
        .scalar()
        or 0
    )
    rows = (
        db.query(PointsTransaction)
        .filter(PointsTransaction.user_id == user.id)
        .order_by(PointsTransaction.created_at.desc(), PointsTransaction.id.desc())
        .offset(offset)
        .limit(safe_limit)
        .all()
    )

    settings = get_loyalty_settings(db)
    cooldown_hours = int(settings.voucher_cooldown_hours or 0)
    cooldown_ends_iso: Optional[str] = None
    if cooldown_hours > 0:
        last_v = (
            db.query(Voucher)
            .filter(Voucher.user_id == user.id, Voucher.source == "points_redemption")
            .order_by(Voucher.created_at.desc())
            .first()
        )
        if last_v and last_v.created_at:
            ends = last_v.created_at + timedelta(hours=cooldown_hours)
            if ends > datetime.utcnow():
                cooldown_ends_iso = ends.isoformat() + "Z"

    total_saved = (
        db.query(func.coalesce(func.sum(SessionBill.discount_amount), 0.0))
        .filter(
            SessionBill.customer_user_id == user.id,
            SessionBill.status == "paid",
            SessionBill.discount_amount > 0,
            SessionBill.voucher_restored_at == None,
        )
        .scalar()
        or 0.0
    )

    return {
        "balance": balance,
        "debtPoints": debt,
        "debtMessage": format_debt_message(debt),
        "estValueRM": round(max(0, balance) / POINTS_PER_RM_REDEMPTION_VALUE, 2),
        "totalSaved": round(float(total_saved), 2),
        "cooldownLimitHours": cooldown_hours,
        "cooldownEndsAt": cooldown_ends_iso,
        "totalTransactions": int(total_txns),
        "page": safe_page,
        "limit": safe_limit,
        "transactions": [
            {
                "id": r.id,
                "points": int(r.points),
                "transaction_type": r.transaction_type,
                "reference_id": r.reference_id or "",
                "bill_number": r.bill_number or "",
                "source": r.source or "",
                "description": r.description,
                "created_at": (r.created_at.isoformat() + "Z") if r.created_at else "",
            }
            for r in rows
        ],
    }


@router.post("/api/customer/points/redeem")
@router.post("/api/user/points/redeem")
def redeem_customer_points(
    payload: dict,
    request: Request,
    db: Session = Depends(_get_db),
):
    user = require_customer_api(request, db)
    try:
        pts = int((payload or {}).get("points") or 0)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="Invalid points amount.")
    return redeem_points_for_voucher(db, user.id, pts)


@router.get("/api/customer/vouchers")
@router.get("/api/user/vouchers")
def get_customer_vouchers(
    request: Request,
    activeOnly: str = "false",
    db: Session = Depends(_get_db),
):
    user = require_customer_api(request, db)
    now = datetime.utcnow()
    q = db.query(Voucher).filter(Voucher.user_id == user.id, Voucher.is_archived == False)
    if str(activeOnly).strip().lower() in ("true", "1", "yes"):
        q = q.filter(
            Voucher.is_active == True,
            Voucher.times_used < Voucher.max_uses,
            or_(Voucher.expires_at == None, Voucher.expires_at > now),
        )
    rows = q.order_by(Voucher.created_at.desc()).limit(200).all()
    return [
        {
            "id": v.id,
            "code": v.code,
            "type": v.type,
            "discount_value": round(float(v.discount_value), 2),
            "min_spend": round(float(v.min_spend or 0.0), 2),
            "max_discount": round(float(v.max_discount), 2) if v.max_discount is not None else None,
            "expires_at": (v.expires_at.isoformat() + "Z") if v.expires_at else None,
            "is_active": bool(v.is_active),
            "is_redeemed": bool(v.is_redeemed or (v.times_used >= v.max_uses)),
            "is_expired": bool(v.expires_at and v.expires_at < now),
            "max_uses": int(v.max_uses or 1),
            "times_used": int(v.times_used or 0),
            "source": v.source,
            "created_at": (v.created_at.isoformat() + "Z") if v.created_at else "",
        }
        for v in rows
    ]


@router.post("/api/customer/vouchers/validate")
def validate_customer_voucher_code(
    payload: dict,
    request: Request,
    db: Session = Depends(_get_db),
):
    user = require_customer_api(request, db)
    ip = _client_ip(request)
    if not _check_voucher_rate_limit(ip):
        raise HTTPException(
            status_code=429,
            detail="Too many invalid voucher attempts. Please try again in 1 hour 30 minutes.",
        )

    code = str((payload or {}).get("code") or "").strip()
    try:
        subtotal = float((payload or {}).get("subtotal") or 0.0)
    except (TypeError, ValueError):
        subtotal = 0.0

    try:
        voucher, discount = validate_voucher_for_user(db, user, code, subtotal)
    except HTTPException as exc:
        if exc.status_code in (400, 404) and "Invalid voucher" in str(exc.detail):
            _record_voucher_failure(ip)
        raise

    _VOUCHER_FAIL_ATTEMPTS.pop(ip, None)
    return {
        "valid": True,
        "discount_amount": discount,
        "voucher": {
            "id": voucher.id,
            "code": voucher.code,
            "type": voucher.type,
            "discount_value": round(float(voucher.discount_value), 2),
            "min_spend": round(float(voucher.min_spend or 0.0), 2),
            "max_discount": round(float(voucher.max_discount), 2) if voucher.max_discount is not None else None,
            "expires_at": (voucher.expires_at.isoformat() + "Z") if voucher.expires_at else None,
        },
    }


@router.get("/api/customer/orders")
def get_customer_order_history(
    request: Request,
    page: int = 1,
    limit: int = 15,
    db: Session = Depends(_get_db),
):
    import json as _json

    user = require_customer_api(request, db)
    safe_page = max(1, int(page or 1))
    safe_limit = min(50, max(1, int(limit or 15)))
    offset = (safe_page - 1) * safe_limit

    base_q = db.query(SessionBill).filter(
        SessionBill.customer_user_id == user.id,
        SessionBill.status == "paid",
    )
    total_orders = base_q.count()
    bills = (
        base_q.order_by(SessionBill.paid_at.desc(), SessionBill.created_at.desc())
        .offset(offset)
        .limit(safe_limit)
        .all()
    )

    bill_ids = [b.id for b in bills]
    refund_sums: dict[str, float] = {}
    if bill_ids:
        rf_rows = (
            db.query(BillRefund.session_id, func.coalesce(func.sum(BillRefund.refund_amount), 0.0))
            .filter(BillRefund.session_id.in_(bill_ids))
            .group_by(BillRefund.session_id)
            .all()
        )
        refund_sums = {sid: float(amt or 0.0) for sid, amt in rf_rows}

    orders_out = []
    for b in bills:
        try:
            items = _json.loads(b.final_bill or "[]")
        except Exception:
            items = []
        subtotal = round(sum(float(it.get("subtotal") or 0.0) for it in items), 2)
        discount = round(float(b.discount_amount or 0.0), 2)
        paid_total = round(max(0.0, subtotal - discount), 2)
        refunded = round(refund_sums.get(b.id, 0.0), 2)
        earned = int(b.points_earned or 0)
        clawed = int(b.points_clawed_back or 0)
        orders_out.append({
            "session_id": b.id,
            "bill_number": b.bill_number or "",
            "items": items,
            "subtotal": subtotal,
            "discount_amount": discount,
            "voucher_code": b.voucher_code or "",
            "total": paid_total,
            "refunded_amount": refunded,
            "points_earned": earned,
            "points_clawed_back": clawed,
            "net_points": earned - clawed,
            "points_status": b.points_status or "none",
            "paid_at": (b.paid_at.isoformat() + "Z") if b.paid_at else "",
            "created_at": (b.created_at.isoformat() + "Z") if b.created_at else "",
        })

    return {
        "total": total_orders,
        "page": safe_page,
        "limit": safe_limit,
        "orders": orders_out,
    }


# ==============================================================================
# Admin Loyalty, Vouchers & Customer Management APIs
# ==============================================================================

@router.get("/api/admin/loyalty/overview")
def admin_loyalty_overview(request: Request, db: Session = Depends(_get_db)):
    require_admin_api(request, db)
    now = datetime.utcnow()
    week_ago = now - timedelta(days=7)

    circulating = (
        db.query(func.coalesce(func.sum(CustomerUser.points_balance), 0))
        .filter(CustomerUser.points_balance > 0)
        .scalar()
        or 0
    )
    net_ledger = db.query(func.coalesce(func.sum(PointsTransaction.points), 0)).scalar() or 0

    total_customers = db.query(func.count(CustomerUser.id)).scalar() or 0
    suspended_customers = (
        db.query(func.count(CustomerUser.id))
        .filter(CustomerUser.is_suspended == True)
        .scalar()
        or 0
    )
    new_this_week = (
        db.query(func.count(CustomerUser.id))
        .filter(CustomerUser.created_at >= week_ago)
        .scalar()
        or 0
    )
    active_7d = (
        db.query(func.count(func.distinct(SessionBill.customer_user_id)))
        .filter(
            SessionBill.customer_user_id != None,
            SessionBill.status == "paid",
            SessionBill.paid_at >= week_ago,
        )
        .scalar()
        or 0
    )

    unclaimed_rows = (
        db.query(
            func.count(SessionBill.id),
            func.coalesce(func.sum(SessionBill.points_earned - SessionBill.points_clawed_back), 0),
        )
        .filter(SessionBill.status == "paid", SessionBill.points_status == "unclaimed")
        .first()
    )
    unclaimed_bills = int(unclaimed_rows[0] or 0) if unclaimed_rows else 0
    unclaimed_pts = int(unclaimed_rows[1] or 0) if unclaimed_rows else 0

    # Local timezone (UTC+8) boundaries for voucher redemption savings
    tz_offset = timedelta(hours=float(os.getenv("TZ_OFFSET_HOURS", "8") or 8))
    now_local = now + tz_offset
    today_start_utc = datetime(now_local.year, now_local.month, now_local.day) - tz_offset
    week_start_local = (now_local - timedelta(days=now_local.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    week_start_utc = week_start_local - tz_offset
    month_start_utc = datetime(now_local.year, now_local.month, 1) - tz_offset

    def _voucher_sum_since(dt_utc: datetime) -> float:
        val = (
            db.query(func.coalesce(func.sum(SessionBill.discount_amount), 0.0))
            .filter(
                SessionBill.status == "paid",
                SessionBill.voucher_id != None,
                SessionBill.discount_amount > 0,
                SessionBill.voucher_restored_at == None,
                SessionBill.paid_at >= dt_utc,
            )
            .scalar()
            or 0.0
        )
        return round(float(val), 2)

    settings = get_loyalty_settings(db)
    return {
        "circulating_points": int(circulating),
        "net_ledger_points": int(net_ledger),
        "est_value_rm": round(max(0, int(circulating)) / POINTS_PER_RM_REDEMPTION_VALUE, 2),
        "total_customers": int(total_customers),
        "active_customers_7d": int(active_7d),
        "suspended_customers": int(suspended_customers),
        "new_this_week": int(new_this_week),
        "unclaimed_bills_count": unclaimed_bills,
        "unclaimed_points_total": max(0, unclaimed_pts),
        "voucher_cooldown_hours": int(settings.voucher_cooldown_hours or 0),
        "voucher_stats": {
            "today": _voucher_sum_since(today_start_utc),
            "this_week": _voucher_sum_since(week_start_utc),
            "this_month": _voucher_sum_since(month_start_utc),
        },
    }


@router.get("/api/admin/loyalty/settings")
def admin_get_loyalty_settings(request: Request, db: Session = Depends(_get_db)):
    require_admin_api(request, db)
    s = get_loyalty_settings(db)
    return {"voucher_cooldown_hours": int(s.voucher_cooldown_hours or 0)}


@router.put("/api/admin/loyalty/settings")
def admin_update_loyalty_settings(
    payload: dict,
    request: Request,
    db: Session = Depends(_get_db),
):
    admin = require_admin_api(request, db)
    try:
        hours = int((payload or {}).get("voucher_cooldown_hours") or 0)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="Cooldown hours must be a whole number >= 0.")
    if hours < 0 or hours > 720:
        raise HTTPException(status_code=400, detail="Cooldown hours must be between 0 and 720.")

    s = get_loyalty_settings(db)
    s.voucher_cooldown_hours = hours
    s.updated_at = datetime.utcnow()
    db.add(
        AuditLog(
            cashier_id=admin.id,
            admin_username=admin.username,
            action="LOYALTY_SETTINGS_UPDATE",
            details=f"Admin '{admin.username}' updated voucher cooldown to {hours}h",
        )
    )
    db.commit()
    return {"success": True, "voucher_cooldown_hours": hours}


@router.get("/api/admin/loyalty/tiers")
def admin_list_tiers(request: Request, db: Session = Depends(_get_db)):
    require_admin_api(request, db)
    rows = db.query(PointRedemptionTier).order_by(PointRedemptionTier.points_required.asc()).all()
    return [
        {
            "id": r.id,
            "points_required": int(r.points_required),
            "discount_amount": round(float(r.discount_amount), 2),
            "is_active": bool(r.is_active),
            "created_at": (r.created_at.isoformat() + "Z") if r.created_at else "",
        }
        for r in rows
    ]


@router.post("/api/admin/loyalty/tiers")
def admin_create_tier(
    payload: dict,
    request: Request,
    db: Session = Depends(_get_db),
):
    admin = require_admin_api(request, db)
    try:
        pts = int((payload or {}).get("points_required") or 0)
        disc = round(float((payload or {}).get("discount_amount") or 0.0), 2)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="Invalid tier points or discount amount.")
    if pts < 1 or disc < 0.10:
        raise HTTPException(status_code=400, detail="Points must be >= 1 and discount >= RM 0.10.")

    dup = db.query(PointRedemptionTier).filter(PointRedemptionTier.points_required == pts).first()
    if dup:
        raise HTTPException(status_code=400, detail=f"A tier for {pts} points already exists.")

    is_act = bool((payload or {}).get("is_active", True))
    tier = PointRedemptionTier(points_required=pts, discount_amount=disc, is_active=is_act)
    db.add(tier)
    db.add(
        AuditLog(
            cashier_id=admin.id,
            admin_username=admin.username,
            action="LOYALTY_TIER_CREATE",
            details=f"Created tier: {pts} pts -> RM {disc:.2f} (active={is_act})",
        )
    )
    db.commit()
    db.refresh(tier)
    return {"success": True, "id": tier.id}


@router.put("/api/admin/loyalty/tiers/{tier_id}")
def admin_update_tier(
    tier_id: int,
    payload: dict,
    request: Request,
    db: Session = Depends(_get_db),
):
    admin = require_admin_api(request, db)
    tier = db.query(PointRedemptionTier).filter(PointRedemptionTier.id == tier_id).first()
    if not tier:
        raise HTTPException(status_code=404, detail="Tier not found.")

    try:
        pts = int((payload or {}).get("points_required", tier.points_required))
        disc = round(float((payload or {}).get("discount_amount", tier.discount_amount)), 2)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="Invalid tier values.")
    if pts < 1 or disc < 0.10:
        raise HTTPException(status_code=400, detail="Points must be >= 1 and discount >= RM 0.10.")

    dup = (
        db.query(PointRedemptionTier)
        .filter(PointRedemptionTier.points_required == pts, PointRedemptionTier.id != tier_id)
        .first()
    )
    if dup:
        raise HTTPException(status_code=400, detail=f"Another tier already uses {pts} points.")

    tier.points_required = pts
    tier.discount_amount = disc
    if "is_active" in (payload or {}):
        tier.is_active = bool(payload["is_active"])

    db.add(
        AuditLog(
            cashier_id=admin.id,
            admin_username=admin.username,
            action="LOYALTY_TIER_UPDATE",
            details=f"Updated tier #{tier_id}: {pts} pts -> RM {disc:.2f} (active={tier.is_active})",
        )
    )
    db.commit()
    return {"success": True}


@router.delete("/api/admin/loyalty/tiers/{tier_id}")
def admin_delete_tier(
    tier_id: int,
    request: Request,
    db: Session = Depends(_get_db),
):
    admin = require_admin_api(request, db)
    tier = db.query(PointRedemptionTier).filter(PointRedemptionTier.id == tier_id).first()
    if not tier:
        raise HTTPException(status_code=404, detail="Tier not found.")
    desc = f"{tier.points_required} pts -> RM {tier.discount_amount:.2f}"
    db.delete(tier)
    db.add(
        AuditLog(
            cashier_id=admin.id,
            admin_username=admin.username,
            action="LOYALTY_TIER_DELETE",
            details=f"Deleted tier #{tier_id} ({desc})",
        )
    )
    db.commit()
    return {"success": True}


@router.get("/api/admin/loyalty/customers")
def admin_list_customers(
    request: Request,
    search: str = "",
    status: str = "all",
    sort: str = "recent",
    page: int = 1,
    limit: int = 15,
    db: Session = Depends(_get_db),
):
    import json as _json

    require_admin_api(request, db)
    safe_page = max(1, int(page or 1))
    safe_limit = min(100, max(1, int(limit or 15)))
    offset = (safe_page - 1) * safe_limit

    q = db.query(CustomerUser)
    st = (status or "all").strip().lower()
    if st == "active":
        q = q.filter(CustomerUser.is_suspended == False)
    elif st == "suspended":
        q = q.filter(CustomerUser.is_suspended == True)
    elif st == "debt":
        q = q.filter(CustomerUser.points_balance < 0)

    s_term = (search or "").strip()
    if s_term:
        like = f"%{s_term}%"
        q = q.filter(
            or_(
                CustomerUser.name.ilike(like),
                CustomerUser.email.ilike(like),
                CustomerUser.phone_number.ilike(like),
            )
        )

    total = q.count()
    if sort == "points":
        q = q.order_by(CustomerUser.points_balance.desc(), CustomerUser.id.desc())
    else:
        q = q.order_by(CustomerUser.created_at.desc(), CustomerUser.id.desc())

    users = q.offset(offset).limit(safe_limit).all()
    uids = [u.id for u in users]

    bill_stats: dict[int, dict] = {uid: {"count": 0, "spent": 0.0, "last_at": ""} for uid in uids}
    if uids:
        paid_bills = (
            db.query(SessionBill)
            .filter(SessionBill.customer_user_id.in_(uids), SessionBill.status == "paid")
            .all()
        )
        b_ids = [b.id for b in paid_bills]
        rf_map: dict[str, float] = {}
        if b_ids:
            for sid, amt in (
                db.query(BillRefund.session_id, func.coalesce(func.sum(BillRefund.refund_amount), 0.0))
                .filter(BillRefund.session_id.in_(b_ids))
                .group_by(BillRefund.session_id)
                .all()
            ):
                rf_map[sid] = float(amt or 0.0)

        for b in paid_bills:
            uid = int(b.customer_user_id)
            if uid not in bill_stats:
                continue
            try:
                items = _json.loads(b.final_bill or "[]")
            except Exception:
                items = []
            sub = sum(float(it.get("subtotal") or 0.0) for it in items)
            net = max(0.0, sub - float(b.discount_amount or 0.0) - rf_map.get(b.id, 0.0))
            bill_stats[uid]["count"] += 1
            bill_stats[uid]["spent"] += net
            ts = (b.paid_at or b.created_at)
            if ts:
                iso = ts.isoformat() + "Z"
                if iso > bill_stats[uid]["last_at"]:
                    bill_stats[uid]["last_at"] = iso

    return {
        "total": total,
        "page": safe_page,
        "limit": safe_limit,
        "total_pages": max(1, math.ceil(total / safe_limit)),
        "customers": [
            {
                "id": u.id,
                "name": u.name or "Customer",
                "email": u.email or "",
                "phone_number": u.phone_number or "",
                "avatar_url": u.avatar_url or "",
                "points_balance": int(u.points_balance or 0),
                "is_suspended": bool(u.is_suspended),
                "suspended_at": (u.suspended_at.isoformat() + "Z") if u.suspended_at else None,
                "created_at": (u.created_at.isoformat() + "Z") if u.created_at else "",
                "last_login_at": (u.last_login_at.isoformat() + "Z") if u.last_login_at else "",
                "order_count": bill_stats[u.id]["count"],
                "total_spent": round(bill_stats[u.id]["spent"], 2),
                "last_order_at": bill_stats[u.id]["last_at"],
            }
            for u in users
        ],
    }


@router.get("/api/admin/loyalty/customers/{customer_id}")
def admin_get_customer_detail(
    customer_id: int,
    request: Request,
    db: Session = Depends(_get_db),
):
    import json as _json

    require_admin_api(request, db)
    u = db.query(CustomerUser).filter(CustomerUser.id == customer_id).first()
    if not u:
        raise HTTPException(status_code=404, detail="Customer not found.")

    txns = (
        db.query(PointsTransaction)
        .filter(PointsTransaction.user_id == u.id)
        .order_by(PointsTransaction.created_at.desc(), PointsTransaction.id.desc())
        .limit(40)
        .all()
    )
    bills = (
        db.query(SessionBill)
        .filter(SessionBill.customer_user_id == u.id, SessionBill.status == "paid")
        .order_by(SessionBill.paid_at.desc(), SessionBill.created_at.desc())
        .limit(30)
        .all()
    )

    orders_list = []
    for b in bills:
        try:
            items = _json.loads(b.final_bill or "[]")
        except Exception:
            items = []
        sub = round(sum(float(it.get("subtotal") or 0.0) for it in items), 2)
        disc = round(float(b.discount_amount or 0.0), 2)
        orders_list.append({
            "session_id": b.id,
            "bill_number": b.bill_number or "",
            "total": round(max(0.0, sub - disc), 2),
            "discount_amount": disc,
            "voucher_code": b.voucher_code or "",
            "points_earned": int(b.points_earned or 0),
            "points_clawed_back": int(b.points_clawed_back or 0),
            "items_summary": ", ".join(f"{it.get('quantity', 1)}x {it.get('name', '')}" for it in items),
            "paid_at": (b.paid_at.isoformat() + "Z") if b.paid_at else "",
        })

    return {
        "customer": map_customer_profile(u),
        "transactions": [
            {
                "id": t.id,
                "points": int(t.points),
                "transaction_type": t.transaction_type,
                "bill_number": t.bill_number or "",
                "description": t.description,
                "created_at": (t.created_at.isoformat() + "Z") if t.created_at else "",
            }
            for t in txns
        ],
        "orders": orders_list,
    }


@router.patch("/api/admin/loyalty/customers/{customer_id}/suspension")
def admin_toggle_customer_suspension(
    customer_id: int,
    payload: dict,
    request: Request,
    db: Session = Depends(_get_db),
):
    admin = require_admin_api(request, db)
    if "is_suspended" not in (payload or {}) and "isSuspended" not in (payload or {}):
        raise HTTPException(status_code=400, detail="is_suspended boolean is required.")
    raw_val = payload.get("is_suspended") if "is_suspended" in payload else payload.get("isSuspended")
    if not isinstance(raw_val, bool):
        raise HTTPException(status_code=400, detail="is_suspended must be a boolean.")

    u = db.query(CustomerUser).filter(CustomerUser.id == customer_id).first()
    if not u:
        raise HTTPException(status_code=404, detail="Customer not found.")

    now = datetime.utcnow()
    u.is_suspended = raw_val
    u.suspended_at = now if raw_val else None
    u.updated_at = now
    db.add(
        AuditLog(
            cashier_id=admin.id,
            admin_username=admin.username,
            action="CUSTOMER_SUSPEND" if raw_val else "CUSTOMER_UNSUSPEND",
            details=f"Admin '{admin.username}' {'suspended' if raw_val else 'unsuspended'} customer {u.email} (ID #{u.id})",
        )
    )
    db.commit()

    if raw_val:
        revoke_all_customer_sessions(db, u.id)

    return {"success": True, "customer": map_customer_profile(u)}


@router.post("/api/admin/loyalty/points/adjust")
def admin_adjust_customer_points(
    payload: dict,
    request: Request,
    db: Session = Depends(_get_db),
):
    admin = require_admin_api(request, db)
    uid = (payload or {}).get("user_id") or (payload or {}).get("userId")
    action = str((payload or {}).get("action") or "").strip().lower()
    desc = str((payload or {}).get("description") or "").strip()[:200]
    try:
        uid_int = int(uid)
        pts_amt = int((payload or {}).get("points") or 0)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="Invalid user ID or points amount.")

    if uid_int <= 0 or pts_amt <= 0 or action not in ("add", "deduct"):
        raise HTTPException(status_code=400, detail="Specify valid user, positive points, and action ('add' or 'deduct').")
    if not desc:
        raise HTTPException(status_code=400, detail="Please provide a reason / description for the adjustment.")

    with _LOYALTY_LOCK:
        _begin_immediate_if_sqlite(db)
        try:
            u = db.query(CustomerUser).filter(CustomerUser.id == uid_int).first()
            if not u:
                db.rollback()
                raise HTTPException(status_code=404, detail="Customer not found.")

            delta = pts_amt if action == "add" else -pts_amt
            txn_type = "admin_add" if action == "add" else "admin_deduct"
            now = datetime.utcnow()

            u.points_balance = int(u.points_balance or 0) + delta
            u.updated_at = now

            db.add(
                PointsTransaction(
                    user_id=u.id,
                    points=delta,
                    transaction_type=txn_type,
                    reference_id=None,
                    bill_number="",
                    source="admin",
                    description=f"Admin ({admin.username}): {desc}",
                    created_at=now,
                )
            )
            db.add(
                AuditLog(
                    cashier_id=admin.id,
                    admin_username=admin.username,
                    action="LOYALTY_POINTS_ADJUST",
                    details=(
                        f"Admin '{admin.username}' {'added' if action == 'add' else 'deducted'} "
                        f"{pts_amt} pts for {u.email} (new balance: {u.points_balance} pts) — {desc}"
                    ),
                )
            )
            db.commit()
            db.refresh(u)

            bal = int(u.points_balance or 0)
            debt = abs(bal) if bal < 0 else 0
            return {
                "success": True,
                "balance": bal,
                "debtPoints": debt,
                "debtMessage": format_debt_message(debt),
            }
        except HTTPException:
            raise
        except Exception as exc:
            db.rollback()
            raise HTTPException(status_code=500, detail=f"Failed to adjust points: {exc}")


@router.get("/api/admin/loyalty/vouchers")
def admin_list_vouchers(
    request: Request,
    source: str = "manual",
    page: int = 1,
    limit: int = 15,
    db: Session = Depends(_get_db),
):
    require_admin_api(request, db)
    safe_page = max(1, int(page or 1))
    safe_limit = min(100, max(1, int(limit or 15)))
    offset = (safe_page - 1) * safe_limit

    src = (source or "manual").strip()
    q = db.query(Voucher).filter(Voucher.is_archived == False)
    if src in ("manual", "points_redemption"):
        q = q.filter(Voucher.source == src)

    total = q.count()
    rows = q.order_by(Voucher.created_at.desc(), Voucher.id.desc()).offset(offset).limit(safe_limit).all()

    uids = list({int(v.user_id) for v in rows if v.user_id is not None})
    user_map: dict[int, CustomerUser] = {}
    if uids:
        for u in db.query(CustomerUser).filter(CustomerUser.id.in_(uids)).all():
            user_map[u.id] = u

    now = datetime.utcnow()
    return {
        "total": total,
        "page": safe_page,
        "limit": safe_limit,
        "total_pages": max(1, math.ceil(total / safe_limit)),
        "vouchers": [
            {
                "id": v.id,
                "code": v.code,
                "type": v.type,
                "discount_value": round(float(v.discount_value), 2),
                "min_spend": round(float(v.min_spend or 0.0), 2),
                "max_discount": round(float(v.max_discount), 2) if v.max_discount is not None else None,
                "expires_at": (v.expires_at.isoformat() + "Z") if v.expires_at else None,
                "is_expired": bool(v.expires_at and v.expires_at < now),
                "is_active": bool(v.is_active),
                "is_redeemed": bool(v.is_redeemed or (v.times_used >= v.max_uses)),
                "max_uses": int(v.max_uses or 1),
                "times_used": int(v.times_used or 0),
                "source": v.source,
                "user_id": v.user_id,
                "user_name": user_map[v.user_id].name if (v.user_id and v.user_id in user_map) else "",
                "user_email": user_map[v.user_id].email if (v.user_id and v.user_id in user_map) else "",
                "created_at": (v.created_at.isoformat() + "Z") if v.created_at else "",
            }
            for v in rows
        ],
    }


@router.post("/api/admin/loyalty/vouchers")
def admin_create_voucher(
    payload: dict,
    request: Request,
    db: Session = Depends(_get_db),
):
    admin = require_admin_api(request, db)
    code = str((payload or {}).get("code") or "").strip().upper()
    if not code or not re.match(r"^[A-Z0-9_-]{3,32}$", code):
        raise HTTPException(
            status_code=400,
            detail="Voucher code must be 3-32 uppercase letters, digits, hyphens, or underscores.",
        )

    v_type = str((payload or {}).get("type") or "fixed").strip().lower()
    if v_type not in ("fixed", "percentage"):
        raise HTTPException(status_code=400, detail="Voucher type must be 'fixed' or 'percentage'.")

    try:
        disc_val = round(float((payload or {}).get("discount_value") or 0.0), 2)
        min_spend = round(float((payload or {}).get("min_spend") or 0.0), 2)
        max_disc_raw = (payload or {}).get("max_discount")
        max_disc = round(float(max_disc_raw), 2) if (max_disc_raw not in (None, "", 0, "0")) else None
        max_uses = max(1, int((payload or {}).get("max_uses") or 1))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="Invalid numeric voucher values.")

    if disc_val <= 0:
        raise HTTPException(status_code=400, detail="Discount value must be greater than 0.")
    if v_type == "percentage" and disc_val > 100:
        raise HTTPException(status_code=400, detail="Percentage discount cannot exceed 100%.")
    if min_spend < 0:
        raise HTTPException(status_code=400, detail="Minimum spend cannot be negative.")

    exp_str = str((payload or {}).get("expires_at") or "").strip()
    if not exp_str:
        raise HTTPException(status_code=400, detail="Expiry date is required.")
    try:
        if len(exp_str) == 10:
            # YYYY-MM-DD -> end of day UTC
            expires_at = datetime.strptime(exp_str, "%Y-%m-%d") + timedelta(hours=23, minutes=59, seconds=59)
        else:
            expires_at = datetime.fromisoformat(exp_str.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid expiry date format.")

    user_email = str((payload or {}).get("user_email") or "").strip().lower()
    assigned_uid: Optional[int] = None
    if user_email:
        u = db.query(CustomerUser).filter(CustomerUser.email == user_email).first()
        if not u:
            raise HTTPException(status_code=400, detail=f"No customer found with email '{user_email}'.")
        assigned_uid = u.id
        max_uses = 1

    exists = db.query(Voucher).filter(Voucher.code == code).first()
    if exists:
        raise HTTPException(status_code=400, detail=f"Voucher code '{code}' already exists.")

    v = Voucher(
        code=code,
        type=v_type,
        discount_value=disc_val,
        min_spend=min_spend,
        max_discount=max_disc,
        expires_at=expires_at,
        is_active=True,
        user_id=assigned_uid,
        is_redeemed=False,
        max_uses=max_uses,
        times_used=0,
        source="manual",
        is_archived=False,
        created_at=datetime.utcnow(),
    )
    db.add(v)
    db.add(
        AuditLog(
            cashier_id=admin.id,
            admin_username=admin.username,
            action="VOUCHER_CREATE",
            details=f"Created voucher '{code}' ({v_type}: {disc_val}, min RM {min_spend:.2f}, max_uses={max_uses})",
        )
    )
    db.commit()
    return {"success": True, "id": v.id, "code": v.code}


@router.put("/api/admin/loyalty/vouchers/{voucher_id}/toggle")
def admin_toggle_voucher(
    voucher_id: int,
    request: Request,
    db: Session = Depends(_get_db),
):
    admin = require_admin_api(request, db)
    v = db.query(Voucher).filter(Voucher.id == voucher_id, Voucher.is_archived == False).first()
    if not v:
        raise HTTPException(status_code=404, detail="Voucher not found.")
    v.is_active = not bool(v.is_active)
    db.add(
        AuditLog(
            cashier_id=admin.id,
            admin_username=admin.username,
            action="VOUCHER_TOGGLE",
            details=f"Toggled voucher '{v.code}' -> active={v.is_active}",
        )
    )
    db.commit()
    return {"success": True, "is_active": bool(v.is_active)}


@router.delete("/api/admin/loyalty/vouchers/{voucher_id}")
def admin_archive_voucher(
    voucher_id: int,
    request: Request,
    db: Session = Depends(_get_db),
):
    admin = require_admin_api(request, db)
    v = db.query(Voucher).filter(Voucher.id == voucher_id).first()
    if not v:
        raise HTTPException(status_code=404, detail="Voucher not found.")
    v.is_archived = True
    v.is_active = False
    db.add(
        AuditLog(
            cashier_id=admin.id,
            admin_username=admin.username,
            action="VOUCHER_DELETE",
            details=f"Archived voucher '{v.code}'",
        )
    )
    db.commit()
    return {"success": True}


@router.get("/api/admin/loyalty/vouchers/export")
def admin_export_vouchers_csv(request: Request, db: Session = Depends(_get_db)):
    require_admin_api(request, db)
    rows = db.query(Voucher).order_by(Voucher.created_at.desc(), Voucher.id.desc()).all()
    uids = list({int(v.user_id) for v in rows if v.user_id is not None})
    email_map = {}
    if uids:
        for uid, em in db.query(CustomerUser.id, CustomerUser.email).filter(CustomerUser.id.in_(uids)).all():
            email_map[uid] = em

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([
        "Code",
        "Source",
        "Type",
        "Value",
        "Min Spend (RM)",
        "Max Discount (RM)",
        "Uses",
        "Assigned To",
        "Active",
        "Archived",
        "Expires At",
        "Created At",
    ])
    for v in rows:
        val_fmt = f"{v.discount_value}%" if v.type == "percentage" else f"RM {v.discount_value:.2f}"
        writer.writerow([
            _csv_safe(v.code),
            _csv_safe(v.source),
            _csv_safe(v.type),
            _csv_safe(val_fmt),
            f"{float(v.min_spend or 0.0):.2f}",
            f"{float(v.max_discount):.2f}" if v.max_discount is not None else "",
            f"{int(v.times_used or 0)}/{int(v.max_uses or 1)}",
            _csv_safe(email_map.get(v.user_id, "Public") if v.user_id else "Public"),
            "Yes" if v.is_active else "No",
            "Yes" if v.is_archived else "No",
            v.expires_at.strftime("%Y-%m-%d %H:%M:%S") if v.expires_at else "",
            v.created_at.strftime("%Y-%m-%d %H:%M:%S") if v.created_at else "",
        ])

    stamp = datetime.utcnow().strftime("%Y%m%d")
    return Response(
        content=buf.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="afrc_vouchers_{stamp}.csv"',
            **NO_CACHE_HEADERS,
        },
    )
