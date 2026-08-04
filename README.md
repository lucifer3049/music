# 音樂下載工具

個人本機音樂收藏工具。從 YouTube Music 取得音訊，以 MusicBrainz 公開 metadata 補齊標籤與封面，依演出者／專輯結構落檔。

本機單人使用的網頁 GUI，FastAPI 單一進程，無外部服務依賴。

## 範圍與限制

- 音訊唯一來源為 YouTube Music（透過 yt-dlp 取公開串流），用途限個人本機收藏。
- **本工具不解密、不繞過任何 DRM**，不涉及任何訂閱制串流平台的音檔。
- 原設計以 KKBOX 為 metadata 來源，實作時發現其專輯與歌曲頁已由 AWS WAF 攔截程式化存取（HTTP 202、`x-amzn-waf-action: challenge`、回應零位元組），繞過機器人驗證不在選項內，因此改用 **MusicBrainz** 公開 API，封面圖取自 **Cover Art Archive**。
- YouTube Music 沒有無損來源。本工具**不重新編碼**，音訊串流原封落檔（m4a 走 remux 容器包裝、opus 走 `-c:a copy`），只是把來源給的東西存起來。
- MusicBrainz 對冷門或非西方語系的發行版收錄不如串流平台完整；查無結果或候選不準時，靠下載前的人工確認畫面手動修正（見「使用流程」）。
- Genre 資料實測只存在於 MusicBrainz 的 release-group 這一層（recording／release 這兩層幾乎都拿不到），額外查一次要多打一支 API、有節流延遲成本，因此只在使用者確認候選、真的要下載那一首時才查（`Pipeline.finalize()`），不在比對階段對每個候選都查。西洋主流專輯通常查得到，華語／非西方或冷門專輯常態性是空的——這是 MusicBrainz 資料覆蓋率的限制，不是程式漏抓（詳見 `.superpowers/sdd/genre-report.md`）。

## 需求

- Python 3.12 以上
- ffmpeg 在 PATH 上（`winget install Gyan.FFmpeg`，安裝後需重開終端機）

## 啟動（一般使用）

**雙擊 `start.bat`。** 它會自己處理其餘的事：

- 首次執行時建立虛擬環境並安裝相依套件（只做一次，需要幾分鐘）
- ffmpeg 不在 PATH 時，自動搜尋常見安裝位置
- 伺服器就緒後自動開啟瀏覽器
- 已經有一份在跑時，不會報錯，直接把瀏覽器指向現有那份

關閉那個主控台視窗即停止伺服器。

想放到桌面的話，對 `start.bat` 按右鍵 →「傳送到」→「桌面（建立捷徑）」。

### 個人設定

複製 `settings.example.cmd` 為 `settings.local.cmd`，把要用的那幾行前面的 `rem` 拿掉再填值。`settings.local.cmd` 不進版控，更新程式不會覆蓋你的設定。

至少建議設定 `MUSICBRAINZ_CONTACT`（見下方「設定」）。

## 啟動（開發用）

    .venv\Scripts\python.exe -m uvicorn app.main:app --port 8899

`app/main.py` 在 import 階段就會呼叫 `require_ffmpeg()`，所以 ffmpeg 必須先在 PATH 上，伺服器才啟動得起來（見「疑難排解」）。

手動建立環境：

    python -m venv .venv
    .venv\Scripts\python.exe -m pip install -e ".[dev]"

> repo 內另有 `.claude/launch.json` 與 `.claude/serve.ps1`，是開發用的啟動捷徑，**會把輸出導向暫存的 `_devlib/`，不是你的正式音樂庫**，只適合改前端時使用。日常請用 `start.bat`。

## 設定

| 環境變數 | 預設值 | 說明 |
|---|---|---|
| `MUSIC_ROOT` | `D:\Music` | m4a 輸出根目錄，供播放器／媒體伺服器掃描 |
| `ARCHIVE_ROOT` | `D:\Archive` | opus 輸出根目錄，冷存不掃描。僅在 `KEEP_ARCHIVE_COPY` 開啟時才會用到 |
| `KEEP_ARCHIVE_COPY` | 關閉 | 是否額外保留一份 opus 冷存副本。填 `1`／`true`／`yes` 開啟，其餘（含未設定）皆為關閉 |
| `MUSICBRAINZ_CONTACT` | 未設定時使用內建佔位字串 | MusicBrainz 規定 User-Agent 必須帶可辨識的聯絡方式，否則請求可能被拒。請設成自己的 email，不要留預設值 |
| `MUSIC_DOWNLOADER_PORT` | `8899` | 網頁介面的埠。只有跟其他程式衝突時才需要改；`start.bat` 遇到埠被占用會自動往後找 |

用 `start.bat` 啟動時，這些變數寫在 `settings.local.cmd` 裡（範本見 `settings.example.cmd`）。

`MUSIC_ROOT` / `ARCHIVE_ROOT` 由 `app/config.py` 的 `load_roots()` 讀取；`MUSICBRAINZ_CONTACT` 由 `app/sources/musicbrainz.py` 直接讀取環境變數，未設定時退回一句提示文字（不是空字串、也不是真實 email），MusicBrainz 有可能因此拒絕請求或降低優先度。

## 輸出格式

預設只產出一份 m4a，不重新編碼：

| 位置 | 格式 | 用途 |
|---|---|---|
| `MUSIC_ROOT` | m4a（AAC，來源原始 bitrate，通常為 128kbps） | 日常播放。Windows 檔案總管、手機、車機原生讀標籤與封面 |
| `ARCHIVE_ROOT` | opus（來源原始 bitrate） | **預設關閉**，需設 `KEEP_ARCHIVE_COPY=1` 才會產生 |

原本設計會同時存一份 opus 當冷存副本。實測發現同一首歌 opus 約 129kbps、m4a 約 128kbps —— 來源 bitrate 逐支影片而異，opus 並不可靠地比較高，多這一份只是把儲存空間翻倍，而且 Windows 檔案總管根本不顯示 Opus 標籤。因此改為預設關閉。

開啟後除了佔用空間加倍，**下載時間也會加倍**，因為兩條串流都要抓。

路徑結構為 `<根目錄>/<專輯演出者>/<專輯>/<曲序 曲名>.<副檔名>`（`app/storage/layout.py`），檔名會做 Windows 非法字元／保留裝置名淨化。封面圖只寫一份 `cover.jpg` 到 `MUSIC_ROOT` 的專輯資料夾內；opus 檔案本身也內嵌封面（Vorbis comment `METADATA_BLOCK_PICTURE`），m4a 內嵌於 `covr` atom。

## 使用流程

1. 貼上網址（支援單曲、專輯、播放清單，一行一個）
2. 系統探測曲目並向 MusicBrainz 查詢候選標籤，停在「待確認」
3. 逐首確認或修改標籤後按「確認並下載」（或「跳過」）
4. 下載、查 genre（若確認的候選沒有 genre）、寫標籤、落檔

已下載完成（`done`）過的 videoId 再次送出會自動標記為「已跳過」；失敗（`failed`）或其他未完成狀態的曲目不算重複，可以重新處理。

若整個網址探測失敗（下架、私人、地區限制、網址格式不支援等），job 本身會帶著錯誤訊息、不含任何曲目，畫面上直接顯示這則錯誤，不會誤判成「還在跑」。

確認或跳過一首已經不在「待確認」狀態的曲目（例如已經確認過、正在下載、或已完成）會收到 409，畫面上會就地顯示錯誤——通常是重複點擊或分頁沒刷新，重新整理頁面即可。

### 確認畫面上的來源連結

每首曲目的候選卡片上方會顯示這首曲目實際探測到的 YouTube 網址（可點開新分頁核對）。這是因為 YouTube Music「複製連結」拿到的常常是「目前正在播放」的那一首，不一定是你原本點開的那一首——同一張專輯常有多個版本（例如 Live 版、英文版）指到不同網址，畫面上只看標題容易分不出來，貼錯連結卻抓到別的版本。下載前先點開這個連結核對一下，比下載完才發現抓錯省事。

### 清除任務紀錄

每個任務卡片右上角有「刪除任務」按鈕，畫面上方也有「清除已完成紀錄」可一次清掉所有已完成（所有曲目都到達 `done`／`failed`／`skipped`，或整個網址探測失敗）的任務。兩者都會先跳出確認提示，且都拒絕刪除還有曲目正在下載或寫標籤的任務（回應 409，避免刪掉一個背景執行緒還在寫入的任務列）。

**清除紀錄不會刪除已下載的檔案，但會一併清掉這個任務的 dedup 紀錄。** dedup（見上方「已下載完成…自動標記為已跳過」）只看資料庫裡是否還留著這個 videoId 的 `done` 記錄——紀錄被清掉之後，資料庫已經不知道這首歌下載過，之後重新送出同一個網址會重新下載一次。這次重新下載本身是安全的：`Pipeline.finalize()` 落檔前一律先檢查目的路徑是否已存在檔案，存在就拒絕覆蓋、把曲目標記為 `failed`（見上方「同一首歌從兩個不同網址…」），不會真的覆蓋掉舊檔案，但畫面上會看到一個「奇怪」的失敗，原因就是這裡——不是程式壞了，是先前清過紀錄。想真的換掉舊檔案，一樣要自行刪除舊檔後再重新確認。

## 測試

    .venv\Scripts\python.exe -m pytest -q

目前共 222 個測試，全部使用 fixture 與 mock，不對 MusicBrainz 或 YouTube 發出任何真實請求。

## 執行紀錄

`logs/app.log`（隨 `start.bat` 啟動時建立，同時輸出到主控台視窗）。

裡面看得到探測到幾首、逐首比對進度、每首完成或失敗的原因。曲目卡在某個狀態時，這裡是第一個該看的地方。檔案輪替上限 2 MB × 4 份，不會無限成長。

## 疑難排解

| 症狀 | 原因與處理 |
|---|---|
| 啟動就報 ffmpeg 錯誤（`FfmpegMissingError`） | ffmpeg 不在 PATH。`app/main.py` 在 import 階段就檢查，裝好後需重開終端機讓 PATH 生效 |
| 候選標籤明顯配錯，或全部很低分 | MusicBrainz 查無結果時會降級用 YouTube 自身資料（分數 0），仍可下載；未設定 `MUSICBRAINZ_CONTACT` 也可能讓請求被拒而觸發降級。冷門或非西方語系的發行版本來就收錄較薄，這正是「待確認」畫面存在的目的——手動改標籤即可 |
| 下載完的檔案 Genre 欄位是空的 | 預期行為（多數情況）。MusicBrainz 的 genre 資料只存在於 release-group 這一層，而且完全仰賴社群標記；西洋主流專輯通常有資料，華語／非西方或冷門專輯常態性沒有人標記，此時就是空的，不是程式漏抓。降級路徑（查無 MusicBrainz 結果、用 YouTube 自身資料組標籤）沒有 MusicBrainz release 可查，genre 一定是空的 |
| `Archive/` 底下的 opus 檔案，檔案總管內容面板看不到任何標籤 | 預期行為，不是 bug。Windows 檔案總管不讀 Opus 標籤。改用 foobar2000、MusicBee 或 VLC 檢視；`Music/` 底下的 m4a 檔案沒有這個問題 |
| 送出後長時間停在「探測中」 | 專輯或播放清單本來就慢：每首都要一次受節流（1.1 秒）的 MusicBrainz 查詢，40 首的專輯約需一分鐘。`logs/app.log` 會顯示「探測到 N 首」與逐首進度，可據此判斷是進行中還是真的卡住 |
| 曲目狀態卡在「failed」 | 展開錯誤訊息。常見於影片下架或地區限制而無法下載，其餘情況可重新送出同一個網址重試 |
| 點「確認並下載」或「跳過」沒反應、出現錯誤 | 後端回 409，代表這首曲目已經不在「待確認」狀態（可能已被確認、正在下載或已完成）。重新整理頁面確認目前狀態 |
| 伺服器重啟後，重啟前正在下載／寫標籤的曲目變成「failed」，錯誤訊息是「伺服器重啟時中斷，可重新確認」 | 預期行為，不是資料遺失。`app/main.py` 啟動時會呼叫 `JobStore.recover_interrupted_tracks()`，把上次行程留在 `matching`／`downloading`／`tagging`（都是非終態）的曲目收斂成 `failed`，避免它們永遠卡住、SSE 也永遠不結束。重新送出同一個網址即可重新處理，`failed` 不算重複（見上方「使用流程」） |
| 同一首歌從兩個不同網址（例如官方 MV 與自動生成的 Topic 頻道上傳）各自確認下載 | 第二次確認會失敗（`failed`），錯誤訊息會附上已存在的目的檔案路徑。dedup 只看 videoId，兩個不同 videoId 比對到同一個 MusicBrainz 候選時會算出同一個落地路徑，系統不會靜默覆蓋已下載的檔案；需要換掉時請自行刪除舊檔後重新確認 |
| 點「刪除任務」或「清除已完成紀錄」沒反應、出現錯誤 | 後端回 409，代表這個任務裡還有曲目正在下載或寫標籤（背景執行緒還在寫入，這時刪除會產生孤兒下載），等它跑完（變成 `done`／`failed`）再刪即可 |
| 清除紀錄後重新送出同一個網址，曲目變成「failed」，錯誤訊息附了一個已存在的檔案路徑 | 預期行為，不是資料遺失。清除紀錄只清資料庫裡的任務列，不會刪除已下載的檔案，但會連帶清掉 dedup 記錄——系統因此不知道這首歌下載過，重新送出會重新跑一次下載；`Pipeline.finalize()` 落檔前一律檢查目的路徑，發現檔案已存在就拒絕覆蓋並標記失敗（跟上一列「同一首歌從兩個不同網址」是同一套保護），不會弄丟舊檔案。需要換掉時一樣得自行刪除舊檔 |
