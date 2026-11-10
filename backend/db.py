from sqlalchemy import create_engine, Column, Integer, String, Float, Boolean, DateTime, Text
from sqlalchemy.orm import declarative_base, sessionmaker
from datetime import datetime
import os
from dotenv import load_dotenv

load_dotenv()
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
    raw_ai = Column(Text)
    final_bill = Column(Text)
    status = Column(String, default="open")
    receipt_email = Column(String, default="")
    created_at = Column(DateTime, default=datetime.utcnow)

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
        if session_columns and "status" not in session_columns:
            conn.exec_driver_sql("ALTER TABLE sessions ADD COLUMN status TEXT DEFAULT 'open'")
        if session_columns and "receipt_email" not in session_columns:
            conn.exec_driver_sql("ALTER TABLE sessions ADD COLUMN receipt_email TEXT DEFAULT ''")
    db = SessionLocal()
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
