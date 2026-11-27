"""Configuration, environment caching, restaurant profile, operation hours, and Caddy sync."""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from datetime import datetime, timedelta
from pathlib import Path

from dotenv import dotenv_values

from ..db import ENV_DIR, ROOT

UPLOAD_DIR = Path(__file__).resolve().parent.parent.parent / "uploads"

NO_CACHE_HEADERS = {
    "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
    "Pragma": "no-cache",
    "Expires": "0",
    "X-Robots-Tag": "noindex, nofollow, noarchive, nosnippet",
}

FORBIDDEN_SESSION_HTML = """<!DOCTYPE html>
<html>
<head>
    <title>403 Forbidden</title>
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
</head>
<body style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0;background:#f8fafc;padding:1rem;">
    <div style="text-align:center;padding:2.5rem 2rem;background:#fff;border-radius:16px;box-shadow:0 4px 20px rgba(0,0,0,0.06);max-width:420px;border:1px solid #e2e8f0;">
        <div style="font-size:3rem;margin-bottom:1rem;">🔒</div>
        <h1 style="color:#0f172a;font-size:1.5rem;margin:0 0 0.5rem 0;font-weight:700;">403 Forbidden</h1>
        <p style="color:#64748b;font-size:0.95rem;line-height:1.5;margin:0;">You do not have permission to view or pay for this bill session.</p>
    </div>
</body>
</html>"""

MAX_RETAKES = int(os.getenv("MAX_RETAKES", "4"))
COOLDOWN_MINUTES = int(os.getenv("COOLDOWN_MINUTES", "25"))
MAX_SCANS_PER_WINDOW = int(os.getenv("MAX_SCANS_PER_WINDOW", "5"))
MAX_SCANS_PER_SESSION = int(os.getenv("MAX_SCANS_PER_SESSION", str(MAX_SCANS_PER_WINDOW)))
MAX_SCANS_PER_DEVICE = int(os.getenv("MAX_SCANS_PER_DEVICE", str(MAX_SCANS_PER_WINDOW)))
MAX_SCANS_PER_IP = int(os.getenv("MAX_SCANS_PER_IP", "6"))
SCAN_WINDOW_MINUTES = int(os.getenv("SCAN_WINDOW_MINUTES", "15"))
BILL_EXPIRY_MINUTES = int(os.getenv("BILL_EXPIRY_MINUTES", "15"))

_RL_FILE_CACHE: dict[str, tuple[tuple[int, int] | None, dict]] = {}


def _read_env_file_cached(path: Path) -> dict:
    key = str(path)
    try:
        st = path.stat()
        sig = (st.st_mtime_ns, st.st_size)
    except OSError:
        _RL_FILE_CACHE.pop(key, None)
        return {}
    cached = _RL_FILE_CACHE.get(key)
    if cached and cached[0] == sig:
        return cached[1]
    data = {k: v for k, v in dotenv_values(path).items() if v is not None and str(v).strip() != ""}
    _RL_FILE_CACHE[key] = (sig, data)
    return data


def _rl_int(merged_file: dict, key: str, default: int, minimum: int = 1) -> int:
    raw = merged_file.get(key)
    if raw is None:
        raw = os.getenv(key)
    if raw is None or str(raw).strip() == "":
        return max(minimum, int(default))
    try:
        return max(minimum, int(str(raw).strip()))
    except (TypeError, ValueError):
        return max(minimum, int(default))


def _rate_limit_config() -> dict:
    merged: dict = {}
    merged.update(_read_env_file_cached(ENV_DIR / "rate-limit.env"))
    merged.update(_read_env_file_cached(ROOT / "rate-limit.env"))
    fallback_window_max = _rl_int(merged, "MAX_SCANS_PER_WINDOW", MAX_SCANS_PER_WINDOW)
    return {
        "max_scans_per_session": _rl_int(merged, "MAX_SCANS_PER_SESSION", fallback_window_max),
        "max_scans_per_device": _rl_int(merged, "MAX_SCANS_PER_DEVICE", fallback_window_max),
        "max_scans_per_ip": _rl_int(merged, "MAX_SCANS_PER_IP", MAX_SCANS_PER_IP),
        "scan_window_minutes": _rl_int(merged, "SCAN_WINDOW_MINUTES", SCAN_WINDOW_MINUTES),
        "cooldown_minutes": _rl_int(merged, "COOLDOWN_MINUTES", COOLDOWN_MINUTES),
        "max_retakes": _rl_int(merged, "MAX_RETAKES", MAX_RETAKES),
    }


def _local_tz_offset() -> timedelta:
    try:
        hours = float(os.getenv("TZ_OFFSET_HOURS", "8"))
    except (TypeError, ValueError):
        hours = 8.0
    return timedelta(hours=hours)


def _shift_year_month(year: int, month: int, delta_months: int) -> tuple[int, int]:
    total = year * 12 + (month - 1) + delta_months
    y, m_idx = divmod(total, 12)
    return y, m_idx + 1


def _csv_escape(val) -> str:
    s = str(val if val is not None else "")
    if s and s[0] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + s
    return s


def _build_maps_embed_url(maps_url: str, rest_name: str) -> str:
    url = (maps_url or "").strip()
    if not url:
        return f"https://maps.google.com/maps?q={rest_name}&output=embed" if rest_name else ""
    if "<iframe" in url.lower():
        m = re.search(r'src=["\']([^"\']+)["\']', url, re.IGNORECASE)
        if m:
            return m.group(1)
    if "maps/embed" in url or "pb=" in url:
        return url
    if "Zemkra7AmRtoGj6Z9" in url or "TakCakap" in rest_name or "Warisan Ibunda" in rest_name:
        return "https://www.google.com/maps/embed?pb=!1m18!1m12!1m3!1d87976.50620304717!2d100.38085804863282!3d5.325134800000003!2m3!1f0!2f0!3f0!3m2!1i1024!2i768!4f13.1!3m3!1m2!1s0x304ac814ae9d8007%3A0x4ba659682bb01a8d!2sTakCakap%20Nasi%20Campur%20(De%20Warisan%20Ibunda)!5e1!3m2!1sen!2smy!4v1794758235590!5m2!1sen!2smy"
    return f"https://maps.google.com/maps?q={rest_name}&output=embed"


def _restaurant_profile() -> dict:
    merged = {
        **_read_env_file_cached(ENV_DIR / ".env"),
        **_read_env_file_cached(ROOT / ".env"),
        **_read_env_file_cached(ENV_DIR / "restaurant.env"),
        **_read_env_file_cached(ROOT / "restaurant.env"),
    }
    rest_name = (
        merged.get("RESTAURANT_NAME")
        or merged.get("STORE_NAME")
        or os.getenv("RESTAURANT_NAME")
        or os.getenv("STORE_NAME")
        or "AFRC"
    ).strip() or "AFRC"
    comp_name = (
        merged.get("COMPANY_NAME")
        or os.getenv("COMPANY_NAME")
        or ""
    ).strip()
    bus_reg = (
        merged.get("BUSINESS_REG_NUMBER")
        or merged.get("COMPANY_REG_NUMBER")
        or os.getenv("BUSINESS_REG_NUMBER")
        or os.getenv("COMPANY_REG_NUMBER")
        or ""
    ).strip()
    comp_phone = (
        merged.get("COMPANY_PHONE")
        or os.getenv("COMPANY_PHONE")
        or ""
    ).strip()
    comp_email = (
        merged.get("COMPANY_EMAIL")
        or os.getenv("COMPANY_EMAIL")
        or ""
    ).strip()
    rest_addr = (
        merged.get("RESTAURANT_ADDRESS")
        or os.getenv("RESTAURANT_ADDRESS")
        or ""
    ).strip()
    g_url = (
        merged.get("GOOGLE_MAPS_URL")
        or os.getenv("GOOGLE_MAPS_URL")
        or (f"https://maps.google.com/?q={rest_name}" if rest_name else "https://maps.google.com")
    ).strip()
    g_embed = _build_maps_embed_url(g_url, rest_name)
    return {
        "restaurant_name": rest_name,
        "company_name": comp_name,
        "business_reg_number": bus_reg,
        "company_phone": comp_phone,
        "company_email": comp_email,
        "restaurant_address": rest_addr,
        "google_maps_url": g_url,
        "google_maps_embed_url": g_embed,
    }


def _restaurant_name() -> str:
    return _restaurant_profile()["restaurant_name"]


def _is_within_hours(current_str: str, start_str: str, end_str: str) -> bool:
    try:
        cur_h, cur_m = map(int, current_str.split(":"))
        st_h, st_m = map(int, start_str.split(":"))
        en_h, en_m = map(int, end_str.split(":"))
        cur = cur_h * 60 + cur_m
        st = st_h * 60 + st_m
        en = en_h * 60 + en_m
        if st <= en:
            return st <= cur <= en
        return cur >= st or cur <= en
    except Exception:
        return True


def _restaurant_settings() -> dict:
    env_data = {
        **_read_env_file_cached(ENV_DIR / ".env"),
        **_read_env_file_cached(ROOT / ".env"),
        **_read_env_file_cached(ENV_DIR / "restaurant.env"),
        **_read_env_file_cached(ROOT / "restaurant.env"),
    }
    coming_soon = env_data.get("COMING_SOON", "false").strip().lower() in ("true", "1", "yes")
    maintenance = env_data.get("MAINTENANCE_MODE", "false").strip().lower() in ("true", "1", "yes")
    email_receipt = env_data.get("ENABLE_EMAIL_RECEIPT", "false").strip().lower() in ("true", "1", "yes")
    op_start = env_data.get("OPERATION_HOURS_START", "08:00").strip() or "08:00"
    op_end = env_data.get("OPERATION_HOURS_END", "17:00").strip() or "17:00"

    now_local = datetime.utcnow() + _local_tz_offset()
    current_time_str = now_local.strftime("%H:%M")
    is_open = _is_within_hours(current_time_str, op_start, op_end)
    op_text = f"{op_start} - {op_end}"

    return {
        "coming_soon": coming_soon,
        "maintenance_mode": maintenance,
        "email_receipt_enabled": email_receipt,
        "operation_hours_start": op_start,
        "operation_hours_end": op_end,
        "is_open": is_open,
        "operation_hour_text": op_text,
        "current_server_time": current_time_str,
    }


def _save_restaurant_settings(updates: dict) -> dict:
    env_path = ENV_DIR / "restaurant.env"
    existing_lines = []
    if env_path.exists():
        with open(env_path, "r", encoding="utf-8") as f:
            existing_lines = f.readlines()

    keys_map = {}
    if "coming_soon" in updates:
        keys_map["COMING_SOON"] = "true" if updates["coming_soon"] else "false"
    if "maintenance_mode" in updates:
        keys_map["MAINTENANCE_MODE"] = "true" if updates["maintenance_mode"] else "false"
    if "email_receipt_enabled" in updates:
        keys_map["ENABLE_EMAIL_RECEIPT"] = "true" if updates["email_receipt_enabled"] else "false"
    elif "enable_email_receipt" in updates:
        keys_map["ENABLE_EMAIL_RECEIPT"] = "true" if updates["enable_email_receipt"] else "false"
    if "operation_hours_start" in updates and updates["operation_hours_start"]:
        keys_map["OPERATION_HOURS_START"] = str(updates["operation_hours_start"]).strip()
    if "operation_hours_end" in updates and updates["operation_hours_end"]:
        keys_map["OPERATION_HOURS_END"] = str(updates["operation_hours_end"]).strip()

    new_lines = []
    handled_keys = set()
    for line in existing_lines:
        s_line = line.strip()
        if s_line and not s_line.startswith("#") and "=" in s_line:
            k, _ = s_line.split("=", 1)
            k = k.strip()
            if k in keys_map:
                new_lines.append(f"{k}={keys_map[k]}\n")
                handled_keys.add(k)
                continue
        new_lines.append(line)

    for k, v in keys_map.items():
        if k not in handled_keys:
            if new_lines and not new_lines[-1].endswith("\n"):
                new_lines.append("\n")
            new_lines.append(f"{k}={v}\n")

    with open(env_path, "w", encoding="utf-8") as f:
        f.writelines(new_lines)

    _RL_FILE_CACHE.pop(str(env_path), None)
    return _restaurant_settings()


def _sync_caddy_domain_sync() -> None:
    raw_domain = os.getenv("DOMAIN", "").strip()
    if not raw_domain:
        return
    caddy_file = Path("/etc/caddy/Caddyfile")
    if not caddy_file.parent.exists():
        return
    caddy_bin = shutil.which("caddy")
    if not caddy_bin:
        return

    domains = [d.strip() for d in re.split(r"[,;\s]+", raw_domain) if d.strip()]
    if not domains:
        return
    domain_header = ", ".join(domains)
    port = os.getenv("PORT", "8000").strip() or "8000"

    expected_content = f"""{domain_header} {{
    header {{
        X-Frame-Options "SAMEORIGIN"
        X-Content-Type-Options "nosniff"
        Referrer-Policy "strict-origin-when-cross-origin"
        X-Robots-Tag "noindex, nofollow, noarchive, nosnippet"
        -Server
    }}
    request_body {{
        max_size 2MB
    }}
    reverse_proxy 127.0.0.1:{port} {{
        header_up -CF-Connecting-IP
        trusted_proxies 173.245.48.0/20 103.21.244.0/22 103.22.200.0/22 103.31.4.0/22 141.101.64.0/18 108.162.192.0/18 190.93.240.0/20 188.114.96.0/20 197.234.240.0/22 198.41.128.0/17 162.158.0.0/15 104.16.0.0/13 104.24.0.0/14 172.64.0.0/13 131.0.72.0/22 2400:cb00::/32 2606:4700::/32 2803:f800::/32 2405:b500::/32 2405:8100::/32 2a06:98c0::/29 2c0f:f248::/32
    }}
}}
"""
    try:
        current_content = caddy_file.read_text(encoding="utf-8") if caddy_file.exists() else ""
        if current_content.strip() != expected_content.strip():
            caddy_file.write_text(expected_content, encoding="utf-8")
            subprocess.run(
                ["caddy", "reload", "--config", str(caddy_file)],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
            )
    except Exception:
        pass
