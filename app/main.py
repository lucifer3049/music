"""應用程式進入點。啟動前檢查 ffmpeg，掛載前端靜態檔。"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.api.routes import create_app
from app.config import keep_archive_copy, load_roots, require_ffmpeg
from app.jobs.store import JobStore
from app.pipeline import Pipeline

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR.parent / "jobs.db"
WORKDIR = BASE_DIR.parent / "downloads"


def build() -> FastAPI:
    require_ffmpeg()  # 缺 ffmpeg 就直接在啟動時炸，不要等下載到一半
    store = JobStore(DB_PATH)
    store.init_schema()
    # 上次行程可能在 MATCHING／DOWNLOADING／TAGGING 半途死掉：這些不是終態，
    # 不收斂會讓那些曲目永遠卡住（SSE 迴圈不結束、confirm/skip 永遠 409）。
    # 見 JobStore.recover_interrupted_tracks() 的說明。
    store.recover_interrupted_tracks("伺服器重啟時中斷，可重新確認")
    # 上次行程也可能死在「探測到一半、還沒建出任何曲目」的空窗期：那種 job
    # 一筆 track 都沒有，上面那行掃不到它。見
    # JobStore.recover_interrupted_jobs() 的說明。
    store.recover_interrupted_jobs("伺服器重啟時中斷，可重新送出這個網址")
    # 正式環境的預設值在這裡決定：冷存副本預設關閉（見 config.keep_archive_copy
    # 的說明）。Pipeline 建構子本身仍預設 True，維持既有測試的原本語意。
    pipeline = Pipeline(
        store,
        load_roots(),
        workdir=WORKDIR,
        keep_archive_copy=keep_archive_copy(),
    )
    application = create_app(pipeline)
    application.mount("/", StaticFiles(directory=BASE_DIR / "web", html=True), name="web")
    return application


app = build()
