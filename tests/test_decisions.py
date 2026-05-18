"""Exhaustive unit tests for the pure decision logic in app.py.

These touch no ffmpeg and no filesystem - they cover the full combination space
of every classification / picker / filter-builder so the integration tests can
stay a small curated set.
"""
from pathlib import Path

import pytest

import app as a

MB = a.MB


# ---------- normalize_codec ----------
@pytest.mark.parametrize("raw,fam", [
    ("h264", "h264"), ("avc", "h264"), ("avc1", "h264"),
    ("hevc", "hevc"), ("h265", "hevc"),
    ("av1", "av1"), ("", ""), ("vp9", "vp9"), (None, ""),
])
def test_normalize_codec(raw, fam):
    assert a.normalize_codec(raw) == fam


# ---------- normalize_segments ----------
def test_normalize_segments_legacy():
    assert a.normalize_segments({"start": 1, "end": 5}) == [(1.0, 5.0)]


def test_normalize_segments_list():
    item = {"segments": [{"start": 0, "end": 2}, {"start": 4, "end": 7}]}
    assert a.normalize_segments(item) == [(0.0, 2.0), (4.0, 7.0)]


def test_normalize_segments_empty_list():
    with pytest.raises(ValueError):
        a.normalize_segments({"segments": []})


def test_normalize_segments_not_a_list():
    with pytest.raises(ValueError):
        a.normalize_segments({"segments": "nope"})


def test_normalize_segments_missing_keys():
    with pytest.raises((KeyError, TypeError)):
        a.normalize_segments({"segments": [{"start": 0}]})


# ---------- decide_mode ----------
def _info(**kw):
    base = {"duration": 60.0, "codec": "h264", "size": 5 * MB,
            "vbitrate": 800.0, "abitrate": 128.0}
    base.update(kw)
    return base


def _opts(**kw):
    base = {"codec": "h264", "container": "mp4", "targetSizeMb": 0,
            "vibrance": 1.0, "stretch": False, "frameAccurateCut": False}
    base.update(kw)
    return base


def test_decide_mode_multi_segment_always_full_encode():
    mode, reason, _ = a.decide_mode(Path("x.mp4"), _info(), _opts(),
                                    [(0, 10), (20, 30)])
    assert mode == "FULL_ENCODE"
    assert "multi-segment" in reason


def test_decide_mode_passthrough_whole_clip():
    mode, _, _ = a.decide_mode(Path("x.mp4"), _info(), _opts(), [(0, 60)])
    assert mode == "PASSTHROUGH"


def test_decide_mode_passthrough_when_under_target():
    mode, _, _ = a.decide_mode(Path("x.mp4"), _info(size=5 * MB),
                               _opts(targetSizeMb=10), [(0, 60)])
    assert mode == "PASSTHROUGH"


def test_decide_mode_full_encode_when_over_target_no_cut():
    mode, reason, _ = a.decide_mode(Path("x.mp4"), _info(size=50 * MB),
                                    _opts(targetSizeMb=10), [(0, 60)])
    assert mode == "FULL_ENCODE"
    assert "exceeds" in reason


def test_decide_mode_stream_copy_cut():
    mode, _, extra = a.decide_mode(Path("x.mp4"), _info(),
                                   _opts(), [(5, 20)])
    assert mode == "STREAM_COPY_CUT"
    assert "predicted_mb" in extra


def test_decide_mode_frame_accurate_forces_encode():
    mode, reason, _ = a.decide_mode(Path("x.mp4"), _info(),
                                    _opts(frameAccurateCut=True), [(5, 20)])
    assert mode == "FULL_ENCODE"
    assert "frameAccurateCut" in reason


@pytest.mark.parametrize("opts_over", [
    {"stretch": True},
    {"vibrance": 1.4},
])
def test_decide_mode_filters_force_encode(opts_over):
    mode, reason, _ = a.decide_mode(Path("x.mp4"), _info(),
                                    _opts(**opts_over), [(0, 60)])
    assert mode == "FULL_ENCODE"
    assert "filter" in reason


def test_decide_mode_codec_change_forces_encode():
    mode, reason, _ = a.decide_mode(Path("x.mp4"), _info(codec="hevc"),
                                    _opts(codec="h264"), [(0, 60)])
    assert mode == "FULL_ENCODE"
    assert "codec change" in reason


def test_decide_mode_container_change_forces_encode():
    mode, reason, _ = a.decide_mode(Path("x.mkv"), _info(),
                                    _opts(container="mp4"), [(0, 60)])
    assert mode == "FULL_ENCODE"
    assert "container change" in reason


def test_decide_mode_cut_predicted_over_target_encodes():
    # huge bitrate so the predicted copy size blows the 1 MB target
    mode, _, _ = a.decide_mode(Path("x.mp4"),
                               _info(vbitrate=20000.0, abitrate=256.0),
                               _opts(targetSizeMb=1), [(5, 40)])
    assert mode == "FULL_ENCODE"


# ---------- pick_resolution ----------
@pytest.mark.parametrize("override,src,expect", [
    ("source", 1440, 1440),
    ("720", 1440, 720),
    ("1080", 720, 720),          # never upscale past source
    ("auto", 1080, 1080),        # auto + no bitrate -> source
])
def test_pick_resolution_overrides(override, src, expect):
    h, _ = a.pick_resolution(None, src, override)
    assert h == expect


@pytest.mark.parametrize("kbps,expect", [
    (8000, 1440), (4000, 1080), (2000, 720), (1200, 540), (600, 480),
])
def test_pick_resolution_auto_bitrate_ladder(kbps, expect):
    h, _ = a.pick_resolution(kbps, 1440, "auto")
    assert h == expect


def test_pick_resolution_caps_at_source():
    h, _ = a.pick_resolution(8000, 720, "auto")
    assert h == 720


# ---------- pick_encoder ----------
def test_pick_encoder_software_always():
    enc, swapped, _ = a.pick_encoder("hevc", "nvidia", 5000, "software_always")
    assert enc == "libx265" and swapped is True


def test_pick_encoder_hardware_always():
    enc, swapped, _ = a.pick_encoder("hevc", "amd", 100, "hardware_always")
    assert enc == "hevc_amf" and swapped is False


def test_pick_encoder_auto_low_bitrate_swaps_to_cpu():
    enc, swapped, _ = a.pick_encoder("av1", "amd", 500, "auto")
    assert enc == "libsvtav1" and swapped is True


def test_pick_encoder_auto_high_bitrate_keeps_hardware():
    enc, swapped, _ = a.pick_encoder("h264", "nvidia", 8000, "auto")
    assert enc == "h264_nvenc" and swapped is False


def test_pick_encoder_cpu_hardware_never_swaps():
    enc, swapped, _ = a.pick_encoder("h264", "cpu", 100, "auto")
    assert enc == "libx264" and swapped is False


# ---------- pick_audio_bitrate ----------
@pytest.mark.parametrize("total,expect", [
    (5000, 192), (3000, 128), (1500, 96), (500, 64), (None, 128),
])
def test_pick_audio_bitrate_auto(total, expect):
    assert a.pick_audio_bitrate(total, "auto") == expect


@pytest.mark.parametrize("mode", [64, "96", 256])
def test_pick_audio_bitrate_explicit(mode):
    assert a.pick_audio_bitrate(1000, mode) == int(mode)


# ---------- resolve_audio_codec ----------
def test_resolve_audio_codec_explicit_opus():
    enc, _ = a.resolve_audio_codec("opus", "mp4", 192)
    assert enc == "libopus"


def test_resolve_audio_codec_webm_forces_opus():
    enc, _ = a.resolve_audio_codec("aac", "webm", 192)
    assert enc == "libopus"


def test_resolve_audio_codec_low_bitrate_forces_opus():
    enc, _ = a.resolve_audio_codec("aac", "mp4", 64)
    assert enc == "libopus"


def test_resolve_audio_codec_normal_aac():
    enc, _ = a.resolve_audio_codec("aac", "mp4", 128)
    assert enc == "aac"


# ---------- speed_preset ----------
@pytest.mark.parametrize("enc", ["libx264", "libx265", "libsvtav1",
                                 "hevc_amf", "h264_nvenc", "av1_qsv"])
@pytest.mark.parametrize("speed", ["quality", "balanced", "fast"])
def test_speed_preset_every_encoder_and_speed(enc, speed):
    assert a.speed_preset(enc, speed)  # non-empty string


def test_speed_preset_unknown_speed_defaults_quality():
    assert a.speed_preset("libx264", "bogus") == a.speed_preset("libx264", "quality")


# ---------- quality_args / bitrate_mode_args ----------
@pytest.mark.parametrize("enc", ["libx264", "libx265", "libsvtav1",
                                 "hevc_amf", "h264_amf", "av1_amf",
                                 "hevc_nvenc", "av1_nvenc", "h264_qsv"])
def test_quality_args_non_empty(enc):
    assert a.quality_args(enc, "balanced")


@pytest.mark.parametrize("enc", ["libx264", "libx265", "libsvtav1",
                                 "hevc_amf", "h264_nvenc", "av1_qsv"])
def test_bitrate_mode_args_non_empty(enc):
    assert a.bitrate_mode_args(enc, "fast")


# ---------- structural_args ----------
def test_structural_args_ten_bit_pixfmt():
    args = a.structural_args("libx265", "hevc", {}, 30, 10, ten_bit=True)
    assert "yuv420p10le" in args


def test_structural_args_eight_bit_pixfmt():
    args = a.structural_args("libx264", "h264", {}, 30, 10, ten_bit=False)
    assert "yuv420p" in args


def test_structural_args_short_clip_single_gop():
    # a <3s clip becomes one GOP: -g equals the total frame count
    args = a.structural_args("libx264", "h264", {}, 30, 2.0)
    gi = args.index("-g")
    assert args[gi + 1] == "60"


def test_structural_args_h264_has_bframes():
    args = a.structural_args("libx264", "h264", {}, 30, 10)
    assert "-bf" in args


# ---------- resolve_target_fps ----------
def test_resolve_target_fps_source_keeps():
    assert a.resolve_target_fps({"fpsMode": "source"}, 60, 5000) == (None, None)


def test_resolve_target_fps_manual_below_source():
    fps, note = a.resolve_target_fps({"fpsMode": "30"}, 60, 5000)
    assert fps == 30 and note


def test_resolve_target_fps_manual_at_or_above_source_keeps():
    fps, _ = a.resolve_target_fps({"fpsMode": "60"}, 60, 5000)
    assert fps is None


def test_resolve_target_fps_custom():
    fps, _ = a.resolve_target_fps({"fpsMode": "custom", "fpsCustom": 40}, 60, 5000)
    assert fps == 40


def test_resolve_target_fps_floor_is_30():
    fps, _ = a.resolve_target_fps({"fpsMode": "10"}, 60, 5000)
    assert fps == 30


def test_resolve_target_fps_auto_tight_budget_drops_to_30():
    fps, _ = a.resolve_target_fps({"fpsMode": "auto"}, 60, 800)
    assert fps == 30


def test_resolve_target_fps_auto_adequate_budget_keeps():
    assert a.resolve_target_fps({"fpsMode": "auto"}, 60, 6000) == (None, None)


# ---------- build_filters ----------
def _finfo(**kw):
    base = {"fps": 30.0, "is_hdr": False, "width": 320, "height": 240,
            "detected_crop": None}
    base.update(kw)
    return base


def test_build_filters_hdr_tonemap_applied():
    vf, _ = a.build_filters({"hdrToneMap": True}, _finfo(is_hdr=True),
                            240, 240, None, "hevc", ten_bit=False)
    assert any("tonemap" in f for f in vf)


def test_build_filters_hdr_ten_bit_passthrough_no_tonemap():
    vf, notes = a.build_filters({"hdrToneMap": True}, _finfo(is_hdr=True),
                                240, 240, None, "hevc", ten_bit=True)
    assert not any("tonemap" in f for f in vf)
    assert any("HDR" in n for n in notes)


def test_build_filters_fps_filter():
    vf, _ = a.build_filters({"fpsMode": "30"}, _finfo(fps=60.0),
                            240, 240, 5000, "h264")
    assert "fps=30" in vf


def test_build_filters_vibrance():
    vf, _ = a.build_filters({"vibrance": 1.5}, _finfo(), 240, 240, None, "h264")
    assert any(f.startswith("eq=saturation") for f in vf)


def test_build_filters_downscale():
    vf, _ = a.build_filters({}, _finfo(height=1440), 1440, 720, None, "h264")
    assert any("scale=-2:720" in f for f in vf)


def test_build_filters_stretch_with_detected_crop():
    info = _finfo(width=320, height=240, detected_crop=(280, 180, 20, 30))
    vf, notes = a.build_filters({"stretch": True}, info, 240, 240, None, "h264")
    assert any(f.startswith("crop=280:180:20:30") for f in vf)
    assert any("setsar=1" == f for f in vf)


def test_build_filters_stretch_without_bars_warns():
    vf, notes = a.build_filters({"stretch": True}, _finfo(detected_crop=None),
                                240, 240, None, "h264")
    assert any("WARNING" in n for n in notes)


def test_build_filters_low_bitrate_denoise():
    vf, _ = a.build_filters({"lowBitrateDenoise": True, "denoiseStrength": "medium"},
                            _finfo(), 240, 240, 900, "h264")
    assert any("hqdn3d" in f for f in vf)


def test_build_filters_no_denoise_at_high_bitrate():
    vf, _ = a.build_filters({"lowBitrateDenoise": True}, _finfo(),
                            240, 240, 8000, "h264")
    assert not any("hqdn3d" in f for f in vf)


# ---------- build_video_graph / build_av_graph ----------
def test_build_video_graph_two_segments():
    graph, vmap = a.build_video_graph([(0, 2), (4, 7)], ["scale=320:240"])
    assert "concat=n=2:v=1:a=0" in graph
    assert "trim=0.000:2.000" in graph and "trim=4.000:7.000" in graph
    assert graph.endswith("[vout]")
    assert vmap == ["-map", "[vout]"]


def test_build_video_graph_empty_vfx_uses_null():
    graph, _ = a.build_video_graph([(0, 2), (3, 5)], [])
    assert "[cv]null[vout]" in graph


def test_build_av_graph_with_audio_filters():
    graph, vmap, amap = a.build_av_graph([(0, 2), (4, 6)], [],
                                         ["loudnorm=I=-16:TP=-1.5:LRA=11",
                                          "volume=6.0dB"])
    assert "concat=n=2:v=1:a=1" in graph
    assert "volume=6.0dB" in graph
    assert amap == ["-map", "[aout]"]


def test_build_av_graph_no_audio_filters_maps_concat_directly():
    graph, vmap, amap = a.build_av_graph([(0, 2), (4, 6)], [], [])
    assert amap == ["-map", "[ca]"]
    assert "[aout]" not in graph


# ---------- volume booster (Phase 4) ----------
@pytest.mark.parametrize("val,db", [
    ("none", 0.0), ("off", 0.0), ("", 0.0), (None, 0.0),
    ("low", 3.0), ("medium", 6.0), ("high", 10.0),
    ("8.5", 8.5), (12, 12.0), (-3, -3.0), ("garbage", 0.0),
])
def test_resolve_volume_db(val, db):
    assert a.resolve_volume_db(val) == db


def test_audio_filters_loudnorm_then_volume():
    afx = a.audio_filters(True, 6.0)
    assert afx[0].startswith("loudnorm")
    assert afx[-1] == "volume=6.0dB"


def test_audio_filters_normalize_only():
    assert a.audio_filters(True, 0.0) == ["loudnorm=I=-16:TP=-1.5:LRA=11"]


def test_audio_filters_volume_only():
    assert a.audio_filters(False, 10.0) == ["volume=10.0dB"]


def test_audio_filters_none():
    assert a.audio_filters(False, 0.0) == []


# ---------- config / migration ----------
def test_default_config_has_phase4_keys():
    assert "volumeBoost" in a.DEFAULT_CONFIG
    assert "cleanupDays" in a.DEFAULT_CONFIG


def test_migrate_config_empty_fills_all_defaults():
    merged, n_new = a.migrate_config({})
    assert merged == a.DEFAULT_CONFIG
    assert n_new == len(a.DEFAULT_CONFIG)


def test_migrate_config_preserves_user_values():
    merged, n_new = a.migrate_config({"codec": "av1", "vibrance": 1.3})
    assert merged["codec"] == "av1" and merged["vibrance"] == 1.3
    assert n_new == len(a.DEFAULT_CONFIG) - 2


def test_migrate_config_ignores_unknown_keys():
    merged, _ = a.migrate_config({"bogusKey": 123})
    assert "bogusKey" not in merged


# ---------- verify_encoder ----------
def test_verify_encoder_cpu_unchanged():
    enc, note = a.verify_encoder("libx264")
    assert enc == "libx264" and note is None


def test_verify_encoder_falls_back_when_unavailable(monkeypatch):
    monkeypatch.setitem(a.AVAILABLE_ENCODERS, "av1_amf", False)
    enc, note = a.verify_encoder("av1_amf")
    assert enc == "libsvtav1" and note
