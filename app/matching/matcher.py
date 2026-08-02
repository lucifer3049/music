"""標題正規化與候選評分。純函式，無 IO，是本專案最該被測透的部分。"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable

from rapidfuzz import fuzz

from app.models import Candidate, SourceTrack, TrackMeta

HIGH_CONFIDENCE = 0.92

# 括號內若整段是雜訊詞就整段丟掉，否則只脫括號保留內容（例如 feat. 資訊）
_NOISE_WORDS = (
    "official", "officialmv", "mv", "m/v", "music video", "audio",
    "lyric", "lyrics", "lyric video", "visualizer",
    "hd", "hq", "4k", "1080p", "720p",
    "高音質", "官方", "官方版", "完整版", "動態歌詞", "純享版",
)
_BRACKETS = re.compile(r"[【\[(（〔]([^】\])）〕]*)[】\])）〕]")
_TITLE_SEP = re.compile(r"\s*[-–—－]\s*")
_WS = re.compile(r"\s+")


def _is_noise(inner: str) -> bool:
    stripped = _WS.sub(" ", inner).strip().lower()
    if not stripped:
        return True
    return all(part.strip() in _NOISE_WORDS for part in stripped.split("/") if part.strip())


def normalize_title(raw: str) -> str:
    """把 YouTube 標題洗成可比對的形式。

    括號內若全是宣傳雜訊就整段移除；否則只脫掉括號、保留內容，
    因為 (feat. X) 這類資訊對比對有用。
    """
    text = unicodedata.normalize("NFKC", raw)
    text = _BRACKETS.sub(lambda m: "" if _is_noise(m.group(1)) else f" {m.group(1)} ", text)
    tokens = [t for t in _WS.split(text) if t and t.lower() not in _NOISE_WORDS]
    return _WS.sub(" ", " ".join(tokens)).strip().lower()


def split_title(raw: str) -> tuple[str | None, str]:
    """從 `演出者 - 歌名` 形式拆出兩段；拆不出來時演出者回 None。"""
    normalized = normalize_title(raw)
    parts = _TITLE_SEP.split(normalized, maxsplit=1)
    if len(parts) == 2 and parts[0] and parts[1]:
        return parts[0].strip(), parts[1].strip()
    return None, normalized


def _ratio(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    return fuzz.token_set_ratio(a, b) / 100.0


def _duration_score(a: int | None, b: int | None) -> float | None:
    """時長吻合度。任一邊缺值回 None，代表這項不參與評分。

    規格書只定義「±3 秒內滿分，線性衰減」，沒有規定衰減終點落在哪裡；
    這裡選擇 30 秒歸零是實作選擇，不是規格書規定的值。
    """
    if a is None or b is None:
        return None
    diff = abs(a - b)
    if diff <= 3:
        return 1.0
    if diff >= 30:
        return 0.0
    return 1.0 - (diff - 3) / 27.0


def score_candidate(source: SourceTrack, meta: TrackMeta) -> float:
    """加權分數，範圍 0.0–1.0。

    權重依規格書公式：0.5 × 歌名 + 0.3 × 演出者 + 0.2 × 時長。
    這代表歌名完全不同、但演出者與時長都精準吻合時，加權和上限為 0.3 + 0.2 = 0.5——
    這是此權重下加權和的固有上限，不是缺陷。真正有行為意義的判斷界線是
    HIGH_CONFIDENCE（0.92），與這個上限有足夠差距，不會被誤判為可疑匹配。
    時長缺值時該項不計分，其餘權重按比例放大，避免無時長資料就永遠達不到門檻。
    """
    if source.track:
        title_guess = normalize_title(source.track)
        if source.artist:
            artist_guess = normalize_title(source.artist)
        else:
            # yt-dlp 常給出「有 track、無 artist」的殘缺結構化欄位；
            # 先用 raw_title 拆一次找回演出者，避免直接拿整段（含歌名字詞的）
            # 髒標題去跟候選的演出者比對。
            artist_guess, _ = split_title(source.raw_title)
    else:
        artist_guess, title_guess = split_title(source.raw_title)

    title_score = _ratio(title_guess, normalize_title(meta.title))

    candidate_artists = normalize_title(" ".join(meta.artists))
    if artist_guess:
        artist_score = _ratio(artist_guess, candidate_artists)
    else:
        # 沒拆出演出者時，用整串原始標題對演出者做寬鬆比對
        artist_score = _ratio(normalize_title(source.raw_title), candidate_artists)

    weighted = [(title_score, 0.5), (artist_score, 0.3)]
    dur = _duration_score(source.duration, meta.duration)
    if dur is not None:
        weighted.append((dur, 0.2))

    total_weight = sum(w for _, w in weighted)
    return sum(s * w for s, w in weighted) / total_weight


def rank_candidates(
    source: SourceTrack, metas: Iterable[TrackMeta], *, limit: int = 3
) -> list[Candidate]:
    scored = [Candidate(meta=m, score=score_candidate(source, m)) for m in metas]
    scored.sort(key=lambda c: c.score, reverse=True)
    return scored[:limit]
