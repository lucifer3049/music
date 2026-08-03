# 音樂下載與標籤工具 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 建立本機網頁工具，輸入 YouTube Music 網址即下載音訊、以 MusicBrainz 公開 metadata 補齊標籤與封面，並依演出者／專輯結構落檔。

**Architecture:** FastAPI 單進程，無外部服務。業務邏輯集中於 service 層（sources / matching / tagging / storage / jobs），路由僅做驗證與回應塑形。純函式模組（matcher、layout）與 IO 模組（sources）嚴格分離。任務狀態存 SQLite，背景下載跑在 thread pool，進度以 SSE 推播。

**Tech Stack:** Python 3.12、FastAPI、uvicorn、yt-dlp（Python API）、ffmpeg（僅用於 opus remux）、mutagen、httpx、BeautifulSoup4、RapidFuzz、sqlite3（stdlib）、pytest。

## Global Constraints

- Python 3.12.4，全部依賴裝在專案內 venv：`D:\專案練習\音樂下載\.venv`
- **不解密、不繞過 DRM。** metadata 來自 MusicBrainz 公開 API，封面來自 Cover Art Archive，音訊唯一來源為 YouTube Music。
- **下載路徑不重編碼音訊。** m4a 直接取容器輸出；opus 只做 `ffmpeg -c:a copy` remux。在 `app/sources/youtube.py` 的下載與 remux 流程中出現 `-b:a` 或 `-q:a` 一律是實作錯誤。（此約束只管下載路徑；`tests/conftest.py` 為了合成測試素材而編碼音訊不受此限。）
- **測試絕不對 MusicBrainz 或 YouTube 發出真實請求。** 一律用 JSON fixture 與 mock。例外只有兩處手動執行的一次性步驟：Task 6 Step 5 的下載實測，與 Task 7 Step 1 的 fixture 擷取。
- 專案根目錄 `D:\專案練習\音樂下載`，套件根 `app/`，測試根 `tests/`
- 預設輸出根：`D:\Music`（m4a）與 `D:\Archive`（opus），可用環境變數 `MUSIC_ROOT`、`ARCHIVE_ROOT` 覆寫
- 高信心門檻常數 `HIGH_CONFIDENCE = 0.92`，定義於 `app/matching/matcher.py`，其他模組一律 import，不得複製字面值
- 所有 dataclass 使用 `@dataclass(frozen=True, slots=True)`
- 每個 Task 結束都 commit

---

## File Structure

| 檔案 | 職責 |
|---|---|
| `pyproject.toml` | 依賴與 pytest 設定 |
| `app/config.py` | 環境變數讀取、`LibraryRoots`、ffmpeg 檢查 |
| `app/models.py` | 跨模組共用 dataclass：`TrackMeta`、`SourceTrack`、`Candidate` |
| `app/storage/layout.py` | Windows 檔名淨化、輸出路徑生成（純函式） |
| `app/matching/matcher.py` | 標題正規化、候選評分、排序（純函式） |
| `app/sources/youtube.py` | 網址分類、yt-dlp probe、雙串流下載、opus remux |
| `app/sources/musicbrainz.py` | MusicBrainz 查詢、JSON 對映、封面網址 |
| `app/tagging/writer.py` | mutagen 標籤寫入（m4a / opus 雙實作）、JPEG 尺寸解析 |
| `app/jobs/store.py` | SQLite schema、任務與曲目 CRUD、狀態機 |
| `app/pipeline.py` | 串接 probe → match → download → tag → 落檔 |
| `app/api/routes.py` | REST 端點與 SSE |
| `app/main.py` | FastAPI app 組裝、靜態檔掛載、啟動檢查 |
| `app/web/index.html` | 前端頁面 |
| `app/web/app.js` | 前端邏輯 |
| `app/web/style.css` | 前端樣式 |

---

## Task 1: 專案骨架與環境檢查

**Files:**
- Create: `pyproject.toml`
- Create: `app/__init__.py`
- Create: `app/config.py`
- Create: `tests/__init__.py`
- Create: `tests/test_config.py`
- Create: `.gitignore`

**Interfaces:**
- Consumes: 無
- Produces: `LibraryRoots(music: Path, archive: Path)`、`load_roots() -> LibraryRoots`、`require_ffmpeg() -> str`（回傳 ffmpeg 執行檔路徑，缺失則 raise `FfmpegMissingError`）

- [ ] **Step 1: 安裝 ffmpeg 並建立 venv**

```bash
winget install --id Gyan.FFmpeg -e --accept-source-agreements --accept-package-agreements
```

安裝後**開新的 shell**（PATH 需重新載入），確認：

```bash
ffmpeg -version
```

預期：第一行出現 `ffmpeg version ...`

建立 venv 並裝依賴：

```bash
python -m venv .venv && .venv/Scripts/python.exe -m pip install --upgrade pip
```

- [ ] **Step 2: 建立 `pyproject.toml`**

```toml
[project]
name = "music-downloader"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = [
    "fastapi>=0.115",
    "uvicorn[standard]>=0.32",
    "yt-dlp>=2026.7.4",
    "mutagen>=1.47",
    "httpx>=0.28",
    "beautifulsoup4>=4.12",
    "lxml>=5.3",
    "rapidfuzz>=3.10",
]

[project.optional-dependencies]
dev = ["pytest>=8.3", "pytest-asyncio>=0.24", "anyio>=4.6"]

[tool.pytest.ini_options]
testpaths = ["tests"]
asyncio_mode = "auto"

[tool.setuptools.packages.find]
include = ["app*"]
```

安裝：

```bash
.venv/Scripts/python.exe -m pip install -e ".[dev]"
```

- [ ] **Step 3: 建立 `.gitignore`**

```gitignore
.venv/
__pycache__/
*.pyc
.pytest_cache/
*.egg-info/
jobs.db
downloads/
```

- [ ] **Step 4: 寫失敗測試 `tests/test_config.py`**

```python
from pathlib import Path

import pytest

from app.config import FfmpegMissingError, LibraryRoots, load_roots, require_ffmpeg


def test_load_roots_uses_defaults(monkeypatch):
    monkeypatch.delenv("MUSIC_ROOT", raising=False)
    monkeypatch.delenv("ARCHIVE_ROOT", raising=False)
    roots = load_roots()
    assert roots == LibraryRoots(music=Path(r"D:\Music"), archive=Path(r"D:\Archive"))


def test_load_roots_reads_env(monkeypatch, tmp_path):
    monkeypatch.setenv("MUSIC_ROOT", str(tmp_path / "m"))
    monkeypatch.setenv("ARCHIVE_ROOT", str(tmp_path / "a"))
    roots = load_roots()
    assert roots.music == tmp_path / "m"
    assert roots.archive == tmp_path / "a"


def test_require_ffmpeg_returns_path():
    assert require_ffmpeg().lower().endswith(("ffmpeg", "ffmpeg.exe"))


def test_require_ffmpeg_raises_when_absent(monkeypatch):
    monkeypatch.setattr("app.config.shutil.which", lambda _: None)
    with pytest.raises(FfmpegMissingError):
        require_ffmpeg()
```

- [ ] **Step 5: 執行測試確認失敗**

Run: `.venv/Scripts/python.exe -m pytest tests/test_config.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.config'`

- [ ] **Step 6: 實作 `app/config.py`**

```python
"""環境設定與外部工具檢查。"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path

DEFAULT_MUSIC_ROOT = Path(r"D:\Music")
DEFAULT_ARCHIVE_ROOT = Path(r"D:\Archive")


class FfmpegMissingError(RuntimeError):
    """ffmpeg 不在 PATH 上。opus remux 與封面處理都需要它。"""


@dataclass(frozen=True, slots=True)
class LibraryRoots:
    music: Path
    archive: Path


def load_roots() -> LibraryRoots:
    music = os.environ.get("MUSIC_ROOT")
    archive = os.environ.get("ARCHIVE_ROOT")
    return LibraryRoots(
        music=Path(music) if music else DEFAULT_MUSIC_ROOT,
        archive=Path(archive) if archive else DEFAULT_ARCHIVE_ROOT,
    )


def require_ffmpeg() -> str:
    path = shutil.which("ffmpeg")
    if path is None:
        raise FfmpegMissingError(
            "找不到 ffmpeg。請執行 `winget install Gyan.FFmpeg` 後重開終端機。"
        )
    return path
```

`app/__init__.py` 與 `tests/__init__.py` 建為空檔。

- [ ] **Step 7: 執行測試確認通過**

Run: `.venv/Scripts/python.exe -m pytest tests/test_config.py -v`
Expected: PASS，4 passed

- [ ] **Step 8: Commit**

```bash
git init
git add pyproject.toml .gitignore app/__init__.py app/config.py tests/__init__.py tests/test_config.py
git commit -m "feat: 專案骨架、環境設定與 ffmpeg 檢查"
```

---

## Task 2: 共用資料模型

**Files:**
- Create: `app/models.py`
- Create: `tests/test_models.py`

**Interfaces:**
- Consumes: 無
- Produces:
  - `TrackMeta(title, artists, album_artist, album, year, track_no, track_total, genre, cover_url, duration, source_url)`
  - `SourceTrack(video_id, url, raw_title, duration, artist, track, album, release_year)`
  - `Candidate(meta: TrackMeta, score: float)`
  - `TrackMeta.display_artists -> str`（以 `; ` 串接，對應「參與演出者」欄位）

- [ ] **Step 1: 寫失敗測試 `tests/test_models.py`**

```python
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
```

- [ ] **Step 2: 執行測試確認失敗**

Run: `.venv/Scripts/python.exe -m pytest tests/test_models.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.models'`

- [ ] **Step 3: 實作 `app/models.py`**

```python
"""跨模組共用的資料模型。全部不可變，方便在 thread pool 之間傳遞。"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SourceTrack:
    """從 YouTube Music 抽出的原始曲目資訊，尚未經過外部 metadata 補齊。"""

    video_id: str
    url: str
    raw_title: str
    duration: int | None
    artist: str | None
    track: str | None
    album: str | None
    release_year: int | None


@dataclass(frozen=True, slots=True)
class TrackMeta:
    """要寫入檔案的最終標籤內容。"""

    title: str
    artists: tuple[str, ...]
    album_artist: str
    album: str
    year: int | None
    track_no: int | None
    track_total: int | None
    genre: str | None
    cover_url: str | None
    duration: int | None
    source_url: str | None

    @property
    def display_artists(self) -> str:
        """對應 Windows 檔案總管「參與演出者」欄位的字串形式。"""
        return "; ".join(self.artists)


@dataclass(frozen=True, slots=True)
class Candidate:
    meta: TrackMeta
    score: float
```

- [ ] **Step 4: 執行測試確認通過**

Run: `.venv/Scripts/python.exe -m pytest tests/test_models.py -v`
Expected: PASS，5 passed

- [ ] **Step 5: Commit**

```bash
git add app/models.py tests/test_models.py
git commit -m "feat: 共用資料模型 TrackMeta / SourceTrack / Candidate"
```

---

## Task 3: Windows 檔名淨化與路徑生成

**Files:**
- Create: `app/storage/__init__.py`
- Create: `app/storage/layout.py`
- Create: `tests/test_layout.py`

**Interfaces:**
- Consumes: `app.models.TrackMeta`、`app.config.LibraryRoots`
- Produces:
  - `sanitize_component(name: str, *, max_len: int = 100) -> str`
  - `TrackPaths(m4a: Path, opus: Path, cover: Path)`
  - `build_paths(roots: LibraryRoots, meta: TrackMeta) -> TrackPaths`

- [ ] **Step 1: 寫失敗測試 `tests/test_layout.py`**

```python
from pathlib import Path

import pytest

from app.config import LibraryRoots
from app.models import TrackMeta
from app.storage.layout import build_paths, sanitize_component


@pytest.mark.parametrize(
    "raw,expected",
    [
        ('a/b\\c:d*e?f"g<h>i|j', "a_b_c_d_e_f_g_h_i_j"),
        ("trailing dots...", "trailing dots"),
        ("trailing space   ", "trailing space"),
        ("CON", "_CON"),
        ("nul", "_nul"),
        ("COM1", "_COM1"),
        ("LPT9", "_LPT9"),
        ("正常名稱", "正常名稱"),
        ("", "_"),
        ("   ", "_"),
        ("...", "_"),
    ],
)
def test_sanitize_component(raw, expected):
    assert sanitize_component(raw) == expected


def test_sanitize_component_truncates():
    assert len(sanitize_component("あ" * 500, max_len=100)) == 100


def test_sanitize_component_strips_control_chars():
    assert sanitize_component("a\x00b\nc") == "abc"


def _meta(**over) -> TrackMeta:
    base = dict(
        title="人間驚鴻宴",
        artists=("指尖笑",),
        album_artist="指尖笑",
        album="人間驚鴻宴",
        year=2026,
        track_no=3,
        track_total=10,
        genre=None,
        cover_url=None,
        duration=207,
        source_url=None,
    )
    base.update(over)
    return TrackMeta(**base)


def test_build_paths_layout(tmp_path):
    roots = LibraryRoots(music=tmp_path / "Music", archive=tmp_path / "Archive")
    paths = build_paths(roots, _meta())
    assert paths.m4a == tmp_path / "Music" / "指尖笑" / "人間驚鴻宴" / "03 人間驚鴻宴.m4a"
    assert paths.opus == tmp_path / "Archive" / "指尖笑" / "人間驚鴻宴" / "03 人間驚鴻宴.opus"
    assert paths.cover == tmp_path / "Music" / "指尖笑" / "人間驚鴻宴" / "cover.jpg"


def test_build_paths_without_track_number(tmp_path):
    roots = LibraryRoots(music=tmp_path / "Music", archive=tmp_path / "Archive")
    paths = build_paths(roots, _meta(track_no=None))
    assert paths.m4a.name == "人間驚鴻宴.m4a"


def test_build_paths_sanitizes_every_component(tmp_path):
    roots = LibraryRoots(music=tmp_path / "Music", archive=tmp_path / "Archive")
    paths = build_paths(roots, _meta(album_artist="A/B", album="C:D", title="E?F"))
    assert paths.m4a == tmp_path / "Music" / "A_B" / "C_D" / "03 E_F.m4a"
```

- [ ] **Step 2: 執行測試確認失敗**

Run: `.venv/Scripts/python.exe -m pytest tests/test_layout.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.storage'`

- [ ] **Step 3: 實作 `app/storage/layout.py`**

```python
"""輸出路徑生成與 Windows 檔名淨化。純函式，不碰檔案系統。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from app.config import LibraryRoots
from app.models import TrackMeta

# Windows 路徑非法字元
_ILLEGAL = re.compile(r'[\\/:*?"<>|]')
# ASCII 控制字元
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
# Windows 保留裝置名（不分大小寫）
_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


@dataclass(frozen=True, slots=True)
class TrackPaths:
    m4a: Path
    opus: Path
    cover: Path


def sanitize_component(name: str, *, max_len: int = 100) -> str:
    """把任意字串轉成安全的單層路徑名稱。

    非法字元換底線；去掉控制字元；去除尾端句點與空白（Windows 會靜默截掉，
    造成路徑對不上）；保留裝置名前面加底線；長度上限避免超過 MAX_PATH。
    """
    cleaned = _CONTROL.sub("", name)
    cleaned = _ILLEGAL.sub("_", cleaned)
    cleaned = cleaned.strip().rstrip(". ")
    cleaned = cleaned[:max_len]
    cleaned = cleaned.rstrip(". ")
    if not cleaned:
        return "_"
    if cleaned.upper() in _RESERVED:
        return "_" + cleaned
    return cleaned


def _filename(meta: TrackMeta, suffix: str) -> str:
    title = sanitize_component(meta.title)
    if meta.track_no is None:
        return f"{title}{suffix}"
    return f"{meta.track_no:02d} {title}{suffix}"


def build_paths(roots: LibraryRoots, meta: TrackMeta) -> TrackPaths:
    artist_dir = sanitize_component(meta.album_artist)
    album_dir = sanitize_component(meta.album)
    music_dir = roots.music / artist_dir / album_dir
    archive_dir = roots.archive / artist_dir / album_dir
    return TrackPaths(
        m4a=music_dir / _filename(meta, ".m4a"),
        opus=archive_dir / _filename(meta, ".opus"),
        cover=music_dir / "cover.jpg",
    )
```

`app/storage/__init__.py` 建為空檔。

- [ ] **Step 4: 執行測試確認通過**

Run: `.venv/Scripts/python.exe -m pytest tests/test_layout.py -v`
Expected: PASS，16 passed

- [ ] **Step 5: Commit**

```bash
git add app/storage/ tests/test_layout.py
git commit -m "feat: Windows 檔名淨化與輸出路徑生成"
```

---

## Task 4: 標題正規化與候選評分

**Files:**
- Create: `app/matching/__init__.py`
- Create: `app/matching/matcher.py`
- Create: `tests/test_matcher.py`

**Interfaces:**
- Consumes: `app.models.SourceTrack`、`app.models.TrackMeta`、`app.models.Candidate`
- Produces:
  - `HIGH_CONFIDENCE: float = 0.92`
  - `normalize_title(raw: str) -> str`
  - `split_title(raw: str) -> tuple[str | None, str]`（回傳 `(artist_guess, title_guess)`）
  - `score_candidate(source: SourceTrack, meta: TrackMeta) -> float`
  - `rank_candidates(source: SourceTrack, metas: Iterable[TrackMeta], *, limit: int = 3) -> list[Candidate]`

- [ ] **Step 1: 寫失敗測試 `tests/test_matcher.py`**

```python
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
    assert score_candidate(_source(), _meta(title="完全不同的歌")) < 0.5


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
```

- [ ] **Step 2: 執行測試確認失敗**

Run: `.venv/Scripts/python.exe -m pytest tests/test_matcher.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.matching'`

- [ ] **Step 3: 實作 `app/matching/matcher.py`**

```python
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
    """時長吻合度。任一邊缺值回 None，代表這項不參與評分。"""
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

    權重：歌名 0.5、演出者 0.3、時長 0.2。
    時長缺值時該項不計分，其餘權重按比例放大，避免無時長資料就永遠達不到門檻。
    """
    if source.track:
        title_guess = normalize_title(source.track)
        artist_guess = normalize_title(source.artist) if source.artist else None
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
```

`app/matching/__init__.py` 建為空檔。

- [ ] **Step 4: 執行測試確認通過**

Run: `.venv/Scripts/python.exe -m pytest tests/test_matcher.py -v`
Expected: PASS，21 passed

若 `test_normalize_title_keeps_feat_content` 失敗，檢查 `_is_noise` 是否誤判 `feat. 某某` 為雜訊 —— `feat.` 不在 `_NOISE_WORDS` 內，應回 False。

- [ ] **Step 5: Commit**

```bash
git add app/matching/ tests/test_matcher.py
git commit -m "feat: 標題正規化與候選評分演算法"
```

---

## Task 5: YouTube Music 網址分類與 metadata 探測

**Files:**
- Create: `app/sources/__init__.py`
- Create: `app/sources/youtube.py`
- Create: `tests/test_youtube_probe.py`

**Interfaces:**
- Consumes: `app.models.SourceTrack`
- Produces:
  - `UrlKind`（`StrEnum`：`SINGLE`、`ALBUM`、`PLAYLIST`）
  - `classify_url(url: str) -> UrlKind`
  - `UnsupportedUrlError`
  - `to_source_track(entry: dict) -> SourceTrack`
  - `probe(url: str, *, ydl_factory=...) -> list[SourceTrack]`

- [ ] **Step 1: 寫失敗測試 `tests/test_youtube_probe.py`**

```python
import pytest

from app.sources.youtube import (
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
    """已下架曲目在 playlist 會是 None，不能讓整批爆掉。"""
    info = {
        "_type": "playlist",
        "entries": [None, {"id": "b", "title": "還在的歌"}],
    }
    tracks = probe(
        "https://music.youtube.com/playlist?list=OLAK5uy_x",
        ydl_factory=lambda opts: _FakeYDL(info),
    )
    assert [t.video_id for t in tracks] == ["b"]
```

- [ ] **Step 2: 執行測試確認失敗**

Run: `.venv/Scripts/python.exe -m pytest tests/test_youtube_probe.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.sources'`

- [ ] **Step 3: 實作 `app/sources/youtube.py` 的分類與探測部分**

```python
"""YouTube Music 來源：網址分類、metadata 探測、雙串流下載。

這是唯一呼叫 yt-dlp 的模組。所有函式都接受注入點，測試不得觸網。
"""

from __future__ import annotations

import re
from enum import StrEnum
from urllib.parse import parse_qs, urlparse

from app.models import SourceTrack

_YT_HOSTS = {"music.youtube.com", "www.youtube.com", "youtube.com", "m.youtube.com"}
_SHORT_HOST = "youtu.be"
# OLAK5uy_ 開頭是 YouTube 自動生成的專輯清單
_ALBUM_LIST_PREFIX = "OLAK5uy_"


class UnsupportedUrlError(ValueError):
    """不是可處理的 YouTube / YouTube Music 網址。"""


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


def probe(url: str, *, ydl_factory=_default_ydl_factory) -> list[SourceTrack]:
    """只抽 metadata，不下載任何音訊。回傳順序即專輯／清單順序。"""
    classify_url(url)  # 先驗證，網址不合法就別浪費一次網路來回
    opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "extract_flat": False,
        "ignoreerrors": True,
    }
    with ydl_factory(opts) as ydl:
        info = ydl.extract_info(url, download=False)

    if info is None:
        return []
    entries = info.get("entries")
    if entries is None:
        return [to_source_track(info)]
    # 下架或私人影片在 entries 裡會是 None，跳過而不是讓整批失敗
    return [to_source_track(e) for e in entries if e and e.get("id")]
```

`app/sources/__init__.py` 建為空檔。

- [ ] **Step 4: 執行測試確認通過**

Run: `.venv/Scripts/python.exe -m pytest tests/test_youtube_probe.py -v`
Expected: PASS，19 passed

- [ ] **Step 5: Commit**

```bash
git add app/sources/ tests/test_youtube_probe.py
git commit -m "feat: YouTube 網址分類與 metadata 探測"
```

---

## Task 6: 雙串流下載與 opus remux

**Files:**
- Modify: `app/sources/youtube.py`（在檔尾新增下載區塊）
- Create: `tests/test_youtube_download.py`

**Interfaces:**
- Consumes: Task 5 的 `app/sources/youtube.py`、`app.config.require_ffmpeg`
- Produces:
  - `DownloadedStreams(m4a: Path, opus: Path)`
  - `DownloadError`
  - `remux_to_opus(src: Path, dest: Path, *, ffmpeg: str, runner=subprocess.run) -> None`
  - `download_streams(video_id: str, workdir: Path, *, ydl_factory=..., ffmpeg=None, runner=subprocess.run) -> DownloadedStreams`

- [ ] **Step 1: 寫失敗測試 `tests/test_youtube_download.py`**

```python
from pathlib import Path

import pytest

from app.sources.youtube import (
    DownloadError,
    DownloadedStreams,
    download_streams,
    remux_to_opus,
)


class _FakeCompleted:
    def __init__(self, returncode=0, stderr=""):
        self.returncode = returncode
        self.stderr = stderr


def test_remux_uses_stream_copy_only(tmp_path):
    calls = []

    def runner(cmd, **kwargs):
        calls.append(cmd)
        Path(cmd[-1]).write_bytes(b"opus")
        return _FakeCompleted()

    src = tmp_path / "in.webm"
    src.write_bytes(b"webm")
    dest = tmp_path / "out.opus"
    remux_to_opus(src, dest, ffmpeg="ffmpeg", runner=runner)

    cmd = calls[0]
    assert "-c:a" in cmd and cmd[cmd.index("-c:a") + 1] == "copy"
    # 任何 bitrate / quality 參數都代表重編碼，是實作錯誤
    assert "-b:a" not in cmd
    assert "-q:a" not in cmd
    assert dest.exists()


def test_remux_raises_on_ffmpeg_failure(tmp_path):
    src = tmp_path / "in.webm"
    src.write_bytes(b"webm")

    def runner(cmd, **kwargs):
        return _FakeCompleted(returncode=1, stderr="boom")

    with pytest.raises(DownloadError, match="boom"):
        remux_to_opus(src, tmp_path / "out.opus", ffmpeg="ffmpeg", runner=runner)


class _FakeYDL:
    """依照 outtmpl 產生假的下載結果檔。"""

    def __init__(self, opts, produced_suffix):
        self._opts = opts
        self._suffix = produced_suffix

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def download(self, urls):
        template = Path(self._opts["outtmpl"]["default"])
        out = template.with_name(template.name.replace(".%(ext)s", self._suffix))
        out.write_bytes(b"fake audio")
        return 0


def test_download_streams_produces_both_files(tmp_path):
    def factory(opts):
        fmt = opts["format"]
        suffix = ".m4a" if "m4a" in fmt else ".webm"
        return _FakeYDL(opts, suffix)

    result = download_streams(
        "abc123", tmp_path, ydl_factory=factory, ffmpeg="ffmpeg",
        runner=lambda cmd, **kw: (Path(cmd[-1]).write_bytes(b"opus"), _FakeCompleted())[1],
    )
    assert isinstance(result, DownloadedStreams)
    assert result.m4a.exists() and result.m4a.suffix == ".m4a"
    assert result.opus.exists() and result.opus.suffix == ".opus"


def test_download_streams_raises_when_m4a_missing(tmp_path):
    class _NoopYDL:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def download(self, urls):
            return 0

    with pytest.raises(DownloadError, match="m4a"):
        download_streams("abc123", tmp_path, ydl_factory=lambda o: _NoopYDL(), ffmpeg="ffmpeg")
```

- [ ] **Step 2: 執行測試確認失敗**

Run: `.venv/Scripts/python.exe -m pytest tests/test_youtube_download.py -v`
Expected: FAIL — `ImportError: cannot import name 'DownloadError'`

- [ ] **Step 3: 在 `app/sources/youtube.py` 檔尾新增下載區塊**

同時在檔頭 import 區補上：

```python
import subprocess
from dataclasses import dataclass
from pathlib import Path

from app.config import require_ffmpeg
```

檔尾新增：

```python
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
    with ydl_factory(opts) as ydl:
        ydl.download([f"https://music.youtube.com/watch?v={video_id}"])


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
    """下載 m4a 與 opus 兩條串流到 workdir，回傳兩個檔案路徑。"""
    workdir.mkdir(parents=True, exist_ok=True)
    ffmpeg = ffmpeg or require_ffmpeg()

    _download_one(video_id, workdir, _FORMAT_M4A, f"{video_id}.m4a_src", ydl_factory)
    m4a = _find_one(workdir, f"{video_id}.m4a_src", (".m4a", ".mp4"))
    if m4a is None:
        raise DownloadError(f"{video_id}：取不到 m4a 串流")
    if m4a.suffix != ".m4a":
        m4a = m4a.rename(m4a.with_suffix(".m4a"))

    _download_one(video_id, workdir, _FORMAT_OPUS, f"{video_id}.opus_src", ydl_factory)
    webm = _find_one(workdir, f"{video_id}.opus_src", (".webm", ".opus", ".ogg"))
    if webm is None:
        raise DownloadError(f"{video_id}：取不到 opus 串流")
    opus = workdir / f"{video_id}.opus"
    if webm.suffix == ".opus":
        webm.replace(opus)
    else:
        remux_to_opus(webm, opus, ffmpeg=ffmpeg, runner=runner)
        webm.unlink(missing_ok=True)

    return DownloadedStreams(m4a=m4a, opus=opus)
```

- [ ] **Step 4: 執行測試確認通過**

Run: `.venv/Scripts/python.exe -m pytest tests/test_youtube_download.py -v`
Expected: PASS，4 passed

- [ ] **Step 5: 手動實測一首（唯一一次觸網，不進 CI）**

```bash
.venv/Scripts/python.exe -c "from pathlib import Path; from app.sources.youtube import download_streams; print(download_streams('dQw4w9WgXcQ', Path('downloads/_probe')))"
```

預期：印出兩個路徑，兩檔皆存在。用 ffprobe 確認**沒有被重編碼**：

```bash
ffprobe -v error -select_streams a:0 -show_entries stream=codec_name,bit_rate -of default=noprint_wrappers=1 downloads/_probe/dQw4w9WgXcQ.opus
```

預期：`codec_name=opus`，bit_rate 約 160000（±20%）。若看到 `codec_name=vorbis` 或明顯偏低的 bitrate，代表發生重編碼，必須修正。

- [ ] **Step 6: Commit**

```bash
git add app/sources/youtube.py tests/test_youtube_download.py
git commit -m "feat: 雙串流下載與 opus 無損 remux"
```

---

## Task 7: MusicBrainz metadata 查詢

**Files:**
- Create: `app/sources/musicbrainz.py`
- Create: `tests/fixtures/mb_recording_search.json`（Step 1 擷取）
- Create: `tests/fixtures/mb_release.json`（Step 1 擷取）
- Create: `tests/test_musicbrainz.py`

**Interfaces:**
- Consumes: `app.models.TrackMeta`
- Produces:
  - `MusicBrainzError(RuntimeError)`
  - `make_client() -> httpx.Client`
  - `search_recordings(query: str, *, client: httpx.Client, limit: int = 3) -> list[dict]`
  - `to_track_meta(recording: dict) -> TrackMeta`
  - `cover_url_for_release(release_mbid: str) -> str`
  - `search(query: str, *, client: httpx.Client) -> list[TrackMeta]`
  - `fetch_cover(url: str, *, client: httpx.Client) -> bytes`

### 為何是 MusicBrainz 而不是 KKBOX

原設計以 KKBOX 為 metadata 來源。實作時實測發現其專輯與歌曲頁已由 AWS WAF 攔截程式化存取：回應 `HTTP 202`、標頭 `x-amzn-waf-action: challenge`、內容零位元組。繞過機器人驗證不在選項內，且即使人工存下 HTML 當 fixture，執行期一樣被擋 —— 那只會做出「測試全綠、實際一跑就死」的模組。

MusicBrainz 是公開的音樂資料庫，其 Web Service 本就是給程式化存取用的，封面走 Cover Art Archive。

### MusicBrainz 的硬性規定（違反會被拒絕服務）

1. **User-Agent 必須帶應用名稱與聯絡方式。** 泛用瀏覽器 UA 會收到 403。格式：`music-downloader/0.1.0 ( <contact> )`。
   聯絡方式從環境變數 `MUSICBRAINZ_CONTACT` 讀取，預設值為說明字串，README 指示使用者填自己的 email。**不得把個人 email 寫死在程式碼裡。**
2. **匿名速率上限為每秒 1 次請求。** 模組內以最小間隔強制，不可只靠呼叫端自律。
3. `recording.length` 單位是**毫秒**，`TrackMeta.duration` 是秒 —— 必須除以 1000。這是最容易出錯的一點。

- [ ] **Step 1: 擷取真實 fixture（手動執行一次，之後測試全離線）**

先確認能連上並看清實際 JSON 結構，再寫解析器 —— 不預猜欄位路徑。

```bash
.venv/Scripts/python.exe -c "
import httpx, json, pathlib, time
pathlib.Path('tests/fixtures').mkdir(parents=True, exist_ok=True)
ua = {'User-Agent': 'music-downloader/0.1.0 ( local-personal-use )'}
with httpx.Client(headers=ua, timeout=20, follow_redirects=True) as c:
    r = c.get('https://musicbrainz.org/ws/2/recording',
              params={'query': 'recording:\"Bohemian Rhapsody\" AND artist:\"Queen\"',
                      'fmt': 'json', 'limit': 3})
    print('search', r.status_code, len(r.text))
    r.raise_for_status()
    pathlib.Path('tests/fixtures/mb_recording_search.json').write_text(r.text, encoding='utf-8')
    data = r.json()
    rec = data['recordings'][0]
    rel = rec['releases'][0]
    print('recording keys:', sorted(rec.keys()))
    print('release keys:', sorted(rel.keys()))
    print(json.dumps(rec, ensure_ascii=False, indent=2)[:2500])
    time.sleep(1.2)
    r2 = c.get(f'https://musicbrainz.org/ws/2/release/{rel[\"id\"]}',
               params={'inc': 'recordings+artist-credits', 'fmt': 'json'})
    print('release', r2.status_code, len(r2.text))
    r2.raise_for_status()
    pathlib.Path('tests/fixtures/mb_release.json').write_text(r2.text, encoding='utf-8')
"
```

同樣手法再抓一組中文專輯（例如 `recording:"稻香" AND artist:"周杰倫"`），確認中文欄位正常，存為 `tests/fixtures/mb_recording_search_cjk.json`。

**接著實際檢查曲序資料在哪裡：**

```bash
.venv/Scripts/python.exe -c "
import json, pathlib
d = json.loads(pathlib.Path('tests/fixtures/mb_recording_search.json').read_text(encoding='utf-8'))
rec = d['recordings'][0]
print('length(ms):', rec.get('length'))
print('artist-credit:', json.dumps(rec.get('artist-credit'), ensure_ascii=False)[:500])
for rel in rec.get('releases', [])[:2]:
    print('--- release', rel.get('title'), rel.get('date'))
    print('  media:', json.dumps(rel.get('media'), ensure_ascii=False)[:800])
"
```

搜尋結果內的 `releases[].media[]` 通常已含 `track-count` 與 `track[].position` —— 若確實如此，一次搜尋即可組出完整 `TrackMeta`，不必再打第二支 API。**依實際觀察決定**，並把結論寫進 `musicbrainz.py` 頂端註解。

若 MusicBrainz 回 403 或連不上，停下來報 NEEDS_CONTEXT，不要偽造 fixture。

- [ ] **Step 2: 寫失敗測試 `tests/test_musicbrainz.py`**

依 Step 1 觀察到的實際內容填入 `EXPECTED_*` 常數。

```python
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

# 依 Step 1 實際擷取到的內容填寫
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
    """MusicBrainz 的 length 是毫秒，TrackMeta.duration 是秒。"""
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
    meta = to_track_meta(_first_recording())
    if meta.track_no is not None:
        assert meta.track_no >= 1
        assert meta.track_total is None or meta.track_total >= meta.track_no


def test_to_track_meta_raises_without_title():
    with pytest.raises(MusicBrainzError):
        to_track_meta({"id": "x"})


def test_to_track_meta_handles_recording_without_releases():
    """單曲未收錄於任何專輯時，album 退回曲名，不得炸掉。"""
    recording = {"id": "x", "title": "孤兒曲", "artist-credit": [{"name": "某人"}]}
    meta = to_track_meta(recording)
    assert meta.album == "孤兒曲"
    assert meta.track_no is None


def test_to_track_meta_cjk_fixture():
    recording = _fixture("mb_recording_search_cjk.json")["recordings"][0]
    meta = to_track_meta(recording)
    assert meta.title
    assert meta.artists


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
        assert fetch_cover("https://coverartarchive.org/release/x/front-500",
                           client=client).startswith(b"\xff\xd8")


def test_fetch_cover_raises_on_missing_art():
    """Cover Art Archive 沒有圖時回 404，呼叫端要能分辨。"""
    with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(404))) as client:
        with pytest.raises(httpx.HTTPStatusError):
            fetch_cover("https://coverartarchive.org/release/x/front-500", client=client)
```

- [ ] **Step 3: 執行測試確認失敗**

Run: `.venv/Scripts/python.exe -m pytest tests/test_musicbrainz.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.sources.musicbrainz'`

- [ ] **Step 4: 實作 `app/sources/musicbrainz.py`**

下方是骨架。`to_track_meta` 內部的欄位路徑依 Step 1 觀察到的實際 JSON 調整；**所有欄位路徑只寫在這個檔案裡**，MusicBrainz 改版時只需改這裡。

```python
"""MusicBrainz 公開 metadata 來源。

僅讀取公開音樂資料庫的曲目資訊與封面圖網址，用於補齊本地檔案標籤。
不涉及任何音訊取得或 DRM 處理 —— 音訊一律來自 YouTube Music。

MusicBrainz 的兩條硬性規定：
  1. User-Agent 必須可辨識並帶聯絡方式，泛用瀏覽器 UA 會收到 403
  2. 匿名存取每秒至多 1 次請求

recording.length 單位是毫秒，TrackMeta.duration 是秒。
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
    """從 release.media 取出曲序與總曲數。結構缺漏時回 (None, None)。"""
    media = release.get("media")
    if not isinstance(media, list) or not media:
        return (None, None)
    first = media[0]
    if not isinstance(first, dict):
        return (None, None)
    total = first.get("track-count")
    tracks = first.get("track")
    position = None
    if isinstance(tracks, list) and tracks and isinstance(tracks[0], dict):
        position = tracks[0].get("position") or tracks[0].get("number")
    return (
        int(position) if isinstance(position, (int, str)) and str(position).isdigit() else None,
        int(total) if isinstance(total, int) else None,
    )


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
        year = _year_from_date(release.get("date"))
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
```

- [ ] **Step 5: 依 fixture 實況調整並讓測試通過**

Run: `.venv/Scripts/python.exe -m pytest tests/test_musicbrainz.py -v`
Expected: PASS

若 `to_track_meta` 的欄位取不到，用下列指令印出 fixture 的實際結構對照調整：

```bash
.venv/Scripts/python.exe -c "
import json, pathlib
d = json.loads(pathlib.Path('tests/fixtures/mb_recording_search.json').read_text(encoding='utf-8'))
print(json.dumps(d['recordings'][0], ensure_ascii=False, indent=2)[:4000])
"
```

- [ ] **Step 6: 節流實測**

確認 `_throttle` 真的擋得住連續呼叫（不觸網，只量時間）：

```bash
.venv/Scripts/python.exe -c "
import time
from app.sources.musicbrainz import _throttle
t0 = time.monotonic()
for _ in range(3):
    _throttle()
print('3 calls took', round(time.monotonic() - t0, 2), 'seconds (expect >= 2.2)')
"
```

預期輸出的秒數 ≥ 2.2。若接近 0，節流沒生效，必須修正。

- [ ] **Step 7: Commit**

```bash
git add app/sources/musicbrainz.py tests/test_musicbrainz.py tests/fixtures/
git commit -m "feat: MusicBrainz metadata 查詢與封面網址組合"
```

---

## Task 8: 標籤寫入（m4a + opus）

**Files:**
- Create: `app/tagging/__init__.py`
- Create: `app/tagging/writer.py`
- Create: `tests/test_tagging.py`
- Create: `tests/conftest.py`

**Interfaces:**
- Consumes: `app.models.TrackMeta`、`app.config.require_ffmpeg`
- Produces:
  - `UnsupportedFormatError`
  - `jpeg_dimensions(data: bytes) -> tuple[int, int]`
  - `write_tags(path: Path, meta: TrackMeta, cover: bytes | None = None) -> None`

- [ ] **Step 1: 建立 `tests/conftest.py`（用 ffmpeg 產生真實小音檔）**

標籤寫入不能靠假檔測 —— mutagen 需要合法容器。用 ffmpeg 現場合成 1 秒無聲音檔。

```python
import subprocess
from pathlib import Path

import pytest

from app.config import require_ffmpeg


def _synth(dest: Path, codec: str, container_args: list[str]) -> Path:
    cmd = [
        require_ffmpeg(), "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo", "-t", "1",
        "-c:a", codec, *container_args, str(dest),
    ]
    subprocess.run(cmd, check=True, capture_output=True)
    return dest


@pytest.fixture
def m4a_file(tmp_path: Path) -> Path:
    return _synth(tmp_path / "sample.m4a", "aac", ["-b:a", "128k"])


@pytest.fixture
def opus_file(tmp_path: Path) -> Path:
    return _synth(tmp_path / "sample.opus", "libopus", ["-b:a", "128k"])


@pytest.fixture
def jpeg_bytes() -> bytes:
    """最小合法 JPEG：SOF0 宣告 64x48，供封面與尺寸解析測試使用。"""
    return bytes.fromhex(
        "ffd8"
        "ffe000104a46494600010100000100010000"
        "ffc0001108003000400301220002110311"
        "ffd9"
    )
```

註：此處 ffmpeg 的 `-b:a` 是為了合成測試素材，與「不重編碼下載」的約束無關。

- [ ] **Step 2: 寫失敗測試 `tests/test_tagging.py`**

```python
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
```

- [ ] **Step 3: 執行測試確認失敗**

Run: `.venv/Scripts/python.exe -m pytest tests/test_tagging.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.tagging'`

- [ ] **Step 4: 實作 `app/tagging/writer.py`**

```python
"""標籤寫入。m4a 走 MP4 atom，opus 走 Vorbis comment。

欄位對映刻意與 Windows 檔案總管「內容」面板一致：
標題 / 參與演出者 / 專輯演出者 / 專輯 / 年份 / 曲序 / 類型 / 封面。
"""

from __future__ import annotations

import base64
import struct
from pathlib import Path

from mutagen.flac import Picture
from mutagen.mp4 import MP4, MP4Cover
from mutagen.oggopus import OggOpus

from app.models import TrackMeta

# 帶尺寸資訊的 JPEG SOF marker（跳過 SOF4/SOF8/SOF12 這些非影像 marker）
_SOF_MARKERS = {
    0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
    0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF,
}


class UnsupportedFormatError(ValueError):
    """副檔名不在支援清單內。"""


def jpeg_dimensions(data: bytes) -> tuple[int, int]:
    """從 JPEG 位元組取出寬高。解析不出來回 (0, 0)。

    自己解析是為了避免只為讀兩個數字就引入影像處理依賴。
    """
    if not data.startswith(b"\xff\xd8"):
        return (0, 0)
    index = 2
    length = len(data)
    while index + 9 < length:
        if data[index] != 0xFF:
            index += 1
            continue
        marker = data[index + 1]
        if marker in _SOF_MARKERS:
            height, width = struct.unpack(">HH", data[index + 5 : index + 9])
            return (width, height)
        if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7:
            index += 2
            continue
        segment = struct.unpack(">H", data[index + 2 : index + 4])[0]
        index += 2 + segment
    return (0, 0)


def _make_picture(cover: bytes) -> Picture:
    picture = Picture()
    picture.data = cover
    picture.type = 3  # front cover
    picture.mime = "image/jpeg"
    picture.width, picture.height = jpeg_dimensions(cover)
    picture.depth = 24
    return picture


def _write_m4a(path: Path, meta: TrackMeta, cover: bytes | None) -> None:
    audio = MP4(path)
    audio["\xa9nam"] = [meta.title]
    audio["\xa9ART"] = [meta.display_artists]
    audio["aART"] = [meta.album_artist]
    audio["\xa9alb"] = [meta.album]
    if meta.year is not None:
        audio["\xa9day"] = [str(meta.year)]
    if meta.track_no is not None:
        audio["trkn"] = [(meta.track_no, meta.track_total or 0)]
    if meta.genre:
        audio["\xa9gen"] = [meta.genre]
    if cover:
        audio["covr"] = [MP4Cover(cover, imageformat=MP4Cover.FORMAT_JPEG)]
    audio.save()


def _write_opus(path: Path, meta: TrackMeta, cover: bytes | None) -> None:
    audio = OggOpus(path)
    audio["TITLE"] = [meta.title]
    # Vorbis comment 允許同一鍵多值，演出者分開存比串接語意更正確
    audio["ARTIST"] = list(meta.artists)
    audio["ALBUMARTIST"] = [meta.album_artist]
    audio["ALBUM"] = [meta.album]
    if meta.year is not None:
        audio["DATE"] = [str(meta.year)]
    if meta.track_no is not None:
        audio["TRACKNUMBER"] = [str(meta.track_no)]
    if meta.track_total is not None:
        audio["TOTALTRACKS"] = [str(meta.track_total)]
    if meta.genre:
        audio["GENRE"] = [meta.genre]
    if cover:
        encoded = base64.b64encode(_make_picture(cover).write()).decode("ascii")
        audio["METADATA_BLOCK_PICTURE"] = [encoded]
    audio.save()


_WRITERS = {".m4a": _write_m4a, ".opus": _write_opus}


def write_tags(path: Path, meta: TrackMeta, cover: bytes | None = None) -> None:
    writer = _WRITERS.get(path.suffix.lower())
    if writer is None:
        raise UnsupportedFormatError(f"不支援的副檔名：{path.suffix}")
    writer(path, meta, cover)
```

`app/tagging/__init__.py` 建為空檔。

- [ ] **Step 5: 執行測試確認通過**

Run: `.venv/Scripts/python.exe -m pytest tests/test_tagging.py -v`
Expected: PASS，10 passed

- [ ] **Step 6: 人工驗證 Windows 檔案總管顯示**

把 `test_write_m4a_tags` 產生的檔案複製到桌面，右鍵 → 內容 → 詳細資料，確認標題、參與演出者、專輯演出者、專輯、年份、曲序、類型都有值，縮圖顯示封面。這是本專案的驗收畫面，必須人工看過一次。

- [ ] **Step 7: Commit**

```bash
git add app/tagging/ tests/test_tagging.py tests/conftest.py
git commit -m "feat: m4a 與 opus 標籤寫入含封面嵌入"
```

---

## Task 9: SQLite 任務儲存與狀態機

**Files:**
- Create: `app/jobs/__init__.py`
- Create: `app/jobs/store.py`
- Create: `tests/test_store.py`

**Interfaces:**
- Consumes: `app.models.SourceTrack`、`app.models.TrackMeta`、`app.models.Candidate`
- Produces:
  - `TrackStatus`（`StrEnum`：`pending`、`matching`、`awaiting_confirm`、`downloading`、`tagging`、`done`、`failed`、`skipped`）
  - `TrackRow(id, job_id, video_id, source: SourceTrack, status, candidates: list[Candidate], chosen: TrackMeta | None, error: str | None)`
  - `JobRow(id, url, created_at, tracks: list[TrackRow])`
  - `JobStore(db_path: Path)` 具方法：`init_schema()`、`create_job(url)`、`add_track(job_id, source)`、`set_candidates(track_id, candidates)`、`confirm(track_id, meta)`、`set_status(track_id, status, error=None)`、`get_track(track_id)`、`get_job(job_id)`、`list_jobs()`、`find_by_video_id(video_id)`、`close()`

- [ ] **Step 1: 寫失敗測試 `tests/test_store.py`**

```python
import pytest

from app.jobs.store import JobStore, TrackStatus
from app.models import Candidate, SourceTrack, TrackMeta


@pytest.fixture
def store(tmp_path):
    s = JobStore(tmp_path / "jobs.db")
    s.init_schema()
    yield s
    s.close()


def _source(video_id="v1") -> SourceTrack:
    return SourceTrack(
        video_id=video_id,
        url=f"https://music.youtube.com/watch?v={video_id}",
        raw_title="【MV】指尖笑 - 人間驚鴻宴",
        duration=207,
        artist=None,
        track=None,
        album=None,
        release_year=None,
    )


def _meta(title="人間驚鴻宴") -> TrackMeta:
    return TrackMeta(
        title=title,
        artists=("指尖笑",),
        album_artist="指尖笑",
        album="人間驚鴻宴",
        year=2026,
        track_no=1,
        track_total=1,
        genre=None,
        cover_url="https://i.kfs.io/x/cover.jpg",
        duration=207,
        source_url=None,
    )


def test_create_job_and_add_track(store):
    job_id = store.create_job("https://music.youtube.com/watch?v=v1")
    track_id = store.add_track(job_id, _source())
    job = store.get_job(job_id)
    assert job.id == job_id
    assert len(job.tracks) == 1
    assert job.tracks[0].id == track_id
    assert job.tracks[0].status == TrackStatus.PENDING


def test_source_track_roundtrips(store):
    job_id = store.create_job("u")
    track_id = store.add_track(job_id, _source())
    assert store.get_track(track_id).source == _source()


def test_set_candidates_roundtrips(store):
    job_id = store.create_job("u")
    track_id = store.add_track(job_id, _source())
    candidates = [Candidate(meta=_meta(), score=0.97), Candidate(meta=_meta("別首"), score=0.4)]
    store.set_candidates(track_id, candidates)
    row = store.get_track(track_id)
    assert row.status == TrackStatus.AWAITING_CONFIRM
    assert [c.score for c in row.candidates] == [0.97, 0.4]
    assert row.candidates[0].meta == _meta()


def test_confirm_sets_chosen_meta(store):
    job_id = store.create_job("u")
    track_id = store.add_track(job_id, _source())
    store.confirm(track_id, _meta())
    row = store.get_track(track_id)
    assert row.chosen == _meta()
    assert row.status == TrackStatus.DOWNLOADING


def test_set_status_records_error(store):
    job_id = store.create_job("u")
    track_id = store.add_track(job_id, _source())
    store.set_status(track_id, TrackStatus.FAILED, error="影片已下架")
    row = store.get_track(track_id)
    assert row.status == TrackStatus.FAILED
    assert row.error == "影片已下架"


def test_status_transition_clears_previous_error(store):
    job_id = store.create_job("u")
    track_id = store.add_track(job_id, _source())
    store.set_status(track_id, TrackStatus.FAILED, error="暫時性錯誤")
    store.set_status(track_id, TrackStatus.PENDING)
    assert store.get_track(track_id).error is None


def test_find_by_video_id_for_dedup(store):
    job_id = store.create_job("u")
    store.add_track(job_id, _source("dup"))
    assert [r.video_id for r in store.find_by_video_id("dup")] == ["dup"]
    assert store.find_by_video_id("nope") == []


def test_list_jobs_newest_first(store):
    first = store.create_job("u1")
    second = store.create_job("u2")
    assert [j.id for j in store.list_jobs()] == [second, first]


def test_tracks_keep_insertion_order(store):
    job_id = store.create_job("u")
    for i in range(5):
        store.add_track(job_id, _source(f"v{i}"))
    assert [t.video_id for t in store.get_job(job_id).tracks] == [f"v{i}" for i in range(5)]


def test_get_track_missing_returns_none(store):
    assert store.get_track(99999) is None


def test_schema_is_idempotent(tmp_path):
    s = JobStore(tmp_path / "jobs.db")
    s.init_schema()
    s.init_schema()
    s.close()
```

- [ ] **Step 2: 執行測試確認失敗**

Run: `.venv/Scripts/python.exe -m pytest tests/test_store.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.jobs'`

- [ ] **Step 3: 實作 `app/jobs/store.py`**

```python
"""任務與曲目狀態的 SQLite 持久層。

只有兩張表，用 stdlib sqlite3 就夠 —— 不引入 ORM。
候選與已確認的 metadata 以 JSON 存欄位，因為它們是整包讀寫、從不被查詢。
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path

from app.models import Candidate, SourceTrack, TrackMeta

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    url        TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tracks (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id     INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    video_id   TEXT NOT NULL,
    source     TEXT NOT NULL,
    status     TEXT NOT NULL,
    candidates TEXT NOT NULL DEFAULT '[]',
    chosen     TEXT,
    error      TEXT
);

CREATE INDEX IF NOT EXISTS idx_tracks_job ON tracks(job_id);
CREATE INDEX IF NOT EXISTS idx_tracks_video ON tracks(video_id);
"""


class TrackStatus(StrEnum):
    PENDING = "pending"
    MATCHING = "matching"
    AWAITING_CONFIRM = "awaiting_confirm"
    DOWNLOADING = "downloading"
    TAGGING = "tagging"
    DONE = "done"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass(frozen=True, slots=True)
class TrackRow:
    id: int
    job_id: int
    video_id: str
    source: SourceTrack
    status: TrackStatus
    candidates: list[Candidate]
    chosen: TrackMeta | None
    error: str | None


@dataclass(frozen=True, slots=True)
class JobRow:
    id: int
    url: str
    created_at: str
    tracks: list[TrackRow]


def _meta_from_dict(data: dict) -> TrackMeta:
    data = dict(data)
    data["artists"] = tuple(data.get("artists", ()))
    return TrackMeta(**data)


def _dump_meta(meta: TrackMeta) -> str:
    payload = asdict(meta)
    payload["artists"] = list(meta.artists)
    return json.dumps(payload, ensure_ascii=False)


class JobStore:
    def __init__(self, db_path: Path) -> None:
        self._path = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        # 背景 thread pool 會共用同一個連線，故關閉 thread 檢查並自行序列化寫入
        self._conn = sqlite3.connect(db_path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute("PRAGMA journal_mode = WAL")

    def init_schema(self) -> None:
        self._conn.executescript(_SCHEMA)

    def close(self) -> None:
        self._conn.close()

    # --- 寫入 ---

    def create_job(self, url: str) -> int:
        cursor = self._conn.execute(
            "INSERT INTO jobs (url, created_at) VALUES (?, ?)",
            (url, datetime.now(timezone.utc).isoformat()),
        )
        return int(cursor.lastrowid)

    def add_track(self, job_id: int, source: SourceTrack) -> int:
        cursor = self._conn.execute(
            "INSERT INTO tracks (job_id, video_id, source, status) VALUES (?, ?, ?, ?)",
            (
                job_id,
                source.video_id,
                json.dumps(asdict(source), ensure_ascii=False),
                TrackStatus.PENDING.value,
            ),
        )
        return int(cursor.lastrowid)

    def set_candidates(self, track_id: int, candidates: list[Candidate]) -> None:
        payload = json.dumps(
            [{"meta": json.loads(_dump_meta(c.meta)), "score": c.score} for c in candidates],
            ensure_ascii=False,
        )
        self._conn.execute(
            "UPDATE tracks SET candidates = ?, status = ?, error = NULL WHERE id = ?",
            (payload, TrackStatus.AWAITING_CONFIRM.value, track_id),
        )

    def confirm(self, track_id: int, meta: TrackMeta) -> None:
        self._conn.execute(
            "UPDATE tracks SET chosen = ?, status = ?, error = NULL WHERE id = ?",
            (_dump_meta(meta), TrackStatus.DOWNLOADING.value, track_id),
        )

    def set_status(self, track_id: int, status: TrackStatus, error: str | None = None) -> None:
        self._conn.execute(
            "UPDATE tracks SET status = ?, error = ? WHERE id = ?",
            (status.value, error, track_id),
        )

    # --- 讀取 ---

    def _row_to_track(self, row: sqlite3.Row) -> TrackRow:
        candidates = [
            Candidate(meta=_meta_from_dict(item["meta"]), score=float(item["score"]))
            for item in json.loads(row["candidates"])
        ]
        chosen = _meta_from_dict(json.loads(row["chosen"])) if row["chosen"] else None
        return TrackRow(
            id=row["id"],
            job_id=row["job_id"],
            video_id=row["video_id"],
            source=SourceTrack(**json.loads(row["source"])),
            status=TrackStatus(row["status"]),
            candidates=candidates,
            chosen=chosen,
            error=row["error"],
        )

    def get_track(self, track_id: int) -> TrackRow | None:
        row = self._conn.execute("SELECT * FROM tracks WHERE id = ?", (track_id,)).fetchone()
        return self._row_to_track(row) if row else None

    def get_job(self, job_id: int) -> JobRow | None:
        job = self._conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if job is None:
            return None
        rows = self._conn.execute(
            "SELECT * FROM tracks WHERE job_id = ? ORDER BY id", (job_id,)
        ).fetchall()
        return JobRow(
            id=job["id"],
            url=job["url"],
            created_at=job["created_at"],
            tracks=[self._row_to_track(r) for r in rows],
        )

    def list_jobs(self) -> list[JobRow]:
        ids = [r["id"] for r in self._conn.execute("SELECT id FROM jobs ORDER BY id DESC")]
        return [job for job in (self.get_job(i) for i in ids) if job is not None]

    def find_by_video_id(self, video_id: str) -> list[TrackRow]:
        rows = self._conn.execute(
            "SELECT * FROM tracks WHERE video_id = ? ORDER BY id", (video_id,)
        ).fetchall()
        return [self._row_to_track(r) for r in rows]
```

`app/jobs/__init__.py` 建為空檔。

- [ ] **Step 4: 執行測試確認通過**

Run: `.venv/Scripts/python.exe -m pytest tests/test_store.py -v`
Expected: PASS，11 passed

- [ ] **Step 5: Commit**

```bash
git add app/jobs/ tests/test_store.py
git commit -m "feat: SQLite 任務儲存與曲目狀態機"
```

---

## Task 10: Pipeline 串接

**Files:**
- Create: `app/pipeline.py`
- Create: `tests/test_pipeline.py`

**Interfaces:**
- Consumes: 全部先前模組
- Produces:
  - `Pipeline(store, roots, *, workdir, client_factory=musicbrainz.make_client, probe_fn=youtube.probe, download_fn=youtube.download_streams, cover_fn=musicbrainz.fetch_cover, search_fn=musicbrainz.search, write_fn=tagging.write_tags)`
  - `Pipeline.submit(url: str) -> int`（建 job，probe，比對，狀態停在 `awaiting_confirm`）
  - `Pipeline.finalize(track_id: int) -> None`（下載、寫標籤、落檔）
  - `fallback_meta(source: SourceTrack) -> TrackMeta`

- [ ] **Step 1: 寫失敗測試 `tests/test_pipeline.py`**

```python
from pathlib import Path

import httpx
import pytest

from app.config import LibraryRoots
from app.jobs.store import JobStore, TrackStatus
from app.models import SourceTrack, TrackMeta
from app.pipeline import Pipeline, fallback_meta
from app.sources.youtube import DownloadedStreams


def _source(video_id="v1") -> SourceTrack:
    return SourceTrack(
        video_id=video_id,
        url=f"https://music.youtube.com/watch?v={video_id}",
        raw_title="【MV】指尖笑 - 人間驚鴻宴",
        duration=207,
        artist="指尖笑",
        track="人間驚鴻宴",
        album=None,
        release_year=None,
    )


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
        cover_url="https://i.kfs.io/x/cover.jpg",
        duration=207,
        source_url=None,
    )
    base.update(over)
    return TrackMeta(**base)


@pytest.fixture
def pipeline(tmp_path):
    store = JobStore(tmp_path / "jobs.db")
    store.init_schema()
    roots = LibraryRoots(music=tmp_path / "Music", archive=tmp_path / "Archive")

    def fake_download(video_id, workdir, **kwargs):
        workdir.mkdir(parents=True, exist_ok=True)
        m4a = workdir / f"{video_id}.m4a"
        opus = workdir / f"{video_id}.opus"
        m4a.write_bytes(b"m4a")
        opus.write_bytes(b"opus")
        return DownloadedStreams(m4a=m4a, opus=opus)

    written = []
    pipe = Pipeline(
        store,
        roots,
        workdir=tmp_path / "work",
        client_factory=lambda: httpx.Client(
            transport=httpx.MockTransport(lambda r: httpx.Response(200, content=b"\xff\xd8jpeg"))
        ),
        probe_fn=lambda url: [_source()],
        search_fn=lambda q, client: [_meta()],
        download_fn=fake_download,
        cover_fn=lambda url, client: b"\xff\xd8jpeg",
        write_fn=lambda path, meta, cover=None: written.append((path, meta, cover)),
    )
    pipe.written = written
    yield pipe
    store.close()


def test_fallback_meta_uses_youtube_fields():
    meta = fallback_meta(_source())
    assert meta.title == "人間驚鴻宴"
    assert meta.artists == ("指尖笑",)
    assert meta.album == "人間驚鴻宴"


def test_fallback_meta_parses_dirty_title():
    source = SourceTrack(
        video_id="v",
        url="u",
        raw_title="【Official MV】指尖笑 - 人間驚鴻宴",
        duration=None,
        artist=None,
        track=None,
        album=None,
        release_year=None,
    )
    meta = fallback_meta(source)
    assert "人間驚鴻宴" in meta.title


def test_submit_stops_at_awaiting_confirm(pipeline):
    job_id = pipeline.submit("https://music.youtube.com/watch?v=v1")
    job = pipeline.store.get_job(job_id)
    assert len(job.tracks) == 1
    track = job.tracks[0]
    assert track.status == TrackStatus.AWAITING_CONFIRM
    assert track.candidates
    assert track.candidates[0].meta.title == "人間驚鴻宴"


def test_submit_falls_back_when_musicbrainz_empty(tmp_path, pipeline):
    pipeline._search_fn = lambda q, client: []
    job_id = pipeline.submit("https://music.youtube.com/watch?v=v1")
    track = pipeline.store.get_job(job_id).tracks[0]
    assert track.status == TrackStatus.AWAITING_CONFIRM
    assert len(track.candidates) == 1
    assert track.candidates[0].score == 0.0


def test_submit_skips_duplicate_video_id(pipeline):
    pipeline.submit("https://music.youtube.com/watch?v=v1")
    job_id = pipeline.submit("https://music.youtube.com/watch?v=v1")
    track = pipeline.store.get_job(job_id).tracks[0]
    assert track.status == TrackStatus.SKIPPED


def test_submit_marks_failed_on_probe_error(pipeline):
    def boom(url):
        raise RuntimeError("影片已下架")

    pipeline._probe_fn = boom
    job_id = pipeline.submit("https://music.youtube.com/watch?v=v1")
    job = pipeline.store.get_job(job_id)
    assert job.tracks == []


def test_finalize_writes_both_files_and_cover(pipeline, tmp_path):
    job_id = pipeline.submit("https://music.youtube.com/watch?v=v1")
    track = pipeline.store.get_job(job_id).tracks[0]
    pipeline.store.confirm(track.id, _meta())
    pipeline.finalize(track.id)

    final = pipeline.store.get_track(track.id)
    assert final.status == TrackStatus.DONE
    assert (tmp_path / "Music" / "指尖笑" / "人間驚鴻宴" / "01 人間驚鴻宴.m4a").exists()
    assert (tmp_path / "Archive" / "指尖笑" / "人間驚鴻宴" / "01 人間驚鴻宴.opus").exists()
    assert (tmp_path / "Music" / "指尖笑" / "人間驚鴻宴" / "cover.jpg").exists()
    assert len(pipeline.written) == 2


def test_finalize_requires_confirmation(pipeline):
    job_id = pipeline.submit("https://music.youtube.com/watch?v=v1")
    track = pipeline.store.get_job(job_id).tracks[0]
    with pytest.raises(ValueError, match="尚未確認"):
        pipeline.finalize(track.id)


def test_finalize_marks_failed_on_download_error(pipeline):
    job_id = pipeline.submit("https://music.youtube.com/watch?v=v1")
    track = pipeline.store.get_job(job_id).tracks[0]
    pipeline.store.confirm(track.id, _meta())

    def boom(video_id, workdir, **kwargs):
        raise RuntimeError("網路斷了")

    pipeline._download_fn = boom
    pipeline.finalize(track.id)
    final = pipeline.store.get_track(track.id)
    assert final.status == TrackStatus.FAILED
    assert "網路斷了" in final.error


def test_finalize_survives_cover_failure(pipeline, tmp_path):
    """封面抓不到不該讓整首曲目失敗。"""
    job_id = pipeline.submit("https://music.youtube.com/watch?v=v1")
    track = pipeline.store.get_job(job_id).tracks[0]
    pipeline.store.confirm(track.id, _meta())

    def boom(url, client):
        raise httpx.HTTPError("404")

    pipeline._cover_fn = boom
    pipeline.finalize(track.id)
    assert pipeline.store.get_track(track.id).status == TrackStatus.DONE
    assert all(cover is None for _, _, cover in pipeline.written)
```

- [ ] **Step 2: 執行測試確認失敗**

Run: `.venv/Scripts/python.exe -m pytest tests/test_pipeline.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.pipeline'`

- [ ] **Step 3: 實作 `app/pipeline.py`**

```python
"""把各模組串成完整流程。

分兩階段，中間必須經過人工確認：
  submit()   探測 → 比對 → 停在 awaiting_confirm
  finalize() 下載 → 寫標籤 → 落檔 → done

所有外部相依都透過建構子注入，測試不需觸網。
"""

from __future__ import annotations

import shutil
from pathlib import Path

from app.config import LibraryRoots
from app.jobs.store import JobStore, TrackStatus
from app.matching.matcher import rank_candidates, split_title
from app.models import Candidate, SourceTrack, TrackMeta
from app.sources import musicbrainz, youtube
from app.storage.layout import build_paths
from app.tagging.writer import write_tags


def fallback_meta(source: SourceTrack) -> TrackMeta:
    """MusicBrainz 查無結果時，用 YouTube 自身資料組出堪用標籤。"""
    if source.track:
        title = source.track
        artist = source.artist
    else:
        artist, title = split_title(source.raw_title)
    artist = artist or source.artist or "未知演出者"
    album = source.album or title
    return TrackMeta(
        title=title,
        artists=(artist,),
        album_artist=artist,
        album=album,
        year=source.release_year,
        track_no=None,
        track_total=None,
        genre=None,
        cover_url=None,
        duration=source.duration,
        source_url=source.url,
    )


class Pipeline:
    def __init__(
        self,
        store: JobStore,
        roots: LibraryRoots,
        *,
        workdir: Path,
        client_factory=musicbrainz.make_client,
        probe_fn=youtube.probe,
        search_fn=musicbrainz.search,
        download_fn=youtube.download_streams,
        cover_fn=musicbrainz.fetch_cover,
        write_fn=write_tags,
    ) -> None:
        self.store = store
        self.roots = roots
        self.workdir = workdir
        self._client_factory = client_factory
        self._probe_fn = probe_fn
        self._search_fn = search_fn
        self._download_fn = download_fn
        self._cover_fn = cover_fn
        self._write_fn = write_fn

    def submit(self, url: str) -> int:
        """探測網址並為每首曲目產生候選標籤。回傳 job id。"""
        job_id = self.store.create_job(url)
        try:
            sources = self._probe_fn(url)
        except Exception:
            # probe 失敗時 job 留著但沒有曲目，前端會顯示空任務
            return job_id

        with self._client_factory() as client:
            for source in sources:
                if self.store.find_by_video_id(source.video_id):
                    track_id = self.store.add_track(job_id, source)
                    self.store.set_status(track_id, TrackStatus.SKIPPED, error="已存在，跳過")
                    continue
                track_id = self.store.add_track(job_id, source)
                self._match_one(track_id, source, client)
        return job_id

    def _match_one(self, track_id: int, source: SourceTrack, client) -> None:
        self.store.set_status(track_id, TrackStatus.MATCHING)
        query = " ".join(filter(None, [source.artist, source.track])) or source.raw_title
        try:
            metas = self._search_fn(query, client=client)
        except Exception:
            metas = []

        if metas:
            candidates = rank_candidates(source, metas)
        else:
            # 降級：用 YouTube 自身資料，分數 0 代表未經 MusicBrainz 比對
            candidates = [Candidate(meta=fallback_meta(source), score=0.0)]
        self.store.set_candidates(track_id, candidates)

    def finalize(self, track_id: int) -> None:
        """下載、寫標籤、落檔。呼叫前必須已 confirm。"""
        row = self.store.get_track(track_id)
        if row is None:
            raise ValueError(f"找不到曲目 {track_id}")
        if row.chosen is None:
            raise ValueError(f"曲目 {track_id} 尚未確認標籤")

        meta = row.chosen
        try:
            self.store.set_status(track_id, TrackStatus.DOWNLOADING)
            streams = self._download_fn(row.video_id, self.workdir)

            self.store.set_status(track_id, TrackStatus.TAGGING)
            cover = self._fetch_cover(meta)
            paths = build_paths(self.roots, meta)
            paths.m4a.parent.mkdir(parents=True, exist_ok=True)
            paths.opus.parent.mkdir(parents=True, exist_ok=True)

            shutil.move(str(streams.m4a), paths.m4a)
            shutil.move(str(streams.opus), paths.opus)
            self._write_fn(paths.m4a, meta, cover)
            self._write_fn(paths.opus, meta, cover)
            if cover and not paths.cover.exists():
                paths.cover.write_bytes(cover)

            self.store.set_status(track_id, TrackStatus.DONE)
        except Exception as exc:
            self.store.set_status(track_id, TrackStatus.FAILED, error=str(exc))

    def _fetch_cover(self, meta: TrackMeta) -> bytes | None:
        """封面失敗不算致命錯誤 —— 沒圖的歌還是要能收藏。"""
        if not meta.cover_url:
            return None
        try:
            with self._client_factory() as client:
                return self._cover_fn(meta.cover_url, client=client)
        except Exception:
            return None
```

- [ ] **Step 4: 執行測試確認通過**

Run: `.venv/Scripts/python.exe -m pytest tests/test_pipeline.py -v`
Expected: PASS，10 passed

- [ ] **Step 5: 全套測試回歸**

Run: `.venv/Scripts/python.exe -m pytest -v`
Expected: 全數 PASS

- [ ] **Step 6: Commit**

```bash
git add app/pipeline.py tests/test_pipeline.py
git commit -m "feat: pipeline 串接探測、比對、下載、標籤與落檔"
```

---

## Task 11: HTTP API 與 SSE

**Files:**
- Create: `app/api/__init__.py`
- Create: `app/api/routes.py`
- Create: `app/main.py`
- Create: `tests/test_api.py`

**Interfaces:**
- Consumes: `app.pipeline.Pipeline`、`app.jobs.store.JobStore`
- Produces:
  - `create_app(pipeline: Pipeline) -> FastAPI`
  - 端點：
    - `POST /api/jobs` body `{"urls": ["..."]}` → `{"job_ids": [1, 2]}`
    - `GET /api/jobs` → `{"jobs": [...]}`
    - `GET /api/jobs/{job_id}` → job 詳情
    - `POST /api/tracks/{track_id}/confirm` body `{"meta": {...}}` → `{"status": "downloading"}`
    - `POST /api/tracks/{track_id}/skip` → `{"status": "skipped"}`
    - `GET /api/jobs/{job_id}/events` → SSE，每秒推一次 job 狀態，全部曲目終止後關閉

- [ ] **Step 1: 寫失敗測試 `tests/test_api.py`**

```python
import httpx
import pytest
from fastapi import FastAPI

from app.api.routes import create_app
from app.config import LibraryRoots
from app.jobs.store import JobStore, TrackStatus
from app.models import SourceTrack, TrackMeta
from app.pipeline import Pipeline
from app.sources.youtube import DownloadedStreams

META_PAYLOAD = {
    "title": "人間驚鴻宴",
    "artists": ["指尖笑"],
    "album_artist": "指尖笑",
    "album": "人間驚鴻宴",
    "year": 2026,
    "track_no": 1,
    "track_total": 1,
    "genre": None,
    "cover_url": None,
    "duration": 207,
    "source_url": None,
}


def _source() -> SourceTrack:
    return SourceTrack(
        video_id="v1",
        url="https://music.youtube.com/watch?v=v1",
        raw_title="指尖笑 - 人間驚鴻宴",
        duration=207,
        artist="指尖笑",
        track="人間驚鴻宴",
        album=None,
        release_year=None,
    )


@pytest.fixture
def app_and_pipeline(tmp_path):
    store = JobStore(tmp_path / "jobs.db")
    store.init_schema()
    roots = LibraryRoots(music=tmp_path / "Music", archive=tmp_path / "Archive")

    def fake_download(video_id, workdir, **kwargs):
        workdir.mkdir(parents=True, exist_ok=True)
        m4a = workdir / f"{video_id}.m4a"
        opus = workdir / f"{video_id}.opus"
        m4a.write_bytes(b"m4a")
        opus.write_bytes(b"opus")
        return DownloadedStreams(m4a=m4a, opus=opus)

    pipeline = Pipeline(
        store,
        roots,
        workdir=tmp_path / "work",
        client_factory=lambda: httpx.Client(transport=httpx.MockTransport(
            lambda r: httpx.Response(200, content=b"")
        )),
        probe_fn=lambda url: [_source()],
        search_fn=lambda q, client: [],
        download_fn=fake_download,
        cover_fn=lambda url, client: b"",
        write_fn=lambda path, meta, cover=None: None,
    )
    app = create_app(pipeline)
    yield app, pipeline
    store.close()


@pytest.fixture
async def client(app_and_pipeline):
    app, _ = app_and_pipeline
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def test_create_app_returns_fastapi(app_and_pipeline):
    assert isinstance(app_and_pipeline[0], FastAPI)


async def test_post_jobs_creates_job(client):
    response = await client.post(
        "/api/jobs", json={"urls": ["https://music.youtube.com/watch?v=v1"]}
    )
    assert response.status_code == 200
    assert len(response.json()["job_ids"]) == 1


async def test_post_jobs_accepts_multiple_urls(client):
    response = await client.post(
        "/api/jobs",
        json={"urls": [
            "https://music.youtube.com/watch?v=v1",
            "https://music.youtube.com/watch?v=v2",
        ]},
    )
    assert len(response.json()["job_ids"]) == 2


async def test_post_jobs_rejects_empty_list(client):
    assert (await client.post("/api/jobs", json={"urls": []})).status_code == 422


async def test_get_job_returns_tracks_with_candidates(client):
    job_id = (await client.post(
        "/api/jobs", json={"urls": ["https://music.youtube.com/watch?v=v1"]}
    )).json()["job_ids"][0]

    body = (await client.get(f"/api/jobs/{job_id}")).json()
    assert body["id"] == job_id
    assert len(body["tracks"]) == 1
    track = body["tracks"][0]
    assert track["status"] == TrackStatus.AWAITING_CONFIRM.value
    assert track["candidates"][0]["meta"]["title"] == "人間驚鴻宴"


async def test_get_job_404(client):
    assert (await client.get("/api/jobs/99999")).status_code == 404


async def test_confirm_track_triggers_finalize(client, app_and_pipeline):
    _, pipeline = app_and_pipeline
    job_id = (await client.post(
        "/api/jobs", json={"urls": ["https://music.youtube.com/watch?v=v1"]}
    )).json()["job_ids"][0]
    track_id = pipeline.store.get_job(job_id).tracks[0].id

    response = await client.post(f"/api/tracks/{track_id}/confirm", json={"meta": META_PAYLOAD})
    assert response.status_code == 200

    # 背景工作完成後狀態應為 done
    await _wait_for(pipeline, track_id, TrackStatus.DONE)


async def test_confirm_rejects_bad_meta(client, app_and_pipeline):
    _, pipeline = app_and_pipeline
    job_id = (await client.post(
        "/api/jobs", json={"urls": ["https://music.youtube.com/watch?v=v1"]}
    )).json()["job_ids"][0]
    track_id = pipeline.store.get_job(job_id).tracks[0].id
    bad = {k: v for k, v in META_PAYLOAD.items() if k != "title"}
    assert (await client.post(
        f"/api/tracks/{track_id}/confirm", json={"meta": bad}
    )).status_code == 422


async def test_skip_track(client, app_and_pipeline):
    _, pipeline = app_and_pipeline
    job_id = (await client.post(
        "/api/jobs", json={"urls": ["https://music.youtube.com/watch?v=v1"]}
    )).json()["job_ids"][0]
    track_id = pipeline.store.get_job(job_id).tracks[0].id

    assert (await client.post(f"/api/tracks/{track_id}/skip")).status_code == 200
    assert pipeline.store.get_track(track_id).status == TrackStatus.SKIPPED


async def test_confirm_404(client):
    assert (await client.post(
        "/api/tracks/99999/confirm", json={"meta": META_PAYLOAD}
    )).status_code == 404


async def _wait_for(pipeline, track_id, status, timeout=5.0):
    import asyncio

    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if pipeline.store.get_track(track_id).status == status:
            return
        await asyncio.sleep(0.05)
    raise AssertionError(
        f"曲目 {track_id} 未在時限內到達 {status}，"
        f"目前為 {pipeline.store.get_track(track_id).status}"
    )
```

- [ ] **Step 2: 執行測試確認失敗**

Run: `.venv/Scripts/python.exe -m pytest tests/test_api.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.api'`

- [ ] **Step 3: 實作 `app/api/routes.py`**

```python
"""HTTP 端點。只做驗證與回應塑形，業務邏輯全在 Pipeline。"""

from __future__ import annotations

import asyncio
import json

from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.jobs.store import JobRow, TrackRow, TrackStatus
from app.models import TrackMeta
from app.pipeline import Pipeline

TERMINAL_STATUSES = {TrackStatus.DONE, TrackStatus.FAILED, TrackStatus.SKIPPED}
SSE_INTERVAL_SECONDS = 1.0


class SubmitRequest(BaseModel):
    urls: list[str] = Field(min_length=1)


class MetaPayload(BaseModel):
    title: str
    artists: list[str]
    album_artist: str
    album: str
    year: int | None = None
    track_no: int | None = None
    track_total: int | None = None
    genre: str | None = None
    cover_url: str | None = None
    duration: int | None = None
    source_url: str | None = None

    def to_meta(self) -> TrackMeta:
        return TrackMeta(
            title=self.title,
            artists=tuple(self.artists),
            album_artist=self.album_artist,
            album=self.album,
            year=self.year,
            track_no=self.track_no,
            track_total=self.track_total,
            genre=self.genre,
            cover_url=self.cover_url,
            duration=self.duration,
            source_url=self.source_url,
        )


class ConfirmRequest(BaseModel):
    meta: MetaPayload


def _meta_dict(meta: TrackMeta) -> dict:
    return {
        "title": meta.title,
        "artists": list(meta.artists),
        "album_artist": meta.album_artist,
        "album": meta.album,
        "year": meta.year,
        "track_no": meta.track_no,
        "track_total": meta.track_total,
        "genre": meta.genre,
        "cover_url": meta.cover_url,
        "duration": meta.duration,
        "source_url": meta.source_url,
    }


def _track_dict(track: TrackRow) -> dict:
    return {
        "id": track.id,
        "video_id": track.video_id,
        "raw_title": track.source.raw_title,
        "youtube_url": track.source.url,
        "duration": track.source.duration,
        "status": track.status.value,
        "error": track.error,
        "candidates": [
            {"score": round(c.score, 4), "meta": _meta_dict(c.meta)} for c in track.candidates
        ],
        "chosen": _meta_dict(track.chosen) if track.chosen else None,
    }


def _job_dict(job: JobRow) -> dict:
    return {
        "id": job.id,
        "url": job.url,
        "created_at": job.created_at,
        "tracks": [_track_dict(t) for t in job.tracks],
    }


def create_app(pipeline: Pipeline) -> FastAPI:
    app = FastAPI(title="音樂下載工具")

    @app.post("/api/jobs")
    async def submit_jobs(request: SubmitRequest) -> dict:
        # submit 會打網路，丟到 thread 避免卡住事件迴圈
        job_ids = [
            await asyncio.to_thread(pipeline.submit, url) for url in request.urls
        ]
        return {"job_ids": job_ids}

    @app.get("/api/jobs")
    async def list_jobs() -> dict:
        return {"jobs": [_job_dict(j) for j in pipeline.store.list_jobs()]}

    @app.get("/api/jobs/{job_id}")
    async def get_job(job_id: int) -> dict:
        job = pipeline.store.get_job(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="找不到任務")
        return _job_dict(job)

    @app.post("/api/tracks/{track_id}/confirm")
    async def confirm_track(track_id: int, request: ConfirmRequest) -> dict:
        if pipeline.store.get_track(track_id) is None:
            raise HTTPException(status_code=404, detail="找不到曲目")
        pipeline.store.confirm(track_id, request.meta.to_meta())
        # 下載在背景跑，立刻回應讓前端可以繼續確認下一首
        asyncio.create_task(asyncio.to_thread(pipeline.finalize, track_id))
        return {"status": TrackStatus.DOWNLOADING.value}

    @app.post("/api/tracks/{track_id}/skip")
    async def skip_track(track_id: int) -> dict:
        if pipeline.store.get_track(track_id) is None:
            raise HTTPException(status_code=404, detail="找不到曲目")
        pipeline.store.set_status(track_id, TrackStatus.SKIPPED)
        return {"status": TrackStatus.SKIPPED.value}

    @app.get("/api/jobs/{job_id}/events")
    async def job_events(job_id: int) -> StreamingResponse:
        if pipeline.store.get_job(job_id) is None:
            raise HTTPException(status_code=404, detail="找不到任務")

        async def stream():
            while True:
                job = pipeline.store.get_job(job_id)
                if job is None:
                    break
                payload = json.dumps(_job_dict(job), ensure_ascii=False)
                yield f"data: {payload}\n\n"
                if job.tracks and all(t.status in TERMINAL_STATUSES for t in job.tracks):
                    break
                await asyncio.sleep(SSE_INTERVAL_SECONDS)

        return StreamingResponse(stream(), media_type="text/event-stream")

    return app
```

`app/api/__init__.py` 建為空檔。

- [ ] **Step 4: 實作 `app/main.py`**

```python
"""應用程式進入點。啟動前檢查 ffmpeg，掛載前端靜態檔。"""

from __future__ import annotations

from pathlib import Path

from fastapi.staticfiles import StaticFiles

from app.api.routes import create_app
from app.config import load_roots, require_ffmpeg
from app.jobs.store import JobStore
from app.pipeline import Pipeline

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR.parent / "jobs.db"
WORKDIR = BASE_DIR.parent / "downloads"


def build() -> "FastAPI":  # noqa: F821
    require_ffmpeg()  # 缺 ffmpeg 就直接在啟動時炸，不要等下載到一半
    store = JobStore(DB_PATH)
    store.init_schema()
    pipeline = Pipeline(store, load_roots(), workdir=WORKDIR)
    application = create_app(pipeline)
    application.mount("/", StaticFiles(directory=BASE_DIR / "web", html=True), name="web")
    return application


app = build()
```

- [ ] **Step 5: 執行測試確認通過**

Run: `.venv/Scripts/python.exe -m pytest tests/test_api.py -v`
Expected: PASS，10 passed

- [ ] **Step 6: Commit**

```bash
git add app/api/ app/main.py tests/test_api.py
git commit -m "feat: HTTP API 與 SSE 進度推播"
```

---

## Task 12: 前端頁面

**Files:**
- Create: `app/web/index.html`
- Create: `app/web/style.css`
- Create: `app/web/app.js`

**Interfaces:**
- Consumes: Task 11 的 API 端點
- Produces: 可操作的網頁介面

- [ ] **Step 1: 建立 `app/web/index.html`**

```html
<!DOCTYPE html>
<html lang="zh-Hant">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>音樂下載工具</title>
  <link rel="stylesheet" href="/style.css">
</head>
<body>
  <header>
    <h1>音樂下載工具</h1>
    <p class="hint">貼上 YouTube Music 網址，一行一個。標籤由 MusicBrainz 公開資料補齊，下載前需確認。</p>
  </header>

  <section class="submit">
    <textarea id="urls" rows="4" placeholder="https://music.youtube.com/watch?v=...
https://music.youtube.com/playlist?list=OLAK5uy_..."></textarea>
    <button id="submit">送出</button>
    <span id="submit-status"></span>
  </section>

  <section id="jobs"></section>

  <template id="track-template">
    <article class="track">
      <div class="track-head">
        <span class="raw-title"></span>
        <span class="status"></span>
      </div>
      <div class="candidates"></div>
      <div class="actions">
        <button class="confirm">確認並下載</button>
        <button class="skip">跳過</button>
      </div>
      <p class="error"></p>
    </article>
  </template>

  <script src="/app.js"></script>
</body>
</html>
```

- [ ] **Step 2: 建立 `app/web/style.css`**

```css
:root {
  color-scheme: light dark;
  --gap: 12px;
  --border: color-mix(in srgb, currentColor 20%, transparent);
}

body {
  font-family: "Segoe UI", "Microsoft JhengHei", system-ui, sans-serif;
  max-width: 900px;
  margin: 0 auto;
  padding: 24px 16px 64px;
  line-height: 1.6;
}

.hint { opacity: 0.7; font-size: 0.9em; }

textarea { width: 100%; font-family: inherit; padding: 8px; }

button {
  padding: 6px 14px;
  cursor: pointer;
  border: 1px solid var(--border);
  border-radius: 4px;
  background: transparent;
  color: inherit;
}

button:disabled { opacity: 0.4; cursor: default; }

.job { border-top: 1px solid var(--border); margin-top: 24px; padding-top: 12px; }

.track {
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: var(--gap);
  margin-bottom: var(--gap);
}

.track-head { display: flex; justify-content: space-between; gap: var(--gap); }
.raw-title { font-weight: 600; }

.status { font-size: 0.85em; opacity: 0.8; white-space: nowrap; }
.status[data-status="done"] { color: seagreen; }
.status[data-status="failed"] { color: crimson; }
.status[data-status="skipped"] { opacity: 0.5; }

.candidates { display: grid; gap: 8px; margin: var(--gap) 0; }

.candidate {
  display: grid;
  grid-template-columns: 56px 1fr auto;
  gap: var(--gap);
  align-items: center;
  padding: 6px;
  border: 1px solid transparent;
  border-radius: 4px;
  cursor: pointer;
}

.candidate:hover { border-color: var(--border); }
.candidate.selected { border-color: currentColor; }
.candidate img { width: 56px; height: 56px; object-fit: cover; border-radius: 3px; }
.candidate .fields { font-size: 0.9em; }
.candidate .score { font-variant-numeric: tabular-nums; font-size: 0.85em; }
.candidate .score.high { color: seagreen; font-weight: 600; }
.candidate .score.low { color: darkorange; }

.actions { display: flex; gap: 8px; }
.error { color: crimson; font-size: 0.85em; margin: 4px 0 0; }
.error:empty { display: none; }
```

- [ ] **Step 3: 建立 `app/web/app.js`**

```javascript
"use strict";

// 與後端 app/matching/matcher.py 的 HIGH_CONFIDENCE 一致，僅用於視覺標示
const HIGH_CONFIDENCE = 0.92;

const jobsEl = document.getElementById("jobs");
const submitBtn = document.getElementById("submit");
const urlsEl = document.getElementById("urls");
const submitStatus = document.getElementById("submit-status");

// track id -> 使用者選中的候選索引
const selections = new Map();
const openStreams = new Map();

submitBtn.addEventListener("click", async () => {
  const urls = urlsEl.value.split("\n").map((s) => s.trim()).filter(Boolean);
  if (urls.length === 0) return;

  submitBtn.disabled = true;
  submitStatus.textContent = "探測中，請稍候…";
  try {
    const response = await fetch("/api/jobs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ urls }),
    });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const { job_ids: jobIds } = await response.json();
    submitStatus.textContent = `已建立 ${jobIds.length} 個任務`;
    urlsEl.value = "";
    jobIds.forEach(watchJob);
  } catch (err) {
    submitStatus.textContent = `送出失敗：${err.message}`;
  } finally {
    submitBtn.disabled = false;
  }
});

function watchJob(jobId) {
  openStreams.get(jobId)?.close();
  const source = new EventSource(`/api/jobs/${jobId}/events`);
  openStreams.set(jobId, source);
  source.onmessage = (event) => renderJob(JSON.parse(event.data));
  source.onerror = () => {
    source.close();
    openStreams.delete(jobId);
  };
}

function renderJob(job) {
  let section = document.getElementById(`job-${job.id}`);
  if (!section) {
    section = document.createElement("div");
    section.id = `job-${job.id}`;
    section.className = "job";
    section.innerHTML = `<h2>任務 #${job.id}</h2><div class="tracks"></div>`;
    jobsEl.prepend(section);
  }
  const container = section.querySelector(".tracks");
  container.replaceChildren(...job.tracks.map(renderTrack));
}

function renderTrack(track) {
  const node = document.getElementById("track-template").content.cloneNode(true);
  const article = node.querySelector(".track");
  article.dataset.trackId = track.id;

  article.querySelector(".raw-title").textContent = track.raw_title;
  const statusEl = article.querySelector(".status");
  statusEl.textContent = statusLabel(track.status);
  statusEl.dataset.status = track.status;
  article.querySelector(".error").textContent = track.error || "";

  const candidatesEl = article.querySelector(".candidates");
  const selected = selections.get(track.id) ?? 0;
  track.candidates.forEach((candidate, index) => {
    candidatesEl.append(renderCandidate(track.id, candidate, index, index === selected));
  });

  const editable = track.status === "awaiting_confirm";
  const confirmBtn = article.querySelector(".confirm");
  const skipBtn = article.querySelector(".skip");
  confirmBtn.disabled = !editable || track.candidates.length === 0;
  skipBtn.disabled = !editable;
  confirmBtn.addEventListener("click", () => confirmTrack(track, selections.get(track.id) ?? 0));
  skipBtn.addEventListener("click", () => skipTrack(track.id));

  return node;
}

function renderCandidate(trackId, candidate, index, isSelected) {
  const meta = candidate.meta;
  const div = document.createElement("div");
  div.className = "candidate" + (isSelected ? " selected" : "");
  div.addEventListener("click", () => {
    selections.set(trackId, index);
    div.parentElement.querySelectorAll(".candidate").forEach((el) => {
      el.classList.remove("selected");
    });
    div.classList.add("selected");
  });

  const cover = document.createElement("img");
  cover.alt = "";
  cover.src = meta.cover_url || "data:image/gif;base64,R0lGODlhAQABAAAAACw=";
  div.append(cover);

  const fields = document.createElement("div");
  fields.className = "fields";
  const trackNo = meta.track_no ? `${String(meta.track_no).padStart(2, "0")}. ` : "";
  fields.innerHTML = [
    `<div><strong>${escapeHtml(trackNo + meta.title)}</strong></div>`,
    `<div>${escapeHtml(meta.artists.join("; "))}</div>`,
    `<div>${escapeHtml(meta.album)}${meta.year ? ` · ${meta.year}` : ""}</div>`,
  ].join("");
  div.append(fields);

  const score = document.createElement("span");
  score.className = "score " + (candidate.score >= HIGH_CONFIDENCE ? "high" : "low");
  score.textContent = candidate.score > 0
    ? `${Math.round(candidate.score * 100)}%`
    : "未配對";
  div.append(score);

  return div;
}

async function confirmTrack(track, index) {
  const meta = track.candidates[index]?.meta;
  if (!meta) return;
  await fetch(`/api/tracks/${track.id}/confirm`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ meta }),
  });
}

async function skipTrack(trackId) {
  await fetch(`/api/tracks/${trackId}/skip`, { method: "POST" });
}

function statusLabel(status) {
  return {
    pending: "等待中",
    matching: "比對中",
    awaiting_confirm: "待確認",
    downloading: "下載中",
    tagging: "寫入標籤",
    done: "完成",
    failed: "失敗",
    skipped: "已跳過",
  }[status] || status;
}

function escapeHtml(text) {
  const div = document.createElement("div");
  div.textContent = text ?? "";
  return div.innerHTML;
}

// 重新整理後接回既有任務
fetch("/api/jobs")
  .then((r) => r.json())
  .then(({ jobs }) => {
    jobs.forEach((job) => {
      renderJob(job);
      const active = job.tracks.some(
        (t) => !["done", "failed", "skipped"].includes(t.status)
      );
      if (active) watchJob(job.id);
    });
  })
  .catch(() => {});
```

- [ ] **Step 4: 啟動並人工驗收**

```bash
.venv/Scripts/python.exe -m uvicorn app.main:app --port 8765
```

開 `http://127.0.0.1:8765`，貼一個你自己的 YouTube Music 專輯網址，走完整流程，確認：

1. 曲目全數列出，每首有候選標籤與分數
2. 高信心候選顯示綠色百分比，未配對顯示「未配對」
3. 點候選可切換選擇
4. 按「確認並下載」後狀態依序變成 下載中 → 寫入標籤 → 完成
5. `D:\Music\<演出者>\<專輯>\` 出現 m4a 與 cover.jpg
6. `D:\Archive\<演出者>\<專輯>\` 出現 opus
7. **在檔案總管對 m4a 右鍵 → 內容 → 詳細資料，欄位與截圖一致**

- [ ] **Step 5: 全套測試回歸**

Run: `.venv/Scripts/python.exe -m pytest -v`
Expected: 全數 PASS

- [ ] **Step 6: Commit**

```bash
git add app/web/
git commit -m "feat: 網頁前端與 SSE 進度顯示"
```

---

## Task 13: 使用說明文件

**Files:**
- Create: `README.md`

**Interfaces:**
- Consumes: 全部先前任務
- Produces: 無程式介面

- [ ] **Step 1: 撰寫 `README.md`**

```markdown
# 音樂下載工具

個人本機音樂收藏工具。從 YouTube Music 取得音訊，以 MusicBrainz 公開 metadata 補齊標籤與封面，依演出者／專輯結構落檔。

## 範圍與限制

- 音訊唯一來源為 YouTube Music，用途限個人本機收藏。
- 原設計以 KKBOX 為 metadata 來源，因其專輯與歌曲頁已由 AWS WAF 攔截程式化存取而改用 MusicBrainz。
- metadata 來自 MusicBrainz 公開 API，封面來自 Cover Art Archive。**本工具不解密、不繞過任何 DRM。**
- YouTube Music 沒有無損來源。本工具不重編碼，只做容器 remux。

## 需求

- Python 3.12 以上
- ffmpeg（`winget install Gyan.FFmpeg`）

## 安裝

    python -m venv .venv
    .venv\Scripts\python.exe -m pip install -e ".[dev]"

## 啟動

    .venv\Scripts\python.exe -m uvicorn app.main:app --port 8765

瀏覽器開 http://127.0.0.1:8765

## 設定

| 環境變數 | 預設值 | 說明 |
|---|---|---|
| `MUSIC_ROOT` | `D:\Music` | m4a 輸出根目錄，供播放器掃描 |
| `MUSICBRAINZ_CONTACT` | 未設定 | MusicBrainz 要求 User-Agent 帶聯絡方式，請填自己的 email |
| `ARCHIVE_ROOT` | `D:\Archive` | opus 輸出根目錄，冷存不掃描 |

## 輸出格式

每首歌同時產出兩份，皆不重編碼：

| 位置 | 格式 | 用途 |
|---|---|---|
| `MUSIC_ROOT` | m4a（AAC 128kbps） | 日常播放。Windows 檔案總管、手機、車機原生讀標籤與封面 |
| `ARCHIVE_ROOT` | opus（約 160kbps） | 冷存。來源端 bitrate 較高，但檔案總管不顯示其標籤 |

## 使用流程

1. 貼上網址（支援單曲、專輯、播放清單，一行一個）
2. 系統探測曲目並向 MusicBrainz 查詢候選標籤，停在「待確認」
3. 逐首確認或修改標籤後按「確認並下載」
4. 下載、寫標籤、落檔

已下載過的 videoId 會自動標記為「已跳過」。

## 測試

    .venv\Scripts\python.exe -m pytest -v

測試不對 MusicBrainz 或 YouTube 發出任何請求，全部使用 fixture 與 mock。

## 疑難排解

| 症狀 | 原因與處理 |
|---|---|
| 啟動即報 ffmpeg 錯誤 | ffmpeg 不在 PATH。安裝後需重開終端機 |
| 候選全部顯示「未配對」 | MusicBrainz 查無結果，或未設定 `MUSICBRAINZ_CONTACT` 導致請求被拒。標籤會降級使用 YouTube 自身資料，仍可下載 |
| 檔案總管看不到 opus 的標籤 | 預期行為。改用 foobar2000、MusicBee 或 VLC 檢視 |
| 曲目狀態卡在「失敗」 | 展開錯誤訊息。影片下架或地區限制無法處理，其餘可重新送出網址 |
```

- [ ] **Step 2: Commit**

```bash
git add README.md
git commit -m "docs: 使用說明與疑難排解"
```

---

## Self-Review

**Spec 覆蓋檢查：**

| Spec 章節 | 對應任務 |
|---|---|
| 1 目的與範圍、法律邊界 | Task 7（僅公開 metadata）、Task 13（README 明載） |
| 2 決策紀錄 | 全部任務；格式決策見 Task 6 |
| 3 架構與模組邊界 | Task 1–12 檔案結構逐一對應 |
| 4 資料流與狀態機 | Task 9（狀態機）、Task 10（兩階段流程） |
| 5 比對演算法 | Task 4 |
| 6 標籤欄位對映 | Task 8 |
| 7 檔案落地與檔名淨化 | Task 3、Task 10 |
| 8 錯誤處理（7 項） | Task 1（ffmpeg）、Task 5（下架跳過）、Task 7（MusicBrainz 降級與節流）、Task 10（重複跳過、下載失敗、封面失敗） |
| 9 測試策略 | 每個任務的 TDD 步驟；fixture 見 Task 7、conftest 見 Task 8 |
| 10 YAGNI 清單 | 未出現於任何任務 |
| 11 環境前提 | Task 1 |

**型別一致性檢查：** `TrackMeta` 欄位在 Task 2 定義後，於 Task 3、7、8、9、10、11 使用一致；`artists` 全程為 `tuple[str, ...]`，僅在 JSON 序列化（Task 9、11）與前端（Task 12）轉為 list。`TrackStatus` 值在 Task 9 定義，Task 10、11、12 引用相同字串。`HIGH_CONFIDENCE` 定義於 Task 4，前端 Task 12 的同名常數僅供視覺標示且已註明來源。

**已知需實作時調整之處：** Task 7 的 MusicBrainz 欄位路徑依賴 Step 1 擷取的真實 fixture，計畫刻意不預先猜測回應結構。

---

## Execution Handoff

計畫共 13 個任務，每個任務結束都有可獨立驗證的產出與一次 commit。
