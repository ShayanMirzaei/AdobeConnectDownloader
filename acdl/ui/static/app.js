"use strict";
const jobsEl = document.getElementById("jobs");
const BUSY = ["queued", "establishing", "downloading", "composing", "pausing"];
const RESUMABLE = ["paused", "incomplete", "error"];

document.getElementById("add").addEventListener("submit", async (e) => {
  e.preventDefault();
  const input = document.getElementById("url");
  const url = input.value.trim();
  if (!url) return;
  input.value = "";
  await fetch("/api/jobs", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ url }),
  });
  refresh();
});

async function action(id, act) {
  if (act === "remove" && !confirm("Remove this download and its files?")) return;
  await fetch(`/api/jobs/${id}/${act}`, { method: "POST" });
  refresh();
}

function pct(j) { return Math.max(0, Math.min(100, j.pct || 0)); }

function statusLabel(j) {
  return {
    establishing: "Connecting…",
    downloading: `Downloading ${j.done || 0}/${j.total || 0}`,
    composing: "Muxing to MP4…",
    pausing: "Pausing…",
    paused: "Paused",
    done: "Done",
    incomplete: "Incomplete",
    error: "Error",
  }[j.status] || "Queued";
}

function metaLine(j) {
  if (j.status === "downloading")
    return `${pct(j).toFixed(1)}% · ${j.rate || 0}× realtime · ~${j.eta_min || 0} min left`;
  if (j.status === "error") return j.error || "failed";
  if (j.status === "done" && j.output) return "Saved: " + j.output;
  if (j.duration_s) return `${Math.round(j.duration_s / 60)} min recording`;
  return "";
}

function esc(s) {
  return (s || "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
}

function render(jobs) {
  if (!jobs.length) { jobsEl.innerHTML = '<p class="empty">No downloads yet.</p>'; return; }
  jobsEl.innerHTML = "";
  for (const j of jobs) {
    const busy = BUSY.includes(j.status);
    const card = document.createElement("div");
    card.className = "card " + j.status;
    card.innerHTML = `
      <div class="row">
        <div class="title">${esc(j.title || j.url)}</div>
        <span class="badge ${j.status}">${statusLabel(j)}</span>
      </div>
      <div class="bar ${j.status === "composing" ? "indet" : ""}"><div class="fill" style="width:${pct(j)}%"></div></div>
      <div class="row sub">
        <div class="meta">${esc(metaLine(j))}</div>
        <div class="ctrls">
          ${busy ? `<button data-a="pause" data-id="${j.id}">Pause</button>` : ""}
          ${RESUMABLE.includes(j.status) ? `<button data-a="resume" data-id="${j.id}">Resume</button>` : ""}
          <button class="danger" data-a="remove" data-id="${j.id}">Remove</button>
        </div>
      </div>`;
    jobsEl.appendChild(card);
  }
  jobsEl.querySelectorAll("button[data-a]").forEach((b) =>
    b.addEventListener("click", () => action(b.dataset.id, b.dataset.a)));
}

async function refresh() {
  try {
    const r = await fetch("/api/jobs");
    render(await r.json());
  } catch (e) { /* server momentarily busy */ }
}

refresh();
setInterval(refresh, 1500);
