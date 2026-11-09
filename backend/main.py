from fastapi import FastAPI, UploadFile, File, HTTPException, Depends, Form
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from sqlalchemy.orm import Session
from typing import List
import uuid
import json
from datetime import datetime

from .db import SessionLocal, init_db, Dish, Cashier, AuditLog, SessionBill
from .schemas import Dish as DishSchema, BillResponse, BillItem, VerifyRequest, ErrorResponse
from .ai_service import recognize_food, RecognitionError

app = FastAPI(title="AFRC Prototype", version="1.0")

# Serve frontend
app.mount("/static", StaticFiles(directory="frontend"), name="static")

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

@app.get("/api/menu")
def get_menu(db: Session = Depends(get_db)):
    dishes = db.query(Dish).all()
    return [{"id": d.id, "name": d.name, "price": d.price, "available": d.available, "unit": d.unit} for d in dishes]

@app.post("/api/menu")
def update_menu(dishes: List[dict], db: Session = Depends(get_db)):
    for d in dishes:
        dish = db.query(Dish).filter(Dish.id == d["id"]).first()
        if dish:
            dish.available = d.get("available", dish.available)
            dish.price = d.get("price", dish.price)
    db.commit()
    return {"status": "updated"}

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

    available = [{"name": d.name, "available": d.available, "unit": d.unit, "price": d.price}
                 for d in db.query(Dish).filter(Dish.available == True).all()]

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
    low_conf = False
    dish_map = {d.name: d for d in db.query(Dish).all()}

    for item in ai_items:
        dish = dish_map.get(item.name)
        if not dish or not dish.available:
            continue
        conf = item.confidence
        needs = conf < 0.85
        if needs:
            low_conf = True
        sub = dish.price * item.quantity
        total += sub
        bill_items.append(BillItem(
            name=dish.name,
            quantity=item.quantity,
            unit_price=dish.price,
            subtotal=round(sub, 2),
            confidence=conf,
            needs_verification=needs
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
        low_confidence=low_conf,
        session_id=session_id
    )

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
                "subtotal": round(sub, 2), "confidence": 1.0, "needs_verification": False
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
