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
