"""把各模組串成完整流程。

分兩階段，中間必須經過人工確認：
  submit()   探測 → 比對 → 停在 awaiting_confirm
  finalize() 下載 → 寫標籤 → 落檔 → done

所有外部相依都透過建構子注入，測試不需觸網。

介面說明（Task 5–9 實作期間與原計畫書落差，記錄於此避免下次重蹈覆轍）：
  - KKBOX 來源已於計畫中途放棄（AWS WAF 擋爬蟲），metadata 一律來自 MusicBrainz。
  - youtube.probe() 在整批曲目都拿不到時拋 ProbeError（而非回傳 []）；
    這屬於「job 層級」失敗，不是單一 track 層級 —— 詳見 _record_probe_failure。
  - musicbrainz.search() 依約定不會拋例外（任何失敗都內部吞掉回傳 []），
    這裡故意不包 try/except，避免把注入的測試替身（或未來真正的臭蟲）也一併吞掉。
  - Cover Art Archive 沒有契約保證一定回 JPEG，而 tagging.writer 寫入 m4a 封面時
    寫死 MP4Cover.FORMAT_JPEG；所以封面位元組在送進 write_tags 前，必須先驗證
    JPEG magic bytes（\xff\xd8），驗不過就當沒有封面，不嵌入錯誤標記格式的圖檔。
"""

from __future__ import annotations

import shutil
from pathlib import Path

from app.config import LibraryRoots
from app.jobs.store import JobStore, TrackStatus
from app.matching.matcher import rank_candidates, split_title
from app.models import Candidate, SourceTrack, TrackMeta
from app.sources import musicbrainz, youtube
from app.storage.layout import build_paths
from app.tagging.writer import write_tags

_JPEG_MAGIC = b"\xff\xd8"


def fallback_meta(source: SourceTrack) -> TrackMeta:
    """MusicBrainz 查無結果時，用 YouTube 自身資料組出堪用標籤。"""
    if source.track:
        title = source.track
        artist = source.artist
    else:
        artist, title = split_title(source.raw_title)
    artist = artist or source.artist or "未知演出者"
    album = source.album or title
    return TrackMeta(
        title=title,
        artists=(artist,),
        album_artist=artist,
        album=album,
        year=source.release_year,
        track_no=None,
        track_total=None,
        genre=None,
        cover_url=None,
        duration=source.duration,
        source_url=source.url,
    )


def _looks_like_jpeg(data: bytes) -> bool:
    return data.startswith(_JPEG_MAGIC)


class Pipeline:
    def __init__(
        self,
        store: JobStore,
        roots: LibraryRoots,
        *,
        workdir: Path,
        client_factory=musicbrainz.make_client,
        probe_fn=youtube.probe,
        search_fn=musicbrainz.search,
        download_fn=youtube.download_streams,
        cover_fn=musicbrainz.fetch_cover,
        write_fn=write_tags,
    ) -> None:
        self.store = store
        self.roots = roots
        self.workdir = workdir
        self._client_factory = client_factory
        self._probe_fn = probe_fn
        self._search_fn = search_fn
        self._download_fn = download_fn
        self._cover_fn = cover_fn
        self._write_fn = write_fn

    def submit(self, url: str) -> int:
        """探測網址並為每首曲目產生候選標籤。回傳 job id。

        probe 整批失敗（下架、私人、地區限制、網址不支援）時，不能無聲返回一個
        空 job —— 使用者需要知道原因才能決定要不要重試。schema 沒有獨立的 job
        層級狀態欄位，因此用一筆佔位 track 記錄失敗訊息（見 _record_probe_failure）。
        其餘非預期例外（真正的臭蟲）原樣冒出，不偽裝成「已記錄的失敗」。
        """
        job_id = self.store.create_job(url)
        try:
            sources = self._probe_fn(url)
        except (youtube.ProbeError, youtube.UnsupportedUrlError) as exc:
            self._record_probe_failure(job_id, url, exc)
            return job_id

        with self._client_factory() as client:
            for source in sources:
                self._process_source(job_id, source, client)
        return job_id

    def _record_probe_failure(
        self, job_id: int, url: str, exc: Exception
    ) -> None:
        placeholder = SourceTrack(
            video_id="",
            url=url,
            raw_title="",
            duration=None,
            artist=None,
            track=None,
            album=None,
            release_year=None,
        )
        track_id = self.store.add_track(job_id, placeholder)
        self.store.set_status(track_id, TrackStatus.FAILED, error=str(exc))

    def _process_source(self, job_id: int, source: SourceTrack, client) -> None:
        """一首曲目探測成功之後的後續處理：判重複／比對，失敗只記錄不外拋。

        單一曲目比對失敗不該連累同批其餘曲目 —— 這裡把例外攔下、記成該曲目
        FAILED，讓 submit() 的迴圈能繼續處理下一首。
        """
        if self.store.find_by_video_id(source.video_id):
            track_id = self.store.add_track(job_id, source)
            self.store.set_status(track_id, TrackStatus.SKIPPED, error="已存在，跳過")
            return

        track_id = self.store.add_track(job_id, source)
        try:
            self._match_one(track_id, source, client)
        except Exception as exc:
            self.store.set_status(track_id, TrackStatus.FAILED, error=str(exc))

    def _match_one(self, track_id: int, source: SourceTrack, client) -> None:
        self.store.set_status(track_id, TrackStatus.MATCHING)
        query = " ".join(filter(None, [source.artist, source.track])) or source.raw_title
        # musicbrainz.search() 依約定不會拋例外，故意不包 try/except。
        metas = self._search_fn(query, client=client)

        if metas:
            candidates = rank_candidates(source, metas)
        else:
            # 降級：用 YouTube 自身資料，分數 0 代表未經 MusicBrainz 比對，
            # 仍要提供給使用者確認，不能因為查無結果就擋住下載。
            candidates = [Candidate(meta=fallback_meta(source), score=0.0)]
        self.store.set_candidates(track_id, candidates)

    def finalize(self, track_id: int) -> None:
        """下載、寫標籤、落檔。呼叫前必須已 confirm。"""
        row = self.store.get_track(track_id)
        if row is None:
            raise ValueError(f"找不到曲目 {track_id}")
        if row.chosen is None:
            raise ValueError(f"曲目 {track_id} 尚未確認標籤")

        meta = row.chosen
        try:
            self.store.set_status(track_id, TrackStatus.DOWNLOADING)
            streams = self._download_fn(row.video_id, self.workdir)

            self.store.set_status(track_id, TrackStatus.TAGGING)
            cover = self._fetch_cover(meta)
            paths = build_paths(self.roots, meta)
            paths.m4a.parent.mkdir(parents=True, exist_ok=True)
            paths.opus.parent.mkdir(parents=True, exist_ok=True)

            shutil.move(str(streams.m4a), paths.m4a)
            shutil.move(str(streams.opus), paths.opus)
            self._write_fn(paths.m4a, meta, cover)
            self._write_fn(paths.opus, meta, cover)
            if cover and not paths.cover.exists():
                paths.cover.write_bytes(cover)

            self.store.set_status(track_id, TrackStatus.DONE)
        except Exception as exc:
            self.store.set_status(track_id, TrackStatus.FAILED, error=str(exc))

    def _fetch_cover(self, meta: TrackMeta) -> bytes | None:
        """封面失敗不算致命錯誤 —— 沒圖的歌還是要能收藏。

        抓下來的位元組若不是 JPEG（Cover Art Archive 沒有契約保證一定是），
        也當沒有封面：tagging.writer 寫 m4a 封面時寫死 FORMAT_JPEG，硬塞非
        JPEG 資料只會產生看似合法、實際上格式標記錯誤的檔案。
        """
        if not meta.cover_url:
            return None
        try:
            with self._client_factory() as client:
                data = self._cover_fn(meta.cover_url, client=client)
        except Exception:
            return None
        if not _looks_like_jpeg(data):
            return None
        return data
