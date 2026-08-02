"""輸出路徑生成與 Windows 檔名淨化。純函式，不碰檔案系統。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from app.config import LibraryRoots
from app.models import TrackMeta

# Windows 路徑非法字元
_ILLEGAL = re.compile(r'[\\/:*?"<>|]')
# ASCII 控制字元
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
# Windows 保留裝置名（不分大小寫）
_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


@dataclass(frozen=True, slots=True)
class TrackPaths:
    m4a: Path
    opus: Path
    cover: Path


def sanitize_component(name: str, *, max_len: int = 100) -> str:
    """把任意字串轉成安全的單層路徑名稱。

    非法字元換底線；去掉控制字元；去除尾端句點與空白（Windows 會靜默截掉，
    造成路徑對不上）；保留裝置名前面加底線；長度上限避免超過 MAX_PATH。
    """
    cleaned = _CONTROL.sub("", name)
    cleaned = _ILLEGAL.sub("_", cleaned)
    cleaned = cleaned.strip().rstrip(". ")
    cleaned = cleaned[:max_len]
    cleaned = cleaned.rstrip(". ")
    if not cleaned:
        return "_"
    if cleaned.upper() in _RESERVED:
        return "_" + cleaned
    return cleaned


def _filename(meta: TrackMeta, suffix: str) -> str:
    title = sanitize_component(meta.title)
    if meta.track_no is None:
        return f"{title}{suffix}"
    return f"{meta.track_no:02d} {title}{suffix}"


def build_paths(roots: LibraryRoots, meta: TrackMeta) -> TrackPaths:
    artist_dir = sanitize_component(meta.album_artist)
    album_dir = sanitize_component(meta.album)
    music_dir = roots.music / artist_dir / album_dir
    archive_dir = roots.archive / artist_dir / album_dir
    return TrackPaths(
        m4a=music_dir / _filename(meta, ".m4a"),
        opus=archive_dir / _filename(meta, ".opus"),
        cover=music_dir / "cover.jpg",
    )
