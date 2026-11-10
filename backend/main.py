from fastapi import FastAPI, UploadFile, File, HTTPException, Depends, Form, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from sqlalchemy import func
from sqlalchemy.orm import Session
from typing import Annotated, List
import math
import os
import uuid
import json
import httpx
import razorpay
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

from .db import SessionLocal, init_db, Dish, Cashier, AuditLog, SessionBill, next_bill_number
from .schemas import Dish as DishSchema, BillResponse, BillItem, VerifyRequest, ErrorResponse
from .ai_service import recognize_food, RecognitionError

UPLOAD_DIR = Path(__file__).resolve().parent.parent / "uploads"
app = FastAPI(title="AFRC Prototype", version="1.0")

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
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    if request.client and request.client.host:
        return request.client.host
    return ""

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

@app.on_event("startup")
def startup():
    init_db()

@app.get("/")
async def root():
    return FileResponse("frontend/index.html")

@app.get("/admin")
async def admin():
    return FileResponse("frontend/admin.html")

@app.get("/cashier")
async def cashier():
    return FileResponse("frontend/cashier.html")

@app.get("/pay")
async def pay_page():
    return FileResponse("frontend/pay.html")

@app.get("/payment/success")
async def payment_success_page():
    return FileResponse("frontend/success.html")

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

@app.post("/api/recognize")
async def recognize(
    request: Request,
    file: UploadFile = File(...),
    cf_turnstile_response: Annotated[str, Form(alias="cf-turnstile-response")] = "",
    db: Session = Depends(get_db),
):
    _require_turnstile(cf_turnstile_response, "recognize", request)
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
    except Exception:
        raise HTTPException(status_code=400, detail=ErrorResponse(
            error_code="UPLOAD_FAILED",
            message="Failed to receive photo. Please try again."
        ).dict())

    available = [_dish_json(d) for d in db.query(Dish).filter(Dish.available == True).all()]
    # The menu is now a plain list. End the read so SQLite is free while Grok runs.
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

    session_id = str(uuid.uuid4())
    bill_number = next_bill_number(db)
    try:
        UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        (UPLOAD_DIR / f"{session_id}.jpg").write_bytes(image_bytes)
    except OSError:
        pass
    session = SessionBill(
        id=session_id,
        bill_number=bill_number,
        raw_ai=json.dumps([i.dict() for i in ai_items]),
        final_bill=json.dumps([b.dict() for b in bill_items]),
    )
    db.add(session)
    db.commit()

    return BillResponse(
        items=bill_items,
        total=round(total, 2),
        session_id=session_id,
        bill_number=bill_number,
    )

def _bill_payload(session: SessionBill) -> dict:
    items = json.loads(session.final_bill or "[]")
    total = round(sum(float(item.get("subtotal") or 0) for item in items), 2)
    img_path = UPLOAD_DIR / f"{session.id}.jpg"
    return {
        "session_id": session.id,
        "bill_number": session.bill_number or f"AAA-0001",
        "items": items,
        "total": total,
        "status": session.status or "open",
        "receipt_email": session.receipt_email or "",
        "curlec_order_id": session.curlec_order_id or "",
        "curlec_payment_id": session.curlec_payment_id or "",
        "created_at": (session.created_at.isoformat() + "Z") if session.created_at else "",
        "paid_at": (session.paid_at.isoformat() + "Z") if session.paid_at else "",
        "has_image": img_path.is_file(),
    }

@app.get("/api/sessions/{session_id}")
def get_session(session_id: str, db: Session = Depends(get_db)):
    bill = db.query(SessionBill).filter(SessionBill.id == session_id).first()
    if not bill:
        raise HTTPException(status_code=404, detail="Bill not found")
    return _bill_payload(bill)

@app.get("/api/sessions/{session_id}/image")
def get_session_image(session_id: str, db: Session = Depends(get_db)):
    bill = db.query(SessionBill).filter(SessionBill.id == session_id).first()
    if not bill:
        raise HTTPException(status_code=404, detail="Bill not found")
    img_path = UPLOAD_DIR / f"{bill.id}.jpg"
    if not img_path.is_file():
        raise HTTPException(status_code=404, detail="Image not found")
    return FileResponse(img_path, media_type="image/jpeg")

@app.post("/api/sessions/{session_id}/need-cashier")
def need_cashier(session_id: str, db: Session = Depends(get_db)):
    bill = db.query(SessionBill).filter(SessionBill.id == session_id).first()
    if not bill:
        raise HTTPException(status_code=404, detail="Bill not found")
    if bill.status == "paid":
        raise HTTPException(status_code=409, detail="This bill is already paid")
    bill.status = "needs_cashier"
    db.commit()
    return {"status": "needs_cashier"}

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
    session_id = str(payload.get("session_id") or "").strip()
    bill = db.query(SessionBill).filter(SessionBill.id == session_id).first()
    if not bill:
        raise HTTPException(status_code=404, detail="Bill not found")
    if bill.status == "needs_cashier":
        raise HTTPException(status_code=409, detail="A cashier is checking this bill")
    if bill.status == "paid":
        return {"status": "paid", "session_id": bill.id}
    email = str(payload.get("email") or "").strip()
    if email and ("@" not in email or "." not in email.split("@")[-1]):
        raise HTTPException(status_code=400, detail="Enter a valid email or leave it blank")
    bill.receipt_email = email
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
        },
        "bills": bills,
    }

@app.post("/api/admin/bills/{session_id}/status")
def update_admin_bill_status(session_id: str, payload: dict, db: Session = Depends(get_db)):
    bill = db.query(SessionBill).filter(SessionBill.id == session_id).first()
    if not bill:
        raise HTTPException(status_code=404, detail="Bill not found")
    new_status = str(payload.get("status") or "").strip().lower()
    if new_status not in ("open", "pending", "paid", "needs_cashier", "cancelled"):
        raise HTTPException(status_code=400, detail="Invalid status")
    bill.status = new_status
    if new_status == "paid" and not bill.paid_at:
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
