import threading

import pytest

from app.jobs.store import JobStore, TrackStatus
from app.models import Candidate, SourceTrack, TrackMeta


@pytest.fixture
def store(tmp_path):
    s = JobStore(tmp_path / "jobs.db")
    s.init_schema()
    yield s
    s.close()


def _source(video_id="v1") -> SourceTrack:
    return SourceTrack(
        video_id=video_id,
        url=f"https://music.youtube.com/watch?v={video_id}",
        raw_title="【MV】指尖笑 - 人間驚鴻宴",
        duration=207,
        artist=None,
        track=None,
        album=None,
        release_year=None,
    )


def _meta(title="人間驚鴻宴") -> TrackMeta:
    return TrackMeta(
        title=title,
        artists=("指尖笑",),
        album_artist="指尖笑",
        album="人間驚鴻宴",
        year=2026,
        track_no=1,
        track_total=1,
        genre=None,
        cover_url="https://coverartarchive.org/release/00000000-0000-0000-0000-000000000000/cover.jpg",
        duration=207,
        source_url=None,
    )


def test_create_job_and_add_track(store):
    job_id = store.create_job("https://music.youtube.com/watch?v=v1")
    track_id = store.add_track(job_id, _source())
    job = store.get_job(job_id)
    assert job.id == job_id
    assert len(job.tracks) == 1
    assert job.tracks[0].id == track_id
    assert job.tracks[0].status == TrackStatus.PENDING


def test_source_track_roundtrips(store):
    job_id = store.create_job("u")
    track_id = store.add_track(job_id, _source())
    assert store.get_track(track_id).source == _source()


def test_set_candidates_roundtrips(store):
    job_id = store.create_job("u")
    track_id = store.add_track(job_id, _source())
    candidates = [Candidate(meta=_meta(), score=0.97), Candidate(meta=_meta("別首"), score=0.4)]
    store.set_candidates(track_id, candidates)
    row = store.get_track(track_id)
    assert row.status == TrackStatus.AWAITING_CONFIRM
    assert [c.score for c in row.candidates] == [0.97, 0.4]
    assert row.candidates[0].meta == _meta()


def test_confirm_sets_chosen_meta(store):
    job_id = store.create_job("u")
    track_id = store.add_track(job_id, _source())
    store.confirm(track_id, _meta())
    row = store.get_track(track_id)
    assert row.chosen == _meta()
    assert row.status == TrackStatus.DOWNLOADING


def test_set_status_records_error(store):
    job_id = store.create_job("u")
    track_id = store.add_track(job_id, _source())
    store.set_status(track_id, TrackStatus.FAILED, error="影片已下架")
    row = store.get_track(track_id)
    assert row.status == TrackStatus.FAILED
    assert row.error == "影片已下架"


def test_status_transition_clears_previous_error(store):
    job_id = store.create_job("u")
    track_id = store.add_track(job_id, _source())
    store.set_status(track_id, TrackStatus.FAILED, error="暫時性錯誤")
    store.set_status(track_id, TrackStatus.PENDING)
    assert store.get_track(track_id).error is None


def test_find_by_video_id_for_dedup(store):
    job_id = store.create_job("u")
    store.add_track(job_id, _source("dup"))
    assert [r.video_id for r in store.find_by_video_id("dup")] == ["dup"]
    assert store.find_by_video_id("nope") == []


def test_list_jobs_newest_first(store):
    first = store.create_job("u1")
    second = store.create_job("u2")
    assert [j.id for j in store.list_jobs()] == [second, first]


def test_tracks_keep_insertion_order(store):
    job_id = store.create_job("u")
    for i in range(5):
        store.add_track(job_id, _source(f"v{i}"))
    assert [t.video_id for t in store.get_job(job_id).tracks] == [f"v{i}" for i in range(5)]


def test_get_track_missing_returns_none(store):
    assert store.get_track(99999) is None


def test_get_job_missing_returns_none(store):
    assert store.get_job(99999) is None


def test_job_error_defaults_to_none(store):
    job_id = store.create_job("u")
    assert store.get_job(job_id).error is None


def test_set_job_error_persists(store):
    job_id = store.create_job("u")
    store.set_job_error(job_id, "無法取得任何曲目：影片已下架")
    job = store.get_job(job_id)
    assert job.error == "無法取得任何曲目：影片已下架"
    assert job.tracks == []


def test_set_job_error_visible_via_list_jobs(store):
    job_id = store.create_job("u")
    store.set_job_error(job_id, "不支援的網址")
    jobs = store.list_jobs()
    assert jobs[0].id == job_id
    assert jobs[0].error == "不支援的網址"


def test_recover_interrupted_tracks_marks_stuck_statuses_failed(store):
    """伺服器重啟時，MATCHING／DOWNLOADING／TAGGING 這三個非終態必須被收斂成
    FAILED，否則永遠卡住（SSE 不結束、confirm/skip 永遠 409）。其餘狀態
    （包含 PENDING）不該被動到。"""
    job_id = store.create_job("u")
    ids = {}
    for status in (
        TrackStatus.PENDING,
        TrackStatus.MATCHING,
        TrackStatus.AWAITING_CONFIRM,
        TrackStatus.DOWNLOADING,
        TrackStatus.TAGGING,
        TrackStatus.DONE,
        TrackStatus.FAILED,
        TrackStatus.SKIPPED,
    ):
        track_id = store.add_track(job_id, _source(status.value))
        store.set_status(track_id, status)
        ids[status] = track_id

    changed = store.recover_interrupted_tracks("伺服器重啟時中斷，可重新確認")
    assert changed == 3

    for status in (TrackStatus.MATCHING, TrackStatus.DOWNLOADING, TrackStatus.TAGGING):
        row = store.get_track(ids[status])
        assert row.status == TrackStatus.FAILED
        assert row.error == "伺服器重啟時中斷，可重新確認"

    for status in (
        TrackStatus.PENDING,
        TrackStatus.AWAITING_CONFIRM,
        TrackStatus.DONE,
        TrackStatus.FAILED,
        TrackStatus.SKIPPED,
    ):
        row = store.get_track(ids[status])
        assert row.status == status


def test_schema_is_idempotent(tmp_path):
    s = JobStore(tmp_path / "jobs.db")
    s.init_schema()
    s.init_schema()
    s.close()


def test_create_job_concurrent_ids_map_to_correct_url(store):
    """lastrowid 跨執行緒誤配的回歸測試。

    sqlite3_last_insert_rowid() 是「連線層級」而非「陳述式層級」的值。若 N 個
    執行緒在共用連線上並行 INSERT，某個執行緒讀到的 lastrowid 可能跟另一個
    執行緒剛完成的 INSERT 交錯，拿到錯誤的 id。每個回傳的 id 都必須對回
    當初真正建立它的那個執行緒所送出的 job。
    """
    n = 16
    urls = [f"https://music.youtube.com/watch?v=concurrent-{i}" for i in range(n)]
    results: list[int | None] = [None] * n
    errors: list[BaseException] = []
    barrier = threading.Barrier(n)

    def worker(i: int) -> None:
        try:
            barrier.wait()
            results[i] = store.create_job(urls[i])
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"worker threads raised: {errors}"
    assert None not in results
    assert len(set(results)) == n, f"duplicate ids returned: {results}"
    for i, job_id in enumerate(results):
        job = store.get_job(job_id)
        assert job is not None, f"job {job_id} not found"
        assert job.url == urls[i], (
            f"thread {i} created url {urls[i]!r} but id {job_id} maps to "
            f"url {job.url!r} instead"
        )


def test_delete_job_removes_job(store):
    job_id = store.create_job("u")
    assert store.delete_job(job_id) is True
    assert store.get_job(job_id) is None


def test_delete_job_missing_returns_false(store):
    assert store.delete_job(99999) is False


def test_delete_job_cascades_to_tracks(store):
    """ON DELETE CASCADE 只是 schema 宣告，實際生不生效取決於這個連線的
    PRAGMA foreign_keys 是不是真的開著（sqlite3 預設關閉，需要每個連線自己
    開）。這裡直接驗證刪 job 之後，底下的 track *列* 也從資料表消失
    （不是只有 job.tracks 因為 join 不到而看起來是空的——用 get_track()
    直接查 track 表本身）。"""
    job_id = store.create_job("u")
    track_id = store.add_track(job_id, _source())
    other_job_id = store.create_job("u2")
    other_track_id = store.add_track(other_job_id, _source("v-other"))

    assert store.delete_job(job_id) is True

    assert store.get_track(track_id) is None
    # 其他 job 底下的 track 不該被殃及
    assert store.get_track(other_track_id) is not None


def test_delete_finished_jobs_leaves_still_populating_job_alone(store):
    """job 剛建立、submit() 還在跑（還沒補上任何 track），既沒有 track 也
    沒有 job.error。這種 job 不算「已完成」，清除已完成紀錄不該動到它——
    否則等於刪掉一個還在探測中的任務。"""
    job_id = store.create_job("u")
    deleted = store.delete_finished_jobs()
    assert deleted == 0
    assert store.get_job(job_id) is not None


def test_delete_finished_jobs_removes_job_with_error(store):
    job_id = store.create_job("u")
    store.set_job_error(job_id, "地區限制")
    deleted = store.delete_finished_jobs()
    assert deleted == 1
    assert store.get_job(job_id) is None


def test_delete_finished_jobs_removes_job_with_all_terminal_tracks(store):
    job_id = store.create_job("u")
    done_id = store.add_track(job_id, _source("v-done"))
    store.set_status(done_id, TrackStatus.DONE)
    failed_id = store.add_track(job_id, _source("v-failed"))
    store.set_status(failed_id, TrackStatus.FAILED, error="下架")
    skipped_id = store.add_track(job_id, _source("v-skipped"))
    store.set_status(skipped_id, TrackStatus.SKIPPED)

    deleted = store.delete_finished_jobs()
    assert deleted == 1
    assert store.get_job(job_id) is None


def test_delete_finished_jobs_leaves_job_with_in_progress_track(store):
    job_id = store.create_job("u")
    done_id = store.add_track(job_id, _source("v-done"))
    store.set_status(done_id, TrackStatus.DONE)
    downloading_id = store.add_track(job_id, _source("v-downloading"))
    store.set_status(downloading_id, TrackStatus.DOWNLOADING)

    deleted = store.delete_finished_jobs()
    assert deleted == 0
    assert store.get_job(job_id) is not None


def test_delete_finished_jobs_only_removes_matching_jobs_and_counts_correctly(store):
    finished_id = store.create_job("finished")
    t = store.add_track(finished_id, _source("v-a"))
    store.set_status(t, TrackStatus.DONE)

    populating_id = store.create_job("populating")

    in_progress_id = store.create_job("in-progress")
    t2 = store.add_track(in_progress_id, _source("v-b"))
    store.set_status(t2, TrackStatus.TAGGING)

    errored_id = store.create_job("errored")
    store.set_job_error(errored_id, "無法取得任何曲目")

    deleted = store.delete_finished_jobs()

    assert deleted == 2
    assert store.get_job(finished_id) is None
    assert store.get_job(errored_id) is None
    assert store.get_job(populating_id) is not None
    assert store.get_job(in_progress_id) is not None


def test_add_track_concurrent_ids_map_to_correct_video(store):
    """跟上面同一個 lastrowid 競速問題，但換成同一個 job 底下並行 add_track。"""
    job_id = store.create_job("https://music.youtube.com/watch?v=parent")
    n = 16
    sources = [_source(f"concurrent-track-{i}") for i in range(n)]
    results: list[int | None] = [None] * n
    errors: list[BaseException] = []
    barrier = threading.Barrier(n)

    def worker(i: int) -> None:
        try:
            barrier.wait()
            results[i] = store.add_track(job_id, sources[i])
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"worker threads raised: {errors}"
    assert None not in results
    assert len(set(results)) == n, f"duplicate ids returned: {results}"
    for i, track_id in enumerate(results):
        row = store.get_track(track_id)
        assert row is not None, f"track {track_id} not found"
        assert row.video_id == sources[i].video_id, (
            f"thread {i} created video_id {sources[i].video_id!r} but id "
            f"{track_id} maps to video_id {row.video_id!r} instead"
        )
