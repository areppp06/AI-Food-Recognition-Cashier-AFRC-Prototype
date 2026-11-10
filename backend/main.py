from fastapi import FastAPI, UploadFile, File, HTTPException, Depends, Form
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from sqlalchemy import func
from sqlalchemy.orm import Session
from typing import List
import math
import uuid
import json
from datetime import datetime

from .db import SessionLocal, init_db, Dish, Cashier, AuditLog, SessionBill
from .schemas import Dish as DishSchema, BillResponse, BillItem, VerifyRequest, ErrorResponse
from .ai_service import recognize_food, RecognitionError

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
async def recognize(file: UploadFile = File(...), db: Session = Depends(get_db)):
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
    session = SessionBill(id=session_id, raw_ai=json.dumps([i.dict() for i in ai_items]),
                          final_bill=json.dumps([b.dict() for b in bill_items]))
    db.add(session)
    db.commit()

    return BillResponse(
        items=bill_items,
        total=round(total, 2),
        session_id=session_id
    )

def _bill_payload(session: SessionBill) -> dict:
    items = json.loads(session.final_bill or "[]")
    total = round(sum(float(item.get("subtotal") or 0) for item in items), 2)
    return {
        "session_id": session.id,
        "items": items,
        "total": total,
        "status": session.status or "open",
        "receipt_email": session.receipt_email or "",
    }

@app.get("/api/sessions/{session_id}")
def get_session(session_id: str, db: Session = Depends(get_db)):
    bill = db.query(SessionBill).filter(SessionBill.id == session_id).first()
    if not bill:
        raise HTTPException(status_code=404, detail="Bill not found")
    return _bill_payload(bill)

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

@app.post("/api/pay")
def pay(payload: dict, db: Session = Depends(get_db)):
    session_id = str(payload.get("session_id") or "").strip()
    bill = db.query(SessionBill).filter(SessionBill.id == session_id).first()
    if not bill:
        raise HTTPException(status_code=404, detail="Bill not found")
    if bill.status == "needs_cashier":
        raise HTTPException(status_code=409, detail="A cashier is checking this bill")
    email = str(payload.get("email") or "").strip()
    if email and ("@" not in email or "." not in email.split("@")[-1]):
        raise HTTPException(status_code=400, detail="Enter a valid email or leave it blank")
    if bill.status != "paid":
        bill.status = "paid"
        bill.receipt_email = email
        db.commit()
    return {"status": "paid", "session_id": bill.id}

@app.post("/api/verify")
def verify(req: VerifyRequest, db: Session = Depends(get_db)):
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

    # Log
    log = AuditLog(cashier_id=cashier.id, action="verify_bill",
                   details=f"session={req.session_id} total={total}")
    db.add(log)
    db.commit()

    return {"items": bill_items, "total": round(total, 2), "verified": True}

@app.get("/api/cashiers")
def list_cashiers(db: Session = Depends(get_db)):
    return [{"id": c.id, "name": c.name, "active": c.active} for c in db.query(Cashier).all()]
