from pathlib import Path

import pytest

from app.config import FfmpegMissingError, LibraryRoots, load_roots, require_ffmpeg


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
