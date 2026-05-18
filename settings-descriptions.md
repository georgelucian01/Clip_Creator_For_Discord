# Clip Creator — Settings Reference

For each setting: a short **tooltip** (one line, suitable for hover text) and a longer **description** (suitable for a help panel or docs page).

---

## Encoder

### Hardware
**Tooltip:** Which encoder family to use. Match this to your GPU; CPU works on any machine but is slower.

**Description:** Selects the underlying encoder. Hardware options (AMD AMF, NVIDIA NVENC, Intel QSV) are fast and barely touch your CPU, ideal for normal recording-quality clips. CPU encoding (libx264/libx265/libsvtav1) is slower but produces noticeably better quality at low bitrates. The "Encoder quality mode" setting below can automatically switch to CPU when the bitrate is too tight for hardware to look good, so leaving this on your GPU brand is usually correct.

### Codec
**Tooltip:** Video compression format. AV1 = best quality per MB. HEVC = good balance. H.264 = most compatible.

**Description:** AV1 offers the best quality-per-megabyte (~30% better than HEVC, ~50% better than H.264) and is ideal for tight size targets like Discord clips. HEVC is a solid middle ground with broad hardware support. H.264 is the most universally compatible but least efficient — only pick it if you specifically need playback on older devices.

### Container
**Tooltip:** Output file format wrapper. MP4 = most compatible, MKV = flexible, WEBM = web-native.

**Description:** The container holds the encoded video and audio streams. MP4 is universally supported and the safe default for almost all use cases. MKV supports more codecs and multiple audio tracks but isn't playable on every device. WEBM is web-focused and pairs naturally with Opus audio. For Discord uploads, all three work — MP4 is the most predictable.

### Target size (MB)
**Tooltip:** Maximum output file size in megabytes. Set 0 to disable and use constant quality instead.

**Description:** When set, the tool calculates the bitrate needed to fit your clip into this size and encodes accordingly. The Discord upload tier setting (below) will override this value when active. Setting to 0 disables size targeting and uses constant-quality encoding (CRF/CQP), which gives consistent quality but unpredictable file size — useful for archiving or when you don't have a hard size limit.

### Encode speed
**Tooltip:** Quality vs speed trade-off. Quality is slowest and best; Fast is quickest. Sets the encoder preset.

**Description:** Controls the encoder's internal preset, trading encoding time against quality. "Quality" is the slowest and best (the original default). "Balanced" is a solid middle ground — noticeably faster for a small quality cost. "Fast" is the quickest, for when turnaround matters more than the last few percent of quality. This affects encoding time, not the target file size.

### Hardware-accelerated decoding
**Tooltip:** GPU-decode the input video (-hwaccel auto). Falls back to software silently if unsupported.

**Description:** Uses the GPU to decode the source video before re-encoding (`-hwaccel auto`), which speeds up FULL_ENCODE jobs. If the platform can't hardware-decode a given file it falls back to software automatically. It is skipped on purpose for the AMF AV1 encoder (a known ffmpeg/AMF bug corrupts the output) and while tone-mapping HDR to SDR. Leave on.

### CRF maxrate multiplier
**Tooltip:** CRF mode only: peak-bitrate ceiling multiplier. Stops complex clips ballooning. 1.0-5.0.

**Description:** Only relevant in constant-quality mode (target size 0). CRF keeps quality constant but lets file size float, so a very complex clip can balloon. This multiplier sets a peak-bitrate ceiling (scaled by resolution) that caps the worst case while leaving normal scenes at constant quality. Higher = looser cap. 2.0 is a sensible default.

---

## Smart Pipeline

### Output height
**Tooltip:** Output resolution. Auto picks the best height for your bitrate budget.

**Description:** Auto matches output resolution to the available bitrate — e.g., 720p at 1.5 Mbps, 540p at 1 Mbps. Encoding 1440p at low bitrates produces blocky output that looks worse than a clean lower-resolution encode, so downscaling is almost always the right call for tight targets. "Source" keeps the original resolution unchanged. Manual options (1440/1080/720/540/480) force a specific height regardless of bitrate. Leave on Auto for size-targeted clips.

### Encoder quality mode
**Tooltip:** When to switch from hardware to CPU encoding for better quality.

**Description:** Auto switches to CPU encoding when the target bitrate is too low for hardware encoders to produce clean results — typically below ~2 Mbps for HEVC or ~1.5 Mbps for AV1. CPU encoders pull ahead at low bitrates because they can spend more time finding efficient compression. "Hardware always" never swaps (faster, but tight clips will look rougher). "Software always" always uses CPU (best quality, slowest). Auto is the best default.

### Two-pass encoding
**Tooltip:** Analyze the clip first, then encode for accurate size targeting and better quality distribution.

**Description:** First pass builds a complexity map of the entire clip (which scenes are busy vs static); second pass uses that map to allocate bits precisely where they're needed. Hits the target size within 1–2% (versus 5–15% overshoot single-pass can produce) AND produces visibly better output by spending bits on complex moments instead of static ones. Costs about 1.5–1.8× the encode time. Highly recommended whenever a target size is set — pure upside.

### 10-bit output
**Tooltip:** Higher color precision per pixel. Smoother gradients, less banding. HEVC/AV1 only.

**Description:** Stores each color channel with 1024 levels instead of 256 (8-bit), eliminating visible banding in smooth gradients — particularly noticeable in skies, fog, smoke, dark scenes, and HDR-style lighting. Counterintuitively often produces slightly smaller files at the same quality because the encoder's internal math is more precise. Requires HEVC or AV1. Modern hardware decoders support it; very old devices may not.

### Low-bitrate denoise
**Tooltip:** Cleans up noise before encoding so bits go to real detail. Only active below ~1.5 Mbps.

**Description:** At low bitrates, encoders waste bandwidth trying to preserve random noise (sensor noise, source compression artifacts) instead of real content. A light denoise pass before encoding strips that noise out, letting the encoder focus its budget on edges, textures, and motion. The result is noticeably cleaner output — fewer blocky artifacts, smoother surfaces. Only applies when video bitrate falls below 1.5 Mbps, so it doesn't affect higher-quality encodes.

### Denoise strength
**Tooltip:** Noise-reduction strength used at low bitrates. Stronger = cleaner but softer.

**Description:** Sets how aggressively low-bitrate denoise cleans the picture (`light` / `medium` / `strong`, mapping to `hqdn3d` parameters). Stronger settings remove more noise and compression grain — giving the encoder more budget for real detail — at the cost of a softer image. Only takes effect when Low-bitrate denoise is on and video bitrate is below 1.5 Mbps.

### Frame rate
**Tooltip:** Output frame rate. Auto drops a 60fps source toward 30 when the bitrate is tight. Minimum 30.

**Description:** Controls the output frame rate. "Auto" drops a high-fps source toward 30 fps when the bitrate budget is tight (60 fps spreads bits too thin at low budgets — fewer, better-encoded frames look smoother). "Don't change" keeps the source rate. Fixed options (60 / 48 / 30 / Custom) force a specific target. The output is never below 30 fps and never upscaled above the source rate. The fps filter is applied before scaling.

### HDR → SDR tone mapping
**Tooltip:** Tone-map HDR sources to SDR (BT.709) so colors look right on normal displays.

**Description:** HDR sources (PQ/HLG transfer, BT.2020 color) look washed out and wrong on standard displays if encoded as-is. When this is on, an HDR source is detected during probing and tone-mapped to SDR BT.709 with the Hable operator — unless 10-bit output is enabled on HEVC/AV1, in which case the HDR signal is passed through instead. H.264 always tone-maps. Leave on unless you specifically want untouched HDR.

### Frame-accurate cut
**Tooltip:** Cut at exactly the requested frame. Forces re-encoding, disables the lossless fast path.

**Description:** When OFF, cuts on clips with no filters and no size target use stream copy — copying encoded bits directly without re-encoding. This is lossless and near-instant, but the cut snaps to the nearest keyframe (typically within ±1–2 seconds of the requested point). When ON, all cuts go through full re-encoding for exact-frame precision, at the cost of speed and one generation of re-encoding. For size-targeted clips this setting has no effect (re-encode happens regardless). Leave OFF unless you specifically need precision cutting on a clip that wouldn't otherwise be re-encoded.

---

## Audio

### Audio bitrate
**Tooltip:** Audio quality. Auto scales with the size budget to free bits for video.

**Description:** Auto adjusts audio bitrate based on the total budget — 192 kbps for loose targets, dropping progressively to 64 kbps for very tight ones — so audio doesn't eat into video's share. On a 10 MB / 60-second Discord clip, fixed 128 kbps audio would take ~13% of your total budget; Auto frees those bits for video. You can override with a fixed value if you have specific audio-quality requirements.

### Audio codec
**Tooltip:** Audio compression. Opus is far better at low bitrates; AAC is more universally compatible.

**Description:** Opus is significantly more efficient than AAC at low bitrates (under ~96 kbps) and produces clean speech and music even at tight budgets. AAC is the safe universal choice and pairs naturally with MP4 — guaranteed playback everywhere. Opus pairs naturally with MKV/WebM, but modern players including Discord handle Opus-in-MP4 fine. For tight Discord clips with Auto audio bitrate, Opus is the better pick.

### Loudness normalization
**Tooltip:** EBU R128 loudness normalization (-16 LUFS) for consistent volume across clips.

**Description:** Clips recorded from different games arrive at wildly different volumes. EBU R128 normalization (`loudnorm`, target -16 LUFS) brings every clip to a consistent loudness so a channel full of clips doesn't swing between whisper-quiet and ear-splitting. It is a light filter with no meaningful speed cost and only applies to FULL_ENCODE jobs (passthrough/stream-copy can't modify audio).

### Volume boost
**Tooltip:** Boost output loudness on top of normalization. Presets add +3 / +6 / +10 dB; Custom sets an exact dB gain.

**Description:** Applies a fixed gain to the audio *after* loudness normalization, so it can push a clip louder than the -16 LUFS normalization target instead of being flattened back to it. The presets are Low (+3 dB), Medium (+6 dB) and High (+10 dB); Custom takes an exact decibel value (positive boosts, negative attenuates). "None" leaves the level untouched. Like normalization it only applies to FULL_ENCODE jobs, and it can be overridden per-clip from the editor.

---

## Defaults

### Vibrance
**Tooltip:** Color saturation boost (100% = no change). Any value other than 100% forces re-encoding.

**Description:** Multiplies color saturation across the entire clip. 100% leaves the source unchanged and preserves the lossless stream-copy fast path for cut-only operations. Values above 100% boost color intensity (useful for muted-looking gameplay capture); below 100% desaturates. Any deviation from 100% means the clip must be re-encoded even for a simple cut, so leave at 100% by default and adjust per-clip when you actually want a color tweak.

### Stretch to 16:9
**Tooltip:** Auto-crop black borders and stretch to 16:9. For 4:3 stretched-resolution gameplay.

**Description:** Detects and removes black borders, then scales the content to fill a 16:9 frame with 1:1 pixel aspect. Designed for 4:3 gameplay captures (CS-style stretched resolutions) that you want filling the frame on a 16:9 display. On upload the app inspects the source aspect ratio and uses this as a per-clip default — on for non-16:9 sources, off for 16:9 ones — but the per-clip control in the editor still overrides it. Black-bar detection (`cropdetect`) runs lazily, only when stretch is actually enabled for a clip, so plain uploads stay fast. Applied to footage that is genuinely 16:9 it would distort the image, so the app warns when no black bars are found.

### FFmpeg path
**Tooltip:** Folder containing ffmpeg.exe and ffprobe.exe. Relative paths resolve from the program directory.

**Description:** Path to the directory holding the ffmpeg and ffprobe binaries. Defaults to `ffmpeg/bin` (bundled with the program). Set to a different absolute path if you have a system-wide ffmpeg installation you want to use instead. Leave blank to fall back to whatever ffmpeg is on your system PATH.

---

## Jobs

### Max concurrent jobs
**Tooltip:** How many clips can encode in parallel. Lower values give each job more resources. Restart to apply.

**Description:** Limits how many encoding jobs run simultaneously; extras wait in a queue. Hardware encoders (AMF/NVENC) handle 2–3 concurrent jobs comfortably. CPU encoding (which Auto mode switches to for tight clips) is core-hungry and usually performs best at 1–2 concurrent jobs so each gets full CPU access. Higher values don't increase throughput — jobs just fight for the same resources. Requires restart to apply.

### DONE button default
**Tooltip:** Default action for the editor's DONE button. Process queues the encode; Process & send also uploads to Discord.

**Description:** Sets what the editor's DONE button does once you finish a clip. "Process" queues the clip for background encoding and loads the next pending file into the editor. "Process & send" does the same and additionally uploads the finished clip to Discord when the job completes. A toggle next to the DONE button overrides this default for an individual clip.

### Auto-cleanup age (days)
**Tooltip:** On startup, delete uploaded and output files older than this many days. 0 = never.

**Description:** On every startup the app deletes plain files in the `uploads/` and `output/` folders whose last-modified time is older than this many days, so the working directories don't grow without bound across sessions. Only files are removed — never folders. Set to 0 to disable automatic cleanup entirely. Default is 14 days.

---

## Discord

### Upload tier
**Tooltip:** Discord's file-size limit by plan. Overrides the Target size value above.

**Description:** Sets the target size automatically based on your Discord plan: Free (10 MB), Nitro Basic (50 MB), Nitro (500 MB). Overrides the manual "Target size" field to prevent uploading clips that would be rejected. Pick whichever matches your account.

### Auto-retry on size reject
**Tooltip:** If Discord rejects the upload as too large, re-encode at 80% size and try again once.

**Description:** Safety net for when the initial encode overshoots Discord's limit (rare with two-pass enabled, more common with single-pass). If Discord refuses the upload with a size error, the tool re-encodes at 80% of the previous target and retries once. Means the occasional clip will be silently re-encoded at lower quality without warning, so enable only if you'd rather have a delivered clip than a failed upload.

### Mode
**Tooltip:** How to send to Discord. Webhook = simple URL. Token = your user/bot account.

**Description:** Webhook mode posts to a Discord webhook URL you've created in Server Settings → Integrations → Webhooks — simple, no account auth needed beyond the URL. Token mode uses a Discord user or bot token to post directly to a channel — more flexible (works in DMs, lets you edit messages, etc.) but the token must be kept secret. Most users want Webhook.

### Webhook URL *(webhook mode)*
**Tooltip:** Full Discord webhook URL. Create one under Server Settings → Integrations → Webhooks.

**Description:** The complete webhook URL Discord generated for you. Treat this like a password — anyone with this URL can post to that channel. The tool masks it in the UI after saving so it doesn't leak in screenshots.

### Token *(token mode)*
**Tooltip:** Your Discord user or bot authentication token. Never share this.

**Description:** Used to authenticate uploads in token mode. User tokens give full access to your account — never paste yours into untrusted software. Bot tokens come from the Discord Developer Portal and are restricted to what the bot is allowed to do. The tool masks the token in the UI after saving.

### Token prefix *(token mode)*
**Tooltip:** Auth header prefix. Blank for user tokens, "Bot" for bot tokens, "Bearer" for OAuth.

**Description:** Discord's API expects different prefixes depending on token type. User tokens use no prefix. Bot tokens use `Bot`. OAuth2 access tokens use `Bearer`. If unsure, blank works for user tokens and `Bot` works for bot tokens.

### Channel ID *(token mode)*
**Tooltip:** Numeric ID of the target channel. Right-click channel in Discord → Copy ID (requires Developer Mode).

**Description:** The channel where uploads will be posted in token mode. Enable Developer Mode in Discord (User Settings → Advanced → Developer Mode), then right-click any channel and choose "Copy Channel ID." Paste the numeric string here.
