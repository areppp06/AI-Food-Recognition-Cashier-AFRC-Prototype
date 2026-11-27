"""Session bill lookup, expiration cleanup, payload formatting, and legacy scan sync."""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta

from fastapi import Request
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from ..db import ScanAttempt, SessionBill, SessionLocal
from ..loyalty import compute_points_from_food_subtotal
from .config_service import BILL_EXPIRY_MINUTES, UPLOAD_DIR, _rate_limit_config, _restaurant_profile, _restaurant_settings
from .menu_service import _dish_alias_map
from .r2_storage import _R2_BILL_KEYS, _R2_SESSION_KEYS
from .security_service import _all_client_tokens, _mask_email, _mask_phone


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
                for _i in range(int(s.retake_count or 0)):
                    to_add.append(ScanAttempt(session_id=s.id, ip="127.0.0.1", device_id="legacy", created_at=dt))
        if to_add:
            db.add_all(to_add)
            db.commit()
    except Exception:
        db.rollback()
    finally:
        db.close()


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
    food_subtotal = round(sum(float(item.get("subtotal") or 0) for item in items), 2)
    discount_amount = round(float(session.discount_amount or 0.0), 2)
    total = round(max(0.0, food_subtotal - discount_amount), 2)
    potential_points = compute_points_from_food_subtotal(food_subtotal)
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
        "subtotal": food_subtotal,
        "discount_amount": discount_amount,
        "voucher_code": session.voucher_code or "",
        "total": total,
        "potential_points": potential_points,
        "points_earned": int(session.points_earned or 0),
        "points_clawed_back": int(session.points_clawed_back or 0),
        "points_status": session.points_status or "none",
        "customer_user_id": session.customer_user_id,
        "status": status,
        "void_reason": void_reason,
        "retake_count": retakes_used,
        "retakes_left": max(0, max_retakes - retakes_used),
        "max_retakes": max_retakes,
        "receipt_email": email_val,
        "receipt_phone": phone_val,
        "email_receipt_enabled": _restaurant_settings()["email_receipt_enabled"],
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
