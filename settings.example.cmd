@echo off
rem ============================================================
rem 個人設定範本
rem
rem 用法：把這個檔案複製成 settings.local.cmd 再修改。
rem settings.local.cmd 不進版控，你的設定不會被更新覆蓋。
rem 每一行前面的 rem 是註解，要啟用該設定就把 rem 拿掉。
rem ============================================================

rem MusicBrainz 要求請求帶聯絡方式，否則可能拒絕服務。
rem 沒設定的話症狀是候選標籤全部變成「未配對」。填你自己的 email。
rem set MUSICBRAINZ_CONTACT=your-email@example.com

rem 音樂庫根目錄。m4a 放這裡，給播放器與媒體伺服器掃描。
rem set MUSIC_ROOT=D:\Music

rem 冷存根目錄。opus 放這裡，不建議讓播放器掃描
rem （否則每首歌會在音樂庫裡出現兩次）。
rem set ARCHIVE_ROOT=D:\Archive

rem 網頁介面的埠。預設 8899，只有跟其他程式衝突時才需要改。
rem set MUSIC_DOWNLOADER_PORT=8899
