"""環境設定與外部工具檢查。"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path

DEFAULT_MUSIC_ROOT = Path(r"D:\Music")
DEFAULT_ARCHIVE_ROOT = Path(r"D:\Archive")

# KEEP_ARCHIVE_COPY 認得的「開啟」拼法，大小寫不敏感；其餘（含未設定）一律視為關閉。
_TRUTHY = {"1", "true", "yes"}


class FfmpegMissingError(RuntimeError):
    """ffmpeg 不在 PATH 上。opus remux 與封面處理都需要它。"""


@dataclass(frozen=True, slots=True)
class LibraryRoots:
    music: Path
    archive: Path


def load_roots() -> LibraryRoots:
    music = os.environ.get("MUSIC_ROOT")
    archive = os.environ.get("ARCHIVE_ROOT")
    return LibraryRoots(
        music=Path(music) if music else DEFAULT_MUSIC_ROOT,
        archive=Path(archive) if archive else DEFAULT_ARCHIVE_ROOT,
    )


def keep_archive_copy() -> bool:
    """是否仍要產生 opus 冷存副本。

    實測同一首歌 opus（itag 251）約 129kbps、m4a（itag 140）約 128kbps ——
    來源 bitrate 逐支影片而異，opus 並不可靠地比較高，多這一份只是把儲存空間
    翻倍，換不到實質好處，因此預設關閉。未設定或無法辨識的值一律視為關閉，
    只有明確填了常見的「開啟」拼法才會打開。
    """
    value = os.environ.get("KEEP_ARCHIVE_COPY", "")
    return value.strip().lower() in _TRUTHY


def require_ffmpeg() -> str:
    path = shutil.which("ffmpeg")
    if path is None:
        raise FfmpegMissingError(
            "找不到 ffmpeg。請執行 `winget install Gyan.FFmpeg` 後重開終端機。"
        )
    return path
