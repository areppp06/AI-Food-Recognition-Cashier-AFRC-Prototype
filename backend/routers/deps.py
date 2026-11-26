"""Shared FastAPI route dependencies."""
from __future__ import annotations

from ..db import SessionLocal


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
