// Clip Creator — front-end
(() => {
  const $  = (s, r = document) => r.querySelector(s);
  const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));

  const state = {
    clips: [],          // {id, file, filename, info, start, end, vibrance, stretch, busyUploading}
    jobId: null,
    poll: null,
    config: null,
  };

  // ---------- toast ----------
  const toastEl = $("#toast");
  let toastTimer;
  function toast(msg, kind = "info") {
    toastEl.textContent = msg;
    toastEl.className = `toast show ${kind}`;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => (toastEl.className = "toast"), 3500);
  }

  // ---------- helpers ----------
  const fmtTime = (s) => {
    s = Math.max(0, Number(s) || 0);
    const m = Math.floor(s / 60);
    const sec = (s - m * 60).toFixed(2).padStart(5, "0");
    return `${m}:${sec}`;
  };
  const fmtSize = (b) => {
    if (!b) return "0 B";
    const u = ["B", "KB", "MB", "GB"];
    let i = 0;
    while (b >= 1024 && i < u.length - 1) { b /= 1024; i++; }
    return `${b.toFixed(b < 10 && i ? 2 : 1)} ${u[i]}`;
  };
  const uid = () => Math.random().toString(36).slice(2, 9);

  // ---------- modals / drawer ----------
  function openModal(id) { $(`#${id}`).classList.add("show"); }
  function closeModal(id) { $(`#${id}`).classList.remove("show"); }
  document.addEventListener("click", (e) => {
    const c = e.target.closest("[data-close]");
    if (c) closeModal(c.dataset.close);
  });
  document.querySelectorAll(".modal").forEach((m) => {
    m.addEventListener("click", (e) => { if (e.target === m) m.classList.remove("show"); });
  });
  $("#btn-settings").onclick = () => openModal("settings-modal");
  $("#btn-outputs").onclick = () => { refreshOutputs(); $("#output-drawer").classList.toggle("show"); };

  $("#btn-delete-all").onclick = async () => {
    const items = await fetch("/api/outputs").then((r) => r.json()).catch(() => []);
    if (!items.length) { toast("Nothing to delete", "info"); return; }
    if (!confirm(`Delete all ${items.length} output clip(s)?`)) return;
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
        start: 0, end: 0,
        vibrance: state.config?.vibrance ?? 1.0,
        stretch: !!state.config?.stretch,
        busyUploading: true, uploadProgress: 0,
      };
      state.clips.push(clip);
      renderClips();
      try {
        const r = await uploadFile(f, (p) => { clip.uploadProgress = p; renderClipProgress(clip); });
        clip.filename = r.filename;
        clip.info = r.info;
        clip.end = r.info.duration || 0;
        clip.busyUploading = false;
        renderClips();
      } catch (err) {
        toast(`Upload failed: ${f.name} — ${err.message}`, "err");
        state.clips = state.clips.filter((c) => c.id !== clip.id);
        renderClips();
      }
    }
    updateProcessBar();
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

  // ---------- clip rendering ----------
  const clipsEl = $("#clips");

  function renderClips() {
    clipsEl.innerHTML = "";
    for (const clip of state.clips) clipsEl.appendChild(buildClipCard(clip));
    updateProcessBar();
  }

  function renderClipProgress(clip) {
    const card = clipsEl.querySelector(`[data-id="${clip.id}"]`);
    if (!card) return;
    const fill = card.querySelector(".upload-fill");
    if (fill) fill.style.width = `${Math.round((clip.uploadProgress || 0) * 100)}%`;
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
    card.innerHTML = `
      <div class="clip-header">
        <div class="clip-name" title="${escapeHtml(clip.file.name)}">${escapeHtml(clip.file.name)}</div>
        <div class="clip-meta">${clip.info.width}×${clip.info.height} · ${clip.info.codec} · ${fmtSize(clip.info.size)} · ${fmtTime(dur)}</div>
        <button class="btn ghost icon clip-remove" title="Remove">✕</button>
      </div>
      <video preload="metadata" src="/api/preview/${encodeURIComponent(clip.filename)}" controls></video>
      <div class="trim">
        <div class="trim-bar">
          <div class="trim-range"></div>
          <div class="trim-handle h-start" data-h="start"></div>
          <div class="trim-handle h-end" data-h="end"></div>
          <div class="trim-cursor"></div>
        </div>
        <div class="trim-times">
          <label>In <input type="text" class="t-in" value="${fmtTime(clip.start)}"></label>
          <span class="muted small clip-duration"></span>
          <label>Out <input type="text" class="t-out" value="${fmtTime(clip.end)}"></label>
        </div>
      </div>
      <div class="clip-controls">
        <label class="ctl">
          <span>Vibrance <small class="vib-label">${Math.round(clip.vibrance*100)}%</small></span>
          <input type="range" class="ctl-vibrance" min="50" max="200" step="5" value="${Math.round(clip.vibrance*100)}">
        </label>
        <label class="ctl check">
          <input type="checkbox" class="ctl-stretch" ${clip.stretch ? "checked" : ""}>
          <span>Stretch to 16:9</span>
        </label>
      </div>
    `;

    const video = card.querySelector("video");
    const bar = card.querySelector(".trim-bar");
    const range = card.querySelector(".trim-range");
    const hStart = card.querySelector(".h-start");
    const hEnd = card.querySelector(".h-end");
    const cursor = card.querySelector(".trim-cursor");
    const tIn = card.querySelector(".t-in");
    const tOut = card.querySelector(".t-out");
    const vibSlider = card.querySelector(".ctl-vibrance");
    const vibLabel = card.querySelector(".vib-label");
    const stretchChk = card.querySelector(".ctl-stretch");
    const durationEl = card.querySelector(".clip-duration");

    const layout = () => {
      const s = clip.start / dur;
      const e = clip.end / dur;
      hStart.style.left = `${s * 100}%`;
      hEnd.style.left = `${e * 100}%`;
      range.style.left = `${s * 100}%`;
      range.style.width = `${Math.max(0, (e - s) * 100)}%`;
      durationEl.textContent = `${fmtTime(clip.end - clip.start)} length`;
    };
    layout();

    const dragHandle = (which) => (ev) => {
      ev.preventDefault();
      const rect = bar.getBoundingClientRect();
      const move = (e) => {
        const x = ((e.touches?.[0]?.clientX ?? e.clientX) - rect.left) / rect.width;
        const t = Math.max(0, Math.min(1, x)) * dur;
        if (which === "start") clip.start = Math.min(t, clip.end - 0.05);
        else clip.end = Math.max(t, clip.start + 0.05);
        tIn.value = fmtTime(clip.start);
        tOut.value = fmtTime(clip.end);
        video.currentTime = which === "start" ? clip.start : clip.end;
        layout();
      };
      const up = () => {
        document.removeEventListener("mousemove", move);
        document.removeEventListener("mouseup", up);
        document.removeEventListener("touchmove", move);
        document.removeEventListener("touchend", up);
      };
      document.addEventListener("mousemove", move);
      document.addEventListener("mouseup", up);
      document.addEventListener("touchmove", move, { passive: false });
      document.addEventListener("touchend", up);
    };
    hStart.addEventListener("mousedown", dragHandle("start"));
    hStart.addEventListener("touchstart", dragHandle("start"));
    hEnd.addEventListener("mousedown", dragHandle("end"));
    hEnd.addEventListener("touchstart", dragHandle("end"));

    bar.addEventListener("click", (e) => {
      if (e.target.classList.contains("trim-handle")) return;
      const rect = bar.getBoundingClientRect();
      const x = (e.clientX - rect.left) / rect.width;
      video.currentTime = Math.max(0, Math.min(1, x)) * dur;
    });

    video.addEventListener("timeupdate", () => {
      cursor.style.left = `${(video.currentTime / dur) * 100}%`;
    });

    const parseTime = (s) => {
      const parts = s.split(":").map(parseFloat);
      if (parts.some(isNaN)) return null;
      if (parts.length === 1) return parts[0];
      if (parts.length === 2) return parts[0] * 60 + parts[1];
      if (parts.length === 3) return parts[0] * 3600 + parts[1] * 60 + parts[2];
      return null;
    };
    tIn.addEventListener("change", () => {
      const t = parseTime(tIn.value);
      if (t == null) { tIn.value = fmtTime(clip.start); return; }
      clip.start = Math.max(0, Math.min(t, clip.end - 0.05));
      tIn.value = fmtTime(clip.start); layout();
    });
    tOut.addEventListener("change", () => {
      const t = parseTime(tOut.value);
      if (t == null) { tOut.value = fmtTime(clip.end); return; }
      clip.end = Math.min(dur, Math.max(t, clip.start + 0.05));
      tOut.value = fmtTime(clip.end); layout();
    });

    vibSlider.addEventListener("input", () => {
      clip.vibrance = vibSlider.value / 100;
      vibLabel.textContent = `${vibSlider.value}%`;
    });
    stretchChk.addEventListener("change", () => { clip.stretch = stretchChk.checked; });

    card.querySelector(".clip-remove").onclick = async () => {
      state.clips = state.clips.filter((c) => c.id !== clip.id);
      if (clip.filename) {
        fetch(`/api/upload/${encodeURIComponent(clip.filename)}`, { method: "DELETE" }).catch(() => {});
      }
      renderClips();
    };

    return card;
  }

  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, (c) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
    })[c]);
  }

  // ---------- process ----------
  const processBar = $("#processbar");
  const processStatus = $("#process-status");
  const processFill = $("#process-fill");
  const btnProcess = $("#btn-process");
  const btnProcessSend = $("#btn-process-send");

  function updateProcessBar() {
    const ready = state.clips.some((c) => !c.busyUploading);
    processBar.classList.toggle("hidden", state.clips.length === 0);
    const busy = !ready || !!state.jobId;
    btnProcess.disabled = busy;
    btnProcessSend.disabled = busy;
    if (!state.jobId) {
      processStatus.textContent = state.clips.length
        ? `${state.clips.length} clip${state.clips.length > 1 ? "s" : ""} queued`
        : "Idle";
      processFill.style.width = "0%";
    }
  }

  async function startProcess(autoSend) {
    const items = state.clips
      .filter((c) => !c.busyUploading)
      .map((c) => ({
        filename: c.filename,
        start: c.start,
        end: c.end,
        opts: { vibrance: c.vibrance, stretch: c.stretch },
      }));
    if (!items.length) return;
    state.autoSend = !!autoSend;
    btnProcess.disabled = true;
    btnProcessSend.disabled = true;
    processStatus.textContent = "Starting…";
    try {
      const r = await fetch("/api/process", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ items }),
      }).then((x) => x.json());
      if (r.error) throw new Error(r.error);
      state.jobId = r.job_id;
      pollJob();
    } catch (e) {
      toast(`Process failed: ${e.message}`, "err");
      btnProcess.disabled = false;
      btnProcessSend.disabled = false;
    }
  }

  btnProcess.onclick = () => startProcess(false);
  btnProcessSend.onclick = () => startProcess(true);

  async function pollJob() {
    if (!state.jobId) return;
    try {
      const j = await fetch(`/api/job/${state.jobId}`).then((r) => r.json());
      processStatus.textContent =
        j.status === "done"
          ? (j.errors.length ? `Done with ${j.errors.length} error(s)` : "Done")
          : `Processing ${j.done}/${j.total}…`;
      processFill.style.width = `${Math.round((j.progress || 0) * 100)}%`;

      if (j.status === "done") {
        clearTimeout(state.poll);
        state.jobId = null;
        if (j.errors.length) toast(j.errors.join("\n"), "err");
        else toast(`Created ${j.outputs.length} clip(s)`, "ok");
        // remove successfully processed clips from server uploads
        for (const c of state.clips) {
          if (c.filename) fetch(`/api/upload/${encodeURIComponent(c.filename)}`, { method: "DELETE" }).catch(()=>{});
        }
        state.clips = [];
        renderClips();
        $("#output-drawer").classList.add("show");
        refreshOutputs();

        if (state.autoSend && j.outputs.length && !j.errors.length) {
          state.autoSend = false;
          await sendBatch(j.outputs);
        }
        return;
      }
      state.poll = setTimeout(pollJob, 600);
    } catch (e) {
      state.poll = setTimeout(pollJob, 1500);
    }
  }

  async function sendBatch(filenames) {
    processStatus.textContent = `Sending 0/${filenames.length} to Discord…`;
    let ok = 0, fail = 0;
    for (let i = 0; i < filenames.length; i++) {
      try {
        const r = await fetch("/api/discord", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ filename: filenames[i], content: "" }),
        }).then((x) => x.json());
        if (r.error) throw new Error(typeof r.error === "string" ? r.error : JSON.stringify(r.error));
        ok++;
      } catch (e) {
        fail++;
        toast(`Send failed for ${filenames[i]}: ${e.message}`, "err");
      }
      processStatus.textContent = `Sending ${i + 1}/${filenames.length} to Discord…`;
    }
    processStatus.textContent = fail
      ? `Sent ${ok}, failed ${fail}` : `Sent all ${ok} to Discord`;
    if (ok && !fail) toast(`Sent ${ok} clip(s) to Discord`, "ok");
  }

  // ---------- output ----------
  async function refreshOutputs() {
    const list = $("#output-list");
    list.innerHTML = `<div class="muted small">Loading…</div>`;
    try {
      const items = await fetch("/api/outputs").then((r) => r.json());
      if (!items.length) {
        list.innerHTML = `<div class="muted small">No clips yet.</div>`; return;
      }
      list.innerHTML = "";
      for (const it of items) {
        const row = document.createElement("div");
        row.className = "out-row";
        row.innerHTML = `
          <video preload="metadata" src="/api/output/${encodeURIComponent(it.filename)}" controls></video>
          <div class="out-meta">
            <div class="out-name" title="${escapeHtml(it.filename)}">${escapeHtml(it.filename)}</div>
            <div class="muted small">${fmtSize(it.size)}</div>
          </div>
          <div class="out-actions">
            <a class="btn ghost" href="/api/output/${encodeURIComponent(it.filename)}" download>Download</a>
            <button class="btn primary" data-send="${escapeHtml(it.filename)}">Send</button>
            <button class="btn ghost icon" data-del="${escapeHtml(it.filename)}" title="Delete">🗑</button>
          </div>
        `;
        list.appendChild(row);
      }
      list.querySelectorAll("[data-send]").forEach((b) => b.onclick = () => promptSend(b.dataset.send));
      list.querySelectorAll("[data-del]").forEach((b) => b.onclick = async () => {
        await fetch(`/api/output/${encodeURIComponent(b.dataset.del)}`, { method: "DELETE" });
        refreshOutputs();
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

  // ---------- settings ----------
  async function loadConfig() {
    state.config = await fetch("/api/config").then((r) => r.json());
    fillSettingsForm(state.config);
  }

  function fillSettingsForm(c) {
    const m = $("#settings-modal");
    m.querySelector("[name=hardware]").value = c.hardware || "cpu";
    m.querySelector("[name=codec]").value = c.codec || "h264";
    m.querySelector("[name=container]").value = c.container || "mp4";
    m.querySelector("[name=targetSizeMb]").value = c.targetSizeMb ?? 25;
    const vib = m.querySelector("[name=vibrance]");
    vib.value = Math.round((c.vibrance ?? 1.0) * 100);
    $("#vibrance-label").textContent = `${vib.value}%`;
    m.querySelector("[name=stretch]").checked = !!c.stretch;
    m.querySelector("[name=ffmpegPath]").value = c.ffmpegPath || "";
    m.querySelector("[name=discordMode]").value = c.discordMode || "webhook";
    m.querySelector("[name=discordWebhook]").value = c.discordWebhook || "";
    m.querySelector("[name=discordChannelId]").value = c.discordChannelId || "";
    m.querySelector("[name=discordTokenPrefix]").value = c.discordTokenPrefix || "";
    m.querySelector("[name=discordToken]").value = c.discordToken ? "••••••••" : "";
    toggleDiscordMode();
  }

  function readSettingsForm() {
    const m = $("#settings-modal");
    return {
      hardware: m.querySelector("[name=hardware]").value,
      codec: m.querySelector("[name=codec]").value,
      container: m.querySelector("[name=container]").value,
      targetSizeMb: Number(m.querySelector("[name=targetSizeMb]").value) || 0,
      vibrance: Number(m.querySelector("[name=vibrance]").value) / 100,
      stretch: m.querySelector("[name=stretch]").checked,
      ffmpegPath: m.querySelector("[name=ffmpegPath]").value,
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

  $("#settings-modal [name=discordMode]").addEventListener("change", toggleDiscordMode);
  $("#settings-modal [name=vibrance]").addEventListener("input", (e) => {
    $("#vibrance-label").textContent = `${e.target.value}%`;
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

  // ---------- init ----------
  loadConfig();
  refreshOutputs();
  updateProcessBar();
})();
