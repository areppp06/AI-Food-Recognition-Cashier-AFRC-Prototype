"""Admin authentication routes (/api/auth/*)."""
from __future__ import annotations

import secrets
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy.orm import Session

from ..auth import (
    SESSION_COOKIE_NAME,
    SESSION_DURATION_DAYS,
    burn_setup_token,
    create_admin_session,
    get_current_admin,
    get_or_create_setup_token,
    hash_password,
    require_admin_api,
    revoke_session,
    verify_password,
)
from ..db import AdminSession, AdminUser, AuditLog
from ..services.config_service import _restaurant_name
from ..services.security_service import _check_login_rate_limit, _record_login_failure, _record_login_success
from .deps import get_db

router = APIRouter()


@router.get("/api/auth/status")
def auth_status(request: Request, db: Session = Depends(get_db)):
    admin_count = db.query(AdminUser).count()
    current_admin = get_current_admin(request, db)
    return {
        "needs_setup": admin_count == 0,
        "is_authenticated": current_admin is not None,
        "username": current_admin.username if current_admin else None,
        "restaurant_name": _restaurant_name() or "AFRC",
    }


@router.post("/api/auth/setup")
def auth_setup(payload: dict, request: Request, response: Response, db: Session = Depends(get_db)):
    if db.query(AdminUser).count() > 0:
        raise HTTPException(status_code=400, detail="Setup already completed. Please log in.")

    req_token = str(payload.get("setup_token") or payload.get("token") or "").strip()
    expected_token = get_or_create_setup_token()
    if not req_token or not secrets.compare_digest(req_token, expected_token):
        raise HTTPException(status_code=403, detail="Invalid or missing one-time setup token. Check your VPS terminal for the link or QR.")

    username = str(payload.get("username") or "").strip()
    password = str(payload.get("password") or "")
    if len(username) < 3:
        raise HTTPException(status_code=400, detail="Username must be at least 3 characters.")
    if len(password) < 6:
        raise HTTPException(status_code=400, detail="Password must be at least 6 characters.")

    user = AdminUser(
        username=username,
        password_hash=hash_password(password),
        created_at=datetime.utcnow(),
        last_login_at=datetime.utcnow(),
    )
    db.add(user)
    db.add(AuditLog(
        cashier_id=0,
        admin_username=username,
        action="ADMIN_INITIAL_SETUP",
        details=f"Initial admin '{username}' created from {request.client.host if request.client else 'local'}",
    ))
    db.commit()
    db.refresh(user)

    burn_setup_token()

    ip = request.client.host if request.client else ""
    ua = request.headers.get("user-agent", "")
    token = create_admin_session(db, user.id, ip, ua)

    is_https = request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=token,
        max_age=86400 * SESSION_DURATION_DAYS,
        httponly=True,
        samesite="lax",
        secure=is_https,
    )
    return {"ok": True, "username": user.username}


@router.post("/api/auth/login")
def auth_login(payload: dict, request: Request, response: Response, db: Session = Depends(get_db)):
    username = str(payload.get("username") or "").strip()
    password = str(payload.get("password") or "")

    _check_login_rate_limit(request, username)

    user = db.query(AdminUser).filter(AdminUser.username == username).first()
    if not user or not verify_password(password, user.password_hash):
        _record_login_failure(request, username, db)
        raise HTTPException(status_code=401, detail="Invalid username or password.")

    user.last_login_at = datetime.utcnow()
    db.commit()

    _record_login_success(request, user.username, user.id, db)

    ip = request.client.host if request.client else ""
    ua = request.headers.get("user-agent", "")
    token = create_admin_session(db, user.id, ip, ua)

    is_https = request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=token,
        max_age=86400 * SESSION_DURATION_DAYS,
        httponly=True,
        samesite="lax",
        secure=is_https,
    )
    return {"ok": True, "username": user.username}


@router.post("/api/auth/logout")
def auth_logout(request: Request, response: Response, db: Session = Depends(get_db)):
    token = request.cookies.get(SESSION_COOKIE_NAME)
    if token:
        revoke_session(db, token)
    response.delete_cookie(key=SESSION_COOKIE_NAME)
    return {"ok": True}


@router.post("/api/auth/change-password")
def auth_change_password(payload: dict, request: Request, db: Session = Depends(get_db)):
    current_admin = require_admin_api(request, db)
    old_password = str(payload.get("old_password") or "")
    new_password = str(payload.get("new_password") or "")

    if not verify_password(old_password, current_admin.password_hash):
        raise HTTPException(status_code=400, detail="Current password incorrect.")
    if len(new_password) < 6:
        raise HTTPException(status_code=400, detail="New password must be at least 6 characters.")

    current_admin.password_hash = hash_password(new_password)

    current_token = request.cookies.get(SESSION_COOKIE_NAME) or ""
    if not current_token:
        auth_h = request.headers.get("Authorization", "")
        if auth_h.startswith("Bearer "):
            current_token = auth_h[7:].strip()
    db.query(AdminSession).filter(
        AdminSession.user_id == current_admin.id,
        AdminSession.token != current_token,
    ).delete()

    db.add(AuditLog(
        cashier_id=current_admin.id,
        admin_username=current_admin.username,
        action="PASSWORD_CHANGE",
        details=f"Admin '{current_admin.username}' updated password (all other sessions revoked)",
    ))
    db.commit()
    return {"ok": True, "message": "Password updated successfully. Other active sessions revoked."}
