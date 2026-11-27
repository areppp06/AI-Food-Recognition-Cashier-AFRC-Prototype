"""
Core Loyalty, Customer Session, Guest Unclaimed Points, Voucher & Debt Engine.
Adapted from takcakap.com-v2 (pointsPolicy.js & loyaltyService.js) for AFRC FastAPI + SQLite WAL.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import math
import os
import secrets
import threading
from datetime import datetime, timedelta
from typing import Optional

from fastapi import HTTPException, Request, Response
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend.db import (
    CustomerSession,
    CustomerUser,
    ENV_DIR,
    GuestUnclaimedSession,
    LoyaltySettings,
    PointRedemptionTier,
    PointsTransaction,
    SessionBill,
    Voucher,
)

# ==============================================================================
# Constants & Pure Domain Policy (from takcakap.com-v2/server/domain/pointsPolicy.js)
# ==============================================================================

POINTS_PER_RM = 10
POINTS_PER_RM_REDEMPTION_VALUE = 200.0  # 200 pts ~= RM 1.00 baseline display value
MIN_PAYABLE_RM = 0.10  # Curlec gateway minimum (10 sen)

CUSTOMER_SESSION_COOKIE = "afrc_customer_session"
CUSTOMER_SESSION_DAYS = 365

GUEST_LOYALTY_COOKIE = "afrc_guest_loyalty"
GUEST_LOYALTY_DAYS = 30

UNCLAIMED_POINTS_COOKIE = "afrc_unclaimed_pts"
UNCLAIMED_POINTS_DAYS = 7

_LOYALTY_LOCK = threading.RLock()
_CACHED_SIGNING_SECRET: Optional[bytes] = None


def compute_points_from_food_subtotal(food_subtotal: float) -> int:
    """
    Earn 10 points per whole RM of food subtotal (floor(subtotal) * 10).
    Excludes non-food fees; zero/negative/NaN yields 0 points.
    """
    try:
        val = float(food_subtotal)
    except (TypeError, ValueError):
        return 0
    if not math.isfinite(val) or val <= 0:
        return 0
    # Round to 4 decimals first to avoid IEEE-754 0.9999999999999999 artifacts
    whole_rm = math.floor(round(val, 4))
    if whole_rm <= 0:
        return 0
    return int(whole_rm * POINTS_PER_RM)


def format_debt_message(debt_points: int) -> Optional[str]:
    amount = max(0, int(math.floor(float(debt_points or 0))))
    if amount <= 0:
        return None
    return (
        f"Points debt: {amount}. Future purchases restore this balance first. "
        "Redemptions and vouchers are frozen until cleared."
    )


def format_voucher_frozen_message(debt_points: int) -> str:
    amount = max(0, int(math.floor(float(debt_points or 0))))
    if amount <= 0:
        return "Vouchers are frozen until your points balance is non-negative."
    return f"Vouchers are frozen. Clear {amount} points debt via purchases first."


def is_redeem_blocked_by_debt(balance: int) -> bool:
    return int(balance or 0) < 0


def is_voucher_use_blocked_by_debt(balance: int) -> bool:
    return int(balance or 0) < 0


# ==============================================================================
# Cryptographic Cookie Signing & Guest Session Helpers
# ==============================================================================

def _get_signing_secret() -> bytes:
    global _CACHED_SIGNING_SECRET
    if _CACHED_SIGNING_SECRET:
        return _CACHED_SIGNING_SECRET

    env_secret = (
        os.getenv("CUSTOMER_SESSION_SECRET", "").strip()
        or os.getenv("JWT_SECRET", "").strip()
    )
    if env_secret and len(env_secret) >= 24:
        _CACHED_SIGNING_SECRET = env_secret.encode("utf-8")
        return _CACHED_SIGNING_SECRET

    secret_file = ENV_DIR / ".customer_secret"
    if secret_file.is_file():
        try:
            raw = secret_file.read_text(encoding="utf-8").strip()
            if len(raw) >= 24:
                _CACHED_SIGNING_SECRET = raw.encode("utf-8")
                return _CACHED_SIGNING_SECRET
        except Exception:
            pass

    generated = secrets.token_urlsafe(32)
    try:
        ENV_DIR.mkdir(parents=True, exist_ok=True)
        secret_file.write_text(generated, encoding="utf-8")
        secret_file.chmod(0o600)
    except Exception:
        pass
    _CACHED_SIGNING_SECRET = generated.encode("utf-8")
    return _CACHED_SIGNING_SECRET


def _hmac_hex(data: str) -> str:
    return hmac.new(_get_signing_secret(), data.encode("utf-8"), hashlib.sha256).hexdigest()


def sign_guest_session_id(guest_session_id: str) -> str:
    sig = _hmac_hex(f"guest:{guest_session_id}")
    return f"{guest_session_id}.{sig}"


def verify_guest_session_cookie(raw_cookie: str) -> Optional[str]:
    if not raw_cookie or "." not in raw_cookie:
        return None
    gsid, sig = raw_cookie.rsplit(".", 1)
    gsid = gsid.strip()
    if not gsid or len(gsid) < 16 or len(gsid) > 96:
        return None
    expected = _hmac_hex(f"guest:{gsid}")
    if not hmac.compare_digest(sig, expected):
        return None
    return gsid


def sign_unclaimed_cookie_payload(guest_session_id: str, bill_id: str, points: int) -> str:
    payload_json = json.dumps(
        {
            "g": guest_session_id,
            "b": bill_id,
            "p": int(points),
            "t": int(datetime.utcnow().timestamp()),
        },
        separators=(",", ":"),
    )
    b64 = base64.urlsafe_b64encode(payload_json.encode("utf-8")).decode("ascii").rstrip("=")
    sig = _hmac_hex(f"unclaimed:{b64}")
    return f"{b64}.{sig}"


def verify_unclaimed_cookie_payload(raw_cookie: str) -> Optional[dict]:
    if not raw_cookie or "." not in raw_cookie:
        return None
    b64, sig = raw_cookie.rsplit(".", 1)
    expected = _hmac_hex(f"unclaimed:{b64}")
    if not hmac.compare_digest(sig, expected):
        return None
    try:
        pad = "=" * (-len(b64) % 4)
        decoded = base64.urlsafe_b64decode((b64 + pad).encode("ascii")).decode("utf-8")
        data = json.loads(decoded)
        if not isinstance(data, dict):
            return None
        return data
    except Exception:
        return None


def is_https_request(request: Request) -> bool:
    return (
        request.url.scheme == "https"
        or request.headers.get("x-forwarded-proto", "").split(",")[0].strip().lower() == "https"
    )


def get_or_create_guest_session_id(request: Request) -> tuple[str, bool]:
    """
    Returns (guest_session_id, is_newly_created).
    Verifies HMAC signature of afrc_guest_loyalty cookie (or falls back to signed unclaimed cookie).
    """
    raw = request.cookies.get(GUEST_LOYALTY_COOKIE, "")
    gsid = verify_guest_session_cookie(raw)
    if gsid:
        return gsid, False

    unclaimed_data = verify_unclaimed_cookie_payload(request.cookies.get(UNCLAIMED_POINTS_COOKIE, ""))
    if unclaimed_data and isinstance(unclaimed_data.get("g"), str) and len(unclaimed_data["g"]) >= 16:
        return unclaimed_data["g"], True

    return secrets.token_urlsafe(24), True


def set_guest_loyalty_cookie(response: Response, request: Request, guest_session_id: str) -> None:
    response.set_cookie(
        key=GUEST_LOYALTY_COOKIE,
        value=sign_guest_session_id(guest_session_id),
        max_age=86400 * GUEST_LOYALTY_DAYS,
        path="/",
        httponly=True,
        samesite="lax",
        secure=is_https_request(request),
    )


def set_unclaimed_points_cookie(
    response: Response,
    request: Request,
    guest_session_id: str,
    bill_id: str,
    points: int,
) -> None:
    response.set_cookie(
        key=UNCLAIMED_POINTS_COOKIE,
        value=sign_unclaimed_cookie_payload(guest_session_id, bill_id, points),
        max_age=86400 * UNCLAIMED_POINTS_DAYS,
        path="/",
        httponly=True,
        samesite="lax",
        secure=is_https_request(request),
    )


def clear_unclaimed_points_cookie(response: Response) -> None:
    response.delete_cookie(key=UNCLAIMED_POINTS_COOKIE, path="/")


# ==============================================================================
# Customer Authentication & Session Helpers
# ==============================================================================

def create_customer_session(db: Session, user_id: int, ip: str = "", user_agent: str = "") -> str:
    token = secrets.token_urlsafe(32)
    now = datetime.utcnow()
    sess = CustomerSession(
        token=token,
        user_id=user_id,
        ip=(ip or "")[:100],
        user_agent=(user_agent or "")[:255],
        created_at=now,
        expires_at=now + timedelta(days=CUSTOMER_SESSION_DAYS),
    )
    db.add(sess)
    db.commit()
    return token


def set_customer_session_cookie(response: Response, request: Request, token: str) -> None:
    response.set_cookie(
        key=CUSTOMER_SESSION_COOKIE,
        value=token,
        max_age=86400 * CUSTOMER_SESSION_DAYS,
        path="/",
        httponly=True,
        samesite="lax",
        secure=is_https_request(request),
    )


def clear_customer_session_cookie(response: Response) -> None:
    response.delete_cookie(key=CUSTOMER_SESSION_COOKIE, path="/")


def _extract_customer_token(request: Request) -> str:
    token = request.cookies.get(CUSTOMER_SESSION_COOKIE, "").strip()
    if token:
        return token
    auth_h = request.headers.get("Authorization", "")
    if auth_h.startswith("Bearer "):
        return auth_h[7:].strip()
    return ""


def get_current_customer(
    request: Request,
    db: Session,
    allow_suspended: bool = False,
) -> Optional[CustomerUser]:
    token = _extract_customer_token(request)
    if not token:
        return None
    now = datetime.utcnow()
    sess = (
        db.query(CustomerSession)
        .filter(CustomerSession.token == token, CustomerSession.expires_at > now)
        .first()
    )
    if not sess:
        return None
    user = db.query(CustomerUser).filter(CustomerUser.id == sess.user_id).first()
    if not user:
        return None
    if user.is_suspended and not allow_suspended:
        return None
    return user


def require_customer_api(request: Request, db: Session) -> CustomerUser:
    token = _extract_customer_token(request)
    if not token:
        raise HTTPException(status_code=401, detail="Please sign in with Google to continue.")
    now = datetime.utcnow()
    sess = (
        db.query(CustomerSession)
        .filter(CustomerSession.token == token, CustomerSession.expires_at > now)
        .first()
    )
    if not sess:
        raise HTTPException(status_code=401, detail="Session expired. Please sign in again.")
    user = db.query(CustomerUser).filter(CustomerUser.id == sess.user_id).first()
    if not user:
        raise HTTPException(status_code=401, detail="Account not found.")
    if user.is_suspended:
        raise HTTPException(status_code=403, detail="Your account is suspended. Please contact support.")
    return user


def revoke_customer_session(db: Session, token: str) -> None:
    if not token:
        return
    db.query(CustomerSession).filter(CustomerSession.token == token).delete()
    db.commit()


def revoke_all_customer_sessions(db: Session, user_id: int) -> None:
    db.query(CustomerSession).filter(CustomerSession.user_id == user_id).delete()
    db.commit()


def map_customer_profile(user: CustomerUser) -> dict:
    balance = int(user.points_balance or 0)
    debt = abs(balance) if balance < 0 else 0
    return {
        "id": user.id,
        "name": user.name or "Customer",
        "email": user.email or "",
        "avatar_url": user.avatar_url or "",
        "phone_number": user.phone_number or "",
        "points_balance": balance,
        "debt_points": debt,
        "debt_message": format_debt_message(debt),
        "is_suspended": bool(user.is_suspended),
        "created_at": (user.created_at.isoformat() + "Z") if user.created_at else "",
    }


# ==============================================================================
# SQLite Immediate Transaction Helper & Bill Subtotal Helpers
# ==============================================================================

def _begin_immediate_if_sqlite(db: Session) -> None:
    """
    Ensures SQLite acquires a RESERVED/IMMEDIATE write lock upfront before reading
    balances/vouchers/unclaimed flags inside _LOYALTY_LOCK.
    """
    try:
        bind = db.get_bind()
        if bind and bind.dialect.name == "sqlite":
            if db.in_transaction():
                db.commit()
            db.execute(text("BEGIN IMMEDIATE"))
    except Exception:
        pass


def get_bill_food_subtotal(bill: SessionBill) -> float:
    try:
        items = json.loads(bill.final_bill or "[]")
    except Exception:
        items = []
    return round(sum(float(it.get("subtotal") or 0.0) for it in items), 2)


def get_loyalty_settings(db: Session) -> LoyaltySettings:
    row = db.query(LoyaltySettings).filter(LoyaltySettings.id == 1).first()
    if not row:
        row = LoyaltySettings(id=1, voucher_cooldown_hours=12)
        db.add(row)
        db.commit()
        db.refresh(row)
    return row


# ==============================================================================
# Voucher Calculation, Validation, Bill Application & Points Redemption
# ==============================================================================

def compute_voucher_discount(voucher: Voucher, food_subtotal: float) -> float:
    """
    Computes discount amount in RM for a voucher against food_subtotal.
    Caps discount so at least MIN_PAYABLE_RM (RM 0.10) remains payable via Curlec.
    """
    subtotal = round(float(food_subtotal or 0.0), 2)
    if subtotal <= MIN_PAYABLE_RM:
        return 0.0

    if voucher.type == "fixed":
        raw_discount = float(voucher.discount_value or 0.0)
    elif voucher.type == "percentage":
        pct = max(0.0, min(100.0, float(voucher.discount_value or 0.0)))
        raw_discount = math.floor(subtotal * pct) / 100.0
        if voucher.max_discount is not None and float(voucher.max_discount) > 0:
            raw_discount = min(raw_discount, float(voucher.max_discount))
    else:
        raw_discount = 0.0

    max_allowed_discount = round(max(0.0, subtotal - MIN_PAYABLE_RM), 2)
    return round(max(0.0, min(raw_discount, max_allowed_discount)), 2)


def validate_voucher_for_user(
    db: Session,
    user: CustomerUser,
    code: str,
    food_subtotal: float,
) -> tuple[Voucher, float]:
    balance = int(user.points_balance or 0)
    if is_voucher_use_blocked_by_debt(balance):
        debt = abs(balance)
        raise HTTPException(status_code=400, detail=format_voucher_frozen_message(debt))

    norm_code = (code or "").strip().upper()
    if not norm_code:
        raise HTTPException(status_code=400, detail="Please enter a voucher code.")

    voucher = (
        db.query(Voucher)
        .filter(Voucher.code == norm_code, Voucher.is_archived == False)
        .first()
    )
    if not voucher:
        raise HTTPException(status_code=404, detail="Invalid voucher code.")
    if not voucher.is_active or int(voucher.times_used or 0) >= int(voucher.max_uses or 1):
        raise HTTPException(
            status_code=400,
            detail="Voucher is no longer valid or has reached its usage limit.",
        )
    now = datetime.utcnow()
    if voucher.expires_at and voucher.expires_at < now:
        raise HTTPException(status_code=400, detail="Voucher has expired.")
    if voucher.user_id is not None and int(voucher.user_id) != int(user.id):
        raise HTTPException(status_code=400, detail="Voucher belongs to another account.")

    min_spend = round(float(voucher.min_spend or 0.0), 2)
    if round(float(food_subtotal or 0.0), 2) + 1e-6 < min_spend:
        raise HTTPException(
            status_code=400,
            detail=f"Minimum spend of RM {min_spend:.2f} required for this voucher.",
        )

    if round(float(food_subtotal or 0.0), 2) <= MIN_PAYABLE_RM:
        raise HTTPException(status_code=400, detail="Bill total is too low to apply a voucher.")

    discount = compute_voucher_discount(voucher, food_subtotal)
    if discount <= 0:
        raise HTTPException(status_code=400, detail="Voucher yields RM 0.00 discount on this bill.")

    return voucher, discount


def apply_or_clear_bill_voucher(
    db: Session,
    bill: SessionBill,
    user: Optional[CustomerUser],
    code: str,
) -> SessionBill:
    """
    Applies a voucher to an unpaid bill (or clears it when code is empty).
    """
    with _LOYALTY_LOCK:
        if bill.status in ("paid", "void", "cancelled", "needs_cashier"):
            raise HTTPException(status_code=409, detail="Cannot modify voucher on a closed or paid bill.")

        norm_code = (code or "").strip().upper()
        if not norm_code:
            bill.voucher_id = None
            bill.voucher_code = ""
            bill.discount_amount = 0.0
            bill.curlec_order_id = ""
            if bill.status == "pending":
                bill.status = "open"
            bill.updated_at = datetime.utcnow()
            db.commit()
            db.refresh(bill)
            return bill

        if not user:
            raise HTTPException(status_code=401, detail="Please sign in with Google to use vouchers.")

        food_subtotal = get_bill_food_subtotal(bill)
        voucher, discount = validate_voucher_for_user(db, user, norm_code, food_subtotal)

        bill.customer_user_id = user.id
        bill.voucher_id = voucher.id
        bill.voucher_code = voucher.code
        bill.discount_amount = discount
        # Reset pending Curlec order so next /api/pay creates an order with the new discounted total
        bill.curlec_order_id = ""
        if bill.status == "pending":
            bill.status = "open"
        bill.updated_at = datetime.utcnow()
        db.commit()
        db.refresh(bill)
        return bill


def revalidate_bill_voucher_before_pay(
    db: Session,
    bill: SessionBill,
    current_user: Optional[CustomerUser],
) -> None:
    """
    Called right before creating a Curlec checkout order (/api/pay) or after a photo retake.
    Ensures any attached voucher is still valid, unexhausted, owned by the user, and meets min_spend.
    """
    if not bill.voucher_id and not (bill.voucher_code or "").strip():
        bill.discount_amount = 0.0
        return

    owner = current_user
    if not owner and bill.customer_user_id:
        owner = db.query(CustomerUser).filter(CustomerUser.id == bill.customer_user_id).first()

    if not owner or owner.is_suspended:
        bill.voucher_id = None
        bill.voucher_code = ""
        bill.discount_amount = 0.0
        db.commit()
        raise HTTPException(
            status_code=400,
            detail="Voucher removed because account session is no longer active. Please review total and try again.",
        )

    food_subtotal = get_bill_food_subtotal(bill)
    try:
        voucher, discount = validate_voucher_for_user(db, owner, bill.voucher_code, food_subtotal)
        bill.voucher_id = voucher.id
        bill.voucher_code = voucher.code
        bill.discount_amount = discount
    except HTTPException as exc:
        bill.voucher_id = None
        bill.voucher_code = ""
        bill.discount_amount = 0.0
        bill.curlec_order_id = ""
        db.commit()
        raise HTTPException(
            status_code=409,
            detail=f"Applied voucher is no longer valid ({exc.detail}). Voucher has been removed; please review the updated total.",
        )


def redeem_points_for_voucher(db: Session, user_id: int, points_to_redeem: int) -> dict:
    """
    Atomically redeems points for a single-use 30-day RM voucher based on active PointRedemptionTier.
    Enforces debt lock, balance check, and configurable cooldown hours.
    """
    if not points_to_redeem or int(points_to_redeem) <= 0:
        raise HTTPException(status_code=400, detail="Invalid redemption points amount.")
    pts_req = int(points_to_redeem)

    with _LOYALTY_LOCK:
        _begin_immediate_if_sqlite(db)
        try:
            user = db.query(CustomerUser).filter(CustomerUser.id == user_id).first()
            if not user:
                db.rollback()
                raise HTTPException(status_code=404, detail="Account not found.")
            if user.is_suspended:
                db.rollback()
                raise HTTPException(status_code=403, detail="Account is suspended.")

            balance = int(user.points_balance or 0)
            if is_redeem_blocked_by_debt(balance):
                debt = abs(balance)
                db.rollback()
                raise HTTPException(status_code=400, detail=format_debt_message(debt))

            if balance < pts_req:
                db.rollback()
                raise HTTPException(status_code=400, detail="Insufficient points balance.")

            tier = (
                db.query(PointRedemptionTier)
                .filter(
                    PointRedemptionTier.points_required == pts_req,
                    PointRedemptionTier.is_active == True,
                )
                .first()
            )
            if not tier:
                db.rollback()
                raise HTTPException(
                    status_code=400,
                    detail="Invalid redemption tier. Please select an active reward.",
                )

            settings = db.query(LoyaltySettings).filter(LoyaltySettings.id == 1).first()
            cooldown_hours = int(settings.voucher_cooldown_hours if settings else 12)
            now = datetime.utcnow()
            if cooldown_hours > 0:
                last_v = (
                    db.query(Voucher)
                    .filter(
                        Voucher.user_id == user.id,
                        Voucher.source == "points_redemption",
                    )
                    .order_by(Voucher.created_at.desc())
                    .first()
                )
                if last_v and last_v.created_at:
                    cooldown_ends = last_v.created_at + timedelta(hours=cooldown_hours)
                    if cooldown_ends > now:
                        rem_secs = max(60, int((cooldown_ends - now).total_seconds()))
                        rem_h, rem_m = divmod(rem_secs // 60, 60)
                        db.rollback()
                        raise HTTPException(
                            status_code=400,
                            detail=(
                                f"You can only redeem one voucher every {cooldown_hours} hours "
                                f"(next available in {rem_h}h {rem_m}m)."
                            ),
                        )

            discount_val = round(float(tier.discount_amount), 2)
            expires_at = now + timedelta(days=30)

            alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
            voucher_obj: Optional[Voucher] = None
            for _ in range(30):
                candidate = "".join(secrets.choice(alphabet) for _ in range(6))
                exists = db.query(Voucher.id).filter(Voucher.code == candidate).first()
                if not exists:
                    voucher_obj = Voucher(
                        code=candidate,
                        type="fixed",
                        discount_value=discount_val,
                        min_spend=0.0,
                        max_discount=None,
                        expires_at=expires_at,
                        is_active=True,
                        user_id=user.id,
                        is_redeemed=False,
                        max_uses=1,
                        times_used=0,
                        source="points_redemption",
                        is_archived=False,
                        created_at=now,
                    )
                    db.add(voucher_obj)
                    db.flush()
                    break

            if not voucher_obj:
                db.rollback()
                raise HTTPException(status_code=500, detail="Could not generate unique voucher code.")

            user.points_balance = balance - pts_req
            user.updated_at = now

            fmt_disc = f"{discount_val:.0f}" if abs(discount_val - round(discount_val)) < 1e-6 else f"{discount_val:.2f}"
            txn = PointsTransaction(
                user_id=user.id,
                points=-pts_req,
                transaction_type="redeemed",
                reference_id=f"voucher:{voucher_obj.id}",
                bill_number="",
                source="redemption",
                description=f"Redeemed - RM {fmt_disc} voucher ({voucher_obj.code})",
                created_at=now,
            )
            db.add(txn)
            db.commit()

            return {
                "success": True,
                "code": voucher_obj.code,
                "discount_value": discount_val,
                "points_redeemed": pts_req,
                "new_balance": int(user.points_balance),
                "expires_at": expires_at.isoformat() + "Z",
            }
        except HTTPException:
            raise
        except Exception as exc:
            db.rollback()
            raise HTTPException(status_code=500, detail=f"Failed to redeem points: {exc}")


# ==============================================================================
# Verified Payment Awarding, Guest Unclaimed Overwrite & Claiming Engine
# ==============================================================================

def process_verified_paid_bill(db: Session, bill_id: str) -> dict:
    """
    Invoked ONLY after Curlec payment capture is cryptographically verified.
    Idempotent & concurrency-safe via _LOYALTY_LOCK + BEGIN IMMEDIATE + conditional status check
    + unique index (transaction_type, reference_id).

    Behavior:
    - Logged-in bill (bill.customer_user_id set): awards points directly to account & ledger.
    - Guest bill (bill.customer_user_id is None):
        * Calculates eligible points.
        * OVERWRITES any prior unclaimed guest points for this guest session (no accumulation!).
        * Stores unclaimed points in GuestUnclaimedSession & marks bill.points_status = 'unclaimed'.
    """
    with _LOYALTY_LOCK:
        _begin_immediate_if_sqlite(db)
        try:
            bill = db.query(SessionBill).filter(SessionBill.id == bill_id).first()
            if not bill:
                db.rollback()
                return {"processed": False, "reason": "bill_not_found"}

            # Strict security gate: must be status='paid' with a verified gateway payment ID
            if (bill.status or "").lower() != "paid" or not (bill.curlec_payment_id or "").strip():
                db.rollback()
                return {"processed": False, "reason": "not_verified_paid"}

            # Idempotency check: only process once when points_status is 'none' (or null/empty)
            cur_pstatus = (bill.points_status or "none").strip().lower()
            if cur_pstatus not in ("", "none"):
                db.commit()
                return {
                    "processed": True,
                    "idempotent": True,
                    "points_earned": int(bill.points_earned or 0),
                    "points_status": cur_pstatus,
                    "customer_user_id": bill.customer_user_id,
                }

            now = datetime.utcnow()

            # 1. Finalize voucher consumption if a voucher was used on this bill
            if bill.voucher_id:
                voucher = db.query(Voucher).filter(Voucher.id == bill.voucher_id).first()
                if voucher:
                    next_used = int(voucher.times_used or 0) + 1
                    max_u = max(1, int(voucher.max_uses or 1))
                    voucher.times_used = next_used
                    voucher.is_redeemed = next_used >= max_u

            # 2. Compute eligible points from food subtotal
            food_subtotal = get_bill_food_subtotal(bill)
            pts = compute_points_from_food_subtotal(food_subtotal)
            display_bill = bill.bill_number or bill.id[:8]

            # 3A. Logged-in customer flow: credit directly
            if bill.customer_user_id:
                user = db.query(CustomerUser).filter(CustomerUser.id == bill.customer_user_id).first()
                if user:
                    # Save phone to user profile if empty
                    if (bill.receipt_phone or "").strip() and not (user.phone_number or "").strip():
                        user.phone_number = bill.receipt_phone.strip()

                    if pts > 0:
                        existing_txn = (
                            db.query(PointsTransaction.id)
                            .filter(
                                PointsTransaction.transaction_type == "earned",
                                PointsTransaction.reference_id == bill.id,
                            )
                            .first()
                        )
                        if not existing_txn:
                            db.add(
                                PointsTransaction(
                                    user_id=user.id,
                                    points=pts,
                                    transaction_type="earned",
                                    reference_id=bill.id,
                                    bill_number=bill.bill_number or "",
                                    source="direct",
                                    description=f"Points earned for Bill #{display_bill}",
                                    created_at=now,
                                )
                            )
                            user.points_balance = int(user.points_balance or 0) + pts
                            user.updated_at = now
                        bill.points_earned = pts
                        bill.points_status = "awarded"
                        bill.points_claimed_by = user.id
                        bill.points_claimed_at = now
                    else:
                        bill.points_earned = 0
                        bill.points_status = "zero"
                        bill.points_claimed_by = user.id
                        bill.points_claimed_at = now

                    db.commit()
                    return {
                        "processed": True,
                        "points_earned": pts,
                        "points_status": bill.points_status,
                        "customer_user_id": user.id,
                    }

            # 3B. Guest checkout flow: hold as unclaimed & OVERWRITE prior guest unclaimed transactions!
            gsid = (bill.guest_session_id or "").strip()
            ctok = (bill.client_token or "").strip()

            # Overwrite prior unclaimed bills on same guest_session_id
            if gsid:
                db.execute(
                    text(
                        "UPDATE sessions SET points_status = 'overwritten', updated_at = :now "
                        "WHERE guest_session_id = :gsid AND id != :bid AND points_status = 'unclaimed'"
                    ),
                    {"now": now, "gsid": gsid, "bid": bill.id},
                )
            # Also overwrite prior unclaimed guest bills on same client_token (belt-and-suspenders)
            if ctok:
                overwritten_by_ctok = (
                    db.query(SessionBill.guest_session_id)
                    .filter(
                        SessionBill.client_token == ctok,
                        SessionBill.id != bill.id,
                        SessionBill.customer_user_id == None,
                        SessionBill.points_status == "unclaimed",
                    )
                    .all()
                )
                for (old_gsid,) in overwritten_by_ctok:
                    if old_gsid and old_gsid != gsid:
                        db.query(GuestUnclaimedSession).filter(
                            GuestUnclaimedSession.guest_session_id == old_gsid
                        ).delete()
                db.execute(
                    text(
                        "UPDATE sessions SET points_status = 'overwritten', updated_at = :now "
                        "WHERE client_token = :ctok AND id != :bid AND customer_user_id IS NULL AND points_status = 'unclaimed'"
                    ),
                    {"now": now, "ctok": ctok, "bid": bill.id},
                )

            if pts > 0:
                bill.points_earned = pts
                bill.points_status = "unclaimed"
                if gsid:
                    guest_row = (
                        db.query(GuestUnclaimedSession)
                        .filter(GuestUnclaimedSession.guest_session_id == gsid)
                        .first()
                    )
                    if guest_row:
                        guest_row.active_bill_id = bill.id
                        guest_row.unclaimed_points = pts
                        guest_row.updated_at = now
                    else:
                        db.add(
                            GuestUnclaimedSession(
                                guest_session_id=gsid,
                                active_bill_id=bill.id,
                                unclaimed_points=pts,
                                created_at=now,
                                updated_at=now,
                            )
                        )
            else:
                bill.points_earned = 0
                bill.points_status = "zero"
                if gsid:
                    db.query(GuestUnclaimedSession).filter(
                        GuestUnclaimedSession.guest_session_id == gsid
                    ).delete()

            db.commit()
            return {
                "processed": True,
                "points_earned": pts,
                "points_status": bill.points_status,
                "guest_session_id": gsid,
            }
        except IntegrityError:
            db.rollback()
            return {"processed": False, "reason": "already_awarded"}
        except Exception:
            db.rollback()
            raise


def get_authoritative_guest_unclaimed(
    db: Session,
    guest_session_id: str,
    client_token: str = "",
) -> dict:
    """
    Returns authoritative unclaimed points from the database for this guest session.
    Never trusts points numbers supplied by the browser or cookies.
    """
    bill: Optional[SessionBill] = None
    gsid = (guest_session_id or "").strip()
    if gsid:
        guest_row = (
            db.query(GuestUnclaimedSession)
            .filter(GuestUnclaimedSession.guest_session_id == gsid)
            .first()
        )
        if guest_row:
            candidate = db.query(SessionBill).filter(SessionBill.id == guest_row.active_bill_id).first()
            if (
                candidate
                and (candidate.status or "").lower() == "paid"
                and (candidate.points_status or "").lower() == "unclaimed"
                and candidate.points_claimed_by is None
                and int(candidate.points_earned or 0) > 0
            ):
                bill = candidate
            else:
                # Stale pointer — clean up
                db.query(GuestUnclaimedSession).filter(
                    GuestUnclaimedSession.guest_session_id == gsid
                ).delete()
                db.commit()

    # Fallback lookup by guest_session_id or client_token if GuestUnclaimedSession wasn't synced yet
    if not bill and gsid:
        bill = (
            db.query(SessionBill)
            .filter(
                SessionBill.guest_session_id == gsid,
                SessionBill.status == "paid",
                SessionBill.points_status == "unclaimed",
                SessionBill.points_claimed_by == None,
                SessionBill.customer_user_id == None,
            )
            .order_by(SessionBill.paid_at.desc(), SessionBill.created_at.desc())
            .first()
        )
    if not bill and (client_token or "").strip():
        bill = (
            db.query(SessionBill)
            .filter(
                SessionBill.client_token == client_token.strip(),
                SessionBill.status == "paid",
                SessionBill.points_status == "unclaimed",
                SessionBill.points_claimed_by == None,
                SessionBill.customer_user_id == None,
            )
            .order_by(SessionBill.paid_at.desc(), SessionBill.created_at.desc())
            .first()
        )

    if not bill or int(bill.points_earned or 0) <= 0:
        return {
            "has_unclaimed": False,
            "points": 0,
            "bill_id": "",
            "bill_number": "",
            "paid_at": "",
        }

    net_unclaimed = max(0, int(bill.points_earned or 0) - int(bill.points_clawed_back or 0))
    if net_unclaimed <= 0:
        return {
            "has_unclaimed": False,
            "points": 0,
            "bill_id": "",
            "bill_number": "",
            "paid_at": "",
        }

    return {
        "has_unclaimed": True,
        "points": net_unclaimed,
        "bill_id": bill.id,
        "bill_number": bill.bill_number or "",
        "paid_at": (bill.paid_at.isoformat() + "Z") if bill.paid_at else "",
    }


def claim_guest_unclaimed_points_for_user(
    db: Session,
    user_id: int,
    guest_session_id: str,
    client_token: str = "",
    explicit_session_id: str = "",
) -> dict:
    """
    Atomically transfers eligible unclaimed points from the guest's session into user_id.
    Guaranteed single-claim via:
    1. _LOYALTY_LOCK + SQLite BEGIN IMMEDIATE
    2. Conditional SQL UPDATE (... WHERE points_status = 'unclaimed' AND points_claimed_by IS NULL)
    3. Unique DB index uniq_points_txn_type_ref on points_transactions(transaction_type, reference_id)
    """
    gsid = (guest_session_id or "").strip()
    ctok = (client_token or "").strip()
    exp_sid = (explicit_session_id or "").strip()

    with _LOYALTY_LOCK:
        _begin_immediate_if_sqlite(db)
        try:
            user = db.query(CustomerUser).filter(CustomerUser.id == user_id).first()
            if not user or user.is_suspended:
                db.rollback()
                return {"claimed": False, "points": 0, "bill_number": "", "bill_id": ""}

            target_bill: Optional[SessionBill] = None

            # 1. Check GuestUnclaimedSession pointer first
            if gsid:
                guest_row = (
                    db.query(GuestUnclaimedSession)
                    .filter(GuestUnclaimedSession.guest_session_id == gsid)
                    .first()
                )
                if guest_row:
                    cand = db.query(SessionBill).filter(SessionBill.id == guest_row.active_bill_id).first()
                    if (
                        cand
                        and (cand.status or "").lower() == "paid"
                        and (cand.points_status or "").lower() == "unclaimed"
                        and cand.points_claimed_by is None
                        and cand.customer_user_id is None
                    ):
                        target_bill = cand

            # 2. Fallback to latest unclaimed bill matching guest_session_id
            if not target_bill and gsid:
                target_bill = (
                    db.query(SessionBill)
                    .filter(
                        SessionBill.guest_session_id == gsid,
                        SessionBill.status == "paid",
                        SessionBill.points_status == "unclaimed",
                        SessionBill.points_claimed_by == None,
                        SessionBill.customer_user_id == None,
                    )
                    .order_by(SessionBill.paid_at.desc(), SessionBill.created_at.desc())
                    .first()
                )

            # 3. Fallback to latest unclaimed bill matching client_token (or explicit_session_id owned by client_token)
            if not target_bill and ctok:
                q = db.query(SessionBill).filter(
                    SessionBill.client_token == ctok,
                    SessionBill.status == "paid",
                    SessionBill.points_status == "unclaimed",
                    SessionBill.points_claimed_by == None,
                    SessionBill.customer_user_id == None,
                )
                if exp_sid:
                    cand = q.filter(SessionBill.id == exp_sid).first()
                    if cand:
                        target_bill = cand
                if not target_bill:
                    target_bill = q.order_by(SessionBill.paid_at.desc(), SessionBill.created_at.desc()).first()

            if not target_bill:
                if gsid:
                    db.query(GuestUnclaimedSession).filter(
                        GuestUnclaimedSession.guest_session_id == gsid
                    ).delete()
                db.commit()
                return {"claimed": False, "points": 0, "bill_number": "", "bill_id": ""}

            pts_to_claim = max(
                0,
                int(target_bill.points_earned or 0) - int(target_bill.points_clawed_back or 0),
            )
            if pts_to_claim <= 0 or not (target_bill.curlec_payment_id or "").strip():
                if gsid:
                    db.query(GuestUnclaimedSession).filter(
                        GuestUnclaimedSession.guest_session_id == gsid
                    ).delete()
                db.commit()
                return {"claimed": False, "points": 0, "bill_number": "", "bill_id": ""}

            now = datetime.utcnow()

            # Atomic conditional state transition on sessions table
            res = db.execute(
                text(
                    "UPDATE sessions "
                    "SET points_status = 'claimed', "
                    "    customer_user_id = :uid, "
                    "    points_claimed_by = :uid, "
                    "    points_claimed_at = :now, "
                    "    updated_at = :now "
                    "WHERE id = :bid "
                    "  AND status = 'paid' "
                    "  AND points_status = 'unclaimed' "
                    "  AND points_claimed_by IS NULL "
                    "  AND customer_user_id IS NULL"
                ),
                {"uid": user.id, "now": now, "bid": target_bill.id},
            )
            if (res.rowcount or 0) != 1:
                db.rollback()
                return {"claimed": False, "points": 0, "bill_number": "", "bill_id": ""}

            display_bill = target_bill.bill_number or target_bill.id[:8]
            db.add(
                PointsTransaction(
                    user_id=user.id,
                    points=pts_to_claim,
                    transaction_type="earned",
                    reference_id=target_bill.id,
                    bill_number=target_bill.bill_number or "",
                    source="guest_claim",
                    description=f"Claimed guest points from Bill #{display_bill}",
                    created_at=now,
                )
            )

            user.points_balance = int(user.points_balance or 0) + pts_to_claim
            if (target_bill.receipt_phone or "").strip() and not (user.phone_number or "").strip():
                user.phone_number = target_bill.receipt_phone.strip()
            user.updated_at = now

            # Clean up GuestUnclaimedSession for both current gsid and bill's gsid
            cleanup_gsids = {g for g in (gsid, (target_bill.guest_session_id or "").strip()) if g}
            if cleanup_gsids:
                db.query(GuestUnclaimedSession).filter(
                    GuestUnclaimedSession.guest_session_id.in_(cleanup_gsids)
                ).delete(synchronize_session=False)

            db.commit()
            db.refresh(user)
            return {
                "claimed": True,
                "points": pts_to_claim,
                "bill_number": target_bill.bill_number or "",
                "bill_id": target_bill.id,
                "new_balance": int(user.points_balance or 0),
            }
        except IntegrityError:
            # Unique index (transaction_type='earned', reference_id=bill.id) prevented duplicate credit
            db.rollback()
            return {"claimed": False, "points": 0, "bill_number": "", "bill_id": ""}
        except Exception:
            db.rollback()
            raise


def clear_guest_unclaimed_session(db: Session, guest_session_id: str, client_token: str = "") -> int:
    """
    Clears unclaimed points associated with the guest session (e.g. session reset).
    """
    gsid = (guest_session_id or "").strip()
    ctok = (client_token or "").strip()
    now = datetime.utcnow()
    cleared = 0
    with _LOYALTY_LOCK:
        _begin_immediate_if_sqlite(db)
        try:
            if gsid:
                r1 = db.execute(
                    text(
                        "UPDATE sessions SET points_status = 'cleared', updated_at = :now "
                        "WHERE guest_session_id = :gsid AND points_status = 'unclaimed'"
                    ),
                    {"now": now, "gsid": gsid},
                )
                cleared += int(r1.rowcount or 0)
                db.query(GuestUnclaimedSession).filter(
                    GuestUnclaimedSession.guest_session_id == gsid
                ).delete()
            if ctok:
                r2 = db.execute(
                    text(
                        "UPDATE sessions SET points_status = 'cleared', updated_at = :now "
                        "WHERE client_token = :ctok AND customer_user_id IS NULL AND points_status = 'unclaimed'"
                    ),
                    {"now": now, "ctok": ctok},
                )
                cleared += int(r2.rowcount or 0)
            db.commit()
            return cleared
        except Exception:
            db.rollback()
            raise


# ==============================================================================
# Refund / Void Clawback & Voucher Restoration (from loyaltyService.js)
# ==============================================================================

def apply_bill_refund_loyalty_effects(
    db: Session,
    bill_id: str,
    total_refunded_rm: float,
    refund_ref: str = "",
    reason: str = "Refund",
) -> dict:
    """
    Handles partial or full refund side-effects on loyalty points and vouchers:
    1. Recomputes eligible points from remaining non-refunded food subtotal.
    2. Claws back excess points from CustomerUser (allowing negative balance -> Points Debt)
       or reduces unclaimed guest points if not yet claimed.
    3. Restores voucher usage counter on full refund (idempotent via voucher_restored_at).
    """
    with _LOYALTY_LOCK:
        _begin_immediate_if_sqlite(db)
        try:
            bill = db.query(SessionBill).filter(SessionBill.id == bill_id).first()
            if not bill:
                db.rollback()
                return {"clawed_back_points": 0, "voucher_restored": False}

            now = datetime.utcnow()
            food_subtotal = get_bill_food_subtotal(bill)
            discount = float(bill.discount_amount or 0.0)
            payable_total = max(0.0, round(food_subtotal - discount, 2))
            is_full_refund = float(total_refunded_rm or 0.0) >= (payable_total - 0.01)

            if is_full_refund:
                remaining_food = 0.0
            else:
                remaining_food = max(0.0, round(food_subtotal - float(total_refunded_rm or 0.0), 2))

            allowed_points_now = compute_points_from_food_subtotal(remaining_food)
            earned_orig = int(bill.points_earned or 0)
            already_clawed = int(bill.points_clawed_back or 0)
            net_currently_held = max(0, earned_orig - already_clawed)
            points_to_clawback = max(0, net_currently_held - allowed_points_now)

            clawed_now = 0
            pstatus = (bill.points_status or "").lower()

            if points_to_clawback > 0:
                if pstatus in ("awarded", "claimed") and bill.points_claimed_by:
                    user = db.query(CustomerUser).filter(CustomerUser.id == bill.points_claimed_by).first()
                    if user:
                        ref_key = f"{bill.id}:{refund_ref}" if refund_ref else bill.id
                        exists = (
                            db.query(PointsTransaction.id)
                            .filter(
                                PointsTransaction.transaction_type == "cancelled_order",
                                PointsTransaction.reference_id == ref_key,
                            )
                            .first()
                        )
                        if not exists:
                            display_bill = bill.bill_number or bill.id[:8]
                            db.add(
                                PointsTransaction(
                                    user_id=user.id,
                                    points=-points_to_clawback,
                                    transaction_type="cancelled_order",
                                    reference_id=ref_key,
                                    bill_number=bill.bill_number or "",
                                    source="refund_clawback",
                                    description=f"{reason or 'Refund'}: reversed {points_to_clawback} pts from Bill #{display_bill}",
                                    created_at=now,
                                )
                            )
                            user.points_balance = int(user.points_balance or 0) - points_to_clawback
                            user.updated_at = now
                            bill.points_clawed_back = already_clawed + points_to_clawback
                            bill.points_clawed_back_at = now
                            if bill.points_clawed_back >= earned_orig:
                                bill.points_status = "clawed_back"
                            clawed_now = points_to_clawback
                elif pstatus == "unclaimed":
                    bill.points_clawed_back = already_clawed + points_to_clawback
                    bill.points_clawed_back_at = now
                    rem_unclaimed = max(0, earned_orig - bill.points_clawed_back)
                    gsid = (bill.guest_session_id or "").strip()
                    if rem_unclaimed <= 0:
                        bill.points_status = "clawed_back"
                        if gsid:
                            db.query(GuestUnclaimedSession).filter(
                                GuestUnclaimedSession.guest_session_id == gsid,
                                GuestUnclaimedSession.active_bill_id == bill.id,
                            ).delete()
                    else:
                        if gsid:
                            grow = (
                                db.query(GuestUnclaimedSession)
                                .filter(
                                    GuestUnclaimedSession.guest_session_id == gsid,
                                    GuestUnclaimedSession.active_bill_id == bill.id,
                                )
                                .first()
                            )
                            if grow:
                                grow.unclaimed_points = rem_unclaimed
                                grow.updated_at = now
                    clawed_now = points_to_clawback

            voucher_restored = False
            if is_full_refund and bill.voucher_id and not bill.voucher_restored_at:
                voucher = db.query(Voucher).filter(Voucher.id == bill.voucher_id).first()
                if voucher:
                    next_used = max(0, int(voucher.times_used or 0) - 1)
                    max_u = max(1, int(voucher.max_uses or 1))
                    voucher.times_used = next_used
                    voucher.is_redeemed = next_used >= max_u
                    voucher_restored = True
                bill.voucher_restored_at = now

            db.commit()
            return {
                "clawed_back_points": clawed_now,
                "voucher_restored": voucher_restored,
            }
        except Exception:
            db.rollback()
            raise
