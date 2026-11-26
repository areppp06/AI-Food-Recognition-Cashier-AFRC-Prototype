"""Customer scan, AI recognition, bill sessions, Curlec payments, and feedback routes."""
from __future__ import annotations

import asyncio
import json
import os
import re
import uuid
from datetime import datetime
from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from sqlalchemy.orm import Session

from ..ai_service import RecognitionError, recognize_food
from ..db import CustomerUser, Dish, Feedback, SessionBill, next_bill_number
from ..loyalty import (
    apply_or_clear_bill_voucher,
    clear_unclaimed_points_cookie,
    get_current_customer,
    get_or_create_guest_session_id,
    revalidate_bill_voucher_before_pay,
    set_guest_loyalty_cookie,
    set_unclaimed_points_cookie,
    validate_voucher_for_user,
)
from ..schemas import BillItem, BillResponse, ErrorResponse
from ..services.bill_service import _bill_payload, _expire_stale_bills, _find_active_bill
from ..services.config_service import NO_CACHE_HEADERS, UPLOAD_DIR, _rate_limit_config, _restaurant_profile, _restaurant_settings
from ..services.menu_service import _bill_quantity, _dish_alias_map, _dish_json
from ..services.payment_service import (
    _create_curlec_order,
    _curlec_client,
    _normalize_my_phone,
    _record_captured_payment,
    _verify_and_capture,
)
from ..services.r2_storage import (
    _R2_BILL_KEYS,
    _R2_MISSING_TTL_SEC,
    _R2_MISSING_UNTIL,
    _R2_SESSION_KEYS,
    _fetch_plate_from_r2,
    _get_bill_image_meta,
    _record_failed_scan,
    _save_bill_image_key,
    _upload_plate_to_r2,
)
from ..services.security_service import (
    _check_cooldown,
    _client_ip,
    _get_client_token,
    _get_device_id,
    _get_scan_session_id,
    _record_and_enforce_scan_limit,
    _require_turnstile,
    _scan_usage_status,
    _set_cooldown,
    _verify_bill_ownership,
)
from .deps import get_db

router = APIRouter()


@router.get("/api/client-state")
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
    settings = _restaurant_settings()

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
                "start": settings["operation_hours_start"],
                "end": settings["operation_hours_end"],
                "is_open": settings["is_open"],
                "text": settings["operation_hour_text"],
                "current_server_time": settings["current_server_time"],
            },
            "coming_soon": settings["coming_soon"],
            "maintenance_mode": settings["maintenance_mode"],
            "email_receipt_enabled": settings["email_receipt_enabled"],
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


@router.post("/api/recognize")
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
            detail="System is currently under maintenance. Please try again later.",
        )
    if settings["coming_soon"]:
        raise HTTPException(
            status_code=503,
            detail="System is not yet launched. Coming soon!",
        )
    if not settings["is_open"]:
        op_text = settings["operation_hour_text"]
        raise HTTPException(
            status_code=403,
            detail=f"You are outside our operation hour. Our operation hour is {op_text} please comeback later.",
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
            message=f"Your rate limit hit, try again after {rem_mins}min.",
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
            message=f"Your rate limit hit, try again after {cfg['cooldown_minutes']}min.",
        ).dict())

    if not file.content_type or not file.content_type.startswith("image/"):
        raise HTTPException(status_code=400, detail=ErrorResponse(
            error_code="INVALID_IMAGE",
            message="Please capture a photo using the camera. Gallery uploads are not allowed.",
        ).dict())

    try:
        image_bytes = await file.read()
        if len(image_bytes) < 1000:
            raise HTTPException(status_code=400, detail=ErrorResponse(
                error_code="UPLOAD_FAILED",
                message="Photo is too small or empty. Please retake.",
            ).dict())
        if len(image_bytes) > 1 * 1024 * 1024:
            raise HTTPException(status_code=413, detail=ErrorResponse(
                error_code="IMAGE_TOO_LARGE",
                message="Photo exceeds 1MB limit. Please retake using camera.",
            ).dict())
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=400, detail=ErrorResponse(
            error_code="UPLOAD_FAILED",
            message="Failed to receive photo. Please try again.",
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
            retake_required=True,
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
            message="AI service is temporarily down. Please try again in a moment.",
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
            message="No matching dishes found on the plate. Please retake or check today's menu.",
        ).dict())

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
            piece_count=piece_count,
        ))

    if not bill_items:
        await _record_failed_scan(
            db=db,
            image_bytes=image_bytes,
            rejection_code="NO_FOOD_DETECTED",
            rejection_message="No matching dishes found on the plate. Please retake or check today's menu.",
            raw_ai=json.dumps([i.dict() for i in ai_items]),
            session_id=scan_sid_val,
            bill_number=scan_bill_no,
            is_retry=is_retry_scan,
            ip=client_ip_val,
            device_id=d_id,
        )
        raise HTTPException(status_code=422, detail=ErrorResponse(
            error_code="NO_FOOD_DETECTED",
            message="No matching dishes found on the plate. Please retake or check today's menu.",
        ).dict())

    gsid, _ = get_or_create_guest_session_id(request)
    current_cust = get_current_customer(request, db)
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
        if gsid and not target_bill.guest_session_id:
            target_bill.guest_session_id = gsid
        if current_cust and not target_bill.customer_user_id:
            target_bill.customer_user_id = current_cust.id
        if target_bill.voucher_id and target_bill.voucher_code:
            voucher_owner = current_cust
            if not voucher_owner and target_bill.customer_user_id:
                voucher_owner = db.query(CustomerUser).filter(CustomerUser.id == target_bill.customer_user_id).first()
            if voucher_owner:
                try:
                    _, new_disc = validate_voucher_for_user(db, voucher_owner, target_bill.voucher_code, total)
                    target_bill.discount_amount = new_disc
                except HTTPException:
                    target_bill.voucher_id = None
                    target_bill.voucher_code = ""
                    target_bill.discount_amount = 0.0
            else:
                target_bill.voucher_id = None
                target_bill.voucher_code = ""
                target_bill.discount_amount = 0.0
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
            guest_session_id=gsid,
            customer_user_id=current_cust.id if current_cust else None,
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
    set_guest_loyalty_cookie(resp, request, gsid)
    return resp


@router.get("/api/sessions/{session_id}")
def get_session(session_id: str, request: Request, db: Session = Depends(get_db)):
    _expire_stale_bills(db)
    bill = db.query(SessionBill).filter(SessionBill.id == session_id).first()
    if not bill:
        raise HTTPException(status_code=404, detail="Bill not found")
    _verify_bill_ownership(bill, request, db)
    c_token = _get_client_token(request)
    gsid, _ = get_or_create_guest_session_id(request)
    current_cust = get_current_customer(request, db)
    changed = False
    if bill.status == "retaking":
        bill.status = "open"
        changed = True
    if bill.status in ("open", "pending"):
        if not bill.client_token and c_token:
            bill.client_token = c_token
            changed = True
        if gsid and not bill.guest_session_id:
            bill.guest_session_id = gsid
            changed = True
        if current_cust and bill.customer_user_id != current_cust.id:
            bill.customer_user_id = current_cust.id
            changed = True
    if changed:
        db.commit()
    resp = JSONResponse(content=_bill_payload(bill, alias_map=_dish_alias_map(db)), headers=NO_CACHE_HEADERS)
    resp.set_cookie("afrc_client_id", c_token, max_age=86400 * 30, path="/", samesite="lax")
    set_guest_loyalty_cookie(resp, request, gsid)
    if bill.status in ("open", "pending"):
        resp.set_cookie("afrc_active_session", bill.id, max_age=3600, path="/", samesite="lax")
    elif bill.status in ("paid", "void", "cancelled"):
        resp.delete_cookie("afrc_active_session", path="/")
        if bill.status == "paid" and (bill.points_status or "").lower() == "unclaimed" and bill.customer_user_id is None:
            pts_net = max(0, int(bill.points_earned or 0) - int(bill.points_clawed_back or 0))
            if pts_net > 0:
                set_unclaimed_points_cookie(resp, request, bill.guest_session_id or gsid, bill.id, pts_net)
    return resp


@router.post("/api/sessions/{session_id}/voucher")
def update_session_voucher(session_id: str, payload: dict, request: Request, db: Session = Depends(get_db)):
    _expire_stale_bills(db)
    bill = db.query(SessionBill).filter(SessionBill.id == session_id).first()
    if not bill:
        raise HTTPException(status_code=404, detail="Bill not found")
    _verify_bill_ownership(bill, request, db, str((payload or {}).get("client_token") or ""))
    current_cust = get_current_customer(request, db)
    code = str((payload or {}).get("code") or "")
    updated_bill = apply_or_clear_bill_voucher(db, bill, current_cust, code)
    return JSONResponse(
        content=_bill_payload(updated_bill, alias_map=_dish_alias_map(db)),
        headers=NO_CACHE_HEADERS,
    )


@router.post("/api/sessions/{session_id}/start-retake")
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


@router.get("/api/sessions/{session_id}/image")
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


@router.post("/api/sessions/{session_id}/need-cashier")
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


@router.post("/api/sessions/{session_id}/cancel")
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


@router.post("/api/sessions/{session_id}/void-retake")
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


@router.post("/api/pay")
def pay(payload: dict, request: Request, db: Session = Depends(get_db)):
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
        if not re.match(r"^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+$", email) or len(email) > 100:
            raise HTTPException(status_code=400, detail="Enter a valid email address or leave it blank")

    gsid, _ = get_or_create_guest_session_id(request)
    current_cust = get_current_customer(request, db)
    if current_cust:
        bill.customer_user_id = current_cust.id
    elif gsid and not bill.guest_session_id:
        bill.guest_session_id = gsid

    revalidate_bill_voucher_before_pay(db, bill, current_cust)

    bill.receipt_phone = phone
    bill.receipt_email = email
    bill.updated_at = datetime.utcnow()
    db.commit()
    checkout = _create_curlec_order(bill, db)
    checkout["status"] = "pending"
    resp = JSONResponse(content=checkout, headers=NO_CACHE_HEADERS)
    set_guest_loyalty_cookie(resp, request, gsid)
    return resp


@router.post("/api/pay/confirm")
def pay_confirm(payload: dict, request: Request, db: Session = Depends(get_db)):
    bill, state = _verify_and_capture(
        payload.get("razorpay_order_id"),
        payload.get("razorpay_payment_id"),
        payload.get("razorpay_signature"),
        db,
    )
    if state != "paid" or not bill:
        raise HTTPException(status_code=402, detail="Payment was not completed")
    gsid, _ = get_or_create_guest_session_id(request)
    resp = JSONResponse(
        content={
            "status": "paid",
            "session_id": bill.id,
            "points_earned": int(bill.points_earned or 0),
            "points_status": bill.points_status or "none",
        },
        headers=NO_CACHE_HEADERS,
    )
    set_guest_loyalty_cookie(resp, request, bill.guest_session_id or gsid)
    if bill.customer_user_id is None and (bill.points_status or "").lower() == "unclaimed" and int(bill.points_earned or 0) > 0:
        set_unclaimed_points_cookie(resp, request, bill.guest_session_id or gsid, bill.id, int(bill.points_earned))
    else:
        clear_unclaimed_points_cookie(resp)
    return resp


@router.post("/api/pay/callback")
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
        gsid, _ = get_or_create_guest_session_id(request)
        resp = RedirectResponse(f"/payment/success?session={quote(session_id)}", status_code=303)
        set_guest_loyalty_cookie(resp, request, bill.guest_session_id or gsid)
        if bill.customer_user_id is None and (bill.points_status or "").lower() == "unclaimed" and int(bill.points_earned or 0) > 0:
            set_unclaimed_points_cookie(resp, request, bill.guest_session_id or gsid, bill.id, int(bill.points_earned))
        else:
            clear_unclaimed_points_cookie(resp)
        return resp
    target = "/pay?payment=failed"
    if session_id:
        target = f"/pay?session={quote(session_id)}&payment=failed"
    return RedirectResponse(target, status_code=303)


@router.post("/api/curlec/webhook")
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
            detail="Invalid webhook signature",
        )

    if event.get("event") in ("payment.captured", "order.paid"):
        entity = ((event.get("payload") or {}).get("payment") or {}).get("entity") or {}
        order_id = str(entity.get("order_id") or "").strip()
        payment = entity
        bill = db.query(SessionBill).filter(SessionBill.curlec_order_id == order_id).first()
        if bill and bill.status != "paid" and str(payment.get("status")) == "captured":
            _record_captured_payment(bill, payment, db)
    return {"status": "ok"}


@router.post("/api/feedback")
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
