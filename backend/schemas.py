from pydantic import BaseModel, Field
from typing import List, Optional

class DishBase(BaseModel):
    name: str
    price: float
    available: bool = True
    unit: str = "portion"
    description: Optional[str] = ""
    pieces_per_portion: Optional[int] = 1

class DishCreate(DishBase):
    pass

class Dish(DishBase):
    id: int
    class Config:
        from_attributes = True

class AIItem(BaseModel):
    name: str
    quantity: float

class AIResponse(BaseModel):
    items: List[AIItem]
    error_code: Optional[str] = None
    error_message: Optional[str] = None

class BillItem(BaseModel):
    name: str
    quantity: float
    unit_price: float
    subtotal: float
    piece_count: Optional[int] = None

class BillResponse(BaseModel):
    items: List[BillItem]
    total: float
    session_id: str
    bill_number: Optional[str] = ""

class ErrorResponse(BaseModel):
    error_code: str
    message: str
    retake_required: bool = True

