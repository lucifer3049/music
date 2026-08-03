"""HTTP 端點。只做驗證與回應塑形，業務邏輯全在 Pipeline。"""

from __future__ import annotations

import asyncio
import json
import logging

from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.jobs.store import JobRow, TrackRow, TrackStatus
from app.models import TrackMeta
from app.pipeline import Pipeline

TERMINAL_STATUSES = {TrackStatus.DONE, TrackStatus.FAILED, TrackStatus.SKIPPED}
SSE_INTERVAL_SECONDS = 1.0

logger = logging.getLogger(__name__)


class SubmitRequest(BaseModel):
    urls: list[str] = Field(min_length=1)


class MetaPayload(BaseModel):
    title: str
    artists: list[str]
    album_artist: str
    album: str
    year: int | None = None
    track_no: int | None = None
    track_total: int | None = None
    genre: str | None = None
    cover_url: str | None = None
    duration: int | None = None
    source_url: str | None = None

    def to_meta(self) -> TrackMeta:
        return TrackMeta(
            title=self.title,
            artists=tuple(self.artists),
            album_artist=self.album_artist,
            album=self.album,
            year=self.year,
            track_no=self.track_no,
            track_total=self.track_total,
            genre=self.genre,
            cover_url=self.cover_url,
            duration=self.duration,
            source_url=self.source_url,
        )


class ConfirmRequest(BaseModel):
    meta: MetaPayload


def _meta_dict(meta: TrackMeta) -> dict:
    return {
        "title": meta.title,
        "artists": list(meta.artists),
        "album_artist": meta.album_artist,
        "album": meta.album,
        "year": meta.year,
        "track_no": meta.track_no,
        "track_total": meta.track_total,
        "genre": meta.genre,
        "cover_url": meta.cover_url,
        "duration": meta.duration,
        "source_url": meta.source_url,
    }


def _track_dict(track: TrackRow) -> dict:
    return {
        "id": track.id,
        "video_id": track.video_id,
        "raw_title": track.source.raw_title,
        "youtube_url": track.source.url,
        "duration": track.source.duration,
        "status": track.status.value,
        "error": track.error,
        "candidates": [
            {"score": round(c.score, 4), "meta": _meta_dict(c.meta)} for c in track.candidates
        ],
        "chosen": _meta_dict(track.chosen) if track.chosen else None,
    }


def _job_dict(job: JobRow) -> dict:
    return {
        "id": job.id,
        "url": job.url,
        "created_at": job.created_at,
        "error": job.error,
        "tracks": [_track_dict(t) for t in job.tracks],
    }


def create_app(pipeline: Pipeline) -> FastAPI:
    app = FastAPI(title="音樂下載工具")

    # finalize() 在背景執行緒跑；asyncio.create_task() 回傳的 Task 只被事件迴圈
    # 弱參照住，若不另外保留強參照，Task 有可能在下載跑到一半時被 GC 回收、
    # 下載無聲消失（見任務說明）。這裡用 app.state 上的集合強參照住所有進行中的
    # 背景工作，任務完成後才在 done_callback 裡移除。
    background_tasks: set[asyncio.Task] = set()
    app.state.background_tasks = background_tasks

    def _on_finalize_done(task: asyncio.Task) -> None:
        background_tasks.discard(task)
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            # Pipeline.finalize() 已經把預期內的失敗都轉成 track 的 FAILED 狀態，
            # 理論上不該再拋例外到這裡；一旦真的發生（例如 store 本身壞掉），
            # 絕不能無聲吞掉——至少要留下記錄，不然使用者只會看到曲目卡住不動、
            # 卻查無原因。
            logger.error("背景下載工作發生未預期例外", exc_info=exc)

    @app.post("/api/jobs")
    async def submit_jobs(request: SubmitRequest) -> dict:
        # submit 會打網路，丟到 thread 避免卡住事件迴圈
        job_ids = [
            await asyncio.to_thread(pipeline.submit, url) for url in request.urls
        ]
        return {"job_ids": job_ids}

    @app.get("/api/jobs")
    async def list_jobs() -> dict:
        return {"jobs": [_job_dict(j) for j in pipeline.store.list_jobs()]}

    @app.get("/api/jobs/{job_id}")
    async def get_job(job_id: int) -> dict:
        job = pipeline.store.get_job(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="找不到任務")
        return _job_dict(job)

    @app.post("/api/tracks/{track_id}/confirm")
    async def confirm_track(track_id: int, request: ConfirmRequest) -> dict:
        track = pipeline.store.get_track(track_id)
        if track is None:
            raise HTTPException(status_code=404, detail="找不到曲目")
        # 只有 AWAITING_CONFIRM 能被確認：已在 DOWNLOADING／TAGGING／DONE／
        # SKIPPED 的曲目若能再次 confirm，會排出第二個 finalize 背景工作，
        # 跟原本進行中（或已完成）的那個搶著寫同一組目的地路徑（Task 11
        # review Finding 2）。
        if track.status != TrackStatus.AWAITING_CONFIRM:
            raise HTTPException(
                status_code=409,
                detail=f"曲目目前狀態為 {track.status.value}，無法確認",
            )
        pipeline.store.confirm(track_id, request.meta.to_meta())
        # 下載在背景跑，立刻回應讓前端可以繼續確認下一首。
        task = asyncio.create_task(asyncio.to_thread(pipeline.finalize, track_id))
        background_tasks.add(task)
        task.add_done_callback(_on_finalize_done)
        return {"status": TrackStatus.DOWNLOADING.value}

    @app.post("/api/tracks/{track_id}/skip")
    async def skip_track(track_id: int) -> dict:
        track = pipeline.store.get_track(track_id)
        if track is None:
            raise HTTPException(status_code=404, detail="找不到曲目")
        if track.status != TrackStatus.AWAITING_CONFIRM:
            raise HTTPException(
                status_code=409,
                detail=f"曲目目前狀態為 {track.status.value}，無法跳過",
            )
        pipeline.store.set_status(track_id, TrackStatus.SKIPPED)
        return {"status": TrackStatus.SKIPPED.value}

    @app.get("/api/jobs/{job_id}/events")
    async def job_events(job_id: int) -> StreamingResponse:
        if pipeline.store.get_job(job_id) is None:
            raise HTTPException(status_code=404, detail="找不到任務")

        async def stream():
            while True:
                job = pipeline.store.get_job(job_id)
                if job is None:
                    break
                payload = json.dumps(_job_dict(job), ensure_ascii=False)
                yield f"data: {payload}\n\n"
                # job.tracks == [] 是兩種完全不同情況共同的表面現象，光看
                # tracks 分不出來：
                #   (a) job 層級探測整批失敗（見 app/pipeline.py 的
                #       Pipeline.submit）——不會再有新曲目加入，必須結束串流；
                #   (b) submit() 還在跑（JobStore 是 autocommit，create_job()
                #       一提交就對外可見，早於 probe_fn 與逐曲 add_track），
                #       tracks 之後就會補上，此時絕不能提早結束。
                # 用 job.error 區分兩者：只有 (a) 會把它設成非 None（見
                # JobStore.set_job_error）。若只看 `all(t.status in
                # TERMINAL for t in job.tracks)`，空陣列會讓 all() 對空陣列
                # 回傳 True，把 (b) 誤判成 (a) 而提早關閉串流（Task 11 review
                # Finding 1）。`job.tracks and ...` 這段則反過來確保 tracks
                # 非空時才用「全部曲目終結」判斷是否結束。
                if job.error is not None or (
                    job.tracks and all(t.status in TERMINAL_STATUSES for t in job.tracks)
                ):
                    break
                await asyncio.sleep(SSE_INTERVAL_SECONDS)

        return StreamingResponse(stream(), media_type="text/event-stream")

    return app
