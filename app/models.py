"""跨模組共用的資料模型。全部不可變，方便在 thread pool 之間傳遞。"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SourceTrack:
    """從 YouTube Music 抽出的原始曲目資訊，尚未經過外部 metadata 補齊。"""

    video_id: str
    url: str
    raw_title: str
    duration: int | None
    artist: str | None
    track: str | None
    album: str | None
    release_year: int | None


@dataclass(frozen=True, slots=True)
class TrackMeta:
    """要寫入檔案的最終標籤內容。"""

    title: str
    artists: tuple[str, ...]
    album_artist: str
    album: str
    year: int | None
    track_no: int | None
    track_total: int | None
    genre: str | None
    cover_url: str | None
    duration: int | None
    source_url: str | None

    @property
    def display_artists(self) -> str:
        """對應 Windows 檔案總管「參與演出者」欄位的字串形式。"""
        return "; ".join(self.artists)


@dataclass(frozen=True, slots=True)
class Candidate:
    meta: TrackMeta
    score: float
