"""應用程式進入點。啟動前檢查 ffmpeg，掛載前端靜態檔。"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.api.routes import create_app
from app.config import load_roots, require_ffmpeg
from app.jobs.store import JobStore
from app.pipeline import Pipeline

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR.parent / "jobs.db"
WORKDIR = BASE_DIR.parent / "downloads"


def build() -> FastAPI:
    require_ffmpeg()  # 缺 ffmpeg 就直接在啟動時炸，不要等下載到一半
    store = JobStore(DB_PATH)
    store.init_schema()
    pipeline = Pipeline(store, load_roots(), workdir=WORKDIR)
    application = create_app(pipeline)
    application.mount("/", StaticFiles(directory=BASE_DIR / "web", html=True), name="web")
    return application


app = build()
