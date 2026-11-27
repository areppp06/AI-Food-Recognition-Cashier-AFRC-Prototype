"""Client identification, bill ownership verification, scan rate limiting, Turnstile, and login security."""
from __future__ import annotations

import hashlib
import logging
import math
import os
from datetime import datetime, timedelta

import httpx
from fastapi import HTTPException, Request
from sqlalchemy.orm import Session

from ..auth import get_current_admin
from ..db import AuditLog, ClientCooldown, ScanAttempt
from ..loyalty import get_current_customer, get_or_create_guest_session_id
from ..schemas import ErrorResponse
from .config_service import _rate_limit_config

logger = logging.getLogger("afrc")

_FAILED_LOGINS_IP: dict[str, list[float]] = {}
_FAILED_LOGINS_USER: dict[str, list[float]] = {}
MAX_LOGIN_FAILURES = 5
LOGIN_LOCKOUT_SECONDS = 15 * 60


def _mask_phone(phone: str) -> str:
    p = str(phone or "").strip()
    if not p or len(p) < 6:
        return p
    if p.startswith("+"):
        prefix = p[:4]
        suffix = p[-3:]
        stars = "•" * max(3, len(p) - 7)
        return f"{prefix}{stars}{suffix}"
    prefix = p[:3]
    suffix = p[-3:]
    stars = "•" * max(3, len(p) - 6)
    return f"{prefix}{stars}{suffix}"


def _mask_email(email: str) -> str:
    e = str(email or "").strip()
    if not e or "@" not in e:
        return e
    parts = e.split("@", 1)
    user, domain = parts[0], parts[1]
    if len(user) <= 2:
        masked_user = user[0] + "•"
    else:
        masked_user = user[0] + ("•" * max(2, len(user) - 2)) + user[-1]
    return f"{masked_user}@{domain}"


def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        parts = [p.strip() for p in forwarded.split(",") if p.strip()]
        if parts:
            return parts[0]
    if request.client and request.client.host:
        return request.client.host
    return ""


def _device_fingerprint(request: Request) -> str:
    ip = _client_ip(request)
    ua = request.headers.get("user-agent", "").strip()
    raw = f"{ip}|{ua}"
    return "fp-" + hashlib.sha256(raw.encode("utf-8", errors="ignore")).hexdigest()[:28]


def _get_device_id(request: Request, explicit_device: str = "", explicit_client: str = "") -> str:
    dev = (
        (explicit_device or "").strip()
        or request.cookies.get("afrc_device_id", "").strip()
        or request.headers.get("x-afrc-device", "").strip()
        or (explicit_client or "").strip()
        or request.cookies.get("afrc_client_id", "").strip()
        or request.headers.get("x-afrc-client", "").strip()
    )
    if dev and len(dev) <= 128:
        return dev
    return _device_fingerprint(request)


def _get_client_token(request: Request, explicit: str = "") -> str:
    token = (
        (explicit or "").strip()
        or request.cookies.get("afrc_client_id", "").strip()
        or request.headers.get("x-afrc-client", "").strip()
    )
    if token and len(token) <= 128:
        return token
    return _device_fingerprint(request)


def _all_client_tokens(request: Request, explicit: str = "", explicit_device: str = "") -> list:
    tokens = []
    for candidate in (
        (explicit or "").strip(),
        (explicit_device or "").strip(),
        request.cookies.get("afrc_client_id", "").strip(),
        request.cookies.get("afrc_device_id", "").strip(),
        request.headers.get("x-afrc-client", "").strip(),
        request.headers.get("x-afrc-device", "").strip(),
        _device_fingerprint(request),
    ):
        if candidate and len(candidate) <= 128 and candidate not in tokens:
            tokens.append(candidate)
    return tokens


def _all_session_ids(request: Request, explicit_session: str = "") -> list:
    sids = []
    for candidate in (
        (explicit_session or "").strip(),
        request.headers.get("x-afrc-session", "").strip(),
        request.cookies.get("afrc_browser_session", "").strip(),
        request.cookies.get("afrc_active_session", "").strip(),
    ):
        if candidate and len(candidate) <= 128 and candidate not in sids:
            sids.append(candidate)
    return sids


def _get_scan_session_id(request: Request, explicit_session: str = "") -> str:
    sid = (
        request.headers.get("x-afrc-session", "").strip()
        or request.cookies.get("afrc_browser_session", "").strip()
        or (explicit_session or "").strip()
        or request.cookies.get("afrc_active_session", "").strip()
    )
    if sid and len(sid) <= 128:
        return sid
    return ""


def _verify_bill_ownership(bill, request: Request, db, explicit_token: str = "") -> None:
    current_admin = get_current_admin(request, db)
    if current_admin:
        return
    if getattr(bill, "customer_user_id", None):
        current_cust = get_current_customer(request, db)
        if current_cust and current_cust.id == bill.customer_user_id:
            return
    gsid, _ = get_or_create_guest_session_id(request)
    if gsid and getattr(bill, "guest_session_id", "") and bill.guest_session_id == gsid:
        return
    if not bill.client_token:
        return
    c_token = _get_client_token(request, explicit_token)
    if not c_token or c_token != bill.client_token:
        raise HTTPException(
            status_code=403,
            detail="You do not have permission to modify this bill."
        )


def _rate_limit_keys(
    request: Request,
    client_token: str = "",
    session_id: str = "",
    device_id: str = "",
) -> list:
    keys = []
    for t in _all_client_tokens(request, client_token, device_id):
        for prefix in ("token:", "device:"):
            k = f"{prefix}{t}"
            if k not in keys:
                keys.append(k)
    ip = _client_ip(request)
    if ip:
        keys.append(f"ip:{ip}")
    for sid in _all_session_ids(request, session_id):
        k = f"session:{sid}"
        if k not in keys:
            keys.append(k)
    return keys


def _scan_usage_status(
    request: Request,
    client_token: str,
    db: Session,
    session_id: str = "",
    device_id: str = "",
) -> dict:
    cfg = _rate_limit_config()
    window_start = datetime.utcnow() - timedelta(minutes=cfg["scan_window_minutes"])
    dev_ids = _all_client_tokens(request, client_token, device_id)
    sids = _all_session_ids(request, session_id)
    ip = _client_ip(request)

    device_scans = (
        db.query(ScanAttempt)
        .filter(
            ScanAttempt.created_at >= window_start,
            ScanAttempt.device_id.in_(dev_ids),
        )
        .count()
        if dev_ids
        else 0
    )
    session_scans = (
        db.query(ScanAttempt)
        .filter(
            ScanAttempt.created_at >= window_start,
            ScanAttempt.session_id.in_(sids),
        )
        .count()
        if sids
        else 0
    )
    ip_scans = (
        db.query(ScanAttempt)
        .filter(
            ScanAttempt.created_at >= window_start,
            ScanAttempt.ip == ip,
        )
        .count()
        if ip
        else 0
    )

    sess_limit = cfg["max_scans_per_session"]
    dev_limit = cfg["max_scans_per_device"]
    ip_limit = cfg["max_scans_per_ip"]
    used = max(device_scans, session_scans)
    remaining = min(
        max(0, sess_limit - session_scans),
        max(0, dev_limit - device_scans),
        max(0, ip_limit - ip_scans),
    )
    return {
        "device_scans": device_scans,
        "session_scans": session_scans,
        "ip_scans": ip_scans,
        "scans_used": used,
        "scans_limit": min(sess_limit, dev_limit),
        "scans_remaining": remaining,
    }


def _check_cooldown(
    request: Request,
    client_token: str,
    db: Session,
    session_id: str = "",
    device_id: str = "",
) -> tuple[int, str]:
    now = datetime.utcnow()
    keys = _rate_limit_keys(
        request,
        client_token=client_token,
        session_id=session_id,
        device_id=device_id,
    )
    if not keys:
        return 0, ""
    rows = db.query(ClientCooldown).filter(ClientCooldown.client_key.in_(keys)).all()
    max_remaining = 0
    reason = ""
    for r in rows:
        if r.cooldown_until and r.cooldown_until > now:
            rem = math.ceil((r.cooldown_until - now).total_seconds() / 60.0)
            if rem > max_remaining:
                max_remaining = rem
                reason = (r.reason or "").strip()
    return max_remaining, reason


def _set_cooldown(
    request: Request,
    client_token: str,
    db: Session,
    session_id: str = "",
    device_id: str = "",
    reason: str = "",
) -> None:
    cfg = _rate_limit_config()
    until = datetime.utcnow() + timedelta(minutes=cfg["cooldown_minutes"])
    keys = _rate_limit_keys(
        request,
        client_token=client_token,
        session_id=session_id,
        device_id=device_id,
    )
    for k in keys:
        row = db.query(ClientCooldown).filter(ClientCooldown.client_key == k).first()
        if row:
            row.cooldown_until = until
            if reason:
                row.reason = reason
        else:
            db.add(ClientCooldown(client_key=k, cooldown_until=until, reason=reason))
    db.commit()


def _record_and_enforce_scan_limit(
    request: Request,
    client_token: str,
    db: Session,
    session_id: str = "",
    device_id: str = "",
) -> dict:
    cfg = _rate_limit_config()
    usage = _scan_usage_status(
        request,
        client_token=client_token,
        db=db,
        session_id=session_id,
        device_id=device_id,
    )
    reason = ""
    if usage["session_scans"] >= cfg["max_scans_per_session"]:
        reason = f"Too many photo scan attempts in this session (max {cfg['max_scans_per_session']} per {cfg['scan_window_minutes']} min)."
    elif usage["device_scans"] >= cfg["max_scans_per_device"]:
        reason = f"Too many photo scan attempts from this device (max {cfg['max_scans_per_device']} per {cfg['scan_window_minutes']} min)."
    elif usage["ip_scans"] >= cfg["max_scans_per_ip"]:
        reason = f"Too many photo scan attempts from your network IP (max {cfg['max_scans_per_ip']} per {cfg['scan_window_minutes']} min)."

    if reason:
        _set_cooldown(
            request,
            client_token=client_token,
            db=db,
            session_id=session_id,
            device_id=device_id,
            reason=reason,
        )
        raise HTTPException(
            status_code=429,
            detail=ErrorResponse(
                error_code="RATE_LIMIT",
                message=f"Your rate limit hit, try again after {cfg['cooldown_minutes']}min.",
            ).dict(),
        )

    dev_id = _get_device_id(request, explicit_device=device_id, explicit_client=client_token)
    sid = _get_scan_session_id(request, explicit_session=session_id)
    ip = _client_ip(request)
    db.add(
        ScanAttempt(
            ip=ip,
            device_id=dev_id,
            session_id=sid,
            created_at=datetime.utcnow(),
        )
    )
    db.commit()
    return _scan_usage_status(
        request,
        client_token=client_token,
        db=db,
        session_id=session_id,
        device_id=device_id,
    )


def _require_turnstile(token: str, expected_action: str, request: Request) -> None:
    hostnames = {
        hostname.strip().lower()
        for hostname in os.getenv("TURNSTILE_HOSTNAMES", "").split(",")
        if hostname.strip()
    }
    domain_env = (os.getenv("DOMAIN") or "").strip().lower()
    if domain_env:
        hostnames.add(domain_env)
        if not domain_env.startswith("www."):
            hostnames.add(f"www.{domain_env}")
        else:
            hostnames.add(domain_env[4:])
    hostnames.update({"localhost", "127.0.0.1"})

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

    result_hostname = str(result.get("hostname") or "").strip().lower()
    result_base = result_hostname[4:] if result_hostname.startswith("www.") else result_hostname
    hostname_matches = any(
        result_hostname == h or result_base == (h[4:] if h.startswith("www.") else h)
        for h in hostnames
    )

    if (
        result.get("success") is not True
        or result.get("action") != expected_action
        or not hostname_matches
    ):
        logger.warning(
            f"Turnstile siteverify rejected: success={result.get('success')}, action={result.get('action')} "
            f"(expected {expected_action}), hostname={result_hostname} (allowed: {hostnames}), "
            f"error-codes={result.get('error-codes')}"
        )
        raise HTTPException(status_code=403, detail="forbidden")


def _check_login_rate_limit(request: Request, username: str) -> None:
    now = datetime.utcnow().timestamp()
    ip = _client_ip(request)
    cutoff = now - LOGIN_LOCKOUT_SECONDS
    for bucket, key, label in [
        (_FAILED_LOGINS_IP, ip, "network IP"),
        (_FAILED_LOGINS_USER, username.strip().lower(), f"account '{username}'"),
    ]:
        if not key:
            continue
        attempts = [t for t in bucket.get(key, []) if t > cutoff]
        bucket[key] = attempts
        if len(attempts) >= MAX_LOGIN_FAILURES:
            oldest = attempts[0]
            remaining_mins = max(1, math.ceil((oldest + LOGIN_LOCKOUT_SECONDS - now) / 60))
            raise HTTPException(
                status_code=429,
                detail=f"Too many failed login attempts for this {label}. Locked for {remaining_mins} more minute(s).",
            )


def _record_login_failure(request: Request, username: str, db: Session) -> None:
    now = datetime.utcnow().timestamp()
    ip = _client_ip(request)
    u = username.strip().lower()
    if ip:
        _FAILED_LOGINS_IP.setdefault(ip, []).append(now)
    if u:
        _FAILED_LOGINS_USER.setdefault(u, []).append(now)
    try:
        db.add(AuditLog(
            cashier_id=0,
            admin_username=username[:100],
            action="LOGIN_FAILED",
            details=f"Failed login attempt for '{username}' from IP {ip}",
        ))
        db.commit()
    except Exception:
        pass


def _record_login_success(request: Request, username: str, user_id: int, db: Session) -> None:
    ip = _client_ip(request)
    u = username.strip().lower()
    _FAILED_LOGINS_USER.pop(u, None)
    if ip and ip in _FAILED_LOGINS_IP:
        _FAILED_LOGINS_IP.pop(ip, None)
    try:
        db.add(AuditLog(
            cashier_id=user_id,
            admin_username=username[:100],
            action="LOGIN_SUCCESS",
            details=f"Admin '{username}' logged in successfully from IP {ip}",
        ))
        db.commit()
    except Exception:
        pass
