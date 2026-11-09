from pydantic import BaseModel
from typing import List, Optional

class DishBase(BaseModel):
    name: str
    price: float
    available: bool = True
    unit: str = "portion"

class DishCreate(DishBase):
    pass

class Dish(DishBase):
    id: int
    class Config:
        from_attributes = True

class AIItem(BaseModel):
    name: str
    quantity: float
    confidence: float

class AIResponse(BaseModel):
    items: List[AIItem]

class BillItem(BaseModel):
    name: str
    quantity: float
    unit_price: float
    subtotal: float
    confidence: float
    needs_verification: bool

class BillResponse(BaseModel):
    items: List[BillItem]
    total: float
    low_confidence: bool
    session_id: str

class VerifyRequest(BaseModel):
    session_id: str
    pin: str
    corrections: List[dict]  # [{"name": "Chicken", "quantity": 2}, ...]
