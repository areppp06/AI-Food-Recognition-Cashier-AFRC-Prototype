"""Menu, categories, public pricelist, and CSV menu import/export routes."""
from __future__ import annotations

import csv
import io
import json
import re
from datetime import datetime
from typing import List

from fastapi import APIRouter, Depends, File, HTTPException, Request, Response, UploadFile
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from ..auth import require_admin_api
from ..db import AuditLog, Category, Dish, ROOT
from ..services.config_service import NO_CACHE_HEADERS, _local_tz_offset, _restaurant_profile
from ..services.menu_service import (
    _clean_categories,
    _clean_description,
    _clean_pieces,
    _dish_json,
    _guess_category_icon,
    _parse_dish_categories,
)
from .deps import get_db

router = APIRouter()


@router.get("/api/public-profile")
def get_public_profile():
    return _restaurant_profile()


@router.get("/api/pricelist")
def get_public_pricelist(db: Session = Depends(get_db)):
    dishes = db.query(Dish).order_by(Dish.name.asc()).all()
    categories = db.query(Category).order_by(Category.sort_order.asc(), Category.id.asc()).all()
    latest_audit = (
        db.query(AuditLog)
        .filter(or_(AuditLog.action.like("MENU_%"), AuditLog.action.like("CATEGORY_%")))
        .order_by(AuditLog.timestamp.desc())
        .first()
    )
    if latest_audit and latest_audit.timestamp:
        last_updated = latest_audit.timestamp.strftime("%Y-%m-%d %H:%M:%S")
    else:
        db_path = ROOT / "database.db"
        if not db_path.exists():
            db_path = ROOT / "afrc.db"
        if db_path.exists():
            last_updated = datetime.fromtimestamp(db_path.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")
        else:
            last_updated = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")

    items = []
    for d in dishes:
        pieces = d.pieces_per_portion or 1
        unit = d.unit or "portion"
        price = round(float(d.price or 0.0), 2)
        price_per_piece = round(price / pieces, 2) if pieces > 0 else price
        display_name = (d.alias or "").strip() or d.name
        cats = _parse_dish_categories(getattr(d, "categories", "[]"))
        items.append({
            "id": d.id,
            "name": display_name,
            "raw_name": d.name,
            "alias": d.alias or "",
            "price": price,
            "unit": unit,
            "pieces_per_portion": pieces,
            "price_per_piece": price_per_piece,
            "available": bool(d.available),
            "description": d.description or "",
            "categories": cats,
        })

    prof = _restaurant_profile()
    return {
        "restaurant_name": prof["restaurant_name"],
        "last_updated": last_updated,
        "items": items,
        "categories": [
            {
                "id": c.id,
                "name": c.name,
                "icon": c.icon or _guess_category_icon(c.name),
            }
            for c in categories
        ],
    }


@router.get("/api/categories")
def get_categories(db: Session = Depends(get_db)):
    cats = db.query(Category).order_by(Category.sort_order.asc(), Category.id.asc()).all()
    dishes = db.query(Dish).all()
    total_counts: dict[str, int] = {}
    avail_counts: dict[str, int] = {}
    for d in dishes:
        d_cats = _parse_dish_categories(getattr(d, "categories", "[]"))
        for c_name in d_cats:
            low = c_name.casefold()
            total_counts[low] = total_counts.get(low, 0) + 1
            if d.available:
                avail_counts[low] = avail_counts.get(low, 0) + 1

    return [
        {
            "id": c.id,
            "name": c.name,
            "icon": c.icon or _guess_category_icon(c.name),
            "sort_order": c.sort_order or 0,
            "dish_count": total_counts.get(c.name.casefold(), 0),
            "available_count": avail_counts.get(c.name.casefold(), 0),
        }
        for c in cats
    ]


@router.post("/api/admin/categories")
def add_category(payload: dict, request: Request, db: Session = Depends(get_db)):
    current_admin = require_admin_api(request, db)
    name = str(payload.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="Category name is required")
    if len(name) > 60:
        raise HTTPException(status_code=400, detail="Category name must be 60 characters or less")
    existing = db.query(Category).filter(func.lower(Category.name) == name.casefold()).first()
    if existing:
        raise HTTPException(status_code=409, detail="A category with this name already exists")

    icon = str(payload.get("icon") or "").strip() or _guess_category_icon(name)
    max_sort = db.query(func.max(Category.sort_order)).scalar()
    next_sort = (int(max_sort) + 1) if max_sort is not None else 0

    cat = Category(name=name, icon=icon, sort_order=next_sort)
    db.add(cat)
    db.add(AuditLog(
        cashier_id=current_admin.id,
        admin_username=current_admin.username,
        action="CATEGORY_ADD",
        details=f"Admin '{current_admin.username}' added category '{cat.name}'",
    ))
    db.commit()
    db.refresh(cat)
    return {
        "id": cat.id,
        "name": cat.name,
        "icon": cat.icon,
        "sort_order": cat.sort_order,
        "dish_count": 0,
        "available_count": 0,
    }


@router.put("/api/admin/categories/reorder")
def reorder_categories(payload: dict, request: Request, db: Session = Depends(get_db)):
    current_admin = require_admin_api(request, db)
    cat_ids = payload.get("category_ids") or payload.get("ids") or []
    if not isinstance(cat_ids, list):
        raise HTTPException(status_code=400, detail="category_ids must be a list of category IDs")

    cats = db.query(Category).all()
    cat_map = {c.id: c for c in cats}
    ordered_names = []
    idx = 0
    seen_ids = set()
    for raw_id in cat_ids:
        try:
            cid = int(raw_id)
        except (TypeError, ValueError):
            continue
        if cid in cat_map and cid not in seen_ids:
            seen_ids.add(cid)
            cat_map[cid].sort_order = idx
            ordered_names.append(cat_map[cid].name)
            idx += 1

    for c in sorted(cats, key=lambda x: (x.sort_order or 0, x.id)):
        if c.id not in seen_ids:
            c.sort_order = idx
            ordered_names.append(c.name)
            idx += 1

    db.add(AuditLog(
        cashier_id=current_admin.id,
        admin_username=current_admin.username,
        action="CATEGORY_REORDER",
        details=f"Admin '{current_admin.username}' reordered categories: {', '.join(ordered_names)}",
    ))
    db.commit()
    return {"ok": True, "categories": get_categories(db)}


@router.put("/api/admin/categories/{cat_id}")
def update_category(cat_id: int, payload: dict, request: Request, db: Session = Depends(get_db)):
    current_admin = require_admin_api(request, db)
    cat = db.query(Category).filter(Category.id == cat_id).first()
    if not cat:
        raise HTTPException(status_code=404, detail="Category not found")

    old_name = cat.name
    new_name = str(payload.get("name") if "name" in payload else cat.name).strip()
    if not new_name:
        raise HTTPException(status_code=400, detail="Category name cannot be empty")
    if new_name.casefold() != old_name.casefold():
        dup = db.query(Category).filter(func.lower(Category.name) == new_name.casefold(), Category.id != cat_id).first()
        if dup:
            raise HTTPException(status_code=409, detail="Another category with this name already exists")

    cat.name = new_name
    if "icon" in payload and str(payload.get("icon") or "").strip():
        cat.icon = str(payload.get("icon")).strip()

    if old_name != new_name:
        for d in db.query(Dish).all():
            d_cats = _parse_dish_categories(getattr(d, "categories", "[]"))
            updated_cats = [new_name if c.casefold() == old_name.casefold() else c for c in d_cats]
            if updated_cats != d_cats:
                d.categories = json.dumps(updated_cats)

    db.add(AuditLog(
        cashier_id=current_admin.id,
        admin_username=current_admin.username,
        action="CATEGORY_UPDATE",
        details=f"Admin '{current_admin.username}' updated category '{old_name}' -> '{new_name}'",
    ))
    db.commit()
    return {"ok": True, "id": cat.id, "name": cat.name, "icon": cat.icon, "sort_order": cat.sort_order}


@router.delete("/api/admin/categories/{cat_id}")
def delete_category(cat_id: int, request: Request, db: Session = Depends(get_db)):
    current_admin = require_admin_api(request, db)
    cat = db.query(Category).filter(Category.id == cat_id).first()
    if not cat:
        raise HTTPException(status_code=404, detail="Category not found")

    cat_name = cat.name
    removed_from_count = 0
    for d in db.query(Dish).all():
        d_cats = _parse_dish_categories(getattr(d, "categories", "[]"))
        filtered = [c for c in d_cats if c.casefold() != cat_name.casefold()]
        if len(filtered) != len(d_cats):
            d.categories = json.dumps(filtered)
            removed_from_count += 1

    db.delete(cat)
    db.add(AuditLog(
        cashier_id=current_admin.id,
        admin_username=current_admin.username,
        action="CATEGORY_DELETE",
        details=f"Admin '{current_admin.username}' deleted category '{cat_name}' (untagged from {removed_from_count} menu item(s))",
    ))
    db.commit()
    return {"status": "deleted", "id": cat_id, "name": cat_name}


@router.get("/api/menu")
def get_menu(db: Session = Depends(get_db)):
    dishes = db.query(Dish).all()
    return [_dish_json(d) for d in dishes]


@router.post("/api/menu")
def update_menu(dishes: List[dict], request: Request, db: Session = Depends(get_db)):
    current_admin = require_admin_api(request, db)
    for d in dishes:
        dish = db.query(Dish).filter(Dish.id == d["id"]).first()
        if dish:
            dish.available = d.get("available", dish.available)
            if "alias" in d:
                dish.alias = str(d.get("alias") or "").strip()
            if "price" in d:
                price_str = str(d["price"]).strip()
                try:
                    p = float(price_str)
                except (TypeError, ValueError):
                    raise HTTPException(status_code=400, detail=f"Invalid price for dish '{dish.name}'")
                if p < 0.10 or p > 10000:
                    raise HTTPException(status_code=400, detail=f"Price must be at least RM 0.10 and cannot exceed RM 10,000 for dish '{dish.name}'")
                if not re.match(r"^\d+(\.\d{1,2})?$", price_str) and round(p, 2) != round(p, 4):
                    raise HTTPException(status_code=400, detail=f"Price can have at most 2 decimal places for dish '{dish.name}'")
                dish.price = round(p, 2)
            if "description" in d:
                dish.description = _clean_description(d.get("description"))
            if "pieces_per_portion" in d:
                dish.pieces_per_portion = _clean_pieces(d.get("pieces_per_portion"))
            if "categories" in d:
                cleaned_cats = _clean_categories(d.get("categories"), db=db, auto_create=True)
                dish.categories = json.dumps(cleaned_cats)
    db.add(AuditLog(
        cashier_id=current_admin.id,
        admin_username=current_admin.username,
        action="MENU_UPDATE",
        details=f"Admin '{current_admin.username}' updated menu ({len(dishes)} item(s) modified)",
    ))
    db.commit()
    return {"status": "updated"}


@router.post("/api/menu/item")
def add_menu_item(payload: dict, request: Request, db: Session = Depends(get_db)):
    current_admin = require_admin_api(request, db)
    name = str(payload.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="Dish name is required")
    price_str = str(payload.get("price") or "").strip()
    try:
        price = float(price_str)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="A valid price is required")
    if price < 0.10 or price > 10000:
        raise HTTPException(status_code=400, detail="Price must be at least RM 0.10 and cannot exceed RM 10,000")
    if not re.match(r"^\d+(\.\d{1,2})?$", price_str) and round(price, 2) != round(price, 4):
        raise HTTPException(status_code=400, detail="Price can have at most 2 decimal places")
    existing = db.query(Dish).filter(func.lower(Dish.name) == name.casefold()).first()
    if existing:
        raise HTTPException(status_code=409, detail="A dish with this name already exists")
    unit = str(payload.get("unit") or "portion").strip() or "portion"
    alias = str(payload.get("alias") or "").strip()
    cleaned_cats = _clean_categories(payload.get("categories"), db=db, auto_create=True)
    dish = Dish(
        name=name,
        alias=alias,
        price=round(price, 2),
        available=bool(payload.get("available", True)),
        unit=unit,
        description=_clean_description(payload.get("description")),
        pieces_per_portion=_clean_pieces(payload.get("pieces_per_portion")),
        categories=json.dumps(cleaned_cats),
    )
    db.add(dish)
    cat_info = f" [Categories: {', '.join(cleaned_cats)}]" if cleaned_cats else ""
    db.add(AuditLog(
        cashier_id=current_admin.id,
        admin_username=current_admin.username,
        action="MENU_ADD",
        details=f"Admin '{current_admin.username}' added dish '{dish.name}' (RM {dish.price:.2f}){cat_info}",
    ))
    db.commit()
    db.refresh(dish)
    return _dish_json(dish)


@router.delete("/api/menu/{dish_id}")
def delete_menu_item(dish_id: int, request: Request, db: Session = Depends(get_db)):
    current_admin = require_admin_api(request, db)
    dish = db.query(Dish).filter(Dish.id == dish_id).first()
    if not dish:
        raise HTTPException(status_code=404, detail="Dish not found")
    d_name = dish.name
    db.delete(dish)
    db.add(AuditLog(
        cashier_id=current_admin.id,
        admin_username=current_admin.username,
        action="MENU_DELETE",
        details=f"Admin '{current_admin.username}' deleted dish '{d_name}'",
    ))
    db.commit()
    return {"status": "deleted", "id": dish_id}


@router.get("/api/admin/menu/export")
def export_admin_menu_csv(request: Request, db: Session = Depends(get_db)):
    require_admin_api(request, db)
    dishes = db.query(Dish).order_by(Dish.name.asc()).all()

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["Name", "Alias", "Price", "Categories", "Pieces", "Available", "Description"])
    for d in dishes:
        p_val = f"{d.price:.2f}" if d.price is not None else "0.00"
        avail_str = "Yes" if d.available else "No"
        pieces_val = d.pieces_per_portion if d.pieces_per_portion is not None else 1
        cats_str = "; ".join(_parse_dish_categories(getattr(d, "categories", "[]")))
        writer.writerow([
            d.name or "",
            d.alias or "",
            p_val,
            cats_str,
            pieces_val,
            avail_str,
            d.description or "",
        ])

    csv_bytes = ("\ufeff" + buf.getvalue()).encode("utf-8")
    date_tag = (datetime.utcnow() + _local_tz_offset()).strftime("%Y%m%d_%H%M%S")
    filename = f"afrc-menu-{date_tag}.csv"
    return Response(
        content=csv_bytes,
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            **NO_CACHE_HEADERS,
        },
    )


@router.post("/api/admin/menu/import")
async def import_admin_menu_csv(
    request: Request,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    current_admin = require_admin_api(request, db)

    fname = (file.filename or "").lower()
    if not fname.endswith(".csv"):
        raise HTTPException(status_code=400, detail="Only CSV files (.csv) are supported")

    content_bytes = await file.read()
    if not content_bytes:
        raise HTTPException(status_code=400, detail="Uploaded CSV file is empty")

    if len(content_bytes) > 5 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="File too large (maximum 5MB allowed)")

    text = None
    for enc in ("utf-8-sig", "utf-8", "latin-1", "cp1252"):
        try:
            text = content_bytes.decode(enc)
            break
        except (UnicodeDecodeError, LookupError):
            continue

    if text is None:
        raise HTTPException(status_code=400, detail="Unable to decode CSV file. Please upload a UTF-8 encoded CSV.")

    first_line = text.splitlines()[0] if text.splitlines() else ""
    delimiter = ";" if (";" in first_line and "," not in first_line) else ","

    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    rows = list(reader)
    if not rows:
        raise HTTPException(status_code=400, detail="CSV file contains no rows")

    raw_headers = rows[0]
    normalized_headers = [h.strip().lower() for h in raw_headers]

    col_map = {}
    for idx, h in enumerate(normalized_headers):
        if h in ("name", "dish name", "dish", "ai dish name", "item", "item name") and "name" not in col_map:
            col_map["name"] = idx
        elif h in ("alias", "customer alias", "display name") and "alias" not in col_map:
            col_map["alias"] = idx
        elif h in ("categories", "category", "tags", "tag") and "categories" not in col_map:
            col_map["categories"] = idx
        elif h in ("price", "price (rm)", "price rm", "rm", "cost") and "price" not in col_map:
            col_map["price"] = idx
        elif h in ("pieces", "piece", "pieces per portion", "pieces / portion", "portion", "qty", "count") and "pieces" not in col_map:
            col_map["pieces"] = idx
        elif h in ("available", "available today", "is available", "active", "status", "enabled") and "available" not in col_map:
            col_map["available"] = idx
        elif h in ("description", "desc", "ai description", "visual description", "ai visual description") and "description" not in col_map:
            col_map["description"] = idx

    if "name" not in col_map:
        raise HTTPException(
            status_code=400,
            detail="CSV is missing the 'Name' column header. Required column: Name (optional: Alias, Price, Categories, Pieces, Available, Description).",
        )

    new_dishes = []
    seen_names = set()

    for row_idx, row in enumerate(rows[1:], start=2):
        if not row or not any(field.strip() for field in row):
            continue

        def get_val(key, default=""):
            idx = col_map.get(key)
            if idx is not None and idx < len(row):
                return row[idx].strip()
            return default

        name = get_val("name")
        if not name:
            raise HTTPException(status_code=400, detail=f"Row {row_idx}: Dish Name cannot be empty")

        name_lower = name.casefold()
        if name_lower in seen_names:
            raise HTTPException(status_code=400, detail=f"Row {row_idx}: Duplicate dish name '{name}' found in CSV")
        seen_names.add(name_lower)

        alias = get_val("alias")

        price_raw = get_val("price", "0")
        price_clean = re.sub(r"[^0-9.]", "", price_raw.replace(",", "."))
        try:
            price_val = float(price_clean) if price_clean else 0.0
        except ValueError:
            raise HTTPException(status_code=400, detail=f"Row {row_idx} ('{name}'): Invalid price '{price_raw}'")

        if price_val < 0.10 or price_val > 10000:
            raise HTTPException(status_code=400, detail=f"Row {row_idx} ('{name}'): Price must be between RM 0.10 and RM 10,000")

        pieces_raw = get_val("pieces", "1")
        try:
            pieces_val = int(re.sub(r"[^0-9]", "", pieces_raw)) if pieces_raw else 1
            if pieces_val < 1:
                pieces_val = 1
            elif pieces_val > 100:
                pieces_val = 100
        except ValueError:
            pieces_val = 1

        avail_raw = get_val("available", "true").lower()
        if avail_raw in ("false", "no", "0", "n", "f", "unavailable", "off", "disable", "disabled"):
            avail_val = False
        else:
            avail_val = True

        desc_raw = get_val("description")
        if len(desc_raw) > 500:
            desc_raw = desc_raw[:500]

        cats_raw = get_val("categories")
        cleaned_cats = _clean_categories(cats_raw, db=db, auto_create=True)

        dish = Dish(
            name=name,
            alias=alias,
            price=round(price_val, 2),
            available=avail_val,
            unit="portion",
            description=desc_raw,
            pieces_per_portion=pieces_val,
            categories=json.dumps(cleaned_cats),
        )
        new_dishes.append(dish)

    if not new_dishes:
        raise HTTPException(status_code=400, detail="No valid menu items found in CSV")

    try:
        db.query(Dish).delete()
        for d in new_dishes:
            db.add(d)
        db.add(AuditLog(
            cashier_id=current_admin.id,
            admin_username=current_admin.username,
            action="MENU_IMPORT",
            details=f"Admin '{current_admin.username}' imported menu CSV overwriting with {len(new_dishes)} item(s)",
        ))
        db.commit()
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Failed to overwrite menu: {str(e)}")

    return {
        "ok": True,
        "count": len(new_dishes),
        "message": f"Successfully imported and overwrote menu with {len(new_dishes)} items.",
    }
