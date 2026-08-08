const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];

const state = {
  config: null,
  me: null,
  video: null,
  url: "",
  start: 0,
  end: 0,
  job: null,
  activeVersion: "original",
  pollToken: 0,
  toastTimer: null,
  userId: null,
};

function getOrCreateUserId() {
  let id = localStorage.getItem("ss_user_id");
  if (!id || !/^[a-zA-Z0-9_-]{8,64}$/.test(id)) {
    // generate 32 char url-safe
    const arr = new Uint8Array(16);
    crypto.getRandomValues(arr);
    id = Array.from(arr).map(b => b.toString(36).padStart(2, '0')).join('').slice(0, 32).replace(/[^a-zA-Z0-9_-]/g, 'A');
    if (id.length < 16) id = Math.random().toString(36).slice(2, 18) + Math.random().toString(36).slice(2, 18);
    id = id.slice(0, 32);
    localStorage.setItem("ss_user_id", id);
  }
  // also set as cookie for backend fallback
  document.cookie = `ss_user_id=${id}; Path=/; Max-Age=31536000; SameSite=Lax`;
  state.userId = id;
  return id;
}
getOrCreateUserId();

const els = {
  landing: $("#landingView"), processing: $("#processingView"), result: $("#resultView"),
  inspectForm: $("#inspectForm"), url: $("#youtubeUrl"), clearUrl: $("#clearUrl"), inspectButton: $("#inspectButton"), urlError: $("#urlError"),
  // legacy help
  cookieHelp: $("#cookieHelp"), closeCookieHelp: $("#closeCookieHelp"), cookieForm: $("#cookieForm"), cookieFile: $("#cookieFile"), cookieBrowse: $("#cookieBrowse"), cookieFileName: $("#cookieFileName"), cookieUpload: $("#cookieUpload"), cookieStatus: $("#cookieStatus"),
  // new per-user gate
  userGate: $("#userCookieGate"), closeGate: $("#closeGate"), gateStatus: $("#gateStatus"), gateUserId: $("#gateUserId"), gateTitle: $("#gateTitle"), gateSubtitle: $("#gateSubtitle"),
  myCookieForm: $("#myCookieForm"), myCookieFile: $("#myCookieFile"), myCookieBrowse: $("#myCookieBrowse"), myCookieFileName: $("#myCookieFileName"), myCookieUpload: $("#myCookieUpload"), myCookieStatus: $("#myCookieStatus"),
  shareToggle: $("#shareToggle"), poolInfo: $("#poolInfo"), useCommunityBtn: $("#useCommunityBtn"), communityStatus: $("#communityStatus"),
  engineStatus: $("#engineStatus"), setup: $("#videoSetup"), thumbnail: $("#videoThumbnail"), durationBadge: $("#videoDurationBadge"),
  setupTitle: $("#setupTitle"), channel: $("#videoChannel"), durationText: $("#videoDurationText"), selectedDuration: $("#selectedDuration"),
  controls: $("#timelineControls"), rangeVisual: $("#rangeVisual"), startRange: $("#startRange"), endRange: $("#endRange"),
  startTime: $("#startTime"), endTime: $("#endTime"), timelineError: $("#timelineError"), startButton: $("#startButton"),
  processingTitle: $("#processingTitle"), processingStage: $("#processingStage"), progressBar: $("#progressBar"), progressPercent: $("#progressPercent"),
  processingVideo: $("#processingVideo"), cancelButton: $("#cancelButton"),
  newButton: $("#newTranscriptButton"), resultTitle: $("#resultTitle"), resultMeta: $("#resultMeta"), resultThumbnail: $("#resultThumbnail"),
  audioRange: $("#audioRange"), audioDuration: $("#audioDuration"), audio: $("#audioPlayer"), wordCount: $("#wordCount"),
  segmentCount: $("#segmentCount"), languageList: $("#languageList"), transcript: $("#transcriptContent"), refined: $("#refinedContent"),
  originalTab: $("#originalTab"), refinedTab: $("#refinedTab"), refinedDot: $("#refinedDot"), search: $("#transcriptSearch"), emptySearch: $("#emptySearch"),
  refineBanner: $("#refineBanner"), refineButton: $("#refineButton"), refinementProgress: $("#refinementProgress"),
  copyButton: $("#copyButton"), downloadButton: $("#downloadButton"), downloadMenu: $("#downloadMenu"), toast: $("#toast"),
};

async function api(path, options = {}) {
  const headers = { "X-User-Id": state.userId, ...(options.headers || {}) };
  // Only set JSON content-type if not FormData
  const isForm = options.body instanceof FormData;
  if (!isForm && !headers["Content-Type"]) headers["Content-Type"] = "application/json";
  const response = await fetch(path, { ...options, headers });
  if (!response.ok) {
    let message = `Request failed (${response.status})`;
    try {
      const data = await response.json();
      message = typeof data.detail === "string" ? data.detail : message;
      // attach status for gate logic
      const err = new Error(message);
      err.status = response.status;
      err.detail = data.detail;
      throw err;
    } catch (e) {
      if (e.status) throw e;
      throw new Error(message);
    }
  }
  if (response.status === 204) return null;
  const ct = response.headers.get("content-type") || "";
  if (ct.includes("application/json")) return response.json();
  return response;
}

function showToast(message, isError = false) {
  clearTimeout(state.toastTimer);
  els.toast.classList.toggle("error", isError);
  $("span", els.toast).textContent = message;
  els.toast.classList.add("show");
  state.toastTimer = setTimeout(() => els.toast.classList.remove("show"), 3600);
}

function formatTime(value, forceHours = false) {
  const total = Math.max(0, Math.round(Number(value) || 0));
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const seconds = total % 60;
  if (hours || forceHours) return `${String(hours).padStart(2, "0")}:${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}`;
  return `${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}`;
}

function parseTime(value) {
  const parts = String(value).trim().split(":");
  if (!parts.length || parts.length > 3 || parts.some(part => !/^\d+(?:\.\d+)?$/.test(part))) throw new Error("Use HH:MM:SS or MM:SS.");
  const nums = parts.map(Number);
  let hours = 0, minutes = 0, seconds = 0;
  if (nums.length === 3) [hours, minutes, seconds] = nums;
  else if (nums.length === 2) [minutes, seconds] = nums;
  else seconds = nums[0];
  if (minutes >= 60 || seconds >= 60) throw new Error("Minutes and seconds must be under 60.");
  return hours * 3600 + minutes * 60 + seconds;
}

function prettyDuration(value) {
  const seconds = Math.max(0, Math.round(value));
  if (seconds < 60) return `${seconds} sec`;
  const minutes = Math.floor(seconds / 60);
  const rest = seconds % 60;
  if (minutes < 60) return rest ? `${minutes}m ${rest}s` : `${minutes} min`;
  const hours = Math.floor(minutes / 60);
  const mins = minutes % 60;
  return mins ? `${hours}h ${mins}m` : `${hours} hr`;
}

function setView(view) {
  const appMode = view !== "landing";
  document.body.classList.toggle("app-mode", appMode);
  els.landing.hidden = view !== "landing";
  els.processing.hidden = view !== "processing";
  els.result.hidden = view !== "result";
  window.scrollTo({ top: 0, behavior: "smooth" });
}

// --- Per-user cookie gate logic ---
async function refreshMe() {
  try {
    const res = await fetch(`/api/me`, { headers: { "X-User-Id": state.userId } });
    const data = await res.json();
    if (data.is_new) {
      // keep existing id, but server suggests new
    }
    if (data.status) state.me = data.status;
    else if (data.user_id) {
      // fetch detailed status
      const s = await fetch(`/api/me/cookies/status`, { headers: { "X-User-Id": state.userId } }).then(r=>r.json());
      state.me = s.status;
      state.pool = s.pool;
    }
    if (data.pool) state.pool = data.pool;
    updateGateUI();
    return data;
  } catch (_) {}
}

function updateGateUI() {
  if (!els.userGate || !state.me) return;
  const me = state.me;
  const pool = state.pool || {};
  if (els.gateUserId) els.gateUserId.textContent = `Your ID: ${state.userId}  •  ${me.valid ? "✓ Cookie valid" : me.expired ? "⚠ Cookie expired" : me.has_cookie ? "Cookie saved" : "No cookie yet"}`;
  if (els.shareToggle) els.shareToggle.checked = !!me.share_enabled;
  // pool info
  if (els.poolInfo) {
    if (pool.has_pool) {
      els.poolInfo.className = "pool-info has-pool";
      els.poolInfo.innerHTML = `✅ <b>${pool.shared_valid_count}</b> community cookie(s) available from <b>${pool.total_donors}</b> donor(s). You can use one instantly — the donor's raw cookie is never shown to you.`;
      if (els.useCommunityBtn) els.useCommunityBtn.disabled = false;
    } else {
      els.poolInfo.className = "pool-info";
      els.poolInfo.innerHTML = `No community cookies yet. Be the first to share, or upload your own in the <b>Upload My Cookie</b> tab. <br><small>${pool.total_users_with_cookie || 0} user(s) have cookies, ${pool.total_donors || 0} sharing.</small>`;
      if (els.useCommunityBtn) els.useCommunityBtn.disabled = true;
    }
  }
  // gate status banner
  if (els.gateStatus) {
    if (me.valid) {
      els.gateStatus.className = "gate-status ok";
      els.gateStatus.textContent = `✓ Your cookie is valid — expires ${me.expires_at ? new Date(me.expires_at*1000).toLocaleDateString() : "in ~30 days"}. You're good to transcribe.`;
    } else if (me.expired) {
      els.gateStatus.className = "gate-status err";
      els.gateStatus.textContent = `Your cookie expired. Please re-upload a fresh cookies.txt. Cookies expire every 2–4 weeks.`;
    } else if (!me.has_cookie) {
      if (pool.has_pool) {
        els.gateStatus.className = "gate-status warn";
        els.gateStatus.textContent = `You haven't uploaded a cookie yet. Upload yours, or borrow a community cookie below.`;
      } else {
        els.gateStatus.className = "gate-status warn";
        els.gateStatus.textContent = `First time: you need to upload your cookies.txt once. We'll remember it until it expires.`;
      }
    } else {
      els.gateStatus.className = "gate-status";
      els.gateStatus.textContent = "";
    }
  }
}

function showGate(show, reason) {
  if (!els.userGate) return;
  els.userGate.hidden = !show;
  if (show) {
    refreshMe();
    if (reason) {
      if (els.gateTitle && reason.includes("expired")) {
        els.gateTitle.textContent = "Your cookie expired — re-upload needed";
      } else if (reason.toLowerCase().includes("upload your")) {
        els.gateTitle.textContent = "You need to upload your cookie — one-time setup";
      }
    }
    els.userGate.scrollIntoView({ behavior: "smooth", block: "nearest" });
  }
}

// gate tabs
$$(".gate-tab").forEach(btn => btn.addEventListener("click", () => {
  $$(".gate-tab").forEach(b => b.classList.remove("active"));
  btn.classList.add("active");
  const tab = btn.dataset.tab;
  $$(".gate-panel").forEach(p => p.hidden = p.dataset.panel !== tab);
}));

async function loadConfig() {
  try {
    const res = await fetch(`/api/config`, { headers: { "X-User-Id": state.userId } });
    state.config = await res.json();
    state.me = state.config.me;
    state.pool = state.config.shared_pool;
    updateGateUI();
    const ready = state.config.ready;
    els.engineStatus.classList.add(ready ? "is-ready" : "is-offline");
    $("span", els.engineStatus).textContent = ready ? "AI engine online" : "Setup needed";
    els.engineStatus.title = ready ? `Using ${state.config.engine}` : "Add GROQ_API_KEY to enable transcription";
  } catch (_) {
    els.engineStatus.classList.add("is-offline");
    $("span", els.engineStatus).textContent = "Engine unavailable";
  }
  refreshMe();
}

els.url.addEventListener("input", () => {
  els.clearUrl.hidden = !els.url.value;
  els.urlError.textContent = "";
});
els.clearUrl.addEventListener("click", () => {
  els.url.value = "";
  els.clearUrl.hidden = true;
  els.url.focus();
});

function isCookieNeededError(msg, status) {
  const m = String(msg || "").toLowerCase();
  return status === 428 || m.includes("upload your") || m.includes("bot") || m.includes("cookies") || m.includes("sign in to confirm") || m.includes("verification");
}

els.inspectForm.addEventListener("submit", async event => {
  event.preventDefault();
  const url = els.url.value.trim();
  if (!url || !/(youtube\.com|youtu\.be)/i.test(url)) {
    els.urlError.textContent = "Enter a valid YouTube video link.";
    els.url.focus();
    return;
  }
  els.urlError.textContent = "";
  els.inspectButton.classList.add("is-loading");
  els.inspectButton.disabled = true;
  try {
    const video = await api("/api/videos/inspect", { method: "POST", body: JSON.stringify({ url }) });
    state.video = video;
    state.url = video.webpage_url || url;
    state.start = 0;
    state.end = Number(video.duration);
    populateVideo(video);
    els.setup.hidden = false;
    if (els.userGate) els.userGate.hidden = true;
    if (els.cookieHelp) els.cookieHelp.hidden = true;
    requestAnimationFrame(() => els.setup.scrollIntoView({ behavior: "smooth", block: "start" }));
    refreshMe();
  } catch (error) {
    const msg = error.message || String(error);
    els.urlError.textContent = msg;
    showToast(msg, true);
    if (isCookieNeededError(msg, error.status)) {
      showGate(true, msg);
    }
  } finally {
    els.inspectButton.classList.remove("is-loading");
    els.inspectButton.disabled = false;
  }
});

if (els.closeGate) els.closeGate.addEventListener("click", () => showGate(false));
if (els.closeCookieHelp) els.closeCookieHelp.addEventListener("click", () => { if (els.cookieHelp) els.cookieHelp.hidden = true; });

// --- My cookie upload ---
if (els.myCookieBrowse) els.myCookieBrowse.addEventListener("click", () => els.myCookieFile.click());
if (els.myCookieFile) els.myCookieFile.addEventListener("change", () => {
  const f = els.myCookieFile.files[0];
  if (els.myCookieFileName) els.myCookieFileName.textContent = f ? f.name : "No file chosen";
  if (els.myCookieUpload) els.myCookieUpload.disabled = !f;
  if (els.myCookieStatus) { els.myCookieStatus.textContent = ""; els.myCookieStatus.className = "cookie-status"; }
});
if (els.myCookieForm) els.myCookieForm.addEventListener("submit", async e => {
  e.preventDefault();
  const f = els.myCookieFile.files[0];
  if (!f) return;
  els.myCookieUpload.classList.add("is-loading");
  els.myCookieUpload.disabled = true;
  if (els.myCookieStatus) { els.myCookieStatus.textContent = "Saving…"; els.myCookieStatus.className = "cookie-status"; }
  try {
    const fd = new FormData();
    fd.append("file", f);
    const res = await fetch("/api/me/cookies", { method: "POST", headers: { "X-User-Id": state.userId }, body: fd });
    if (!res.ok) {
      const data = await res.json().catch(() => ({}));
      throw new Error(data.detail || `Upload failed (${res.status})`);
    }
    const data = await res.json();
    state.me = data.status;
    updateGateUI();
    if (els.myCookieStatus) { els.myCookieStatus.textContent = "✓ Saved! Retrying your video…"; els.myCookieStatus.className = "cookie-status ok"; }
    showToast("Cookie saved for your ID. Retrying…");
    els.inspectForm.requestSubmit();
  } catch (err) {
    if (els.myCookieStatus) { els.myCookieStatus.textContent = err.message; els.myCookieStatus.className = "cookie-status err"; }
    showToast(err.message, true);
  } finally {
    els.myCookieUpload.classList.remove("is-loading");
    if (els.myCookieFile && els.myCookieFile.files[0]) els.myCookieUpload.disabled = false;
  }
});

// share toggle
if (els.shareToggle) els.shareToggle.addEventListener("change", async () => {
  const enabled = els.shareToggle.checked;
  try {
    const res = await fetch(`/api/me/cookies/share?enabled=${enabled}`, { method: "POST", headers: { "X-User-Id": state.userId } });
    if (!res.ok) {
      const data = await res.json().catch(() => ({}));
      throw new Error(data.detail || "Could not update sharing");
    }
    const data = await res.json();
    state.me = data.status;
    updateGateUI();
    showToast(enabled ? "Thanks! Others can now use your cookie when you're idle." : "Sharing disabled.");
  } catch (err) {
    showToast(err.message, true);
    els.shareToggle.checked = !enabled;
  }
});

// use community
if (els.useCommunityBtn) els.useCommunityBtn.addEventListener("click", async () => {
  els.useCommunityBtn.classList.add("is-loading");
  els.useCommunityBtn.disabled = true;
  if (els.communityStatus) { els.communityStatus.textContent = "Checking pool…"; els.communityStatus.className = "cookie-status"; }
  try {
    const res = await fetch(`/api/me/cookies/use-shared`, { method: "POST", headers: { "X-User-Id": state.userId } });
    if (!res.ok) {
      const data = await res.json().catch(() => ({}));
      throw new Error(data.detail || `Failed (${res.status})`);
    }
    if (els.communityStatus) { els.communityStatus.textContent = "✓ Community cookie assigned! Retrying…"; els.communityStatus.className = "cookie-status ok"; }
    showToast("Using community cookie. Retrying…");
    els.inspectForm.requestSubmit();
  } catch (err) {
    if (els.communityStatus) { els.communityStatus.textContent = err.message; els.communityStatus.className = "cookie-status err"; }
    showToast(err.message, true);
  } finally {
    els.useCommunityBtn.classList.remove("is-loading");
    els.useCommunityBtn.disabled = false;
  }
});

// legacy cookie help (global fallback) — keep for compatibility
if (els.cookieBrowse) els.cookieBrowse.addEventListener("click", () => els.cookieFile.click());
if (els.cookieFile) els.cookieFile.addEventListener("change", () => {
  const f = els.cookieFile.files[0];
  if (els.cookieFileName) els.cookieFileName.textContent = f ? f.name : "No file chosen";
  if (els.cookieUpload) els.cookieUpload.disabled = !f;
  if (els.cookieStatus) { els.cookieStatus.textContent = ""; els.cookieStatus.className = "cookie-status"; }
});
if (els.cookieForm) els.cookieForm.addEventListener("submit", async e => {
  e.preventDefault();
  const f = els.cookieFile.files[0];
  if (!f) return;
  els.cookieUpload.classList.add("is-loading");
  els.cookieUpload.disabled = true;
  if (els.cookieStatus) { els.cookieStatus.textContent = "Uploading…"; els.cookieStatus.className = "cookie-status"; }
  try {
    const fd = new FormData();
    fd.append("file", f);
    const res = await fetch("/api/cookies", { method: "POST", body: fd });
    if (!res.ok) {
      const data = await res.json().catch(() => ({}));
      throw new Error(data.detail || `Upload failed (${res.status})`);
    }
    if (els.cookieStatus) { els.cookieStatus.textContent = "Cookies saved — retrying…"; els.cookieStatus.className = "cookie-status ok"; }
    showToast("Cookies saved. Retrying…");
    els.inspectForm.requestSubmit();
  } catch (err) {
    if (els.cookieStatus) { els.cookieStatus.textContent = err.message; els.cookieStatus.className = "cookie-status err"; }
    showToast(err.message, true);
  } finally {
    els.cookieUpload.classList.remove("is-loading");
    els.cookieUpload.disabled = !els.cookieFile.files[0];
  }
});

function populateVideo(video) {
  els.thumbnail.src = video.thumbnail || "";
  els.thumbnail.alt = `Thumbnail for ${video.title}`;
  els.durationBadge.textContent = video.duration_label || formatTime(video.duration, true);
  els.setupTitle.textContent = video.title;
  els.channel.textContent = video.channel;
  els.durationText.textContent = prettyDuration(video.duration);
  els.startRange.max = video.duration;
  els.endRange.max = video.duration;
  els.startRange.value = 0;
  els.endRange.value = video.duration;
  els.startTime.value = formatTime(0, video.duration >= 3600);
  els.endTime.value = formatTime(video.duration, video.duration >= 3600);
  const full = $("input[name='rangeMode'][value='full']");
  full.checked = true;
  els.controls.classList.add("is-disabled");
  updateTimeline();
}

$$('input[name="rangeMode"]').forEach(input => input.addEventListener("change", event => {
  const custom = event.target.value === "custom";
  els.controls.classList.toggle("is-disabled", !custom);
  if (!custom && state.video) {
    state.start = 0;
    state.end = Number(state.video.duration);
    updateTimeline();
  }
}));

els.startRange.addEventListener("input", () => {
  const proposed = Number(els.startRange.value);
  state.start = Math.min(proposed, state.end - 1);
  updateTimeline();
});
els.endRange.addEventListener("input", () => {
  const proposed = Number(els.endRange.value);
  state.end = Math.max(proposed, state.start + 1);
  updateTimeline();
});

function applyTypedTime(which) {
  if (!state.video) return;
  try {
    const value = parseTime(which === "start" ? els.startTime.value : els.endTime.value);
    if (which === "start") state.start = Math.max(0, Math.min(value, state.end - 1));
    else state.end = Math.min(Number(state.video.duration), Math.max(value, state.start + 1));
    els.timelineError.textContent = "";
  } catch (error) {
    els.timelineError.textContent = error.message;
  }
  updateTimeline();
}
els.startTime.addEventListener("change", () => applyTypedTime("start"));
els.endTime.addEventListener("change", () => applyTypedTime("end"));
els.startTime.addEventListener("keydown", event => { if (event.key === "Enter") { event.preventDefault(); applyTypedTime("start"); } });
els.endTime.addEventListener("keydown", event => { if (event.key === "Enter") { event.preventDefault(); applyTypedTime("end"); } });

function updateTimeline() {
  if (!state.video) return;
  const duration = Number(state.video.duration);
  state.start = Math.max(0, Math.min(state.start, duration - 1));
  state.end = Math.min(duration, Math.max(state.end, state.start + 1));
  els.startRange.value = state.start;
  els.endRange.value = state.end;
  els.startTime.value = formatTime(state.start, duration >= 3600);
  els.endTime.value = formatTime(state.end, duration >= 3600);
  els.rangeVisual.style.setProperty("--start", `${(state.start / duration) * 100}%`);
  els.rangeVisual.style.setProperty("--end", `${(state.end / duration) * 100}%`);
  els.selectedDuration.textContent = `${formatTime(state.end - state.start, duration >= 3600)} selected`;
}

els.startButton.addEventListener("click", async () => {
  if (!state.video) return;
  if (!state.config?.ready) {
    showToast("The server needs a GROQ_API_KEY before it can transcribe.", true);
    return;
  }
  els.startButton.classList.add("is-loading");
  els.startButton.disabled = true;
  try {
    const job = await api("/api/jobs", {
      method: "POST",
      body: JSON.stringify({ url: state.url, start_seconds: state.start, end_seconds: state.end }),
    });
    state.job = job;
    beginProcessing(job);
  } catch (error) {
    showToast(error.message, true);
    if (isCookieNeededError(error.message, error.status)) showGate(true, error.message);
  } finally {
    els.startButton.classList.remove("is-loading");
    els.startButton.disabled = false;
  }
});

function beginProcessing(job) {
  setView("processing");
  els.processingTitle.textContent = "Preparing your transcript";
  els.processingStage.textContent = job.stage;
  els.cancelButton.textContent = "Cancel transcription";
  els.cancelButton.dataset.mode = "cancel";
  $("img", els.processingVideo).src = job.video.thumbnail || "";
  $("strong", els.processingVideo).textContent = job.video.title;
  $("span", els.processingVideo).textContent = `${formatTime(job.selection.start)} → ${formatTime(job.selection.end)} · ${prettyDuration(job.selection.duration)}`;
  updateProgress(job);
  state.pollToken += 1;
  pollJob(state.pollToken);
}

function updateProgress(job) {
  const progress = Math.max(0, Math.min(100, Number(job.progress) || 0));
  els.processingStage.textContent = job.stage || "Working carefully";
  els.progressBar.style.width = `${progress}%`;
  els.progressPercent.textContent = `${Math.round(progress)}%`;
  if (job.status === "transcribing") els.processingTitle.textContent = "Hearing every word";
  if (job.status === "processing") els.processingTitle.textContent = "Finishing the details";
}

async function pollJob(token) {
  while (token === state.pollToken && state.job) {
    try {
      const job = await api(`/api/jobs/${state.job.id}`);
      if (token !== state.pollToken) return;
      state.job = job;
      updateProgress(job);
      if (job.status === "complete") {
        renderResult(job);
        return;
      }
      if (["failed", "cancelled"].includes(job.status)) {
        showToast(job.error || "Transcription was cancelled.", job.status === "failed");
        setView("landing");
        if (job.status === "failed") {
          els.setup.scrollIntoView({ behavior: "smooth" });
          if (isCookieNeededError(job.error)) showGate(true, job.error);
        }
        return;
      }
    } catch (error) {
      showToast(error.message, true);
      setView("landing");
      return;
    }
    await new Promise(resolve => setTimeout(resolve, 1100));
  }
}

els.cancelButton.addEventListener("click", async () => {
  if (els.cancelButton.dataset.mode === "back") {
    setView("landing");
    return;
  }
  const job = state.job;
  state.pollToken += 1;
  if (job) {
    els.cancelButton.disabled = true;
    try { await api(`/api/jobs/${job.id}`, { method: "DELETE" }); } catch (_) { /* already gone */ }
    els.cancelButton.disabled = false;
  }
  state.job = null;
  setView("landing");
  showToast("Transcription cancelled.");
});

function renderResult(job) {
  state.activeVersion = "original";
  els.resultTitle.textContent = job.video.title;
  const languageNames = (job.languages || []).map(item => item.name).join(" + ") || "Auto-detected";
  els.resultMeta.textContent = `${job.video.channel} · ${formatTime(job.selection.start)}–${formatTime(job.selection.end)} · ${languageNames}`;
  els.resultThumbnail.src = job.video.thumbnail || "";
  els.resultThumbnail.alt = `Thumbnail for ${job.video.title}`;
  els.audioRange.textContent = `${formatTime(job.selection.start)} — ${formatTime(job.selection.end)}`;
  els.audioDuration.textContent = formatTime(job.selection.duration);
  els.audio.src = job.audio_url;
  els.wordCount.textContent = Number(job.word_count || 0).toLocaleString();
  els.segmentCount.textContent = Number(job.segments?.length || 0).toLocaleString();
  els.languageList.textContent = languageNames;
  els.search.value = "";
  renderSegments(job.segments || []);
  updateRefinementUI(job);
  switchTab("original");
  setView("result");
}

function renderSegments(segments, query = "") {
  els.transcript.replaceChildren();
  let visible = 0;
  const fragment = document.createDocumentFragment();
  segments.forEach((segment, index) => {
    if (query && !segment.text.toLocaleLowerCase().includes(query.toLocaleLowerCase())) return;
    visible += 1;
    const row = document.createElement("div");
    row.className = "transcript-line";
    row.dataset.index = index;
    row.dataset.start = segment.start;
    const button = document.createElement("button");
    button.className = "timestamp";
    button.type = "button";
    button.title = "Play from this timestamp";
    button.innerHTML = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="m9 7 8 5-8 5V7Z"/></svg>';
    button.append(document.createTextNode(formatTime(segment.video_start ?? segment.start)));
    button.addEventListener("click", () => {
      els.audio.currentTime = Math.max(0, Number(segment.start));
      els.audio.play().catch(() => {});
      $$(".transcript-line.is-active", els.transcript).forEach(line => line.classList.remove("is-active"));
      row.classList.add("is-active");
    });
    const paragraph = document.createElement("p");
    appendHighlighted(paragraph, segment.text, query);
    row.append(button, paragraph);
    fragment.append(row);
  });
  els.transcript.append(fragment);
  els.emptySearch.hidden = visible > 0 || !query;
}

function appendHighlighted(target, text, query) {
  if (!query) {
    target.textContent = text;
    return;
  }
  const lower = text.toLocaleLowerCase();
  const needle = query.toLocaleLowerCase();
  let cursor = 0;
  let index;
  while ((index = lower.indexOf(needle, cursor)) !== -1) {
    target.append(document.createTextNode(text.slice(cursor, index)));
    const mark = document.createElement("mark");
    mark.textContent = text.slice(index, index + query.length);
    target.append(mark);
    cursor = index + query.length;
  }
  target.append(document.createTextNode(text.slice(cursor)));
}

els.audio.addEventListener("timeupdate", () => {
  if (!state.job || state.activeVersion !== "original") return;
  const time = els.audio.currentTime;
  let activeIndex = -1;
  const segments = state.job.segments || [];
  for (let index = 0; index < segments.length; index += 1) {
    if (time >= segments[index].start && time < segments[index].end + .15) { activeIndex = index; break; }
  }
  $$(".transcript-line.is-active", els.transcript).forEach(line => {
    if (Number(line.dataset.index) !== activeIndex) line.classList.remove("is-active");
  });
  if (activeIndex >= 0) $(`.transcript-line[data-index="${activeIndex}"]`, els.transcript)?.classList.add("is-active");
});

function refinedPlaceholder() {
  els.refined.innerHTML = '<div class="refined-placeholder"><div><svg viewBox="0 0 24 24"><path d="M12 2 9.5 8.5 3 11l6.5 2.5L12 20l2.5-6.5L21 11l-6.5-2.5L12 2Z"/></svg><strong>No refined version yet</strong><p>Choose “Refine with AI” for a careful correction pass. The original remains untouched.</p></div></div>';
}

function renderRefined(text, query = "") {
  els.refined.replaceChildren();
  if (!text) { refinedPlaceholder(); return; }
  const content = document.createElement("div");
  appendHighlighted(content, text, query);
  els.refined.append(content);
  els.emptySearch.hidden = !query || text.toLocaleLowerCase().includes(query.toLocaleLowerCase());
}

function switchTab(version) {
  state.activeVersion = version;
  const original = version === "original";
  els.originalTab.classList.toggle("active", original);
  els.refinedTab.classList.toggle("active", !original);
  els.originalTab.setAttribute("aria-selected", String(original));
  els.refinedTab.setAttribute("aria-selected", String(!original));
  els.transcript.hidden = !original;
  els.refined.hidden = original;
  els.search.value = "";
  els.emptySearch.hidden = true;
  if (!original) renderRefined(state.job?.refined_transcript || "");
}
els.originalTab.addEventListener("click", () => switchTab("original"));
els.refinedTab.addEventListener("click", () => switchTab("refined"));

els.search.addEventListener("input", () => {
  const query = els.search.value.trim();
  if (state.activeVersion === "original") renderSegments(state.job?.segments || [], query);
  else renderRefined(state.job?.refined_transcript || "", query);
});

function updateRefinementUI(job) {
  const refinement = job.refinement || { status: "idle", progress: 0 };
  const running = ["queued", "refining"].includes(refinement.status);
  els.refineButton.disabled = running;
  els.refineButton.classList.toggle("is-loading", running);
  els.refinementProgress.hidden = !running;
  if (running) {
    $("b", els.refinementProgress).textContent = `${Math.round(refinement.progress || 0)}%`;
    $("em", els.refinementProgress).style.width = `${refinement.progress || 0}%`;
  }
  const complete = refinement.status === "complete" && Boolean(job.refined_transcript);
  els.refinedDot.classList.toggle("ready", complete);
  els.refineBanner.hidden = complete;
  if (complete) renderRefined(job.refined_transcript);
}

els.refineButton.addEventListener("click", async () => {
  if (!state.job) return;
  try {
    state.job = await api(`/api/jobs/${state.job.id}/refine`, { method: "POST" });
    updateRefinementUI(state.job);
    pollRefinement(state.job.id, state.pollToken);
  } catch (error) {
    showToast(error.message, true);
  }
});

async function pollRefinement(jobId, token) {
  while (state.job?.id === jobId && token === state.pollToken) {
    await new Promise(resolve => setTimeout(resolve, 900));
    try {
      state.job = await api(`/api/jobs/${jobId}`);
      updateRefinementUI(state.job);
      const status = state.job.refinement.status;
      if (status === "complete") {
        showToast("AI refinement is ready. Your original is unchanged.");
        switchTab("refined");
        return;
      }
      if (status === "failed") {
        showToast(state.job.refinement.error || "Refinement failed.", true);
        return;
      }
    } catch (error) {
      showToast(error.message, true);
      return;
    }
  }
}

els.copyButton.addEventListener("click", async () => {
  const text = state.activeVersion === "refined" ? state.job?.refined_transcript : state.job?.transcript;
  if (!text) return showToast("There is no text to copy yet.", true);
  try {
    await navigator.clipboard.writeText(text);
    $("span", els.copyButton).textContent = "Copied";
    showToast("Transcript copied to clipboard.");
    setTimeout(() => $("span", els.copyButton).textContent = "Copy", 1600);
  } catch (_) {
    showToast("Clipboard access was blocked by your browser.", true);
  }
});

els.downloadButton.addEventListener("click", event => {
  event.stopPropagation();
  const opening = els.downloadMenu.hidden;
  els.downloadMenu.hidden = !opening;
  els.downloadButton.setAttribute("aria-expanded", String(opening));
});
document.addEventListener("click", event => {
  if (!event.target.closest(".download-wrap")) {
    els.downloadMenu.hidden = true;
    els.downloadButton.setAttribute("aria-expanded", "false");
  }
});
$$("[data-format]", els.downloadMenu).forEach(button => button.addEventListener("click", () => {
  if (!state.job) return;
  const format = button.dataset.format;
  let version = state.activeVersion;
  if (version === "refined" && !state.job.refined_transcript) version = "original";
  window.location.href = `/api/jobs/${state.job.id}/download?format=${format}&version=${version}&timeline=video`;
  els.downloadMenu.hidden = true;
  showToast(`${format.toUpperCase()} export started.`);
}));

els.newButton.addEventListener("click", async () => {
  const previous = state.job;
  state.pollToken += 1;
  state.job = null;
  els.audio.pause();
  els.audio.removeAttribute("src");
  els.audio.load();
  setView("landing");
  if (previous) api(`/api/jobs/${previous.id}`, { method: "DELETE" }).catch(() => {});
});

loadConfig();
