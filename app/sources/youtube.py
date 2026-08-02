"""YouTube Music 來源：網址分類、metadata 探測、雙串流下載。

這是唯一呼叫 yt-dlp 的模組。所有函式都接受注入點，測試不得觸網。
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from app.config import require_ffmpeg
from app.models import SourceTrack

_YT_HOSTS = {"music.youtube.com", "www.youtube.com", "youtube.com", "m.youtube.com"}
_SHORT_HOST = "youtu.be"
# OLAK5uy_ 開頭是 YouTube 自動生成的專輯清單
_ALBUM_LIST_PREFIX = "OLAK5uy_"


class UnsupportedUrlError(ValueError):
    """不是可處理的 YouTube / YouTube Music 網址。"""


class ProbeError(RuntimeError):
    """yt-dlp 無法取得這個網址的任何曲目（下架、私人、地區限制等）。"""


class UrlKind(StrEnum):
    SINGLE = "single"
    ALBUM = "album"
    PLAYLIST = "playlist"


def classify_url(url: str) -> UrlKind:
    parsed = urlparse(url.strip())
    host = parsed.netloc.lower()
    query = parse_qs(parsed.query)

    if host == _SHORT_HOST and parsed.path.strip("/"):
        return UrlKind.SINGLE
    if host not in _YT_HOSTS:
        raise UnsupportedUrlError(f"不支援的網址：{url!r}")

    # watch?v= 一律當單曲處理，即使同時帶 &list=
    if query.get("v"):
        return UrlKind.SINGLE
    list_id = (query.get("list") or [""])[0]
    if list_id.startswith(_ALBUM_LIST_PREFIX):
        return UrlKind.ALBUM
    if list_id:
        return UrlKind.PLAYLIST
    raise UnsupportedUrlError(f"網址沒有 v 或 list 參數：{url!r}")


def _first_str(entry: dict, *keys: str) -> str | None:
    for key in keys:
        value = entry.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, (list, tuple)) and value:
            joined = ", ".join(str(v).strip() for v in value if str(v).strip())
            if joined:
                return joined
    return None


def to_source_track(entry: dict) -> SourceTrack:
    """把 yt-dlp 的 info dict 轉成 SourceTrack。缺欄位一律 None，不猜。"""
    video_id = entry["id"]
    year = entry.get("release_year")
    return SourceTrack(
        video_id=video_id,
        url=f"https://music.youtube.com/watch?v={video_id}",
        raw_title=entry.get("title") or "",
        duration=int(entry["duration"]) if entry.get("duration") else None,
        artist=_first_str(entry, "artist", "artists"),
        track=_first_str(entry, "track"),
        album=_first_str(entry, "album"),
        release_year=int(year) if year else None,
    )


def _default_ydl_factory(opts: dict):
    import yt_dlp

    return yt_dlp.YoutubeDL(opts)


class _CollectingLogger:
    """接住 yt-dlp 的診斷訊息，讓抽取失敗時原因不會被 ignoreerrors 吞掉。"""

    def __init__(self) -> None:
        self.messages: list[str] = []

    def debug(self, msg: str) -> None:
        pass

    def info(self, msg: str) -> None:
        pass

    def warning(self, msg: str) -> None:
        self.messages.append(msg)

    def error(self, msg: str) -> None:
        self.messages.append(msg)


def probe(url: str, *, ydl_factory=_default_ydl_factory) -> list[SourceTrack]:
    """只抽 metadata，不下載任何音訊。回傳順序即專輯／清單順序。"""
    classify_url(url)  # 先驗證，網址不合法就別浪費一次網路來回
    logger = _CollectingLogger()
    opts = {
        "skip_download": True,
        "extract_flat": False,
        "ignoreerrors": True,
        "logger": logger,
    }
    with ydl_factory(opts) as ydl:
        info = ydl.extract_info(url, download=False)

    if info is None:
        raise ProbeError(_probe_error_message(url, logger.messages))
    entries = info.get("entries")
    if entries is None:
        return [to_source_track(info)]
    # 下架或私人影片在 entries 裡會是 None，跳過而不是讓整批失敗
    tracks = [to_source_track(e) for e in entries if e and e.get("id")]
    if not tracks:
        raise ProbeError(_probe_error_message(url, logger.messages))
    return tracks


def _probe_error_message(url: str, messages: list[str]) -> str:
    if messages:
        return f"無法取得任何曲目：{url!r}\n" + "\n".join(messages)
    return f"無法取得任何曲目，yt-dlp 未回傳原因：{url!r}"


# --- 下載 ---------------------------------------------------------------

# 兩條串流都取「來源最高品質、不重編碼」：
#   m4a  = AAC 128kbps（itag 140），容器即目標格式，零後處理
#   opus = Opus ~160kbps（itag 251），webm 容器，需 remux 成 .opus
_FORMAT_M4A = "bestaudio[ext=m4a]/bestaudio[acodec^=mp4a]"
_FORMAT_OPUS = "bestaudio[acodec=opus]/bestaudio[ext=webm]"


class DownloadError(RuntimeError):
    """下載或 remux 失敗。"""


@dataclass(frozen=True, slots=True)
class DownloadedStreams:
    m4a: Path
    opus: Path


def _download_one(video_id: str, workdir: Path, fmt: str, stem: str, ydl_factory) -> None:
    opts = {
        "quiet": True,
        "no_warnings": True,
        "format": fmt,
        "outtmpl": {"default": str(workdir / f"{stem}.%(ext)s")},
        "noplaylist": True,
        "retries": 5,
        "fragment_retries": 5,
    }
    try:
        with ydl_factory(opts) as ydl:
            ydl.download([f"https://music.youtube.com/watch?v={video_id}"])
    except Exception as exc:
        # yt_dlp.utils.DownloadError 與本模組的 DownloadError 同名但不同類別，
        # 呼叫端 `from app.sources.youtube import DownloadError` 只會接到這一個。
        raise DownloadError(f"{video_id}：下載失敗（{fmt}）") from exc


def _find_one(workdir: Path, stem: str, suffixes: tuple[str, ...]) -> Path | None:
    for suffix in suffixes:
        path = workdir / f"{stem}{suffix}"
        if path.exists():
            return path
    return None


def remux_to_opus(src: Path, dest: Path, *, ffmpeg: str, runner=subprocess.run) -> None:
    """把 webm 容器裡的 Opus 串流原封搬進 .opus 容器。

    `-c:a copy` 是硬性要求：這裡不做任何重編碼。
    """
    cmd = [
        ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
        "-i", str(src),
        "-vn",
        "-c:a", "copy",
        str(dest),
    ]
    result = runner(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise DownloadError(f"ffmpeg remux 失敗：{result.stderr}")


def download_streams(
    video_id: str,
    workdir: Path,
    *,
    ydl_factory=_default_ydl_factory,
    ffmpeg: str | None = None,
    runner=subprocess.run,
) -> DownloadedStreams:
    """下載 m4a 與 opus 兩條串流到 workdir，回傳兩個檔案路徑。

    失敗時（任一階段拋出例外）會清掉這次呼叫已經在 workdir 建立的檔案，
    避免下次針對同一個 workdir 重試時，撞見上次留下的殘缺檔案。
    """
    workdir.mkdir(parents=True, exist_ok=True)
    ffmpeg = ffmpeg or require_ffmpeg()

    created: list[Path] = []
    try:
        _download_one(video_id, workdir, _FORMAT_M4A, f"{video_id}.m4a_src", ydl_factory)
        found_m4a = _find_one(workdir, f"{video_id}.m4a_src", (".m4a", ".mp4"))
        if found_m4a is None:
            raise DownloadError(f"{video_id}：取不到 m4a 串流")
        created.append(found_m4a)
        m4a = workdir / f"{video_id}.m4a"
        found_m4a.rename(m4a)
        created.append(m4a)

        _download_one(video_id, workdir, _FORMAT_OPUS, f"{video_id}.opus_src", ydl_factory)
        webm = _find_one(workdir, f"{video_id}.opus_src", (".webm", ".opus", ".ogg"))
        if webm is None:
            raise DownloadError(f"{video_id}：取不到 opus 串流")
        created.append(webm)
        opus = workdir / f"{video_id}.opus"
        if webm.suffix == ".opus":
            webm.replace(opus)
            created.append(opus)
        else:
            remux_to_opus(webm, opus, ffmpeg=ffmpeg, runner=runner)
            created.append(opus)
            webm.unlink(missing_ok=True)

        return DownloadedStreams(m4a=m4a, opus=opus)
    except Exception:
        for path in created:
            path.unlink(missing_ok=True)
        raise
