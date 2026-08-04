from pathlib import Path

import httpx
import pytest

from app.config import LibraryRoots
from app.jobs.store import JobStore, TrackStatus
from app.models import SourceTrack, TrackMeta
from app.pipeline import Pipeline, fallback_meta
from app.sources import musicbrainz, youtube
from app.sources.youtube import DownloadedStreams


def _source(video_id="v1") -> SourceTrack:
    return SourceTrack(
        video_id=video_id,
        url=f"https://music.youtube.com/watch?v={video_id}",
        raw_title="【MV】指尖笑 - 人間驚鴻宴",
        duration=207,
        artist="指尖笑",
        track="人間驚鴻宴",
        album=None,
        release_year=None,
    )


def _meta(**over) -> TrackMeta:
    base = dict(
        title="人間驚鴻宴",
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
    base.update(over)
    return TrackMeta(**base)


@pytest.fixture
def pipeline(tmp_path):
    store = JobStore(tmp_path / "jobs.db")
    store.init_schema()
    roots = LibraryRoots(music=tmp_path / "Music", archive=tmp_path / "Archive")

    def fake_download(video_id, workdir, **kwargs):
        workdir.mkdir(parents=True, exist_ok=True)
        m4a = workdir / f"{video_id}.m4a"
        opus = workdir / f"{video_id}.opus"
        m4a.write_bytes(b"m4a")
        opus.write_bytes(b"opus")
        return DownloadedStreams(m4a=m4a, opus=opus)

    written = []
    pipe = Pipeline(
        store,
        roots,
        workdir=tmp_path / "work",
        client_factory=lambda: httpx.Client(
            transport=httpx.MockTransport(lambda r: httpx.Response(200, content=b"\xff\xd8jpeg"))
        ),
        probe_fn=lambda url: [_source()],
        search_fn=lambda q, client: [_meta()],
        download_fn=fake_download,
        cover_fn=lambda url, client: b"\xff\xd8jpeg",
        write_fn=lambda path, meta, cover=None: written.append((path, meta, cover)),
    )
    pipe.written = written
    yield pipe
    store.close()


def test_fallback_meta_uses_youtube_fields():
    meta = fallback_meta(_source())
    assert meta.title == "人間驚鴻宴"
    assert meta.artists == ("指尖笑",)
    assert meta.album == "人間驚鴻宴"


def test_fallback_meta_parses_dirty_title():
    source = SourceTrack(
        video_id="v",
        url="u",
        raw_title="【Official MV】指尖笑 - 人間驚鴻宴",
        duration=None,
        artist=None,
        track=None,
        album=None,
        release_year=None,
    )
    meta = fallback_meta(source)
    assert "人間驚鴻宴" in meta.title


def test_submit_stops_at_awaiting_confirm(pipeline):
    job_id = pipeline.submit("https://music.youtube.com/watch?v=v1")
    job = pipeline.store.get_job(job_id)
    assert len(job.tracks) == 1
    track = job.tracks[0]
    assert track.status == TrackStatus.AWAITING_CONFIRM
    assert track.candidates
    assert track.candidates[0].meta.title == "人間驚鴻宴"


def test_submit_falls_back_when_musicbrainz_empty(tmp_path, pipeline):
    pipeline._search_fn = lambda q, client: []
    job_id = pipeline.submit("https://music.youtube.com/watch?v=v1")
    track = pipeline.store.get_job(job_id).tracks[0]
    assert track.status == TrackStatus.AWAITING_CONFIRM
    assert len(track.candidates) == 1
    assert track.candidates[0].score == 0.0


def test_submit_skips_duplicate_video_id(pipeline):
    """只有先前已成功完成（DONE）的 video_id 才算重複；未完成的不算。"""
    job_id = pipeline.submit("https://music.youtube.com/watch?v=v1")
    first_track = pipeline.store.get_job(job_id).tracks[0]
    pipeline.store.confirm(first_track.id, _meta())
    pipeline.finalize(first_track.id)
    assert pipeline.store.get_track(first_track.id).status == TrackStatus.DONE

    job_id2 = pipeline.submit("https://music.youtube.com/watch?v=v1")
    track = pipeline.store.get_job(job_id2).tracks[0]
    assert track.status == TrackStatus.SKIPPED


def test_submit_does_not_skip_after_failed_download(pipeline):
    """FAILED 的舊紀錄不能擋住重試 —— 使用者重新送出同一個網址時，
    新的一筆必須能正常走到 AWAITING_CONFIRM，而不是被誤判成「已存在」。"""
    job_id = pipeline.submit("https://music.youtube.com/watch?v=v1")
    first_track = pipeline.store.get_job(job_id).tracks[0]
    pipeline.store.confirm(first_track.id, _meta())

    def boom(video_id, workdir, **kwargs):
        raise RuntimeError("網路斷了")

    pipeline._download_fn = boom
    pipeline.finalize(first_track.id)
    assert pipeline.store.get_track(first_track.id).status == TrackStatus.FAILED

    job_id2 = pipeline.submit("https://music.youtube.com/watch?v=v1")
    track = pipeline.store.get_job(job_id2).tracks[0]
    assert track.status != TrackStatus.SKIPPED
    assert track.status == TrackStatus.AWAITING_CONFIRM


def test_submit_persists_probe_error_message(pipeline):
    """ProbeError（下架／私人／地區限制）必須被記錄，不能無聲吞掉。

    這是 job 層級的失敗（probe 連一首曲目都拿不到），記錄在 jobs.error 欄位，
    不再用佔位 track —— job.tracks 保持空陣列，不會混進一筆不對應真實曲目的
    假記錄。
    """

    def boom(url):
        raise youtube.ProbeError(f"無法取得任何曲目：{url!r}\nERROR: 這部影片不存在")

    pipeline._probe_fn = boom
    job_id = pipeline.submit("https://music.youtube.com/watch?v=v1")
    job = pipeline.store.get_job(job_id)

    assert job.tracks == []
    assert job.error is not None
    assert "這部影片不存在" in job.error


def test_submit_persists_unsupported_url_message(pipeline):
    """classify_url 在 probe() 內部第一步就可能拋 UnsupportedUrlError；
    這同樣是「這個網址整批都拿不到東西」，走跟 ProbeError 一樣的記錄路徑。"""

    def boom(url):
        raise youtube.UnsupportedUrlError(f"不支援的網址：{url!r}")

    pipeline._probe_fn = boom
    job_id = pipeline.submit("https://example.com/not-youtube")
    job = pipeline.store.get_job(job_id)

    assert job.tracks == []
    assert job.error is not None
    assert "不支援的網址" in job.error


def test_submit_propagates_unexpected_probe_exception(pipeline):
    """probe_fn 拋出非 ProbeError/UnsupportedUrlError 的例外代表真正的臭蟲，
    不該被吞掉包裝成「已記錄的失敗」——必須整個冒出去讓呼叫端知道系統壞了。"""

    def boom(url):
        raise RuntimeError("這是真正的臭蟲")

    pipeline._probe_fn = boom
    with pytest.raises(RuntimeError, match="這是真正的臭蟲"):
        pipeline.submit("https://music.youtube.com/watch?v=v1")


def test_submit_continues_when_one_track_match_fails(pipeline):
    """同一批裡一首曲目比對失敗，不該連累其餘曲目探測與比對。"""
    sources = [_source(video_id="v1"), _source(video_id="v2")]
    pipeline._probe_fn = lambda url: sources

    calls = []

    def flaky_search(q, client):
        calls.append(q)
        if len(calls) == 1:
            raise RuntimeError("search 掛了")
        return [_meta()]

    pipeline._search_fn = flaky_search
    job_id = pipeline.submit("https://music.youtube.com/watch?v=playlist")
    job = pipeline.store.get_job(job_id)

    assert len(job.tracks) == 2
    assert job.tracks[0].status == TrackStatus.FAILED
    assert "search 掛了" in job.tracks[0].error
    assert job.tracks[1].status == TrackStatus.AWAITING_CONFIRM


def test_finalize_writes_both_files_and_cover(pipeline, tmp_path):
    job_id = pipeline.submit("https://music.youtube.com/watch?v=v1")
    track = pipeline.store.get_job(job_id).tracks[0]
    pipeline.store.confirm(track.id, _meta())
    pipeline.finalize(track.id)

    final = pipeline.store.get_track(track.id)
    assert final.status == TrackStatus.DONE
    assert (tmp_path / "Music" / "指尖笑" / "人間驚鴻宴" / "01 人間驚鴻宴.m4a").exists()
    assert (tmp_path / "Archive" / "指尖笑" / "人間驚鴻宴" / "01 人間驚鴻宴.opus").exists()
    assert (tmp_path / "Music" / "指尖笑" / "人間驚鴻宴" / "cover.jpg").exists()
    assert len(pipeline.written) == 2


def test_finalize_requires_confirmation(pipeline):
    job_id = pipeline.submit("https://music.youtube.com/watch?v=v1")
    track = pipeline.store.get_job(job_id).tracks[0]
    with pytest.raises(ValueError, match="尚未確認"):
        pipeline.finalize(track.id)


def test_finalize_marks_failed_on_download_error(pipeline):
    job_id = pipeline.submit("https://music.youtube.com/watch?v=v1")
    track = pipeline.store.get_job(job_id).tracks[0]
    pipeline.store.confirm(track.id, _meta())

    def boom(video_id, workdir, **kwargs):
        raise RuntimeError("網路斷了")

    pipeline._download_fn = boom
    pipeline.finalize(track.id)
    final = pipeline.store.get_track(track.id)
    assert final.status == TrackStatus.FAILED
    assert "網路斷了" in final.error


def test_finalize_survives_cover_failure(pipeline, tmp_path):
    """封面抓不到不該讓整首曲目失敗。"""
    job_id = pipeline.submit("https://music.youtube.com/watch?v=v1")
    track = pipeline.store.get_job(job_id).tracks[0]
    pipeline.store.confirm(track.id, _meta())

    def boom(url, client):
        raise httpx.HTTPError("404")

    pipeline._cover_fn = boom
    pipeline.finalize(track.id)
    assert pipeline.store.get_track(track.id).status == TrackStatus.DONE
    assert all(cover is None for _, _, cover in pipeline.written)


def test_finalize_skips_non_jpeg_cover(pipeline):
    """Cover Art Archive 沒有契約保證一定回 JPEG；不是 JPEG 就不能標成
    MP4Cover.FORMAT_JPEG 嵌進檔案，寧可沒有封面。"""
    job_id = pipeline.submit("https://music.youtube.com/watch?v=v1")
    track = pipeline.store.get_job(job_id).tracks[0]
    pipeline.store.confirm(track.id, _meta())

    pipeline._cover_fn = lambda url, client: b"GIF89a\x00\x00not a jpeg"
    pipeline.finalize(track.id)

    final = pipeline.store.get_track(track.id)
    assert final.status == TrackStatus.DONE
    assert all(cover is None for _, _, cover in pipeline.written)


def test_finalize_leaves_library_untouched_when_tagging_fails(pipeline, tmp_path):
    """標籤寫入必須發生在檔案還在 workdir 的時候，寫失敗就不能把未標記的檔案
    留在真正的 Music/Archive 目錄裡 —— 那樣從資料夾看會跟正常下載完全分不出來。"""
    job_id = pipeline.submit("https://music.youtube.com/watch?v=v1")
    track = pipeline.store.get_job(job_id).tracks[0]
    pipeline.store.confirm(track.id, _meta())

    def boom(path, meta, cover=None):
        raise RuntimeError("tag 寫入壞掉")

    pipeline._write_fn = boom
    pipeline.finalize(track.id)

    final = pipeline.store.get_track(track.id)
    assert final.status == TrackStatus.FAILED

    def _files(root):
        return list(root.rglob("*")) if root.exists() else []

    assert _files(tmp_path / "Music") == []
    assert _files(tmp_path / "Archive") == []


def test_finalize_skips_cover_when_no_cover_url(pipeline):
    job_id = pipeline.submit("https://music.youtube.com/watch?v=v1")
    track = pipeline.store.get_job(job_id).tracks[0]
    pipeline.store.confirm(track.id, _meta(cover_url=None))

    calls = []
    pipeline._cover_fn = lambda url, client: calls.append(url) or b"\xff\xd8jpeg"
    pipeline.finalize(track.id)

    assert calls == []
    assert pipeline.store.get_track(track.id).status == TrackStatus.DONE


# --- genre：只在 finalize() 對使用者確認的那一首查，不在 search() 比對階段查 ---

_REAL_RELEASE_MBID = "1e14b2d6-8652-3dcc-b60b-4fb5fe79e024"


def _meta_with_real_cover(**over):
    """cover_url 用 musicbrainz.cover_url_for_release() 組出，
    genre_fn 才拆得出 release MBID（見 release_mbid_for_genre_lookup()）。"""
    over.setdefault("cover_url", musicbrainz.cover_url_for_release(_REAL_RELEASE_MBID))
    return _meta(**over)


def test_finalize_fills_genre_for_confirmed_candidate(pipeline):
    """使用者確認的候選沒有 genre 時，finalize() 要去查一次並寫進實際落地的標籤。"""
    job_id = pipeline.submit("https://music.youtube.com/watch?v=v1")
    track = pipeline.store.get_job(job_id).tracks[0]
    pipeline.store.confirm(track.id, _meta_with_real_cover())

    calls = []

    def fake_genre_fn(release_mbid, *, client):
        calls.append(release_mbid)
        return "hard rock"

    pipeline._genre_fn = fake_genre_fn
    pipeline.finalize(track.id)

    assert calls == [_REAL_RELEASE_MBID]
    assert pipeline.store.get_track(track.id).status == TrackStatus.DONE
    assert all(meta.genre == "hard rock" for _, meta, _ in pipeline.written)


def test_finalize_leaves_genre_none_when_lookup_finds_nothing(pipeline):
    """release-group 沒有 genre 資料（實測常見案例）時，genre_fn 回 None，
    寫入的標籤也該是 None，不能編造一個假類型。"""
    job_id = pipeline.submit("https://music.youtube.com/watch?v=v1")
    track = pipeline.store.get_job(job_id).tracks[0]
    pipeline.store.confirm(track.id, _meta_with_real_cover())

    pipeline._genre_fn = lambda release_mbid, *, client: None
    pipeline.finalize(track.id)

    assert pipeline.store.get_track(track.id).status == TrackStatus.DONE
    assert all(meta.genre is None for _, meta, _ in pipeline.written)


def test_finalize_survives_genre_lookup_failure(pipeline):
    """genre 查詢失敗不算致命錯誤（比照封面圖的既有慣例）—— 沒查到 genre 的歌
    還是要能下載完成，不能整首曲目變成 FAILED。"""
    job_id = pipeline.submit("https://music.youtube.com/watch?v=v1")
    track = pipeline.store.get_job(job_id).tracks[0]
    pipeline.store.confirm(track.id, _meta_with_real_cover())

    def boom(release_mbid, *, client):
        raise httpx.HTTPError("503")

    pipeline._genre_fn = boom
    pipeline.finalize(track.id)

    final = pipeline.store.get_track(track.id)
    assert final.status == TrackStatus.DONE
    assert all(meta.genre is None for _, meta, _ in pipeline.written)


def test_finalize_skips_genre_lookup_when_no_release_mbid(pipeline):
    """cover_url 不是 cover_url_for_release() 組出的形狀（例如 fallback_meta 或
    測試替身塞的假網址）時，沒有 release MBID 可查，不該打任何請求。"""
    job_id = pipeline.submit("https://music.youtube.com/watch?v=v1")
    track = pipeline.store.get_job(job_id).tracks[0]
    pipeline.store.confirm(track.id, _meta())  # 預設 cover_url 是假網址

    calls = []
    pipeline._genre_fn = lambda release_mbid, *, client: calls.append(release_mbid) or "rock"
    pipeline.finalize(track.id)

    assert calls == []
    assert all(meta.genre is None for _, meta, _ in pipeline.written)


def test_submit_never_looks_up_genre_during_matching(pipeline):
    """genre 查詢決策（genre-report.md Step 2）：只在使用者確認候選、真的要
    下載那一首時才查，不在 search() 比對階段對每個候選都查——否則比對延遲
    乘上候選數。submit() 全程不該碰 genre_fn。候選要用真實形狀的 cover_url，
    不然即使 _match_one 誤呼叫 genre 查詢，也會被 release_mbid 解析失敗擋掉，
    測不出這條規則。"""
    pipeline._search_fn = lambda q, client: [_meta_with_real_cover()]
    calls = []
    pipeline._genre_fn = lambda release_mbid, *, client: calls.append(release_mbid) or "rock"
    pipeline.submit("https://music.youtube.com/watch?v=v1")
    assert calls == []
