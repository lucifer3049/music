import asyncio
import gc
import json
import logging
import threading

import httpx
import pytest
from fastapi import FastAPI

from app.api.routes import SubmitRequest, create_app
from app.config import LibraryRoots
from app.jobs.store import JobStore, TrackStatus
from app.models import SourceTrack, TrackMeta
from app.pipeline import Pipeline
from app.sources import youtube
from app.sources.youtube import DownloadedStreams

META_PAYLOAD = {
    "title": "人間驚鴻宴",
    "artists": ["指尖笑"],
    "album_artist": "指尖笑",
    "album": "人間驚鴻宴",
    "year": 2026,
    "track_no": 1,
    "track_total": 1,
    "genre": None,
    "cover_url": None,
    "duration": 207,
    "source_url": None,
}


def _source(video_id: str = "v1") -> SourceTrack:
    return SourceTrack(
        video_id=video_id,
        url=f"https://music.youtube.com/watch?v={video_id}",
        raw_title="指尖笑 - 人間驚鴻宴",
        duration=207,
        artist="指尖笑",
        track="人間驚鴻宴",
        album=None,
        release_year=None,
    )


def _fake_download(video_id, workdir, **kwargs):
    workdir.mkdir(parents=True, exist_ok=True)
    m4a = workdir / f"{video_id}.m4a"
    opus = workdir / f"{video_id}.opus"
    m4a.write_bytes(b"m4a")
    opus.write_bytes(b"opus")
    return DownloadedStreams(m4a=m4a, opus=opus)


def _build_pipeline(tmp_path, **overrides):
    store = JobStore(tmp_path / "jobs.db")
    store.init_schema()
    roots = LibraryRoots(music=tmp_path / "Music", archive=tmp_path / "Archive")
    kwargs = dict(
        workdir=tmp_path / "work",
        client_factory=lambda: httpx.Client(
            transport=httpx.MockTransport(lambda r: httpx.Response(200, content=b""))
        ),
        probe_fn=lambda url: [_source()],
        search_fn=lambda q, client: [],
        download_fn=_fake_download,
        cover_fn=lambda url, client: b"",
        write_fn=lambda path, meta, cover=None: None,
    )
    kwargs.update(overrides)
    return Pipeline(store, roots, **kwargs)


@pytest.fixture
def app_and_pipeline(tmp_path):
    pipeline = _build_pipeline(tmp_path)
    app = create_app(pipeline)
    yield app, pipeline
    pipeline.store.close()


@pytest.fixture
async def client(app_and_pipeline):
    app, _ = app_and_pipeline
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def test_create_app_returns_fastapi(app_and_pipeline):
    assert isinstance(app_and_pipeline[0], FastAPI)


async def test_post_jobs_creates_job(client):
    response = await client.post(
        "/api/jobs", json={"urls": ["https://music.youtube.com/watch?v=v1"]}
    )
    assert response.status_code == 200
    assert len(response.json()["job_ids"]) == 1


async def test_post_jobs_accepts_multiple_urls(client):
    response = await client.post(
        "/api/jobs",
        json={"urls": [
            "https://music.youtube.com/watch?v=v1",
            "https://music.youtube.com/watch?v=v2",
        ]},
    )
    assert len(response.json()["job_ids"]) == 2


async def test_post_jobs_rejects_empty_list(client):
    assert (await client.post("/api/jobs", json={"urls": []})).status_code == 422


async def test_get_job_returns_tracks_with_candidates(client):
    job_id = (await client.post(
        "/api/jobs", json={"urls": ["https://music.youtube.com/watch?v=v1"]}
    )).json()["job_ids"][0]

    body = (await client.get(f"/api/jobs/{job_id}")).json()
    assert body["id"] == job_id
    assert len(body["tracks"]) == 1
    track = body["tracks"][0]
    assert track["status"] == TrackStatus.AWAITING_CONFIRM.value
    assert track["candidates"][0]["meta"]["title"] == "人間驚鴻宴"


async def test_get_job_404(client):
    assert (await client.get("/api/jobs/99999")).status_code == 404


async def test_list_jobs_returns_created_jobs(client):
    await client.post("/api/jobs", json={"urls": ["https://music.youtube.com/watch?v=v1"]})
    body = (await client.get("/api/jobs")).json()
    assert len(body["jobs"]) == 1


async def test_confirm_track_triggers_finalize(client, app_and_pipeline):
    _, pipeline = app_and_pipeline
    job_id = (await client.post(
        "/api/jobs", json={"urls": ["https://music.youtube.com/watch?v=v1"]}
    )).json()["job_ids"][0]
    track_id = pipeline.store.get_job(job_id).tracks[0].id

    response = await client.post(f"/api/tracks/{track_id}/confirm", json={"meta": META_PAYLOAD})
    assert response.status_code == 200

    # 背景工作完成後狀態應為 done
    await _wait_for(pipeline, track_id, TrackStatus.DONE)


async def test_confirm_rejects_bad_meta(client, app_and_pipeline):
    _, pipeline = app_and_pipeline
    job_id = (await client.post(
        "/api/jobs", json={"urls": ["https://music.youtube.com/watch?v=v1"]}
    )).json()["job_ids"][0]
    track_id = pipeline.store.get_job(job_id).tracks[0].id
    bad = {k: v for k, v in META_PAYLOAD.items() if k != "title"}
    assert (await client.post(
        f"/api/tracks/{track_id}/confirm", json={"meta": bad}
    )).status_code == 422


async def test_skip_track(client, app_and_pipeline):
    _, pipeline = app_and_pipeline
    job_id = (await client.post(
        "/api/jobs", json={"urls": ["https://music.youtube.com/watch?v=v1"]}
    )).json()["job_ids"][0]
    track_id = pipeline.store.get_job(job_id).tracks[0].id

    assert (await client.post(f"/api/tracks/{track_id}/skip")).status_code == 200
    assert pipeline.store.get_track(track_id).status == TrackStatus.SKIPPED


async def test_confirm_404(client):
    assert (await client.post(
        "/api/tracks/99999/confirm", json={"meta": META_PAYLOAD}
    )).status_code == 404


async def test_skip_404(client):
    assert (await client.post("/api/tracks/99999/skip")).status_code == 404


# --- confirm/skip 必須檢查曲目目前狀態（見任務說明：已在 DOWNLOADING／DONE
#     的曲目若能再次被 confirm，會產生第二個 finalize 背景工作，兩邊搶著寫
#     同一組目的地路徑）---


async def test_confirm_rejects_already_done_track(client, app_and_pipeline):
    _, pipeline = app_and_pipeline
    job_id = (await client.post(
        "/api/jobs", json={"urls": ["https://music.youtube.com/watch?v=v1"]}
    )).json()["job_ids"][0]
    track_id = pipeline.store.get_job(job_id).tracks[0].id
    await client.post(f"/api/tracks/{track_id}/confirm", json={"meta": META_PAYLOAD})
    await _wait_for(pipeline, track_id, TrackStatus.DONE)

    response = await client.post(f"/api/tracks/{track_id}/confirm", json={"meta": META_PAYLOAD})
    assert response.status_code == 409
    assert TrackStatus.DONE.value in response.json()["detail"]


async def test_confirm_rejects_track_already_downloading(tmp_path):
    """卡住 finalize，確定性地讓 track 停在 DOWNLOADING，驗證第二次 confirm
    會被擋下（而不是又排一個背景工作出來跟第一個搶著寫同一組檔案路徑）。"""
    gate = threading.Event()

    def gated_download(video_id, workdir, **kwargs):
        gate.wait(timeout=5)
        return _fake_download(video_id, workdir, **kwargs)

    pipeline = _build_pipeline(tmp_path, download_fn=gated_download)
    app = create_app(pipeline)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        job_id = (await c.post(
            "/api/jobs", json={"urls": ["https://music.youtube.com/watch?v=v1"]}
        )).json()["job_ids"][0]
        track_id = pipeline.store.get_job(job_id).tracks[0].id

        first = await c.post(f"/api/tracks/{track_id}/confirm", json={"meta": META_PAYLOAD})
        assert first.status_code == 200
        assert pipeline.store.get_track(track_id).status == TrackStatus.DOWNLOADING

        second = await c.post(f"/api/tracks/{track_id}/confirm", json={"meta": META_PAYLOAD})
        assert second.status_code == 409
        assert TrackStatus.DOWNLOADING.value in second.json()["detail"]

        gate.set()
        await _wait_for(pipeline, track_id, TrackStatus.DONE)
        await asyncio.sleep(0)  # 讓 done_callback 有機會跑，避免懸掛的背景 task
    pipeline.store.close()


async def test_skip_rejects_already_done_track(client, app_and_pipeline):
    _, pipeline = app_and_pipeline
    job_id = (await client.post(
        "/api/jobs", json={"urls": ["https://music.youtube.com/watch?v=v1"]}
    )).json()["job_ids"][0]
    track_id = pipeline.store.get_job(job_id).tracks[0].id
    await client.post(f"/api/tracks/{track_id}/confirm", json={"meta": META_PAYLOAD})
    await _wait_for(pipeline, track_id, TrackStatus.DONE)

    response = await client.post(f"/api/tracks/{track_id}/skip")
    assert response.status_code == 409
    assert TrackStatus.DONE.value in response.json()["detail"]


# --- job 層級錯誤（Task 10 之後 probe 整批失敗記錄在 jobs.error，tracks 維持空陣列）---


async def test_get_job_surfaces_job_level_error(tmp_path):
    def failing_probe(url):
        raise youtube.ProbeError("這部影片已下架")

    pipeline = _build_pipeline(tmp_path, probe_fn=failing_probe)
    app = create_app(pipeline)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        job_id = (await c.post(
            "/api/jobs", json={"urls": ["https://music.youtube.com/watch?v=bad"]}
        )).json()["job_ids"][0]
        body = (await c.get(f"/api/jobs/{job_id}")).json()

    assert body["tracks"] == []
    assert body["error"] is not None
    assert "已下架" in body["error"]
    pipeline.store.close()


# --- SSE ---


async def test_events_stream_terminates_for_job_with_no_tracks(tmp_path):
    """job.tracks 是空陣列時（job 層級探測失敗），SSE 不能無窮迴圈。"""

    def failing_probe(url):
        raise youtube.ProbeError("地區限制")

    pipeline = _build_pipeline(tmp_path, probe_fn=failing_probe)
    app = create_app(pipeline)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        job_id = (await c.post(
            "/api/jobs", json={"urls": ["https://music.youtube.com/watch?v=bad"]}
        )).json()["job_ids"][0]

        async def _drain():
            events = []
            async with c.stream("GET", f"/api/jobs/{job_id}/events") as response:
                assert response.status_code == 200
                async for line in response.aiter_lines():
                    if line.startswith("data:"):
                        events.append(line)
            return events

        events = await asyncio.wait_for(_drain(), timeout=3.0)
    assert events
    payload = json.loads(events[-1][len("data: "):])
    assert payload["tracks"] == []
    pipeline.store.close()


def _endpoint(app: FastAPI, path: str):
    """依路徑找出路由函式本體（繞過 ASGI 傳輸層直接呼叫用）。"""
    for route in app.routes:
        if getattr(route, "path", None) == path:
            return route.endpoint
    raise AssertionError(f"找不到路由 {path}")


async def test_events_stream_survives_population_window_then_reports_terminal(
    tmp_path, monkeypatch
):
    """job 建立後、submit() 還卡在 probe_fn 裡時（tracks 仍是空陣列、
    job.error 也還是 None），SSE 不能把這個「還在填充中」的窗口誤判成
    「job 層級探測失敗、永遠不會再有新曲目」而提早關閉串流。

    JobStore 是 autocommit（isolation_level=None），create_job() 一提交就對
    外可見，早於 probe_fn 與逐曲 add_track 執行；POST /api/jobs 把 submit()
    丟給 asyncio.to_thread，整個 probe + 逐曲比對期間事件迴圈是空的，此時
    另一個請求打開 SSE 觀察到的 job.tracks == [] 和「job 層級失敗」在表面上
    無法區分——區分依據是 job.error 是否為 None。用 threading.Event 卡住
    probe_fn，確定性地打開這個窗口。

    注意：httpx.ASGITransport.handle_async_request 會把整個 ASGI 呼叫（含
    StreamingResponse 產生器整段跑完）buffer 完才回傳 Response 物件給
    client（見 httpx/_transports/asgi.py：`await self.app(...)` 完全結束
    後才組出 Response），沒辦法拿來觀察「串流在另一個請求還卡住時是否仍
    活著」這種真正的漸進式時序——用 client.stream() 讀到第一行之前，
    generator 早就把整段（含最終的 break）跑完了。因此這裡直接呼叫路由
    函式本體、逐塊消費 StreamingResponse.body_iterator（就是 routes.py
    裡那個 async generator 本尊），取得未被 buffer 的真實時序；confirm／
    skip 這類非串流端點沒有這個問題，一樣直接呼叫路由函式，語意等同經過
    HTTP 層。
    """
    monkeypatch.setattr("app.api.routes.SSE_INTERVAL_SECONDS", 0.05)

    probe_started = threading.Event()
    probe_gate = threading.Event()

    def gated_probe(url):
        probe_started.set()
        assert probe_gate.wait(timeout=5), "probe_gate 逾時未被釋放"
        return [_source()]

    pipeline = _build_pipeline(tmp_path, probe_fn=gated_probe)
    app = create_app(pipeline)
    submit_jobs = _endpoint(app, "/api/jobs")
    job_events = _endpoint(app, "/api/jobs/{job_id}/events")
    skip_track = _endpoint(app, "/api/tracks/{track_id}/skip")

    async def _run():
        submit_task = asyncio.create_task(
            submit_jobs(SubmitRequest(urls=["https://music.youtube.com/watch?v=v1"]))
        )

        for _ in range(200):
            if probe_started.is_set():
                break
            await asyncio.sleep(0.01)
        else:
            raise AssertionError("probe_fn 沒有在時限內被呼叫")

        jobs = pipeline.store.list_jobs()
        assert len(jobs) == 1
        job_id = jobs[0].id
        assert jobs[0].tracks == []
        assert jobs[0].error is None

        response = await job_events(job_id)
        assert response.status_code == 200

        events: list[dict] = []
        skip_sent = False
        async for chunk in response.body_iterator:
            text = chunk if isinstance(chunk, str) else chunk.decode("utf-8")
            for line in text.splitlines():
                if not line.startswith("data:"):
                    continue
                payload = json.loads(line[len("data: "):])
                events.append(payload)

                if len(events) == 2:
                    # 串流在填充窗口期間至少撐過兩輪、沒有被空陣列誤判提早
                    # 結束——這正是本測試要證明的行為。現在才放行 probe，
                    # 讓 submit() 補上曲目。
                    probe_gate.set()
                elif payload["tracks"] and not skip_sent:
                    skip_sent = True
                    track_id = payload["tracks"][0]["id"]
                    skip_result = await skip_track(track_id)
                    assert skip_result["status"] == TrackStatus.SKIPPED.value

        submit_result = await submit_task
        assert submit_result["job_ids"] == [job_id]
        return events

    events = await asyncio.wait_for(_run(), timeout=8.0)

    assert len(events) >= 3
    assert events[0]["tracks"] == []
    assert events[1]["tracks"] == []
    assert events[-1]["tracks"][0]["status"] == TrackStatus.SKIPPED.value
    pipeline.store.close()


async def test_events_stream_reports_terminal_status(client, app_and_pipeline):
    _, pipeline = app_and_pipeline
    job_id = (await client.post(
        "/api/jobs", json={"urls": ["https://music.youtube.com/watch?v=v1"]}
    )).json()["job_ids"][0]
    track_id = pipeline.store.get_job(job_id).tracks[0].id
    await client.post(f"/api/tracks/{track_id}/confirm", json={"meta": META_PAYLOAD})

    async def _drain():
        events = []
        async with client.stream("GET", f"/api/jobs/{job_id}/events") as response:
            assert response.status_code == 200
            async for line in response.aiter_lines():
                if line.startswith("data:"):
                    events.append(line)
        return events

    events = await asyncio.wait_for(_drain(), timeout=5.0)
    assert events
    payload = json.loads(events[-1][len("data: "):])
    assert payload["tracks"][0]["status"] == TrackStatus.DONE.value


async def test_events_404(client):
    assert (await client.get("/api/jobs/99999/events")).status_code == 404


# --- 背景下載工作的存活期（見任務說明：asyncio.create_task 只有弱參照，
#     沒被強參照住的 Task 可能在下載中途被 GC 回收，下載無聲消失）---


async def test_confirm_keeps_strong_reference_until_finalize_completes(tmp_path):
    """confirm 建立的背景 task 必須被某處強參照住，直到完成才釋放。

    用 threading.Event 卡住 finalize，確定性地驗證：
    - task 開始執行、尚未完成時，仍能在追蹤集合裡找到它（強參照存在）；
    - 完成之後，追蹤集合會清乾淨（沒有洩漏）。
    若實作退回成裸的 `asyncio.create_task(...)`（不存參照），這個追蹤集合
    根本不會存在／不會有內容，斷言會失敗。
    """
    gate = threading.Event()

    def gated_download(video_id, workdir, **kwargs):
        gate.wait(timeout=5)
        return _fake_download(video_id, workdir, **kwargs)

    pipeline = _build_pipeline(tmp_path, download_fn=gated_download)
    app = create_app(pipeline)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        job_id = (await c.post(
            "/api/jobs", json={"urls": ["https://music.youtube.com/watch?v=v1"]}
        )).json()["job_ids"][0]
        track_id = pipeline.store.get_job(job_id).tracks[0].id

        response = await c.post(f"/api/tracks/{track_id}/confirm", json={"meta": META_PAYLOAD})
        assert response.status_code == 200

        # finalize 目前卡在 gate.wait() 裡（還沒完成）。趁現在做幾次 gc.collect()，
        # 再確認追蹤集合仍持有這個 task —— 這正是「強參照」要保護的窗口。
        for _ in range(5):
            await asyncio.sleep(0)
            gc.collect()
        assert len(app.state.background_tasks) == 1

        gate.set()
        await _wait_for(pipeline, track_id, TrackStatus.DONE)
        await asyncio.sleep(0)  # 讓 done_callback 有機會跑
        assert len(app.state.background_tasks) == 0
    pipeline.store.close()


async def test_confirm_background_exception_is_logged(tmp_path, caplog):
    """finalize 背景例外不能被無聲吞掉——至少要有記錄可查。"""
    pipeline = _build_pipeline(tmp_path)

    def boom(track_id):
        raise RuntimeError("finalize 內部炸開了")

    pipeline.finalize = boom  # type: ignore[method-assign]
    app = create_app(pipeline)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        job_id = (await c.post(
            "/api/jobs", json={"urls": ["https://music.youtube.com/watch?v=v1"]}
        )).json()["job_ids"][0]
        track_id = pipeline.store.get_job(job_id).tracks[0].id

        with caplog.at_level(logging.ERROR):
            response = await c.post(
                f"/api/tracks/{track_id}/confirm", json={"meta": META_PAYLOAD}
            )
            assert response.status_code == 200
            for _ in range(50):
                if not app.state.background_tasks:
                    break
                await asyncio.sleep(0.05)
            else:
                raise AssertionError("背景工作沒有在時限內結束")

    assert any("finalize 內部炸開了" in record.getMessage() for record in caplog.records) or any(
        record.exc_info and "finalize 內部炸開了" in str(record.exc_info[1])
        for record in caplog.records
    )
    pipeline.store.close()


# --- 刪除任務 / 清除已完成紀錄 ---


async def test_delete_job_removes_it(client, app_and_pipeline):
    _, pipeline = app_and_pipeline
    job_id = (await client.post(
        "/api/jobs", json={"urls": ["https://music.youtube.com/watch?v=v1"]}
    )).json()["job_ids"][0]

    response = await client.delete(f"/api/jobs/{job_id}")
    assert response.status_code == 200
    assert response.json() == {"deleted": True}
    assert pipeline.store.get_job(job_id) is None


async def test_delete_job_404(client):
    response = await client.delete("/api/jobs/99999")
    assert response.status_code == 404


async def test_delete_job_rejects_track_in_flight(tmp_path):
    """曲目正在 DOWNLOADING／TAGGING 時，背景執行緒還在寫 store 與檔案系統，
    這時把 job 刪掉，那個背景 thread 之後的每一次 store 寫入都會操作在一個
    已經不存在的 job_id 上（cascade 早把 track 列也刪了），是資料損毀的
    寫入孤兒資料 —— 必須先擋下來，跟 confirm／skip 對非 awaiting_confirm
    曲目回 409 是同一個道理（見 test_confirm_rejects_track_already_downloading）。"""
    gate = threading.Event()

    def gated_download(video_id, workdir, **kwargs):
        gate.wait(timeout=5)
        return _fake_download(video_id, workdir, **kwargs)

    pipeline = _build_pipeline(tmp_path, download_fn=gated_download)
    app = create_app(pipeline)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        job_id = (await c.post(
            "/api/jobs", json={"urls": ["https://music.youtube.com/watch?v=v1"]}
        )).json()["job_ids"][0]
        track_id = pipeline.store.get_job(job_id).tracks[0].id

        confirm_resp = await c.post(f"/api/tracks/{track_id}/confirm", json={"meta": META_PAYLOAD})
        assert confirm_resp.status_code == 200
        assert pipeline.store.get_track(track_id).status == TrackStatus.DOWNLOADING

        delete_resp = await c.delete(f"/api/jobs/{job_id}")
        assert delete_resp.status_code == 409
        assert TrackStatus.DOWNLOADING.value in delete_resp.json()["detail"]
        # 沒被真的刪掉
        assert pipeline.store.get_job(job_id) is not None

        gate.set()
        await _wait_for(pipeline, track_id, TrackStatus.DONE)
        await asyncio.sleep(0)  # 讓 done_callback 有機會跑，避免懸掛的背景 task
    pipeline.store.close()


async def test_delete_job_allowed_once_track_reaches_terminal_status(client, app_and_pipeline):
    """跟上面相反的情境：曲目已經跑完（DONE），這時刪除必須放行——不能因為
    這個 job 曾經有過背景工作就永遠鎖死。"""
    _, pipeline = app_and_pipeline
    job_id = (await client.post(
        "/api/jobs", json={"urls": ["https://music.youtube.com/watch?v=v1"]}
    )).json()["job_ids"][0]
    track_id = pipeline.store.get_job(job_id).tracks[0].id
    await client.post(f"/api/tracks/{track_id}/confirm", json={"meta": META_PAYLOAD})
    await _wait_for(pipeline, track_id, TrackStatus.DONE)

    response = await client.delete(f"/api/jobs/{job_id}")
    assert response.status_code == 200
    assert pipeline.store.get_job(job_id) is None


async def test_clear_finished_jobs_returns_deleted_count(client, app_and_pipeline):
    _, pipeline = app_and_pipeline
    job_id = (await client.post(
        "/api/jobs", json={"urls": ["https://music.youtube.com/watch?v=v1"]}
    )).json()["job_ids"][0]
    track_id = pipeline.store.get_job(job_id).tracks[0].id
    await client.post(f"/api/tracks/{track_id}/confirm", json={"meta": META_PAYLOAD})
    await _wait_for(pipeline, track_id, TrackStatus.DONE)

    response = await client.post("/api/jobs/clear-finished")
    assert response.status_code == 200
    assert response.json() == {"deleted": 1}
    assert pipeline.store.get_job(job_id) is None


async def test_clear_finished_jobs_leaves_populating_job_alone(client, app_and_pipeline):
    """探測還沒完成（沒有 track、沒有 job.error）的 job 不能被清掉——見
    JobStore.delete_finished_jobs() 的說明。"""
    _, pipeline = app_and_pipeline
    job_id = (await client.post(
        "/api/jobs", json={"urls": ["https://music.youtube.com/watch?v=v1"]}
    )).json()["job_ids"][0]
    track_id = pipeline.store.get_job(job_id).tracks[0].id
    # 手動把唯一的 track 撥回非終態，模擬「job 還在跑」，不牽動 delete_finished_jobs
    # 判斷 job 是否完成的邏輯本身（那部分已經在 test_store.py 覆蓋過）。
    pipeline.store.set_status(track_id, TrackStatus.DOWNLOADING)

    response = await client.post("/api/jobs/clear-finished")
    assert response.status_code == 200
    assert response.json() == {"deleted": 0}
    assert pipeline.store.get_job(job_id) is not None


async def _wait_for(pipeline, track_id, status, timeout=5.0):
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if pipeline.store.get_track(track_id).status == status:
            return
        await asyncio.sleep(0.05)
    raise AssertionError(
        f"曲目 {track_id} 未在時限內到達 {status}，"
        f"目前為 {pipeline.store.get_track(track_id).status}"
    )
