"""標籤寫入。m4a 走 MP4 atom，opus 走 Vorbis comment。

欄位對映刻意與 Windows 檔案總管「內容」面板一致：
標題 / 參與演出者 / 專輯演出者 / 專輯 / 年份 / 曲序 / 類型 / 封面。
"""

from __future__ import annotations

import base64
import struct
from pathlib import Path

from mutagen.flac import Picture
from mutagen.mp4 import MP4, MP4Cover
from mutagen.oggopus import OggOpus

from app.models import TrackMeta

# 帶尺寸資訊的 JPEG SOF marker（跳過 SOF4/SOF8/SOF12 這些非影像 marker）
_SOF_MARKERS = {
    0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
    0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF,
}


class UnsupportedFormatError(ValueError):
    """副檔名不在支援清單內。"""


def jpeg_dimensions(data: bytes) -> tuple[int, int]:
    """從 JPEG 位元組取出寬高。解析不出來回 (0, 0)。

    自己解析是為了避免只為讀兩個數字就引入影像處理依賴。
    """
    if not data.startswith(b"\xff\xd8"):
        return (0, 0)
    index = 2
    length = len(data)
    while index + 9 < length:
        if data[index] != 0xFF:
            index += 1
            continue
        marker = data[index + 1]
        if marker in _SOF_MARKERS:
            height, width = struct.unpack(">HH", data[index + 5 : index + 9])
            return (width, height)
        if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7:
            index += 2
            continue
        segment = struct.unpack(">H", data[index + 2 : index + 4])[0]
        index += 2 + segment
    return (0, 0)


def _make_picture(cover: bytes) -> Picture:
    picture = Picture()
    picture.data = cover
    picture.type = 3  # front cover
    picture.mime = "image/jpeg"
    picture.width, picture.height = jpeg_dimensions(cover)
    picture.depth = 24
    return picture


def _assign(audio, key: str, value) -> None:
    """有值就寫，沒值就把既有的清掉。

    清掉這一步是必要的，不是保險：yt-dlp 抓下來的檔案帶著 YouTube 自己的標籤，
    只寫不刪的話，我們沒有值的欄位就會留著來源檔的殘留值。實例——阿YueYue 的
    云翳之上 MusicBrainz 查無此曲、YouTube 的 release_year 又是荒謬的 1060 被
    消毒成 None，結果檔案裡留著 YouTube 上傳年份 2026，看起來像是查證過的年份。
    TrackMeta 表達的是這個檔案標籤的完整意圖，None 代表「不該有值」。
    """
    if value is None:
        audio.pop(key, None)
    else:
        audio[key] = value


def _write_m4a(path: Path, meta: TrackMeta, cover: bytes | None) -> None:
    audio = MP4(path)
    audio["\xa9nam"] = [meta.title]
    audio["\xa9ART"] = [meta.display_artists]
    audio["aART"] = [meta.album_artist]
    audio["\xa9alb"] = [meta.album]
    _assign(audio, "\xa9day", [str(meta.year)] if meta.year is not None else None)
    _assign(
        audio,
        "trkn",
        [(meta.track_no, meta.track_total or 0)] if meta.track_no is not None else None,
    )
    _assign(audio, "\xa9gen", [meta.genre] if meta.genre else None)
    _assign(
        audio,
        "covr",
        [MP4Cover(cover, imageformat=MP4Cover.FORMAT_JPEG)] if cover else None,
    )
    audio.save()


def _write_opus(path: Path, meta: TrackMeta, cover: bytes | None) -> None:
    audio = OggOpus(path)
    audio["TITLE"] = [meta.title]
    # Vorbis comment 允許同一鍵多值，演出者分開存比串接語意更正確
    audio["ARTIST"] = list(meta.artists)
    audio["ALBUMARTIST"] = [meta.album_artist]
    audio["ALBUM"] = [meta.album]
    _assign(audio, "DATE", [str(meta.year)] if meta.year is not None else None)
    _assign(
        audio, "TRACKNUMBER", [str(meta.track_no)] if meta.track_no is not None else None
    )
    _assign(
        audio,
        "TOTALTRACKS",
        [str(meta.track_total)] if meta.track_total is not None else None,
    )
    _assign(audio, "GENRE", [meta.genre] if meta.genre else None)
    _assign(
        audio,
        "METADATA_BLOCK_PICTURE",
        [base64.b64encode(_make_picture(cover).write()).decode("ascii")] if cover else None,
    )
    audio.save()


_WRITERS = {".m4a": _write_m4a, ".opus": _write_opus}


def write_tags(path: Path, meta: TrackMeta, cover: bytes | None = None) -> None:
    writer = _WRITERS.get(path.suffix.lower())
    if writer is None:
        raise UnsupportedFormatError(f"不支援的副檔名：{path.suffix}")
    writer(path, meta, cover)
