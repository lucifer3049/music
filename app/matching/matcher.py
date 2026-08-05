"""標題正規化與候選評分。純函式，無 IO，是本專案最該被測透的部分。"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable

import zhconv
from rapidfuzz import fuzz

from app.models import Candidate, SourceTrack, TrackMeta

HIGH_CONFIDENCE = 0.92

# 低於此分數的候選不足以當預設答案。呼叫端（Pipeline._match_one）據此決定要不要
# 把 YouTube 自己的資料排到前面 —— 網頁預設選中第一個候選，順序等於預設答案。
MIN_ACCEPTABLE_SCORE = 0.75

# 兩道硬否決的界線。這兩項不是「扣分」而是「矛盾」：對不上就不可能是同一首歌，
# 留在候選清單裡只會靠字面相近爬到第一名（見 rank_candidates）。
#
# 時長界線刻意等於 _duration_score() 的歸零點，不是另訂一個更嚴的值：同一首歌
# 在不同發行版之間差十幾二十秒是常態（實測 Queen 的 Bohemian Rhapsody，
# MusicBrainz 前三筆是 338／325／130 秒），15 秒的界線會把整組正確候選一起殺掉。
# 否決的對象是「時長已經零分」的候選 —— 那不是版本差異，是別首歌。
DURATION_ZERO_DIFF_SECONDS = 30
MIN_ARTIST_SCORE = 0.5

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


def han_variants(text: str) -> tuple[str, ...]:
    """原字串加上它的簡繁對譯，去重後保留原字串在最前。

    給呼叫端逐個試查用（見 Pipeline._match_one）：YouTube 標「云翳之上」而
    MusicBrainz 收錄「雲翳之上」時，原字體查不到、另一種字體查得到。
    純 ASCII 或本來就沒有簡繁差異的字串只會回一個元素 —— 每多一個變體就是
    多一次受 1.1 秒節流的查詢，不能白付。
    """
    out = [text]
    for locale in ("zh-hant", "zh-hans"):
        converted = zhconv.convert(text, locale)
        if converted not in out:
            out.append(converted)
    return tuple(out)


def _fold_han(text: str) -> str:
    """比對前把簡繁收斂成同一種字體。

    只用在 _ratio() 內部，不能滲進 normalize_title() —— 後者的輸出會經
    split_title() 流進 pipeline.fallback_meta()，最後成為檔案標籤與資料夾
    名稱；在那裡折疊等於把使用者的收藏改寫成另一種字體。
    """
    return zhconv.convert(text, "zh-hans")


def _ratio(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    return fuzz.token_set_ratio(_fold_han(a), _fold_han(b)) / 100.0


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
    if diff >= DURATION_ZERO_DIFF_SECONDS:
        return 0.0
    return 1.0 - (diff - 3) / (DURATION_ZERO_DIFF_SECONDS - 3)


def _guess_fields(source: SourceTrack) -> tuple[str | None, str]:
    """從 SourceTrack 推出要拿去比對的（演出者, 歌名）。"""
    if source.track:
        title_guess = normalize_title(source.track)
        if source.artist:
            return normalize_title(source.artist), title_guess
        # yt-dlp 常給出「有 track、無 artist」的殘缺結構化欄位；
        # 先用 raw_title 拆一次找回演出者，避免直接拿整段（含歌名字詞的）
        # 髒標題去跟候選的演出者比對。
        artist_guess, _ = split_title(source.raw_title)
        return artist_guess, title_guess
    artist_guess, title_guess = split_title(source.raw_title)
    return artist_guess, title_guess


def is_plausible(source: SourceTrack, meta: TrackMeta) -> bool:
    """候選有沒有資格進清單。回 False 代表兩者互相矛盾，不是分數低而已。

    這兩道否決存在的理由：加權分數對「字面相近、實際無關」的候選毫無抵抗力。
    實例 —— 云翳之上（阿YueYue，277 秒）在 MusicBrainz 查無此曲，自由查詢
    撈回「云端之上」（李安健，120 秒），四個字中了三個，分數 0.375 拿下第一名，
    而網頁預設選中第一個候選。

      時長：兩邊都有值且時長分數已歸零（差距達 DURATION_ZERO_DIFF_SECONDS）
            才否決。任一邊缺值不算矛盾，不能拿來否決。
      演出者：只在 yt-dlp 給了結構化 artist 時才套用 —— 那是 Topic 頻道由
            發行商上傳的欄位，可信度足以當否決依據；從髒標題猜出來的演出者
            不夠格，猜錯就會把正確候選一起殺掉。
            已知代價：MusicBrainz 對部分亞洲藝人記的是羅馬字（ヨルシカ 對
            Yorushika），這種候選會被誤殺、退回 YouTube 自身資料。標籤因此
            少了專輯／年份／封面，但不會是「別人的歌」——寧可少，不要錯。
    """
    if source.duration is not None and meta.duration is not None:
        if abs(source.duration - meta.duration) >= DURATION_ZERO_DIFF_SECONDS:
            return False
    if source.artist:
        artist_score = _ratio(normalize_title(source.artist), normalize_title(" ".join(meta.artists)))
        if artist_score < MIN_ARTIST_SCORE:
            return False
    return True


def score_candidate(source: SourceTrack, meta: TrackMeta) -> float:
    """加權分數，範圍 0.0–1.0。

    權重依規格書公式：0.5 × 歌名 + 0.3 × 演出者 + 0.2 × 時長。
    這代表歌名完全不同、但演出者與時長都精準吻合時，加權和上限為 0.3 + 0.2 = 0.5——
    這是此權重下加權和的固有上限，不是缺陷。真正有行為意義的判斷界線是
    HIGH_CONFIDENCE（0.92），與這個上限有足夠差距，不會被誤判為可疑匹配。
    時長缺值時該項不計分，其餘權重按比例放大，避免無時長資料就永遠達不到門檻。
    """
    artist_guess, title_guess = _guess_fields(source)

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
    """排名候選。與來源互相矛盾的候選在計分前先剔除（見 is_plausible）。"""
    scored = [
        Candidate(meta=m, score=score_candidate(source, m))
        for m in metas
        if is_plausible(source, m)
    ]
    scored.sort(key=lambda c: c.score, reverse=True)
    return scored[:limit]
