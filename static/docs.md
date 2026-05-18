# Clip Creator — Help & Documentation

Clip Creator cuts, recolors, encodes and uploads gameplay clips to Discord.
This page explains every setting in depth — the hover tooltips are deliberately
short, this is the full reference.

---

## The editor

Clips are edited one at a time in the timeline editor. A clip can keep
**multiple segments** — you mark the parts of the recording to *keep*, and the
gaps between them are dropped. The kept segments are joined, in playback order,
into a single output clip.

- **Timeline** — a filmstrip of the clip. Each keep-segment is a highlighted
  block; the dimmed gaps between blocks are footage that will be removed.
- **Segment edges** — drag the left or right edge of a block to adjust its
  in/out point. The video scrubs live as you drag.
- **Add segment** — the "+ Add segment" button, or double-click an empty part
  of the timeline. Delete a segment with the x on its block.
- **Segment list** — shows each segment's in/out timecode; the numbers are
  editable directly and have frame-step buttons.
- **Loop preview** — plays only the kept segments, back to back, so you can
  check the cut before processing.
- **Precise-cut badge** — shows how the clip will be processed: Passthrough,
  Stream-copy or Full encode.

A single-segment clip that only trims the start/end can still use the fast
stream-copy path. Two or more keep-segments always go through a full encode,
because the trim-and-join needs a real filtergraph.

### Keyboard shortcuts

While a clip is focused in the editor:

- **Space** — play / pause.
- **Left / Right** — step one frame. **Shift+Left / Shift+Right** — step one second.
- **I / O** — set the in / out point of the active segment to the playhead.
- **Ctrl+Enter** — DONE (finish the clip).
- **Esc** — close a modal or drawer.

## Work queue

Upload as many files as you like — they stack up in the **pending strip**. You
edit one clip at a time; clicking **DONE** sends that clip to background
processing and immediately loads the next pending file into the editor.

The DONE button has two modes:

- **Process** — queue the clip for encoding.
- **Process & send** — queue it, then upload the finished file to Discord.

The default mode comes from the **DONE button default** setting; a toggle next
to the button overrides it for the current clip. Background jobs appear in the
**Processing** panel with a live stage and progress bar; finished clips land in
the **Output** drawer. Several clips encode at once, up to the concurrent-jobs
cap.

---

## The Smart Pipeline

Every clip is classified into one of three modes *before* ffmpeg runs. The
pipeline always picks the cheapest mode that still satisfies your settings.

### Passthrough

No cut, no filters, no codec or container change, and the file already fits the
size target. The file is copied byte-for-byte. Completes in well under a second
with zero quality loss.

### Stream-Copy Cut

A cut is needed, but nothing touches the pixels — no filters, no conversion, and
the predicted output fits the target. The encoded bits are copied directly with
`-c copy`. Lossless and near-instant. The only caveat: the cut snaps to the
nearest keyframe, so the start point can land within roughly one to two seconds
of where you asked.

### Full Encode

Everything else: filters (stretch, vibrance), a codec or container change, a
size target that the copy paths can't meet, or frame-accurate cutting. This is a
real re-encode and the only mode where resolution, encoder, audio and HDR
handling are actively tuned.

**Why auto-resolution matters.** Encoding 1440p at a low bitrate produces blocky
output that looks worse than a clean lower-resolution encode. When output height
is `auto`, the pipeline maps the available video bitrate to the highest height
that still encodes cleanly.

**Why frame rate matters.** At a very tight budget, 60 fps spreads bits too
thin. Dropping to 48 or 30 fps gives every remaining frame more bits and looks
visibly smoother. The **Frame rate** setting controls this: `Auto` drops a
high-fps source toward 30 when the budget is tight; `Don't change` keeps the
source rate; or pick a fixed target (60 / 48 / 30 / custom). The output is
never below 30 fps and never upscaled above the source rate. fps is applied
*before* scaling.

---

## Encoder

### Hardware

Selects the encoder family. Hardware encoders — `AMD AMF`, `NVIDIA NVENC`,
`Intel QSV` — are fast and barely touch the CPU. CPU encoders —
`libx264` / `libx265` / `libsvtav1` — are slower but noticeably better at low
bitrates because they can spend more time searching for efficient compression.

Leaving this on your GPU brand is usually correct: the encoder quality mode can
auto-swap to CPU when the bitrate gets too tight for hardware to look good.

If you pick a brand your machine doesn't have, the capability probe detects it
at startup, grays the option out, and the pipeline falls back to the CPU
equivalent automatically (`hevc_amf` becomes `libx265`, and so on).

### Codec

- **AV1** — best quality per megabyte, roughly 30% better than HEVC and 50%
  better than H.264. Ideal for tight targets like Discord clips.
- **HEVC / H.265** — a solid middle ground with broad hardware support.
- **H.264** — the most universally compatible but least efficient. Pick it only
  when you specifically need playback on older devices.

### Container

`MP4` is universally supported and the safe default. `MKV` is more flexible
(more codecs, multiple audio tracks) but not playable everywhere. `WebM` is
web-native and pairs naturally with Opus audio. All three work for Discord.

MP4 output also gets `+faststart`: the `moov` atom is moved to the front of the
file so Discord and browsers can start playback before the full download
finishes.

### Target size (MB)

When set, the tool computes the bitrate needed to fit the clip into this size.
`0` disables size targeting and uses constant-quality (CRF/CQP) encoding —
consistent quality, unpredictable file size. The Discord upload tier overrides
this value when active.

In CRF mode a maxrate ceiling is still applied so a complex 60-second clip can't
balloon to 100 MB+. The ceiling scales with resolution and the
`crfMaxrateMultiplier` setting.

### Encode speed

Trades encoding time against quality by setting the encoder's internal preset.
`Quality` is the slowest and best (the original default). `Balanced` is a solid
middle ground — noticeably faster with only a small quality cost. `Fast` is the
quickest, for when turnaround matters more than squeezing out the last few
percent. This affects encoding speed, not the target file size.

### Presets

The Settings panel has three one-click presets that fill the form for you:

- **Discord** — the field-tested config for Discord clips: AV1 10-bit + Opus in
  a WebM container, two-pass, source resolution, medium low-bitrate denoise,
  auto frame rate, stretch-to-16:9, sized for the free tier with auto-retry on a
  size rejection. The best all-round choice for sharing clips.
- **Max quality** — CPU AV1, two-pass, 10-bit, source resolution and frame
  rate. Slowest, for archival or when quality is everything.
- **Fast** — hardware HEVC, single-pass, fast preset. Quickest turnaround.

Presets only fill the form — review the values and click Save to apply them.
Your hardware brand, ffmpeg path and Discord credentials are never touched.

---

## HDR handling

If the source is HDR (PQ or HLG transfer, or BT.2020 color), the pipeline
detects it during probing.

- With **10-bit output** on HEVC or AV1, the HDR signal is passed through.
- Otherwise the clip is **tone-mapped to SDR** (BT.709) using the Hable
  operator. H.264 always tone-maps because it has no practical HDR path.

Hardware decoding is skipped while tone-mapping, because some GPU decode paths
choke on the linear-light color conversion — software decode is used instead.

---

## Audio

### Why loudness normalization exists

Clips recorded from different games arrive at wildly different volumes. EBU R128
normalization (`loudnorm`, target -16 LUFS) brings every clip to a consistent
loudness so a Discord channel full of clips doesn't swing between whisper-quiet
and ear-splitting. It's a light filter with no meaningful speed cost.

### Audio bitrate

`Auto` scales the audio bitrate with the total budget — 192 kbps for loose
targets down to 64 kbps for very tight ones — so audio doesn't steal bits the
video needs far more. On a 10 MB / 60-second Discord clip, fixed 128 kbps audio
would eat about 13% of the whole budget.

### Why Opus beats AAC at low bitrates

Opus is significantly more efficient than AAC below about 96 kbps and stays
clean on both speech and music at tight budgets. AAC is the universal-
compatibility choice and pairs naturally with MP4. For tight Discord clips with
auto audio bitrate, Opus is the better pick — and the pipeline switches to it
automatically for WebM or whenever the audio budget drops below 96 kbps.

### Volume boost

A manual loudness boost applied on top of normalization. The presets add
+3 dB (Low), +6 dB (Medium) or +10 dB (High); Custom takes an exact decibel
value. The boost is applied *after* loudness normalization, so it can push a
clip above the -16 LUFS normalization target instead of being flattened back to
it. Set it per-clip in the editor, or as a default in Settings. Negative custom
values attenuate instead of boosting.

---

## Smart Pipeline settings

### Encoder quality mode

`Auto` swaps to CPU encoding when the target bitrate is too low for hardware to
look clean — typically below ~2 Mbps for HEVC or ~1.5 Mbps for AV1.
`Hardware always` never swaps (faster, rougher tight clips). `Software always`
always uses the CPU encoder (best quality, slowest).

### Two-pass encoding

The first pass builds a complexity map of the clip; the second spends bits where
they're needed. Hits the target within 1-2% and looks visibly better. Costs
about 1.5-1.8x the encode time. Recommended whenever a size target is set.

### 10-bit output

Stores 1024 levels per color channel instead of 256, eliminating banding in
skies, smoke and dark scenes. Often produces slightly smaller files at the same
quality. Requires HEVC or AV1.

### Low-bitrate denoise

At low bitrates, encoders waste bandwidth preserving sensor noise. A denoise
pass strips it out so the budget goes to real detail. Only active below
1.5 Mbps. Strength is configurable: `light`, `medium`, `strong`.

### Frame-accurate cut

When off, filter-free cuts use lossless stream copy but snap to a keyframe.
When on, every cut is re-encoded for exact-frame precision at the cost of speed.

---

## Discord

### Upload tier

Sets the target size from your Discord plan: Free (10 MB), Basic (50 MB),
Nitro (500 MB). This **overrides** the manual target size field so you never
build a clip Discord will reject.

### Auto-retry on size reject

If Discord refuses an upload as too large, the tool re-encodes at 80% of the
previous target and retries once. Enable it if you'd rather have a slightly
lower-quality clip delivered than a failed upload.

### Mode

`Webhook` posts to a webhook URL you create under Server Settings →
Integrations → Webhooks — simple and needs no account auth. `Token` mode uses a
Discord user or bot token. Most users want Webhook. User tokens technically
violate Discord's Terms of Service, even for personal use.

---

## Housekeeping

### Auto-stretch detection

When you add a clip, the app checks its aspect ratio. A non-16:9 source — for
example 4:3 stretched-resolution gameplay — gets **Stretch to 16:9** enabled by
default; a 16:9 source gets it left off. This is only a starting default: the
per-clip stretch control still overrides it. Black-bar detection itself runs
only when stretch is actually enabled for a clip, so plain uploads stay fast.

### Auto-cleanup of old files

On startup the app deletes files in the `uploads/` and `output/` folders older
than the **Auto-cleanup age** setting (default 14 days; set 0 to disable). Only
plain files are removed, never folders — so the working directories don't grow
without bound between sessions.

---

## Troubleshooting

- **"input file appears incomplete or corrupted"** — the file failed a one-
  second test decode. You probably clipped a recording while OBS or ShadowPlay
  was still writing it. Stop the recording first, then clip.
- **"cannot fit Ns into N MB at acceptable quality"** — the duration is too long
  for the size target. Shorten the clip, raise the target, or pick a lower
  output height.
- **An encoder shows "(not detected)"** — your ffmpeg build or GPU doesn't
  provide it. The pipeline falls back to the CPU equivalent automatically.
- **A clip came out as garbage / glitchy blocks** — a hardware encoder produced
  a corrupt stream (some GPUs list an encoder they can't actually do, e.g.
  `av1_amf` on pre-RDNA3 AMD cards). The app decode-verifies every
  hardware-encoded clip, disables the bad encoder, and re-encodes once on the
  CPU — so this self-corrects after the first affected clip. The same fallback
  also kicks in if a hardware encoder fails the encode outright.
- **stretchTo169 warns about no black bars** — the footage is already 16:9.
  Stretching it would distort the image, so leave the option off for that clip.
- **A job survived an app restart** — pending jobs are mirrored to disk and
  re-queued automatically when the app starts again.
