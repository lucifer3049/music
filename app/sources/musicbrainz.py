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
"""

from __future__ import annotations

import os
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
    except (httpx.HTTPError, MusicBrainzError, ValueError):
        return []

    results: list[TrackMeta] = []
    for recording in recordings:
        try:
            results.append(to_track_meta(recording))
        except (MusicBrainzError, KeyError, TypeError):
            continue
    return results


def fetch_cover(url: str, *, client: httpx.Client) -> bytes:
    """抓封面圖。Cover Art Archive 沒有圖時回 404，讓呼叫端自行決定。"""
    response = client.get(url)
    response.raise_for_status()
    return response.content
