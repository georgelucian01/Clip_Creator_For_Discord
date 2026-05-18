# Clip Creator — Smart Pipeline Upgrade

This release reworks `app.py` to avoid unnecessary re-encoding, preserve quality,
and adapt to tight size budgets (Discord's 10 MB free tier in particular).

## What's new

### Three-mode pipeline (`decide_mode`)
Every clip is now classified before ffmpeg runs:

- **PASSTHROUGH** — no cut, no filters, no codec/container change, already under
  target. The file is copied byte-for-byte. Completes in well under a second.
- **STREAM_COPY_CUT** — cut only, no filters, no conversion, predicted output
  fits the target. Runs `ffmpeg -ss .. -to .. -i .. -c copy -avoid_negative_ts
  make_zero`. Lossless and near-instant. Snaps to the nearest keyframe.
- **FULL_ENCODE** — everything else (filters, size target, codec/container
  change, frame-accurate cut).

Predicted copy size is estimated from `input_bitrate × cut_duration × 1.05`.

### Auto resolution scaling
In FULL_ENCODE with a size target, output height is picked from the available
video bitrate (≥6 Mbps→source, 3.5–6→1080, 1.8–3.5→720, 1.0–1.8→540, <1.0→480).
Source height is the ceiling — never upscales. Uses `scale=-2:H:flags=lanczos`.

### Encoder auto-selection
Hardware encoders (AMF/NVENC/QSV) degrade badly at low bitrates, so the pipeline
swaps to the matching CPU encoder when the video budget falls below a per-codec
threshold (h264 2500, hevc 1800, av1 1500 kbps). CPU encoders use `slow` / preset
`6`.

### Adaptive audio
Audio bitrate scales with the total budget (192/128/96/64 kbps). Opus is used
automatically for webm containers or when audio drops below 96 kbps.

### Two-pass encoding
Real two-pass for CPU encoders when a size target is set and the clip is ≥5 s.
NVENC uses `-multipass fullres` instead. Passlog files live in a per-job temp dir
(`clip_<job_id>_*`) so concurrent jobs never collide; cleaned up afterward.

### Better per-encoder quality args
Refined `quality_args()` (CRF/CQ) plus a new `bitrate_mode_args()` with
encoder-specific tuning (x265 psy params, x264 `tune film`, svt-av1 `tune=0`,
NVENC `p7/hq`, AMF `quality/transcoding`). Adds `-pix_fmt`, GOP sizing, B-frames,
`-fflags +genpts`, optional 10-bit.

### Codec ceilings + floor
AV1 ceiling lowered to 3500 kbps. A hard 300 kbps video floor refuses jobs that
would produce unwatchable output, with a clear, actionable error.

### Pre-flight validation
Before queuing: trim-range bounds, size-target achievability, and free disk space
(≥2× input). Errors are returned immediately, before any encoding.

### Job system
- `POST /api/job/<id>/cancel` — terminates ffmpeg (kill after 3 s), marks
  `cancelled`.
- `GET /api/job/<id>/log` — last ~200 lines of ffmpeg stderr.
- JOBS capped at 50 (oldest finished entries evicted).
- Concurrency cap via semaphore; overflow jobs sit in `queued`.
- Per-stage reporting in the `stage` field (`probing`, `pass1`, `pass2`,
  `single_pass`, `stream_copy`, `passthrough`, `done`, `cancelled`).

### Low-bitrate denoise
`hqdn3d=1.5:1.5:6:6` is added when video bitrate < 1500 kbps.

### Probe cache
ffprobe results cached in memory, keyed by `(filename, mtime, size)`.

### Discord
- Tier presets (`discordTier`: free 10 MB / basic 50 MB / nitro 500 MB) override
  `targetSizeMb`.
- Optional one-shot auto-retry at an 80 % target on a size rejection.
- Error responses now surface Discord's human-readable message.

### Cleanup
- Default `discordChannelId` is now `""` (was a hardcoded channel ID).
- `-loglevel warning` on ffmpeg invocations; stderr still captured for errors.
- `pathlib.Path` used consistently.

## New config keys (with defaults)

| Key | Default | Purpose |
|-----|---------|---------|
| `frameAccurateCut` | `false` | Force FULL_ENCODE for an exact (non-keyframe) cut |
| `outputHeight` | `"auto"` | `auto` / `source` / `1440` / `1080` / `720` / `540` / `480` |
| `encoderQualityMode` | `"auto"` | `auto` / `hardware_always` / `software_always` |
| `audioBitrateMode` | `"auto"` | `"auto"` or a fixed numeric kbps value |
| `audioCodec` | `"aac"` | `aac` / `opus` (auto-switches to opus for webm/low bitrate) |
| `twoPass` | `true` | Two-pass encoding (applies only when `targetSizeMb > 0`) |
| `tenBit` | `false` | 10-bit output for hevc/av1 |
| `maxConcurrentJobs` | `2` | Concurrent encoding jobs (read once at startup) |
| `lowBitrateDenoise` | `true` | Mild denoise when video bitrate < 1500 kbps |
| `discordTier` | `"free"` | `free` / `basic` / `nitro` — overrides `targetSizeMb` |
| `autoRetryOnDiscordReject` | `false` | Re-encode at 80 % target and retry once on size reject |

Existing config keys are unchanged. Old `config.json` files load cleanly — new
keys take the defaults above (`load_config` merges `config.json` over
`DEFAULT_CONFIG`).

## Notes

- Route paths and response shapes are unchanged; the existing frontend keeps
  working. New endpoints (`/cancel`, `/log`) and the `stage` field are additive.
- `discordTier` defaults to `free`, so the effective size target is 10 MB unless
  the tier is changed in settings.

---

# Phase 2 — Missing Features & Polish

This phase adds the remaining encoding upgrades, a documentation page, and a
round of UI polish. No new Python dependencies were added — every new feature
runs through the bundled ffmpeg/ffprobe binaries.

## Backend features

### Hardware-accelerated decoding
FULL_ENCODE jobs now decode the input with `-hwaccel auto -hwaccel_output_format
auto` (placed before `-i`). Picks DXVA/VideoToolbox/VAAPI per platform and falls
back to software silently. Skipped automatically while tone-mapping HDR→SDR, as
some hwaccel paths choke on the color conversion. Config: `hwaccelDecode`.

### Frame-rate control
The **Frame rate** setting (`fpsMode`) controls output fps:
`auto` drops a high-fps source toward 30 when the bitrate is tight
(<2200 kbps → 48, <1200 kbps → 30); `source` keeps the source rate; or pick a
fixed target — `60` / `48` / `30` / `custom` (`fpsCustom`). The output is never
below 30 fps and never upscaled above the source rate. The fps filter is
applied before scaling.

### HDR → SDR tone mapping
HDR sources (PQ/HLG transfer or BT.2020 color) are detected during probing.
10-bit HEVC/AV1 passes HDR through; otherwise the clip is tone-mapped to BT.709
with the Hable operator. Config: `hdrToneMap`. `is_hdr` is stored in the probe
cache.

### Audio loudness normalization
FULL_ENCODE audio runs through `loudnorm=I=-16:TP=-1.5:LRA=11` (EBU R128) for
consistent volume across clips from different games. Config: `normalizeAudio`.

### Auto black-bar detection
`detect_crop()` samples 2 s at the clip midpoint and is cached with the probe.
`stretchTo169` now uses the detected crop instead of blindly stretching, and
warns when no black bars are found (4:3 stretched-res clips crop correctly).

### Scene-aware keyframes
FULL_ENCODE adds a 2 s max GOP plus `-keyint_min <fps>` for clips ≥3 s so the
encoder can still place a keyframe on a scene change (short clips keep the
whole-clip-GOP behavior). The generic `-sc_threshold` AVOption is intentionally
not passed — libx264/libx265/libsvtav1/hardware encoders run their own scene-cut
by default (x264's default is already 40) and the option only emits a warning.

### MP4 faststart
MP4 output gets `-movflags +faststart` so playback can start before download
completes. (Already present; now logged in the job summary.)

### FFmpeg capability probe + corrupt-output guard
At startup the app probes `ffmpeg -encoders` and additionally runs a real test
encode for each hardware encoder (results cached in `encoder_cache.json`).
`GET /api/capabilities` exposes the result; the settings UI grays out hardware
the machine lacks. If a selected encoder is unavailable, the pipeline falls back
to the CPU equivalent.

As a general safety net, every hardware-encoded output is **decode-verified**
after encoding. If the stream is broken, that encoder is disabled and the clip
is re-encoded once on the CPU fallback — so a corrupt clip never reaches the
user.

**av1_amf + hwaccel fix:** `-hwaccel auto` decode feeding the AMF AV1 encoder
produces a corrupt bitstream (an ffmpeg/AMF bug — the AV1 encoder itself is
fine). Hardware decoding is now skipped specifically for `av1_amf`; `hevc_amf`,
`h264_amf` and CPU encoders are unaffected and keep hwaccel.

### Input integrity check
Pre-flight runs a 1-second test decode and warns on very recently modified
files — rejects clips that OBS/ShadowPlay is still writing.

### CRF rate-control ceiling
CRF/CQP mode (`targetSizeMb=0`) adds a resolution-scaled `-maxrate`/`-bufsize`
ceiling so a complex clip can't balloon. Config: `crfMaxrateMultiplier`.

### Configurable denoise strength
Low-bitrate denoise strength is now `light`/`medium`/`strong`. Config:
`denoiseStrength`.

### Config migration helper
`migrate_config()` deep-merges old config files; on startup the merged result is
written back so users see every available key.

### Thumbnail, duplicate detection, queue persistence
- `GET /api/job/<id>/thumbnail` returns a cached JPG frame from the first output.
- An identical job re-submitted within 5 s returns the original job id.
- Pending jobs are mirrored to `jobs_state.json` and re-queued on restart.

## Frontend

- **No auto-open.** Completed jobs no longer open the output drawer or steal
  focus — a toast and an optional browser notification announce completion.
- **Help page.** A `?` button opens a docs modal rendering `static/docs.md`
  (rendered by a tiny built-in markdown renderer — no CDN, works offline).
- **Toasts & notifications** for job completion / failure / cancellation.
- **Copy buttons** for the webhook URL, token, and job log.
- **Job detail modal** with thumbnail, per-clip summary and ffmpeg log tail.
- **Collapsible settings sections** (details/summary accordion).
- **Dark / light mode toggle**, stored in `localStorage`.
- **Keyboard shortcuts**: `Ctrl/Cmd+Enter` submits, `Escape` closes modals.
- **HDR badge** on clip cards for HDR sources.

## Presets & encode speed

The Settings panel now has **three one-click presets** (Discord / Max quality /
Fast) that fill the form, plus an **Encode speed** setting (`encodeSpeed`:
`quality` / `balanced` / `fast`) that maps to each encoder's internal preset.
`quality` reproduces the original encoder presets, so the default behaviour is
unchanged; `balanced` and `fast` trade quality for speed.

Pre-flight validation also now rejects an impossible **WebM + non-AV1** codec
combination with a clear message instead of letting ffmpeg fail cryptically
(WebM only carries AV1 video among the codecs this app offers).

The **Discord preset** now targets WebM (AV1 10-bit + Opus) — WebM pairs
natively with Opus and 10-bit AV1 cuts banding; both are universally supported.

**SVT-AV1 fix:** newer SVT-AV1 builds reject `-maxrate` in bitrate-target mode
(`Max Bitrate only supported with CRF mode`). `-maxrate`/`-bufsize` are no
longer passed to `libsvtav1` in size-target mode — it does its own VBR rate
control from `-b:v`. Other encoders are unaffected.

## New config keys (Phase 2)

| Key | Default | Section | Purpose |
|-----|---------|---------|---------|
| `hwaccelDecode` | `true` | Encoder | `-hwaccel auto` for decoding |
| `encodeSpeed` | `"quality"` | Encoder | `quality` / `balanced` / `fast` encoder preset |
| `fpsMode` | `"auto"` | Smart Pipeline | `auto` / `source` / `60` / `48` / `30` / `custom` |
| `fpsCustom` | `48` | Smart Pipeline | Manual fps target when `fpsMode=custom` |
| `hdrToneMap` | `true` | Smart Pipeline | Tone-map HDR→SDR when needed |
| `normalizeAudio` | `true` | Audio | EBU R128 loudness normalization |
| `denoiseStrength` | `"light"` | Smart Pipeline | light / medium / strong hqdn3d |
| `crfMaxrateMultiplier` | `2.0` | Encoder | Maxrate ceiling multiplier for CRF |

All keys merge cleanly into old `config.json` files via `migrate_config()`.

## New endpoints

- `GET /api/capabilities` — encoders supported by this ffmpeg build.
- `GET /api/job/<id>/thumbnail` — cached JPG frame from the job's first output.

Route paths and existing response shapes are unchanged.

---

# UI Polish — Settings Modal Refresh

A cosmetic-only pass over the settings UI. No backend, route, config-key or
JSON-shape changes — purely `templates/index.html`, `static/app.js` and
`static/styles.css`.

## Settings modal redesign

- Each collapsible section (Encoder / Smart pipeline / Audio / Defaults / Jobs /
  Discord) is now a rounded **card** with a gradient-tinted section icon, a
  hoverable header and a rotating expand chevron.
- Form inputs gained custom dropdown arrows, an accent focus glow and hover
  borders; checkboxes are framed pill rows that highlight on hover.
- The modal is wider for settings (700px), has a gradient header strip and a
  **sticky Save / Cancel footer** that stays reachable while scrolling.
- Preset buttons are bolder; the **Discord** preset uses an accent gradient to
  read as the recommended choice.

## Special-version banner removed

The "This is a special version of Clip Creator..." intro banner was removed
along with its dismiss button, the `clipBannerDismissed` localStorage handling
and the unused `.special-banner` CSS.
