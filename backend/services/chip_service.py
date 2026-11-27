"""CHIP (chip-in.asia) payment gateway helpers for /selfservice."""
from __future__ import annotations

import logging
import os
from datetime import datetime
from typing import Any

import httpx
from fastapi import HTTPException, Request
from sqlalchemy.orm import Session

from ..db import ENV_DIR, ROOT, SelfServiceBill
from .config_service import _read_env_file_cached, _restaurant_profile

logger = logging.getLogger("afrc")

CHIP_API_BASE = "https://gate.chip-in.asia/api/v1"


def _payment_env() -> dict[str, str]:
    merged: dict[str, str] = {}
    for path in (
        ROOT / "payment.env",
        ENV_DIR / "payment.env",
        ROOT / "chip.env",
        ENV_DIR / "chip.env",
    ):
        merged.update(_read_env_file_cached(path))
    return merged


def _chip_config() -> tuple[str, str]:
    env_data = _payment_env()
    secret_key = (env_data.get("CHIP_SECRET_KEY") or os.getenv("CHIP_SECRET_KEY", "")).strip()
    brand_id = (env_data.get("CHIP_BRAND_ID") or os.getenv("CHIP_BRAND_ID", "")).strip()
    if not secret_key or not brand_id:
        raise HTTPException(
            status_code=503,
            detail="CHIP payment gateway is not configured in payment.env.",
        )
    return secret_key, brand_id


def _external_base_url(request: Request) -> str:
    forwarded_proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    forwarded_host = request.headers.get(
        "x-forwarded-host",
        request.headers.get("host", str(request.base_url.netloc)),
    )
    if not forwarded_host or forwarded_host == "testserver":
        forwarded_host = "takcakap.com"
        forwarded_proto = "https"
    return f"{forwarded_proto}://{forwarded_host}"


def create_chip_purchase_for_selfservice(
    request: Request,
    bill: SelfServiceBill,
    items: list[dict[str, Any]],
) -> dict[str, Any]:
    """Create a CHIP purchase for a SelfServiceBill and return {purchase_id, checkout_url}."""
    secret_key, brand_id = _chip_config()
    amount_sen = int(round(float(bill.total_amount or 0.0) * 100))
    if amount_sen < 10:
        raise HTTPException(
            status_code=400,
            detail="Minimum payment amount is RM 0.10.",
        )

    base_url = _external_base_url(request)
    callback_url = f"{base_url}/api/selfservice/chip-callback?bill_id={bill.id}"
    cancel_url = f"{base_url}/selfservice?payment=cancelled"
    webhook_url = f"{base_url}/api/selfservice/chip-webhook"

    products_payload: list[dict[str, Any]] = []
    prod_sum = 0
    for it in items:
        qty = max(1, int(it.get("quantity") or 1))
        u_sen = int(round(float(it.get("unit_price") or 0.0) * 100))
        prod_sum += u_sen * qty
        products_payload.append({
            "name": str(it.get("name") or "Item")[:64],
            "price": u_sen,
            "quantity": qty,
        })
    if products_payload and prod_sum != amount_sen:
        q0 = max(1, int(products_payload[0]["quantity"]))
        products_payload[0]["price"] = max(1, products_payload[0]["price"] + (amount_sen - prod_sum) // q0)

    prof = _restaurant_profile()
    store_name = (prof.get("restaurant_name") or "Gerai TakCakap").strip()
    email = (bill.receipt_email or "").strip()

    chip_payload = {
        "brand_id": brand_id,
        "client": {
            "email": email or f"kiosk_{bill.id[:8]}@takcakap.com",
            "full_name": store_name,
        },
        "purchase": {
            "currency": "MYR",
            "products": products_payload,
            "notes": f"{store_name} Self-Service {bill.bill_number}",
        },
        "reference": bill.bill_number,
        "send_receipt": bool(email),
        "payment_method_whitelist": [
            "duitnow_qr",
            "dnqr",
            "fpx",
            "visa",
            "mastercard",
            "razer_tng",
            "razer_grabpay",
            "razer_shopeepay",
        ],
        "success_redirect": f"{callback_url}&status=success",
        "failure_redirect": f"{callback_url}&status=failure",
        "cancel_redirect": cancel_url,
        "success_callback": webhook_url,
    }

    try:
        with httpx.Client(timeout=15.0) as client:
            resp = client.post(
                f"{CHIP_API_BASE}/purchases/",
                headers={
                    "Authorization": f"Bearer {secret_key}",
                    "Content-Type": "application/json",
                },
                json=chip_payload,
            )
        if resp.status_code not in (200, 201):
            logger.error(f"[CHIP] Purchase creation failed HTTP {resp.status_code}: {resp.text[:300]}")
            raise HTTPException(
                status_code=502,
                detail=f"CHIP gateway error (HTTP {resp.status_code}). Please try again.",
            )
        pdata = resp.json()
    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"[CHIP] Exception creating purchase for {bill.bill_number}: {e}")
        raise HTTPException(status_code=502, detail="Could not connect to CHIP payment gateway.")

    purchase_id = str(pdata.get("id") or "").strip()
    checkout_url = str(pdata.get("checkout_url") or "").strip()
    if not purchase_id or not checkout_url:
        logger.error(f"[CHIP] Unexpected response missing id/checkout_url: {pdata}")
        raise HTTPException(status_code=502, detail="Invalid response from CHIP payment gateway.")

    return {"purchase_id": purchase_id, "checkout_url": checkout_url}


def verify_and_mark_selfservice_paid(bill: SelfServiceBill, db: Session) -> bool:
    """Verify CHIP purchase status via CHIP API and mark SelfServiceBill paid if cleared."""
    if bill.status == "paid":
        return True
    pid = (bill.chip_purchase_id or "").strip()
    if not pid:
        return False
    secret_key, _brand_id = _chip_config()
    try:
        with httpx.Client(timeout=10.0) as client:
            resp = client.get(
                f"{CHIP_API_BASE}/purchases/{pid}/",
                headers={"Authorization": f"Bearer {secret_key}"},
            )
        if resp.status_code != 200:
            logger.error(f"[CHIP] Verify failed HTTP {resp.status_code} for {pid}")
            return False
        pdata = resp.json()
    except Exception as e:
        logger.error(f"[CHIP] Verify exception for {pid}: {e}")
        return False

    p_status = str(pdata.get("status") or "").lower()
    if p_status in ("paid", "cleared", "settled"):
        bill.status = "paid"
        bill.paid_at = bill.paid_at or datetime.utcnow()
        bill.updated_at = datetime.utcnow()
        db.commit()
        return True
    return False
