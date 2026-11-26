"""Menu, categories, dish serialization, and portion pricing helpers."""
from __future__ import annotations

import json
import math
import re

from fastapi import HTTPException
from sqlalchemy.orm import Session

from ..db import Category, Dish, SessionLocal


def _clean_description(value) -> str:
    text = str(value or "").strip()
    if len(text) > 500:
        raise HTTPException(status_code=400, detail="Description must be 500 characters or less")
    return text


def _clean_pieces(value) -> int:
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


def _guess_category_icon(name: str) -> str:
    low = (name or "").strip().lower()
    if any(k in low for k in ("coffee", "espresso", "latte", "tea", "mocha", "cappuccino", "kopi", "teh")):
        return "local_cafe"
    if any(k in low for k in ("bakery", "pastry", "pastries", "bread", "croissant", "scone", "bun", "roti")):
        return "bakery_dining"
    if any(k in low for k in ("sandwich", "bowl", "lunch", "panini", "toast", "wrap", "salad")):
        return "lunch_dining"
    if any(k in low for k in ("cold", "drink", "beverage", "juice", "soda", "smoothie", "water", "air", "minuman", "jus")):
        return "glass_cup"
    if any(k in low for k in ("dessert", "sweet", "cake", "cookie", "ice cream", "brownie", "kuih", "pencuci mulut")):
        return "cake"
    if any(k in low for k in ("rice", "nasi", "main", "dinner", "meal", "noodle", "mee", "pasta", "ayam", "lauk", "kari")):
        return "dinner_dining"
    if any(k in low for k in ("side", "snack", "add", "fries", "tapas", "appetizer", "keropok")):
        return "tapas"
    if any(k in low for k in ("breakfast", "sarapan", "pagi")):
        return "bakery_dining"
    if any(k in low for k in ("set", "combo")):
        return "set_meal"
    return "restaurant"


def _parse_dish_categories(raw_val) -> list[str]:
    if not raw_val:
        return []
    items = []
    if isinstance(raw_val, list):
        items = [str(x).strip() for x in raw_val if str(x).strip()]
    else:
        s = str(raw_val).strip()
        if s.startswith("["):
            try:
                parsed = json.loads(s)
                if isinstance(parsed, list):
                    items = [str(x).strip() for x in parsed if str(x).strip()]
            except Exception:
                items = [x.strip() for x in re.split(r"[;|,]", s) if x.strip()]
        else:
            items = [x.strip() for x in re.split(r"[;|,]", s) if x.strip()]
    deduped = []
    seen = set()
    for c in items:
        low = c.casefold()
        if low not in seen:
            seen.add(low)
            deduped.append(c)
    return deduped[:3]


def _clean_categories(raw_val, db: Session = None, auto_create: bool = False) -> list[str]:
    if not raw_val:
        return []
    if isinstance(raw_val, list):
        raw_list = [str(x).strip() for x in raw_val if str(x).strip()]
    else:
        s = str(raw_val).strip()
        if s.startswith("["):
            try:
                parsed = json.loads(s)
                raw_list = [str(x).strip() for x in parsed if str(x).strip()] if isinstance(parsed, list) else []
            except Exception:
                raw_list = [x.strip() for x in re.split(r"[;|,]", s) if x.strip()]
        else:
            raw_list = [x.strip() for x in re.split(r"[;|,]", s) if x.strip()]

    deduped = []
    seen = set()
    for c in raw_list:
        low = c.casefold()
        if low not in seen:
            seen.add(low)
            deduped.append(c)

    if len(deduped) > 3:
        raise HTTPException(status_code=400, detail="Each menu item can be tagged with at most 3 categories")

    if db is not None and deduped:
        existing_cats = db.query(Category).order_by(Category.sort_order.asc(), Category.id.asc()).all()
        cat_map = {c.name.casefold(): c.name for c in existing_cats}
        max_order = max((c.sort_order or 0 for c in existing_cats), default=-1)
        resolved = []
        for c_name in deduped:
            low = c_name.casefold()
            if low in cat_map:
                resolved.append(cat_map[low])
            elif auto_create:
                max_order += 1
                new_cat = Category(
                    name=c_name,
                    icon=_guess_category_icon(c_name),
                    sort_order=max_order,
                )
                db.add(new_cat)
                db.flush()
                cat_map[low] = new_cat.name
                resolved.append(new_cat.name)
            else:
                resolved.append(c_name)
        return resolved

    return deduped


def _dish_json(dish: Dish) -> dict:
    return {
        "id": dish.id,
        "name": dish.name,
        "alias": dish.alias or "",
        "display_name": (dish.alias or "").strip() or dish.name,
        "price": dish.price,
        "available": dish.available,
        "unit": dish.unit,
        "description": dish.description or "",
        "pieces_per_portion": dish.pieces_per_portion or 1,
        "categories": _parse_dish_categories(getattr(dish, "categories", "[]")),
    }


def _dish_alias_map(db: Session = None) -> dict[str, str]:
    close_after = False
    if db is None:
        db = SessionLocal()
        close_after = True
    try:
        dishes = db.query(Dish.name, Dish.alias).all()
        mapping = {}
        for name, alias in dishes:
            if name:
                disp = (alias or "").strip() or name
                mapping[name] = disp
                mapping[name.casefold()] = disp
                mapping[" ".join(name.strip().casefold().split())] = disp
        return mapping
    finally:
        if close_after:
            db.close()
