"""Self-Service Kiosk (/selfservice) page, CHIP payment, and independent bill routes."""
from __future__ import annotations

import csv
import io
import json
import re
import uuid
from datetime import datetime
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, StreamingResponse
from sqlalchemy import or_
from sqlalchemy.orm import Session

from ..auth import require_admin_api
from ..db import AuditLog, Dish, SelfServiceBill, next_selfservice_bill_number
from ..services.chip_service import create_chip_purchase_for_selfservice, verify_and_mark_selfservice_paid
from ..services.config_service import NO_CACHE_HEADERS, _local_tz_offset, _operation_hours_status, _restaurant_profile, _restaurant_settings
from ..services.security_service import _get_client_token, _get_device_id
from .deps import get_db

router = APIRouter()


def _serialize_ss_bill(b: SelfServiceBill) -> dict:
    try:
        items = json.loads(b.items_json or "[]")
    except Exception:
        items = []
    return {
        "id": b.id,
        "bill_number": b.bill_number or "",
        "status": b.status or "pending",
        "void_reason": b.void_reason or "",
        "items": items,
        "total": round(float(b.total_amount or 0.0), 2),
        "receipt_email": b.receipt_email or "",
        "chip_purchase_id": b.chip_purchase_id or "",
        "payment_method": b.payment_method or "chip",
        "created_at": (b.created_at.isoformat() + "Z") if b.created_at else "",
        "updated_at": (b.updated_at.isoformat() + "Z") if b.updated_at else "",
        "paid_at": (b.paid_at.isoformat() + "Z") if b.paid_at else "",
    }


@router.get("/selfservice")
async def selfservice_page():
    settings = _restaurant_settings()
    if settings["maintenance_mode"]:
        return RedirectResponse("/maintenance", status_code=307)
    if settings["coming_soon"]:
        return RedirectResponse("/coming-soon", status_code=307)
    return FileResponse("frontend/selfservice.html", headers=NO_CACHE_HEADERS)


@router.get("/selfservice/success")
async def selfservice_success_page():
    return FileResponse("frontend/selfservice_success.html", headers=NO_CACHE_HEADERS)


@router.get("/api/selfservice/status")
def selfservice_status():
    settings = _restaurant_settings()
    op = _operation_hours_status()
    prof = _restaurant_profile()
    return JSONResponse(
        content={
            "maintenance_mode": bool(settings["maintenance_mode"]),
            "coming_soon": bool(settings["coming_soon"]),
            "is_open": bool(op.get("is_open", True)),
            "operation_hour": op.get("operation_hour") or prof.get("operation_hour") or "08:00 - 17:00",
            "restaurant_name": prof.get("restaurant_name") or "AFRC Self-Service",
        },
        headers=NO_CACHE_HEADERS,
    )


@router.post("/api/selfservice/pay")
async def selfservice_pay(request: Request, db: Session = Depends(get_db)):
    settings = _restaurant_settings()
    if settings["maintenance_mode"] or settings["coming_soon"]:
        raise HTTPException(status_code=503, detail="Store is currently unavailable.")
    op = _operation_hours_status()
    if not op.get("is_open", True):
        raise HTTPException(
            status_code=403,
            detail=f"You are outside our operation hours ({op.get('operation_hour', '08:00 - 17:00')}).",
        )

    try:
        payload = await request.json()
    except Exception:
        payload = {}

    raw_items = (payload or {}).get("items") or []
    if not isinstance(raw_items, list) or not raw_items:
        raise HTTPException(status_code=400, detail="Please add at least one item to your order.")

    email = str((payload or {}).get("email") or "").strip()
    if email:
        if email[0] in ("=", "+", "-", "@"):
            raise HTTPException(status_code=400, detail="Invalid email format.")
        if not re.match(r"^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+$", email) or len(email) > 100:
            raise HTTPException(status_code=400, detail="Enter a valid email address or leave it blank.")

    dish_ids = []
    for entry in raw_items:
        try:
            did = int(entry.get("dish_id") or entry.get("id"))
            qty = int(entry.get("quantity") or 0)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="Invalid item selection.")
        if qty <= 0 or qty > 99:
            raise HTTPException(status_code=400, detail="Item quantity must be between 1 and 99.")
        dish_ids.append((did, qty))

    dishes_by_id = {
        d.id: d for d in db.query(Dish).filter(Dish.id.in_([x[0] for x in dish_ids])).all()
    }

    bill_items = []
    total = 0.0
    for did, qty in dish_ids:
        dish = dishes_by_id.get(did)
        if not dish or not dish.available:
            raise HTTPException(
                status_code=400,
                detail=f"{dish.name if dish else 'Selected item'} is currently unavailable.",
            )
        unit_price = round(float(dish.price or 0.0), 2)
        sub = round(unit_price * qty, 2)
        total += sub
        bill_items.append({
            "dish_id": dish.id,
            "name": dish.name,
            "quantity": qty,
            "unit_price": unit_price,
            "subtotal": sub,
            "pieces_per_portion": int(dish.pieces_per_portion or 1),
        })

    total = round(total, 2)
    if total < 0.10:
        raise HTTPException(status_code=400, detail="Minimum order total is RM 0.10.")

    bill_id = str(uuid.uuid4())
    bill_number = next_selfservice_bill_number(db)
    c_token = _get_client_token(request, str((payload or {}).get("client_token") or ""))
    d_id = _get_device_id(request, explicit_device=str((payload or {}).get("device_id") or ""))

    bill = SelfServiceBill(
        id=bill_id,
        bill_number=bill_number,
        items_json=json.dumps(bill_items),
        total_amount=total,
        status="pending",
        receipt_email=email,
        payment_method="chip",
        client_token=c_token,
        device_id=d_id,
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )
    db.add(bill)
    db.commit()

    try:
        chip_res = create_chip_purchase_for_selfservice(request, bill, bill_items)
    except HTTPException:
        bill.status = "failed"
        bill.void_reason = "CHIP initialization failed"
        bill.updated_at = datetime.utcnow()
        db.commit()
        raise

    bill.chip_purchase_id = chip_res["purchase_id"]
    bill.chip_checkout_url = chip_res["checkout_url"]
    bill.updated_at = datetime.utcnow()
    db.commit()

    return JSONResponse(
        content={
            "gateway": "chip",
            "bill_id": bill.id,
            "bill_number": bill.bill_number,
            "total": total,
            "checkout_url": chip_res["checkout_url"],
        },
        headers=NO_CACHE_HEADERS,
    )


@router.get("/api/selfservice/chip-callback")
def selfservice_chip_callback(
    bill_id: str = "",
    status: str = "",
    id: str = "",
    purchase_id: str = "",
    db: Session = Depends(get_db),
):
    bill = None
    if bill_id:
        bill = db.query(SelfServiceBill).filter(SelfServiceBill.id == bill_id.strip()).first()
    pid = (purchase_id or id or "").strip()
    if not bill and pid:
        bill = db.query(SelfServiceBill).filter(SelfServiceBill.chip_purchase_id == pid).first()

    if not bill:
        return RedirectResponse("/selfservice?payment=failed", status_code=303)

    if status in ("failure", "cancelled"):
        if bill.status == "pending":
            bill.status = "cancelled" if status == "cancelled" else "failed"
            bill.void_reason = f"Customer {status} payment"
            bill.updated_at = datetime.utcnow()
            db.commit()
        return RedirectResponse(f"/selfservice?payment={quote(status)}", status_code=303)

    if verify_and_mark_selfservice_paid(bill, db):
        return RedirectResponse(f"/selfservice/success?bill={quote(bill.id)}", status_code=303)
    return RedirectResponse(f"/selfservice/success?bill={quote(bill.id)}&pending=1", status_code=303)


@router.post("/api/selfservice/chip-webhook")
async def selfservice_chip_webhook(request: Request, db: Session = Depends(get_db)):
    try:
        payload = json.loads(await request.body())
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON payload")

    pid = str(payload.get("id") or "").strip()
    if not pid:
        raise HTTPException(status_code=400, detail="Missing purchase ID")

    bill = db.query(SelfServiceBill).filter(SelfServiceBill.chip_purchase_id == pid).first()
    if not bill:
        return {"ok": True, "ignored": True}

    if str(payload.get("status") or "").lower() in ("paid", "cleared", "settled"):
        verify_and_mark_selfservice_paid(bill, db)
    return {"ok": True}


@router.get("/api/selfservice/bills/{bill_id}")
def get_selfservice_bill(bill_id: str, db: Session = Depends(get_db)):
    bill = db.query(SelfServiceBill).filter(SelfServiceBill.id == bill_id.strip()).first()
    if not bill:
        raise HTTPException(status_code=404, detail="Bill not found")
    if bill.status == "pending" and bill.chip_purchase_id:
        verify_and_mark_selfservice_paid(bill, db)
    prof = _restaurant_profile()
    data = _serialize_ss_bill(bill)
    data["restaurant_name"] = prof.get("restaurant_name") or "AFRC"
    data["company_name"] = prof.get("company_name") or ""
    data["company_phone"] = prof.get("company_phone") or ""
    data["company_email"] = prof.get("company_email") or ""
    return JSONResponse(content=data, headers=NO_CACHE_HEADERS)


# ---------------------------------------------------------------------------
# Admin: Independent Self-Checkout Bills
# ---------------------------------------------------------------------------

@router.get("/api/admin/selfservice-bills")
def list_admin_selfservice_bills(
    request: Request,
    status: str = "all",
    q: str = "",
    limit: int = 100,
    offset: int = 0,
    db: Session = Depends(get_db),
):
    require_admin_api(request, db)
    query = db.query(SelfServiceBill)
    st = (status or "all").strip().lower()
    if st != "all":
        query = query.filter(SelfServiceBill.status == st)
    search = (q or "").strip()
    if search:
        like = f"%{search}%"
        query = query.filter(
            or_(
                SelfServiceBill.bill_number.ilike(like),
                SelfServiceBill.receipt_email.ilike(like),
                SelfServiceBill.chip_purchase_id.ilike(like),
                SelfServiceBill.id.ilike(like),
            )
        )
    total_count = query.count()
    rows = (
        query.order_by(SelfServiceBill.created_at.desc())
        .offset(max(0, offset))
        .limit(min(500, max(1, limit)))
        .all()
    )

    all_rows = db.query(SelfServiceBill).all()
    paid_rev = sum(float(r.total_amount or 0.0) for r in all_rows if r.status == "paid")
    paid_cnt = sum(1 for r in all_rows if r.status == "paid")
    pending_cnt = sum(1 for r in all_rows if r.status == "pending")
    void_cnt = sum(1 for r in all_rows if r.status in ("void", "cancelled", "failed"))

    return JSONResponse(
        content={
            "bills": [_serialize_ss_bill(r) for r in rows],
            "total_count": total_count,
            "summary": {
                "paid_revenue": round(paid_rev, 2),
                "paid_count": paid_cnt,
                "pending_count": pending_cnt,
                "void_count": void_cnt,
            },
        },
        headers=NO_CACHE_HEADERS,
    )


@router.post("/api/admin/selfservice-bills/{bill_id}/status")
async def update_admin_selfservice_bill_status(
    bill_id: str,
    request: Request,
    db: Session = Depends(get_db),
):
    admin = require_admin_api(request, db)
    bill = db.query(SelfServiceBill).filter(SelfServiceBill.id == bill_id.strip()).first()
    if not bill:
        raise HTTPException(status_code=404, detail="Self-service bill not found")

    try:
        payload = await request.json()
    except Exception:
        payload = {}
    new_status = str((payload or {}).get("status") or "").strip().lower()
    reason = str((payload or {}).get("reason") or "").strip()
    if new_status not in ("paid", "pending", "void", "cancelled"):
        raise HTTPException(status_code=400, detail="Invalid status")

    old_status = bill.status
    bill.status = new_status
    if new_status == "paid" and not bill.paid_at:
        bill.paid_at = datetime.utcnow()
    if reason:
        bill.void_reason = reason
    bill.updated_at = datetime.utcnow()

    db.add(
        AuditLog(
            admin_username=getattr(admin, "username", "admin"),
            action="selfservice_bill_status",
            details=f"{bill.bill_number}: {old_status} -> {new_status}" + (f" ({reason})" if reason else ""),
            timestamp=datetime.utcnow(),
        )
    )
    db.commit()
    return JSONResponse(content={"ok": True, "bill": _serialize_ss_bill(bill)}, headers=NO_CACHE_HEADERS)


@router.get("/api/admin/selfservice-bills/export")
def export_admin_selfservice_bills_csv(request: Request, db: Session = Depends(get_db)):
    require_admin_api(request, db)
    offset = _local_tz_offset()
    rows = db.query(SelfServiceBill).order_by(SelfServiceBill.created_at.desc()).all()

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["Bill Number", "Status", "Total (RM)", "Items", "Email", "CHIP Purchase ID", "Created At", "Paid At"])
    for r in rows:
        try:
            items = json.loads(r.items_json or "[]")
            items_str = "; ".join(f"{it.get('name')} x{it.get('quantity')}" for it in items)
        except Exception:
            items_str = ""
        c_at = (r.created_at + offset).strftime("%Y-%m-%d %H:%M:%S") if r.created_at else ""
        p_at = (r.paid_at + offset).strftime("%Y-%m-%d %H:%M:%S") if r.paid_at else ""
        writer.writerow([
            r.bill_number or "",
            r.status or "",
            f"{float(r.total_amount or 0.0):.2f}",
            items_str,
            r.receipt_email or "",
            r.chip_purchase_id or "",
            c_at,
            p_at,
        ])

    buf.seek(0)
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={
            "Content-Disposition": "attachment; filename=selfservice-bills.csv",
            **NO_CACHE_HEADERS,
        },
    )
