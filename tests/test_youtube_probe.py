import pytest

from app.sources.youtube import (
    ProbeError,
    UnsupportedUrlError,
    UrlKind,
    classify_url,
    probe,
    to_source_track,
)


@pytest.mark.parametrize(
    "url,kind",
    [
        ("https://music.youtube.com/watch?v=abc123", UrlKind.SINGLE),
        ("https://music.youtube.com/watch?v=abc123&list=OLAK5uy_x", UrlKind.SINGLE),
        ("https://www.youtube.com/watch?v=abc123", UrlKind.SINGLE),
        ("https://youtu.be/abc123", UrlKind.SINGLE),
        ("https://music.youtube.com/playlist?list=OLAK5uy_abc", UrlKind.ALBUM),
        ("https://music.youtube.com/playlist?list=PLabc123", UrlKind.PLAYLIST),
        ("https://music.youtube.com/playlist?list=RDCLAK5uy_abc", UrlKind.PLAYLIST),
    ],
)
def test_classify_url(url, kind):
    assert classify_url(url) == kind


@pytest.mark.parametrize(
    "url",
    [
        "https://www.kkbox.com/tw/tc/song/abc",
        "https://example.com/foo",
        "not a url",
        "",
    ],
)
def test_classify_url_rejects_unsupported(url):
    with pytest.raises(UnsupportedUrlError):
        classify_url(url)


def test_to_source_track_prefers_structured_fields():
    entry = {
        "id": "abc123",
        "title": "【MV】指尖笑 - 人間驚鴻宴",
        "duration": 207,
        "artist": "指尖笑",
        "track": "人間驚鴻宴",
        "album": "人間驚鴻宴",
        "release_year": 2026,
    }
    st = to_source_track(entry)
    assert st.video_id == "abc123"
    assert st.url == "https://music.youtube.com/watch?v=abc123"
    assert st.track == "人間驚鴻宴"
    assert st.artist == "指尖笑"
    assert st.release_year == 2026


def test_to_source_track_handles_missing_fields():
    st = to_source_track({"id": "x", "title": "某標題"})
    assert st.track is None
    assert st.artist is None
    assert st.duration is None
    assert st.raw_title == "某標題"


def test_to_source_track_joins_artists_list():
    st = to_source_track({"id": "x", "title": "t", "artists": ["A", "B"]})
    assert st.artist == "A, B"


class _FakeYDL:
    """模擬 yt_dlp.YoutubeDL 的 context manager 介面。"""

    def __init__(self, info):
        self._info = info

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def extract_info(self, url, download=False):
        return self._info


def test_probe_single_returns_one_track():
    info = {"id": "abc", "title": "某歌", "duration": 100}
    tracks = probe(
        "https://music.youtube.com/watch?v=abc",
        ydl_factory=lambda opts: _FakeYDL(info),
    )
    assert len(tracks) == 1
    assert tracks[0].video_id == "abc"


def test_probe_album_returns_all_entries_with_order():
    info = {
        "_type": "playlist",
        "title": "某專輯",
        "entries": [
            {"id": "a", "title": "第一首", "duration": 100},
            {"id": "b", "title": "第二首", "duration": 200},
        ],
    }
    tracks = probe(
        "https://music.youtube.com/playlist?list=OLAK5uy_x",
        ydl_factory=lambda opts: _FakeYDL(info),
    )
    assert [t.video_id for t in tracks] == ["a", "b"]


def test_probe_skips_none_entries():
    """已下架曲目在 playlist 會是 None，不能讓整批爆掉 —— 清單裡壞掉一首
    不能拖垮整批，這是規格明訂的行為。"""
    info = {
        "_type": "playlist",
        "entries": [None, {"id": "b", "title": "還在的歌"}],
    }
    tracks = probe(
        "https://music.youtube.com/playlist?list=OLAK5uy_x",
        ydl_factory=lambda opts: _FakeYDL(info),
    )
    assert [t.video_id for t in tracks] == ["b"]


def test_probe_raises_when_nothing_extracted():
    """單曲整個抽不到時要帶著原因拋出，而不是靜靜回空清單。"""
    class _DeadYDL:
        def __enter__(self):
            return self
        def __exit__(self, *exc):
            return False
        def extract_info(self, url, download=False):
            return None

    with pytest.raises(ProbeError):
        probe("https://music.youtube.com/watch?v=gone", ydl_factory=lambda opts: _DeadYDL())


def test_probe_error_carries_ytdlp_message():
    """yt-dlp 報的原因要出現在例外訊息裡，Task 10 才有東西可存。"""
    class _LoggingYDL:
        def __init__(self, opts):
            self._logger = opts.get("logger")
        def __enter__(self):
            return self
        def __exit__(self, *exc):
            return False
        def extract_info(self, url, download=False):
            if self._logger is not None:
                self._logger.error("ERROR: Video unavailable. This video is private")
            return None

    with pytest.raises(ProbeError, match="private"):
        probe("https://music.youtube.com/watch?v=gone", ydl_factory=_LoggingYDL)


class _OptsCapturingYDL:
    """記下 probe() 實際交給 yt-dlp 的 opts。"""

    captured: dict = {}

    def __init__(self, opts):
        type(self).captured = opts

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def extract_info(self, url, download=False):
        return {"id": "abc", "title": "某歌", "duration": 100}


def test_probe_sets_noplaylist_for_single_url_carrying_a_list_id():
    """從 YouTube Music 複製單曲網址會夾帶 &list=OLAK5uy_...。

    不設 noplaylist 的話 yt-dlp 會展開整張專輯逐首抓 metadata：實測 39 首的
    專輯要 39.6 秒，而不是 1.8 秒。
    """
    probe(
        "https://music.youtube.com/watch?v=abc&list=OLAK5uy_x",
        ydl_factory=_OptsCapturingYDL,
    )
    assert _OptsCapturingYDL.captured["noplaylist"] is True


def test_probe_does_not_set_noplaylist_for_album_url():
    """專輯網址就是要整張，不能把清單關掉。"""
    probe(
        "https://music.youtube.com/playlist?list=OLAK5uy_x",
        ydl_factory=_OptsCapturingYDL,
    )
    assert _OptsCapturingYDL.captured["noplaylist"] is False


def test_probe_does_not_set_noplaylist_for_playlist_url():
    probe(
        "https://music.youtube.com/playlist?list=PLabc",
        ydl_factory=_OptsCapturingYDL,
    )
    assert _OptsCapturingYDL.captured["noplaylist"] is False
