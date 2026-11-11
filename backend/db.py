from sqlalchemy import create_engine, Column, Integer, String, Float, Boolean, DateTime, Text
from sqlalchemy.orm import declarative_base, sessionmaker
from datetime import datetime
import os
from dotenv import load_dotenv
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENV_DIR = ROOT / "env"
load_dotenv(ENV_DIR / "rate-limit.env")
load_dotenv(ROOT / "rate-limit.env")
load_dotenv(ENV_DIR / ".env")
load_dotenv(ENV_DIR / "curlec.env")
load_dotenv(ROOT / ".env")
load_dotenv(ROOT / "curlec.env")
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./afrc.db")

engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

class Dish(Base):
    __tablename__ = "dishes"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, unique=True, index=True)
    price = Column(Float)
    available = Column(Boolean, default=True)
    unit = Column(String, default="portion")
    description = Column(Text, default="")
    pieces_per_portion = Column(Integer, default=1)

class Cashier(Base):
    __tablename__ = "cashiers"
    id = Column(Integer, primary_key=True)
    name = Column(String)
    pin = Column(String)  # 4 digit, store hashed in prod
    active = Column(Boolean, default=True)

class AuditLog(Base):
    __tablename__ = "audit_logs"
    id = Column(Integer, primary_key=True)
    cashier_id = Column(Integer)
    action = Column(String)
    details = Column(Text)
    timestamp = Column(DateTime, default=datetime.utcnow)

class SessionBill(Base):
    __tablename__ = "sessions"
    id = Column(String, primary_key=True)
    bill_number = Column(String, default="", index=True)
    raw_ai = Column(Text)
    final_bill = Column(Text)
    status = Column(String, default="open")
    void_reason = Column(String, default="")
    receipt_email = Column(String, default="")
    curlec_order_id = Column(String, default="")
    curlec_payment_id = Column(String, default="")
    image_key = Column(String, default="")
    retake_count = Column(Integer, default=0)
    client_token = Column(String, default="", index=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow)
    paid_at = Column(DateTime, nullable=True)

class ClientCooldown(Base):
    __tablename__ = "client_cooldowns"
    client_key = Column(String, primary_key=True)
    cooldown_until = Column(DateTime)
    reason = Column(String, default="")

class ScanAttempt(Base):
    __tablename__ = "scan_attempts"
    id = Column(Integer, primary_key=True, index=True)
    ip = Column(String, default="", index=True)
    device_id = Column(String, default="", index=True)
    session_id = Column(String, default="", index=True)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)

def format_bill_number(seq: int) -> str:
    seq = max(1, int(seq))
    prefix_idx, num = divmod(seq - 1, 9999)
    num += 1
    c1 = chr(ord("A") + ((prefix_idx // (26 * 26)) % 26))
    c2 = chr(ord("A") + ((prefix_idx // 26) % 26))
    c3 = chr(ord("A") + (prefix_idx % 26))
    return f"{c1}{c2}{c3}-{num:04d}"

def parse_bill_seq(bill_number: str) -> int:
    if not bill_number or "-" not in bill_number:
        return 0
    prefix, digits = bill_number.strip().upper().split("-", 1)
    if len(prefix) != 3 or not prefix.isalpha() or not digits.isdigit():
        return 0
    p_idx = (
        (ord(prefix[0]) - ord("A")) * 26 * 26
        + (ord(prefix[1]) - ord("A")) * 26
        + (ord(prefix[2]) - ord("A"))
    )
    return p_idx * 9999 + int(digits)

def next_bill_number(db) -> str:
    rows = db.query(SessionBill.bill_number).all()
    max_seq = max((parse_bill_seq(r[0]) for r in rows if r and r[0]), default=0)
    return format_bill_number(max_seq + 1)

def init_db():
    Base.metadata.create_all(bind=engine)
    with engine.begin() as conn:
        columns = [row[1] for row in conn.exec_driver_sql("PRAGMA table_info(dishes)")]
        if "description" not in columns:
            conn.exec_driver_sql("ALTER TABLE dishes ADD COLUMN description TEXT DEFAULT ''")
        if "pieces_per_portion" not in columns:
            conn.exec_driver_sql("ALTER TABLE dishes ADD COLUMN pieces_per_portion INTEGER DEFAULT 1")
        conn.exec_driver_sql("UPDATE dishes SET pieces_per_portion = 1 WHERE pieces_per_portion IS NULL")
        session_columns = [row[1] for row in conn.exec_driver_sql("PRAGMA table_info(sessions)")]
        if session_columns and "bill_number" not in session_columns:
            conn.exec_driver_sql("ALTER TABLE sessions ADD COLUMN bill_number TEXT DEFAULT ''")
        if session_columns and "status" not in session_columns:
            conn.exec_driver_sql("ALTER TABLE sessions ADD COLUMN status TEXT DEFAULT 'open'")
        if session_columns and "void_reason" not in session_columns:
            conn.exec_driver_sql("ALTER TABLE sessions ADD COLUMN void_reason TEXT DEFAULT ''")
        if session_columns and "receipt_email" not in session_columns:
            conn.exec_driver_sql("ALTER TABLE sessions ADD COLUMN receipt_email TEXT DEFAULT ''")
        if session_columns and "curlec_order_id" not in session_columns:
            conn.exec_driver_sql("ALTER TABLE sessions ADD COLUMN curlec_order_id TEXT DEFAULT ''")
        if session_columns and "curlec_payment_id" not in session_columns:
            conn.exec_driver_sql("ALTER TABLE sessions ADD COLUMN curlec_payment_id TEXT DEFAULT ''")
        if session_columns and "image_key" not in session_columns:
            conn.exec_driver_sql("ALTER TABLE sessions ADD COLUMN image_key TEXT DEFAULT ''")
        if session_columns and "retake_count" not in session_columns:
            conn.exec_driver_sql("ALTER TABLE sessions ADD COLUMN retake_count INTEGER DEFAULT 0")
        if session_columns and "client_token" not in session_columns:
            conn.exec_driver_sql("ALTER TABLE sessions ADD COLUMN client_token TEXT DEFAULT ''")
        if session_columns and "updated_at" not in session_columns:
            conn.exec_driver_sql("ALTER TABLE sessions ADD COLUMN updated_at DATETIME")
        if session_columns and "paid_at" not in session_columns:
            conn.exec_driver_sql("ALTER TABLE sessions ADD COLUMN paid_at DATETIME")
        cooldown_columns = [row[1] for row in conn.exec_driver_sql("PRAGMA table_info(client_cooldowns)")]
        if cooldown_columns and "reason" not in cooldown_columns:
            conn.exec_driver_sql("ALTER TABLE client_cooldowns ADD COLUMN reason TEXT DEFAULT ''")
        conn.exec_driver_sql(
            "UPDATE sessions SET status = 'void', void_reason = 'Needed Cashier Assistance' "
            "WHERE status = 'needs_cashier'"
        )
        conn.exec_driver_sql(
            "UPDATE sessions SET status = 'void', void_reason = 'Customer Cancelled' "
            "WHERE status = 'cancelled' AND (void_reason IS NULL OR void_reason = '')"
        )
        conn.exec_driver_sql(
            "UPDATE sessions SET void_reason = 'Exceeded 4 Retries Limit' "
            "WHERE status = 'void' AND (void_reason IS NULL OR void_reason = '') AND retake_count >= 4"
        )
        conn.exec_driver_sql(
            "UPDATE sessions SET void_reason = 'Bill timed out' "
            "WHERE status = 'void' AND (void_reason IS NULL OR void_reason = '' OR void_reason LIKE 'Ghosted / Stale Bill%')"
        )
    db = SessionLocal()
    # Backfill bill_number for any existing sessions missing one
    missing_bills = (
        db.query(SessionBill)
        .filter((SessionBill.bill_number == None) | (SessionBill.bill_number == ""))
        .order_by(SessionBill.created_at.asc())
        .all()
    )
    if missing_bills:
        existing_rows = db.query(SessionBill.bill_number).all()
        seq = max((parse_bill_seq(r[0]) for r in existing_rows if r and r[0]), default=0)
        for s in missing_bills:
            seq += 1
            s.bill_number = format_bill_number(seq)
        db.commit()
    # Seed default dishes if empty
    if db.query(Dish).count() == 0:
        seeds = [
            Dish(name="Chicken", price=5.0, available=True),
            Dish(name="Rice", price=1.0, available=True),
            Dish(name="Vegetables", price=2.0, available=True),
            Dish(name="Fish (per block)", price=5.0, available=True),
            Dish(name="Catfish", price=4.0, available=True),
            Dish(name="Ikan bawal (per piece)", price=6.0, available=True),
            Dish(name="Beef", price=8.0, available=False),
            Dish(name="Lamb", price=9.0, available=False),
        ]
        db.add_all(seeds)
        db.add(Cashier(name="Admin", pin="1234", active=True))
        db.commit()
    db.close()
