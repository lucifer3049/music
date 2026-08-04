@echo off
chcp 65001 >nul
cd /d "%~dp0"
title 音樂下載工具

rem 使用者自己的設定（音樂庫路徑、MusicBrainz 聯絡方式）。
rem 這個檔案不進版控，改了不會被覆蓋。範本見 settings.example.cmd。
if exist "settings.local.cmd" call "settings.local.cmd"

rem venv 不存在就建起來。這是 Python 腳本自己做不到的部分，
rem 其餘所有邏輯都在 scripts\launch.py。
if not exist ".venv\Scripts\python.exe" (
    echo 首次啟動，正在建立虛擬環境並安裝相依套件…
    echo 這只會做一次，需要幾分鐘。
    echo.
    py -3.12 -m venv .venv 2>nul || python -m venv .venv
    if not exist ".venv\Scripts\python.exe" (
        echo.
        echo ============================================================
        echo 建立虛擬環境失敗。請確認已安裝 Python 3.12 以上並在 PATH 上。
        echo ============================================================
        echo.
        pause
        exit /b 1
    )
    ".venv\Scripts\python.exe" -m pip install --upgrade pip
    ".venv\Scripts\python.exe" -m pip install -e "."
    echo.
)

".venv\Scripts\python.exe" "scripts\launch.py"

rem launch.py 正常結束時已自行處理提示；異常退出才在這裡攔住視窗。
if errorlevel 1 pause
