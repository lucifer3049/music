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
        cover_url="https://i.kfs.io/x/cover.jpg",
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


def test_schema_is_idempotent(tmp_path):
    s = JobStore(tmp_path / "jobs.db")
    s.init_schema()
    s.init_schema()
    s.close()
