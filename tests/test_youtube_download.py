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


# --- Finding 1: m4a 輸出不該保留內部暫存 stem 的 "_src" 中綴 ------------------


def test_download_streams_m4a_output_has_no_temp_infix(tmp_path):
    def factory(opts):
        fmt = opts["format"]
        suffix = ".m4a" if "m4a" in fmt else ".webm"
        return _FakeYDL(opts, suffix)

    result = download_streams(
        "abc123", tmp_path, ydl_factory=factory, ffmpeg="ffmpeg",
        runner=lambda cmd, **kw: (Path(cmd[-1]).write_bytes(b"opus"), _FakeCompleted())[1],
    )
    assert result.m4a.name == "abc123.m4a"
    assert "m4a_src" not in str(result.m4a)


# --- Finding 2: 真正的 yt-dlp 例外要包成本模組的 DownloadError -----------------


class _FakeNetworkError(Exception):
    """代表 yt_dlp.utils.DownloadError 之類、與本模組同名但不同類別的例外。"""


class _RaisingYDL:
    def __init__(self, opts):
        self._opts = opts

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def download(self, urls):
        raise _FakeNetworkError("HTTP Error 403: Forbidden")


def test_download_streams_wraps_underlying_download_exception(tmp_path):
    def factory(opts):
        return _RaisingYDL(opts)

    with pytest.raises(DownloadError) as exc_info:
        download_streams("abc123", tmp_path, ydl_factory=factory, ffmpeg="ffmpeg")

    assert "abc123" in str(exc_info.value)
    assert isinstance(exc_info.value.__cause__, _FakeNetworkError)


# --- Finding 3: 失敗時清掉本次呼叫已建立的殘留檔案 -----------------------------


def test_download_streams_cleans_up_partial_artifacts_on_remux_failure(tmp_path):
    def factory(opts):
        fmt = opts["format"]
        suffix = ".m4a" if "m4a" in fmt else ".webm"
        return _FakeYDL(opts, suffix)

    def failing_runner(cmd, **kwargs):
        return _FakeCompleted(returncode=1, stderr="boom")

    with pytest.raises(DownloadError):
        download_streams(
            "abc123", tmp_path, ydl_factory=factory, ffmpeg="ffmpeg", runner=failing_runner,
        )

    assert list(tmp_path.iterdir()) == []


# --- Minor：未覆蓋到的分支 ------------------------------------------------------


def test_download_streams_accepts_mp4_fallback_for_m4a(tmp_path):
    def factory(opts):
        fmt = opts["format"]
        suffix = ".mp4" if "m4a" in fmt else ".webm"
        return _FakeYDL(opts, suffix)

    result = download_streams(
        "abc123", tmp_path, ydl_factory=factory, ffmpeg="ffmpeg",
        runner=lambda cmd, **kw: (Path(cmd[-1]).write_bytes(b"opus"), _FakeCompleted())[1],
    )
    assert result.m4a.name == "abc123.m4a"
    assert result.m4a.exists()


def test_download_streams_accepts_opus_container_directly(tmp_path):
    def factory(opts):
        fmt = opts["format"]
        suffix = ".m4a" if "m4a" in fmt else ".opus"
        return _FakeYDL(opts, suffix)

    calls = []

    def runner(cmd, **kwargs):
        calls.append(cmd)
        return _FakeCompleted()

    result = download_streams(
        "abc123", tmp_path, ydl_factory=factory, ffmpeg="ffmpeg", runner=runner,
    )
    assert result.opus.exists()
    assert result.opus.name == "abc123.opus"
    assert calls == []  # 已經是 .opus 容器，不該呼叫 remux
