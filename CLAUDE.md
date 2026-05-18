# CLAUDE.md — Clip Creator (Smart Pipeline Edition, Phase 2)

Flask-based video clipping tool: cut, recolor, encode, and upload gameplay clips
to Discord. Single-user local app, Windows. Phase 1 built the three-mode
pipeline; Phase 2 added hardware decode, HDR, loudnorm, capability probing,
presets, a docs page and more. This file reflects the current state.

## Run it

```
python app.py        # serves http://127.0.0.1:5000
```

Windows + PowerShell. The user runs the app locally to test features — **edit
the main files directly, no branches/worktrees, no commits/PRs unless asked.**
Python venv is in `.venv/`. ffmpeg/ffprobe ship in `ffmpeg/bin/` (path is
configurable via `ffmpegPath`). The app must also be runnable as a packaged
`.exe` (PyInstaller) — keep `BUNDLE_DIR` vs `BASE_DIR` path handling intact.

### Testing tips for Claude

- To restart the server cleanly, ALWAYS `taskkill //F //IM python.exe` first —
  stale background servers on :5000 will silently serve old code and waste time.
- `samples/` holds large real test clips (CS2 = AV1 1920x1440 4:3, Dota2 = HEVC
  2560x1440). Use short segments for quick tests.
- Verify an encoded output is not corrupt with
  `ffmpeg -v error -i <out> -f null -` (non-empty stderr = corrupt).

## File layout

```
app.py                   # entire Flask backend (single file)
templates/index.html     # single-page UI
static/app.js            # all front-end logic (vanilla JS, IIFE, no framework)
static/styles.css        # dark + light theme (CSS vars; body.light toggles)
static/docs.md           # in-app help content (rendered by the "?" modal)
config.json              # user config (gitignored; contains real Discord secrets)
config.example.json      # safe template
encoder_cache.json       # generated: validated-encoder cache (per ffmpeg build)
jobs_state.json          # generated: pending-job mirror for restart recovery
settings-descriptions.md # long-form settings reference (source for docs.md)
CHANGES.md               # full changelog (Smart Pipeline + Phase 2)
uploads/  output/        # working dirs (created at startup)
ffmpeg/bin/              # bundled ffmpeg + ffprobe
samples/                 # large real test videos
clip_app/                # a separate packaged copy — ignore for dev
launcher.py, *.spec      # PyInstaller packaging — bundles templates/ + static/
```

## Architecture — backend (`app.py`)

**Three-mode pipeline.** `decide_mode()` classifies each clip:
- `PASSTHROUGH` — copy the file untouched (no cut/filters/conversion, fits target).
- `STREAM_COPY_CUT` — lossless `-c copy` keyframe cut, near-instant.
- `FULL_ENCODE` — real re-encode (filters, size target, codec/container change).

Anything that touches pixels (filters, codec/container change, `frameAccurateCut`)
forces FULL_ENCODE before the copy-path size check runs.

**FULL_ENCODE flow** lives in `process_clip()`:
- decision helpers: `pick_resolution()`, `pick_encoder()`, `verify_encoder()`
  (CPU fallback if encoder unusable), `pick_audio_bitrate()`,
  `resolve_audio_codec()`, `resolve_target_fps()`.
- `build_filters()` builds the `-vf` chain: HDR→SDR tone-map → fps → crop/scale
  → denoise → vibrance. Returns `(filters, notes)`.
- `structural_args()` = pix_fmt / GOP / B-frames; `quality_args()` (CRF/CQ) and
  `bitrate_mode_args()` (size-target) take an `encodeSpeed` and call
  `speed_preset()` for the per-encoder `-preset`/`-quality` value.
- `do_encode(enc)` is a nested closure that runs the actual encode (two-pass for
  CPU encoders when target set and clip ≥5s, else single-pass). It is **called
  again with the CPU fallback** if a hardware-encoder output fails the
  post-encode decode-verify (`output_decodes_ok()`); a confirmed-broken encoder
  is recorded via `mark_encoder_broken()`.

**Capability probe.** At startup `probe_encoders()` lists `ffmpeg -encoders` and
runs a real test-encode+decode for each hardware encoder; results cached in
`encoder_cache.json` keyed by the ffmpeg binary fingerprint. `AVAILABLE_ENCODERS`
drives `verify_encoder()` and `GET /api/capabilities`.

**Job system.** `JOBS` dict + `JOBS_LOCK`. Each `/api/process` call spawns a
daemon thread (`run_job`) that acquires `JOB_SEMAPHORE` (concurrency cap), then
processes items. Job dict fields: `status`, `stage`, `progress`, `done`, `total`,
`outputs`, `errors`, `summaries`, `details`, `proc`, `cancel_requested`, `log`,
`created`, `signature`. `_job_view()` strips `proc`/`log` before serialization.
Pending jobs are mirrored to `jobs_state.json` (`persist_pending`) and re-queued
on startup by `restore_jobs()`. Duplicate `/api/process` submits within 5 s
(same `signature`) return the existing job id.

**Probe cache:** `_PROBE_CACHE` keyed by `(filename, mtime, size)`; stores
`is_hdr`, `color_transfer`, `detected_crop` (midpoint `cropdetect` sample).

**Startup:** `startup()` runs `migrate_config_file()`, `probe_encoders()`,
`restore_jobs()` — and is called at import time so the frozen `.exe` works.

## API routes (response shapes are frozen — the frontend depends on them)

- `POST /api/upload` → `{filename, info}`  (`info` includes `is_hdr`, `detected_crop`)
- `POST /api/process` → `{job_id}` (or `{error}` 400 on pre-flight failure)
- `GET  /api/job/<id>` → full job view (incl. `stage`, `details`)
- `POST /api/job/<id>/cancel`, `GET /api/job/<id>/log`
- `GET  /api/job/<id>/thumbnail` → JPG frame from the job's first output
- `GET  /api/capabilities` → `{encoder: bool}` usable-encoder map
- `GET/DELETE /api/output(s)`, `GET /api/preview/<f>`, `DELETE /api/upload/<f>`
- `POST /api/discord`, `GET/POST /api/config`, `GET /api/health`
- docs are served as the static file `/static/docs.md`

Job `status`: `queued`, `running`, `done`, `cancelled`. Frontend stops polling
on `done`/`cancelled`. A fully-errored batch still ends `done` with `errors[]`.
`stage` values: `queued`, `probing`, `pass1`, `pass2`, `single_pass`,
`stream_copy`, `passthrough`, `verifying`, `done`, `cancelled`.

## Config

`DEFAULT_CONFIG` in `app.py` is the source of truth. `migrate_config()`
deep-merges `config.json` over it (old files load clean, new keys default); the
merged result is written back at startup. `api_save_config()` only persists keys
present in `DEFAULT_CONFIG`.

**When adding a config key: add it to `DEFAULT_CONFIG`, the settings form in
`index.html`, both `fillSettingsForm`/`readSettingsForm` in `app.js`, and the
`opts` dict in `api_process()`.**

Current keys: `ffmpegPath`, `hardware`, `codec`, `container`, `targetSizeMb`,
`vibrance`, `stretch`, `discordMode`/`discordToken`/`discordTokenPrefix`/
`discordChannelId`/`discordWebhook`, `frameAccurateCut`, `outputHeight`,
`encoderQualityMode`, `encodeSpeed`, `audioBitrateMode`, `audioCodec`, `twoPass`,
`tenBit`, `maxConcurrentJobs`, `lowBitrateDenoise`, `discordTier`,
`autoRetryOnDiscordReject`, `hwaccelDecode`, `fpsMode`, `fpsCustom`,
`hdrToneMap`, `normalizeAudio`, `denoiseStrength`, `crfMaxrateMultiplier`.

## Gotchas / conventions

- **No non-ASCII in strings that get logged.** Windows console is cp1252;
  `log.info()` of an em-dash etc. throws `UnicodeEncodeError` and breaks jobs.
  Use plain ASCII hyphens in code-generated/log strings.
- **`discordTier` overrides `targetSizeMb`.** Default tier `free` ⇒ effective
  target is always 10 MB unless the tier is changed. CRF mode (`targetSizeMb=0`)
  is effectively unreachable from the UI because of this.
- **`maxConcurrentJobs` is read once at startup** (`JOB_SEMAPHORE` sized at
  import). Changing it needs an app restart.
- **`av1_amf` + `-hwaccel auto` = corrupt bitstream** on some ffmpeg/AMF builds.
  `do_encode()` skips hwaccel specifically for `av1_amf` (software decode). Other
  encoders keep hwaccel. The decode-verify + CPU re-encode is the general
  backstop for any other flaky hardware encoder.
- **SVT-AV1 rejects `-maxrate` outside CRF mode.** `-maxrate`/`-bufsize` are not
  passed to `libsvtav1` in size-target mode (it does VBR from `-b:v` alone).
- **WebM only carries AV1** among this app's codecs. `validate_items()` rejects
  WebM + H.264/HEVC up front.
- **Hardware encoders + 10-bit:** only `TEN_BIT_ENCODERS` (software + NVENC +
  QSV) get `yuv420p10le`; AMF / H.264 silently fall back to 8-bit.
- `encoder_cache.json` / `jobs_state.json` are generated sidecar files — safe to
  delete (they regenerate); delete `encoder_cache.json` to force a re-probe.
- PyInstaller-aware paths: `BUNDLE_DIR` (bundled assets, incl. `static/docs.md`)
  vs `BASE_DIR` (user data). The `.spec` bundles whole `templates/` + `static/`.
- Frontend is one vanilla-JS IIFE; no build step. No CDNs — the markdown for the
  docs modal is rendered by a small built-in renderer so the app works offline.

## Constraints (still in force)

- Don't change existing route paths or JSON response shapes.
- Don't change the `uploads/` / `output/` / `config.json` layout.
- No DB / Redis / Celery / external task queue — the job system is threads only.
- New frontend/Python dependencies are allowed if justified, but none have been
  needed so far (everything runs through stdlib + flask/requests/werkzeug +
  bundled ffmpeg). Document any new dependency in `requirements.txt`.

## Frontend notes

- Settings modal: collapsible `<details>` sections; exposes every config key.
- **Three preset buttons** (`applyPreset()`): Discord (the user's field-tested
  AV1/WebM/10-bit config), Max quality, Fast. Presets fill the form only.
- Topbar: docs `?` modal (renders `static/docs.md`), dark/light theme toggle
  (localStorage), Output drawer, Settings.
- Toasts + optional browser notifications on job finish; **jobs never auto-open
  a tab or steal focus.** Job-detail modal shows thumbnail + log.
- Progress bar shows live `stage` pill + a job `details` line.
- Keyboard: `Ctrl/Cmd+Enter` submits, `Escape` closes modals/drawer.

## Docs to keep in sync

`CHANGES.md` (changelog), `static/docs.md` (in-app help),
`settings-descriptions.md` (settings reference), and this file. When a feature
or config key changes, update all that apply.

## Manual test checklist

Passthrough / stream-copy cut / full-encode size target (auto-downscale,
two-pass) / concurrent jobs / cancellation / pre-flight rejection / hardware
encoder decode-verify + CPU fallback / HDR tone-map / fps modes (auto/manual,
min 30) / cropdetect stretch / capability graying / queue survives restart /
duplicate submit / thumbnail / config migration / docs modal.
