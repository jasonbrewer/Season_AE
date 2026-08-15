"use strict";

const el = (id) => document.getElementById(id);

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

function showPath(path) {
  const node = el("folder-path");
  node.textContent = path || "No folder chosen";
  node.classList.toggle("muted", !path);
}

async function pickFolder() {
  showError("");
  const button = el("pick-folder");
  button.disabled = true;
  try {
    const result = await api("/api/pick-folder", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ prompt: "Choose a folder" }),
    });
    if (!result.cancelled) {
      showPath(result.path);
    }
  } catch (err) {
    showError(err.message);
  } finally {
    button.disabled = false;
  }
}

el("pick-folder").addEventListener("click", pickFolder);
