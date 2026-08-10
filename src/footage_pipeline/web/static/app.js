"use strict";

const POLL_INTERVAL_MS = 500;

const el = (id) => document.getElementById(id);
const state = { source: null, backupRoot: null, pickerAvailable: true, pollTimer: null };

function humanBytes(bytes) {
  const units = ["B", "KB", "MB", "GB", "TB"];
  let size = Number(bytes) || 0;
  let unit = 0;
  while (size >= 1024 && unit < units.length - 1) {
    size /= 1024;
    unit += 1;
  }
  return unit === 0 ? `${size} B` : `${size.toFixed(1)} ${units[unit]}`;
}

async function api(path, options) {
  const response = await fetch(path, options);
  let payload = null;
  try {
    payload = await response.json();
  } catch (err) {
    payload = null;
  }
  if (!response.ok) {
    const detail = (payload && payload.detail) || `Request failed (${response.status})`;
    throw new Error(detail);
  }
  return payload;
}

function showError(message) {
  const node = el("error");
  node.textContent = message || "";
  node.classList.toggle("hidden", !message);
}

function refreshStartButton() {
  const ready = Boolean(state.source && state.backupRoot);
  el("start").disabled = !ready;
  if (!state.backupRoot) {
    el("start-note").textContent = "Set a backup root first.";
  } else if (!state.source) {
    el("start-note").textContent = "Choose a source folder.";
  } else if (!state.pickerAvailable) {
    el("start-note").textContent = "Native folder picker unavailable (macOS only).";
  } else {
    el("start-note").textContent = "";
  }
}

async function loadSettings() {
  const settings = await api("/api/settings");
  state.backupRoot = settings.backup_root;
  el("backup-root").textContent = settings.backup_root || "Not set";
  el("backup-root").classList.toggle("muted", !settings.backup_root);

  const warning = el("dest-warning");
  if (settings.backup_root && !settings.backup_root_exists) {
    warning.textContent = "This folder is not currently reachable — is the drive mounted?";
    warning.classList.remove("hidden");
  } else {
    warning.classList.add("hidden");
  }

  state.pickerAvailable = settings.picker_available;
  if (!state.source && settings.last_source) {
    setSource(settings.last_source, true);
  }
  refreshStartButton();
}

function setSource(path, isPrefill) {
  state.source = path;
  const node = el("source-path");
  node.textContent = isPrefill ? `${path}  (last used)` : path;
  node.classList.remove("muted");
  refreshStartButton();
}

async function pickFolder(prompt, defaultPath) {
  return api("/api/pick-folder", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ prompt, default_path: defaultPath || null }),
  });
}

async function onPickDestination() {
  showError("");
  try {
    const picked = await pickFolder("Choose the backup root", state.backupRoot);
    if (picked.cancelled) return;
    await api("/api/settings", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ backup_root: picked.path }),
    });
    await loadSettings();
  } catch (err) {
    showError(err.message);
  }
}

async function onPickSource() {
  showError("");
  try {
    const picked = await pickFolder("Choose the folder to back up", state.source);
    if (picked.cancelled) return;
    setSource(picked.path, false);
  } catch (err) {
    showError(err.message);
  }
}

async function onStart() {
  showError("");
  el("report-card").classList.add("hidden");
  el("start").disabled = true;
  try {
    await api("/api/backup/start", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ source: state.source }),
    });
    el("progress-card").classList.remove("hidden");
    startPolling();
  } catch (err) {
    showError(err.message);
    refreshStartButton();
  }
}

function renderProgress(progress) {
  const pct = progress.total_bytes
    ? Math.min(100, (progress.bytes_done / progress.total_bytes) * 100)
    : 0;
  el("bar-fill").style.width = `${pct}%`;
  el("p-files").textContent = `${progress.files_done} / ${progress.total_files}`;
  el("p-bytes").textContent =
    `${humanBytes(progress.bytes_done)} / ${humanBytes(progress.total_bytes)}`;
  el("p-phase").textContent = progress.phase;
  el("p-current").textContent = progress.current_file || "—";
}

function renderReport(result) {
  const totals = result.totals || {};
  el("r-copied").textContent = totals.copied ?? 0;
  el("r-skipped").textContent = totals.skipped ?? 0;
  el("r-conflicts").textContent = totals.conflicts ?? 0;
  el("r-failed").textContent = totals.failed ?? 0;
  el("r-symlinks").textContent = totals.symlinks_skipped ?? 0;
  el("r-manifest").textContent = result.manifest_path || "—";
  el("r-log").textContent = result.log_path || "—";

  const verdict = el("verdict");
  verdict.textContent = result.overall;
  verdict.className = `verdict ${result.overall === "PASS" ? "pass" : "fail"}`;

  const issues = result.issues || [];
  const list = el("issue-list");
  list.textContent = "";
  issues.forEach((row) => {
    const item = document.createElement("li");
    const tag = document.createElement("span");
    tag.className = `tag ${row.status}`;
    tag.textContent = row.status;
    item.appendChild(tag);
    item.appendChild(document.createTextNode(`${row.relative_path} — ${row.error || ""}`));
    list.appendChild(item);
  });
  el("issues").classList.toggle("hidden", issues.length === 0);

  const more = el("issue-more");
  const hidden = (result.issue_count || 0) - issues.length;
  more.textContent = hidden > 0 ? `…and ${hidden} more (see the manifest).` : "";
  more.classList.toggle("hidden", hidden <= 0);

  el("report-card").classList.remove("hidden");
}

function stopPolling() {
  if (state.pollTimer) {
    clearInterval(state.pollTimer);
    state.pollTimer = null;
  }
}

async function poll() {
  try {
    const status = await api("/api/backup/status");
    renderProgress(status.progress);
    if (status.error) {
      showError(status.error);
      stopPolling();
      refreshStartButton();
      return;
    }
    if (!status.running && status.result) {
      renderReport(status.result);
      stopPolling();
      refreshStartButton();
      loadSettings();
    }
  } catch (err) {
    showError(err.message);
    stopPolling();
    refreshStartButton();
  }
}

function startPolling() {
  stopPolling();
  poll();
  state.pollTimer = setInterval(poll, POLL_INTERVAL_MS);
}

el("pick-dest").addEventListener("click", onPickDestination);
el("pick-source").addEventListener("click", onPickSource);
el("start").addEventListener("click", onStart);

loadSettings().catch((err) => showError(err.message));
// A run started before this page loaded (or a reload mid-run) keeps its UI.
api("/api/backup/status")
  .then((status) => {
    if (status.running) {
      el("progress-card").classList.remove("hidden");
      startPolling();
    }
  })
  .catch(() => {});
