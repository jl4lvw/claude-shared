"""Pydantic スキーマ（リクエスト/レスポンス）."""

from __future__ import annotations

from datetime import date, datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

Priority = Literal["high", "mid", "low"]
Status = Literal["todo", "doing", "done"]

# ===== 共通バリデーション型 =====
# 色: #RRGGBB / #RGB のみ許可
HexColor = Annotated[str, StringConstraints(pattern=r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")]
# アイコン: 絵文字やごく短い記号のみ想定（最大6文字）、制御文字不可
IconStr = Annotated[str, StringConstraints(max_length=6, pattern=r"^[^\x00-\x1f<>&\"']*$")]
# 名前・タイトル等: タグ混入防止
SafeName = Annotated[str, StringConstraints(min_length=1, max_length=50, pattern=r"^[^<>]+$")]
SafeTitle = Annotated[str, StringConstraints(min_length=1, max_length=500, pattern=r"^[^<>]+$")]
SafeMemo = Annotated[str, StringConstraints(max_length=2000)]
SafeProductCode = Annotated[str, StringConstraints(max_length=100, pattern=r"^[^<>]*$")]


class CategoryBase(BaseModel):
    name: SafeName
    icon: IconStr = ""
    color: HexColor = "#1a73e8"
    sort_order: int = Field(default=0, ge=0, le=9999)


class CategoryCreate(CategoryBase):
    pass


class CategoryUpdate(BaseModel):
    name: SafeName | None = None
    icon: IconStr | None = None
    color: HexColor | None = None
    sort_order: int | None = Field(default=None, ge=0, le=9999)


class CategoryOut(CategoryBase):
    id: int
    model_config = ConfigDict(from_attributes=True)


class MemberBase(BaseModel):
    name: SafeName
    color: HexColor = "#666666"


class MemberCreate(MemberBase):
    pass


class MemberOut(MemberBase):
    id: int
    model_config = ConfigDict(from_attributes=True)


def _validate_iso_date(v: str | None) -> str | None:
    """`YYYY-MM-DD` を実在日として検証（2026-99-99 等を弾く）."""
    if v is None or v == "":
        return None
    try:
        date.fromisoformat(v)
    except ValueError as exc:
        raise ValueError(f"due_date must be a valid YYYY-MM-DD date: {v!r}") from exc
    return v


class TaskBase(BaseModel):
    title: SafeTitle
    owner: SafeName | None = None      # 責任者
    assignee: SafeName | None = None   # 作業者
    due_date: Annotated[str, StringConstraints(pattern=r"^\d{4}-\d{2}-\d{2}$")] | None = None
    priority: Priority = "low"
    product_code: SafeProductCode = ""
    price: int = Field(default=0, ge=0, le=999_999_999)
    memo: SafeMemo = ""
    images: list[str] = Field(default_factory=list)
    extra: dict = Field(default_factory=dict)

    @field_validator("due_date", mode="after")
    @classmethod
    def _check_due_date(cls, v: str | None) -> str | None:
        return _validate_iso_date(v)


class TaskCreate(TaskBase):
    category_id: int
    client_request_id: str | None = None  # idempotency-key (任意)


class TaskUpdate(BaseModel):
    title: SafeTitle | None = None
    owner: SafeName | None = None
    assignee: SafeName | None = None
    due_date: Annotated[str, StringConstraints(pattern=r"^\d{4}-\d{2}-\d{2}$")] | None = None
    priority: Priority | None = None
    status: Status | None = None
    product_code: SafeProductCode | None = None
    price: int | None = Field(default=None, ge=0, le=999_999_999)
    memo: SafeMemo | None = None
    category_id: int | None = None
    archived: bool | None = None
    extra: dict | None = None
    # 楽観ロック用: モーダル開いた時の updated_at を持たせる
    expected_updated_at: datetime | None = None

    @field_validator("due_date", mode="after")
    @classmethod
    def _check_due_date(cls, v: str | None) -> str | None:
        return _validate_iso_date(v)


class SettingsOut(BaseModel):
    archive_days: int
    flame_cap: int
    default_priority: Priority
    model_config = ConfigDict(from_attributes=True)


class SettingsUpdate(BaseModel):
    archive_days: int | None = Field(default=None, ge=1, le=365)
    flame_cap: int | None = Field(default=None, ge=0, le=100)
    default_priority: Priority | None = None


class TaskOut(TaskBase):
    id: int
    category_id: int
    status: Status
    archived: bool
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None
    model_config = ConfigDict(from_attributes=True)


# ===== /r 遠隔指示スキーマ =====
# 指示本文: 制御文字以外なら自由（Markdown / 改行 OK）。長すぎは弾く。
RInstructionBody = Annotated[str, StringConstraints(min_length=1, max_length=10000)]
# code: "001"〜"999" (3桁), 枯渇時 "1000"〜"9999" (4桁) を許可。
RInstructionCode = Annotated[str, StringConstraints(pattern=r"^\d{3,4}$")]


class RInstructionCreate(BaseModel):
    body: RInstructionBody
    code: RInstructionCode | None = None  # None ならサーバ採番


class RInstructionUpdate(BaseModel):
    body: RInstructionBody
    reason: Annotated[str, StringConstraints(max_length=200)] = ""


class RInstructionVersionOut(BaseModel):
    id: int
    body: str
    reason: str
    created_at: datetime
    model_config = ConfigDict(from_attributes=True)


class RInstructionOut(BaseModel):
    id: int
    code: str
    body: str
    created_at: datetime
    updated_at: datetime
    consumed_at: datetime | None
    archived: bool
    model_config = ConfigDict(from_attributes=True)


class RInstructionDetailOut(RInstructionOut):
    versions: list[RInstructionVersionOut] = Field(default_factory=list)
