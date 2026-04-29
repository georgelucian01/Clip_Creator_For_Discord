# Clip Creator

A small local video editor that runs in your browser. Cut, stretch, vibrance,
compress and convert clips with hardware acceleration — then send them
straight to a Discord channel.

Built as a Flask app; ships as a single `ClipCreator.exe` for Windows.

## Features

- **Trim** with a draggable dual-handle timeline
- **Stretch to 16:9** with auto crop-detect (kills letterbox bars)
- **Vibrance** boost via FFmpeg `eq=saturation`
- **Compress to a target size** (e.g. 25 MB for Discord) with a per-codec
  bitrate ceiling so short clips don't get wastefully large
- **Codec / container conversion** — H.264 / HEVC / AV1 in MP4 / MKV / WebM
- **Hardware acceleration** — AMD AMF, NVIDIA NVENC, Intel QSV, or CPU
- **Discord upload** — webhook (recommended) *or* user-token / Bot / Bearer
- **Background jobs** with progress polling — UI never freezes
- **"Process & Send all"** to one-click batch your queue to Discord
- Single-pass FFmpeg pipeline (one subprocess per clip, not three)

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
git clone https://github.com/<you>/<repo>.git
cd <repo>
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

| Key | Default | Notes |
|---|---|---|
| `ffmpegPath` | `ffmpeg/bin` | Folder with `ffmpeg.exe` / `ffprobe.exe`. Empty = system PATH. |
| `hardware` | `cpu` | `cpu` / `amd` / `nvidia` / `intel` |
| `codec` | `h264` | `h264` / `hevc` / `av1` |
| `container` | `mp4` | `mp4` / `mkv` / `webm` |
| `targetSizeMb` | `25` | Hard cap. `0` = quality mode (CRF/CQ). |
| `vibrance` | `1.0` | 1.0 = neutral, 1.2 = +20% saturation |
| `stretch` | `false` | Auto-crop borders & scale to 1920×1080 |
| `discordMode` | `webhook` | `webhook` or `token` |

## Repository layout

```
app.py                — Flask backend, FFmpeg pipeline, job system, Discord
launcher.py           — Entry point for the bundled exe (browser + server)
clip_creator.spec     — PyInstaller spec
templates/index.html  — UI
static/app.js         — front-end logic
static/styles.css     — dark theme
config.example.json   — copy to config.json and edit
requirements.txt      — Flask + requests
```

## License

Personal project — do whatever you want with it.
