"""API-route tests: upload, capabilities, config, output management, filmstrip /
thumbnail endpoints, job control, and every /api/process pre-flight rejection.
"""
import io
import json

import pytest

import app as a
from conftest import needs_ffmpeg
from _util import base_opts, run_job, upload

pytestmark = needs_ffmpeg


@pytest.fixture
def config_backup():
    """Snapshot config.json and restore it verbatim afterwards - the config
    tests must not disturb the user's real settings or Discord secrets."""
    cf = a.CONFIG_FILE
    original = cf.read_bytes() if cf.exists() else None
    yield
    if original is not None:
        cf.write_bytes(original)


# ---------- health / capabilities ----------
def test_health(client):
    body = client.get("/api/health").get_json()
    assert "ffmpeg" in body and "ffmpeg_path" in body


def test_capabilities(client):
    body = client.get("/api/capabilities").get_json()
    assert isinstance(body, dict)
    # CPU encoders are always reported
    assert "libx264" in body


# ---------- upload ----------
def test_upload_no_file(client):
    r = client.post("/api/upload")
    assert r.status_code == 400


def test_upload_rejects_non_video(client):
    data = {"file": (io.BytesIO(b"not a video"), "junk.txt")}
    r = client.post("/api/upload", data=data, content_type="multipart/form-data")
    assert r.status_code == 400


def test_upload_ok_returns_info(client, sample):
    body = upload(client, sample)
    assert body["filename"]
    info = body["info"]
    # frozen info shape the frontend relies on
    for key in ("duration", "width", "height", "is_hdr", "has_audio",
                "suggest_stretch", "detected_crop"):
        assert key in info
    # lazy crop: detection has NOT run on a plain upload
    assert info["detected_crop"] is None
    assert info["crop_checked"] is False


def test_upload_4x3_suggests_stretch(client, sample_letterbox):
    """A 320x240 (4:3) source should come back with suggest_stretch true."""
    info = upload(client, sample_letterbox)["info"]
    assert info["suggest_stretch"] is True


# ---------- config ----------
def test_config_get_masks_secrets(client):
    body = client.get("/api/config").get_json()
    assert isinstance(body, dict)
    assert "volumeBoost" in body and "cleanupDays" in body
    if body.get("discordToken"):
        assert set(body["discordToken"]) <= {"•"}


def test_config_save_roundtrip(client, config_backup):
    before = client.get("/api/config").get_json()
    new = dict(before)
    new["volumeBoost"] = "high"
    new["cleanupDays"] = 7
    r = client.post("/api/config", json=new)
    assert r.status_code == 200
    after = client.get("/api/config").get_json()
    assert after["volumeBoost"] == "high"
    assert after["cleanupDays"] == 7


def test_config_save_ignores_unknown_keys(client, config_backup):
    r = client.post("/api/config", json={"bogusKey": 1, "codec": "av1"})
    assert r.status_code == 200
    assert "bogusKey" not in a.load_config()


# ---------- /api/process pre-flight rejections ----------
def test_process_no_items(client):
    r = client.post("/api/process", json={"items": []})
    assert r.status_code == 400


def test_process_missing_segments(client, sample):
    up = upload(client, sample)
    code, body = run_job(client, [{"filename": up["filename"]}], base_opts())
    assert code == 400 and "error" in body


def test_process_empty_segments(client, sample):
    up = upload(client, sample)
    code, body = run_job(client, [{"filename": up["filename"], "segments": []}],
                         base_opts())
    assert code == 400


def test_process_segment_out_of_range(client, sample):
    up = upload(client, sample)
    code, body = run_job(
        client, [{"filename": up["filename"], "segments": [{"start": 0, "end": 999}]}],
        base_opts())
    assert code == 400 and "error" in body


def test_process_unknown_upload(client):
    code, body = run_job(
        client, [{"filename": "does_not_exist.mp4", "segments": [{"start": 0, "end": 1}]}],
        base_opts())
    assert code == 400


def test_process_webm_non_av1_rejected(client, sample):
    up = upload(client, sample)
    code, body = run_job(
        client, [{"filename": up["filename"], "segments": [{"start": 0, "end": 6}]}],
        base_opts(codec="h264", container="webm"))
    assert code == 400 and "webm" in body["error"].lower()


def test_process_size_floor_rejection(client, sample):
    """A 6s clip cannot fit a 0.1 MB target at watchable quality -> rejected."""
    up = upload(client, sample)
    code, body = run_job(
        client, [{"filename": up["filename"], "segments": [{"start": 0, "end": 6}]}],
        base_opts(codec="h264", container="mp4", targetSizeMb=0.1, vibrance=1.2))
    assert code == 400 and "error" in body


# ---------- duplicate-submit dedup ----------
def test_duplicate_submit_returns_same_job(client, sample):
    up = upload(client, sample)
    item = {"filename": up["filename"], "segments": [{"start": 0, "end": 6}]}
    opts = base_opts(vibrance=1.25)
    r1 = client.post("/api/process", json={"items": [item], "opts": opts})
    r2 = client.post("/api/process", json={"items": [item], "opts": opts})
    assert r1.status_code == 200 and r2.status_code == 200
    assert r1.get_json()["job_id"] == r2.get_json()["job_id"]
    # drain it so it does not linger
    run_job(client, [item], opts)


# ---------- job control ----------
def test_job_unknown_404(client):
    assert client.get("/api/job/nope").status_code == 404


def test_job_cancel(client, sample):
    up = upload(client, sample)
    item = {"filename": up["filename"], "segments": [{"start": 0, "end": 6}]}
    r = client.post("/api/process",
                     json={"items": [item], "opts": base_opts(vibrance=1.3)})
    job_id = r.get_json()["job_id"]
    cr = client.post(f"/api/job/{job_id}/cancel")
    assert cr.status_code == 200 and cr.get_json()["ok"] is True
    assert client.get(f"/api/job/{job_id}").get_json()["status"] == "cancelled"


def test_job_log_endpoint(client, sample):
    up = upload(client, sample)
    _, job = run_job(
        client, [{"filename": up["filename"], "segments": [{"start": 0, "end": 6}],
                  "opts": {"vibrance": 1.2}}], base_opts())
    # the job id is not in the finished view; re-run a tiny one for the log route
    r = client.post("/api/process", json={
        "items": [{"filename": up["filename"], "segments": [{"start": 0, "end": 6}]}],
        "opts": base_opts(vibrance=1.21)})
    job_id = r.get_json()["job_id"]
    lr = client.get(f"/api/job/{job_id}/log")
    assert lr.status_code == 200 and "log" in lr.get_json()


def test_job_view_hides_internal_fields(client, sample):
    up = upload(client, sample)
    r = client.post("/api/process", json={
        "items": [{"filename": up["filename"], "segments": [{"start": 0, "end": 6}]}],
        "opts": base_opts(vibrance=1.22)})
    view = client.get(f"/api/job/{r.get_json()['job_id']}").get_json()
    assert "proc" not in view and "log" not in view
    assert "stage" in view and "details" in view


# ---------- outputs ----------
def test_outputs_list_and_delete(client, sample):
    up = upload(client, sample)
    _, job = run_job(
        client, [{"filename": up["filename"], "segments": [{"start": 0, "end": 6}],
                  "opts": {"vibrance": 1.3}}], base_opts())
    name = job["outputs"][0]
    listing = client.get("/api/outputs").get_json()
    assert any(f["filename"] == name for f in listing)
    # the file is downloadable
    assert client.get(f"/api/output/{name}").status_code == 200
    # and deletable
    assert client.delete(f"/api/output/{name}").status_code == 200
    listing2 = client.get("/api/outputs").get_json()
    assert not any(f["filename"] == name for f in listing2)


# ---------- filmstrip / thumbnail ----------
def test_filmstrip_endpoint(client, sample):
    up = upload(client, sample)
    r = client.get(f"/api/filmstrip/{up['filename']}")
    assert r.status_code == 200 and r.mimetype == "image/jpeg"


def test_filmstrip_unknown_file(client):
    assert client.get("/api/filmstrip/nope.mp4").status_code == 404


def test_upload_thumbnail_endpoint(client, sample):
    up = upload(client, sample)
    r = client.get(f"/api/upload-thumb/{up['filename']}")
    assert r.status_code == 200 and r.mimetype == "image/jpeg"


def test_preview_serves_upload(client, sample):
    up = upload(client, sample)
    assert client.get(f"/api/preview/{up['filename']}").status_code == 200


def test_delete_upload(client, sample):
    up = upload(client, sample)
    assert client.delete(f"/api/upload/{up['filename']}").status_code == 200
    assert (a.UPLOAD_DIR / up["filename"]).exists() is False
