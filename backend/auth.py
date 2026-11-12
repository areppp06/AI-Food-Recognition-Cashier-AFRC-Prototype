import bcrypt
import secrets
import sys
from datetime import datetime, timedelta
from typing import Optional
from fastapi import Request, HTTPException, Depends
from sqlalchemy.orm import Session
from backend.db import AdminUser, AdminSession, SessionLocal

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


# CLI Helper functions for manual terminal management
def cli_main():
    args = sys.argv[1:]
    if not args:
        print("Usage: python -m backend.auth [create-admin | reset-password | list-admins]")
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

        else:
            print(f"Unknown command: {cmd}")
            sys.exit(1)
    finally:
        db.close()


if __name__ == "__main__":
    cli_main()
