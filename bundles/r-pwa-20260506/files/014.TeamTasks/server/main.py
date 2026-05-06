"""チームタスク PWA サーバ (port 8086).

起動:
    cd server
    python -m uvicorn main:app --host 127.0.0.1 --port 8086
"""

from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from db import SessionLocal, init_db
from models import Settings as SettingsModel
from routers import categories, members, r as r_router, settings as settings_router, tasks, ws
from routers.tasks import UPLOAD_DIR, archive_old as _archive_old_endpoint
from seed import seed

logger = logging.getLogger("teamtasks.app")


def _read_pwa_build_time() -> str:
    """PWA の app.js から APP_BUILD_TIME を抽出（サーバ・クライアント整合性の単一ソース）."""
    try:
        js = (Path(__file__).parent.parent / "pwa" / "app.js").read_text(encoding="utf-8")
        m = re.search(r'APP_BUILD_TIME\s*=\s*"([^"]+)"', js)
        return m.group(1) if m else "unknown"
    except Exception:
        return "unknown"


APP_BUILD_TIME = _read_pwa_build_time()

app = FastAPI(title="TeamTasks-PWA", version="0.1.0")


@app.middleware("http")
async def add_version_header(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-App-Version"] = APP_BUILD_TIME
    return response

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://sfuji.f5.si",
        "https://192.168.1.175",
    ],
    allow_origin_regex=r"^https?://localhost(:\d+)?$",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


_archive_task: asyncio.Task | None = None


async def _archive_loop() -> None:
    """1時間ごとに古い完了タスクを archived=True にする（サーバ集約、client poll 廃止）."""
    while True:
        try:
            await asyncio.sleep(3600)
            with SessionLocal() as db:
                _archive_old_endpoint(days=None, db=db)
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.warning("archive loop error: %s", e)


@app.on_event("startup")
async def on_startup() -> None:
    global _archive_task
    init_db()
    seed()
    ws.set_main_loop(asyncio.get_running_loop())
    _archive_task = asyncio.create_task(_archive_loop())


@app.on_event("shutdown")
async def on_shutdown() -> None:
    if _archive_task:
        _archive_task.cancel()


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "app": "teamtasks-pwa"}


app.include_router(categories.router)
app.include_router(tasks.router)
app.include_router(members.router)
app.include_router(settings_router.router)
app.include_router(ws.router)
app.include_router(r_router.router)

# 添付画像の静的配信 (Caddy 経由: /tasksapi/uploads/<tid>/<file>)
app.mount("/uploads", StaticFiles(directory=str(UPLOAD_DIR)), name="uploads")
