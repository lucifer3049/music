from pathlib import Path

import pytest

from app.config import LibraryRoots
from app.models import TrackMeta
from app.storage.layout import build_paths, sanitize_component


@pytest.mark.parametrize(
    "raw,expected",
    [
        ('a/b\\c:d*e?f"g<h>i|j', "a_b_c_d_e_f_g_h_i_j"),
        ("trailing dots...", "trailing dots"),
        ("trailing space   ", "trailing space"),
        ("CON", "_CON"),
        ("nul", "_nul"),
        ("COM1", "_COM1"),
        ("LPT9", "_LPT9"),
        ("CON.txt", "_CON.txt"),
        ("NUL.mp3", "_NUL.mp3"),
        ("com1.log", "_com1.log"),
        ("CONCERT", "CONCERT"),
        ("正常名稱", "正常名稱"),
        ("", "_"),
        ("   ", "_"),
        ("...", "_"),
    ],
)
def test_sanitize_component(raw, expected):
    assert sanitize_component(raw) == expected


def test_sanitize_component_truncates():
    assert len(sanitize_component("あ" * 500, max_len=100)) == 100


def test_sanitize_component_strips_control_chars():
    assert sanitize_component("a\x00b\nc") == "abc"


def _meta(**over) -> TrackMeta:
    base = dict(
        title="人間驚鴻宴",
        artists=("指尖笑",),
        album_artist="指尖笑",
        album="人間驚鴻宴",
        year=2026,
        track_no=3,
        track_total=10,
        genre=None,
        cover_url=None,
        duration=207,
        source_url=None,
    )
    base.update(over)
    return TrackMeta(**base)


def test_build_paths_layout(tmp_path):
    roots = LibraryRoots(music=tmp_path / "Music", archive=tmp_path / "Archive")
    paths = build_paths(roots, _meta())
    assert paths.m4a == tmp_path / "Music" / "指尖笑" / "人間驚鴻宴" / "03 人間驚鴻宴.m4a"
    assert paths.opus == tmp_path / "Archive" / "指尖笑" / "人間驚鴻宴" / "03 人間驚鴻宴.opus"
    assert paths.cover == tmp_path / "Music" / "指尖笑" / "人間驚鴻宴" / "cover.jpg"


def test_build_paths_without_track_number(tmp_path):
    roots = LibraryRoots(music=tmp_path / "Music", archive=tmp_path / "Archive")
    paths = build_paths(roots, _meta(track_no=None))
    assert paths.m4a.name == "人間驚鴻宴.m4a"


def test_build_paths_sanitizes_every_component(tmp_path):
    roots = LibraryRoots(music=tmp_path / "Music", archive=tmp_path / "Archive")
    paths = build_paths(roots, _meta(album_artist="A/B", album="C:D", title="E?F"))
    assert paths.m4a == tmp_path / "Music" / "A_B" / "C_D" / "03 E_F.m4a"
