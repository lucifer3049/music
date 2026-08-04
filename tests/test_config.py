from pathlib import Path

import pytest

from app.config import (
    FfmpegMissingError,
    LibraryRoots,
    keep_archive_copy,
    load_roots,
    require_ffmpeg,
)


def test_load_roots_uses_defaults(monkeypatch):
    monkeypatch.delenv("MUSIC_ROOT", raising=False)
    monkeypatch.delenv("ARCHIVE_ROOT", raising=False)
    roots = load_roots()
    assert roots == LibraryRoots(music=Path(r"D:\Music"), archive=Path(r"D:\Archive"))


def test_load_roots_reads_env(monkeypatch, tmp_path):
    monkeypatch.setenv("MUSIC_ROOT", str(tmp_path / "m"))
    monkeypatch.setenv("ARCHIVE_ROOT", str(tmp_path / "a"))
    roots = load_roots()
    assert roots.music == tmp_path / "m"
    assert roots.archive == tmp_path / "a"


def test_require_ffmpeg_returns_path():
    assert require_ffmpeg().lower().endswith(("ffmpeg", "ffmpeg.exe"))


def test_require_ffmpeg_raises_when_absent(monkeypatch):
    monkeypatch.setattr("app.config.shutil.which", lambda _: None)
    with pytest.raises(FfmpegMissingError):
        require_ffmpeg()


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "Yes", " yes "])
def test_keep_archive_copy_recognises_truthy_spellings(monkeypatch, value):
    monkeypatch.setenv("KEEP_ARCHIVE_COPY", value)
    assert keep_archive_copy() is True


@pytest.mark.parametrize("value", ["0", "false", "no", "", "  ", "maybe", "on"])
def test_keep_archive_copy_treats_anything_else_as_off(monkeypatch, value):
    """無法辨識的值一律關閉 —— 打錯字不該悄悄讓儲存空間翻倍。"""
    monkeypatch.setenv("KEEP_ARCHIVE_COPY", value)
    assert keep_archive_copy() is False


def test_keep_archive_copy_defaults_to_off(monkeypatch):
    monkeypatch.delenv("KEEP_ARCHIVE_COPY", raising=False)
    assert keep_archive_copy() is False
