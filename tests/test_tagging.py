import base64

import pytest
from mutagen.flac import Picture
from mutagen.mp4 import MP4
from mutagen.oggopus import OggOpus

from app.models import TrackMeta
from app.tagging.writer import UnsupportedFormatError, jpeg_dimensions, write_tags


def _meta(**over) -> TrackMeta:
    base = dict(
        title="人間驚鴻宴",
        artists=("指尖笑", "另一位"),
        album_artist="指尖笑",
        album="人間驚鴻宴",
        year=2026,
        track_no=3,
        track_total=10,
        genre="流行",
        cover_url=None,
        duration=207,
        source_url=None,
    )
    base.update(over)
    return TrackMeta(**base)


def test_jpeg_dimensions(jpeg_bytes):
    assert jpeg_dimensions(jpeg_bytes) == (64, 48)


def test_jpeg_dimensions_on_garbage():
    assert jpeg_dimensions(b"not a jpeg") == (0, 0)


def test_write_m4a_tags(m4a_file, jpeg_bytes):
    write_tags(m4a_file, _meta(), cover=jpeg_bytes)
    tags = MP4(m4a_file)
    assert tags["\xa9nam"] == ["人間驚鴻宴"]
    assert tags["\xa9ART"] == ["指尖笑; 另一位"]
    assert tags["aART"] == ["指尖笑"]
    assert tags["\xa9alb"] == ["人間驚鴻宴"]
    assert tags["\xa9day"] == ["2026"]
    assert tags["trkn"] == [(3, 10)]
    assert tags["\xa9gen"] == ["流行"]
    assert bytes(tags["covr"][0]) == jpeg_bytes


def test_write_opus_tags(opus_file, jpeg_bytes):
    write_tags(opus_file, _meta(), cover=jpeg_bytes)
    tags = OggOpus(opus_file)
    assert tags["TITLE"] == ["人間驚鴻宴"]
    assert tags["ARTIST"] == ["指尖笑", "另一位"]
    assert tags["ALBUMARTIST"] == ["指尖笑"]
    assert tags["ALBUM"] == ["人間驚鴻宴"]
    assert tags["DATE"] == ["2026"]
    assert tags["TRACKNUMBER"] == ["3"]
    assert tags["TOTALTRACKS"] == ["10"]
    assert tags["GENRE"] == ["流行"]
    picture = Picture(base64.b64decode(tags["METADATA_BLOCK_PICTURE"][0]))
    assert picture.data == jpeg_bytes
    assert picture.mime == "image/jpeg"
    assert (picture.width, picture.height) == (64, 48)


def test_write_tags_without_cover(m4a_file):
    write_tags(m4a_file, _meta(), cover=None)
    assert "covr" not in MP4(m4a_file)


def test_optional_fields_omitted_when_none(m4a_file):
    write_tags(m4a_file, _meta(year=None, genre=None, track_no=None, track_total=None))
    tags = MP4(m4a_file)
    assert "\xa9day" not in tags
    assert "\xa9gen" not in tags
    assert "trkn" not in tags


def test_track_number_without_total(opus_file):
    write_tags(opus_file, _meta(track_total=None))
    tags = OggOpus(opus_file)
    assert tags["TRACKNUMBER"] == ["3"]
    assert "TOTALTRACKS" not in tags


def test_emoji_and_cjk_survive_roundtrip(opus_file):
    write_tags(opus_file, _meta(title="測試 🎵 曲"))
    assert OggOpus(opus_file)["TITLE"] == ["測試 🎵 曲"]


def test_unsupported_extension(tmp_path):
    path = tmp_path / "x.flac"
    path.write_bytes(b"x")
    with pytest.raises(UnsupportedFormatError):
        write_tags(path, _meta())
