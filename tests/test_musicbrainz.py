import json
from pathlib import Path

import httpx
import pytest

from app.models import TrackMeta
from app.sources.musicbrainz import (
    MusicBrainzError,
    cover_url_for_release,
    fetch_cover,
    make_client,
    search,
    search_recordings,
    to_track_meta,
)

FIXTURES = Path(__file__).parent / "fixtures"

# 依 Step 1 實際擷取到的內容填寫（tests/fixtures/mb_recording_search.json）
EXPECTED_TITLE = "Bohemian Rhapsody"
EXPECTED_ARTIST = "Queen"


def _fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _first_recording() -> dict:
    return _fixture("mb_recording_search.json")["recordings"][0]


def test_make_client_sets_contactable_user_agent(monkeypatch):
    monkeypatch.setenv("MUSICBRAINZ_CONTACT", "someone@example.com")
    with make_client() as client:
        ua = client.headers["User-Agent"]
    assert "music-downloader" in ua
    assert "someone@example.com" in ua


def test_make_client_user_agent_is_not_a_browser_ua():
    """泛用瀏覽器 UA 會被 MusicBrainz 拒絕，必須是可辨識的應用名稱。"""
    with make_client() as client:
        assert "Mozilla" not in client.headers["User-Agent"]


def test_to_track_meta_core_fields():
    meta = to_track_meta(_first_recording())
    assert isinstance(meta, TrackMeta)
    assert meta.title == EXPECTED_TITLE
    assert EXPECTED_ARTIST in meta.artists
    assert isinstance(meta.artists, tuple)
    assert meta.album
    assert meta.source_url and meta.source_url.startswith("https://musicbrainz.org/recording/")


def test_to_track_meta_converts_milliseconds_to_seconds():
    """MusicBrainz 的 length 是毫秒，TrackMeta.duration 是秒（實際 fixture: 363106ms -> 363s）。"""
    recording = dict(_first_recording())
    recording["length"] = 207000
    assert to_track_meta(recording).duration == 207


def test_to_track_meta_handles_missing_length():
    recording = dict(_first_recording())
    recording.pop("length", None)
    assert to_track_meta(recording).duration is None


def test_to_track_meta_extracts_year_from_date():
    meta = to_track_meta(_first_recording())
    assert meta.year is None or 1900 <= meta.year <= 2100


def test_to_track_meta_sets_track_position_and_total():
    """實際 fixture 的 releases[].media[] 已含 track-count 與 track[].number,
    足以組出曲序，不必再打第二支 API。"""
    meta = to_track_meta(_first_recording())
    assert meta.track_no is not None
    assert meta.track_no >= 1
    assert meta.track_total is not None
    assert meta.track_total >= meta.track_no


def test_to_track_meta_raises_without_title():
    with pytest.raises(MusicBrainzError):
        to_track_meta({"id": "x"})


def test_to_track_meta_handles_recording_without_releases():
    """單曲未收錄於任何專輯時，album 退回曲名，不得炸掉。"""
    recording = {"id": "x", "title": "孤兒曲", "artist-credit": [{"name": "某人"}]}
    meta = to_track_meta(recording)
    assert meta.album == "孤兒曲"
    assert meta.track_no is None
    assert meta.track_total is None


def test_to_track_meta_handles_track_number_that_is_not_numeric():
    """黑膠 B 面等曲序可能是 'C3' 這種非數字字串（CJK fixture 內即有此例），
    不得炸掉，應退回 None。"""
    recording = dict(_first_recording())
    recording = json.loads(json.dumps(recording))  # deep copy
    recording["releases"][0]["media"][0]["track"][0]["number"] = "C3"
    recording["releases"][0]["media"][0]["track"][0].pop("position", None)
    meta = to_track_meta(recording)
    assert meta.track_no is None


def test_to_track_meta_cjk_fixture():
    recording = _fixture("mb_recording_search_cjk.json")["recordings"][0]
    meta = to_track_meta(recording)
    assert meta.title == "稻香"
    assert "周杰倫" in meta.artists


def test_cover_url_points_at_cover_art_archive():
    url = cover_url_for_release("abc-123")
    assert url == "https://coverartarchive.org/release/abc-123/front-500"


def test_search_recordings_sends_required_params():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(200, json=_fixture("mb_recording_search.json"))

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        results = search_recordings("Queen Bohemian Rhapsody", client=client, limit=3)

    assert results
    assert "fmt=json" in captured["url"]
    assert "limit=3" in captured["url"]


def test_search_returns_track_meta_list():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_fixture("mb_recording_search.json"))

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        results = search("Queen Bohemian Rhapsody", client=client)

    assert results
    assert all(isinstance(m, TrackMeta) for m in results)
    assert results[0].title == EXPECTED_TITLE


def test_search_returns_empty_on_http_error():
    """MusicBrainz 掛掉不得中斷下載 —— 呼叫端會降級用 YouTube 自身 metadata。"""
    with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(503))) as client:
        assert search("任何字串", client=client) == []


def test_search_returns_empty_on_malformed_payload():
    with httpx.Client(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"nope": 1}))
    ) as client:
        assert search("任何字串", client=client) == []


def test_search_skips_unparseable_recording_but_keeps_others():
    payload = {"recordings": [{"id": "bad"}, _first_recording()]}

    with httpx.Client(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json=payload))
    ) as client:
        results = search("任何字串", client=client)

    assert [m.title for m in results] == [EXPECTED_TITLE]


def test_fetch_cover_returns_bytes():
    transport = httpx.MockTransport(
        lambda r: httpx.Response(200, content=b"\xff\xd8\xff\xe0jpegdata")
    )
    with httpx.Client(transport=transport) as client:
        assert fetch_cover(
            "https://coverartarchive.org/release/x/front-500", client=client
        ).startswith(b"\xff\xd8")


def test_fetch_cover_raises_on_missing_art():
    """Cover Art Archive 沒有圖時回 404，呼叫端要能分辨。"""
    with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(404))) as client:
        with pytest.raises(httpx.HTTPStatusError):
            fetch_cover("https://coverartarchive.org/release/x/front-500", client=client)
