"""Shared pytest fixtures for the Clip Creator test suite.

Makes the project root importable (`import app`), exposes a Flask test client,
and builds a handful of tiny synthetic source clips once per session. All clips
are 320x240 so encodes finish fast; the suite skips cleanly when ffmpeg is
absent.
"""
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import app as clipapp  # noqa: E402

FFMPEG, FFPROBE = clipapp.get_bins()


def _ffmpeg_ok() -> bool:
    try:
        subprocess.run([FFMPEG, "-version"], capture_output=True, timeout=10)
        return True
    except Exception:
        return False


FFMPEG_OK = _ffmpeg_ok()
needs_ffmpeg = pytest.mark.skipif(not FFMPEG_OK, reason="ffmpeg not available")


def _build(path: Path, *args: str) -> Path:
    subprocess.run([FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
                    *args, str(path)], check=True, capture_output=True)
    return path


@pytest.fixture(scope="session")
def client():
    clipapp.app.config["TESTING"] = True
    return clipapp.app.test_client()


@pytest.fixture(scope="session")
def media_dir(tmp_path_factory):
    return tmp_path_factory.mktemp("media")


@pytest.fixture(scope="session")
def sample(media_dir):
    """6s 320x240 30fps H.264 + AAC audio - the general-purpose source."""
    return _build(media_dir / "sample.mp4",
                  "-f", "lavfi", "-i", "testsrc2=s=320x240:r=30:d=6",
                  "-f", "lavfi", "-i", "sine=frequency=440:duration=6",
                  "-c:v", "libx264", "-pix_fmt", "yuv420p",
                  "-c:a", "aac", "-shortest")


@pytest.fixture(scope="session")
def sample_noaudio(media_dir):
    """6s 320x240 30fps H.264, no audio stream."""
    return _build(media_dir / "sample_noaudio.mp4",
                  "-f", "lavfi", "-i", "testsrc2=s=320x240:r=30:d=6",
                  "-c:v", "libx264", "-pix_fmt", "yuv420p", "-an")


@pytest.fixture(scope="session")
def sample_60fps(media_dir):
    """5s 320x240 60fps H.264 + audio - lets fps-drop actually take effect."""
    return _build(media_dir / "sample_60.mp4",
                  "-f", "lavfi", "-i", "testsrc2=s=320x240:r=60:d=5",
                  "-f", "lavfi", "-i", "sine=frequency=440:duration=5",
                  "-c:v", "libx264", "-pix_fmt", "yuv420p",
                  "-c:a", "aac", "-shortest")


@pytest.fixture(scope="session")
def sample_letterbox(media_dir):
    """6s clip with 320x240 frame but 280x180 content centred in black bars -
    gives cropdetect something real to find for the stretch path."""
    return _build(media_dir / "sample_letterbox.mp4",
                  "-f", "lavfi", "-i",
                  "testsrc2=s=280x180:r=30:d=6,pad=320:240:20:30:black",
                  "-f", "lavfi", "-i", "sine=frequency=440:duration=6",
                  "-c:v", "libx264", "-pix_fmt", "yuv420p",
                  "-c:a", "aac", "-shortest")
