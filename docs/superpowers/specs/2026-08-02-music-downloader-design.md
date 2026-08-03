# 音樂下載與標籤工具 — 設計文件

日期：2026-08-02
狀態：已核可，待撰寫實作計畫

## 1. 目的與範圍

個人本機音樂收藏工具。輸入 YouTube Music 網址，下載音訊，自動寫入完整 metadata（標題、參與演出者、專輯演出者、專輯、年份、曲序、類型、封面），依演出者／專輯結構落檔。

metadata 主要由 MusicBrainz 公開 API 補齊，因為 YouTube Music 自身標籤品質不穩。封面圖取自 Cover Art Archive。

### 法律與技術邊界

- **不解密、不繞過任何 DRM。** 本工具不涉及訂閱制串流平台的音檔。
- 原設計以 KKBOX 為 metadata 來源，實作時發現其專輯與歌曲頁已由 AWS WAF 攔截程式化存取（HTTP 202、`x-amzn-waf-action: challenge`、回應零位元組）。繞過機器人驗證不在選項內，故改用 MusicBrainz。
- 音訊唯一來源為 YouTube Music，透過 yt-dlp 取公開串流，用途限個人本機收藏。
- MusicBrainz 匿名存取速率上限為每秒 1 次請求，且要求 User-Agent 帶應用名稱與聯絡方式，否則會被拒絕。兩者皆為硬性規定。

### 品質前提

YouTube Music **沒有無損來源**。最高為 Opus ~160kbps（itag 251）或 AAC 128kbps（itag 140）。轉為 FLAC 或 MP3 320kbps 不提升品質，僅增加體積並可能二次劣化。因此本工具**預設不重編碼**，只做容器 remux。

## 2. 決策紀錄

| 決策 | 選擇 | 理由 |
|---|---|---|
| metadata 來源 | MusicBrainz + Cover Art Archive | 官方公開 API，專為程式化存取設計；KKBOX 已封鎖爬取 |
| 使用介面 | 本機網頁 GUI | 貼網址、看進度、下載前改標籤 |
| 格式策略 | 保留原始串流，不重編碼 | 有損來源轉檔無益 |
| 抽取串流 | m4a 與 opus 兩條都抓 | m4a 相容 Windows 檔案總管與車機；opus bitrate 較高供冷存 |
| 標籤配對 | 自動猜 + 下載前人工確認 | 字串比對必有誤判，錯標籤不得默默流進收藏 |
| 輸入類型 | 單曲、專輯、播放清單、多行批次 | 全部支援 |
| 目錄結構 | 演出者/專輯/曲序 曲名 | 主流播放器與媒體伺服器通用慣例 |

### 為何 m4a 與 opus 分兩棵樹

同資料夾放同名不同副檔的兩份檔案，會讓播放器與媒體伺服器掃描時每首曲目出現兩次。因此分為 `Music/`（m4a，供掃描）與 `Archive/`（opus，不掃描）。

## 3. 架構

FastAPI 單進程，無外部服務依賴。業務邏輯集中於 service 層，路由僅做驗證與回應塑形。

```
app/
  sources/youtube.py    yt-dlp 包裝：網址分類、抽 metadata、抽串流
  sources/musicbrainz.py  MusicBrainz 查詢與 JSON 對映（欄位路徑集中管理）
  matching/matcher.py   純函式：標題正規化 + 候選評分（無 IO）
  tagging/writer.py     mutagen：MP4Writer / OpusWriter 共用介面
  storage/layout.py     路徑生成、Windows 檔名淨化
  jobs/queue.py         SQLite 任務佇列與狀態機
  api/routes.py         REST 端點 + SSE 進度推播
  web/                  靜態前端（原生 JS，不引入框架）
```

模組邊界原則：

- `matcher` 與 `layout` 為純函式，無 IO，最易測試
- `sources/` 是唯一觸及網路的層
- `tagging/` 對外只暴露一個 `write_tags(path, metadata)` 介面，m4a 與 opus 差異封裝在內
- 背景下載以 `asyncio` + thread pool 執行，狀態存 SQLite

**不採用 Celery/Redis**：單人本機工具，任務量級不需要，多顧兩個服務不划算。

## 4. 資料流

```
貼網址
  → 分類（單曲 / 專輯 / 播放清單 / 多行批次）
  → 建立 job(s)，狀態 pending
  → yt-dlp 只抽 metadata，不下載
  → MusicBrainz 搜尋並比對，產生候選
  → 狀態 awaiting_confirm，暫停
  → 使用者於 GUI 確認或手動修改
  → 下載 m4a 與 opus 兩條串流
  → 寫入標籤
  → 落檔至兩棵目錄樹
  → 狀態 done
```

關鍵設計：**確認發生在下載之前**。避免錯誤標籤先落地再回頭修正。

### 任務狀態機

`pending` → `probing` → `matching` → `awaiting_confirm` → `downloading` → `tagging` → `done`

任何階段可轉入 `failed`，保留錯誤訊息並允許重試。

## 5. 比對演算法

輸入來源優先序：

1. yt-dlp 回傳的 `artist` / `track` / `album` 欄位（YouTube Music 官方曲目通常具備）
2. 前項缺漏時，解析影片標題

標題正規化：

- 剝除 `【】`、`()`、`[]` 內的雜訊詞：Official、MV、4K、Lyric、HD、高音質、官方等
- 全形半形統一
- 大小寫收斂

評分（MusicBrainz 搜尋結果取 top 3 候選）：

```
score = 0.5 × 歌名相似度（RapidFuzz token_set_ratio）
      + 0.3 × 演出者相似度
      + 0.2 × 時長吻合度（差距 ±3 秒內滿分，線性衰減）
```

- score ≥ 0.92：標記為高信心，GUI 提供「全部通過」批次確認
- score < 0.92：強制逐首人工確認

## 6. 標籤欄位對映

| 使用者可見欄位 | M4A atom | Opus (Vorbis comment) |
|---|---|---|
| 標題 | `©nam` | TITLE |
| 參與演出者 | `©ART` | ARTIST |
| 專輯演出者 | `aART` | ALBUMARTIST |
| 專輯 | `©alb` | ALBUM |
| 年份 | `©day` | DATE |
| 曲序 | `trkn` | TRACKNUMBER / TOTALTRACKS |
| 類型 | `©gen` | GENRE |
| 封面 | `covr` | METADATA_BLOCK_PICTURE |

Opus 封面採 base64 編碼的 FLAC Picture block 塞入 Vorbis comment，mutagen 原生支援。

**已知限制**：Windows 檔案總管不讀取 Opus/Ogg 標籤與封面。`Archive/` 樹的檔案需以 foobar2000、MusicBee、VLC 等播放器檢視。`Music/` 樹的 m4a 則於檔案總管正常顯示。

## 7. 檔案落地

```
D:\Music\<演出者>\<專輯>\01 <歌名>.m4a
D:\Music\<演出者>\<專輯>\cover.jpg
D:\Archive\<演出者>\<專輯>\01 <歌名>.opus
```

根目錄路徑可設定。

檔名淨化規則：

- 替換 Windows 非法字元 `\ / : * ? " < > |`
- 移除尾端句點與空白
- 迴避保留字：CON、PRN、AUX、NUL、COM1-9、LPT1-9
- 路徑總長度上限處理（超長時截斷曲名，保留副檔名）

## 8. 錯誤處理

| 情境 | 處理 |
|---|---|
| 影片下架、地區限制 | job 標 `failed` 並存錯誤訊息，可重試，不中斷整批 |
| MusicBrainz 查無結果 | 降級使用 YouTube 自身 metadata，標記「未配對」旗標供事後補齊 |
| MusicBrainz 回應格式變動 | JSON 欄位路徑集中管理，整批降級但下載照常進行 |
| 重複下載 | 以 videoId 為唯一鍵，已存在則跳過，可強制覆寫 |
| ffmpeg 缺失 | 啟動時檢查並直接報錯，不等下載中途失敗 |
| 網路中斷 | yt-dlp 內建重試；job 狀態存 SQLite，重啟可續 |
| MusicBrainz 請求頻率 | 每次請求間隔至少 1 秒，同專輯結果快取 |

## 9. 測試策略

- `matcher`：以真實髒標題語料做 table test（含 `【MV】`、`(Official Audio)`、`feat.` 等變體）
- `musicbrainz`：儲存 JSON fixture 離線測試，**測試絕不對真實站台發出請求**
- `tagging`：產生小檔寫入後以 mutagen 讀回驗證，涵蓋中文與 emoji 字串
- `layout`：Windows 非法字元與保留字 table test
- yt-dlp 一律 mock，不觸網路
- API 層：pytest + httpx `ASGITransport`，依賴以 `app.dependency_overrides` 替換

## 10. 明確不做（YAGNI）

- DRM 解密或繞過
- 使用者系統、多租戶
- 歌詞抓取
- 音量正規化、音質分析
- Celery / Redis 任務佇列
- 前端框架

## 11. 環境前提

- Python 3.12.4（已具備）
- yt-dlp 2026.07.04（已具備）
- **ffmpeg 尚未安裝** — remux 與封面嵌入均依賴，需先安裝：`winget install Gyan.FFmpeg`
