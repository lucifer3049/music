"""YouTube Music 來源：網址分類、metadata 探測、雙串流下載。

這是唯一呼叫 yt-dlp 的模組。所有函式都接受注入點，測試不得觸網。
"""

from __future__ import annotations

from enum import StrEnum
from urllib.parse import parse_qs, urlparse

from app.models import SourceTrack

_YT_HOSTS = {"music.youtube.com", "www.youtube.com", "youtube.com", "m.youtube.com"}
_SHORT_HOST = "youtu.be"
# OLAK5uy_ 開頭是 YouTube 自動生成的專輯清單
_ALBUM_LIST_PREFIX = "OLAK5uy_"


class UnsupportedUrlError(ValueError):
    """不是可處理的 YouTube / YouTube Music 網址。"""


class UrlKind(StrEnum):
    SINGLE = "single"
    ALBUM = "album"
    PLAYLIST = "playlist"


def classify_url(url: str) -> UrlKind:
    parsed = urlparse(url.strip())
    host = parsed.netloc.lower()
    query = parse_qs(parsed.query)

    if host == _SHORT_HOST and parsed.path.strip("/"):
        return UrlKind.SINGLE
    if host not in _YT_HOSTS:
        raise UnsupportedUrlError(f"不支援的網址：{url!r}")

    # watch?v= 一律當單曲處理，即使同時帶 &list=
    if query.get("v"):
        return UrlKind.SINGLE
    list_id = (query.get("list") or [""])[0]
    if list_id.startswith(_ALBUM_LIST_PREFIX):
        return UrlKind.ALBUM
    if list_id:
        return UrlKind.PLAYLIST
    raise UnsupportedUrlError(f"網址沒有 v 或 list 參數：{url!r}")


def _first_str(entry: dict, *keys: str) -> str | None:
    for key in keys:
        value = entry.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, (list, tuple)) and value:
            joined = ", ".join(str(v).strip() for v in value if str(v).strip())
            if joined:
                return joined
    return None


def to_source_track(entry: dict) -> SourceTrack:
    """把 yt-dlp 的 info dict 轉成 SourceTrack。缺欄位一律 None，不猜。"""
    video_id = entry["id"]
    year = entry.get("release_year")
    return SourceTrack(
        video_id=video_id,
        url=f"https://music.youtube.com/watch?v={video_id}",
        raw_title=entry.get("title") or "",
        duration=int(entry["duration"]) if entry.get("duration") else None,
        artist=_first_str(entry, "artist", "artists"),
        track=_first_str(entry, "track"),
        album=_first_str(entry, "album"),
        release_year=int(year) if year else None,
    )


def _default_ydl_factory(opts: dict):
    import yt_dlp

    return yt_dlp.YoutubeDL(opts)


def probe(url: str, *, ydl_factory=_default_ydl_factory) -> list[SourceTrack]:
    """只抽 metadata，不下載任何音訊。回傳順序即專輯／清單順序。"""
    classify_url(url)  # 先驗證，網址不合法就別浪費一次網路來回
    opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "extract_flat": False,
        "ignoreerrors": True,
    }
    with ydl_factory(opts) as ydl:
        info = ydl.extract_info(url, download=False)

    if info is None:
        return []
    entries = info.get("entries")
    if entries is None:
        return [to_source_track(info)]
    # 下架或私人影片在 entries 裡會是 None，跳過而不是讓整批失敗
    return [to_source_track(e) for e in entries if e and e.get("id")]
