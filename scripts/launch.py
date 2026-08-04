"""一鍵啟動：檢查環境、啟動伺服器、等它活起來之後開瀏覽器。

由根目錄的 start.bat 呼叫，也可以直接跑：

    .venv\\Scripts\\python.exe scripts\\launch.py

刻意寫成獨立腳本而不是塞進 .bat：批次檔難讀難改，而 Python 本來就是這個
專案的必要條件。start.bat 只負責「確保 venv 存在」這件 Python 自己做不到
的事，其餘都在這裡。
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import shutil
import socket
import sys
import threading
import time
import webbrowser
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PORT = 8899
HOST = "127.0.0.1"
LOG_DIR = PROJECT_ROOT / "logs"
LOG_FILE = LOG_DIR / "app.log"
# 保留最近幾份，避免長期使用把磁碟塞滿。
LOG_MAX_BYTES = 2 * 1024 * 1024
LOG_BACKUP_COUNT = 3
# 埠被占用時最多往後找幾個。避免使用者已經開著一份卻看到一句冷冰冰的錯誤。
PORT_SEARCH_RANGE = 10
STARTUP_TIMEOUT_SECONDS = 60

# ffmpeg 若不在 PATH 上，先找這些常見安裝位置再放棄。
# 用萬用字元比對，不寫死版本號。
_FFMPEG_GLOBS = (
    r"AppData\Local\Microsoft\WinGet\Packages\Gyan.FFmpeg*\*\bin",
    r"AppData\Local\Microsoft\WinGet\Packages\BtbN.FFmpeg*\*\bin",
    r"scoop\apps\ffmpeg\current\bin",
)
_FFMPEG_SYSTEM_DIRS = (
    r"C:\ProgramData\chocolatey\bin",
    r"C:\ffmpeg\bin",
)


def setup_logging() -> None:
    """同時寫主控台與 logs/app.log。

    主控台視窗一關就什麼都不剩，但下載是長時間作業，出事時往往已經關掉了。
    檔案輪替上限 2 MB × 4 份，長期使用不會塞爆磁碟。
    """
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    file_handler = logging.handlers.RotatingFileHandler(
        LOG_FILE, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUP_COUNT, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    file_handler.setLevel(logging.INFO)

    console = logging.StreamHandler()
    console.setFormatter(formatter)
    console.setLevel(logging.INFO)

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.handlers.clear()
    root.addHandler(file_handler)
    root.addHandler(console)

    # yt-dlp 每首歌會噴大量 debug，只留警告以上，否則 log 檔會被它淹掉。
    logging.getLogger("yt_dlp").setLevel(logging.WARNING)


def _die(message: str) -> None:
    print()
    print("=" * 60)
    print(message)
    print("=" * 60)
    print()
    input("按 Enter 關閉…")
    sys.exit(1)


def ensure_ffmpeg() -> None:
    """確保 ffmpeg 找得到。找不到就把它加進本次執行的 PATH。

    app/main.py 在 import 階段就會呼叫 require_ffmpeg()，所以這件事必須在
    載入應用程式之前完成。
    """
    if shutil.which("ffmpeg"):
        return

    home = Path.home()
    candidates: list[Path] = []
    for pattern in _FFMPEG_GLOBS:
        candidates.extend(home.glob(pattern))
    candidates.extend(Path(p) for p in _FFMPEG_SYSTEM_DIRS)

    for directory in candidates:
        if (directory / "ffmpeg.exe").exists():
            os.environ["PATH"] = f"{directory}{os.pathsep}{os.environ.get('PATH', '')}"
            print(f"[環境] ffmpeg 不在 PATH，已自動使用：{directory}")
            return

    _die(
        "找不到 ffmpeg，無法啟動。\n\n"
        "ffmpeg 負責 opus 容器轉封裝與封面處理，是必要元件。\n"
        "請在終端機執行下列指令安裝，裝完後重新開機或重新登入讓 PATH 生效：\n\n"
        "    winget install Gyan.FFmpeg\n"
    )


def _port_is_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind((HOST, port))
        except OSError:
            return False
    return True


def _server_is_up(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.5)
        return probe.connect_ex((HOST, port)) == 0


def resolve_port() -> tuple[int, bool]:
    """回傳 (埠號, 是否已有執行中的實例)。

    使用者重複雙擊 start.bat 是很自然的事。與其讓第二份因為埠被占用而崩掉，
    不如認出「已經開著一份了」，直接把瀏覽器指過去。
    """
    configured = os.environ.get("MUSIC_DOWNLOADER_PORT")
    if configured:
        try:
            return int(configured), not _port_is_free(int(configured))
        except ValueError:
            _die(f"環境變數 MUSIC_DOWNLOADER_PORT 不是有效的埠號：{configured!r}")

    if _port_is_free(DEFAULT_PORT):
        return DEFAULT_PORT, False
    if _server_is_up(DEFAULT_PORT):
        return DEFAULT_PORT, True

    for port in range(DEFAULT_PORT + 1, DEFAULT_PORT + PORT_SEARCH_RANGE):
        if _port_is_free(port):
            return port, False

    _die(
        f"連續 {PORT_SEARCH_RANGE} 個埠（{DEFAULT_PORT} 起）都被占用，無法啟動。\n"
        "請關掉占用這些埠的程式，或設定環境變數 MUSIC_DOWNLOADER_PORT 指定其他埠。"
    )
    raise AssertionError("unreachable")


def _open_browser_when_ready(url: str, port: int) -> None:
    deadline = time.monotonic() + STARTUP_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if _server_is_up(port):
            webbrowser.open(url)
            return
        time.sleep(0.3)
    print(f"[警告] 伺服器超過 {STARTUP_TIMEOUT_SECONDS} 秒仍未就緒，請手動開啟 {url}")


def _warn_missing_contact() -> None:
    if os.environ.get("MUSICBRAINZ_CONTACT"):
        return
    print(
        "[提醒] 尚未設定 MUSICBRAINZ_CONTACT。\n"
        "       MusicBrainz 要求請求帶聯絡方式，未設定時查詢可能被拒絕，\n"
        "       症狀是候選標籤全部變成「未配對」。\n"
        "       設定方式：複製 settings.example.cmd 為 settings.local.cmd 後填入你的 email。"
    )


def main() -> None:
    os.chdir(PROJECT_ROOT)
    sys.path.insert(0, str(PROJECT_ROOT))

    setup_logging()
    ensure_ffmpeg()
    _warn_missing_contact()

    port, already_running = resolve_port()
    url = f"http://{HOST}:{port}"

    if already_running:
        print(f"[資訊] 偵測到已有一份在 {url} 執行中，直接開啟瀏覽器。")
        webbrowser.open(url)
        input("按 Enter 關閉這個視窗（不會影響執行中的那一份）…")
        return

    print(f"音樂下載工具 — {url}")
    print("關閉這個視窗即停止伺服器。")
    print(f"執行紀錄：{LOG_FILE}")
    print()

    threading.Thread(target=_open_browser_when_ready, args=(url, port), daemon=True).start()

    import uvicorn

    try:
        # log_config=None 才不會讓 uvicorn 蓋掉 setup_logging() 裝好的 handler，
        # 否則它的紀錄只會進主控台、不會落到檔案。
        uvicorn.run("app.main:app", host=HOST, port=port, log_level="info", log_config=None)
    except KeyboardInterrupt:
        pass
    except Exception as exc:  # noqa: BLE001 — 最外層，要讓使用者看得到原因
        _die(f"伺服器啟動失敗：{type(exc).__name__}: {exc}")


if __name__ == "__main__":
    main()
