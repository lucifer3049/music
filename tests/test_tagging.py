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


def test_write_m4a_clears_fields_the_metadata_does_not_have(m4a_file, jpeg_bytes):
    """meta 沒有的欄位要清掉，不能留著檔案裡原本的值。

    yt-dlp 抓下來的 m4a 帶著 YouTube 自己的標籤。實例：阿YueYue／云翳之上
    下載後年份是 2026（YouTube 的上傳年份），而 MusicBrainz 查無此曲、
    release_year 又是荒謬的 1060 被消毒成 None——結果就是我們明知道「沒有
    可信年份」，檔案裡卻留著一個看起來煞有介事的錯年份。
    TrackMeta 是這個檔案標籤的完整意圖，None 代表「不該有值」，不是「不要管」。
    """
    write_tags(m4a_file, _meta(), cover=jpeg_bytes)
    assert "\xa9day" in MP4(m4a_file)

    write_tags(m4a_file, _meta(year=None, track_no=None, genre=None), cover=None)
    tags = MP4(m4a_file)
    assert "\xa9day" not in tags
    assert "trkn" not in tags
    assert "\xa9gen" not in tags
    assert "covr" not in tags
    # 有值的欄位照常寫入
    assert tags["\xa9nam"] == ["人間驚鴻宴"]


def test_write_opus_clears_fields_the_metadata_does_not_have(opus_file, jpeg_bytes):
    write_tags(opus_file, _meta(), cover=jpeg_bytes)
    assert "date" in OggOpus(opus_file)

    write_tags(
        opus_file,
        _meta(year=None, track_no=None, track_total=None, genre=None),
        cover=None,
    )
    tags = OggOpus(opus_file)
    assert "date" not in tags
    assert "tracknumber" not in tags
    assert "totaltracks" not in tags
    assert "genre" not in tags
    assert "metadata_block_picture" not in tags
    assert tags["TITLE"] == ["人間驚鴻宴"]


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
