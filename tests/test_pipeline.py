from pathlib import Path

import httpx
import mutagen
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
        cover_url="https://coverartarchive.org/release/00000000-0000-0000-0000-000000000000/cover.jpg",
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


def test_submit_does_not_skip_after_startup_recovery(pipeline):
    """模擬伺服器重啟：曲目卡在 DOWNLOADING 時被 JobStore.recover_interrupted_tracks()
    收斂成 FAILED 之後，重新送出同一個網址必須能正常走到 AWAITING_CONFIRM，
    而不是被舊紀錄擋住（dedup 只認 DONE，見 Pipeline._process_source()）。"""
    job_id = pipeline.submit("https://music.youtube.com/watch?v=v1")
    first_track = pipeline.store.get_job(job_id).tracks[0]
    pipeline.store.confirm(first_track.id, _meta())
    # 不呼叫 finalize()：模擬 confirm() 已把狀態設成 DOWNLOADING、但 process
    # 在背景執行緒真正跑 finalize() 之前就死掉了。
    assert pipeline.store.get_track(first_track.id).status == TrackStatus.DOWNLOADING

    changed = pipeline.store.recover_interrupted_tracks("伺服器重啟時中斷，可重新確認")
    assert changed == 1
    assert pipeline.store.get_track(first_track.id).status == TrackStatus.FAILED

    job_id2 = pipeline.submit("https://music.youtube.com/watch?v=v1")
    track = pipeline.store.get_job(job_id2).tracks[0]
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


def test_finalize_marks_failed_on_mutagen_error_not_internal_error(pipeline):
    """mutagen.MutagenError 繼承 Exception、不是 OSError（實測於本專案 venv 驗證），
    截斷或格式錯誤的下載檔案是最可能觸發的真實情境，必須落進「下載／標籤失敗」
    分支（訊息不帶「未預期的內部錯誤」前綴），而不是被誤判成程式臭蟲。

    只斷言 status 或只斷言訊息內容「有出現」都無法區分兩個 except 分支（兩邊
    都會把例外訊息塞進 final.error）——必須斷言前綴的有無，見
    test_finalize_marks_internal_error_on_programming_bug 的對照組。"""
    job_id = pipeline.submit("https://music.youtube.com/watch?v=v1")
    track = pipeline.store.get_job(job_id).tracks[0]
    pipeline.store.confirm(track.id, _meta())

    def boom(path, meta, cover=None):
        raise mutagen.MutagenError("檔案已截斷，無法解析")

    pipeline._write_fn = boom
    pipeline.finalize(track.id)

    final = pipeline.store.get_track(track.id)
    assert final.status == TrackStatus.FAILED
    assert "檔案已截斷，無法解析" in final.error
    assert not final.error.startswith("未預期的內部錯誤")


def test_finalize_marks_internal_error_on_programming_bug(pipeline):
    """真正的程式錯誤（例如 AttributeError）必須帶著「未預期的內部錯誤」前綴，
    跟上面 MutagenError 的「下載／標籤失敗」分支明確區分開——這是這兩個 except
    子句存在的唯一理由，訊息前綴是唯一能證明兩邊真的走不同分支的訊號。"""
    job_id = pipeline.submit("https://music.youtube.com/watch?v=v1")
    track = pipeline.store.get_job(job_id).tracks[0]
    pipeline.store.confirm(track.id, _meta())

    def boom(path, meta, cover=None):
        raise AttributeError("'NoneType' object has no attribute 'save'")

    pipeline._write_fn = boom
    pipeline.finalize(track.id)

    final = pipeline.store.get_track(track.id)
    assert final.status == TrackStatus.FAILED
    assert final.error.startswith("未預期的內部錯誤：")
    assert "'NoneType' object has no attribute 'save'" in final.error


def test_finalize_does_not_overwrite_existing_destination(pipeline, tmp_path):
    """dedup 只看 video_id；同一首歌從兩個不同 video_id 下載，比對到同一個
    MusicBrainz 候選時會算出同一個落地路徑。shutil.move() 在 Windows 上撞到
    既有檔案會靜默覆蓋，第二次確認絕不能無聲毀掉第一次的下載成果——必須擋下
    並標成 FAILED，讓使用者自己決定，而不是默默用新檔案取代舊檔案。"""
    job_id = pipeline.submit("https://music.youtube.com/watch?v=v1")
    track = pipeline.store.get_job(job_id).tracks[0]
    pipeline.store.confirm(track.id, _meta())
    pipeline.finalize(track.id)
    assert pipeline.store.get_track(track.id).status == TrackStatus.DONE

    dest_m4a = tmp_path / "Music" / "指尖笑" / "人間驚鴻宴" / "01 人間驚鴻宴.m4a"
    original_bytes = dest_m4a.read_bytes()

    # 第二筆曲目：不同 video_id，但比對到一樣的 MusicBrainz 候選（因此落地
    # 路徑相同）——模擬「同一首歌，官方 MV + Topic 頻道兩個不同 video_id」。
    pipeline._probe_fn = lambda url: [_source(video_id="v2")]
    job_id2 = pipeline.submit("https://music.youtube.com/watch?v=v2")
    track2 = pipeline.store.get_job(job_id2).tracks[0]
    assert track2.status == TrackStatus.AWAITING_CONFIRM  # 不同 video_id，dedup 不擋
    pipeline.store.confirm(track2.id, _meta())
    pipeline.finalize(track2.id)

    final2 = pipeline.store.get_track(track2.id)
    assert final2.status == TrackStatus.FAILED
    assert str(dest_m4a) in final2.error

    # 第一次下載的檔案必須原封不動，第二次確認的暫存串流也不該留在 workdir。
    assert dest_m4a.read_bytes() == original_bytes
    assert list((tmp_path / "work").rglob("*")) == []


def test_finalize_uses_a_workdir_unique_per_track_even_for_same_video_id(pipeline):
    """两个不同 track_id 對應同一個 video_id 時（同一首歌重複送出、尚未確認任何
    一筆），各自的 finalize() 必須用不同的暫存 workdir——youtube.download_streams()
    內部用 video_id 當暫存檔名主幹，若兩個並行的 finalize() 共用同一個 workdir，
    會撞進同一組暫存路徑，彼此的失敗清理還可能刪掉對方正在搬移的檔案
    （Task 12 review Defect 5）。"""
    job_id = pipeline.submit("https://music.youtube.com/watch?v=v1")
    track1 = pipeline.store.get_job(job_id).tracks[0]
    job_id2 = pipeline.submit("https://music.youtube.com/watch?v=v1")
    track2 = pipeline.store.get_job(job_id2).tracks[0]
    assert track1.id != track2.id

    pipeline.store.confirm(track1.id, _meta())
    pipeline.store.confirm(track2.id, _meta())

    seen_workdirs = []

    def fake_download(video_id, workdir, **kwargs):
        seen_workdirs.append(workdir)
        workdir.mkdir(parents=True, exist_ok=True)
        m4a = workdir / f"{video_id}.m4a"
        opus = workdir / f"{video_id}.opus"
        m4a.write_bytes(b"m4a")
        opus.write_bytes(b"opus")
        return DownloadedStreams(m4a=m4a, opus=opus)

    pipeline._download_fn = fake_download
    pipeline.finalize(track1.id)
    pipeline.finalize(track2.id)

    assert len(seen_workdirs) == 2
    assert seen_workdirs[0] != seen_workdirs[1]


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
    留在真正的 Music/Archive 目錄裡 —— 那樣從資料夾看會跟正常下載完全分不出來。

    同時（Task 12 review Defect 2）：下載成功、標籤寫入才失敗時，已經落地在
    workdir 的完整 m4a／opus 串流檔（約 10 MB／次）也不能留著不管——
    Pipeline.finalize() 的失敗路徑必須主動清掉它們，不能只是不搬走了事。"""
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
    # workdir 底下這首曲目專屬的暫存目錄不該留下任何殘檔（含子目錄本身）。
    assert _files(tmp_path / "work") == []


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


def _archive_pipeline(tmp_path, *, keep_archive: bool, write_fn=None):
    """建一條可切換冷存副本的 pipeline，其餘相依都注入假實作。"""
    store = JobStore(tmp_path / f"jobs-{keep_archive}.db")
    store.init_schema()
    roots = LibraryRoots(music=tmp_path / "Music", archive=tmp_path / "Archive")

    def fake_download(video_id, workdir, *, fetch_opus=True, **kwargs):
        workdir.mkdir(parents=True, exist_ok=True)
        m4a = workdir / f"{video_id}.m4a"
        m4a.write_bytes(b"m4a")
        opus = None
        if fetch_opus:
            opus = workdir / f"{video_id}.opus"
            opus.write_bytes(b"opus")
        return DownloadedStreams(m4a=m4a, opus=opus)

    tagged = []
    pipe = Pipeline(
        store,
        roots,
        workdir=tmp_path / "work",
        client_factory=lambda: httpx.Client(
            transport=httpx.MockTransport(lambda r: httpx.Response(200, content=b""))
        ),
        probe_fn=lambda url: [_source()],
        search_fn=lambda q, client: [],
        download_fn=fake_download,
        cover_fn=lambda url, client: b"",
        genre_fn=lambda mbid, client: None,
        write_fn=write_fn or (lambda path, meta, cover=None: tagged.append(path)),
        keep_archive_copy=keep_archive,
    )
    pipe.tagged = tagged
    return pipe


def _run_to_finalize(pipe):
    job_id = pipe.submit("https://music.youtube.com/watch?v=v1")
    track = pipe.store.get_job(job_id).tracks[0]
    pipe.store.confirm(track.id, _meta())
    pipe.finalize(track.id)
    return pipe.store.get_track(track.id)


def test_finalize_files_only_m4a_when_archive_disabled(tmp_path):
    pipe = _archive_pipeline(tmp_path, keep_archive=False)
    final = _run_to_finalize(pipe)

    assert final.status == TrackStatus.DONE
    assert (tmp_path / "Music" / "指尖笑" / "人間驚鴻宴" / "01 人間驚鴻宴.m4a").exists()
    assert not (tmp_path / "Archive").exists(), "冷存關閉時不該建出 Archive 樹"
    assert [p.suffix for p in pipe.tagged] == [".m4a"], "只該對 m4a 寫標籤"
    pipe.store.close()


def test_finalize_files_both_when_archive_enabled(tmp_path):
    pipe = _archive_pipeline(tmp_path, keep_archive=True)
    final = _run_to_finalize(pipe)

    assert final.status == TrackStatus.DONE
    assert (tmp_path / "Music" / "指尖笑" / "人間驚鴻宴" / "01 人間驚鴻宴.m4a").exists()
    assert (tmp_path / "Archive" / "指尖笑" / "人間驚鴻宴" / "01 人間驚鴻宴.opus").exists()
    assert sorted(p.suffix for p in pipe.tagged) == [".m4a", ".opus"]
    pipe.store.close()


def test_stale_archive_file_does_not_block_download_when_archive_disabled(tmp_path):
    """冷存關閉後，Archive 樹裡的舊檔不該擋住一次根本不會寫到那裡的下載。"""
    stale = tmp_path / "Archive" / "指尖笑" / "人間驚鴻宴" / "01 人間驚鴻宴.opus"
    stale.parent.mkdir(parents=True, exist_ok=True)
    stale.write_bytes(b"previous run")

    pipe = _archive_pipeline(tmp_path, keep_archive=False)
    final = _run_to_finalize(pipe)

    assert final.status == TrackStatus.DONE, f"不該被舊檔擋下：{final.error}"
    assert stale.read_bytes() == b"previous run", "舊檔不該被動到"
    pipe.store.close()


@pytest.mark.parametrize("keep_archive", [True, False])
def test_workdir_left_clean_on_tagging_failure(tmp_path, keep_archive):
    def boom(path, meta, cover=None):
        raise RuntimeError("寫標籤爆炸")

    pipe = _archive_pipeline(tmp_path, keep_archive=keep_archive, write_fn=boom)
    final = _run_to_finalize(pipe)

    assert final.status == TrackStatus.FAILED
    leftovers = list((tmp_path / "work").rglob("*")) if (tmp_path / "work").exists() else []
    assert not [p for p in leftovers if p.is_file()], f"workdir 留下殘骸：{leftovers}"
    assert not (tmp_path / "Music").exists() or not list((tmp_path / "Music").rglob("*.m4a"))
    pipe.store.close()


def _query_capturing_pipeline(tmp_path, search_results):
    """記錄 pipeline 實際送給 MusicBrainz 的查詢字串。

    search_results 是一串回應：每次呼叫依序取用，用來模擬「第一次查空、
    退回自由查詢才有結果」的情況。
    """
    store = JobStore(tmp_path / "jobs.db")
    store.init_schema()
    queries = []
    responses = list(search_results)

    def fake_search(query, *, client):
        queries.append(query)
        return responses.pop(0) if responses else []

    pipe = Pipeline(
        store,
        LibraryRoots(music=tmp_path / "Music", archive=tmp_path / "Archive"),
        workdir=tmp_path / "work",
        client_factory=lambda: httpx.Client(
            transport=httpx.MockTransport(lambda r: httpx.Response(200, content=b""))
        ),
        probe_fn=lambda url: [_source()],
        search_fn=fake_search,
        download_fn=lambda *a, **k: None,
        cover_fn=lambda url, client: b"",
        write_fn=lambda path, meta, cover=None: None,
    )
    pipe.queries = queries
    return pipe


def test_match_uses_field_qualified_query(tmp_path):
    """歌名與演出者必須各進各的欄位。

    實測 ReoNa 的 Amore：自由查詢 `ReoNa Amore` 前三筆全是不相干的
    「ピルグリム -ReoNa ver.-」，欄位限定後第一筆就是 Amore。
    """
    pipe = _query_capturing_pipeline(tmp_path, [[_meta()]])
    pipe.submit("https://music.youtube.com/watch?v=v1")

    assert pipe.queries == ['recording:"人間驚鴻宴" AND artist:"指尖笑"']
    pipe.store.close()


def test_match_falls_back_to_free_text_when_qualified_query_finds_nothing(tmp_path):
    """欄位限定查得準但也嚴格；查空時要再試一次自由查詢，別直接放棄。

    退讓順序：欄位限定（原字體 → 另一種字體）→ 自由查詢（同樣兩種字體）。
    """
    pipe = _query_capturing_pipeline(tmp_path, [[], [], [_meta()]])
    job_id = pipe.submit("https://music.youtube.com/watch?v=v1")

    assert len(pipe.queries) == 3
    assert pipe.queries[0].startswith("recording:")
    assert pipe.queries[1].startswith("recording:")
    assert pipe.queries[2] == _source().raw_title

    track = pipe.store.get_job(job_id).tracks[0]
    assert track.candidates[0].score > 0, "退回查詢找到的結果不該被當成未配對"
    pipe.store.close()


def test_match_never_exceeds_three_queries_per_track(tmp_path):
    """一首曲目最多三次查詢，這是使用者要等多久的直接上限。

    MusicBrainz 匿名存取限每秒 1 次，每多一次查詢就是整批多等一輪 ——
    39 首的專輯全部查空時，三次是 129 秒，四次就是 172 秒。自由查詢那段
    刻意不試簡繁變體，就是為了守住這個上限（見 Pipeline._match_one）。
    """
    pipe = _query_capturing_pipeline(tmp_path, [[], [], []])
    pipe.submit("https://music.youtube.com/watch?v=v1")
    assert len(pipe.queries) == 3
    pipe.store.close()


def test_match_retries_qualified_query_in_the_other_han_script(tmp_path):
    """簡繁寫法不同不該讓歌整個查不到。

    實例：YouTube 標成「云翳之上」，MusicBrainz 收錄「雲翳之上」，
    原字體的欄位限定查詢一無所獲。
    """
    pipe = _query_capturing_pipeline(tmp_path, [[], [_meta()]])
    pipe.submit("https://music.youtube.com/watch?v=v1")

    assert len(pipe.queries) == 2
    assert pipe.queries[1] == 'recording:"人间惊鸿宴" AND artist:"指尖笑"'
    pipe.store.close()


def test_match_does_not_retry_when_qualified_query_succeeds(tmp_path):
    """有結果就不該多打一次 —— 每次查詢都要付 1.1 秒節流成本。"""
    pipe = _query_capturing_pipeline(tmp_path, [[_meta()]])
    pipe.submit("https://music.youtube.com/watch?v=v1")
    assert len(pipe.queries) == 1
    pipe.store.close()


def test_match_does_not_multiply_queries_for_ascii_titles(tmp_path):
    """純 ASCII 曲目沒有簡繁變體可試，不能因此多付一次節流成本。"""
    ascii_source = SourceTrack(
        video_id="v1",
        url="https://music.youtube.com/watch?v=v1",
        raw_title="Queen - Bohemian Rhapsody",
        duration=207,
        artist="Queen",
        track="Bohemian Rhapsody",
        album=None,
        release_year=None,
    )
    pipe = _query_capturing_pipeline(tmp_path, [[], []])
    pipe._probe_fn = lambda url: [ascii_source]
    pipe.submit("https://music.youtube.com/watch?v=v1")

    assert pipe.queries == [
        'recording:"Bohemian Rhapsody" AND artist:"Queen"',
        "Queen - Bohemian Rhapsody",
    ]
    pipe.store.close()


def test_weak_candidates_are_outranked_by_youtube_meta(tmp_path):
    """比對不出可信結果時，預設候選必須是 YouTube 自己的資料。

    實例：云翳之上／阿YueYue 在 MusicBrainz 查無此曲，自由查詢撈回
    「云端之上／李安健」這種字面相近卻毫不相干的歌。網頁預設選中第一個
    候選，所以順序直接決定使用者按下確認後寫進檔案的標籤。
    弱候選仍保留在後面，讓使用者能自行判斷。
    """
    # 演出者吻合、時長缺值（兩道硬否決都攔不住），只有歌名對不上 —— 這種
    # 「看起來像、其實不是」的候選只能靠分數地板擋。
    pipe = _query_capturing_pipeline(
        tmp_path, [[_meta(title="人間夜行", duration=None)]]
    )
    job_id = pipe.submit("https://music.youtube.com/watch?v=v1")

    track = pipe.store.get_job(job_id).tracks[0]
    assert track.candidates[0].score == 0.0
    assert track.candidates[0].meta.title == "人間驚鴻宴"
    assert len(track.candidates) == 2, "弱候選要保留，不是丟掉"
    pipe.store.close()


def test_strong_candidate_stays_first(tmp_path):
    """比對得出可信結果時，不能被 YouTube 降級資料擠掉。"""
    pipe = _query_capturing_pipeline(tmp_path, [[_meta()]])
    job_id = pipe.submit("https://music.youtube.com/watch?v=v1")

    track = pipe.store.get_job(job_id).tracks[0]
    assert track.candidates[0].score >= 0.9
    assert track.candidates[0].meta.year == 2026
    pipe.store.close()
