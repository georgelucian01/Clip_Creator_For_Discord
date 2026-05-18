# Clip Creator

A small local video editor that runs in your browser. Cut gameplay clips into
one or more keep-segments, recolor, stretch, compress and convert them — then
send them straight to a Discord channel.

Built as a Flask app; ships as a single `ClipCreator.exe` for Windows.

## Features

- **Multi-segment editor** — mark several keep-ranges on a filmstrip timeline;
  the kept parts are joined into one clip and the gaps are dropped
- **Smart three-mode pipeline** — copies instead of re-encoding whenever it can:
  passthrough, lossless stream-copy cut, or a full re-encode
- **Work queue** — upload many files, edit one at a time; **DONE** pushes each
  clip to background processing and loads the next one
- **Stretch to 16:9** with auto crop-detect — auto-enabled for non-16:9 sources
- **Vibrance** boost and a post-normalization **volume boost**
- **Compress to a target size** (or a Discord tier) with per-codec bitrate
  ceilings, optional two-pass, and auto resolution / encoder / fps tuning
- **Codec / container conversion** — H.264 / HEVC / AV1 in MP4 / MKV / WebM
- **Hardware acceleration** — AMD AMF, NVIDIA NVENC, Intel QSV, or CPU, with a
  startup capability probe and automatic CPU fallback for flaky encoders
- **HDR → SDR tone mapping** and EBU R128 loudness normalization
- **Discord upload** — webhook (recommended) *or* user-token / Bot / Bearer
- **Background jobs** with progress polling, cancellation and restart recovery

## Quick start (.exe)

1. Grab the latest **`ClipCreator.exe`** from the
   [Releases](../../releases) page.
2. Drop it in a folder.
3. Download an FFmpeg build (any recent gpl/full build works) and put it
   next to the exe so you have:

   ```
   YourFolder/
   ├── ClipCreator.exe
   └── ffmpeg/
       └── bin/
           ├── ffmpeg.exe
           └── ffprobe.exe
   ```

   I use [gyan.dev's "release full" build](https://www.gyan.dev/ffmpeg/builds/).

4. Double-click `ClipCreator.exe`. A console window opens, the server
   starts, and your browser opens to `http://127.0.0.1:5000`.

`uploads/`, `output/` and `config.json` are created next to the exe on
first run.

## Run from source

```cmd
git clone https://github.com/georgelucian01/Clip_Creator_For_Discord.git
cd Clip_Creator_For_Discord
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\python app.py
```

Open http://127.0.0.1:5000.

You still need an FFmpeg build in `ffmpeg/bin/` (or set the path in
**Settings → FFmpeg path**).

## Build the .exe yourself

```cmd
.venv\Scripts\pip install pyinstaller
.venv\Scripts\pyinstaller clip_creator.spec --clean --noconfirm
```

Output: `dist/ClipCreator.exe` (~16 MB, console attached).

The spec file bundles `templates/` and `static/` but **not** FFmpeg —
ship that as a sibling folder.

## Using the editor

Clips are edited one at a time on a filmstrip timeline. Each clip keeps one or
more **segments** — the parts of the recording you want to keep; the gaps
between them are removed and the kept parts joined into a single output.

- Drag a segment block's edges to set its in/out point; **+ Add segment** (or
  double-click the timeline) adds another keep-range.
- **Loop preview** plays only the kept parts so you can check the cut.
- **DONE** (`Ctrl+Enter`) sends the clip to background processing and loads the
  next pending file. Its mode — *Process* or *Process & send* — is set per-clip
  or via the **DONE button default** setting.
- Keyboard: `Space` play/pause, `←/→` step a frame, `Shift+←/→` step a second,
  `I` / `O` set the active segment's in/out.

The in-app **?** (Help) modal documents every setting and shortcut in depth.

## Configure Discord

Open **Settings** in the top-right.

### Webhook (recommended — no ToS risk)

In Discord: **Channel → Edit → Integrations → Webhooks → New Webhook → Copy URL**.
Paste it into Settings, mode "Webhook". Done.

### User token / Bearer / Bot

Pick "User token / Bot / Bearer" in the dropdown and paste your token.

> **Heads up:** sending messages with a *user account token* is technically
> a Discord ToS violation ("self-bot"), even for personal use. Use a
> webhook unless you specifically need a real user identity on the message.

The Discord channel ID is the last number in
`https://discord.com/channels/<server>/<channel>`.

## Settings reference

The in-app Help modal and `settings-descriptions.md` cover every option in
detail. A few of the most-used keys:

| Key | Default | Notes |
|---|---|---|
| `ffmpegPath` | `ffmpeg/bin` | Folder with `ffmpeg.exe` / `ffprobe.exe`. Empty = system PATH. |
| `hardware` | `amd` | `cpu` / `amd` / `nvidia` / `intel` |
| `codec` | `hevc` | `h264` / `hevc` / `av1` |
| `container` | `mp4` | `mp4` / `mkv` / `webm` |
| `targetSizeMb` | `25` | Hard cap. `0` = quality mode (CRF/CQ). Discord tier overrides it. |
| `discordTier` | `free` | `free` (10 MB) / `basic` (50 MB) / `nitro` (500 MB) |
| `vibrance` | `1.0` | 1.0 = neutral, 1.2 = +20% saturation |
| `stretch` | `false` | Auto-crop borders & scale to 16:9 (auto-set per clip by aspect ratio) |
| `volumeBoost` | `none` | `none` / `low` / `medium` / `high`, or a dB number |
| `doneAction` | `process` | DONE default: `process` / `process_send` |
| `cleanupDays` | `14` | Delete `uploads/` & `output/` files older than N days (`0` = never) |
| `discordMode` | `webhook` | `webhook` or `token` |

## Run the tests

A dev-only `pytest` suite lives in `tests/` — exhaustive unit tests for the
decision logic plus real-encode integration and API tests.

```cmd
.venv\Scripts\pip install pytest
.venv\Scripts\python -m pytest
```

Tests skip cleanly when FFmpeg is unavailable; hardware-encoder tests run only
for the encoders the startup probe finds usable on this machine.

## Repository layout

```
app.py                — Flask backend, FFmpeg pipeline, job system, Discord
launcher.py           — Entry point for the bundled exe (browser + server)
clip_creator.spec     — PyInstaller spec
templates/index.html  — UI
static/app.js         — front-end logic
static/styles.css     — dark / light theme
static/docs.md        — in-app help content (the ? modal)
settings-descriptions.md — long-form settings reference
CHANGES.md            — changelog
tests/                — pytest suite (dev-only)
config.example.json   — copy to config.json and edit
requirements.txt      — Flask + requests (pytest noted as dev-only)
```

`encoder_cache.json` and `jobs_state.json` are generated sidecar files — safe to
delete (they regenerate; deleting `encoder_cache.json` forces an encoder
re-probe).

## License

Personal project — do whatever you want with it.
