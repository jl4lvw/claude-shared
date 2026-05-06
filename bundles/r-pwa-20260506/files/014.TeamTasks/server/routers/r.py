"""/r 遠隔指示 API（Google Tasks 認証切れ問題の置換）.

エンドポイント:
    GET    /r/                    一覧（フィルタ: status / archived / limit）
    POST   /r/                    新規作成（code 省略時はサーバ採番）
    GET    /r/{code}              単発閲覧（副作用なし、版履歴も返す）
    PATCH  /r/{code}              本文編集（新版を append、削除はしない）
    POST   /r/{code}/consume      取り込み確定（先着 200、後続 409 already_consumed）
    POST   /r/{code}/restore      consumed_at を NULL に戻す（誤取り込み救済）
    POST   /r/{code}/archive      論理削除トグル（archived フラグ反転）

設計メモ:
    - GET は副作用なし。/r スキル本体は GET → 確認 → POST /consume で確定する。
    - 採番は「直近 100 件の code を避けて 100-999 でランダム → DB INSERT 試行 →
      IntegrityError なら別候補で再試行」。空きが極端に減ったら 4 桁にエスカレーション。
    - consume は CAS 風 UPDATE: WHERE consumed_at IS NULL + rowcount==1 判定で
      複数 PC 同時取り込み時の取りこぼしを防ぐ。
"""

from __future__ import annotations

import random
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from db import get_db
from models import RInstruction, RInstructionVersion
from schemas import (
    RInstructionCreate,
    RInstructionDetailOut,
    RInstructionOut,
    RInstructionUpdate,
)

router = APIRouter(prefix="/r", tags=["r-instructions"])


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _generate_code(db: Session) -> str:
    """衝突回避ロジック付きの code 生成.

    Phase 1: 直近 100 件の code を避けて 100-999 のランダム値を最大 50 回試す。
    Phase 2: それでもダメなら 100-999 を全走査して未使用を探す。
    Phase 3: 3 桁が枯渇していたら 1000-9999 のランダムを最大 50 回試す。
    """
    recent = {
        row[0]
        for row in db.execute(
            text("SELECT code FROM r_instructions ORDER BY id DESC LIMIT 100")
        ).all()
    }
    for _ in range(50):
        cand = f"{random.randint(100, 999):03d}"
        if cand not in recent:
            return cand

    used3 = {
        row[0]
        for row in db.execute(
            text("SELECT code FROM r_instructions WHERE length(code)=3")
        ).all()
    }
    for n in range(100, 1000):
        cand = f"{n:03d}"
        if cand not in used3:
            return cand

    used4 = {
        row[0]
        for row in db.execute(
            text("SELECT code FROM r_instructions WHERE length(code)=4")
        ).all()
    }
    for _ in range(50):
        cand = f"{random.randint(1000, 9999)}"
        if cand not in used4:
            return cand

    raise HTTPException(503, "code space exhausted (3-digit + 4-digit both saturated)")


def _serialize_detail(ins: RInstruction) -> dict:
    """関連オブジェクトを含む詳細レスポンス（versions 込み）."""
    return {
        "id": ins.id,
        "code": ins.code,
        "body": ins.body,
        "created_at": ins.created_at,
        "updated_at": ins.updated_at,
        "consumed_at": ins.consumed_at,
        "archived": ins.archived,
        "versions": [
            {
                "id": v.id,
                "body": v.body,
                "reason": v.reason,
                "created_at": v.created_at,
            }
            for v in ins.versions
        ],
    }


@router.get("/", response_model=list[RInstructionOut])
def list_instructions(
    status: str = Query("all", pattern="^(all|unconsumed|consumed)$"),
    archived: int = Query(0, ge=0, le=1),
    limit: int = Query(200, ge=1, le=1000),
    db: Session = Depends(get_db),
) -> list[RInstruction]:
    q = db.query(RInstruction)
    if status == "unconsumed":
        q = q.filter(RInstruction.consumed_at.is_(None))
    elif status == "consumed":
        q = q.filter(RInstruction.consumed_at.is_not(None))
    if archived == 0:
        q = q.filter(RInstruction.archived.is_(False))
    return q.order_by(RInstruction.id.desc()).limit(limit).all()


@router.post("/", response_model=RInstructionOut, status_code=201)
def create_instruction(
    payload: RInstructionCreate,
    db: Session = Depends(get_db),
) -> RInstruction:
    body = payload.body
    requested_code = payload.code
    now = _utcnow()

    last_err: Exception | None = None
    for _attempt in range(10):
        code = requested_code or _generate_code(db)
        try:
            ins = RInstruction(code=code, body=body, created_at=now, updated_at=now)
            db.add(ins)
            db.flush()
            ver = RInstructionVersion(
                instruction_id=ins.id,
                body=body,
                reason="created",
                created_at=now,
            )
            db.add(ver)
            db.commit()
            db.refresh(ins)
            return ins
        except IntegrityError as e:
            db.rollback()
            last_err = e
            if requested_code:
                # ユーザ指定コードが既存 → 即 409
                raise HTTPException(409, f"code {requested_code!r} already exists") from e
            # ランダム採番なら別候補で再試行
            continue

    raise HTTPException(
        503,
        f"failed to generate unique code after 10 attempts: {last_err!r}",
    )


@router.get("/{code}", response_model=RInstructionDetailOut)
def get_instruction(code: str, db: Session = Depends(get_db)) -> dict:
    if not code.isdigit() or not (3 <= len(code) <= 4):
        raise HTTPException(400, f"invalid code format: {code!r}")
    ins = db.query(RInstruction).filter(RInstruction.code == code).first()
    if ins is None:
        raise HTTPException(404, f"code {code!r} not found")
    return _serialize_detail(ins)


@router.patch("/{code}", response_model=RInstructionDetailOut)
def update_instruction(
    code: str,
    payload: RInstructionUpdate,
    db: Session = Depends(get_db),
) -> dict:
    ins = db.query(RInstruction).filter(RInstruction.code == code).first()
    if ins is None:
        raise HTTPException(404, f"code {code!r} not found")
    now = _utcnow()
    ins.body = payload.body
    ins.updated_at = now
    ver = RInstructionVersion(
        instruction_id=ins.id,
        body=payload.body,
        reason=payload.reason or "edited",
        created_at=now,
    )
    db.add(ver)
    db.commit()
    db.refresh(ins)
    return _serialize_detail(ins)


@router.post("/{code}/consume", response_model=RInstructionOut)
def consume_instruction(code: str, db: Session = Depends(get_db)) -> RInstruction:
    """取り込み確定（先着 200 / 既消費 409）.

    CAS 風 UPDATE で「未消費の行のみ更新」、rowcount で勝者判定。
    """
    now = _utcnow()
    result = db.execute(
        text(
            "UPDATE r_instructions SET consumed_at = :now, updated_at = :now "
            "WHERE code = :code AND consumed_at IS NULL"
        ),
        {"now": now, "code": code},
    )
    db.commit()

    ins = db.query(RInstruction).filter(RInstruction.code == code).first()
    if ins is None:
        raise HTTPException(404, f"code {code!r} not found")

    if result.rowcount == 0:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "already_consumed",
                "code": ins.code,
                "consumed_at": ins.consumed_at.isoformat() if ins.consumed_at else None,
            },
        )
    return ins


@router.post("/{code}/restore", response_model=RInstructionOut)
def restore_instruction(code: str, db: Session = Depends(get_db)) -> RInstruction:
    """consumed_at を NULL に戻す（誤取り込み救済）."""
    ins = db.query(RInstruction).filter(RInstruction.code == code).first()
    if ins is None:
        raise HTTPException(404, f"code {code!r} not found")
    ins.consumed_at = None
    ins.updated_at = _utcnow()
    db.commit()
    db.refresh(ins)
    return ins


@router.post("/{code}/archive", response_model=RInstructionOut)
def archive_instruction(code: str, db: Session = Depends(get_db)) -> RInstruction:
    """archived フラグをトグル（論理削除）."""
    ins = db.query(RInstruction).filter(RInstruction.code == code).first()
    if ins is None:
        raise HTTPException(404, f"code {code!r} not found")
    ins.archived = not ins.archived
    ins.updated_at = _utcnow()
    db.commit()
    db.refresh(ins)
    return ins
