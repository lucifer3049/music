import pytest

from app.matching.matcher import (
    HIGH_CONFIDENCE,
    normalize_title,
    rank_candidates,
    score_candidate,
    split_title,
)
from app.models import SourceTrack, TrackMeta


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("【Official MV】人間驚鴻宴", "人間驚鴻宴"),
        ("人間驚鴻宴 (Official Audio)", "人間驚鴻宴"),
        ("人間驚鴻宴 [4K][高音質]", "人間驚鴻宴"),
        ("人間驚鴻宴 (Lyric Video)", "人間驚鴻宴"),
        ("人間驚鴻宴 HD", "人間驚鴻宴"),
        ("ＡＢＣ", "abc"),
        ("  多餘   空白  ", "多餘 空白"),
        ("人間驚鴻宴", "人間驚鴻宴"),
    ],
)
def test_normalize_title(raw, expected):
    assert normalize_title(raw) == expected


def test_normalize_title_keeps_feat_content():
    # feat. 資訊是有效訊號，只脫掉括號不丟內容
    assert normalize_title("歌名 (feat. 某某)") == "歌名 feat. 某某"


@pytest.mark.parametrize(
    "raw,artist,title",
    [
        ("指尖笑 - 人間驚鴻宴", "指尖笑", "人間驚鴻宴"),
        ("指尖笑－人間驚鴻宴", "指尖笑", "人間驚鴻宴"),
        ("【MV】指尖笑 - 人間驚鴻宴", "指尖笑", "人間驚鴻宴"),
        ("人間驚鴻宴", None, "人間驚鴻宴"),
    ],
)
def test_split_title(raw, artist, title):
    assert split_title(raw) == (artist, title)


def _source(**over) -> SourceTrack:
    base = dict(
        video_id="v1",
        url="https://music.youtube.com/watch?v=v1",
        raw_title="【Official MV】指尖笑 - 人間驚鴻宴",
        duration=207,
        artist=None,
        track=None,
        album=None,
        release_year=None,
    )
    base.update(over)
    return SourceTrack(**base)


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
        source_url="https://www.kkbox.com/tw/tc/song/a",
    )
    base.update(over)
    return TrackMeta(**base)


def test_exact_match_scores_high():
    assert score_candidate(_source(), _meta()) >= HIGH_CONFIDENCE


def test_structured_fields_preferred_over_raw_title():
    # yt-dlp 給了乾淨欄位時，髒標題不應拖低分數
    source = _source(raw_title="随便什麼亂七八糟的標題", artist="指尖笑", track="人間驚鴻宴")
    assert score_candidate(source, _meta()) >= HIGH_CONFIDENCE


def test_wrong_title_scores_low():
    """歌名全錯時分數必須遠低於自動通過門檻。

    演出者與時長都吻合仍會拿到 0.5（0.3 + 0.2），這是加權和的固有上限，
    不是缺陷 —— 真正有行為意義的界線是 HIGH_CONFIDENCE，離它很遠。
    """
    score = score_candidate(_source(), _meta(title="完全不同的歌"))
    assert score < HIGH_CONFIDENCE - 0.3


def test_track_without_artist_recovers_artist_from_title():
    """artist 缺值時要從標題還原演出者，而不是拿整串標題去比對演出者。

    wrong_artist 刻意取用歌名本身：舊的 fallback 拿整串 raw_title 比對演出者，
    會把歌名 token 誤判成演出者吻合，讓這個候選拿到滿分。
    """
    source = _source(raw_title="指尖笑 - 人間驚鴻宴", track="人間驚鴻宴", artist=None)
    good = score_candidate(source, _meta())
    wrong_artist = score_candidate(
        source, _meta(artists=("人間驚鴻宴",), album_artist="人間驚鴻宴")
    )
    assert good >= HIGH_CONFIDENCE
    assert good > wrong_artist


def test_duration_mismatch_lowers_score():
    close = score_candidate(_source(duration=207), _meta(duration=209))
    far = score_candidate(_source(duration=207), _meta(duration=400))
    assert close > far


def test_missing_duration_is_neutral_not_penalized():
    scored = score_candidate(_source(duration=None), _meta())
    assert scored >= HIGH_CONFIDENCE


def test_rank_candidates_sorts_and_limits():
    metas = [
        _meta(title="完全不同的歌"),
        _meta(),
        _meta(title="人間驚鴻宴 (Live)"),
        _meta(title="另一首"),
    ]
    ranked = rank_candidates(_source(), metas, limit=3)
    assert len(ranked) == 3
    assert ranked[0].meta.title == "人間驚鴻宴"
    assert ranked[0].score >= ranked[1].score >= ranked[2].score


def test_rank_candidates_empty_input():
    assert rank_candidates(_source(), []) == []
