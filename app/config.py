"""環境設定與外部工具檢查。"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path

DEFAULT_MUSIC_ROOT = Path(r"D:\Music")
DEFAULT_ARCHIVE_ROOT = Path(r"D:\Archive")


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


def require_ffmpeg() -> str:
    path = shutil.which("ffmpeg")
    if path is None:
        raise FfmpegMissingError(
            "找不到 ffmpeg。請執行 `winget install Gyan.FFmpeg` 後重開終端機。"
        )
    return path
