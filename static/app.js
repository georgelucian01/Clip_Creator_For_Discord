// Clip Creator — front-end
(() => {
  const $  = (s, r = document) => r.querySelector(s);
  const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));

  const DISCORD_TIERS = { free: 10, basic: 50, nitro: 500 };
  const CODEC_LABELS_UI = { h264: "H.264", hevc: "HEVC", av1: "AV1" };
  const HW_LABELS_UI = {
    cpu: "CPU", amd: "AMD", nvidia: "NVENC", intel: "QSV",
  };

  const state = {
    // clip: {id, file, filename, info, segments:[{id,start,end}], activeSegId,
    //        vibrance, stretch, fpsOverride, doneMode, busyUploading, _ctl}
    pending: [],        // uploaded clips waiting to be edited
    editing: null,      // the one clip currently in the editor slot
    jobs: [],           // tracked background jobs (processing panel)
    jobPoll: null,
    config: null,
    caps: null,
    outputSort: localStorage.getItem("clipOutputSort") || "newest",
    queueExpanded: localStorage.getItem("clipQueueExpanded") === "1",
    processingExpanded: false,
    confirmResolve: null,
  };

  // ---------- toast ----------
  const toastEl = $("#toast");
  let toastTimer;
  function toast(msg, kind = "info") {
    toastEl.textContent = msg;
    toastEl.className = `toast show ${kind}`;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => (toastEl.className = "toast"), 3800);
  }

  // ---------- browser notifications ----------
  function ensureNotifyPermission() {
    if (!("Notification" in window)) return;
    if (Notification.permission === "default") {
      Notification.requestPermission().catch(() => {});
    }
  }
  function notify(title, body) {
    try {
      if ("Notification" in window && Notification.permission === "granted") {
        new Notification(title, { body });
      }
    } catch { /* ignore */ }
  }

  // ---------- clipboard ----------
  async function copyText(text) {
    if (!text) return false;
    try {
      if (navigator.clipboard && window.isSecureContext) {
        await navigator.clipboard.writeText(text);
        return true;
      }
    } catch { /* fall through */ }
    try {
      const ta = document.createElement("textarea");
      ta.value = text;
      ta.style.position = "fixed";
      ta.style.opacity = "0";
      document.body.appendChild(ta);
      ta.select();
      const ok = document.execCommand("copy");
      document.body.removeChild(ta);
      return ok;
    } catch {
      return false;
    }
  }

  // ---------- helpers ----------
  const fmtTime = (s) => {
    s = Math.max(0, Number(s) || 0);
    const m = Math.floor(s / 60);
    const sec = (s - m * 60).toFixed(2).padStart(5, "0");
    return `${m}:${sec}`;
  };
  const fmtDuration = (sec) => {
    sec = Math.max(0, Math.floor(Number(sec) || 0));
    if (sec < 60) return `${sec}s`;
    const m = Math.floor(sec / 60);
    const s = sec % 60;
    return s ? `${m}m ${s}s` : `${m}m`;
  };
  const fmtSize = (b) => {
    if (!b) return "0 B";
    const u = ["B", "KB", "MB", "GB"];
    let i = 0;
    while (b >= 1024 && i < u.length - 1) { b /= 1024; i++; }
    return `${b.toFixed(b < 10 && i ? 2 : 1)} ${u[i]}`;
  };
  const uid = () => Math.random().toString(36).slice(2, 9);

  // ---------- theme ----------
  function applyTheme(theme) {
    const light = theme === "light";
    document.body.classList.toggle("light", light);
    $("#btn-theme").textContent = light ? "🌙" : "☀";
    $("#btn-theme").title = light ? "Switch to dark mode" : "Switch to light mode";
  }
  $("#btn-theme").onclick = () => {
    const next = document.body.classList.contains("light") ? "dark" : "light";
    localStorage.setItem("clipTheme", next);
    applyTheme(next);
  };
  (() => {
    const saved = localStorage.getItem("clipTheme");
    if (saved) applyTheme(saved);
    else if (window.matchMedia("(prefers-color-scheme: light)").matches) applyTheme("light");
    else applyTheme("dark");
  })();

  // ---------- chrome / layout ----------
  function hasClipsInFlight() {
    return state.pending.length > 0 || !!state.editing ||
      state.pending.some((c) => c.busyUploading);
  }

  function updateChrome() {
    const hasClips = hasClipsInFlight();
    const editing = !!state.editing;
    document.body.classList.toggle("has-clips", hasClips);
    document.body.classList.toggle("is-editing", editing);

    const compact = $("#upload-compact");
    compact.classList.toggle("hidden", !hasClips);
    if (hasClips) {
      const n = state.pending.length + (state.editing ? 1 : 0);
      $("#upload-compact-hint").textContent =
        editing ? `Editing 1 clip${state.pending.length ? `, ${state.pending.length} queued` : ""}` : "";
    }

    const proc = $("#processing");
    const showCompact = editing && state.jobs.length > 0 && !state.processingExpanded;
    proc.classList.toggle("compact", showCompact);
    proc.classList.toggle("expanded-full", state.processingExpanded);
    $("#btn-processing-toggle").classList.toggle("hidden", !editing || !state.jobs.length);
    renderProcessingCompact();
  }

  function playDoneSound() {
    if (localStorage.getItem("clipSoundOff") === "1") return;
    try {
      const ctx = new (window.AudioContext || window.webkitAudioContext)();
      const o = ctx.createOscillator();
      const g = ctx.createGain();
      o.connect(g);
      g.connect(ctx.destination);
      o.frequency.value = 880;
      g.gain.setValueAtTime(0.07, ctx.currentTime);
      g.gain.exponentialRampToValueAtTime(0.001, ctx.currentTime + 0.18);
      o.start();
      o.stop(ctx.currentTime + 0.18);
    } catch { /* ignore */ }
  }

  async function loadHealth() {
    const dot = $("#health-dot");
    try {
      const h = await fetch("/api/health").then((r) => r.json());
      dot.className = `health-dot ${h.ffmpeg ? "ok" : "err"}`;
      dot.title = h.ffmpeg ? `ffmpeg OK` : "ffmpeg not found - check Settings";
    } catch {
      dot.className = "health-dot err";
      dot.title = "Could not reach server";
    }
  }

  function confirmAction(message, title = "Confirm") {
    return new Promise((resolve) => {
      state.confirmResolve = resolve;
      $("#confirm-title").textContent = title;
      $("#confirm-msg").textContent = message;
      openModal("confirm-modal");
    });
  }
  $("#confirm-ok").onclick = () => {
    closeModal("confirm-modal");
    if (state.confirmResolve) state.confirmResolve(true);
    state.confirmResolve = null;
  };
  document.querySelector("#confirm-modal [data-close='confirm-modal']")?.addEventListener("click", () => {
    if (state.confirmResolve) state.confirmResolve(false);
    state.confirmResolve = null;
  });

  $("#btn-shortcuts").onclick = () => openModal("shortcuts-modal");
  $("#btn-add-more").onclick = () => $("#file-input").click();

  $("#btn-queue-expand").onclick = () => {
    state.queueExpanded = !state.queueExpanded;
    localStorage.setItem("clipQueueExpanded", state.queueExpanded ? "1" : "0");
    $("#queue-bar").classList.toggle("expanded", state.queueExpanded);
  };
  if (state.queueExpanded) $("#queue-bar").classList.add("expanded");

  $("#btn-processing-toggle").onclick = () => {
    state.processingExpanded = !state.processingExpanded;
    updateChrome();
  };

  // ---------- modals / drawer ----------
  function openModal(id) { $(`#${id}`).classList.add("show"); }
  function closeModal(id) { $(`#${id}`).classList.remove("show"); }
  document.addEventListener("click", (e) => {
    const c = e.target.closest("[data-close]");
    if (!c) return;
    if (c.dataset.close === "confirm-modal" && state.confirmResolve) {
      state.confirmResolve(false);
      state.confirmResolve = null;
    }
    closeModal(c.dataset.close);
  });
  document.querySelectorAll(".modal").forEach((m) => {
    m.addEventListener("click", (e) => { if (e.target === m) m.classList.remove("show"); });
  });
  $("#btn-settings").onclick = () => { openModal("settings-modal"); updateSettingsUI(); };
  $("#btn-outputs").onclick = () => { refreshOutputs(); $("#output-drawer").classList.toggle("show"); };
  $("#btn-docs").onclick = () => { openDocs(); };

  $("#btn-send-selected").onclick = async () => {
    const names = getSelectedOutputs();
    if (!names.length) { toast("Select clips to send", "info"); return; }
    await sendBatch(names);
  };

  $("#output-sort").value = state.outputSort;
  $("#output-sort").onchange = () => {
    state.outputSort = $("#output-sort").value;
    localStorage.setItem("clipOutputSort", state.outputSort);
    refreshOutputs();
  };

  $("#btn-delete-all").onclick = async () => {
    const items = await fetch("/api/outputs").then((r) => r.json()).catch(() => []);
    if (!items.length) { toast("Nothing to delete", "info"); return; }
    if (!await confirmAction(`Delete all ${items.length} output clip(s)? This cannot be undone.`, "Delete all")) return;
    const r = await fetch("/api/outputs", { method: "DELETE" }).then((x) => x.json()).catch(() => ({}));
    if (r.deleted != null) toast(`Deleted ${r.deleted} clip(s)`, "ok");
    refreshOutputs();
  };

  // ---------- drop zone ----------
  const dz = $("#dropzone");
  const fi = $("#file-input");
  dz.addEventListener("click", () => fi.click());
  dz.addEventListener("dragover", (e) => { e.preventDefault(); dz.classList.add("drag"); });
  dz.addEventListener("dragleave", () => dz.classList.remove("drag"));
  dz.addEventListener("drop", (e) => {
    e.preventDefault(); dz.classList.remove("drag");
    // snapshot to Array — DataTransfer.files is live and clears on drop end
    handleFiles(Array.from(e.dataTransfer.files));
  });
  fi.addEventListener("change", (e) => {
    // CRITICAL: snapshot to Array BEFORE clearing the input. e.target.files is
    // a live FileList — `fi.value = ""` wipes it, and since handleFiles is
    // async, the second iteration would see an empty list (only 1st uploaded).
    const picked = Array.from(e.target.files);
    fi.value = "";
    handleFiles(picked);
  });

  async function handleFiles(files) {
    for (const f of files) {
      if (!f.type.startsWith("video/") && !/\.(mp4|mkv|mov|webm|avi|flv)$/i.test(f.name)) {
        toast(`Skipping non-video: ${f.name}`, "err"); continue;
      }
      const clip = {
        id: uid(), file: f, filename: null, info: null,
        segments: [], activeSegId: null,
        vibrance: state.config?.vibrance ?? 1.0,
        stretch: !!state.config?.stretch,
        fpsOverride: "default",
        volumeOverride: "default",
        doneMode: state.config?.doneAction || "process",
        busyUploading: true, uploadProgress: 0,
      };
      state.pending.push(clip);
      renderQueue();
      try {
        const r = await uploadFile(f, (p) => { clip.uploadProgress = p; renderQueueProgress(clip); });
        clip.filename = r.filename;
        clip.info = r.info;
        // auto-stretch: a non-16:9 source (e.g. 4:3 CS gameplay) defaults to
        // stretch on. A 16:9 source keeps the user's configured default.
        if (r.info && r.info.suggest_stretch) clip.stretch = true;
        const seg0 = { id: uid(), start: 0, end: r.info.duration || 0 };
        clip.segments = [seg0];
        clip.activeSegId = seg0.id;
        clip.busyUploading = false;
        renderQueue();
        loadNextIntoEditor();
      } catch (err) {
        toast(`Upload failed: ${f.name} — ${err.message}`, "err");
        state.pending = state.pending.filter((c) => c.id !== clip.id);
        renderQueue();
      }
    }
  }

  function uploadFile(file, onProgress) {
    return new Promise((resolve, reject) => {
      const xhr = new XMLHttpRequest();
      xhr.open("POST", "/api/upload");
      xhr.upload.onprogress = (e) => e.lengthComputable && onProgress(e.loaded / e.total);
      xhr.onload = () => {
        try {
          const j = JSON.parse(xhr.responseText);
          if (xhr.status >= 200 && xhr.status < 300) resolve(j);
          else reject(new Error(j.error || `HTTP ${xhr.status}`));
        } catch { reject(new Error(`HTTP ${xhr.status}`)); }
      };
      xhr.onerror = () => reject(new Error("network"));
      const fd = new FormData();
      fd.append("file", file);
      xhr.send(fd);
    });
  }

  // ---------- pending strip + editor slot ----------
  const editorSlot = $("#editor-slot");

  // pull the next fully-uploaded pending clip into the (empty) editor slot
  function loadNextIntoEditor() {
    if (!state.editing) {
      const idx = state.pending.findIndex((c) => !c.busyUploading);
      if (idx >= 0) state.editing = state.pending.splice(idx, 1)[0];
    }
    renderQueue();
    renderEditorSlot();
    updateChrome();
  }

  function promotePendingToEditor(id) {
    const clip = state.pending.find((c) => c.id === id);
    if (!clip || clip.busyUploading) return;
    state.pending = state.pending.filter((c) => c.id !== id);
    if (state.editing) state.pending.unshift(state.editing);
    state.editing = clip;
    renderQueue();
    renderEditorSlot();
    updateChrome();
  }

  function discardEditing() {
    const clip = state.editing;
    if (!clip) return;
    if (clip.filename) {
      fetch(`/api/upload/${encodeURIComponent(clip.filename)}`, { method: "DELETE" }).catch(() => {});
    }
    state.editing = null;
    loadNextIntoEditor();
  }

  function discardPending(id) {
    const clip = state.pending.find((c) => c.id === id);
    if (!clip) return;
    state.pending = state.pending.filter((c) => c.id !== id);
    if (clip.filename) {
      fetch(`/api/upload/${encodeURIComponent(clip.filename)}`, { method: "DELETE" }).catch(() => {});
    }
    renderQueue();
    updateChrome();
  }

  function renderEditorSlot() {
    editorSlot.innerHTML = "";
    if (!state.editing) {
      const waiting = state.pending.some((c) => c.busyUploading);
      editorSlot.innerHTML = `<div class="editor-empty muted">${
        waiting ? "Uploading…"
          : state.jobs.length ? "Queue clear - all clips sent to processing."
            : (document.body.classList.contains("has-clips")
              ? "Select a queued clip or add more videos."
              : "Drop videos above to start editing.")
      }</div>`;
      return;
    }
    editorSlot.appendChild(buildClipCard(state.editing));
  }

  function renderQueue() {
    const bar = $("#queue-bar");
    const strip = $("#queue-strip");
    bar.classList.toggle("hidden", state.pending.length === 0);
    $("#queue-count").textContent = String(state.pending.length);
    strip.innerHTML = "";
    for (const clip of state.pending) {
      const el = document.createElement("div");
      el.className = "queue-item";
      el.dataset.id = clip.id;
      el.tabIndex = clip.busyUploading ? -1 : 0;
      el.title = clip.file?.name || "";
      if (clip.busyUploading) {
        el.innerHTML = `
          <div class="queue-thumb uploading">
            <div class="bar"><div class="upload-fill fill" style="width:${Math.round((clip.uploadProgress || 0) * 100)}%"></div></div>
          </div>
          <div class="queue-name">${escapeHtml(clip.file.name)}</div>`;
      } else {
        el.innerHTML = `
          <div class="queue-thumb" style="background-image:url(/api/upload-thumb/${encodeURIComponent(clip.filename)})"></div>
          <button type="button" class="queue-del" title="Discard">✕</button>
          <div class="queue-name">${escapeHtml(clip.file.name)}</div>`;
        el.querySelector(".queue-del").onclick = (e) => {
          e.stopPropagation();
          discardPending(clip.id);
        };
        el.onclick = () => promotePendingToEditor(clip.id);
        el.onkeydown = (e) => {
          if (e.key === "Enter" || e.key === " ") {
            e.preventDefault();
            promotePendingToEditor(clip.id);
          }
        };
      }
      strip.appendChild(el);
    }
    updateChrome();
  }

  function renderQueueProgress(clip) {
    const fill = $(`#queue-strip [data-id="${clip.id}"] .upload-fill`);
    if (fill) fill.style.width = `${Math.round((clip.uploadProgress || 0) * 100)}%`;
  }

  const normCodec = (n) => {
    n = String(n || "").toLowerCase();
    if (["h264", "avc", "avc1"].includes(n)) return "h264";
    if (["hevc", "h265"].includes(n)) return "hevc";
    if (n === "av1") return "av1";
    return n;
  };

  // client-side mirror of the backend decide_mode() - drives the cut badge.
  function predictMode(clip) {
    const segs = clip.segments || [];
    if (segs.length > 1) return { label: "Full encode", cls: "full" };
    const s = segs[0] || { start: 0, end: 0 };
    const dur = clip.info?.duration || 0;
    const cfg = state.config || {};
    const cutNeeded = s.start > 0.1 || (dur - s.end) > 0.1;
    const filters = !!clip.stretch || Math.abs((clip.vibrance ?? 1) - 1) > 0.01;
    const codecChange = normCodec(clip.info?.codec) !== (cfg.codec || "h264");
    const srcContainer = (clip.filename || "").split(".").pop().toLowerCase();
    const containerChange = srcContainer !== (cfg.container || "mp4");
    const frameAccurate = !!cfg.frameAccurateCut;
    if (filters || codecChange || containerChange || (frameAccurate && cutNeeded))
      return { label: "Full encode", cls: "full" };
    if (!cutNeeded) return { label: "Passthrough", cls: "pass" };
    return { label: "Stream-copy", cls: "copy" };
  }

  function parseTimecode(s) {
    const parts = String(s).split(":").map(parseFloat);
    if (parts.some(isNaN)) return null;
    if (parts.length === 1) return parts[0];
    if (parts.length === 2) return parts[0] * 60 + parts[1];
    if (parts.length === 3) return parts[0] * 3600 + parts[1] * 60 + parts[2];
    return null;
  }

  function buildClipCard(clip) {
    const card = document.createElement("div");
    card.className = "clip";
    card.dataset.id = clip.id;

    if (clip.busyUploading) {
      card.innerHTML = `
        <div class="clip-uploading">
          <div class="clip-name">${escapeHtml(clip.file.name)}</div>
          <div class="bar"><div class="upload-fill fill"></div></div>
          <div class="muted small">uploading…</div>
        </div>`;
      return card;
    }

    const dur = clip.info?.duration || 0;
    const fps = Math.max(1, clip.info?.fps || 30);
    const frame = 1 / fps;
    const hdr = clip.info?.is_hdr ? ` · <span class="tag-hdr">HDR</span>` : "";
    card.innerHTML = `
      <div class="clip-header">
        <div class="clip-name" title="${escapeHtml(clip.file.name)}">${escapeHtml(clip.file.name)}</div>
        <div class="clip-meta">${clip.info.width}×${clip.info.height} · ${clip.info.codec} · ${clip.info.fps ? Math.round(clip.info.fps) + "fps · " : ""}${fmtSize(clip.info.size)} · ${fmtTime(dur)}${hdr}</div>
        <button class="btn ghost icon clip-remove" title="Remove">✕</button>
      </div>
      <video preload="metadata" src="/api/preview/${encodeURIComponent(clip.filename)}" controls></video>
      <div class="editor">
        <div class="timeline">
          <div class="tl-film"></div>
          <div class="tl-gaps"></div>
          <div class="tl-segs"></div>
          <div class="tl-playhead"></div>
        </div>
        <div class="tl-toolbar">
          <button class="btn ghost small tl-play" title="Play / pause (Space)">▶ Play</button>
          <button class="btn ghost small tl-add" title="Add a keep-segment (or double-click the timeline)">+ Add segment</button>
          <button class="btn ghost small tl-loop" title="Loop-play only the kept segments">▶ Loop preview</button>
          <span class="cut-badge"></span>
          <span class="keep-readout muted small"></span>
        </div>
        <div class="seg-list"></div>
        <div class="muted small tl-hint">Space play/pause · ←/→ step frame · Shift+←/→ step 1s · I/O set in/out of active segment</div>
      </div>
      <div class="clip-controls">
        <label class="ctl">
          <span>Vibrance <small class="vib-label">${Math.round(clip.vibrance*100)}%</small></span>
          <input type="range" class="ctl-vibrance" min="50" max="200" step="5" value="${Math.round(clip.vibrance*100)}">
        </label>
        <label class="ctl fps">
          <span>Frame rate</span>
          <select class="ctl-fps">
            <option value="default">Default (settings)</option>
            <option value="source">Source (unchanged)</option>
            <option value="60">60 fps</option>
            <option value="48">48 fps</option>
            <option value="30">30 fps</option>
          </select>
        </label>
        <label class="ctl vol">
          <span>Volume boost</span>
          <select class="ctl-volume">
            <option value="default">Default (settings)</option>
            <option value="none">None</option>
            <option value="low">Low (+3 dB)</option>
            <option value="medium">Medium (+6 dB)</option>
            <option value="high">High (+10 dB)</option>
          </select>
        </label>
        <label class="ctl check">
          <input type="checkbox" class="ctl-stretch" ${clip.stretch ? "checked" : ""}>
          <span>Stretch to 16:9</span>
        </label>
      </div>
      <div class="done-row">
        <span class="done-hint muted small">DONE queues this clip for processing and loads the next one (Ctrl+Enter)</span>
        <select class="done-mode" title="What DONE does with this clip">
          <option value="process">Process</option>
          <option value="process_send">Process &amp; send</option>
        </select>
        <button class="btn primary done-btn" title="Finish this clip (Ctrl+Enter)">DONE ✓</button>
      </div>
    `;

    const video = card.querySelector("video");
    const timeline = card.querySelector(".timeline");
    const playhead = card.querySelector(".tl-playhead");
    const playBtn = card.querySelector(".tl-play");
    const loopBtn = card.querySelector(".tl-loop");

    // filmstrip background (loaded async; failure is non-fatal)
    const filmImg = new Image();
    filmImg.onload = () => { card.querySelector(".tl-film").style.backgroundImage = `url(${filmImg.src})`; };
    filmImg.src = `/api/filmstrip/${encodeURIComponent(clip.filename)}`;

    const pct = (t) => (dur ? Math.max(0, Math.min(100, (t / dur) * 100)) : 0);
    const sortedSegs = () => [...clip.segments].sort((a, b) => a.start - b.start);

    // clamp + apply one segment edge, keeping segments inside their neighbours
    function setEdge(sid, edge, value) {
      const seg = clip.segments.find((s) => s.id === sid);
      if (!seg) return;
      const segs = sortedSegs();
      const idx = segs.indexOf(seg);
      const prev = segs[idx - 1], next = segs[idx + 1];
      if (edge === "start") {
        seg.start = Math.max(prev ? prev.end : 0, Math.min(value, seg.end - frame));
      } else {
        seg.end = Math.min(next ? next.start : dur, Math.max(value, seg.start + frame));
      }
      try { video.currentTime = edge === "start" ? seg.start : seg.end; } catch {}
    }

    function setActive(sid) { clip.activeSegId = sid; renderEditor(); }

    function deleteSeg(sid) {
      if (clip.segments.length <= 1) { toast("Keep at least one segment", "info"); return; }
      clip.segments = clip.segments.filter((s) => s.id !== sid);
      if (clip.activeSegId === sid) clip.activeSegId = clip.segments[0].id;
      renderEditor();
    }

    function addSegment(center) {
      center = Math.max(0, Math.min(dur, center));
      let lo = 0, hi = dur;
      for (const s of sortedSegs()) {
        if (s.end <= center) lo = Math.max(lo, s.end);
        else if (s.start >= center) { hi = Math.min(hi, s.start); break; }
        else { toast("That spot is already inside a segment", "info"); return; }
      }
      if (hi - lo < 0.5) { toast("No room for a new segment here", "info"); return; }
      const want = Math.min(4, hi - lo);
      let st = center - want / 2, en = center + want / 2;
      if (st < lo) { en += lo - st; st = lo; }
      if (en > hi) { st -= en - hi; en = hi; }
      const seg = { id: uid(), start: Math.max(lo, st), end: Math.min(hi, en) };
      clip.segments.push(seg);
      clip.activeSegId = seg.id;
      renderEditor();
    }

    function addSegmentLargestGap() {
      const segs = sortedSegs();
      const gaps = []; let cur = 0;
      for (const s of segs) { if (s.start > cur) gaps.push([cur, s.start]); cur = Math.max(cur, s.end); }
      if (cur < dur) gaps.push([cur, dur]);
      if (!gaps.length) { toast("Timeline is full", "info"); return; }
      gaps.sort((a, b) => (b[1] - b[0]) - (a[1] - a[0]));
      addSegment((gaps[0][0] + gaps[0][1]) / 2);
    }

    // visual-only refresh (positions, gaps, readout, badge) - used during drag
    function layoutEditor() {
      card.querySelectorAll(".tl-seg").forEach((el) => {
        const s = clip.segments.find((x) => x.id === el.dataset.sid);
        if (!s) return;
        el.style.left = pct(s.start) + "%";
        el.style.width = (pct(s.end) - pct(s.start)) + "%";
      });
      const segs = sortedSegs();
      let g = "", cur = 0;
      for (const s of segs) {
        if (s.start > cur) g += `<div class="tl-gap" style="left:${pct(cur)}%;width:${pct(s.start) - pct(cur)}%"></div>`;
        cur = Math.max(cur, s.end);
      }
      if (cur < dur) g += `<div class="tl-gap" style="left:${pct(cur)}%;width:${100 - pct(cur)}%"></div>`;
      card.querySelector(".tl-gaps").innerHTML = g;
      card.querySelectorAll(".seg-row").forEach((row) => {
        const s = clip.segments.find((x) => x.id === row.dataset.sid);
        if (!s) return;
        const i = row.querySelector(".seg-in"), o = row.querySelector(".seg-out");
        if (document.activeElement !== i) i.value = fmtTime(s.start);
        if (document.activeElement !== o) o.value = fmtTime(s.end);
        row.querySelector(".seg-len").textContent = fmtTime(s.end - s.start);
      });
      const kept = clip.segments.reduce((a, s) => a + (s.end - s.start), 0);
      card.querySelector(".keep-readout").textContent = `keeping ${fmtTime(kept)} of ${fmtTime(dur)}`;
      const pm = predictMode(clip);
      const badge = card.querySelector(".cut-badge");
      badge.textContent = pm.label;
      badge.className = `cut-badge ${pm.cls}`;
    }

    // full rebuild of segment blocks + segment list, then re-bind events
    function renderEditor() {
      card.querySelector(".tl-segs").innerHTML = clip.segments.map((s) => `
        <div class="tl-seg ${s.id === clip.activeSegId ? "active" : ""}" data-sid="${s.id}"
             style="left:${pct(s.start)}%;width:${pct(s.end) - pct(s.start)}%">
          <div class="tl-seg-h tl-seg-h-l"></div>
          <div class="tl-seg-h tl-seg-h-r"></div>
          <button class="tl-seg-del" title="Delete segment">✕</button>
        </div>`).join("");
      card.querySelector(".seg-list").innerHTML = clip.segments.map((s, i) => `
        <div class="seg-row ${s.id === clip.activeSegId ? "active" : ""}" data-sid="${s.id}">
          <span class="seg-tag">${i + 1}</span>
          <span class="seg-fields">
            <label>In
              <button class="seg-step" data-edge="start" data-dir="-1" title="-1 frame">◀</button>
              <input class="seg-in" value="${fmtTime(s.start)}">
              <button class="seg-step" data-edge="start" data-dir="1" title="+1 frame">▶</button>
            </label>
            <label>Out
              <button class="seg-step" data-edge="end" data-dir="-1" title="-1 frame">◀</button>
              <input class="seg-out" value="${fmtTime(s.end)}">
              <button class="seg-step" data-edge="end" data-dir="1" title="+1 frame">▶</button>
            </label>
          </span>
          <span class="seg-len muted small"></span>
          <button class="btn ghost icon seg-del-row" title="Delete segment">✕</button>
        </div>`).join("");
      bindEditorEvents();
      layoutEditor();
    }

    function dragEdge(sid, edge) {
      return (ev) => {
        ev.preventDefault(); ev.stopPropagation();
        setActive(sid);
        const rect = timeline.getBoundingClientRect();
        const move = (e) => {
          const x = ((e.touches?.[0]?.clientX ?? e.clientX) - rect.left) / rect.width;
          setEdge(sid, edge, Math.max(0, Math.min(1, x)) * dur);
          layoutEditor();
        };
        const up = () => {
          document.removeEventListener("mousemove", move);
          document.removeEventListener("mouseup", up);
          document.removeEventListener("touchmove", move);
          document.removeEventListener("touchend", up);
          renderEditor();
        };
        document.addEventListener("mousemove", move);
        document.addEventListener("mouseup", up);
        document.addEventListener("touchmove", move, { passive: false });
        document.addEventListener("touchend", up);
      };
    }

    function bindEditorEvents() {
      card.querySelectorAll(".tl-seg").forEach((el) => {
        const sid = el.dataset.sid;
        el.addEventListener("mousedown", (e) => {
          if (e.target.closest(".tl-seg-h") || e.target.closest(".tl-seg-del")) return;
          setActive(sid);
        });
        el.querySelector(".tl-seg-del").onclick = (e) => { e.stopPropagation(); deleteSeg(sid); };
        const hl = el.querySelector(".tl-seg-h-l"), hr = el.querySelector(".tl-seg-h-r");
        hl.addEventListener("mousedown", dragEdge(sid, "start"));
        hl.addEventListener("touchstart", dragEdge(sid, "start"), { passive: false });
        hr.addEventListener("mousedown", dragEdge(sid, "end"));
        hr.addEventListener("touchstart", dragEdge(sid, "end"), { passive: false });
      });
      card.querySelectorAll(".seg-row").forEach((row) => {
        const sid = row.dataset.sid;
        row.addEventListener("click", (e) => {
          if (e.target.tagName === "INPUT" || e.target.tagName === "BUTTON") return;
          setActive(sid);
        });
        row.querySelector(".seg-in").addEventListener("change", (e) => {
          const t = parseTimecode(e.target.value);
          if (t != null) setEdge(sid, "start", t);
          renderEditor();
        });
        row.querySelector(".seg-out").addEventListener("change", (e) => {
          const t = parseTimecode(e.target.value);
          if (t != null) setEdge(sid, "end", t);
          renderEditor();
        });
        row.querySelectorAll(".seg-step").forEach((b) => {
          b.onclick = () => {
            const s = clip.segments.find((x) => x.id === sid);
            if (!s) return;
            setEdge(sid, b.dataset.edge, s[b.dataset.edge] + Number(b.dataset.dir) * frame);
            renderEditor();
          };
        });
        row.querySelector(".seg-del-row").onclick = () => deleteSeg(sid);
      });
    }

    // timeline seek / add-segment
    timeline.addEventListener("click", (e) => {
      if (e.target.closest(".tl-seg")) return;
      const rect = timeline.getBoundingClientRect();
      try { video.currentTime = Math.max(0, Math.min(dur, ((e.clientX - rect.left) / rect.width) * dur)); } catch {}
    });
    timeline.addEventListener("dblclick", (e) => {
      if (e.target.closest(".tl-seg")) return;
      const rect = timeline.getBoundingClientRect();
      addSegment(((e.clientX - rect.left) / rect.width) * dur);
    });

    // playback + loop-preview
    function syncPlayBtn() {
      playBtn.textContent = video.paused ? "▶ Play" : "⏸ Pause";
    }
    playBtn.onclick = () => { video.paused ? video.play() : video.pause(); };
    video.addEventListener("play", syncPlayBtn);
    video.addEventListener("pause", syncPlayBtn);

    loopBtn.onclick = () => {
      clip._looping = !clip._looping;
      loopBtn.classList.toggle("on", clip._looping);
      loopBtn.textContent = clip._looping ? "⏹ Stop loop" : "▶ Loop preview";
      if (clip._looping) {
        const segs = sortedSegs();
        clip._loopIdx = 0;
        try { video.currentTime = segs[0].start; } catch {}
        video.play();
      }
    };

    video.addEventListener("timeupdate", () => {
      playhead.style.left = pct(video.currentTime) + "%";
      if (clip._looping) {
        const segs = sortedSegs();
        if (!segs.length) return;
        let idx = clip._loopIdx ?? 0;
        const seg = segs[Math.min(idx, segs.length - 1)];
        if (video.currentTime >= seg.end - 0.03 || video.currentTime < seg.start - 0.3) {
          idx = (idx + 1) % segs.length;
          clip._loopIdx = idx;
          try { video.currentTime = segs[idx].start; } catch {}
        }
      }
    });

    // keyboard control hooks (consumed by the global keydown handler)
    clip._ctl = {
      playPause: () => { video.paused ? video.play() : video.pause(); },
      step: (frames) => { video.pause(); try { video.currentTime = Math.max(0, Math.min(dur, video.currentTime + frames * frame)); } catch {} },
      stepSec: (d) => { video.pause(); try { video.currentTime = Math.max(0, Math.min(dur, video.currentTime + d)); } catch {} },
      setIn: () => { if (clip.activeSegId) { setEdge(clip.activeSegId, "start", video.currentTime); renderEditor(); } },
      setOut: () => { if (clip.activeSegId) { setEdge(clip.activeSegId, "end", video.currentTime); renderEditor(); } },
    };

    // controls
    card.querySelector(".tl-add").onclick = addSegmentLargestGap;
    const vibSlider = card.querySelector(".ctl-vibrance");
    const vibLabel = card.querySelector(".vib-label");
    vibSlider.addEventListener("input", () => {
      clip.vibrance = vibSlider.value / 100;
      vibLabel.textContent = `${vibSlider.value}%`;
      layoutEditor();
    });
    const fpsSel = card.querySelector(".ctl-fps");
    fpsSel.value = clip.fpsOverride || "default";
    fpsSel.addEventListener("change", () => { clip.fpsOverride = fpsSel.value; });
    const volSel = card.querySelector(".ctl-volume");
    volSel.value = clip.volumeOverride || "default";
    volSel.addEventListener("change", () => { clip.volumeOverride = volSel.value; });
    const stretchChk = card.querySelector(".ctl-stretch");
    stretchChk.addEventListener("change", () => { clip.stretch = stretchChk.checked; layoutEditor(); });

    // DONE row
    const doneSel = card.querySelector(".done-mode");
    doneSel.value = clip.doneMode || "process";
    doneSel.addEventListener("change", () => { clip.doneMode = doneSel.value; });
    card.querySelector(".done-btn").onclick = () => doneClip(clip);

    card.querySelector(".clip-remove").onclick = () => discardEditing();

    renderEditor();
    return card;
  }

  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, (c) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
    })[c]);
  }

  // ---------- jobs / processing panel ----------
  const STAGE_LABELS = {
    queued: "Queued", probing: "Probing", pass1: "Pass 1", pass2: "Pass 2",
    single_pass: "Encoding", stream_copy: "Stream copy", passthrough: "Passthrough",
    verifying: "Verifying", done: "Done", cancelled: "Cancelled", error: "Error",
  };
  const HW_LABELS = {
    cpu: "CPU", amd: "GPU · AMD AMF",
    nvidia: "GPU · NVIDIA NVENC", intel: "GPU · Intel QSV",
  };
  const CODEC_LABELS = { h264: "H.264", hevc: "HEVC", av1: "AV1" };

  function jobDetailsLine(d) {
    if (!d) return "";
    const parts = [
      CODEC_LABELS[d.codec] || d.codec,
      (d.container || "").toUpperCase(),
      HW_LABELS[d.hardware] || d.hardware,
      d.targetMb > 0 ? `${d.targetMb} MB` : "no size cap",
    ];
    if (d.segments > 1) parts.push(`${d.segments} segments`);
    return parts.filter(Boolean).join("  ·  ");
  }

  // submit the editing clip as one background job, then advance the queue
  async function doneClip(clip) {
    if (!clip || !clip.filename) return;
    if (!clip.segments || !clip.segments.length) {
      toast("Add at least one keep-segment first", "err"); return;
    }
    const btn = $("#editor-slot .done-btn");
    if (btn) { btn.disabled = true; btn.textContent = "Starting…"; }
    const opts = { vibrance: clip.vibrance, stretch: clip.stretch };
    if (clip.fpsOverride && clip.fpsOverride !== "default") opts.fpsMode = clip.fpsOverride;
    if (clip.volumeOverride && clip.volumeOverride !== "default") opts.volumeBoost = clip.volumeOverride;
    const item = {
      filename: clip.filename,
      segments: clip.segments.map((s) => ({ start: s.start, end: s.end })),
      opts,
    };
    ensureNotifyPermission();
    try {
      const r = await fetch("/api/process", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ items: [item] }),
      }).then((x) => x.json());
      if (r.error) throw new Error(r.error);
      state.jobs.unshift({
        id: r.job_id, label: clip.file?.name || clip.filename,
        uploadFilename: clip.filename,
        status: "queued", stage: "queued", progress: 0, done: 0, total: 1,
        outputs: [], errors: [], details: null, summaries: [],
        autoSend: clip.doneMode === "process_send", sent: false,
        startedAt: Date.now(),
      });
      state.editing = null;
      loadNextIntoEditor();
      renderProcessing();
      ensureJobPolling();
    } catch (e) {
      toast(`Could not start processing: ${e.message}`, "err");
      if (btn) { btn.disabled = false; btn.textContent = "DONE ✓"; }
    }
  }

  function ensureJobPolling() {
    if (!state.jobPoll) pollJobs();
  }

  async function pollJobs() {
    state.jobPoll = null;
    const active = state.jobs.filter((j) => j.status !== "done" && j.status !== "cancelled");
    if (!active.length) return;
    await Promise.all(active.map(refreshJob));
    renderProcessing();
    state.jobPoll = setTimeout(pollJobs, 800);
  }

  async function refreshJob(j) {
    try {
      const d = await fetch(`/api/job/${j.id}`).then((r) => r.json());
      if (d.error) return;
      const wasFinished = j.status === "done" || j.status === "cancelled";
      Object.assign(j, {
        status: d.status, stage: d.stage, progress: d.progress || 0,
        done: d.done, total: d.total, outputs: d.outputs || [],
        errors: d.errors || [], details: d.details, summaries: d.summaries || [],
      });
      if (!wasFinished && (d.status === "done" || d.status === "cancelled")) {
        onJobFinished(j);
      }
    } catch { /* transient - retried next tick */ }
  }

  function onJobFinished(j) {
    refreshOutputs();
    if (j.status === "cancelled") { toast(`${j.label}: cancelled`, "info"); return; }
    if (j.errors.length) {
      toast(`${j.label}: failed - ${j.errors.length} error(s)`, "err");
      notify("Clip Creator", `${j.label} failed`);
      return;
    }
    const out = j.outputs[0] || j.label;
    toast(`Clip ready: ${out}`, "ok");
    notify("Clip Creator", `Clip ready: ${out}`);
    playDoneSound();
    // the source upload is no longer needed once the job has finished
    if (j.uploadFilename) {
      fetch(`/api/upload/${encodeURIComponent(j.uploadFilename)}`, { method: "DELETE" }).catch(() => {});
    }
    if (j.autoSend && j.outputs.length && !j.sent) {
      j.sent = true;
      sendBatch(j.outputs);
    }
  }

  async function cancelJob(id) {
    try {
      await fetch(`/api/job/${id}/cancel`, { method: "POST" });
      toast("Cancelling job…", "info");
    } catch {
      toast("Cancel request failed", "err");
    }
  }

  function dismissJob(id) {
    state.jobs = state.jobs.filter((j) => j.id !== id);
    renderProcessing();
    renderEditorSlot();
  }

  function jobEta(j) {
    const p = j.progress || 0;
    if (p < 0.05 || p >= 0.99 || !j.startedAt) return "";
    const elapsed = (Date.now() - j.startedAt) / 1000;
    const rem = elapsed * (1 - p) / p;
    return rem >= 5 ? `~${fmtDuration(rem)} left` : "";
  }

  function jobTimingLine(j) {
    if (!j.startedAt) return "";
    const parts = [`${fmtDuration((Date.now() - j.startedAt) / 1000)} elapsed`];
    const eta = jobEta(j);
    if (eta) parts.push(eta);
    return parts.join(" · ");
  }

  function renderProcessingCompact() {
    const el = $("#processing-compact");
    const active = state.jobs.find((j) => j.status === "running") ||
      state.jobs.find((j) => j.status === "queued");
    if (!active) {
      el.classList.add("hidden");
      el.innerHTML = "";
      return;
    }
    el.classList.remove("hidden");
    const pct = Math.round((active.progress || 0) * 100);
    const stage = STAGE_LABELS[active.stage] || active.stage || "";
    const timing = jobTimingLine(active);
    el.innerHTML = `
      <span class="processing-compact-meta">${escapeHtml(active.label)} · ${escapeHtml(stage)}${timing ? ` · ${escapeHtml(timing)}` : ""}</span>
      <div class="bar"><div class="fill" style="width:${pct}%"></div></div>`;
  }

  function renderProcessing() {
    const sec = $("#processing");
    sec.classList.toggle("hidden", state.jobs.length === 0);
    const encoding = state.jobs.filter((j) => j.status === "running").length;
    const queued = state.jobs.filter((j) => j.status === "queued").length;
    const bits = [];
    if (encoding) bits.push(`${encoding} encoding`);
    if (queued) bits.push(`${queued} queued`);
    $("#processing-summary").textContent = bits.length ? `— ${bits.join(", ")}` : "";
    const list = $("#processing-list");
    list.innerHTML = "";
    for (const j of state.jobs) list.appendChild(buildJobRow(j));
    renderProcessingCompact();
    updateChrome();
  }

  function buildJobRow(j) {
    const row = document.createElement("div");
    row.className = "job-row";
    const finished = j.status === "done" || j.status === "cancelled";
    const failed = j.status === "done" && j.errors.length;
    const stageLabel = failed ? "Error" : (STAGE_LABELS[j.stage] || j.stage || "");
    row.innerHTML = `
      <div class="job-thumb-wrap">
        ${j.outputs.length
          ? `<img class="job-row-thumb" src="/api/job/${j.id}/thumbnail?t=${Date.now()}" alt="">`
          : `<div class="job-row-thumb empty"></div>`}
      </div>
      <div class="job-row-main">
        <div class="job-row-top">
          <span class="job-row-name" title="${escapeHtml(j.label)}">${escapeHtml(j.label)}</span>
          <span class="stage-pill ${failed ? "error" : (j.stage || "")}">${escapeHtml(stageLabel)}</span>
        </div>
        <div class="bar"><div class="fill" style="width:${Math.round((j.progress || 0) * 100)}%"></div></div>
        <div class="job-row-meta muted small">${escapeHtml(jobDetailsLine(j.details))}</div>
        <div class="job-row-timing muted small">${escapeHtml(jobTimingLine(j))}</div>
        ${j.errors.length ? `<div class="job-row-err">${escapeHtml(j.errors[0])}</div>` : ""}
      </div>
      <div class="job-row-actions"></div>
    `;
    const actions = row.querySelector(".job-row-actions");
    const mkBtn = (label, cls, fn) => {
      const b = document.createElement("button");
      b.className = `btn ${cls} small`;
      b.textContent = label;
      b.onclick = fn;
      return b;
    };
    actions.appendChild(mkBtn("Details", "ghost", () => openJobDetail(j.id)));
    if (!finished) {
      actions.appendChild(mkBtn("Cancel", "danger", () => cancelJob(j.id)));
    } else {
      if (j.outputs.length && !failed) {
        actions.appendChild(mkBtn("Re-send", "ghost", () => promptSend(j.outputs[0])));
      }
      actions.appendChild(mkBtn("Dismiss", "ghost", () => dismissJob(j.id)));
    }
    return row;
  }

  async function sendBatch(filenames) {
    let ok = 0, fail = 0;
    for (const fn of filenames) {
      try {
        const r = await fetch("/api/discord", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ filename: fn, content: "" }),
        }).then((x) => x.json());
        if (r.error) throw new Error(typeof r.error === "string" ? r.error : JSON.stringify(r.error));
        ok++;
      } catch (e) {
        fail++;
        toast(`Send failed for ${fn}: ${e.message}`, "err");
      }
    }
    if (ok && !fail) toast(`Sent ${ok} clip(s) to Discord`, "ok");
    else if (ok) toast(`Sent ${ok}, failed ${fail}`, "warn");
  }

  // ---------- job detail modal ----------
  async function openJobDetail(jobId) {
    if (!jobId) return;
    openModal("job-modal");
    const thumb = $("#job-thumb");
    const preview = $("#job-preview");
    const summary = $("#job-summary");
    const logEl = $("#job-log");
    thumb.classList.add("hidden");
    preview.classList.add("hidden");
    preview.removeAttribute("src");
    summary.innerHTML = `<div class="muted small">Loading…</div>`;
    logEl.textContent = "Loading…";
    try {
      const j = await fetch(`/api/job/${jobId}`).then((r) => r.json());
      if (j.error) throw new Error(j.error);
      const rows = [];
      rows.push(`<div class="row">Status: <strong>${escapeHtml(j.status)}</strong> · ${j.done}/${j.total} done</div>`);
      const d = j.details || {};
      rows.push(`<div class="row">${escapeHtml((CODEC_LABELS[d.codec] || d.codec || "") + " / " + (d.container || "").toUpperCase() + " / " + (HW_LABELS[d.hardware] || d.hardware || ""))}</div>`);
      rows.push(`<div class="row">${d.targetMb > 0 ? d.targetMb + " MB target" : "no size cap"}${d.tier ? " · tier " + escapeHtml(d.tier) : ""}</div>`);
      for (const s of (j.summaries || [])) rows.push(`<div class="row">${escapeHtml(s)}</div>`);
      for (const e of (j.errors || [])) rows.push(`<div class="row" style="color:var(--err)">${escapeHtml(e)}</div>`);
      summary.innerHTML = rows.join("");
      if (j.outputs && j.outputs.length) {
        const outFn = j.outputs[0];
        preview.src = `/api/output/${encodeURIComponent(outFn)}`;
        preview.onloadeddata = () => preview.classList.remove("hidden");
        preview.onerror = () => preview.classList.add("hidden");
        thumb.src = `/api/job/${jobId}/thumbnail?t=${Date.now()}`;
        thumb.onload = () => thumb.classList.remove("hidden");
        thumb.onerror = () => thumb.classList.add("hidden");
      }
    } catch (e) {
      summary.innerHTML = `<div class="muted small">Could not load job: ${escapeHtml(e.message)}</div>`;
    }
    try {
      const l = await fetch(`/api/job/${jobId}/log`).then((r) => r.json());
      logEl.textContent = l.log && l.log.trim() ? l.log : "No log output (passthrough / stream-copy job).";
    } catch {
      logEl.textContent = "Could not load log.";
    }
  }

  $("#btn-copy-log").onclick = async () => {
    const ok = await copyText($("#job-log").textContent);
    toast(ok ? "Log copied to clipboard" : "Copy failed", ok ? "ok" : "err");
  };

  function discordLimitBytes() {
    const tier = state.config?.discordTier || "free";
    const mb = DISCORD_TIERS[tier];
    return mb ? mb * 1024 * 1024 : 0;
  }

  function sortOutputItems(items) {
    const c = [...items];
    const mode = state.outputSort;
    if (mode === "oldest") c.sort((a, b) => a.mtime - b.mtime);
    else if (mode === "name") c.sort((a, b) => a.filename.localeCompare(b.filename));
    else if (mode === "size") c.sort((a, b) => b.size - a.size);
    else c.sort((a, b) => b.mtime - a.mtime);
    return c;
  }

  function getSelectedOutputs() {
    return $$("#output-list .out-check:checked").map((cb) => cb.value);
  }

  async function refreshOutputs() {
    const list = $("#output-list");
    list.innerHTML = `<div class="muted small">Loading…</div>`;
    try {
      const items = sortOutputItems(await fetch("/api/outputs").then((r) => r.json()));
      if (!items.length) {
        list.innerHTML = `<div class="muted small">No clips yet.</div>`; return;
      }
      const limit = discordLimitBytes();
      list.innerHTML = "";
      for (const it of items) {
        const over = limit > 0 && it.size > limit * 0.98;
        const row = document.createElement("div");
        row.className = "out-row has-check" + (over ? " over-limit" : "");
        row.innerHTML = `
          <input type="checkbox" class="out-check" value="${escapeHtml(it.filename)}">
          <video preload="metadata" src="/api/output/${encodeURIComponent(it.filename)}" controls></video>
          <div class="out-meta">
            <div class="out-name" title="${escapeHtml(it.filename)}">${escapeHtml(it.filename)}</div>
            <div class="muted small out-size">${fmtSize(it.size)}${over ? " (over Discord tier)" : ""}</div>
          </div>
          <div class="out-actions">
            <a class="btn ghost" href="/api/output/${encodeURIComponent(it.filename)}" download>Download</a>
            <button type="button" class="btn primary" data-send="${escapeHtml(it.filename)}">Send</button>
            <button type="button" class="btn ghost icon" data-del="${escapeHtml(it.filename)}" title="Delete">Del</button>
          </div>
        `;
        list.appendChild(row);
      }
      list.querySelectorAll("[data-send]").forEach((b) => {
        b.onclick = () => promptSend(b.dataset.send);
      });
      list.querySelectorAll("[data-del]").forEach((b) => {
        b.onclick = async () => {
          if (!await confirmAction(`Delete ${b.dataset.del}?`, "Delete clip")) return;
          await fetch(`/api/output/${encodeURIComponent(b.dataset.del)}`, { method: "DELETE" });
          refreshOutputs();
        };
      });
    } catch (e) {
      list.innerHTML = `<div class="muted small">Failed to load.</div>`;
    }
  }

  // ---------- discord send ----------
  let pendingSend = null;
  function promptSend(filename) {
    pendingSend = filename;
    const mode = state.config?.discordMode || "webhook";
    const target = mode === "webhook"
      ? `via webhook${state.config?.discordWebhook ? "" : " (not configured)"}`
      : `to channel ${state.config?.discordChannelId || "(none)"}`;
    $("#send-target").textContent = `${filename} → Discord ${target}`;
    $("#send-content").value = "";
    openModal("send-modal");
  }
  $("#btn-send-confirm").onclick = async () => {
    if (!pendingSend) return;
    const content = $("#send-content").value;
    const btn = $("#btn-send-confirm");
    btn.disabled = true; btn.textContent = "Sending…";
    try {
      const r = await fetch("/api/discord", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ filename: pendingSend, content }),
      }).then((x) => x.json());
      if (r.error) throw new Error(typeof r.error === "string" ? r.error : JSON.stringify(r.error));
      toast("Sent to Discord", "ok");
      closeModal("send-modal");
    } catch (e) {
      toast(`Discord error: ${e.message}`, "err");
    } finally {
      btn.disabled = false; btn.textContent = "Send";
      pendingSend = null;
    }
  };

  // ---------- docs ----------
  let docsLoaded = false;
  async function openDocs() {
    openModal("docs-modal");
    if (docsLoaded) return;
    const el = $("#docs-content");
    try {
      const md = await fetch("/static/docs.md").then((r) => r.text());
      el.innerHTML = renderMarkdown(md);
      docsLoaded = true;
    } catch (e) {
      el.innerHTML = `<p class="muted">Could not load documentation.</p>`;
    }
  }

  // Minimal markdown renderer — headings, bold, inline code, fenced code,
  // unordered lists, horizontal rules. No external library / CDN (offline app).
  function renderMarkdown(md) {
    const esc = (s) => s.replace(/[&<>]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]));
    const inline = (s) => esc(s)
      .replace(/`([^`]+)`/g, "<code>$1</code>")
      .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
    const lines = md.replace(/\r\n/g, "\n").split("\n");
    let html = "", inCode = false, code = [], listOpen = false, buf = null;
    const flush = () => {
      if (!buf) return;
      html += buf.type === "p" ? `<p>${inline(buf.text)}</p>` : `<li>${inline(buf.text)}</li>`;
      buf = null;
    };
    const closeList = () => { if (listOpen) { html += "</ul>"; listOpen = false; } };
    for (const line of lines) {
      if (line.trim().startsWith("```")) {
        flush();
        if (inCode) { html += `<pre><code>${esc(code.join("\n"))}</code></pre>`; code = []; inCode = false; }
        else { closeList(); inCode = true; }
        continue;
      }
      if (inCode) { code.push(line); continue; }
      const t = line.trim();
      if (t === "") { flush(); closeList(); continue; }
      if (/^#{1,3}\s/.test(t)) {
        flush(); closeList();
        const lvl = t.match(/^#+/)[0].length;
        html += `<h${lvl}>${inline(t.replace(/^#+\s/, ""))}</h${lvl}>`;
        continue;
      }
      if (/^---+$/.test(t)) { flush(); closeList(); html += "<hr>"; continue; }
      if (/^-\s+/.test(t)) {
        flush();
        if (!listOpen) { html += "<ul>"; listOpen = true; }
        buf = { type: "li", text: t.replace(/^-\s+/, "") };
        continue;
      }
      // indented continuation of the current block, else a paragraph
      if (/^\s/.test(line) && buf) { buf.text += " " + t; continue; }
      if (buf && buf.type === "p") { buf.text += " " + t; continue; }
      flush(); closeList();
      buf = { type: "p", text: t };
    }
    flush(); closeList();
    if (inCode) html += `<pre><code>${esc(code.join("\n"))}</code></pre>`;
    return html;
  }

  // ---------- settings ----------
  async function loadConfig() {
    state.config = await fetch("/api/config").then((r) => r.json());
    fillSettingsForm(state.config);
  }

  async function loadCapabilities() {
    try {
      state.caps = await fetch("/api/capabilities").then((r) => r.json());
      applyCapabilities(state.caps);
    } catch { /* non-fatal */ }
  }

  const HW_ENCODERS = {
    amd: ["hevc_amf", "h264_amf", "av1_amf"],
    nvidia: ["hevc_nvenc", "h264_nvenc", "av1_nvenc"],
    intel: ["hevc_qsv", "h264_qsv", "av1_qsv"],
  };
  function applyCapabilities(caps) {
    if (!caps) return;
    const sel = $("#settings-modal [name=hardware]");
    for (const brand of ["amd", "nvidia", "intel"]) {
      const opt = sel.querySelector(`option[value="${brand}"]`);
      if (!opt) continue;
      const have = HW_ENCODERS[brand].some((e) => caps[e]);
      opt.disabled = !have;
      const base = opt.textContent.replace(/ \(not detected\)$/, "");
      opt.textContent = have ? base : `${base} (not detected)`;
    }
  }


  function effectiveTargetMb(vals) {
    const tier = vals.discordTier;
    if (tier && DISCORD_TIERS[tier] != null) return DISCORD_TIERS[tier];
    return Number(vals.targetSizeMb) || 0;
  }

  function validateSettingsVals(vals) {
    const warns = [];
    if (vals.container === "webm" && vals.codec !== "av1") {
      warns.push("WebM only supports AV1. Switch codec to AV1 or use MP4/MKV.");
    }
    return warns;
  }

  function updateSettingsUI() {
    const m = $("#settings-modal");
    if (!m.classList.contains("show") && !state.config) return;
    const vals = {
      hardware: m.querySelector("[name=hardware]").value,
      codec: m.querySelector("[name=codec]").value,
      container: m.querySelector("[name=container]").value,
      targetSizeMb: Number(m.querySelector("[name=targetSizeMb]").value) || 0,
      discordTier: m.querySelector("[name=discordTier]").value,
      encodeSpeed: m.querySelector("[name=encodeSpeed]").value,
      twoPass: m.querySelector("[name=twoPass]").checked,
    };
    const eff = effectiveTargetMb(vals);
    const codec = CODEC_LABELS_UI[vals.codec] || vals.codec;
    const cont = (vals.container || "").toUpperCase();
    const hw = HW_LABELS_UI[vals.hardware] || vals.hardware;
    const tierNote = DISCORD_TIERS[vals.discordTier]
      ? `Discord ${vals.discordTier} tier overrides size`
      : "";
    $("#settings-summary").innerHTML =
      `<strong>Effective output:</strong> ${codec} · ${cont} · ${hw} · ` +
      (eff > 0 ? `~${eff} MB target` : "no size cap (CRF)") +
      (tierNote ? ` <span class="muted small">(${tierNote})</span>` : "");
    const warns = validateSettingsVals(vals);
    const wEl = $("#settings-warnings");
    if (warns.length) {
      wEl.textContent = warns.join(" ");
      wEl.classList.remove("hidden");
    } else {
      wEl.classList.add("hidden");
      wEl.textContent = "";
    }
  }

  function fillSettingsForm(c) {
    const m = $("#settings-modal");
    m.querySelector("[name=hardware]").value = c.hardware || "cpu";
    m.querySelector("[name=codec]").value = c.codec || "h264";
    m.querySelector("[name=container]").value = c.container || "mp4";
    m.querySelector("[name=targetSizeMb]").value = c.targetSizeMb ?? 25;
    m.querySelector("[name=hwaccelDecode]").checked = c.hwaccelDecode !== false;
    m.querySelector("[name=crfMaxrateMultiplier]").value = c.crfMaxrateMultiplier ?? 2.0;
    const vib = m.querySelector("[name=vibrance]");
    vib.value = Math.round((c.vibrance ?? 1.0) * 100);
    $("#vibrance-label").textContent = `${vib.value}%`;
    m.querySelector("[name=stretch]").checked = !!c.stretch;
    m.querySelector("[name=ffmpegPath]").value = c.ffmpegPath || "";
    // smart pipeline
    m.querySelector("[name=outputHeight]").value = c.outputHeight ?? "auto";
    m.querySelector("[name=encoderQualityMode]").value = c.encoderQualityMode ?? "auto";
    m.querySelector("[name=encodeSpeed]").value = c.encodeSpeed ?? "quality";
    m.querySelector("[name=denoiseStrength]").value = c.denoiseStrength ?? "light";
    m.querySelector("[name=twoPass]").checked = c.twoPass !== false;
    m.querySelector("[name=tenBit]").checked = !!c.tenBit;
    m.querySelector("[name=lowBitrateDenoise]").checked = c.lowBitrateDenoise !== false;
    m.querySelector("[name=fpsMode]").value = c.fpsMode ?? "auto";
    m.querySelector("[name=fpsCustom]").value = c.fpsCustom ?? 48;
    toggleFpsCustom();
    m.querySelector("[name=hdrToneMap]").checked = c.hdrToneMap !== false;
    m.querySelector("[name=frameAccurateCut]").checked = !!c.frameAccurateCut;
    // audio
    m.querySelector("[name=audioBitrateMode]").value = String(c.audioBitrateMode ?? "auto");
    m.querySelector("[name=audioCodec]").value = c.audioCodec ?? "aac";
    m.querySelector("[name=normalizeAudio]").checked = c.normalizeAudio !== false;
    // volume boost: a preset name maps to its option; anything else (a numeric
    // dB value) selects Custom and fills the dB field.
    const vb = c.volumeBoost ?? "none";
    const vbPreset = ["none", "low", "medium", "high"].includes(String(vb));
    m.querySelector("[name=volumeBoost]").value = vbPreset ? String(vb) : "custom";
    m.querySelector("[name=volumeBoostDb]").value = vbPreset ? 6 : Number(vb) || 6;
    toggleVolumeCustom();
    // jobs
    m.querySelector("[name=maxConcurrentJobs]").value = c.maxConcurrentJobs ?? 2;
    m.querySelector("[name=doneAction]").value = c.doneAction ?? "process";
    m.querySelector("[name=cleanupDays]").value = c.cleanupDays ?? 14;
    // discord
    m.querySelector("[name=discordTier]").value = c.discordTier ?? "free";
    m.querySelector("[name=autoRetryOnDiscordReject]").checked = !!c.autoRetryOnDiscordReject;
    m.querySelector("[name=discordMode]").value = c.discordMode || "webhook";
    m.querySelector("[name=discordWebhook]").value = c.discordWebhook || "";
    m.querySelector("[name=discordChannelId]").value = c.discordChannelId || "";
    m.querySelector("[name=discordTokenPrefix]").value = c.discordTokenPrefix || "";
    m.querySelector("[name=discordToken]").value = c.discordToken ? "••••••••" : "";
    toggleDiscordMode();
    if (state.caps) applyCapabilities(state.caps);
    updateSettingsUI();
  }

  function readSettingsForm() {
    const m = $("#settings-modal");
    const abm = m.querySelector("[name=audioBitrateMode]").value;
    return {
      hardware: m.querySelector("[name=hardware]").value,
      codec: m.querySelector("[name=codec]").value,
      container: m.querySelector("[name=container]").value,
      targetSizeMb: Number(m.querySelector("[name=targetSizeMb]").value) || 0,
      hwaccelDecode: m.querySelector("[name=hwaccelDecode]").checked,
      crfMaxrateMultiplier: Number(m.querySelector("[name=crfMaxrateMultiplier]").value) || 2.0,
      vibrance: Number(m.querySelector("[name=vibrance]").value) / 100,
      stretch: m.querySelector("[name=stretch]").checked,
      ffmpegPath: m.querySelector("[name=ffmpegPath]").value,
      outputHeight: m.querySelector("[name=outputHeight]").value,
      encoderQualityMode: m.querySelector("[name=encoderQualityMode]").value,
      encodeSpeed: m.querySelector("[name=encodeSpeed]").value,
      denoiseStrength: m.querySelector("[name=denoiseStrength]").value,
      twoPass: m.querySelector("[name=twoPass]").checked,
      tenBit: m.querySelector("[name=tenBit]").checked,
      lowBitrateDenoise: m.querySelector("[name=lowBitrateDenoise]").checked,
      fpsMode: m.querySelector("[name=fpsMode]").value,
      fpsCustom: Number(m.querySelector("[name=fpsCustom]").value) || 48,
      hdrToneMap: m.querySelector("[name=hdrToneMap]").checked,
      frameAccurateCut: m.querySelector("[name=frameAccurateCut]").checked,
      audioBitrateMode: abm === "auto" ? "auto" : Number(abm),
      audioCodec: m.querySelector("[name=audioCodec]").value,
      normalizeAudio: m.querySelector("[name=normalizeAudio]").checked,
      volumeBoost: m.querySelector("[name=volumeBoost]").value === "custom"
        ? (Number(m.querySelector("[name=volumeBoostDb]").value) || 0)
        : m.querySelector("[name=volumeBoost]").value,
      maxConcurrentJobs: Number(m.querySelector("[name=maxConcurrentJobs]").value) || 2,
      doneAction: m.querySelector("[name=doneAction]").value,
      cleanupDays: Number(m.querySelector("[name=cleanupDays]").value) || 0,
      discordTier: m.querySelector("[name=discordTier]").value,
      autoRetryOnDiscordReject: m.querySelector("[name=autoRetryOnDiscordReject]").checked,
      discordMode: m.querySelector("[name=discordMode]").value,
      discordWebhook: m.querySelector("[name=discordWebhook]").value,
      discordChannelId: m.querySelector("[name=discordChannelId]").value,
      discordTokenPrefix: m.querySelector("[name=discordTokenPrefix]").value,
      discordToken: m.querySelector("[name=discordToken]").value,
    };
  }

  function toggleDiscordMode() {
    const mode = $("#settings-modal [name=discordMode]").value;
    $$(".webhook-only").forEach((el) => (el.style.display = mode === "webhook" ? "" : "none"));
    $$(".token-only").forEach((el) => (el.style.display = mode === "token" ? "" : "none"));
  }

  // the custom-fps field is only relevant when Frame rate is set to Custom
  function toggleFpsCustom() {
    const custom = $("#settings-modal [name=fpsMode]").value === "custom";
    $("#fps-custom-wrap").style.display = custom ? "" : "none";
  }

  // the custom-dB field is only relevant when Volume boost is set to Custom
  function toggleVolumeCustom() {
    const custom = $("#settings-modal [name=volumeBoost]").value === "custom";
    $("#volume-custom-wrap").style.display = custom ? "" : "none";
  }

  $("#settings-modal").addEventListener("input", (e) => {
      if (e.target.matches("select, input")) updateSettingsUI();
    });
    $("#settings-modal").addEventListener("change", (e) => {
      if (e.target.matches("select, input")) updateSettingsUI();
    });
    $("#settings-modal [name=discordMode]").addEventListener("change", toggleDiscordMode);
  $("#settings-modal [name=fpsMode]").addEventListener("change", toggleFpsCustom);
  $("#settings-modal [name=volumeBoost]").addEventListener("change", toggleVolumeCustom);
  $("#settings-modal [name=vibrance]").addEventListener("input", (e) => {
    $("#vibrance-label").textContent = `${e.target.value}%`;
  });

  // copy buttons (webhook / token)
  $$("[data-copy]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const field = $(`#settings-modal [name="${btn.dataset.copy}"]`);
      const ok = await copyText(field ? field.value : "");
      toast(ok ? "Copied to clipboard" : "Nothing to copy", ok ? "ok" : "err");
    });
  });

  // Presets fill the settings form only; the user reviews and Saves. The
  // hardware brand, ffmpeg path and Discord credentials are never touched.
  function applyPreset(name, values) {
    const m = $("#settings-modal");
    for (const [k, v] of Object.entries(values)) {
      const el = m.querySelector(`[name="${k}"]`);
      if (!el) continue;
      if (el.type === "checkbox") el.checked = !!v;
      else el.value = v;
    }
    toggleDiscordMode();
    toggleFpsCustom();
    toggleVolumeCustom();
    $("#vibrance-label").textContent =
      `${m.querySelector("[name=vibrance]").value}%`;
    updateSettingsUI();
    toast(`${name} preset applied — review and Save`, "ok");
  }

  // Discord — the user's hand-tuned, field-tested config for Discord clips:
  // AV1 10-bit + Opus in WebM, two-pass, source resolution, medium denoise,
  // stretch-to-16:9, auto fps. Sized for the free tier with auto-retry.
  $("#btn-preset-discord").onclick = () => applyPreset("Discord", {
    codec: "av1", container: "webm", targetSizeMb: 10,
    outputHeight: "source", encoderQualityMode: "auto", encodeSpeed: "quality",
    twoPass: true, tenBit: true, lowBitrateDenoise: true,
    denoiseStrength: "medium", fpsMode: "auto", fpsCustom: 48,
    hdrToneMap: true, hwaccelDecode: true, normalizeAudio: true,
    crfMaxrateMultiplier: 2, frameAccurateCut: false, stretch: true,
    audioBitrateMode: "auto", audioCodec: "opus",
    discordTier: "free", autoRetryOnDiscordReject: true,
  });

  // Max quality — CPU AV1, two-pass, 10-bit, source resolution & frame rate.
  $("#btn-preset-quality").onclick = () => applyPreset("Max quality", {
    codec: "av1", container: "mp4", outputHeight: "source",
    encoderQualityMode: "software_always", encodeSpeed: "quality",
    twoPass: true, tenBit: true, lowBitrateDenoise: true,
    denoiseStrength: "light", fpsMode: "source", hdrToneMap: true,
    hwaccelDecode: true, normalizeAudio: true, frameAccurateCut: false,
    audioBitrateMode: "192", audioCodec: "opus",
  });

  // Fast — hardware HEVC, single-pass, fast preset; quickest turnaround.
  $("#btn-preset-speed").onclick = () => applyPreset("Fast", {
    codec: "hevc", container: "mp4", outputHeight: "auto",
    encoderQualityMode: "hardware_always", encodeSpeed: "fast",
    twoPass: false, tenBit: false, lowBitrateDenoise: false,
    denoiseStrength: "light", fpsMode: "auto", hdrToneMap: true,
    hwaccelDecode: true, normalizeAudio: true, frameAccurateCut: false,
    audioBitrateMode: "auto", audioCodec: "aac",
  });

  $("#btn-save-settings").onclick = async () => {
    const body = readSettingsForm();
    const r = await fetch("/api/config", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }).then((x) => x.json());
    if (r.ok) {
      toast("Settings saved", "ok");
      closeModal("settings-modal");
      await loadConfig();
    } else {
      toast(`Save failed: ${r.error || "unknown"}`, "err");
    }
  };

  // ---------- keyboard shortcuts ----------
  document.addEventListener("keydown", (e) => {
    // Escape closes any open modal / the output drawer
    if (e.key === "Escape") {
      if (state.confirmResolve) {
        state.confirmResolve(false);
        state.confirmResolve = null;
        closeModal("confirm-modal");
        return;
      }
      let closed = false;
      $(".modal.show").forEach((m) => { m.classList.remove("show"); closed = true; });
      const drawer = $("#output-drawer");
      if (!closed && drawer.classList.contains("show")) drawer.classList.remove("show");
      return;
    }
    // Ctrl/Cmd+Enter = DONE the clip in the editor (blast through a batch)
    if ((e.ctrlKey || e.metaKey) && e.key === "Enter") {
      if ($("#send-modal").classList.contains("show")) {
        e.preventDefault(); $("#btn-send-confirm").click(); return;
      }
      if (state.editing) { e.preventDefault(); doneClip(state.editing); }
      return;
    }

    // editor shortcuts - act on the clip in the editor. Skipped while typing
    // in a field or with a modal open.
    const tag = (e.target.tagName || "").toUpperCase();
    if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") return;
    if ($(".modal.show")) return;
    const fc = state.editing;
    if (!fc || !fc._ctl) return;
    if (e.key === " ") { e.preventDefault(); fc._ctl.playPause(); }
    else if (e.key === "ArrowLeft") { e.preventDefault(); e.shiftKey ? fc._ctl.stepSec(-1) : fc._ctl.step(-1); }
    else if (e.key === "ArrowRight") { e.preventDefault(); e.shiftKey ? fc._ctl.stepSec(1) : fc._ctl.step(1); }
    else if (e.key === "i" || e.key === "I") { e.preventDefault(); fc._ctl.setIn(); }
    else if (e.key === "o" || e.key === "O") { e.preventDefault(); fc._ctl.setOut(); }
  });

  // ---------- init ----------
  loadConfig();
  loadCapabilities();
  refreshOutputs();
  renderQueue();
  renderEditorSlot();
  loadHealth();
  updateChrome();
})();
