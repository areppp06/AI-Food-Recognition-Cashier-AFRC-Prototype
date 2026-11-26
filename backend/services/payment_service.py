"""Curlec / Razorpay checkout, capture, verification, phone normalization, and refunds."""
from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime
from urllib.parse import quote

import razorpay
from fastapi import HTTPException
from sqlalchemy.orm import Session

from ..db import BillRefund, SessionBill
from ..loyalty import process_verified_paid_bill
from .bill_service import _bill_payload
from .config_service import _restaurant_profile

logger = logging.getLogger("afrc")

_IN_FLIGHT_REFUNDS: set[str] = set()
_REFUND_LOCK = threading.Lock()


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
    try:
        process_verified_paid_bill(db, bill.id)
        db.refresh(bill)
    except Exception as e:
        logger.exception(f"Loyalty processing failed after payment capture for bill {bill.id}: {e}")
    return True


def _verify_and_capture(order_id: str, payment_id: str, signature: str, db: Session):
    order_id = str(order_id or "").strip()
    payment_id = str(payment_id or "").strip()
    signature = str(signature or "").strip()
    bill = db.query(SessionBill).filter(SessionBill.curlec_order_id == order_id).first()
    if not bill or order_id != (bill.curlec_order_id or ""):
        return None, "unpaid"
    if bill.status == "paid":
        if (bill.points_status or "none") in ("", "none") and (bill.curlec_payment_id or "").strip():
            try:
                process_verified_paid_bill(db, bill.id)
                db.refresh(bill)
            except Exception:
                pass
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
