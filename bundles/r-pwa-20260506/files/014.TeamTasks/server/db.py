"""SQLite データベース接続管理."""

from __future__ import annotations

from pathlib import Path
from typing import Generator

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

DB_PATH = Path(__file__).parent / "tasks.db"
DATABASE_URL = f"sqlite:///{DB_PATH}"

engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False},
    echo=False,
)


@event.listens_for(Engine, "connect")
def _enable_sqlite_fk(dbapi_connection, _connection_record) -> None:
    """SQLite 接続ごとに PRAGMA を設定（FK / WAL / busy_timeout）.

    /r 同居（r_instructions）と TeamTasks (tasks) で同 DB を共有するため、
    WAL モードで読み書きの並行性を高め、busy_timeout で一時的な競合を吸収する。
    """
    try:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.close()
    except Exception:
        # 他DBエンジンでは無視
        pass

SessionLocal = sessionmaker(
    bind=engine,
    autoflush=False,
    autocommit=False,
    expire_on_commit=False,
)


class Base(DeclarativeBase):
    pass


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db() -> None:
    """全テーブル作成 + user_version に基づく段階的マイグレーション."""
    from sqlalchemy import text

    from models import (  # noqa: F401
        Category,
        Member,
        RInstruction,
        RInstructionVersion,
        Settings,
        Task,
    )

    Base.metadata.create_all(bind=engine)
    _apply_migrations()


# --- Migrations (user_version ベース) ---
# 各関数は対応バージョンへの *上昇* 処理を実装（冪等に書くこと）
def _m1_add_task_product_and_price(conn) -> None:
    from sqlalchemy import text
    cols = {r[1] for r in conn.execute(text("PRAGMA table_info(tasks)"))}
    if "product_code" not in cols:
        conn.execute(text("ALTER TABLE tasks ADD COLUMN product_code TEXT DEFAULT ''"))
    if "price" not in cols:
        conn.execute(text("ALTER TABLE tasks ADD COLUMN price INTEGER DEFAULT 0"))


def _m2_add_task_owner(conn) -> None:
    from sqlalchemy import text
    cols = {r[1] for r in conn.execute(text("PRAGMA table_info(tasks)"))}
    if "owner" not in cols:
        conn.execute(text("ALTER TABLE tasks ADD COLUMN owner TEXT"))


def _m3_add_task_images(conn) -> None:
    from sqlalchemy import text
    cols = {r[1] for r in conn.execute(text("PRAGMA table_info(tasks)"))}
    if "images" not in cols:
        conn.execute(text("ALTER TABLE tasks ADD COLUMN images TEXT DEFAULT '[]'"))


# (バージョン番号, 上昇関数) の順序リスト
_MIGRATIONS: list[tuple[int, callable]] = [
    (1, _m1_add_task_product_and_price),
    (2, _m2_add_task_owner),
    (3, _m3_add_task_images),
]
TARGET_USER_VERSION = max(v for v, _ in _MIGRATIONS)


def _apply_migrations() -> None:
    """user_version を読み、未適用のマイグレーションを昇順に実行."""
    from sqlalchemy import text

    with engine.begin() as conn:
        current = conn.execute(text("PRAGMA user_version")).scalar() or 0
        for ver, fn in _MIGRATIONS:
            if current < ver:
                fn(conn)
                conn.execute(text(f"PRAGMA user_version={ver}"))
                current = ver
