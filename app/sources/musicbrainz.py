"""MusicBrainz 公開 metadata 來源。

僅讀取公開音樂資料庫的曲目資訊與封面圖網址，用於補齊本地檔案標籤。
不涉及任何音訊取得或 DRM 處理 —— 音訊一律來自 YouTube Music。

MusicBrainz 的兩條硬性規定：
  1. User-Agent 必須可辨識並帶聯絡方式，泛用瀏覽器 UA 會收到 403
  2. 匿名存取每秒至多 1 次請求

recording.length 單位是毫秒，TrackMeta.duration 是秒。

實測結論（Step 1，2026-08-03，查詢 "Bohemian Rhapsody"/Queen 與 "稻香"/周杰倫）：
  recording 搜尋端點回應的 releases[] 已經內嵌 media[]，而且 media[].track[] 只含
  「符合這次搜尋的那一首」單曲條目（不是整張專輯的曲目清單），所以：
    - media[0]["track-count"] 就是「這首歌所在那張碟片」的總曲數
    - media[0]["track"][0]["number"] 就是這首歌在該碟片內的曲序
  一次 recording 搜尋即可組出完整 TrackMeta，不必再打 release 端點第二支 API。
  另外要注意：
    - release["track-count"] 是整個 release（可能跨多張碟）的總曲數，並非本曲
      所在碟片的曲數，不可誤用 —— 曲序要用 media[0]["track-count"]。
    - track["number"] 是字串，不一定是數字（黑膠可能是 "C3" 這種側別+序號），
      沒有獨立的 track["position"] 欄位；非數字時退回 None，不可炸掉。

實測結論（Genre 調查，詳見 .superpowers/sdd/genre-report.md）：
  recording 搜尋端點（不論帶不帶 inc）與 recording／release lookup 端點幾乎都拿不到
  genre 資料；真正有資料的是 release-group 這一層。一次 release lookup、
  `inc=genres+release-groups` 即可同時拿到 release 與其內嵌 release-group 的 genres，
  見 fetch_genre()。這一趟查詢有實質延遲成本（節流 1.1 秒／次），因此故意不在
  search() 比對階段對每個候選都查，只在 Pipeline.finalize() 對使用者確認的那一首
  查一次（見 app/pipeline.py 的 Pipeline._fetch_genre()）。
"""

from __future__ import annotations

import os
import re
import threading
import time

import httpx

from app.models import TrackMeta

WS_BASE = "https://musicbrainz.org/ws/2"
COVER_ART_BASE = "https://coverartarchive.org/release"
APP_VERSION = "0.1.0"
DEFAULT_CONTACT = "set MUSICBRAINZ_CONTACT to your email"
# MusicBrainz 匿名速率上限：每秒 1 次
MIN_REQUEST_INTERVAL_SECONDS = 1.1
MAX_SEARCH_RESULTS = 3

_rate_lock = threading.Lock()
_last_request_at = 0.0


class MusicBrainzError(RuntimeError):
    """回應結構與預期不符，或缺少組成標籤的必要欄位。"""


def _user_agent() -> str:
    contact = os.environ.get("MUSICBRAINZ_CONTACT", DEFAULT_CONTACT)
    return f"music-downloader/{APP_VERSION} ( {contact} )"


def make_client() -> httpx.Client:
    return httpx.Client(
        headers={"User-Agent": _user_agent(), "Accept": "application/json"},
        follow_redirects=True,
        timeout=20.0,
    )


def _throttle() -> None:
    """強制請求間隔。多執行緒共用同一節流器。"""
    global _last_request_at
    with _rate_lock:
        elapsed = time.monotonic() - _last_request_at
        if elapsed < MIN_REQUEST_INTERVAL_SECONDS:
            time.sleep(MIN_REQUEST_INTERVAL_SECONDS - elapsed)
        _last_request_at = time.monotonic()


def _artist_names(credit) -> tuple[str, ...]:
    """把 artist-credit 陣列攤平成名字 tuple。"""
    if not isinstance(credit, list):
        return ()
    names = []
    for item in credit:
        if isinstance(item, dict):
            name = item.get("name") or (item.get("artist") or {}).get("name")
            if isinstance(name, str) and name.strip():
                names.append(name.strip())
    return tuple(names)


def _year_from_date(date: str | None) -> int | None:
    if not isinstance(date, str) or len(date) < 4 or not date[:4].isdigit():
        return None
    return int(date[:4])


def _pick_release(recording: dict) -> dict | None:
    """挑一張代表專輯。優先有日期者，其次第一張。"""
    releases = recording.get("releases")
    if not isinstance(releases, list) or not releases:
        return None
    dated = [r for r in releases if isinstance(r, dict) and r.get("date")]
    return (dated or releases)[0]


def _track_position(release: dict) -> tuple[int | None, int | None]:
    """從 release.media[0] 取出這首歌在所屬碟片內的曲序與該碟片總曲數。

    搜尋端點回傳的 media[].track[] 只含符合這次查詢的那一首，因此 track[0]
    就是本曲；media[0]["track-count"] 是「這張碟片」的總曲數（release 層級的
    track-count 是跨碟總數，不能拿來配對曲序，故意不用）。結構缺漏或曲序非
    數字（例如黑膠 "C3"）時回 (None, None) / (None, total)，不得炸掉。
    """
    media = release.get("media")
    if not isinstance(media, list) or not media:
        return (None, None)
    first = media[0]
    if not isinstance(first, dict):
        return (None, None)
    total = first.get("track-count")
    total = total if isinstance(total, int) else None

    tracks = first.get("track")
    position = None
    if isinstance(tracks, list) and tracks and isinstance(tracks[0], dict):
        raw_position = tracks[0].get("position")
        if raw_position is None:
            raw_position = tracks[0].get("number")
        if isinstance(raw_position, int):
            position = raw_position
        elif isinstance(raw_position, str) and raw_position.isdigit():
            position = int(raw_position)

    return (position, total)


def cover_url_for_release(release_mbid: str) -> str:
    return f"{COVER_ART_BASE}/{release_mbid}/front-500"


def release_mbid_for_genre_lookup(meta: TrackMeta) -> str | None:
    """從 TrackMeta.cover_url 反推 release MBID，供事後（Pipeline.finalize()）查 genre 用。

    genre 查詢決策（見 .superpowers/sdd/genre-report.md Step 2）：只在使用者確認候選、
    真的要下載那一首時才查，不在 search() 比對階段查（否則比對延遲乘上候選數）。但
    `TrackMeta` 不能新增欄位存 release MBID —— `app/jobs/store.py` 的 `_meta_from_dict()`
    對已存在資料庫的舊資料是 `TrackMeta(**data)`，沒有 dataclass 預設值，新增必填欄位
    會讓舊資料讀取直接炸掉、整個工作列表掛掉。

    好在 `cover_url` 本來就是 `cover_url_for_release()` 組出來的
    `f"{COVER_ART_BASE}/{release_mbid}/front-500"`，release MBID 已經原樣編在裡面，
    這裡只是照組成規則反向拆解，不是新增耦合。cover_url 為 None（單曲不屬於任何
    release）時同樣沒有 release-group 可查，回 None。
    """
    if not isinstance(meta.cover_url, str):
        return None
    prefix = f"{COVER_ART_BASE}/"
    suffix = "/front-500"
    if not (meta.cover_url.startswith(prefix) and meta.cover_url.endswith(suffix)):
        return None
    mbid = meta.cover_url[len(prefix) : -len(suffix)]
    return mbid or None


def _top_genre(genres) -> str | None:
    """把 MusicBrainz 的 genres[]（受控詞彙，帶投票數 count）收斂成單一字串。

    取 count 最高者：這是「大眾認知的主要類型」最直接的代理指標，比「取回應
    第一筆」（MusicBrainz 不保證陣列順序穩定）或「全部串接」（Windows 檔案總管
    Genre 欄位是單一字串式呈現，塞一長串逗號分隔的類型不利閱讀／篩選，且會讓
    tagging.writer 寫入的 `©gen` / `GENRE` 語意模糊）更合理。count 同分時用
    名稱字母序決一，避免同一首歌每次抓到的順序不同、寫入的字串跳來跳去。
    """
    if not isinstance(genres, list) or not genres:
        return None
    candidates: list[tuple[int, str]] = []
    for item in genres:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        if not isinstance(name, str) or not name.strip():
            continue
        count = item.get("count")
        count = count if isinstance(count, int) else 0
        candidates.append((count, name.strip()))
    if not candidates:
        return None
    candidates.sort(key=lambda pair: (-pair[0], pair[1]))
    return candidates[0][1]


def fetch_genre(release_mbid: str, *, client: httpx.Client) -> str | None:
    """查詢一首曲目所屬 release 的 genre，收斂成單一字串。

    實測結論（見 .superpowers/sdd/genre-report.md）：recording 與 release 這兩層幾乎
    沒人力標記 genre，即使帶 `inc=genres` 也常是空的；真正有資料的是 release-group
    這一層。一次 release lookup、`inc=genres+release-groups` 就能同時拿到 release 本身
    與其內嵌 release-group 的 genres（MusicBrainz 會把 `genres` inc 套用到回應裡出現的
    每個實體），不必為了 release-group 再多打一支 API。優先採 release-group 的結果，
    release 本身幾乎必空但仍當退路檢查一次。

    只使用 genres（受控詞彙），不退回 tags（自由標記）：實測樣本中沒有一個案例是
    「genres 空但 tags 有料」——recording／release 這兩層兩者一起空，release-group／
    artist 這兩層兩者一起有料，tags 沒有多覆蓋到 genres 覆蓋不到的案例，卻會多帶進
    非類型的雜訊詞（年代、心情、其他自由標籤）。

    只在 Pipeline.finalize() 對使用者確認的那一首呼叫，不在 search() 比對階段對每個
    候選呼叫（1.1 秒節流 × 最多 3 個候選 = 比對延遲變 4 倍，划不來換一個比對階段
    用不到的欄位）。

    行為比照 fetch_cover()：任何失敗（HTTP 錯誤、回應形狀不符）原樣冒出例外，由
    呼叫端（Pipeline._fetch_genre）決定要不要吞掉——genre 查詢失敗不該讓整首曲目
    下載失敗。
    """
    _throttle()
    response = client.get(
        f"{WS_BASE}/release/{release_mbid}",
        params={"fmt": "json", "inc": "genres+release-groups"},
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise MusicBrainzError("回應不是預期的物件結構")

    release_group = payload.get("release-group")
    genres = release_group.get("genres") if isinstance(release_group, dict) else None
    genre = _top_genre(genres)
    if genre is not None:
        return genre
    return _top_genre(payload.get("genres"))


def to_track_meta(recording: dict) -> TrackMeta:
    """把一筆 MusicBrainz recording 轉成 TrackMeta。"""
    title = recording.get("title")
    if not isinstance(title, str) or not title.strip():
        raise MusicBrainzError(f"recording 缺少 title：{recording.get('id')!r}")
    title = title.strip()

    artists = _artist_names(recording.get("artist-credit"))
    release = _pick_release(recording)

    if release is None:
        album = title
        album_artists: tuple[str, ...] = ()
        year = None
        track_no = track_total = None
        cover_url = None
    else:
        album = (release.get("title") or title).strip()
        album_artists = _artist_names(release.get("artist-credit"))
        year = _year_from_date(release.get("date")) or _year_from_date(
            recording.get("first-release-date")
        )
        track_no, track_total = _track_position(release)
        cover_url = cover_url_for_release(release["id"]) if release.get("id") else None

    length_ms = recording.get("length")
    duration = int(length_ms) // 1000 if isinstance(length_ms, (int, float)) else None

    return TrackMeta(
        title=title,
        artists=artists,
        album_artist=(album_artists or artists or ("",))[0],
        album=album,
        year=year,
        track_no=track_no,
        track_total=track_total,
        genre=None,
        cover_url=cover_url,
        duration=duration,
        source_url=f"https://musicbrainz.org/recording/{recording['id']}"
        if recording.get("id")
        else None,
    )


def _quote_phrase(value: str) -> str:
    """把字串包成 Lucene 片語。

    片語內只有反斜線與雙引號需要跳脫；其餘符號（`-`、`:`、`(` 等）在雙引號內
    本來就是字面值，多加跳脫反而會讓 MusicBrainz 搜不到含這些字元的歌名。
    """
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def build_recording_query(*, track: str | None, artist: str | None, raw_title: str) -> str:
    """組出 MusicBrainz 的 recording 搜尋語句。

    必須用欄位限定語法（`recording:"歌名" AND artist:"演出者"`），不能把歌名與
    演出者用空白串成一句就丟出去 —— 後者等於要 MusicBrainz 自己猜哪個詞是什麼。

    實測差異（ReoNa 的 Amore）：
      `ReoNa Amore`                              -> 前三筆全是「ピルグリム -ReoNa ver.-」
      `recording:"Amore" AND artist:"ReoNa"`     -> 第一筆就是 Amore

    欄位缺漏時逐級退讓：只有歌名就單查歌名，兩者皆無才退回原始標題的自由查詢。
    """
    if track and artist:
        return f"recording:{_quote_phrase(track)} AND artist:{_quote_phrase(artist)}"
    if track:
        return f"recording:{_quote_phrase(track)}"
    if artist:
        return f"recording:{_quote_phrase(raw_title)} AND artist:{_quote_phrase(artist)}"
    return raw_title


def search_recordings(
    query: str, *, client: httpx.Client, limit: int = MAX_SEARCH_RESULTS
) -> list[dict]:
    """呼叫 recording 搜尋端點，回傳原始 recording dict 清單。"""
    _throttle()
    response = client.get(
        f"{WS_BASE}/recording",
        params={"query": query, "fmt": "json", "limit": limit},
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise MusicBrainzError("回應不是預期的物件結構")
    recordings = payload.get("recordings")
    if not isinstance(recordings, list):
        raise MusicBrainzError("回應缺少 recordings 陣列")
    return recordings


def search(query: str, *, client: httpx.Client) -> list[TrackMeta]:
    """搜尋並轉成 TrackMeta 候選清單。

    任何網路或解析失敗都回空清單 —— 呼叫端會降級用 YouTube 自身 metadata，
    不該因為 MusicBrainz 出事就中斷下載。
    """
    try:
        recordings = search_recordings(query, client=client)
    except (httpx.HTTPError, MusicBrainzError, ValueError, AttributeError):
        return []

    results: list[TrackMeta] = []
    for recording in recordings:
        if not isinstance(recording, dict):
            continue
        try:
            results.append(to_track_meta(recording))
        except (MusicBrainzError, KeyError, TypeError, AttributeError):
            continue
    return results


def fetch_cover(url: str, *, client: httpx.Client) -> bytes:
    """抓封面圖。Cover Art Archive 沒有圖時回 404，讓呼叫端自行決定。"""
    response = client.get(url)
    response.raise_for_status()
    return response.content
