"""Clip Creator — minimal Flask backend.

Single-pass FFmpeg pipeline: cut -> stretch/vibrance -> encode (HW or SW) ->
size-targeted bitrate. Background jobs with polling. Discord upload via user
token, Bearer, Bot, or webhook URL.
"""
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

import requests
from flask import Flask, jsonify, render_template, request, send_from_directory
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

for d in (UPLOAD_DIR, OUTPUT_DIR):
    d.mkdir(exist_ok=True)

DEFAULT_CONFIG = {
    "ffmpegPath": "ffmpeg/bin",
    "hardware": "amd",            # cpu | nvidia | amd | intel
    "codec": "hevc",              # h264 | hevc | av1
    "container": "mp4",           # mp4 | mkv | webm
    "targetSizeMb": 25,           # 0 = no size cap (CRF/CQ quality mode)
    "vibrance": 1.0,
    "stretch": False,
    "discordMode": "webhook",     # webhook | token
    "discordToken": "",
    "discordTokenPrefix": "",     # "" | "Bearer" | "Bot"
    "discordChannelId": "1115263445844119562",
    "discordWebhook": "",
}

app = Flask(__name__,
            static_folder=str(BUNDLE_DIR / "static"),
            template_folder=str(BUNDLE_DIR / "templates"))
app.config["MAX_CONTENT_LENGTH"] = 4 * 1024 * 1024 * 1024  # 4 GB upload cap


# ---------- config ----------
def load_config() -> dict:
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                return {**DEFAULT_CONFIG, **json.load(f)}
        except Exception as e:
            log.warning("config read failed: %s", e)
    return DEFAULT_CONFIG.copy()


def save_config(cfg: dict) -> None:
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)


# ---------- ffmpeg helpers ----------
def get_bins() -> tuple[str, str]:
    p = load_config().get("ffmpegPath", "")
    if p and not os.path.isabs(p):
        p = str(BASE_DIR / p)
    if p:
        return os.path.join(p, "ffmpeg.exe"), os.path.join(p, "ffprobe.exe")
    return "ffmpeg", "ffprobe"


def probe_video(path: Path) -> dict:
    _, ffprobe = get_bins()
    cmd = [ffprobe, "-v", "quiet", "-print_format", "json",
           "-show_format", "-show_streams", str(path)]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"ffprobe failed: {r.stderr}")
    data = json.loads(r.stdout)
    vstream = next((s for s in data["streams"] if s["codec_type"] == "video"), None)
    if not vstream:
        raise RuntimeError("no video stream")
    return {
        "duration": float(data["format"].get("duration", 0)),
        "width": int(vstream.get("width", 0)),
        "height": int(vstream.get("height", 0)),
        "size": int(data["format"].get("size", 0)),
        "codec": vstream.get("codec_name", ""),
    }


def cropdetect(path: Path) -> tuple[int, int, int, int] | None:
    ffmpeg, _ = get_bins()
    cmd = [ffmpeg, "-i", str(path),
           "-vf", "cropdetect=limit=24:round=2",
           "-frames:v", "120", "-f", "null", "-"]
    r = subprocess.run(cmd, capture_output=True, text=True)
    crop = None
    for line in r.stderr.split("\n"):
        if "crop=" in line:
            crop = line.split("crop=")[-1].split(" ")[0]
    if not crop:
        return None
    try:
        w, h, x, y = map(int, crop.split(":"))
        return w, h, x, y
    except ValueError:
        return None


def encoder_for(codec: str, hardware: str) -> str:
    table = {
        "nvidia": {"h264": "h264_nvenc", "hevc": "hevc_nvenc", "av1": "av1_nvenc"},
        "amd":    {"h264": "h264_amf",   "hevc": "hevc_amf",   "av1": "av1_amf"},
        "intel":  {"h264": "h264_qsv",   "hevc": "hevc_qsv",   "av1": "av1_qsv"},
        "cpu":    {"h264": "libx264",    "hevc": "libx265",    "av1": "libsvtav1"},
    }
    return table.get(hardware, table["cpu"]).get(codec, "libx264")


def quality_args(encoder: str) -> list[str]:
    """CQ/CRF args when no target size is set."""
    if "amf" in encoder:
        return ["-rc", "cqp", "-qp_i", "22", "-qp_p", "24", "-quality", "quality"]
    if "nvenc" in encoder:
        return ["-preset", "p5", "-rc", "vbr", "-cq", "23"]
    if "qsv" in encoder:
        return ["-global_quality", "23"]
    if encoder == "libsvtav1":
        return ["-preset", "8", "-crf", "32"]
    return ["-preset", "medium", "-crf", "23"]


# ---------- job system ----------
JOBS: dict[str, dict] = {}
JOBS_LOCK = threading.Lock()
DURATION_RE = re.compile(r"time=(\d+):(\d+):(\d+\.\d+)")


def update_job(job_id: str, **kw):
    with JOBS_LOCK:
        if job_id in JOBS:
            JOBS[job_id].update(kw)


def job_append_output(job_id: str, name: str):
    with JOBS_LOCK:
        if job_id in JOBS:
            JOBS[job_id]["outputs"].append(name)


def job_append_error(job_id: str, msg: str):
    with JOBS_LOCK:
        if job_id in JOBS:
            JOBS[job_id]["errors"].append(msg)


def run_ffmpeg_with_progress(cmd: list[str], duration: float, on_progress) -> None:
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                            text=True, encoding="utf-8", errors="replace")
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
    if proc.returncode != 0:
        raise RuntimeError("".join(last_err)[-1000:])


def process_clip(input_file: Path, output_file: Path,
                 start: float, end: float, opts: dict, on_progress) -> None:
    ffmpeg, _ = get_bins()
    duration = max(0.1, end - start)

    vf: list[str] = []
    if opts.get("stretch"):
        crop = cropdetect(input_file)
        if crop:
            w, h, x, y = crop
            vf.append(f"crop={w}:{h}:{x}:{y}")
        vf.append("scale=1920:1080:flags=lanczos")
        vf.append("setsar=1")

    vibrance = float(opts.get("vibrance", 1.0))
    if abs(vibrance - 1.0) > 0.01:
        vf.append(f"eq=saturation={vibrance:.3f}")

    encoder = encoder_for(opts.get("codec", "h264"), opts.get("hardware", "cpu"))
    target_mb = float(opts.get("targetSizeMb", 0))
    audio_kbps = 128

    cmd: list[str] = [ffmpeg, "-y", "-hide_banner",
                      "-ss", f"{start:.3f}", "-i", str(input_file),
                      "-t", f"{duration:.3f}"]
    if vf:
        cmd += ["-vf", ",".join(vf)]
    cmd += ["-c:v", encoder]

    if target_mb > 0:
        # bits available for video (account for audio + ~5% container overhead)
        total_kbps = (target_mb * 8 * 1024 * 0.95) / duration
        video_kbps = max(150, int(total_kbps - audio_kbps))
        # cap at quality-ceiling so short clips don't get wastefully large.
        # we stay in bitrate mode (with cbr-ish maxrate) so output remains
        # under the target — quality just won't keep climbing past the ceiling.
        codec = opts.get("codec", "h264")
        ceiling = {"h264": 10000, "hevc": 6000, "av1": 4000}.get(codec, 10000)
        video_kbps = min(video_kbps, ceiling)
        cmd += ["-b:v", f"{video_kbps}k",
                "-maxrate", f"{int(video_kbps * 1.2)}k",
                "-bufsize", f"{int(video_kbps * 2)}k"]
        if encoder in ("libx264", "libx265"):
            cmd += ["-preset", "medium"]
    else:
        cmd += quality_args(encoder)

    cmd += ["-c:a", "aac", "-b:a", f"{audio_kbps}k", "-movflags", "+faststart",
            str(output_file)]

    log.info("ffmpeg: %s", " ".join(cmd))
    run_ffmpeg_with_progress(cmd, duration, on_progress)


def run_job(job_id: str, items: list[dict], opts: dict) -> None:
    n = len(items)
    for i, item in enumerate(items):
        try:
            input_path = UPLOAD_DIR / item["filename"]
            if not input_path.exists():
                raise FileNotFoundError(item["filename"])
            stem = Path(item["filename"]).stem
            # strip leading timestamp_ from upload name for cleaner output
            stem = re.sub(r"^\d{10,}_", "", stem)
            container = opts.get("container", "mp4")
            ts = int(time.time() * 1000)
            output_path = OUTPUT_DIR / f"{stem}_{i+1}_{ts}.{container}"

            merged = {**opts, **item.get("opts", {})}

            def on_progress(frac, _i=i):
                with JOBS_LOCK:
                    if job_id in JOBS:
                        JOBS[job_id]["progress"] = (_i + frac) / n

            process_clip(input_path, output_path,
                         float(item["start"]), float(item["end"]),
                         merged, on_progress)
            job_append_output(job_id, output_path.name)
        except Exception as e:
            log.exception("job %s item %d failed", job_id, i)
            job_append_error(job_id, f"{item.get('filename','?')}: {e}")
        with JOBS_LOCK:
            if job_id in JOBS:
                JOBS[job_id]["done"] = i + 1
                JOBS[job_id]["progress"] = (i + 1) / n
    update_job(job_id, status="done")


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
    opts = {
        "codec": cfg.get("codec", "h264"),
        "hardware": cfg.get("hardware", "cpu"),
        "container": cfg.get("container", "mp4"),
        "targetSizeMb": float(cfg.get("targetSizeMb", 0)),
        "vibrance": float(cfg.get("vibrance", 1.0)),
        "stretch": bool(cfg.get("stretch", False)),
    }
    if "opts" in data and isinstance(data["opts"], dict):
        opts.update(data["opts"])

    job_id = uuid.uuid4().hex[:10]
    with JOBS_LOCK:
        JOBS[job_id] = {
            "status": "running", "progress": 0.0, "done": 0,
            "total": len(items), "outputs": [], "errors": [],
        }
    threading.Thread(target=run_job, args=(job_id, items, opts), daemon=True).start()
    return jsonify({"job_id": job_id})


@app.get("/api/job/<job_id>")
def api_job(job_id):
    with JOBS_LOCK:
        j = JOBS.get(job_id)
        if not j:
            return jsonify({"error": "unknown job"}), 404
        return jsonify(j)


# ---------- discord ----------
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
    fp = open(path, "rb")
    try:
        if mode == "webhook":
            url = data.get("webhook") or cfg.get("discordWebhook", "")
            if not url:
                return jsonify({"error": "no webhook configured"}), 400
            files = {"files[0]": (filename, fp, "video/mp4")}
            payload = {"content": content} if content else {}
            form = {"payload_json": json.dumps(payload)} if payload else {}
            r = requests.post(url, data=form, files=files, timeout=300)
        else:
            token = cfg.get("discordToken", "")
            if not token:
                return jsonify({"error": "no token configured"}), 400
            channel_id = data.get("channel_id") or cfg.get("discordChannelId", "")
            if not channel_id:
                return jsonify({"error": "no channel id"}), 400
            prefix = cfg.get("discordTokenPrefix", "")
            auth = f"{prefix} {token}".strip() if prefix else token
            url = f"https://discord.com/api/v10/channels/{channel_id}/messages"
            files = {"files[0]": (filename, fp, "video/mp4")}
            form = {"payload_json": json.dumps({"content": content})}
            r = requests.post(url, headers={"Authorization": auth},
                              data=form, files=files, timeout=300)
    finally:
        fp.close()

    if r.status_code >= 300:
        return jsonify({"error": r.text, "status": r.status_code}), 502
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


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=False, threaded=True)
