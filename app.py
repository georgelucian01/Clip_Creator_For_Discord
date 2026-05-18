"""Clip Creator - Flask backend (smart pipeline edition).

Three-mode pipeline that avoids re-encoding whenever possible:
  PASSTHROUGH      - copy the file untouched (no cut, no filters, fits target)
  STREAM_COPY_CUT  - lossless keyframe cut via -c copy (near-instant)
  FULL_ENCODE      - real re-encode, size-targeted, with auto resolution/encoder/
                     audio scaling and optional two-pass.

Background jobs with polling, concurrency cap, cancellation. Discord upload via
user token, Bearer, Bot, or webhook URL.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path

import requests
from flask import (Flask, jsonify, render_template, request, send_file,
                   send_from_directory)
from werkzeug.utils import secure_filename

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("clip")

# When frozen by PyInstaller:
#   - bundled assets (templates/, static/) live in sys._MEIPASS (temp dir)
#   - user data (uploads/, output/, config.json, ffmpeg/) lives next to the .exe
# In dev mode both are the script dir.
FROZEN = getattr(sys, "frozen", False)
BUNDLE_DIR = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
BASE_DIR = Path(sys.executable).resolve().parent if FROZEN else Path(__file__).resolve().parent

UPLOAD_DIR = BASE_DIR / "uploads"
OUTPUT_DIR = BASE_DIR / "output"
CONFIG_FILE = BASE_DIR / "config.json"
# Validated-encoder cache - so the test encodes only run once per ffmpeg build.
ENCODER_CACHE_FILE = BASE_DIR / "encoder_cache.json"
# Thumbnail cache lives in the system temp dir - keeps output/ layout untouched.
THUMB_DIR = Path(tempfile.gettempdir()) / "clip_creator_thumbs"

for d in (UPLOAD_DIR, OUTPUT_DIR, THUMB_DIR):
    d.mkdir(exist_ok=True)

# Discord upload limits per tier (MB). discordTier overrides targetSizeMb.
DISCORD_TIERS = {"free": 10, "basic": 50, "nitro": 500}

DEFAULT_CONFIG = {
    "ffmpegPath": "ffmpeg/bin",
    "hardware": "amd",                 # cpu | nvidia | amd | intel
    "codec": "hevc",                   # h264 | hevc | av1
    "container": "mp4",                # mp4 | mkv | webm
    "targetSizeMb": 25,                # 0 = no size cap (CRF/CQ quality mode)
    "vibrance": 1.0,
    "stretch": False,
    "discordMode": "webhook",          # webhook | token
    "discordToken": "",
    "discordTokenPrefix": "",          # "" | "Bearer" | "Bot"
    "discordChannelId": "",
    "discordWebhook": "",
    # --- smart pipeline (new) ---
    "frameAccurateCut": False,         # true forces FULL_ENCODE for exact cuts
    "outputHeight": "auto",            # auto | source | 1440 | 1080 | 720 | 540 | 480
    "encoderQualityMode": "auto",      # auto | hardware_always | software_always
    "encodeSpeed": "quality",          # quality | balanced | fast - encoder preset
    "audioBitrateMode": "auto",        # "auto" or a numeric kbps value
    "audioCodec": "aac",               # aac | opus
    "twoPass": True,                   # applies only when targetSizeMb > 0
    "tenBit": False,                   # 10-bit output for hevc/av1
    "maxConcurrentJobs": 2,
    "lowBitrateDenoise": True,         # mild denoise when video_kbps < 1500
    "discordTier": "free",             # free | basic | nitro - overrides targetSizeMb
    "autoRetryOnDiscordReject": False,
    # --- phase 2 ---
    "hwaccelDecode": True,             # -hwaccel auto for FULL_ENCODE input
    "fpsMode": "auto",                 # auto | source | 60 | 48 | 30 | custom
    "fpsCustom": 48,                   # manual fps target when fpsMode=custom
    "hdrToneMap": True,                # tone-map HDR->SDR when needed
    "normalizeAudio": True,            # EBU R128 loudness normalization
    "denoiseStrength": "light",        # light | medium | strong
    "crfMaxrateMultiplier": 2.0,       # maxrate ceiling multiplier for CRF mode
    # --- phase 3 ---
    "doneAction": "process",           # DONE button default: process | process_send
    # --- phase 4 ---
    "volumeBoost": "none",             # none | low | medium | high | numeric dB
    "cleanupDays": 14,                 # delete uploads/output older than N days (0=never)
}

app = Flask(__name__,
            static_folder=str(BUNDLE_DIR / "static"),
            template_folder=str(BUNDLE_DIR / "templates"))
app.config["MAX_CONTENT_LENGTH"] = 4 * 1024 * 1024 * 1024  # 4 GB upload cap

MB = 1024 * 1024
VIDEO_FLOOR_KBPS = 300   # below this, output is unwatchable - refuse the job
CODEC_CEILINGS = {"h264": 10000, "hevc": 6000, "av1": 3500}


# ---------- config ----------
def migrate_config(old_cfg: dict) -> tuple[dict, int]:
    """Deep-merge an existing config over DEFAULT_CONFIG. Preserves user values,
    fills any missing keys with defaults. Returns (merged, n_new) where n_new is
    the count of keys that were absent and had to be defaulted. Key renames go
    in the marked block below as the schema evolves."""
    merged = DEFAULT_CONFIG.copy()
    n_new = 0
    for key in DEFAULT_CONFIG:
        if key in old_cfg:
            merged[key] = old_cfg[key]
        else:
            n_new += 1

    # --- handle renames here (example pattern for future use) ---
    # if "oldKeyName" in old_cfg:
    #     merged["newKeyName"] = old_cfg["oldKeyName"]

    return merged, n_new


def load_config() -> dict:
    """Merge config.json over DEFAULT_CONFIG - existing keys preserved, new keys
    take defaults, so old config files load cleanly."""
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                merged, _ = migrate_config(json.load(f))
                return merged
        except Exception as e:
            log.warning("config read failed: %s", e)
    return DEFAULT_CONFIG.copy()


def save_config(cfg: dict) -> None:
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)


def migrate_config_file() -> None:
    """On startup, rewrite config.json with any new keys filled in so the user
    sees and can hand-edit every available option."""
    if not CONFIG_FILE.exists():
        save_config(DEFAULT_CONFIG.copy())
        log.info("Config created with defaults")
        return
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            old = json.load(f)
    except Exception as e:
        log.warning("config migration skipped: %s", e)
        return
    merged, n_new = migrate_config(old)
    if n_new:
        save_config(merged)
        log.info("Config migrated: added %d new keys with defaults", n_new)


# ---------- ffmpeg helpers ----------
def get_bins() -> tuple[str, str]:
    p = load_config().get("ffmpegPath", "")
    if p and not os.path.isabs(p):
        p = str(BASE_DIR / p)
    if p:
        return str(Path(p) / "ffmpeg.exe"), str(Path(p) / "ffprobe.exe")
    return "ffmpeg", "ffprobe"


def normalize_codec(name: str) -> str:
    """Map ffprobe codec_name to our codec family."""
    n = (name or "").lower()
    if n in ("h264", "avc", "avc1"):
        return "h264"
    if n in ("hevc", "h265"):
        return "hevc"
    if n == "av1":
        return "av1"
    return n


# Probe cache - keyed by (filename, mtime, size). Invalidated automatically when
# any of those change. No eviction needed: one entry per uploaded file.
_PROBE_CACHE: dict[tuple, dict] = {}
_PROBE_LOCK = threading.Lock()


def probe_video(path: Path) -> dict:
    st = path.stat()
    key = (path.name, st.st_mtime, st.st_size)
    with _PROBE_LOCK:
        cached = _PROBE_CACHE.get(key)
    if cached:
        return cached

    _, ffprobe = get_bins()
    cmd = [ffprobe, "-v", "quiet", "-print_format", "json",
           "-show_format", "-show_streams", str(path)]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"ffprobe failed: {r.stderr}")
    data = json.loads(r.stdout)
    fmt = data.get("format", {})
    streams = data.get("streams", [])
    vstream = next((s for s in streams if s["codec_type"] == "video"), None)
    astream = next((s for s in streams if s["codec_type"] == "audio"), None)
    if not vstream:
        raise RuntimeError("no video stream")

    duration = float(fmt.get("duration", 0) or 0)
    size = int(fmt.get("size", 0) or 0)

    # total bitrate (kbps): container value, or estimated from size/duration
    total_kbps = 0.0
    if fmt.get("bit_rate"):
        total_kbps = float(fmt["bit_rate"]) / 1000
    elif duration > 0:
        total_kbps = (size * 8) / duration / 1000

    a_kbps = 0.0
    if astream and astream.get("bit_rate"):
        a_kbps = float(astream["bit_rate"]) / 1000
    elif astream:
        a_kbps = 128.0  # rough guess when the stream omits a bitrate

    if vstream.get("bit_rate"):
        v_kbps = float(vstream["bit_rate"]) / 1000
    else:
        v_kbps = max(0.0, total_kbps - a_kbps)

    # frame rate from r_frame_rate ("60/1")
    fps = 30.0
    rfr = vstream.get("r_frame_rate", "30/1")
    try:
        num, den = rfr.split("/")
        fps = float(num) / float(den) if float(den) else 30.0
    except Exception:
        pass

    # HDR detection - PQ (smpte2084) / HLG (arib-std-b67) transfer, or BT.2020.
    color_transfer = (vstream.get("color_transfer") or "").lower()
    color_space = (vstream.get("color_space") or "").lower()
    is_hdr = (color_transfer in ("smpte2084", "arib-std-b67")
              or color_space in ("bt2020nc", "bt2020c"))

    sw = int(vstream.get("width", 0))
    sh = int(vstream.get("height", 0))
    # suggest stretch-to-16:9 when the source is not ~16:9 (e.g. 4:3 CS gameplay
    # at 1920x1440). A pure default - the per-clip stretch control overrides it.
    aspect = (sw / sh) if sh else (16 / 9)
    suggest_stretch = abs(aspect - 16 / 9) > 0.05

    info = {
        "duration": duration,
        "width": sw,
        "height": sh,
        "suggest_stretch": suggest_stretch,
        "size": size,
        "codec": vstream.get("codec_name", ""),
        "vbitrate": v_kbps,                       # kbps
        "abitrate": a_kbps,                       # kbps
        "total_bitrate": total_kbps or (v_kbps + a_kbps),
        "fps": fps,
        "has_audio": astream is not None,
        "acodec": astream.get("codec_name", "") if astream else "",
        "is_hdr": is_hdr,
        "color_transfer": color_transfer,
    }
    # black-bar detection is deferred (lazy): cropdetect costs ~2s and is only
    # needed when stretch is actually enabled. ensure_crop() fills these on
    # demand from the FULL_ENCODE path. See ensure_crop().
    info["detected_crop"] = None
    info["crop_checked"] = False
    with _PROBE_LOCK:
        _PROBE_CACHE[key] = info
    return info


def detect_crop(path: Path, duration: float) -> tuple[int, int, int, int] | None:
    """Sample 2 s at the clip midpoint and report ffmpeg's suggested crop, or
    None if cropdetect produced nothing usable. The caller decides whether the
    crop is significant enough (>8 px from source) to actually apply."""
    ffmpeg, _ = get_bins()
    midpoint = max(0.0, duration / 2 - 1.0)
    cmd = [ffmpeg, "-hide_banner", "-ss", f"{midpoint:.3f}", "-i", str(path),
           "-t", "2", "-vf", "cropdetect=24:2:0", "-an", "-f", "null", "-"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except Exception:
        return None
    crop = None
    for line in r.stderr.split("\n"):
        if "crop=" in line:
            crop = line.split("crop=")[-1].split(" ")[0].strip()
    if not crop:
        return None
    try:
        w, h, x, y = map(int, crop.split(":"))
    except ValueError:
        return None
    return w, h, x, y


def ensure_crop(path: Path, info: dict) -> None:
    """Lazily run black-bar detection and store the result on `info`. Called from
    the FULL_ENCODE path only when stretch is enabled, so the ~2s cropdetect
    cost is never paid on a plain upload. `info` is the cached probe dict, so
    the result persists for the process lifetime; the crop_checked flag stops
    a clip with genuinely no bars from being re-probed on every encode."""
    if info.get("crop_checked"):
        return
    try:
        info["detected_crop"] = detect_crop(path, info.get("duration", 0.0))
    except Exception:
        info["detected_crop"] = None
    info["crop_checked"] = True


def encoder_for(codec: str, hardware: str) -> str:
    table = {
        "nvidia": {"h264": "h264_nvenc", "hevc": "hevc_nvenc", "av1": "av1_nvenc"},
        "amd":    {"h264": "h264_amf",   "hevc": "hevc_amf",   "av1": "av1_amf"},
        "intel":  {"h264": "h264_qsv",   "hevc": "hevc_qsv",   "av1": "av1_qsv"},
        "cpu":    {"h264": "libx264",    "hevc": "libx265",    "av1": "libsvtav1"},
    }
    return table.get(hardware, table["cpu"]).get(codec, "libx264")


# Encoders this ffmpeg build supports AND that actually produce usable output
# on this machine - validated once at startup, cached in encoder_cache.json.
AVAILABLE_ENCODERS: dict[str, bool] = {}
CPU_ENCODERS = ["libsvtav1", "libx265", "libx264"]
HW_ENCODERS = ["hevc_amf", "h264_amf", "av1_amf", "hevc_nvenc", "h264_nvenc",
               "av1_nvenc", "hevc_qsv", "h264_qsv", "av1_qsv"]
CPU_FALLBACK = {
    "hevc_amf": "libx265", "h264_amf": "libx264", "av1_amf": "libsvtav1",
    "hevc_nvenc": "libx265", "h264_nvenc": "libx264", "av1_nvenc": "libsvtav1",
    "hevc_qsv": "libx265", "h264_qsv": "libx264", "av1_qsv": "libsvtav1",
}
# Encoders that reliably support 10-bit (yuv420p10le) output. AMF and the H.264
# encoders are deliberately excluded - forcing 10-bit on them produces either a
# pixel-format error or a corrupt stream.
TEN_BIT_ENCODERS = {"libx265", "libsvtav1", "hevc_nvenc", "av1_nvenc",
                    "hevc_qsv", "av1_qsv"}


def _ffmpeg_fingerprint(ffmpeg: str) -> str:
    try:
        st = os.stat(ffmpeg)
        return f"{ffmpeg}:{st.st_size}:{int(st.st_mtime)}"
    except OSError:
        return ffmpeg


def _test_hw_encoder(ffmpeg: str, enc: str) -> bool:
    """Encode a few synthetic frames, then decode them back. Returns True only
    if the result is decodable WITHOUT errors. Catches the common trap where an
    encoder is listed by `ffmpeg -encoders` but the GPU silicon can't do it -
    e.g. av1_amf on a pre-RDNA3 AMD GPU, which encodes to visible garbage."""
    tmp = THUMB_DIR / f"_enctest_{enc}.mp4"
    try:
        enc_r = subprocess.run(
            [ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
             "-f", "lavfi", "-i", "testsrc2=s=640x480:r=30:d=1",
             "-c:v", enc, "-frames:v", "24", str(tmp)],
            capture_output=True, text=True, timeout=45)
        if enc_r.returncode != 0 or not tmp.exists() or tmp.stat().st_size < 512:
            return False
        # decode-verify: a corrupt hardware bitstream decodes with errors even
        # when ffmpeg exits 0, so an error-level stderr means the stream is bad.
        dec_r = subprocess.run(
            [ffmpeg, "-hide_banner", "-loglevel", "error",
             "-i", str(tmp), "-f", "null", "-"],
            capture_output=True, text=True, timeout=45)
        if dec_r.returncode != 0:
            return False
        return not (dec_r.stderr or "").strip()
    except Exception:
        return False
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass


def probe_encoders() -> dict[str, bool]:
    """Determine which encoders are usable on this machine. CPU encoders are
    trusted from the `ffmpeg -encoders` list; hardware encoders are additionally
    validated with a real test encode (results cached per ffmpeg build)."""
    ffmpeg, _ = get_bins()
    listed: set[str] = set()
    try:
        r = subprocess.run([ffmpeg, "-hide_banner", "-encoders"],
                           capture_output=True, text=True, timeout=15)
        out = r.stdout or ""
        for n in CPU_ENCODERS + HW_ENCODERS:
            if n in out:
                listed.add(n)
    except Exception as e:
        log.warning("encoder list probe failed: %s", e)

    # load cached hardware-validation results (keyed to this ffmpeg binary)
    fp = _ffmpeg_fingerprint(ffmpeg)
    cache: dict = {}
    try:
        if ENCODER_CACHE_FILE.exists():
            blob = json.loads(ENCODER_CACHE_FILE.read_text(encoding="utf-8"))
            if blob.get("fingerprint") == fp:
                cache = blob.get("encoders", {})
    except Exception:
        cache = {}

    result = {n: (n in listed) for n in CPU_ENCODERS}
    pending = [n for n in HW_ENCODERS if n in listed and n not in cache]
    if pending:
        log.info("validating hardware encoders (one-time): %s",
                 ", ".join(pending))
    changed = False
    for n in HW_ENCODERS:
        if n not in listed:
            result[n] = False
            continue
        if n in cache:
            result[n] = bool(cache[n])
            continue
        ok = _test_hw_encoder(ffmpeg, n)
        result[n] = ok
        cache[n] = ok
        changed = True
        log.info("encoder test: %s -> %s", n,
                 "OK" if ok else "FAILED (produces unusable output - disabled)")
    if changed:
        try:
            ENCODER_CACHE_FILE.write_text(
                json.dumps({"fingerprint": fp, "encoders": cache}, indent=2),
                encoding="utf-8")
        except Exception as e:
            log.warning("could not write encoder cache: %s", e)

    have = [n for n, ok in result.items() if ok]
    log.info("usable encoders: %s", ", ".join(have) or "none")
    return result


def verify_encoder(encoder: str) -> tuple[str, str | None]:
    """If the chosen encoder is not usable on this machine (not in this ffmpeg
    build, or previously found to produce corrupt output), fall back to the CPU
    equivalent."""
    if not AVAILABLE_ENCODERS or AVAILABLE_ENCODERS.get(encoder, True):
        return encoder, None
    fallback = CPU_FALLBACK.get(encoder, "libx264")
    return fallback, f"encoder {encoder} unavailable - falling back to {fallback}"


def output_decodes_ok(path: Path) -> bool:
    """Decode the whole file and return False if ffmpeg reports stream errors.
    A flaky hardware encoder can write a file that looks fine by size but is
    corrupt garbage on playback - this catches that."""
    ffmpeg, _ = get_bins()
    try:
        r = subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error",
                            "-i", str(path), "-f", "null", "-"],
                           capture_output=True, text=True, timeout=240)
    except Exception as e:
        log.warning("output decode-verify could not run: %s", e)
        return True  # don't block delivery on a verifier failure
    if r.returncode != 0:
        return False
    return not (r.stderr or "").strip()


def mark_encoder_broken(enc: str) -> None:
    """Permanently disable an encoder that produced a corrupt stream, so future
    jobs skip straight to the CPU fallback instead of re-encoding twice."""
    AVAILABLE_ENCODERS[enc] = False
    ffmpeg, _ = get_bins()
    fp = _ffmpeg_fingerprint(ffmpeg)
    try:
        cache: dict = {}
        if ENCODER_CACHE_FILE.exists():
            blob = json.loads(ENCODER_CACHE_FILE.read_text(encoding="utf-8"))
            if blob.get("fingerprint") == fp:
                cache = blob.get("encoders", {})
        cache[enc] = False
        ENCODER_CACHE_FILE.write_text(
            json.dumps({"fingerprint": fp, "encoders": cache}, indent=2),
            encoding="utf-8")
    except Exception as e:
        log.warning("could not persist broken-encoder mark: %s", e)


# ---------- decision helpers ----------
def normalize_segments(item: dict) -> list[tuple[float, float]]:
    """Keep-segments as (start, end) tuples in playback order. Accepts the new
    {segments: [{start, end}, ...]} shape, or the legacy flat {start, end} as a
    single-segment fallback so nothing breaks mid-migration."""
    raw = item.get("segments")
    if raw is None:
        return [(float(item["start"]), float(item["end"]))]
    if not isinstance(raw, list) or not raw:
        raise ValueError("segments must be a non-empty list")
    out: list[tuple[float, float]] = []
    for seg in raw:
        out.append((float(seg["start"]), float(seg["end"])))
    return out


def decide_mode(input_path: Path, info: dict, opts: dict,
                segments: list[tuple[float, float]]) -> tuple[str, str, dict]:
    """Classify a job into PASSTHROUGH / STREAM_COPY_CUT / FULL_ENCODE.

    The order matters: anything that *requires* pixels to be touched (filters,
    codec/container change, frame-accurate cut) forces FULL_ENCODE first. Only
    then do we consider whether a copy-based path fits the size budget.
    """
    duration = info["duration"]
    # multi-segment joins always re-encode - the trim+concat needs a filtergraph.
    if len(segments) > 1:
        return ("FULL_ENCODE",
                f"multi-segment join ({len(segments)} keep-segments)", {})
    start, end = segments[0]
    cut_duration = max(0.0, end - start)
    # cut needed if either edge is more than 100 ms from the source bounds
    cut_needed = start > 0.1 or (duration - end) > 0.1

    vibrance = float(opts.get("vibrance", 1.0))
    filters = bool(opts.get("stretch")) or abs(vibrance - 1.0) > 0.01

    src_family = normalize_codec(info.get("codec", ""))
    want_codec = opts.get("codec", "h264")
    src_container = input_path.suffix.lower().lstrip(".")
    want_container = opts.get("container", "mp4")
    codec_change = src_family != want_codec
    container_change = src_container != want_container

    target_mb = float(opts.get("targetSizeMb", 0) or 0)
    frame_accurate = bool(opts.get("frameAccurateCut", False))

    if frame_accurate and cut_needed:
        return "FULL_ENCODE", "frameAccurateCut=true forces re-encode for an exact cut", {}
    if filters:
        return "FULL_ENCODE", "filters required (stretch/vibrance)", {}
    if codec_change:
        return "FULL_ENCODE", f"codec change {src_family or '?'} -> {want_codec}", {}
    if container_change:
        return "FULL_ENCODE", f"container change {src_container or '?'} -> {want_container}", {}

    if not cut_needed:
        if target_mb <= 0 or info["size"] <= target_mb * MB:
            return "PASSTHROUGH", "passthrough: file already meets all criteria", {}
        return ("FULL_ENCODE",
                f"file {info['size']/MB:.1f}MB exceeds {target_mb}MB target", {})

    # cut only, no filters, no codec/container change - estimate copy output size.
    # input_bitrate x cut_duration + audio overhead, x1.05 for container overhead.
    total_kbps = info["vbitrate"] + info["abitrate"]
    predicted_bytes = (total_kbps * 1000 / 8) * cut_duration * 1.05
    extra = {"predicted_mb": predicted_bytes / MB}
    if target_mb <= 0 or predicted_bytes <= target_mb * MB:
        return ("STREAM_COPY_CUT",
                f"cut only - predicted ~{predicted_bytes/MB:.1f}MB fits target", extra)
    return ("FULL_ENCODE",
            f"predicted copy ~{predicted_bytes/MB:.1f}MB exceeds {target_mb}MB target", extra)


def pick_resolution(video_kbps: float | None, source_height: int,
                    override: str) -> tuple[int, str]:
    """Pick output height. Source height is the ceiling - never upscale.

    Low bitrates encode much cleaner at a lower resolution: a 1 Mbps stream
    looks blocky at 1440p but acceptable at 540p. The table maps the available
    video bitrate to the highest resolution that still encodes cleanly.
    """
    if override and override != "auto":
        if override == "source":
            return source_height, "outputHeight=source"
        try:
            h = int(override)
            return min(h, source_height), f"outputHeight override -> {min(h, source_height)}p"
        except ValueError:
            pass
    if video_kbps is None:
        return source_height, "no size target -> source resolution"
    if video_kbps >= 6000:
        h = source_height
    elif video_kbps >= 3500:
        h = 1080
    elif video_kbps >= 1800:
        h = 720
    elif video_kbps >= 1000:
        h = 540
    else:
        h = 480
    h = min(h, source_height)
    return h, f"auto: video_kbps={video_kbps/1000:.1f} Mbps -> {h}p"


def pick_encoder(codec: str, hardware: str, video_kbps: float | None,
                 quality_mode: str) -> tuple[str, bool, str]:
    """Pick the encoder. Hardware encoders fall apart at low bitrates, so when
    the video budget is tight we swap to the software encoder."""
    hw_enc = encoder_for(codec, hardware)
    cpu_enc = encoder_for(codec, "cpu")
    if quality_mode == "software_always":
        return cpu_enc, (hw_enc != cpu_enc), "software_always"
    if quality_mode == "hardware_always":
        return hw_enc, False, "hardware_always"
    # auto
    threshold = {"h264": 2500, "hevc": 1800, "av1": 1500}.get(codec, 2500)
    if video_kbps is not None and video_kbps < threshold and hardware != "cpu":
        return cpu_enc, True, (f"auto: video {video_kbps:.0f}kbps < {threshold}kbps "
                               f"-> CPU encoder {cpu_enc}")
    return hw_enc, False, "auto: bitrate adequate for hardware encoder"


def pick_audio_bitrate(total_kbps: float | None, mode) -> int:
    """Adaptive audio bitrate - at tight total budgets, fixed 128k steals bits
    that the video needs far more."""
    if mode not in (None, "", "auto"):
        try:
            return int(float(mode))
        except (TypeError, ValueError):
            pass
    if total_kbps is None:
        return 128
    if total_kbps > 4000:
        return 192
    if total_kbps >= 2000:
        return 128
    if total_kbps >= 1000:
        return 96
    return 64


def resolve_audio_codec(cfg_codec: str, container: str,
                        audio_kbps: int) -> tuple[str, str]:
    """Return (ffmpeg_encoder_name, label). Opus beats AAC at low bitrates, so
    switch to it for webm or when the audio budget drops below 96 kbps."""
    if cfg_codec == "opus":
        return "libopus", "opus"
    if container == "webm" or audio_kbps < 96:
        return "libopus", "opus (auto: webm/low-bitrate)"
    return "aac", "aac"


SPEED_LEVELS = ("quality", "balanced", "fast")

# Per-encoder -preset / -quality value for each encode-speed level. The
# "quality" column reproduces the original hardcoded values, so the default
# (encodeSpeed="quality") leaves encoding behaviour unchanged.
_SPEED_PRESET = {
    "libx264":   {"quality": "slow",    "balanced": "medium",  "fast": "veryfast"},
    "libx265":   {"quality": "slow",    "balanced": "medium",  "fast": "fast"},
    "libsvtav1": {"quality": "6",       "balanced": "8",       "fast": "10"},
    "amf":       {"quality": "quality", "balanced": "balanced","fast": "speed"},
    "nvenc":     {"quality": "p7",      "balanced": "p5",      "fast": "p3"},
    "qsv":       {"quality": "slower",  "balanced": "medium",  "fast": "veryfast"},
}


def speed_preset(encoder: str, speed: str) -> str:
    """The encoder-specific -preset / -quality value for the chosen speed."""
    speed = speed if speed in SPEED_LEVELS else "quality"
    if encoder in _SPEED_PRESET:
        return _SPEED_PRESET[encoder][speed]
    for fam in ("amf", "nvenc", "qsv"):
        if fam in encoder:
            return _SPEED_PRESET[fam][speed]
    return _SPEED_PRESET["libx264"][speed]


def quality_args(encoder: str, speed: str = "quality") -> list[str]:
    """CRF/CQ args - used when no size target is set."""
    p = speed_preset(encoder, speed)
    if encoder == "libx265":
        return ["-preset", p, "-crf", "22"]
    if encoder == "libx264":
        return ["-preset", p, "-crf", "20"]
    if encoder == "libsvtav1":
        return ["-preset", p, "-crf", "28"]
    if encoder in ("hevc_amf", "h264_amf", "av1_amf"):
        return ["-rc", "cqp", "-qp_i", "20", "-qp_p", "22", "-qp_b", "24",
                "-quality", p]
    if encoder in ("hevc_nvenc", "h264_nvenc"):
        return ["-preset", p, "-tune", "hq", "-rc", "vbr", "-cq", "20"]
    if encoder == "av1_nvenc":
        return ["-preset", p, "-tune", "hq", "-rc", "vbr", "-cq", "28"]
    if "qsv" in encoder:
        return ["-preset", p, "-global_quality", "23"]
    return ["-preset", p, "-crf", "22"]


def bitrate_mode_args(encoder: str, speed: str = "quality") -> list[str]:
    """Per-encoder tuning for bitrate-targeted (size-capped) encoding."""
    p = speed_preset(encoder, speed)
    if encoder == "libx265":
        return ["-preset", p,
                "-x265-params", "aq-mode=3:psy-rd=2.0:psy-rdoq=1.0"]
    if encoder == "libx264":
        return ["-preset", p, "-tune", "film"]
    if encoder == "libsvtav1":
        return ["-preset", p, "-svtav1-params", "tune=0"]
    if encoder in ("hevc_amf", "h264_amf", "av1_amf"):
        return ["-quality", p, "-usage", "transcoding"]
    if encoder in ("hevc_nvenc", "h264_nvenc"):
        return ["-preset", p, "-tune", "hq", "-rc", "vbr",
                "-multipass", "fullres"]
    if encoder == "av1_nvenc":
        return ["-preset", p, "-tune", "hq"]
    if "qsv" in encoder:
        return ["-preset", p]
    return ["-preset", p]


def structural_args(encoder: str, codec: str, opts: dict,
                    fps: float, duration: float,
                    ten_bit: bool = False) -> list[str]:
    """pix_fmt, GOP and B-frame args shared by both encode modes. `ten_bit` is
    the EFFECTIVE 10-bit decision (already checked against TEN_BIT_ENCODERS)."""
    args: list[str] = []
    # 10-bit at the same bitrate has noticeably less banding (hevc/av1 only).
    if ten_bit:
        args += ["-pix_fmt", "yuv420p10le"]
    else:
        args += ["-pix_fmt", "yuv420p"]

    # GOP: 2-second keyframes normally; for very short clips make the whole clip
    # one GOP so the encoder can compress across all frames.
    if duration < 3.0:
        total_frames = max(1, int(fps * duration))
        args += ["-g", str(total_frames)]
    else:
        # Scene-aware placement: 2s max GOP, 1s min interval so the encoder can
        # still insert a keyframe on a scene change without over-spending.
        # Note: the generic -sc_threshold AVOption is NOT honored by libx264/
        # libx265/libsvtav1/hardware encoders (they run their own scene-cut by
        # default - x264's default scenecut is already 40), and passing it just
        # emits an "option not used" warning. -keyint_min IS honored.
        args += ["-g", str(max(1, int(fps * 2))),
                 "-keyint_min", str(max(1, int(fps)))]

    # B-frames where supported. av1_amf has limited B-frame support.
    if codec in ("h264", "hevc"):
        args += ["-bf", "3"]
        if encoder in ("libx264", "libx265"):
            args += ["-refs", "4"]
    elif encoder == "av1_amf":
        args += ["-bf", "2"]
    return args


# ---------- job system ----------
JOBS: dict[str, dict] = {}
JOBS_LOCK = threading.Lock()
JOBS_CAP = 50
DURATION_RE = re.compile(r"time=(\d+):(\d+):(\d+\.\d+)")

# Concurrency cap - limits how many jobs encode at once. Sized once at startup.
_MAX_CONCURRENT = int(load_config().get("maxConcurrentJobs", 2) or 2)
JOB_SEMAPHORE = threading.BoundedSemaphore(max(1, _MAX_CONCURRENT))


class JobCancelled(Exception):
    """Raised when a running job was cancelled via the API."""


# ---------- queue persistence (graceful shutdown / restart) ----------
# Pending job specs are mirrored to disk so an unfinished queue survives an app
# restart. No DB - just a small JSON sidecar next to config.json.
JOBS_STATE_FILE = BASE_DIR / "jobs_state.json"
_STATE_LOCK = threading.Lock()


def _read_state() -> dict:
    if not JOBS_STATE_FILE.exists():
        return {}
    try:
        return json.loads(JOBS_STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _write_state(data: dict) -> None:
    try:
        JOBS_STATE_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except Exception as e:
        log.warning("could not persist job state: %s", e)


def persist_pending(job_id: str, items: list, opts: dict, details: dict) -> None:
    with _STATE_LOCK:
        data = _read_state()
        data[job_id] = {"items": items, "opts": opts, "details": details,
                        "created": time.time()}
        _write_state(data)


def unpersist_pending(job_id: str) -> None:
    with _STATE_LOCK:
        data = _read_state()
        if job_id in data:
            del data[job_id]
            _write_state(data)


def _register_job(job_id: str, items: list, opts: dict,
                   details: dict, signature: str = "") -> None:
    with JOBS_LOCK:
        JOBS[job_id] = {
            "status": "queued", "stage": "queued", "progress": 0.0, "done": 0,
            "total": len(items), "outputs": [], "errors": [], "summaries": [],
            "proc": None, "cancel_requested": False, "log": [],
            "created": time.time(), "signature": signature, "details": details,
        }


def restore_jobs() -> None:
    """On startup, re-queue any job that didn't finish before the last shutdown.
    Running jobs simply restart from the top - outputs are overwritten."""
    data = _read_state()
    if not data:
        return
    for job_id, spec in list(data.items()):
        items = spec.get("items") or []
        opts = spec.get("opts") or {}
        details = spec.get("details") or {}
        if not items:
            unpersist_pending(job_id)
            continue
        _register_job(job_id, items, opts, details)
        log.info("restored job %s (%d item(s)) from previous session",
                 job_id, len(items))
        threading.Thread(target=run_job, args=(job_id, items, opts),
                         daemon=True).start()


def update_job(job_id: str, **kw):
    with JOBS_LOCK:
        if job_id in JOBS:
            JOBS[job_id].update(kw)


def set_stage(job_id: str, stage: str):
    with JOBS_LOCK:
        if job_id in JOBS:
            JOBS[job_id]["stage"] = stage


def job_append_output(job_id: str, name: str):
    with JOBS_LOCK:
        if job_id in JOBS:
            JOBS[job_id]["outputs"].append(name)


def job_append_error(job_id: str, msg: str):
    with JOBS_LOCK:
        if job_id in JOBS:
            JOBS[job_id]["errors"].append(msg)


def job_cancelled(job_id: str) -> bool:
    with JOBS_LOCK:
        return bool(JOBS.get(job_id, {}).get("cancel_requested"))


def evict_old_jobs():
    """Cap JOBS at JOBS_CAP - drop the oldest finished entries when over."""
    with JOBS_LOCK:
        if len(JOBS) <= JOBS_CAP:
            return
        finished = sorted(
            (jid for jid, j in JOBS.items()
             if j.get("status") in ("done", "cancelled")),
            key=lambda jid: JOBS[jid].get("created", 0),
        )
        while len(JOBS) > JOBS_CAP and finished:
            del JOBS[finished.pop(0)]


def run_ffmpeg_with_progress(job_id: str, cmd: list[str], duration: float,
                             on_progress) -> None:
    """Run ffmpeg, stream progress, capture stderr. Registers the process on the
    job dict so the cancel endpoint can terminate it."""
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                            text=True, encoding="utf-8", errors="replace")
    with JOBS_LOCK:
        if job_id in JOBS:
            JOBS[job_id]["proc"] = proc
    last_err: list[str] = []
    assert proc.stderr is not None
    for line in proc.stderr:
        last_err.append(line)
        if len(last_err) > 200:
            last_err = last_err[-200:]
        m = DURATION_RE.search(line)
        if m and duration > 0:
            t = int(m[1]) * 3600 + int(m[2]) * 60 + float(m[3])
            on_progress(min(1.0, t / duration))
    proc.wait()
    with JOBS_LOCK:
        if job_id in JOBS:
            JOBS[job_id]["proc"] = None
            JOBS[job_id]["log"] = list(last_err)
    if proc.returncode != 0:
        if job_cancelled(job_id):
            raise JobCancelled()
        raise RuntimeError("".join(last_err)[-1200:])


def input_args(ffmpeg: str, input_path: Path, start: float,
               duration: float, hwaccel: bool = False,
               multi: bool = False) -> list[str]:
    """Common ffmpeg prefix. -ss before -i = fast seek. -fflags +genpts repairs
    bad PTS from some game-capture tools. -hwaccel auto (placed before -i) lets
    ffmpeg GPU-decode the input, falling back to software silently if it can't.

    Note: only -hwaccel auto is passed. -hwaccel_output_format does NOT accept
    'auto' (ffmpeg errors out and disables hwaccel entirely); leaving it unset
    lets ffmpeg download decoded frames to a software format, which is what the
    software -vf filter chain needs anyway.

    When `multi` is set the whole file is decoded (no -ss/-t): the
    -filter_complex trim graph does the cutting itself."""
    args = [ffmpeg, "-y", "-hide_banner", "-loglevel", "warning", "-stats",
            "-fflags", "+genpts"]
    if hwaccel:
        args += ["-hwaccel", "auto"]
    if multi:
        args += ["-i", str(input_path)]
    else:
        args += ["-ss", f"{start:.3f}", "-i", str(input_path),
                 "-t", f"{duration:.3f}"]
    return args


def build_video_graph(segments: list[tuple[float, float]],
                      vfx: list[str]) -> tuple[str, list[str]]:
    """Video-only -filter_complex: trim each keep-segment, concat them, then run
    the FULL_ENCODE video filter chain. Returns (graph, ["-map", "[vout]"])."""
    n = len(segments)
    parts: list[str] = []
    ins: list[str] = []
    for i, (s, e) in enumerate(segments):
        parts.append(f"[0:v]trim={s:.3f}:{e:.3f},setpts=PTS-STARTPTS[v{i}]")
        ins.append(f"[v{i}]")
    parts.append("".join(ins) + f"concat=n={n}:v=1:a=0[cv]")
    parts.append("[cv]" + (",".join(vfx) if vfx else "null") + "[vout]")
    return ";".join(parts), ["-map", "[vout]"]


def build_av_graph(segments: list[tuple[float, float]], vfx: list[str],
                   afx: list[str]) -> tuple[str, list[str], list[str]]:
    """Video+audio -filter_complex: trim+concat both streams, run the video
    filter chain, and run the audio filter chain `afx` (loudnorm, volume boost).
    The audio chain has to live in the graph because -af cannot coexist with
    -filter_complex. Returns (graph, video_map, audio_map)."""
    n = len(segments)
    parts: list[str] = []
    ins: list[str] = []
    for i, (s, e) in enumerate(segments):
        parts.append(f"[0:v]trim={s:.3f}:{e:.3f},setpts=PTS-STARTPTS[v{i}]")
        parts.append(f"[0:a]atrim={s:.3f}:{e:.3f},asetpts=PTS-STARTPTS[a{i}]")
        ins.append(f"[v{i}]")
        ins.append(f"[a{i}]")
    parts.append("".join(ins) + f"concat=n={n}:v=1:a=1[cv][ca]")
    parts.append("[cv]" + (",".join(vfx) if vfx else "null") + "[vout]")
    if afx:
        parts.append("[ca]" + ",".join(afx) + "[aout]")
        return ";".join(parts), ["-map", "[vout]"], ["-map", "[aout]"]
    return ";".join(parts), ["-map", "[vout]"], ["-map", "[ca]"]


# Volume-booster presets, in dB of gain. A numeric volumeBoost value is taken
# as raw dB; "none" / "" / 0 disables the boost.
VOLUME_PRESETS = {"low": 3.0, "medium": 6.0, "high": 10.0}


def resolve_volume_db(value) -> float:
    """dB of gain for a volumeBoost setting: a preset name, a numeric dB value,
    or none/off (0 dB)."""
    if value in (None, "", "none", "off"):
        return 0.0
    if isinstance(value, str) and value in VOLUME_PRESETS:
        return VOLUME_PRESETS[value]
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def audio_filters(normalize_audio: bool, volume_db: float) -> list[str]:
    """The audio filter chain shared by both encode paths: EBU R128 loudness
    normalization first, then an optional manual volume boost on top. The boost
    rides AFTER loudnorm so it can push the clip louder than the -16 LUFS
    target rather than being flattened back to it."""
    afx: list[str] = []
    if normalize_audio:
        afx.append("loudnorm=I=-16:TP=-1.5:LRA=11")
    if abs(volume_db) > 0.01:
        afx.append(f"volume={volume_db:.1f}dB")
    return afx


DENOISE_PRESETS = {
    "light": "hqdn3d=1.5:1.5:6:6",
    "medium": "hqdn3d=2.0:2.0:4:4",
    "strong": "hqdn3d=3.0:3.0:3:3",
}

# Tone-map PQ/HLG HDR to BT.709 SDR (Hable operator, no extra desaturation).
HDR_TONEMAP = ("zscale=t=linear:npl=100,format=gbrpf32le,zscale=p=bt709,"
               "tonemap=hable:desat=0,zscale=t=bt709:m=bt709:r=tv,format=yuv420p")

FPS_FLOOR = 30  # never output below this - 30 is the lowest acceptable smoothness


def resolve_target_fps(opts: dict, source_fps: float,
                       video_kbps: float | None) -> tuple[int | None, str | None]:
    """Decide the output frame rate. Returns (fps, note); fps None means keep
    the source rate untouched.

      auto    - drop a high-fps source toward 30 when the bitrate is tight
      source  - never change the frame rate
      60/48/30/custom - a fixed manual target

    The output is never below FPS_FLOOR (30) and never above the source rate."""
    mode = str(opts.get("fpsMode", "auto"))
    if mode in ("source", "keep", "", "none"):
        return None, None

    if mode == "auto":
        if video_kbps is None or source_fps <= FPS_FLOOR:
            return None, None
        if video_kbps < 1200:
            return FPS_FLOOR, f"fps auto: dropped to {FPS_FLOOR} (very tight budget)"
        if video_kbps < 2200 and source_fps > 48:
            return 48, "fps auto: dropped to 48 (low bitrate budget)"
        return None, None

    # manual numeric target
    raw = opts.get("fpsCustom", 48) if mode == "custom" else mode
    try:
        target = int(round(float(raw)))
    except (TypeError, ValueError):
        return None, None
    target = max(FPS_FLOOR, target)
    if target >= source_fps - 0.01:
        return None, (f"fps target {target} >= source {source_fps:.0f} "
                      f"- keeping source")
    return target, f"fps set to {target} (manual target)"


def build_filters(opts: dict, info: dict, source_height: int,
                   target_height: int, video_kbps: float | None,
                   codec: str, ten_bit: bool = False) -> tuple[list[str], list[str]]:
    """Assemble the -vf chain for FULL_ENCODE. Returns (filters, notes).
    `ten_bit` is the EFFECTIVE 10-bit decision made by the caller."""
    vf: list[str] = []
    notes: list[str] = []
    vibrance = float(opts.get("vibrance", 1.0))
    source_fps = float(info.get("fps", 30.0) or 30.0)
    is_hdr = bool(info.get("is_hdr"))

    # HDR -> SDR tone mapping. 10-bit hevc/av1 keeps HDR; otherwise tone-map.
    if is_hdr:
        if opts.get("hdrToneMap", True):
            if ten_bit:
                notes.append("HDR detected: passing through 10-bit HDR")
            else:
                vf.append(HDR_TONEMAP)
                notes.append("HDR detected: tone-mapping PQ->BT.709 (Hable)")
        else:
            notes.append("HDR detected: tone mapping disabled (hdrToneMap=false)")

    # frame rate - resolved before any scale filter so we scale fewer frames.
    target_fps, fps_note = resolve_target_fps(opts, source_fps, video_kbps)
    if target_fps:
        vf.append(f"fps={target_fps}")
    if fps_note:
        notes.append(fps_note)

    if opts.get("stretch"):
        # only crop+stretch when real black bars are detected. A clip with no
        # bars (already 16:9) is left untouched - stretching it would distort.
        crop = info.get("detected_crop")
        sw = int(info.get("width", 0) or 0)
        sh = int(info.get("height", 0) or 0)
        if crop and (abs(crop[0] - sw) > 8 or abs(crop[1] - sh) > 8):
            cw, ch, cx, cy = crop
            vf.append(f"crop={cw}:{ch}:{cx}:{cy}")
            notes.append(f"stretchTo169: cropping black bars to {cw}x{ch}")
            out_w = (round(target_height * 16 / 9) // 2) * 2
            vf.append(f"scale={out_w}:{target_height}:flags=lanczos")
            vf.append("setsar=1")
        else:
            notes.append("stretchTo169: no black bars detected - leaving "
                         "aspect ratio unchanged")
            if source_height > target_height:
                vf.append(f"scale=-2:{target_height}:flags=lanczos")
    elif source_height > target_height:
        # -2 keeps width divisible by 2 while preserving aspect ratio
        vf.append(f"scale=-2:{target_height}:flags=lanczos")

    # mild denoise at low bitrates: the encoder otherwise wastes bits trying to
    # preserve capture noise. Trades a little detail for a cleaner result.
    if (opts.get("lowBitrateDenoise", True) and video_kbps is not None
            and video_kbps < 1500):
        strength = str(opts.get("denoiseStrength", "light"))
        vf.append(DENOISE_PRESETS.get(strength, DENOISE_PRESETS["light"]))
        notes.append(f"denoise: {strength} (hqdn3d params)")

    if abs(vibrance - 1.0) > 0.01:
        vf.append(f"eq=saturation={vibrance:.3f}")
    return vf, notes


def process_clip(job_id: str, input_path: Path, output_path: Path,
                 segments: list[tuple[float, float]], opts: dict, info: dict,
                 temp_dir: Path, on_progress) -> str:
    """Process one clip. `segments` is a list of (start, end) keep-ranges in
    playback order; >1 segment is always a FULL_ENCODE trim+concat join.
    Returns a short human-readable summary of what it did."""
    ffmpeg, _ = get_bins()
    multi = len(segments) > 1
    total_kept = sum(max(0.0, e - s) for s, e in segments)
    duration = max(0.1, total_kept)
    start, end = segments[0]   # single-segment copy paths use the first range

    mode, reason, extra = decide_mode(input_path, info, opts, segments)
    log.info("[%s] mode=%s - %s", job_id, mode, reason)

    # ---- PASSTHROUGH: nothing to do, just copy ----
    if mode == "PASSTHROUGH":
        set_stage(job_id, "passthrough")
        shutil.copy2(input_path, output_path)
        on_progress(1.0)
        return f"passthrough - {reason}"

    # ---- STREAM_COPY_CUT: lossless keyframe cut ----
    if mode == "STREAM_COPY_CUT":
        set_stage(job_id, "stream_copy")
        # cut snaps to the nearest preceding keyframe (typically <1-2s off for
        # game capture). avoid_negative_ts make_zero => plays from frame 1.
        cmd = [ffmpeg, "-y", "-hide_banner", "-loglevel", "warning", "-stats",
               "-ss", f"{start:.3f}", "-to", f"{end:.3f}", "-i", str(input_path),
               "-c", "copy", "-avoid_negative_ts", "make_zero", str(output_path)]
        log.info("[%s] ffmpeg: %s", job_id, " ".join(cmd))
        run_ffmpeg_with_progress(job_id, cmd, duration, on_progress)
        on_progress(1.0)
        pm = extra.get("predicted_mb")
        return f"stream-copy cut - {reason}" + (
            f" (predicted {pm:.1f}MB)" if pm else "")

    # ---- FULL_ENCODE ----
    set_stage(job_id, "probing")
    codec = opts.get("codec", "h264")
    container = opts.get("container", "mp4")
    target_mb = float(opts.get("targetSizeMb", 0) or 0)
    fps = info.get("fps", 30.0)
    source_height = info.get("height", 0) or 1080

    # lazy black-bar detection - only pay the cropdetect cost when stretch is on
    if opts.get("stretch"):
        ensure_crop(input_path, info)

    notes: list[str] = []
    if multi:
        notes.append(f"multi-segment: {len(segments)} keep-segments joined "
                     f"(total {total_kept:.1f}s kept)")
    video_kbps: float | None = None
    audio_kbps = pick_audio_bitrate(None, opts.get("audioBitrateMode", "auto"))
    total_kbps: float | None = None

    if target_mb > 0:
        # bits available (0.95 leaves headroom for container overhead)
        total_kbps = (target_mb * 8 * 1024 * 0.95) / duration
        audio_kbps = pick_audio_bitrate(total_kbps, opts.get("audioBitrateMode", "auto"))
        video_kbps = total_kbps - audio_kbps
        ceiling = CODEC_CEILINGS.get(codec, 10000)
        if video_kbps > ceiling:
            notes.append(f"video bitrate capped at codec ceiling {ceiling}kbps")
            video_kbps = ceiling
        if video_kbps < VIDEO_FLOOR_KBPS:
            raise RuntimeError(
                f"Cannot fit {duration:.0f}s into {target_mb:.0f}MB at acceptable "
                f"quality (would need {video_kbps:.0f}kbps video, floor is "
                f"{VIDEO_FLOOR_KBPS}kbps). Reduce duration, raise the target, or "
                f"pick a lower-resolution output.")
        notes.append(f"budget: total={total_kbps:.0f}kbps "
                     f"video={video_kbps:.0f}kbps audio={audio_kbps}kbps")

    # resolution
    target_height, res_reason = pick_resolution(
        video_kbps, source_height, str(opts.get("outputHeight", "auto")))
    notes.append(f"resolution {res_reason}")

    # encoder
    encoder, swapped, enc_reason = pick_encoder(
        codec, opts.get("hardware", "cpu"), video_kbps,
        opts.get("encoderQualityMode", "auto"))
    encoder, verify_note = verify_encoder(encoder)
    if verify_note:
        notes.append(verify_note)
    notes.append(f"encoder {encoder} ({enc_reason})")

    # audio codec + args - EBU R128 loudness normalization keeps clips at a
    # consistent volume across different source games.
    aud_enc, aud_label = resolve_audio_codec(
        opts.get("audioCodec", "aac"), container, audio_kbps)
    # audio filter chain: loudnorm + optional volume boost. The single-segment
    # path rides it on -af; multi-segment cannot (-af and -filter_complex are
    # mutually exclusive) so it goes into the filtergraph on the [ca] pad
    # instead - see build_av_graph().
    volume_db = resolve_volume_db(opts.get("volumeBoost", "none"))
    afx = audio_filters(bool(opts.get("normalizeAudio", True)), volume_db)
    if info.get("has_audio"):
        aargs = ["-c:a", aud_enc, "-b:a", f"{audio_kbps}k"]
        if afx and not multi:
            aargs += ["-af", ",".join(afx)]
        if opts.get("normalizeAudio", True):
            notes.append("audio: applying EBU R128 loudness normalization "
                         "(-16 LUFS)")
        if abs(volume_db) > 0.01:
            notes.append(f"audio: volume boost {volume_db:+.1f} dB")
    else:
        aargs = ["-an"]

    movflags = ["-movflags", "+faststart"] if container == "mp4" else []
    if movflags:
        notes.append("container: MP4 faststart enabled")

    is_hdr = bool(info.get("is_hdr"))

    def do_encode(enc: str) -> list[str]:
        """Run FULL_ENCODE with encoder `enc`, writing output_path. Returns a
        list of notes. Every encoder-dependent choice (10-bit, hwaccel, filter
        graph, rate-control, two-pass) is rebuilt here so the CPU-fallback
        re-encode below is fully correct."""
        en: list[str] = []

        # effective 10-bit: requested AND hevc/av1 AND this encoder supports it.
        # Forcing yuv420p10le on an encoder that can't (e.g. av1_amf) yields a
        # pixel-format error or a corrupt stream.
        ten = (bool(opts.get("tenBit")) and codec in ("hevc", "av1")
               and enc in TEN_BIT_ENCODERS)
        if opts.get("tenBit") and codec in ("hevc", "av1") and not ten:
            en.append(f"10-bit requested but encoder {enc} does not support "
                      f"it - using 8-bit")

        # hwaccel decode - skipped in two cases:
        #  1. encoder is av1_amf: confirmed ffmpeg/AMF bug - hwaccel-decoded
        #     frames feeding the AMF AV1 encoder produce a corrupt bitstream.
        #     av1_amf is perfectly fine with software decode. hevc_amf /
        #     h264_amf / CPU encoders are unaffected and keep hwaccel.
        #  2. tone-mapping HDR->SDR: some hwaccel paths choke on the
        #     linear-light color conversion.
        use_hw = bool(opts.get("hwaccelDecode", True))
        if use_hw and enc == "av1_amf":
            use_hw = False
            en.append("hwaccel skipped: av1_amf + hwaccel decode is broken "
                      "(software decode used instead)")
        elif use_hw and is_hdr and not ten and opts.get("hdrToneMap", True):
            use_hw = False
            en.append("hwaccel skipped: HDR->SDR tone mapping active")
        elif use_hw:
            en.append("hwaccel: auto")

        vfx, vfx_notes = build_filters(opts, info, source_height,
                                       target_height, video_kbps, codec, ten)
        en += vfx_notes

        speed = str(opts.get("encodeSpeed", "quality"))
        if speed not in SPEED_LEVELS:
            speed = "quality"
        en.append(f"encode speed: {speed} (preset {speed_preset(enc, speed)})")
        if target_mb > 0:
            va = bitrate_mode_args(enc, speed) + ["-b:v", f"{int(video_kbps)}k"]
            # SVT-AV1 rejects -maxrate outside CRF mode ("Max Bitrate only
            # supported with CRF mode"); it does its own VBR rate control from
            # -b:v alone. Every other encoder takes the peak-rate cap.
            if enc != "libsvtav1":
                va += ["-maxrate", f"{int(video_kbps * 1.45)}k",
                       "-bufsize", f"{int(video_kbps * 2)}k"]
        else:
            va = quality_args(enc, speed)
            # CRF/CQP mode: cap peak bitrate so a complex clip can't balloon.
            mult = min(5.0, max(1.0, float(
                opts.get("crfMaxrateMultiplier", 2.0) or 2.0)))
            mr = int(8000 * (target_height / 1080) ** 2 * mult)
            va += ["-maxrate", f"{mr}k", "-bufsize", f"{mr * 2}k"]
            en.append(f"CRF mode: maxrate ceiling = {mr} kbps "
                      f"(multiplier={mult})")
        va += structural_args(enc, codec, opts, fps, duration, ten)
        if duration >= 3.0:
            en.append("keyframe: 2s GOP, 1s min interval (encoder scene-cut)")

        # real 2-pass only for CPU encoders; <5s clips stay single-pass.
        tp = (bool(opts.get("twoPass", True)) and target_mb > 0
              and duration >= 5.0
              and enc in ("libx264", "libx265", "libsvtav1"))

        # Filter args differ per mode: single-segment uses a plain -vf chain;
        # multi-segment uses a -filter_complex trim/concat graph. pass 1 only
        # analyzes video so it always uses the video-only graph.
        vf_arg = ["-vf", ",".join(vfx)] if vfx else []
        if multi:
            vgraph, vmap = build_video_graph(segments, vfx)
            pass1_filter = ["-filter_complex", vgraph] + vmap
            if info.get("has_audio"):
                avgraph, avvmap, avamap = build_av_graph(segments, vfx, afx)
                final_filter = ["-filter_complex", avgraph] + avvmap
                final_amap = avamap
            else:
                final_filter, final_amap = pass1_filter, []
        else:
            pass1_filter, final_filter, final_amap = vf_arg, vf_arg, []

        if tp:
            passlog = str(temp_dir / "ffpass")
            set_stage(job_id, "pass1")
            cmd1 = (input_args(ffmpeg, input_path, start, duration, use_hw, multi)
                    + pass1_filter + ["-c:v", enc] + va
                    + ["-pass", "1", "-passlogfile", passlog,
                       "-an", "-f", "null", os.devnull])
            log.info("[%s] ffmpeg pass1: %s", job_id, " ".join(cmd1))
            run_ffmpeg_with_progress(job_id, cmd1, duration,
                                     lambda f: on_progress(f * 0.5))
            if job_cancelled(job_id):
                raise JobCancelled()
            set_stage(job_id, "pass2")
            cmd2 = (input_args(ffmpeg, input_path, start, duration, use_hw, multi)
                    + final_filter + ["-c:v", enc] + va
                    + ["-pass", "2", "-passlogfile", passlog]
                    + final_amap + aargs + movflags + [str(output_path)])
            log.info("[%s] ffmpeg pass2: %s", job_id, " ".join(cmd2))
            run_ffmpeg_with_progress(job_id, cmd2, duration,
                                     lambda f: on_progress(0.5 + f * 0.5))
            en.append("two-pass encode")
        else:
            set_stage(job_id, "single_pass")
            cmd = (input_args(ffmpeg, input_path, start, duration, use_hw, multi)
                   + final_filter + ["-c:v", enc] + va
                   + final_amap + aargs + movflags + [str(output_path)])
            log.info("[%s] ffmpeg single-pass: %s", job_id, " ".join(cmd))
            run_ffmpeg_with_progress(job_id, cmd, duration, on_progress)
            if target_mb > 0:
                en.append("single-pass encode")
        return en

    # Run the encode. A hardware encoder can fail outright here - some AMF
    # builds list and pass the synthetic probe but reject the real encode args
    # ("encoder->Init() failed"). Treat an outright failure the same way as a
    # corrupt-output failure below: disable that encoder and re-encode on CPU.
    try:
        notes += do_encode(encoder)
    except JobCancelled:
        raise
    except RuntimeError as e:
        if encoder in CPU_ENCODERS:
            raise
        cpu_enc = CPU_FALLBACK.get(encoder, "libx264")
        log.warning("[%s] %s encode failed - re-encoding on %s (%s)",
                    job_id, encoder, cpu_enc, str(e)[:200])
        notes.append(f"WARNING: {encoder} encode failed - re-encoded on CPU "
                     f"({cpu_enc})")
        mark_encoder_broken(encoder)
        encoder = cpu_enc
        on_progress(0.0)
        notes += do_encode(encoder)

    # Verify a hardware-encoder result. Some GPU encoders (notably av1_amf on
    # certain AMD GPUs) write a bitstream that passes a synthetic probe yet is
    # corrupt garbage for real content. Decode-verify the actual output and, if
    # broken, disable that encoder and re-encode once on the CPU fallback.
    if encoder not in CPU_ENCODERS and output_path.exists():
        set_stage(job_id, "verifying")
        if not output_decodes_ok(output_path):
            cpu_enc = CPU_FALLBACK.get(encoder, "libx264")
            log.warning("[%s] %s output failed decode-verify - re-encoding "
                        "on %s", job_id, encoder, cpu_enc)
            notes.append(f"WARNING: {encoder} produced a corrupt stream - "
                         f"re-encoded on CPU ({cpu_enc})")
            mark_encoder_broken(encoder)
            encoder = cpu_enc
            on_progress(0.0)
            notes += do_encode(encoder)

    on_progress(1.0)

    # transparency: predicted vs actual
    if output_path.exists() and target_mb > 0:
        actual_mb = output_path.stat().st_size / MB
        notes.append(f"output {actual_mb:.2f}MB (target {target_mb:.0f}MB)")
    notes.append(f"audio: {aud_label} @ {audio_kbps}kbps")
    summary = f"full encode - {reason}; " + "; ".join(notes)
    log.info("[%s] %s", job_id, summary)
    return summary


def run_job(job_id: str, items: list[dict], opts: dict) -> None:
    """Job worker thread. Waits on the concurrency semaphore (status 'queued'
    until a slot frees), then processes every item."""
    n = len(items)
    set_stage(job_id, "queued")
    JOB_SEMAPHORE.acquire()
    temp_dir: Path | None = None
    try:
        if job_cancelled(job_id):
            update_job(job_id, status="cancelled", stage="cancelled")
            return
        update_job(job_id, status="running")
        temp_dir = Path(tempfile.mkdtemp(prefix=f"clip_{job_id}_"))

        for i, item in enumerate(items):
            if job_cancelled(job_id):
                update_job(job_id, status="cancelled", stage="cancelled")
                return
            try:
                input_path = UPLOAD_DIR / item["filename"]
                if not input_path.exists():
                    raise FileNotFoundError(item["filename"])
                info = probe_video(input_path)
                stem = Path(item["filename"]).stem
                # strip leading timestamp_ from upload name for cleaner output
                stem = re.sub(r"^\d{10,}_", "", stem)
                container = opts.get("container", "mp4")
                ts = int(time.time() * 1000)
                output_path = OUTPUT_DIR / f"{stem}_{i+1}_{ts}.{container}"

                merged = {**opts, **item.get("opts", {})}

                segments = normalize_segments(item)
                total_kept = sum(e - s for s, e in segments)
                if total_kept < 1.0:
                    log.warning("[%s] clip %d keeps %.2fs (<1s) - encoder "
                                "behaviour is unreliable at this scale",
                                job_id, i + 1, total_kept)

                def on_progress(frac, _i=i):
                    with JOBS_LOCK:
                        if job_id in JOBS:
                            JOBS[job_id]["progress"] = (_i + frac) / n

                summary = process_clip(job_id, input_path, output_path,
                                        segments, merged, info, temp_dir,
                                        on_progress)
                job_append_output(job_id, output_path.name)
                with JOBS_LOCK:
                    if job_id in JOBS:
                        JOBS[job_id].setdefault("summaries", []).append(summary)
            except JobCancelled:
                update_job(job_id, status="cancelled", stage="cancelled")
                return
            except Exception as e:
                log.exception("job %s item %d failed", job_id, i)
                job_append_error(job_id, f"{item.get('filename','?')}: {e}")
            with JOBS_LOCK:
                if job_id in JOBS:
                    JOBS[job_id]["done"] = i + 1
                    JOBS[job_id]["progress"] = (i + 1) / n
        update_job(job_id, status="done", stage="done")
    finally:
        JOB_SEMAPHORE.release()
        unpersist_pending(job_id)
        if temp_dir and temp_dir.exists():
            shutil.rmtree(temp_dir, ignore_errors=True)


# ---------- pre-flight validation ----------
def check_input_integrity(path: Path) -> str | None:
    """Reject files that are incomplete/corrupt before queuing. Catches the case
    where a user clips a recording OBS/ShadowPlay is still writing."""
    try:
        age = time.time() - path.stat().st_mtime
        if age < 5:
            log.warning("%s modified %.1fs ago - may still be writing",
                        path.name, age)
    except OSError:
        pass
    ffmpeg, _ = get_bins()
    try:
        r = subprocess.run([ffmpeg, "-hide_banner", "-v", "error",
                            "-t", "1", "-i", str(path), "-f", "null", "-"],
                           capture_output=True, text=True, timeout=30)
    except Exception as e:
        return f"input test decode failed: {e}"
    if r.returncode != 0:
        return ("input file appears incomplete or corrupted "
                "(ffmpeg test decode failed)")
    return None


def validate_items(items: list[dict], opts: dict) -> str | None:
    """Return an error string if any item is invalid, else None."""
    target_mb = float(opts.get("targetSizeMb", 0) or 0)
    total_input = 0

    # codec/container compatibility - WebM only carries AV1 video (of the codecs
    # this app offers), so reject the impossible combo with a clear message
    # rather than letting ffmpeg fail with a cryptic muxer error.
    codec = str(opts.get("codec", "h264"))
    container = str(opts.get("container", "mp4"))
    if container == "webm" and codec != "av1":
        return (f"WebM container supports only AV1 video, not {codec.upper()}. "
                f"Switch the codec to AV1, or pick the MP4 or MKV container.")

    integrity_checked: set[str] = set()
    for idx, item in enumerate(items):
        fname = item.get("filename", "?")
        input_path = UPLOAD_DIR / str(fname)
        if not input_path.exists():
            return f"{fname}: upload not found"
        try:
            info = probe_video(input_path)
        except Exception as e:
            return f"{fname}: probe failed: {e}"
        total_input += info["size"]

        if str(fname) not in integrity_checked:
            integrity_checked.add(str(fname))
            integrity_err = check_input_integrity(input_path)
            if integrity_err:
                return f"{fname}: {integrity_err}"

        duration = info["duration"]
        try:
            segments = normalize_segments(item)
        except (KeyError, TypeError, ValueError) as e:
            return f"{fname}: missing/invalid segments ({e})"
        if not segments:
            return f"{fname}: no keep-segments"
        # validate every segment: 0 <= start < end <= duration (100 ms tol.)
        for si, (start, end) in enumerate(segments):
            if start < -0.1 or end > duration + 0.1 or start >= end:
                return (f"{fname}: invalid segment {si+1} range "
                        f"{start:.2f}-{end:.2f}s (source is {duration:.2f}s)")
        # cut_dur is the SUM of kept segments - dropping the boring middle buys
        # bitrate, so the size-floor check uses the total kept duration.
        cut_dur = sum(e - s for s, e in segments)

        # only enforce the size floor when this item would actually re-encode
        mode, _, _ = decide_mode(input_path, info, opts, segments)
        if mode == "FULL_ENCODE" and target_mb > 0:
            total = (target_mb * 8 * 1024 * 0.95) / cut_dur
            audio = pick_audio_bitrate(total, opts.get("audioBitrateMode", "auto"))
            video = total - audio
            if video < VIDEO_FLOOR_KBPS:
                max_dur = (target_mb * 8 * 1024 * 0.95) / (VIDEO_FLOOR_KBPS + audio)
                return (f"{fname}: cannot fit {cut_dur:.0f}s into {target_mb:.0f}MB "
                        f"at acceptable quality (would need <{VIDEO_FLOOR_KBPS}kbps "
                        f"video). Reduce duration to <={max_dur:.0f}s, raise the "
                        f"target, or pick a lower-resolution output.")

    # free disk space >= 2x total input size
    try:
        free = shutil.disk_usage(OUTPUT_DIR).free
        if free < total_input * 2:
            return (f"Not enough free disk space: need ~{total_input*2/MB:.0f}MB, "
                    f"have {free/MB:.0f}MB free")
    except Exception:
        pass
    return None


# ---------- routes ----------
@app.route("/")
def index():
    return render_template("index.html")


@app.post("/api/upload")
def api_upload():
    if "file" not in request.files:
        return jsonify({"error": "no file"}), 400
    f = request.files["file"]
    if not f.filename:
        return jsonify({"error": "empty filename"}), 400
    safe = f"{int(time.time() * 1000)}_{secure_filename(f.filename)}"
    path = UPLOAD_DIR / safe
    f.save(path)
    try:
        info = probe_video(path)
    except Exception as e:
        path.unlink(missing_ok=True)
        return jsonify({"error": f"probe failed: {e}"}), 400
    return jsonify({"filename": safe, "info": info})


@app.get("/api/preview/<path:filename>")
def api_preview(filename):
    return send_from_directory(UPLOAD_DIR, filename, conditional=True)


@app.get("/api/output/<path:filename>")
def api_output(filename):
    return send_from_directory(OUTPUT_DIR, filename,
                               conditional=True, as_attachment=False)


@app.get("/api/outputs")
def api_outputs():
    files = []
    for p in sorted(OUTPUT_DIR.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True):
        if p.is_file():
            files.append({
                "filename": p.name,
                "size": p.stat().st_size,
                "mtime": p.stat().st_mtime,
            })
    return jsonify(files)


@app.delete("/api/output/<path:filename>")
def api_delete_output(filename):
    p = OUTPUT_DIR / filename
    if p.exists() and p.is_file():
        p.unlink()
    return jsonify({"ok": True})


@app.delete("/api/outputs")
def api_delete_all_outputs():
    deleted = 0
    errors = []
    for p in OUTPUT_DIR.iterdir():
        if not p.is_file():
            continue
        try:
            p.unlink()
            deleted += 1
        except Exception as e:
            errors.append(f"{p.name}: {e}")
    return jsonify({"ok": True, "deleted": deleted, "errors": errors})


@app.delete("/api/upload/<path:filename>")
def api_delete_upload(filename):
    p = UPLOAD_DIR / filename
    if p.exists() and p.is_file():
        p.unlink()
    return jsonify({"ok": True})


@app.post("/api/process")
def api_process():
    data = request.get_json(force=True)
    items = data.get("items") or []
    if not items:
        return jsonify({"error": "no items"}), 400

    cfg = load_config()
    # discordTier overrides targetSizeMb (Discord's hard upload limit wins)
    target_mb = float(cfg.get("targetSizeMb", 0) or 0)
    tier = cfg.get("discordTier", "free")
    if tier in DISCORD_TIERS:
        target_mb = DISCORD_TIERS[tier]

    opts = {
        "codec": cfg.get("codec", "h264"),
        "hardware": cfg.get("hardware", "cpu"),
        "container": cfg.get("container", "mp4"),
        "targetSizeMb": target_mb,
        "vibrance": float(cfg.get("vibrance", 1.0)),
        "stretch": bool(cfg.get("stretch", False)),
        "frameAccurateCut": bool(cfg.get("frameAccurateCut", False)),
        "outputHeight": cfg.get("outputHeight", "auto"),
        "encoderQualityMode": cfg.get("encoderQualityMode", "auto"),
        "encodeSpeed": cfg.get("encodeSpeed", "quality"),
        "audioBitrateMode": cfg.get("audioBitrateMode", "auto"),
        "audioCodec": cfg.get("audioCodec", "aac"),
        "twoPass": bool(cfg.get("twoPass", True)),
        "tenBit": bool(cfg.get("tenBit", False)),
        "lowBitrateDenoise": bool(cfg.get("lowBitrateDenoise", True)),
        "hwaccelDecode": bool(cfg.get("hwaccelDecode", True)),
        "fpsMode": cfg.get("fpsMode", "auto"),
        "fpsCustom": cfg.get("fpsCustom", 48),
        "hdrToneMap": bool(cfg.get("hdrToneMap", True)),
        "normalizeAudio": bool(cfg.get("normalizeAudio", True)),
        "denoiseStrength": cfg.get("denoiseStrength", "light"),
        "crfMaxrateMultiplier": float(cfg.get("crfMaxrateMultiplier", 2.0) or 2.0),
        "volumeBoost": cfg.get("volumeBoost", "none"),
    }
    if "opts" in data and isinstance(data["opts"], dict):
        opts.update(data["opts"])

    # duplicate detection - a double-submit of the identical job within 5s
    # returns the original job id instead of starting a second encode.
    sig = hashlib.sha1(
        json.dumps({"items": items, "opts": opts}, sort_keys=True,
                   default=str).encode()).hexdigest()
    now = time.time()
    with JOBS_LOCK:
        for jid, j in JOBS.items():
            if (j.get("signature") == sig
                    and now - j.get("created", 0) < 5
                    and j.get("status") in ("queued", "running")):
                log.info("duplicate submit - returning existing job %s", jid)
                return jsonify({"job_id": jid})

    # pre-flight validation - reject before queuing
    err = validate_items(items, opts)
    if err:
        return jsonify({"error": err}), 400

    details = {
        "codec": opts["codec"],
        "container": opts["container"],
        "targetMb": target_mb,
        "hardware": opts["hardware"],
        "encoderQualityMode": opts["encoderQualityMode"],
        "twoPass": opts["twoPass"],
        "outputHeight": opts["outputHeight"],
        "audioCodec": opts["audioCodec"],
        "tier": tier,
        # total keep-segments across all items (validation already passed, so
        # normalize_segments cannot raise here)
        "segments": sum(len(normalize_segments(it)) for it in items),
    }
    job_id = uuid.uuid4().hex[:10]
    _register_job(job_id, items, opts, details, sig)
    evict_old_jobs()
    persist_pending(job_id, items, opts, details)
    threading.Thread(target=run_job, args=(job_id, items, opts), daemon=True).start()
    return jsonify({"job_id": job_id})


def _job_view(j: dict) -> dict:
    """JSON-safe copy of a job dict (drop the Popen handle and verbose log)."""
    return {k: v for k, v in j.items() if k not in ("proc", "log")}


@app.get("/api/job/<job_id>")
def api_job(job_id):
    with JOBS_LOCK:
        j = JOBS.get(job_id)
        if not j:
            return jsonify({"error": "unknown job"}), 404
        return jsonify(_job_view(j))


@app.post("/api/job/<job_id>/cancel")
def api_job_cancel(job_id):
    with JOBS_LOCK:
        j = JOBS.get(job_id)
        if not j:
            return jsonify({"error": "unknown job"}), 404
        j["cancel_requested"] = True
        proc = j.get("proc")
    if proc and proc.poll() is None:
        proc.terminate()

        def _kill_later(p):
            time.sleep(3)
            if p.poll() is None:
                p.kill()
        threading.Thread(target=_kill_later, args=(proc,), daemon=True).start()
    update_job(job_id, status="cancelled", stage="cancelled")
    return jsonify({"ok": True})


@app.get("/api/job/<job_id>/log")
def api_job_log(job_id):
    with JOBS_LOCK:
        j = JOBS.get(job_id)
        if not j:
            return jsonify({"error": "unknown job"}), 404
        lines = list(j.get("log", []))[-200:]
    return jsonify({"job_id": job_id, "log": "".join(lines)})


def _thumbnail_for(src: Path) -> Path | None:
    """Extract (and cache) a single JPG frame from a video. Cache key includes
    mtime + size so a regenerated output gets a fresh thumbnail."""
    st = src.stat()
    out = THUMB_DIR / f"{src.stem}_{int(st.st_mtime)}_{st.st_size}.jpg"
    if out.exists():
        return out
    ffmpeg, _ = get_bins()
    for seek in ("1", "0"):  # 1s in; fall back to frame 0 for very short clips
        cmd = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
               "-ss", seek, "-i", str(src), "-frames:v", "1",
               "-vf", "scale=320:-2", str(out)]
        try:
            r = subprocess.run(cmd, capture_output=True, timeout=30)
        except Exception:
            return None
        if r.returncode == 0 and out.exists():
            return out
    return out if out.exists() else None


@app.get("/api/job/<job_id>/thumbnail")
def api_job_thumbnail(job_id):
    with JOBS_LOCK:
        j = JOBS.get(job_id)
        if not j:
            return jsonify({"error": "unknown job"}), 404
        outputs = list(j.get("outputs", []))
    if not outputs:
        return jsonify({"error": "no output yet"}), 404
    src = OUTPUT_DIR / outputs[0]
    if not src.exists():
        return jsonify({"error": "output missing"}), 404
    thumb = _thumbnail_for(src)
    if not thumb or not thumb.exists():
        return jsonify({"error": "thumbnail generation failed"}), 500
    return send_file(thumb, mimetype="image/jpeg")


def _filmstrip_for(src: Path) -> Path | None:
    """Build (and cache) a horizontal sprite of ~15 frames sampled evenly across
    the clip. Used as the timeline background in the editor. Cache key includes
    mtime + size like _thumbnail_for so a replaced upload regenerates."""
    st = src.stat()
    out = THUMB_DIR / f"strip_{src.stem}_{int(st.st_mtime)}_{st.st_size}.jpg"
    if out.exists():
        return out
    try:
        info = probe_video(src)
        duration = max(0.1, info["duration"])
    except Exception:
        return None
    n = 15
    ffmpeg, _ = get_bins()
    cmd = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-i", str(src),
           "-vf", f"fps={n}/{duration:.3f},scale=160:-1,tile={n}x1",
           "-frames:v", "1", str(out)]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=90)
    except Exception:
        return None
    return out if (r.returncode == 0 and out.exists()) else None


@app.get("/api/filmstrip/<path:filename>")
def api_filmstrip(filename):
    """Filmstrip sprite JPG for an uploaded file - the timeline editor background."""
    src = UPLOAD_DIR / filename
    if not src.exists():
        return jsonify({"error": "upload not found"}), 404
    strip = _filmstrip_for(src)
    if not strip or not strip.exists():
        return jsonify({"error": "filmstrip generation failed"}), 500
    return send_file(strip, mimetype="image/jpeg")


@app.get("/api/upload-thumb/<path:filename>")
def api_upload_thumb(filename):
    """Single-frame JPG thumbnail of an uploaded file - for the pending strip."""
    src = UPLOAD_DIR / filename
    if not src.exists():
        return jsonify({"error": "upload not found"}), 404
    thumb = _thumbnail_for(src)
    if not thumb or not thumb.exists():
        return jsonify({"error": "thumbnail generation failed"}), 500
    return send_file(thumb, mimetype="image/jpeg")


@app.get("/api/capabilities")
def api_capabilities():
    """Encoders this ffmpeg build supports - the UI grays out the rest."""
    return jsonify(AVAILABLE_ENCODERS)


# ---------- discord ----------
def _discord_error_message(resp) -> str:
    """Pull a human-readable message out of Discord's JSON error response."""
    try:
        body = resp.json()
    except Exception:
        return resp.text or f"HTTP {resp.status_code}"
    if isinstance(body, dict):
        msg = body.get("message")
        if msg:
            code = body.get("code")
            return f"{msg}" + (f" (code {code})" if code else "")
    return resp.text or f"HTTP {resp.status_code}"


def _is_size_reject(resp) -> bool:
    txt = (resp.text or "").lower()
    return resp.status_code in (413,) or "too large" in txt or "max size" in txt


def _shrink_output(path: Path, target_mb: float) -> Path:
    """Re-encode an existing output file to fit a tighter size budget. Used by
    the optional Discord auto-retry path.

    The encoder MUST match the container: WebM only carries AV1 + Opus, so
    re-encoding a .webm with H.264/AAC produces a muxer error. MP4/MKV take the
    universally-compatible H.264 + AAC."""
    ffmpeg, _ = get_bins()
    info = probe_video(path)
    duration = max(0.1, info["duration"])
    total_kbps = (target_mb * 8 * 1024 * 0.95) / duration
    audio_kbps = pick_audio_bitrate(total_kbps, "auto")
    video_kbps = max(VIDEO_FLOOR_KBPS, total_kbps - audio_kbps)
    out = path.with_name(path.stem + "_retry" + path.suffix)

    container = path.suffix.lower().lstrip(".")
    if container == "webm":
        # WebM cannot carry H.264/AAC. SVT-AV1 rejects -maxrate outside CRF
        # mode, so rate-control is the -b:v target alone.
        vargs = ["-c:v", "libsvtav1", "-preset", "8", "-b:v", f"{int(video_kbps)}k"]
        aargs = ["-c:a", "libopus", "-b:a", f"{audio_kbps}k"]
    else:
        vargs = ["-c:v", "libx264", "-preset", "slow", "-b:v", f"{int(video_kbps)}k",
                 "-maxrate", f"{int(video_kbps*1.45)}k",
                 "-bufsize", f"{int(video_kbps*2)}k"]
        aargs = ["-c:a", "aac", "-b:a", f"{audio_kbps}k"]
    cmd = ([ffmpeg, "-y", "-hide_banner", "-loglevel", "warning", "-i", str(path)]
           + vargs + aargs + [str(out)])
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"retry re-encode failed: {r.stderr[-400:]}")
    return out


def _guess_mime(filename: str) -> str:
    """Content-type for a Discord attachment, by extension - so a .webm clip
    isn't mislabelled as video/mp4."""
    return {".webm": "video/webm", ".mkv": "video/x-matroska",
            ".mp4": "video/mp4"}.get(Path(filename).suffix.lower(), "video/mp4")


def _discord_upload(path: Path, filename: str, content: str,
                    mode: str, cfg: dict, data: dict):
    """Perform a single Discord upload. Returns the requests.Response."""
    mime = _guess_mime(filename)
    with open(path, "rb") as fp:
        if mode == "webhook":
            url = data.get("webhook") or cfg.get("discordWebhook", "")
            if not url:
                raise ValueError("no webhook configured")
            files = {"files[0]": (filename, fp, mime)}
            payload = {"content": content} if content else {}
            form = {"payload_json": json.dumps(payload)} if payload else {}
            return requests.post(url, data=form, files=files, timeout=300)
        token = cfg.get("discordToken", "")
        if not token:
            raise ValueError("no token configured")
        channel_id = data.get("channel_id") or cfg.get("discordChannelId", "")
        if not channel_id:
            raise ValueError("no channel id")
        prefix = cfg.get("discordTokenPrefix", "")
        auth = f"{prefix} {token}".strip() if prefix else token
        url = f"https://discord.com/api/v10/channels/{channel_id}/messages"
        files = {"files[0]": (filename, fp, mime)}
        form = {"payload_json": json.dumps({"content": content})}
        return requests.post(url, headers={"Authorization": auth},
                             data=form, files=files, timeout=300)


@app.post("/api/discord")
def api_discord():
    data = request.get_json(force=True)
    cfg = load_config()
    filename = data.get("filename")
    content = data.get("content", "")

    if not filename:
        return jsonify({"error": "filename required"}), 400
    path = OUTPUT_DIR / filename
    if not path.exists():
        return jsonify({"error": "file not found"}), 404

    mode = data.get("mode") or cfg.get("discordMode", "webhook")
    try:
        r = _discord_upload(path, filename, content, mode, cfg, data)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    # optional one-shot auto-retry on a size rejection
    retry_file: Path | None = None
    if (r.status_code >= 300 and _is_size_reject(r)
            and cfg.get("autoRetryOnDiscordReject", False)):
        try:
            cur_mb = path.stat().st_size / MB
            tighter = cur_mb * 0.8
            log.info("discord size reject - auto-retry at %.1fMB target", tighter)
            retry_file = _shrink_output(path, tighter)
            r = _discord_upload(retry_file, retry_file.name, content, mode, cfg, data)
        except Exception as e:
            log.warning("discord auto-retry failed: %s", e)
        finally:
            if retry_file and retry_file.exists():
                retry_file.unlink(missing_ok=True)

    if r.status_code >= 300:
        return jsonify({"error": _discord_error_message(r),
                        "status": r.status_code}), 502
    try:
        body = r.json()
    except Exception:
        body = {"raw": r.text}
    return jsonify({"ok": True, "response": body})


# ---------- config routes ----------
@app.get("/api/config")
def api_get_config():
    cfg = load_config()
    safe = dict(cfg)
    if safe.get("discordToken"):
        safe["discordToken"] = "••••••••"
    if safe.get("discordWebhook"):
        # redact token portion of webhook
        safe["discordWebhook"] = re.sub(r"(/webhooks/\d+/)[^/?]+",
                                        r"\1••••••••", safe["discordWebhook"])
    return jsonify(safe)


@app.post("/api/config")
def api_save_config():
    new = request.get_json(force=True)
    cfg = load_config()
    # don't overwrite secrets if the UI sent the masked placeholder
    if new.get("discordToken") in ("••••••••", "", None):
        new["discordToken"] = cfg.get("discordToken", "")
    if new.get("discordWebhook") and "••••••••" in new.get("discordWebhook", ""):
        new["discordWebhook"] = cfg.get("discordWebhook", "")
    cfg.update({k: v for k, v in new.items() if k in DEFAULT_CONFIG})
    save_config(cfg)
    return jsonify({"ok": True})


@app.get("/api/health")
def api_health():
    ffmpeg, ffprobe = get_bins()
    ok = True
    try:
        subprocess.run([ffmpeg, "-version"], capture_output=True, check=True, timeout=5)
    except Exception:
        ok = False
    return jsonify({"ffmpeg": ok, "ffmpeg_path": ffmpeg})


def cleanup_old_files() -> None:
    """Delete files in uploads/ and output/ older than cleanupDays. Conservative:
    only plain files are removed, never directories. cleanupDays=0 disables it."""
    days = int(load_config().get("cleanupDays", 14) or 0)
    if days <= 0:
        return
    cutoff = time.time() - days * 86400
    removed = 0
    for d in (UPLOAD_DIR, OUTPUT_DIR):
        try:
            entries = list(d.iterdir())
        except OSError:
            continue
        for p in entries:
            try:
                if p.is_file() and p.stat().st_mtime < cutoff:
                    p.unlink()
                    removed += 1
            except OSError:
                pass
    if removed:
        log.info("cleanup: removed %d file(s) older than %d days", removed, days)


def startup() -> None:
    """One-time startup work: migrate config, clean stale files, probe encoder
    support, and re-queue any jobs left unfinished by a previous run."""
    migrate_config_file()
    cleanup_old_files()
    AVAILABLE_ENCODERS.update(probe_encoders())
    restore_jobs()


startup()


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=False, threaded=True)
