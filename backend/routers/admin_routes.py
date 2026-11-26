"""Admin management routes: bills, analytics, exports, refunds, audit logs, settings, failed scans, feedbacks."""
from __future__ import annotations

import csv
import io
import json
import logging
import math
import re
from datetime import datetime, timedelta
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from ..auth import require_admin_api
from ..db import AuditLog, BillRefund, Cashier, FailedScan, Feedback, SessionBill
from ..loyalty import apply_bill_refund_loyalty_effects
from ..services.analytics_service import _compute_overview_data
from ..services.bill_service import _bill_payload, _expire_stale_bills
from ..services.config_service import (
    NO_CACHE_HEADERS,
    UPLOAD_DIR,
    _csv_escape,
    _local_tz_offset,
    _restaurant_profile,
    _restaurant_settings,
    _save_restaurant_settings,
)
from ..services.menu_service import _dish_alias_map
from ..services.payment_service import (
    _IN_FLIGHT_REFUNDS,
    _REFUND_LOCK,
    _build_refund_info,
    _curlec_client,
)
from ..services.r2_storage import _fetch_plate_from_r2
from .deps import get_db

logger = logging.getLogger("afrc")

router = APIRouter()


@router.get("/api/admin/bills")
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


@router.get("/api/admin/analytics")
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


@router.get("/api/admin/export")
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


@router.post("/api/admin/bills/{session_id}/status")
def update_admin_bill_status(session_id: str, payload: dict, request: Request, db: Session = Depends(get_db)):
    current_admin = require_admin_api(request, db)
    bill = db.query(SessionBill).filter(SessionBill.id == session_id).first()
    if not bill:
        raise HTTPException(status_code=404, detail="Bill not found")
    prev_status = (bill.status or "").lower()
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
        details=f"Bill #{bill.bill_number}: status set to {new_status} by admin '{current_admin.username}'",
    ))
    db.commit()
    if prev_status == "paid" and bill.status == "void":
        bp = _bill_payload(bill)
        try:
            apply_bill_refund_loyalty_effects(
                db=db,
                bill_id=bill.id,
                total_refunded_rm=float(bp["total"]),
                refund_ref="void",
                reason="Voided by Admin",
            )
            db.refresh(bill)
        except Exception as e:
            logger.exception(f"Void loyalty clawback failed for bill {bill.id}: {e}")
    return _bill_payload(bill)


@router.get("/api/admin/bills/by-number/{bill_number}")
def get_admin_bill_by_number(bill_number: str, request: Request, db: Session = Depends(get_db)):
    require_admin_api(request, db)
    _expire_stale_bills(db)
    clean_no = bill_number.strip().lstrip("#")
    bill = db.query(SessionBill).filter(
        or_(
            func.lower(SessionBill.bill_number) == clean_no.lower(),
            func.lower(SessionBill.bill_number) == bill_number.strip().lower(),
        )
    ).first()
    if not bill:
        raise HTTPException(status_code=404, detail="Bill not found")
    refunded_amt = db.query(func.coalesce(func.sum(BillRefund.refund_amount), 0.0)).filter(
        BillRefund.session_id == bill.id,
        BillRefund.status == "completed",
    ).scalar() or 0.0
    return JSONResponse(content=_bill_payload(bill, float(refunded_amt), is_owner_or_admin=True, alias_map=_dish_alias_map(db)), headers=NO_CACHE_HEADERS)


@router.get("/api/admin/bills/{session_id}/refund-info")
def get_admin_bill_refund_info(session_id: str, request: Request, db: Session = Depends(get_db)):
    require_admin_api(request, db)
    bill = db.query(SessionBill).filter(SessionBill.id == session_id).first()
    if not bill:
        raise HTTPException(status_code=404, detail="Bill not found")
    return _build_refund_info(bill, db, sync_curlec_contact=True)


@router.post("/api/admin/bills/{session_id}/refund")
def refund_admin_bill(session_id: str, payload: dict, request: Request, db: Session = Depends(get_db)):
    current_admin = require_admin_api(request, db)
    with _REFUND_LOCK:
        if session_id in _IN_FLIGHT_REFUNDS:
            raise HTTPException(
                status_code=409,
                detail="A refund is currently in progress for this bill. Please wait a moment.",
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
        has_discount = float(bill.discount_amount or 0.0) > 0
        if not has_discount and total_refund > remaining_refundable + 0.01:
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
        cumulative_refunded = round(float(info.get("refunded_amount") or 0.0) + total_refund, 2)
        loyalty_refund_effect = {"clawed_back_points": 0, "voucher_restored": False}
        try:
            loyalty_refund_effect = apply_bill_refund_loyalty_effects(
                db=db,
                bill_id=bill.id,
                total_refunded_rm=cumulative_refunded,
                refund_ref=f"rfnd-{rf_row.id}",
                reason=reason or "Admin refund",
            )
            db.refresh(bill)
        except Exception as e:
            logger.exception(f"Refund loyalty clawback failed for bill {bill.id}: {e}")
        return {
            "status": "ok",
            "refund": {
                "id": rf_row.id,
                "curlec_refund_id": curlec_refund_id,
                "refund_amount": round(total_refund, 2),
                "status": refund_status,
                "items": refund_items_payload,
                "reason": reason,
                "loyalty": loyalty_refund_effect,
            },
            "refund_info": _build_refund_info(bill, db, sync_curlec_contact=False),
        }
    finally:
        with _REFUND_LOCK:
            _IN_FLIGHT_REFUNDS.discard(session_id)


@router.get("/api/admin/audit-logs")
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
                "System / Admin" if l.cashier_id == 0 else f"Cashier #{l.cashier_id}",
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


@router.get("/api/admin/restaurant/settings")
def get_admin_restaurant_settings(request: Request, db: Session = Depends(get_db)):
    require_admin_api(request, db)
    prof = _restaurant_profile()
    settings = _restaurant_settings()
    return {
        **settings,
        "restaurant_name": prof["restaurant_name"],
        "company_name": prof["company_name"],
    }


@router.post("/api/admin/restaurant/settings")
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
    if "email_receipt_enabled" in data or "enable_email_receipt" in data:
        changes.append(f"email_receipt={'ENABLED' if updated['email_receipt_enabled'] else 'DISABLED'}")
    if op_start is not None or op_end is not None:
        changes.append(f"operation_hours={updated['operation_hours_start']}-{updated['operation_hours_end']}")

    change_desc = ", ".join(changes) or "no changes"
    db.add(AuditLog(
        cashier_id=current_admin.id,
        admin_username=current_admin.username,
        action="RESTAURANT_SETTINGS_UPDATE",
        details=f"Admin '{current_admin.username}' updated restaurant settings: {change_desc}",
    ))
    db.commit()

    prof = _restaurant_profile()
    return {
        **updated,
        "restaurant_name": prof["restaurant_name"],
        "company_name": prof["company_name"],
    }


@router.get("/api/admin/failed-scans")
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
    require_admin_api(request, db)
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


@router.get("/api/admin/failed-scans/{scan_id}/image")
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


@router.get("/api/admin/feedbacks")
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
        },
    }
