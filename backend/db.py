from sqlalchemy import create_engine, event, Column, Integer, String, Float, Boolean, DateTime, Text
from sqlalchemy.orm import declarative_base, sessionmaker
from sqlalchemy.pool import NullPool
from datetime import datetime
import os
from dotenv import load_dotenv
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENV_DIR = ROOT / "env"
load_dotenv(ENV_DIR / ".env")
load_dotenv(ROOT / ".env")
load_dotenv(ENV_DIR / "AI_model.env")
load_dotenv(ROOT / "AI_model.env")
load_dotenv(ENV_DIR / "cloudflare.env")
load_dotenv(ROOT / "cloudflare.env")
load_dotenv(ENV_DIR / "restaurant.env")
load_dotenv(ROOT / "restaurant.env")
load_dotenv(ENV_DIR / "rate-limit.env")
load_dotenv(ROOT / "rate-limit.env")
load_dotenv(ENV_DIR / "curlec.env")
load_dotenv(ROOT / "curlec.env")
load_dotenv(ENV_DIR / "loyalty.env")
load_dotenv(ROOT / "loyalty.env")
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./afrc.db")

_is_sqlite = DATABASE_URL.startswith("sqlite")
if _is_sqlite:
    engine = create_engine(
        DATABASE_URL,
        connect_args={"check_same_thread": False, "timeout": 30},
        poolclass=NullPool,
    )
else:
    engine = create_engine(DATABASE_URL)

if _is_sqlite:
    @event.listens_for(engine, "connect")
    def _configure_sqlite_connection(dbapi_connection, _connection_record):
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.execute("PRAGMA busy_timeout=15000")
        finally:
            cursor.close()

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

class Category(Base):
    __tablename__ = "categories"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, unique=True, index=True, nullable=False)
    icon = Column(String, default="restaurant")
    sort_order = Column(Integer, default=0, index=True)
    created_at = Column(DateTime, default=datetime.utcnow)

class Dish(Base):
    __tablename__ = "dishes"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, unique=True, index=True)
    alias = Column(String, default="")
    price = Column(Float)
    available = Column(Boolean, default=True)
    unit = Column(String, default="portion")
    description = Column(Text, default="")
    pieces_per_portion = Column(Integer, default=1)
    categories = Column(Text, default="[]")  # JSON array of up to 3 category names

class Cashier(Base):
    __tablename__ = "cashiers"
    id = Column(Integer, primary_key=True)
    name = Column(String)
    pin = Column(String)  # 4 digit, store hashed in prod
    active = Column(Boolean, default=True)

class AdminUser(Base):
    __tablename__ = "admin_users"
    id = Column(Integer, primary_key=True, index=True)
    username = Column(String, unique=True, index=True, nullable=False)
    password_hash = Column(String, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    last_login_at = Column(DateTime, nullable=True)

class AdminSession(Base):
    __tablename__ = "admin_sessions"
    token = Column(String, primary_key=True, index=True)
    user_id = Column(Integer, nullable=False, index=True)
    ip = Column(String, default="")
    user_agent = Column(String, default="")
    created_at = Column(DateTime, default=datetime.utcnow)
    expires_at = Column(DateTime, nullable=False, index=True)

class AuditLog(Base):
    __tablename__ = "audit_logs"
    id = Column(Integer, primary_key=True)
    cashier_id = Column(Integer)
    admin_username = Column(String(100), nullable=True)
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
    receipt_phone = Column(String, default="")
    curlec_order_id = Column(String, default="")
    curlec_payment_id = Column(String, default="")
    image_key = Column(String, default="")
    retake_count = Column(Integer, default=0)
    client_token = Column(String, default="", index=True)
    customer_user_id = Column(Integer, nullable=True, index=True)
    guest_session_id = Column(String, default="", index=True)
    points_earned = Column(Integer, default=0)
    points_status = Column(String, default="none")
    points_claimed_by = Column(Integer, nullable=True)
    points_claimed_at = Column(DateTime, nullable=True)
    points_clawed_back = Column(Integer, default=0)
    points_clawed_back_at = Column(DateTime, nullable=True)
    voucher_id = Column(Integer, nullable=True)
    voucher_code = Column(String, default="")
    discount_amount = Column(Float, default=0.0)
    voucher_restored_at = Column(DateTime, nullable=True)
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

class FailedScan(Base):
    __tablename__ = "failed_scans"
    id = Column(Integer, primary_key=True, index=True)
    session_id = Column(String, default="", index=True)
    bill_number = Column(String, default="", index=True)
    is_retry = Column(Boolean, default=False, index=True)
    ip = Column(String, default="", index=True)
    device_id = Column(String, default="", index=True)
    rejection_code = Column(String, default="UNKNOWN", index=True)
    rejection_message = Column(String, default="")
    image_key = Column(String, default="")
    raw_ai_response = Column(Text, default="")
    created_at = Column(DateTime, default=datetime.utcnow, index=True)

class BillRefund(Base):
    __tablename__ = "bill_refunds"
    id = Column(Integer, primary_key=True, index=True)
    session_id = Column(String, index=True)
    bill_number = Column(String, default="")
    curlec_payment_id = Column(String, default="")
    curlec_refund_id = Column(String, default="")
    refund_amount = Column(Float, default=0.0)
    refunded_items = Column(Text, default="[]")
    reason = Column(String, default="")
    status = Column(String, default="processed")
    created_at = Column(DateTime, default=datetime.utcnow)

class Feedback(Base):
    __tablename__ = "feedbacks"
    id = Column(Integer, primary_key=True, index=True)
    session_id = Column(String, index=True)
    bill_number = Column(String, default="", index=True)
    phone = Column(String, default="")
    rating = Column(Integer, default=5)
    prefix_comment = Column(String, default="")
    comment = Column(String(80), default="")
    created_at = Column(DateTime, default=datetime.utcnow, index=True)


class CustomerUser(Base):
    __tablename__ = "customer_users"
    id = Column(Integer, primary_key=True, index=True)
    google_id = Column(String(128), unique=True, index=True, nullable=False)
    email = Column(String(255), unique=True, index=True, nullable=False)
    name = Column(String(255), default="Customer", nullable=False)
    avatar_url = Column(String(512), default="")
    phone_number = Column(String(32), default="")
    points_balance = Column(Integer, default=0, nullable=False)
    is_suspended = Column(Boolean, default=False, nullable=False)
    suspended_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)
    updated_at = Column(DateTime, default=datetime.utcnow)
    last_login_at = Column(DateTime, nullable=True)


class CustomerSession(Base):
    __tablename__ = "customer_sessions"
    token = Column(String(128), primary_key=True, index=True)
    user_id = Column(Integer, nullable=False, index=True)
    ip = Column(String(100), default="")
    user_agent = Column(String(255), default="")
    created_at = Column(DateTime, default=datetime.utcnow)
    expires_at = Column(DateTime, nullable=False, index=True)


class PointsTransaction(Base):
    __tablename__ = "points_transactions"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, nullable=False, index=True)
    points = Column(Integer, nullable=False)
    transaction_type = Column(String(50), nullable=False, index=True)  # earned, redeemed, admin_add, admin_deduct, cancelled_order
    reference_id = Column(String(128), nullable=True, index=True)
    bill_number = Column(String(32), default="")
    source = Column(String(32), default="direct")  # direct, guest_claim, redemption, refund_clawback, admin
    description = Column(String(255), nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)


class PointRedemptionTier(Base):
    __tablename__ = "point_redemption_tiers"
    id = Column(Integer, primary_key=True, index=True)
    points_required = Column(Integer, nullable=False)
    discount_amount = Column(Float, nullable=False)
    is_active = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)


class Voucher(Base):
    __tablename__ = "vouchers"
    id = Column(Integer, primary_key=True, index=True)
    code = Column(String(50), unique=True, index=True, nullable=False)
    type = Column(String(20), nullable=False)  # fixed, percentage
    discount_value = Column(Float, nullable=False)
    min_spend = Column(Float, default=0.0, nullable=False)
    max_discount = Column(Float, nullable=True)
    expires_at = Column(DateTime, nullable=True)
    is_active = Column(Boolean, default=True, nullable=False)
    user_id = Column(Integer, nullable=True, index=True)
    is_redeemed = Column(Boolean, default=False, nullable=False)
    max_uses = Column(Integer, default=1, nullable=False)
    times_used = Column(Integer, default=0, nullable=False)
    source = Column(String(32), default="manual", nullable=False)  # points_redemption, manual
    is_archived = Column(Boolean, default=False, nullable=False)
    archived_at = Column(DateTime, nullable=True)
    archived_by = Column(String(100), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)


class GuestUnclaimedSession(Base):
    __tablename__ = "guest_unclaimed_sessions"
    guest_session_id = Column(String(128), primary_key=True, index=True)
    active_bill_id = Column(String(64), nullable=False, index=True)
    unclaimed_points = Column(Integer, default=0, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow)


class LoyaltySettings(Base):
    __tablename__ = "loyalty_settings"
    id = Column(Integer, primary_key=True)
    voucher_cooldown_hours = Column(Integer, default=12, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow)

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
        if "alias" not in columns:
            conn.exec_driver_sql("ALTER TABLE dishes ADD COLUMN alias TEXT DEFAULT ''")
        if "description" not in columns:
            conn.exec_driver_sql("ALTER TABLE dishes ADD COLUMN description TEXT DEFAULT ''")
        if "pieces_per_portion" not in columns:
            conn.exec_driver_sql("ALTER TABLE dishes ADD COLUMN pieces_per_portion INTEGER DEFAULT 1")
        if "categories" not in columns:
            conn.exec_driver_sql("ALTER TABLE dishes ADD COLUMN categories TEXT DEFAULT '[]'")
        conn.exec_driver_sql("UPDATE dishes SET pieces_per_portion = 1 WHERE pieces_per_portion IS NULL")
        conn.exec_driver_sql("UPDATE dishes SET categories = '[]' WHERE categories IS NULL OR categories = ''")

        cat_columns = [row[1] for row in conn.exec_driver_sql("PRAGMA table_info(categories)")]
        if cat_columns and "icon" not in cat_columns:
            conn.exec_driver_sql("ALTER TABLE categories ADD COLUMN icon TEXT DEFAULT 'restaurant'")
        if cat_columns and "sort_order" not in cat_columns:
            conn.exec_driver_sql("ALTER TABLE categories ADD COLUMN sort_order INTEGER DEFAULT 0")
        session_columns = [row[1] for row in conn.exec_driver_sql("PRAGMA table_info(sessions)")]
        if session_columns and "bill_number" not in session_columns:
            conn.exec_driver_sql("ALTER TABLE sessions ADD COLUMN bill_number TEXT DEFAULT ''")
        if session_columns and "status" not in session_columns:
            conn.exec_driver_sql("ALTER TABLE sessions ADD COLUMN status TEXT DEFAULT 'open'")
        if session_columns and "void_reason" not in session_columns:
            conn.exec_driver_sql("ALTER TABLE sessions ADD COLUMN void_reason TEXT DEFAULT ''")
        if session_columns and "receipt_email" not in session_columns:
            conn.exec_driver_sql("ALTER TABLE sessions ADD COLUMN receipt_email TEXT DEFAULT ''")
        if session_columns and "receipt_phone" not in session_columns:
            conn.exec_driver_sql("ALTER TABLE sessions ADD COLUMN receipt_phone TEXT DEFAULT ''")
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
        if session_columns and "customer_user_id" not in session_columns:
            conn.exec_driver_sql("ALTER TABLE sessions ADD COLUMN customer_user_id INTEGER")
        if session_columns and "guest_session_id" not in session_columns:
            conn.exec_driver_sql("ALTER TABLE sessions ADD COLUMN guest_session_id TEXT DEFAULT ''")
        if session_columns and "points_earned" not in session_columns:
            conn.exec_driver_sql("ALTER TABLE sessions ADD COLUMN points_earned INTEGER DEFAULT 0")
        if session_columns and "points_status" not in session_columns:
            conn.exec_driver_sql("ALTER TABLE sessions ADD COLUMN points_status TEXT DEFAULT 'none'")
        if session_columns and "points_claimed_by" not in session_columns:
            conn.exec_driver_sql("ALTER TABLE sessions ADD COLUMN points_claimed_by INTEGER")
        if session_columns and "points_claimed_at" not in session_columns:
            conn.exec_driver_sql("ALTER TABLE sessions ADD COLUMN points_claimed_at DATETIME")
        if session_columns and "points_clawed_back" not in session_columns:
            conn.exec_driver_sql("ALTER TABLE sessions ADD COLUMN points_clawed_back INTEGER DEFAULT 0")
        if session_columns and "points_clawed_back_at" not in session_columns:
            conn.exec_driver_sql("ALTER TABLE sessions ADD COLUMN points_clawed_back_at DATETIME")
        if session_columns and "voucher_id" not in session_columns:
            conn.exec_driver_sql("ALTER TABLE sessions ADD COLUMN voucher_id INTEGER")
        if session_columns and "voucher_code" not in session_columns:
            conn.exec_driver_sql("ALTER TABLE sessions ADD COLUMN voucher_code TEXT DEFAULT ''")
        if session_columns and "discount_amount" not in session_columns:
            conn.exec_driver_sql("ALTER TABLE sessions ADD COLUMN discount_amount REAL DEFAULT 0.0")
        if session_columns and "voucher_restored_at" not in session_columns:
            conn.exec_driver_sql("ALTER TABLE sessions ADD COLUMN voucher_restored_at DATETIME")

        conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS idx_sessions_customer_user_id ON sessions (customer_user_id)"
        )
        conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS idx_sessions_guest_session_id ON sessions (guest_session_id)"
        )
        conn.exec_driver_sql(
            "CREATE UNIQUE INDEX IF NOT EXISTS uniq_points_txn_type_ref "
            "ON points_transactions (transaction_type, reference_id)"
        )
        conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS idx_points_txn_user_created "
            "ON points_transactions (user_id, created_at)"
        )
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
        voucher_columns = [row[1] for row in conn.exec_driver_sql("PRAGMA table_info(vouchers)")]
        if voucher_columns and "archived_at" not in voucher_columns:
            conn.exec_driver_sql("ALTER TABLE vouchers ADD COLUMN archived_at DATETIME")
        if voucher_columns and "archived_by" not in voucher_columns:
            conn.exec_driver_sql("ALTER TABLE vouchers ADD COLUMN archived_by TEXT")
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
    # Seed default categories if empty
    if db.query(Category).count() == 0:
        default_cats = [
            Category(name="Ala carte", icon="lunch_dining", sort_order=0),
            Category(name="Lauk Pauk", icon="restaurant", sort_order=1),
            Category(name="Sarapan", icon="bakery_dining", sort_order=2),
            Category(name="Set", icon="set_meal", sort_order=3),
            Category(name="Minuman", icon="local_cafe", sort_order=4),
        ]
        db.add_all(default_cats)
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
        db.commit()
    # Seed default point redemption tiers (from takcakap.com-v2) if empty
    if db.query(PointRedemptionTier).count() == 0:
        db.add_all([
            PointRedemptionTier(points_required=200, discount_amount=1.00, is_active=True),
            PointRedemptionTier(points_required=800, discount_amount=5.00, is_active=True),
            PointRedemptionTier(points_required=1480, discount_amount=10.00, is_active=True),
        ])
        db.commit()
    # Seed default loyalty settings if empty
    if db.query(LoyaltySettings).count() == 0:
        db.add(LoyaltySettings(id=1, voucher_cooldown_hours=12))
        db.commit()
    missing_archive_stamp = (
        db.query(Voucher)
        .filter(Voucher.is_archived == True, Voucher.archived_at == None)
        .all()
    )
    if missing_archive_stamp:
        logs = (
            db.query(AuditLog)
            .filter(AuditLog.action == "VOUCHER_DELETE")
            .order_by(AuditLog.timestamp.asc())
            .all()
        )
        by_code = {}
        for log in logs:
            details = log.details or ""
            marker = "voucher '"
            if marker in details:
                code = details.split(marker, 1)[1].split("'", 1)[0]
                if code:
                    by_code[code] = log
        for voucher in missing_archive_stamp:
            log = by_code.get(voucher.code)
            if log:
                voucher.archived_at = log.timestamp
                voucher.archived_by = log.admin_username or ""
        db.commit()
    db.close()
