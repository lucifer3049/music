from app.models import Candidate, SourceTrack, TrackMeta


def _meta(**over) -> TrackMeta:
    base = dict(
        title="人間驚鴻宴",
        artists=("指尖笑",),
        album_artist="指尖笑",
        album="人間驚鴻宴",
        year=2026,
        track_no=1,
        track_total=1,
        genre=None,
        cover_url=None,
        duration=207,
        source_url="https://www.kkbox.com/tw/tc/song/abc",
    )
    base.update(over)
    return TrackMeta(**base)


def test_display_artists_joins_multiple():
    meta = _meta(artists=("指尖笑", "某某"))
    assert meta.display_artists == "指尖笑; 某某"


def test_display_artists_single():
    assert _meta().display_artists == "指尖笑"


def test_trackmeta_is_hashable_and_frozen():
    assert hash(_meta()) == hash(_meta())


def test_source_track_holds_raw_title():
    st = SourceTrack(
        video_id="abc123",
        url="https://music.youtube.com/watch?v=abc123",
        raw_title="【Official MV】指尖笑 - 人間驚鴻宴",
        duration=207,
        artist=None,
        track=None,
        album=None,
        release_year=None,
    )
    assert st.video_id == "abc123"


def test_candidate_carries_score():
    assert Candidate(meta=_meta(), score=0.95).score == 0.95
