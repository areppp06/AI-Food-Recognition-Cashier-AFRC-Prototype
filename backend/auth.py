from pathlib import Path
import bcrypt
import secrets
import sys
from datetime import datetime, timedelta
from typing import Optional
from fastapi import Request, HTTPException, Depends
from sqlalchemy.orm import Session
from backend.db import AdminUser, AdminSession, SessionLocal, ENV_DIR

SESSION_COOKIE_NAME = "afrc_admin_session"
SESSION_DURATION_DAYS = 7


def hash_password(password: str) -> str:
    """Hashes a plain text password with bcrypt salt."""
    salt = bcrypt.gensalt(rounds=12)
    return bcrypt.hashpw(password.encode("utf-8"), salt).decode("utf-8")


def verify_password(password: str, hashed: str) -> bool:
    """Verifies a plain text password against a bcrypt hash."""
    if not password or not hashed:
        return False
    try:
        return bcrypt.checkpw(password.encode("utf-8"), hashed.encode("utf-8"))
    except Exception:
        return False


def create_admin_session(db: Session, user_id: int, ip: str = "", user_agent: str = "") -> str:
    """Generates a secure random session token and stores it in the database."""
    token = secrets.token_urlsafe(32)
    expires_at = datetime.utcnow() + timedelta(days=SESSION_DURATION_DAYS)
    sess = AdminSession(
        token=token,
        user_id=user_id,
        ip=ip[:100] if ip else "",
        user_agent=user_agent[:255] if user_agent else "",
        created_at=datetime.utcnow(),
        expires_at=expires_at,
    )
    db.add(sess)
    db.commit()
    return token


def get_session_admin(db: Session, token: str) -> Optional[AdminUser]:
    """Retrieves the AdminUser for a valid, unexpired session token."""
    if not token:
        return None
    now = datetime.utcnow()
    sess = (
        db.query(AdminSession)
        .filter(AdminSession.token == token, AdminSession.expires_at > now)
        .first()
    )
    if not sess:
        return None
    return db.query(AdminUser).filter(AdminUser.id == sess.user_id).first()


def revoke_session(db: Session, token: str) -> None:
    """Immediately invalidates a session token in the database."""
    if not token:
        return
    db.query(AdminSession).filter(AdminSession.token == token).delete()
    db.commit()


def revoke_all_user_sessions(db: Session, user_id: int) -> None:
    """Revokes all active sessions for a specific user."""
    db.query(AdminSession).filter(AdminSession.user_id == user_id).delete()
    db.commit()


def get_current_admin(request: Request, db: Session) -> Optional[AdminUser]:
    """Inspects the request cookies or Authorization header for a valid admin session."""
    token = request.cookies.get(SESSION_COOKIE_NAME)
    if not token:
        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            token = auth_header[7:].strip()
    if not token:
        return None
    return get_session_admin(db, token)


def require_admin_api(request: Request, db: Session) -> AdminUser:
    """FastAPI dependency: raises 401 Unauthorized if not authenticated."""
    admin = get_current_admin(request, db)
    if not admin:
        raise HTTPException(status_code=401, detail="Unauthorized")
    return admin



SETUP_TOKEN_FILE = ENV_DIR / ".setup_token"

def get_or_create_setup_token(force_new: bool = False) -> str:
    """Gets or generates a secure 32-character random slug token for initial admin setup."""
    if not force_new and SETUP_TOKEN_FILE.is_file():
        try:
            tok = SETUP_TOKEN_FILE.read_text(encoding="utf-8").strip()
            if tok and len(tok) >= 16:
                return tok
        except Exception:
            pass
    tok = secrets.token_urlsafe(24)  # 32 url-safe base64 characters
    ENV_DIR.mkdir(parents=True, exist_ok=True)
    SETUP_TOKEN_FILE.write_text(tok, encoding="utf-8")
    try:
        SETUP_TOKEN_FILE.chmod(0o600)
    except Exception:
        pass
    return tok

def burn_setup_token() -> None:
    """Permanently removes the one-time setup token after admin creation."""
    try:
        if SETUP_TOKEN_FILE.is_file():
            SETUP_TOKEN_FILE.unlink()
    except Exception:
        pass

def print_setup_qr_cli(base_url: str = ""):
    """Displays terminal setup link and scannable QR code if no admin user exists."""
    db = SessionLocal()
    try:
        admin_count = db.query(AdminUser).count()
        if admin_count > 0:
            return
        token = get_or_create_setup_token()
        if not base_url:
            domain = ""
            caddy_file = Path("/etc/caddy/Caddyfile")
            if caddy_file.is_file():
                try:
                    for line in caddy_file.read_text().splitlines():
                        line = line.strip()
                        if line and not line.startswith("#") and "{" in line:
                            domain = line.split("{")[0].strip()
                            break
                except Exception:
                    pass
            if domain:
                base_url = f"https://{domain}"
            else:
                base_url = "http://localhost:8000"

        setup_url = f"{base_url}/login?setup_token={token}"
        print("\n" + "=" * 65)
        print("  [!] INITIAL ADMIN ONBOARDING SETUP REQUIRED")
        print("=" * 65)
        print("  Please open this one-time admin setup URL or scan the QR code")
        print("  on mobile to configure your admin account:\n")
        print(f"  URL: {setup_url}\n")
        try:
            import qrcode
            qr = qrcode.QRCode(border=1)
            qr.add_data(setup_url)
            qr.print_ascii(invert=True)
        except Exception as e:
            print(f"  (QR code display error: {e})")
        print("=" * 65 + "\n")
    finally:
        db.close()

# CLI Helper functions for manual terminal management
def cli_main():
    args = sys.argv[1:]
    if not args:
        print("Usage: python -m backend.auth [create-admin | reset-password | list-admins | setup-token | print-setup-qr]")
        sys.exit(1)

    cmd = args[0].lower()
    db = SessionLocal()
    try:
        if cmd == "create-admin":
            if len(args) < 3:
                print("Usage: python -m backend.auth create-admin <username> <password>")
                sys.exit(1)
            username, password = args[1].strip(), args[2]
            existing = db.query(AdminUser).filter(AdminUser.username == username).first()
            if existing:
                print(f"Error: Admin user '{username}' already exists.")
                sys.exit(1)
            new_admin = AdminUser(
                username=username,
                password_hash=hash_password(password),
                created_at=datetime.utcnow(),
            )
            db.add(new_admin)
            db.commit()
            print(f"[✓] Successfully created admin user: {username}")

        elif cmd == "reset-password":
            if len(args) < 3:
                print("Usage: python -m backend.auth reset-password <username> <new_password>")
                sys.exit(1)
            username, new_password = args[1].strip(), args[2]
            user = db.query(AdminUser).filter(AdminUser.username == username).first()
            if not user:
                print(f"Error: Admin user '{username}' not found.")
                sys.exit(1)
            user.password_hash = hash_password(new_password)
            revoke_all_user_sessions(db, user.id)
            db.commit()
            print(f"[✓] Password reset successfully for admin: {username} (All sessions revoked)")

        elif cmd == "list-admins":
            users = db.query(AdminUser).all()
            if not users:
                print("No admin users found in database.")
            else:
                print(f"Found {len(users)} admin user(s):")
                for u in users:
                    print(f" - ID: {u.id} | Username: {u.username} | Created: {u.created_at}")

        elif cmd == "setup-token":
            tok = get_or_create_setup_token()
            print(f"One-Time Setup Token: {tok}")

        elif cmd == "print-setup-qr":
            url_arg = args[1] if len(args) > 1 else ""
            print_setup_qr_cli(url_arg)

        else:
            print(f"Unknown command: {cmd}")
            sys.exit(1)
    finally:
        db.close()


if __name__ == "__main__":
    cli_main()
