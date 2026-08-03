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
