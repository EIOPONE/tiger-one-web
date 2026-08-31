"""Engine/session setup.

Set DATABASE_URL to a Postgres connection string in production, e.g.:
    postgresql+psycopg2://user:password@host:5432/tiger_one

If DATABASE_URL is not set, falls back to a local SQLite file so the app
runs with zero setup during development.
"""
from __future__ import annotations

import os
from contextlib import contextmanager

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from .models import Base

DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///./tiger_one.db")

# Render (and Heroku) hand out URLs starting "postgres://" or "postgresql://" —
# SQLAlchemy needs the psycopg2 driver named explicitly.
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql+psycopg2://", 1)
elif DATABASE_URL.startswith("postgresql://"):
    DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+psycopg2://", 1)

connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(DATABASE_URL, connect_args=connect_args, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

# Lightweight stopgap for adding columns to a database that already exists
# (e.g. the live Render Postgres instance) without a full migrations tool.
# Base.metadata.create_all only creates missing TABLES, not missing COLUMNS
# on tables that already exist — this covers that gap for now. Worth
# replacing with Alembic once real customer data is on there and schema
# changes need to be safer/reversible than "add a nullable column".
_LIGHT_MIGRATIONS = [
    "ALTER TABLE deliveries ADD COLUMN IF NOT EXISTS driver_user_id INTEGER REFERENCES app_users(user_id)",
    "ALTER TABLE deliveries ADD COLUMN IF NOT EXISTS scheduled_date DATE",
    "ALTER TABLE customers ADD COLUMN IF NOT EXISTS xero_contact_id VARCHAR",
    "ALTER TABLE customers ADD COLUMN IF NOT EXISTS xero_synced_at TIMESTAMP",
    "ALTER TABLE orders ADD COLUMN IF NOT EXISTS xero_invoice_id VARCHAR",
    "ALTER TABLE orders ADD COLUMN IF NOT EXISTS xero_invoice_number VARCHAR",
    "ALTER TABLE orders ADD COLUMN IF NOT EXISTS xero_synced_at TIMESTAMP",
]


def _run_light_migrations() -> None:
    if engine.dialect.name != "postgresql":
        return  # SQLite dev DBs are recreated from scratch each time — nothing to migrate.
    with engine.begin() as conn:
        for statement in _LIGHT_MIGRATIONS:
            try:
                conn.execute(text(statement))
            except Exception:
                pass  # column already present, or another instance just added it — safe to ignore


def init_db() -> None:
    Base.metadata.create_all(engine)
    _run_light_migrations()


@contextmanager
def get_session():
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
