"""Helpers shared by the integration and API tests. Not collected by pytest
(filename does not start with `test_`)."""
import json
import subprocess
import time
from pathlib import Path

import app as clipapp

FFMPEG, FFPROBE = clipapp.get_bins()
OUTPUT_DIR = clipapp.OUTPUT_DIR


def base_opts(**over) -> dict:
    """A fully-explicit opts dict so a test does not depend on the user's
    config.json. Override individual keys via kwargs."""
    o = {
        "codec": "h264", "container": "mp4", "hardware": "cpu",
        "targetSizeMb": 0, "vibrance": 1.0, "stretch": False,
        "frameAccurateCut": False, "twoPass": True, "tenBit": False,
        "normalizeAudio": True, "volumeBoost": "none", "fpsMode": "source",
        "fpsCustom": 48, "outputHeight": "source", "encoderQualityMode": "auto",
        "encodeSpeed": "fast", "audioCodec": "aac", "audioBitrateMode": "auto",
        "hwaccelDecode": False, "hdrToneMap": True, "denoiseStrength": "light",
        "lowBitrateDenoise": True, "crfMaxrateMultiplier": 2.0,
    }
    o.update(over)
    return o


def upload(client, path: Path, name: str | None = None) -> dict:
    """POST a file to /api/upload; return the JSON ({filename, info})."""
    with open(path, "rb") as fp:
        r = client.post("/api/upload",
                         data={"file": (fp, name or Path(path).name)},
                         content_type="multipart/form-data")
    assert r.status_code == 200, r.get_data(as_text=True)
    return r.get_json()


def run_job(client, items: list, opts: dict | None = None):
    """POST /api/process and poll to completion. Returns (status_code, json):
    on a 200 the json is the finished job view, otherwise the error body."""
    body = {"items": items}
    if opts is not None:
        body["opts"] = opts
    r = client.post("/api/process", json=body)
    if r.status_code != 200:
        return r.status_code, r.get_json()
    job_id = r.get_json()["job_id"]
    for _ in range(400):
        j = client.get(f"/api/job/{job_id}").get_json()
        if j["status"] in ("done", "cancelled"):
            return 200, j
        time.sleep(0.3)
    raise AssertionError(f"job {job_id} did not finish in time")


def streams(path: Path) -> list[dict]:
    """ffprobe stream list for an output file."""
    r = subprocess.run([FFPROBE, "-v", "quiet", "-print_format", "json",
                        "-show_streams", "-show_format", str(path)],
                       capture_output=True, text=True)
    return json.loads(r.stdout or "{}")


def decodes_clean(name: str) -> bool:
    """Decode an output file end to end; True only if ffmpeg reports no errors."""
    out = OUTPUT_DIR / name
    assert out.exists(), f"output {name} missing"
    r = subprocess.run([FFMPEG, "-v", "error", "-i", str(out), "-f", "null", "-"],
                       capture_output=True, text=True)
    return r.returncode == 0 and not (r.stderr or "").strip()


def video_stream(name: str) -> dict:
    data = streams(OUTPUT_DIR / name)
    return next(s for s in data.get("streams", [])
                if s.get("codec_type") == "video")


def audio_stream(name: str) -> dict | None:
    data = streams(OUTPUT_DIR / name)
    return next((s for s in data.get("streams", [])
                 if s.get("codec_type") == "audio"), None)


def duration_of(name: str) -> float:
    data = streams(OUTPUT_DIR / name)
    return float(data.get("format", {}).get("duration", 0) or 0)
