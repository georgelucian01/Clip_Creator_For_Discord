"""Real-encode integration tests covering every pipeline path and the
meaningful setting combinations. Each test runs one ffmpeg encode of a tiny
320x240 clip, so the whole file finishes in a couple of minutes.

Hardware-encoder tests are generated from whatever probe_encoders() found
usable on this machine; on a CPU-only box that parametrization is simply empty.
"""
import pytest

import app as a
from conftest import needs_ffmpeg
from _util import (audio_stream, base_opts, decodes_clean, duration_of,
                   run_job, streams, upload, video_stream)

pytestmark = needs_ffmpeg


# hardware encoders this machine actually validated as usable
_HW_MAP = {
    "h264_amf": ("h264", "amd"), "hevc_amf": ("hevc", "amd"),
    "av1_amf": ("av1", "amd"),
    "h264_nvenc": ("h264", "nvidia"), "hevc_nvenc": ("hevc", "nvidia"),
    "av1_nvenc": ("av1", "nvidia"),
    "h264_qsv": ("h264", "intel"), "hevc_qsv": ("hevc", "intel"),
    "av1_qsv": ("av1", "intel"),
}
USABLE_HW = [enc for enc in _HW_MAP if a.AVAILABLE_ENCODERS.get(enc)]


def _summary(job: str) -> str:
    return " ".join(job["summaries"]).lower()


def _norm_codec(name: str) -> str:
    return a.normalize_codec(name)


# ---------- pipeline modes ----------
def test_passthrough(client, sample):
    up = upload(client, sample)
    code, job = run_job(
        client, [{"filename": up["filename"], "segments": [{"start": 0, "end": 6}]}],
        base_opts(codec="h264", container="mp4"))
    assert code == 200 and job["status"] == "done"
    assert "passthrough" in _summary(job)
    assert job["outputs"]


def test_stream_copy_cut(client, sample):
    up = upload(client, sample)
    code, job = run_job(
        client, [{"filename": up["filename"], "segments": [{"start": 1, "end": 4}]}],
        base_opts(codec="h264", container="mp4"))
    assert code == 200 and job["status"] == "done"
    assert "stream-copy" in _summary(job)
    assert decodes_clean(job["outputs"][0])


def test_full_encode_single(client, sample):
    up = upload(client, sample)
    code, job = run_job(
        client, [{"filename": up["filename"], "segments": [{"start": 0, "end": 6}]}],
        base_opts(vibrance=1.3))
    assert code == 200 and job["status"] == "done"
    assert "full encode" in _summary(job)
    assert decodes_clean(job["outputs"][0])


def test_multi_segment_join(client, sample):
    up = upload(client, sample)
    code, job = run_job(
        client, [{"filename": up["filename"],
                  "segments": [{"start": 0, "end": 2}, {"start": 4, "end": 6}]}],
        base_opts())
    assert code == 200 and job["status"] == "done"
    assert "multi-segment" in _summary(job)
    out = job["outputs"][0]
    assert decodes_clean(out)
    # two 2s keep-segments -> ~4s output
    assert abs(duration_of(out) - 4.0) < 1.0


def test_legacy_single_segment_shape(client, sample):
    """The pre-multi-segment {start,end} item shape must still be accepted."""
    up = upload(client, sample)
    code, job = run_job(
        client, [{"filename": up["filename"], "start": 0, "end": 6,
                  "opts": {"vibrance": 1.2}}],
        base_opts())
    assert code == 200 and job["status"] == "done"
    assert decodes_clean(job["outputs"][0])


# ---------- codec / container matrix ----------
@pytest.mark.parametrize("codec,container", [
    ("h264", "mp4"), ("h264", "mkv"),
    ("hevc", "mp4"), ("hevc", "mkv"),
    ("av1", "mp4"), ("av1", "mkv"), ("av1", "webm"),
])
def test_codec_container_matrix(client, sample, codec, container):
    up = upload(client, sample)
    code, job = run_job(
        client, [{"filename": up["filename"], "segments": [{"start": 0, "end": 6}]}],
        # vibrance forces a real encode for every combination
        base_opts(codec=codec, container=container, vibrance=1.05))
    assert code == 200 and job["status"] == "done", job
    out = job["outputs"][0]
    assert out.endswith(f".{container}")
    assert decodes_clean(out)
    assert _norm_codec(video_stream(out)["codec_name"]) == codec


# ---------- hardware encoders (whatever this machine has) ----------
@pytest.mark.parametrize("hw_enc", USABLE_HW)
def test_hardware_encoder(client, sample, hw_enc):
    codec, hardware = _HW_MAP[hw_enc]
    container = "webm" if codec == "av1" else "mkv"
    up = upload(client, sample)
    # size-target (bitrate) mode - the path the app is actually built for.
    # CRF/cqp mode is exercised separately via the CPU encoders.
    code, job = run_job(
        client, [{"filename": up["filename"], "segments": [{"start": 0, "end": 6}]}],
        base_opts(codec=codec, container=container, hardware=hardware,
                  encoderQualityMode="hardware_always", targetSizeMb=5,
                  vibrance=1.05))
    assert code == 200 and job["status"] == "done", job
    assert job["outputs"], job.get("errors")
    # decode-verify + CPU fallback guarantees a clean output even if the GPU
    # encoder is flaky - so the output must always decode.
    assert decodes_clean(job["outputs"][0])
    assert _norm_codec(video_stream(job["outputs"][0])["codec_name"]) == codec


# ---------- size targeting ----------
def test_two_pass_size_target(client, sample):
    up = upload(client, sample)
    # vibrance forces FULL_ENCODE - the tiny synthetic clip would otherwise
    # passthrough, since it already fits the size target untouched.
    code, job = run_job(
        client, [{"filename": up["filename"], "segments": [{"start": 0, "end": 6}]}],
        base_opts(targetSizeMb=3, twoPass=True, encodeSpeed="fast", vibrance=1.05))
    assert code == 200 and job["status"] == "done"
    assert "two-pass" in _summary(job)
    assert decodes_clean(job["outputs"][0])


def test_single_pass_size_target(client, sample):
    up = upload(client, sample)
    code, job = run_job(
        client, [{"filename": up["filename"], "segments": [{"start": 0, "end": 6}]}],
        base_opts(targetSizeMb=3, twoPass=False, vibrance=1.05))
    assert code == 200 and job["status"] == "done"
    assert decodes_clean(job["outputs"][0])


def test_size_target_is_respected(client, sample):
    up = upload(client, sample)
    target_mb = 2
    code, job = run_job(
        client, [{"filename": up["filename"], "segments": [{"start": 0, "end": 6}]}],
        base_opts(targetSizeMb=target_mb, vibrance=1.05))
    assert code == 200 and job["status"] == "done"
    out = a.OUTPUT_DIR / job["outputs"][0]
    # generous ceiling - VBR overshoots, but it must be in the right ballpark
    assert out.stat().st_size < target_mb * a.MB * 1.6


def test_crf_quality_mode(client, sample):
    """targetSizeMb=0 -> constant-quality (CRF/CQ) encode, no size cap."""
    up = upload(client, sample)
    code, job = run_job(
        client, [{"filename": up["filename"], "segments": [{"start": 0, "end": 6}]}],
        base_opts(targetSizeMb=0, vibrance=1.2))
    assert code == 200 and job["status"] == "done"
    assert "crf mode" in _summary(job)
    assert decodes_clean(job["outputs"][0])


# ---------- 10-bit ----------
@pytest.mark.parametrize("codec,container", [("hevc", "mkv"), ("av1", "mkv")])
def test_ten_bit_output(client, sample, codec, container):
    up = upload(client, sample)
    code, job = run_job(
        client, [{"filename": up["filename"], "segments": [{"start": 0, "end": 6}]}],
        base_opts(codec=codec, container=container, tenBit=True, vibrance=1.05))
    assert code == 200 and job["status"] == "done"
    out = job["outputs"][0]
    assert decodes_clean(out)
    assert "10le" in video_stream(out)["pix_fmt"]


# ---------- audio ----------
def test_no_audio_source(client, sample_noaudio):
    up = upload(client, sample_noaudio)
    code, job = run_job(
        client, [{"filename": up["filename"], "segments": [{"start": 0, "end": 6}]}],
        base_opts(vibrance=1.2))
    assert code == 200 and job["status"] == "done"
    out = job["outputs"][0]
    assert decodes_clean(out)
    assert audio_stream(out) is None


def test_normalize_audio_off(client, sample):
    up = upload(client, sample)
    code, job = run_job(
        client, [{"filename": up["filename"], "segments": [{"start": 0, "end": 6}]}],
        base_opts(vibrance=1.2, normalizeAudio=False))
    assert code == 200 and job["status"] == "done"
    assert "loudness normalization" not in _summary(job)
    assert audio_stream(job["outputs"][0]) is not None


def test_audio_codec_opus(client, sample):
    up = upload(client, sample)
    code, job = run_job(
        client, [{"filename": up["filename"], "segments": [{"start": 0, "end": 6}]}],
        base_opts(audioCodec="opus", vibrance=1.05))
    assert code == 200 and job["status"] == "done"
    assert audio_stream(job["outputs"][0])["codec_name"] == "opus"


# ---------- volume booster (Phase 4) ----------
@pytest.mark.parametrize("boost", ["low", "medium", "high", "6"])
def test_volume_boost_single_segment(client, sample, boost):
    up = upload(client, sample)
    code, job = run_job(
        client, [{"filename": up["filename"], "segments": [{"start": 0, "end": 6}]}],
        base_opts(vibrance=1.05, volumeBoost=boost))
    assert code == 200 and job["status"] == "done"
    assert "volume boost" in _summary(job)
    assert decodes_clean(job["outputs"][0])


def test_volume_boost_multi_segment(client, sample):
    """Volume boost in the multi-segment filter_complex audio chain."""
    up = upload(client, sample)
    code, job = run_job(
        client, [{"filename": up["filename"],
                  "segments": [{"start": 0, "end": 2}, {"start": 3, "end": 6}]}],
        base_opts(volumeBoost="high"))
    assert code == 200 and job["status"] == "done"
    assert "volume boost" in _summary(job)
    out = job["outputs"][0]
    assert decodes_clean(out)
    assert audio_stream(out) is not None


# ---------- fps ----------
def test_fps_override_drops_rate(client, sample_60fps):
    up = upload(client, sample_60fps)
    code, job = run_job(
        client, [{"filename": up["filename"], "segments": [{"start": 0, "end": 5}]}],
        # vibrance forces the re-encode; fpsMode=30 then takes effect
        base_opts(vibrance=1.1, fpsMode="30"))
    assert code == 200 and job["status"] == "done"
    out = job["outputs"][0]
    assert decodes_clean(out)
    num, den = video_stream(out)["r_frame_rate"].split("/")
    assert abs(float(num) / float(den) - 30.0) < 1.0


# ---------- stretch / crop ----------
def test_stretch_crops_letterbox(client, sample_letterbox):
    up = upload(client, sample_letterbox)
    code, job = run_job(
        client, [{"filename": up["filename"], "segments": [{"start": 0, "end": 6}]}],
        base_opts(stretch=True))
    assert code == 200 and job["status"] == "done"
    out = job["outputs"][0]
    assert decodes_clean(out)
    vs = video_stream(out)
    # stretched output is 16:9-ish, much wider than the 4:3 source frame
    assert vs["width"] / vs["height"] > 1.6


# ---------- speed presets / hwaccel ----------
@pytest.mark.parametrize("speed", ["quality", "balanced", "fast"])
def test_encode_speeds(client, sample, speed):
    up = upload(client, sample)
    code, job = run_job(
        client, [{"filename": up["filename"], "segments": [{"start": 0, "end": 6}]}],
        base_opts(vibrance=1.05, encodeSpeed=speed))
    assert code == 200 and job["status"] == "done"
    assert decodes_clean(job["outputs"][0])


def test_hwaccel_decode_enabled(client, sample):
    up = upload(client, sample)
    code, job = run_job(
        client, [{"filename": up["filename"], "segments": [{"start": 0, "end": 6}]}],
        base_opts(vibrance=1.2, hwaccelDecode=True))
    assert code == 200 and job["status"] == "done"
    assert decodes_clean(job["outputs"][0])


# ---------- batch of multiple items in one job ----------
def test_batch_multiple_items(client, sample, sample_noaudio):
    a_up = upload(client, sample)
    b_up = upload(client, sample_noaudio)
    code, job = run_job(client, [
        {"filename": a_up["filename"], "segments": [{"start": 0, "end": 6}],
         "opts": {"vibrance": 1.2}},
        {"filename": b_up["filename"], "segments": [{"start": 0, "end": 6}],
         "opts": {"vibrance": 1.2}},
    ], base_opts())
    assert code == 200 and job["status"] == "done"
    assert len(job["outputs"]) == 2
    assert all(decodes_clean(o) for o in job["outputs"])
