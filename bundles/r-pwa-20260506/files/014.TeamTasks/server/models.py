"""データモデル定義."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Category(Base):
    __tablename__ = "categories"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    icon: Mapped[str] = mapped_column(String(20), default="")
    color: Mapped[str] = mapped_column(String(20), default="#1a73e8")
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    tasks: Mapped[list["Task"]] = relationship(back_populates="category")


class Member(Base):
    __tablename__ = "members"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(50), nullable=False)
    color: Mapped[str] = mapped_column(String(20), default="#666666")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Settings(Base):
    __tablename__ = "settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    archive_days: Mapped[int] = mapped_column(Integer, default=30)
    flame_cap: Mapped[int] = mapped_column(Integer, default=0)  # 0 = unlimited
    default_priority: Mapped[str] = mapped_column(String(10), default="low")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class Task(Base):
    __tablename__ = "tasks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    category_id: Mapped[int] = mapped_column(
        ForeignKey("categories.id", ondelete="CASCADE"),
        nullable=False,
    )
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    owner: Mapped[str | None] = mapped_column(String(50), nullable=True)      # 責任者
    assignee: Mapped[str | None] = mapped_column(String(50), nullable=True)   # 作業者
    due_date: Mapped[str | None] = mapped_column(String(10), nullable=True)  # YYYY-MM-DD
    priority: Mapped[str] = mapped_column(String(10), default="low")  # high/mid/low
    status: Mapped[str] = mapped_column(String(10), default="todo")  # todo/done
    product_code: Mapped[str] = mapped_column(String(100), default="")
    price: Mapped[int] = mapped_column(Integer, default=0)
    memo: Mapped[str] = mapped_column(String(2000), default="")
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    archived: Mapped[bool] = mapped_column(Boolean, default=False)
    images: Mapped[list] = mapped_column(JSON, default=list)
    extra: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    category: Mapped[Category] = relationship(back_populates="tasks")


class RInstruction(Base):
    """/r スキル遠隔指示の本体テーブル（旧 Google Tasks の置換）.

    code: 表示・指定用の番号（"001"〜"999" 通常 / 枯渇時 "1000"〜"9999"）。UNIQUE。
    body は最新版のミラー。版管理は r_instruction_versions に追記。
    consumed_at: NULL なら未取り込み、値があれば /r で取り込み済み。
    archived: 論理削除（PWA から trash トグル可、データは残る）。
    """

    __tablename__ = "r_instructions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(8), unique=True, nullable=False, index=True)
    body: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow, nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    archived: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    versions: Mapped[list["RInstructionVersion"]] = relationship(
        back_populates="instruction",
        cascade="all, delete-orphan",
        order_by="RInstructionVersion.id",
    )


class RInstructionVersion(Base):
    """/r 指示の編集履歴（append-only、削除不可）."""

    __tablename__ = "r_instruction_versions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    instruction_id: Mapped[int] = mapped_column(
        ForeignKey("r_instructions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    body: Mapped[str] = mapped_column(Text, nullable=False)
    reason: Mapped[str] = mapped_column(String(200), default="", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)

    instruction: Mapped[RInstruction] = relationship(back_populates="versions")
