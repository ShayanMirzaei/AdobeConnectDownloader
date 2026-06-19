"use strict";
const jobsEl = document.getElementById("jobs");
const BUSY = ["queued", "establishing", "downloading", "composing", "pausing"];
const RESUMABLE = ["paused", "incomplete", "error"];
const EDITABLE = ["queued", "paused", "incomplete", "error", "done"];
let editing = null;   // job id whose card is in edit mode (freezes refresh so it isn't clobbered)

// ---- add to queue ----
document.getElementById("add").addEventListener("submit", async (e) => {
  e.preventDefault();
  const ta = document.getElementById("urls");
  const courseEl = document.getElementById("course");
  const urls = ta.value.split(/[\r\n]+/).map((s) => s.trim()).filter(Boolean);
  if (!urls.length) return;
  ta.value = "";
  await fetch("/api/jobs", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ urls, course: courseEl.value.trim() }),
  });
  courseEl.value = "";
  refresh();
});

// ---- save folder ----
async function loadSettings() {
  try {
    const s = await (await fetch("/api/settings")).json();
    const el = document.getElementById("saveto");
    if (document.activeElement !== el) el.value = s.out_root || "";
    el.placeholder = s.default || "~/Downloads";
  } catch (e) { /* ignore */ }
}
document.getElementById("savebtn").addEventListener("click", async () => {
  const out_root = document.getElementById("saveto").value.trim();
  const hint = document.getElementById("savehint");
  await fetch("/api/settings", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ out_root }),
  });
  hint.textContent = "Saved. New downloads go here; each course in its own subfolder.";
  setTimeout(() => { hint.textContent =
    "Each course goes in its own subfolder; files are named by recording date."; }, 2500);
  loadSettings();
});

async function action(id, act) {
  if (act === "remove" && !confirm("Remove this download and its files?")) return;
  await fetch(`/api/jobs/${id}/${act}`, { method: "POST" });
  refresh();
}

function pct(j) { return Math.max(0, Math.min(100, j.pct || 0)); }
function basename(p) { return (p || "").split(/[\\/]/).pop(); }

function statusLabel(j) {
  return {
    establishing: "Connecting…",
    downloading: `Downloading ${j.done || 0}/${j.total || 0}`,
    composing: "Muxing to MP4…",
    pausing: "Pausing…",
    paused: "Paused",
    queued: "Queued",
    done: "Done",
    incomplete: "Incomplete",
    error: "Error",
  }[j.status] || "Queued";
}

function metaLine(j) {
  if (j.status === "downloading")
    return `${pct(j).toFixed(1)}% · ${j.rate || 0}× realtime · ~${j.eta_min || 0} min left`;
  if (j.status === "error") return j.error || "failed";
  if (j.status === "done" && j.out) return "Saved: " + j.out;
  if (j.duration_s) return `${Math.round(j.duration_s / 60)} min recording`;
  return "";
}

function destLine(j) {
  const file = basename(j.out);
  if (!file) return "";
  return (j.course ? esc(j.course) + " / " : "") + "<span class='fname'>" + esc(file) + "</span>";
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
    if (editing === j.id) {
      card.innerHTML = `
        <div class="row"><div class="title">${esc(j.title || j.url)}</div></div>
        <div class="editor">
          <label>Course folder<input class="ed-course" type="text" value="${esc(j.course || "")}"></label>
          <label>File name<input class="ed-name" type="text" value="${esc(j.name || "")}"></label>
          <div class="ctrls">
            <button data-a="save" data-id="${j.id}">Save</button>
            <button class="danger" data-a="canceledit" data-id="${j.id}">Cancel</button>
          </div>
        </div>`;
      jobsEl.appendChild(card);
      continue;
    }
    card.innerHTML = `
      <div class="row">
        <div class="title">${esc(j.title || j.url)}</div>
        <span class="badge ${j.status}">${statusLabel(j)}</span>
      </div>
      ${destLine(j) ? `<div class="dest">${destLine(j)}</div>` : ""}
      <div class="bar ${j.status === "composing" ? "indet" : ""}"><div class="fill" style="width:${pct(j)}%"></div></div>
      <div class="row sub">
        <div class="meta">${esc(metaLine(j))}</div>
        <div class="ctrls">
          ${EDITABLE.includes(j.status) ? `<button data-a="edit" data-id="${j.id}">Edit</button>` : ""}
          ${busy ? `<button data-a="pause" data-id="${j.id}">Pause</button>` : ""}
          ${RESUMABLE.includes(j.status) ? `<button data-a="resume" data-id="${j.id}">Resume</button>` : ""}
          <button class="danger" data-a="remove" data-id="${j.id}">Remove</button>
        </div>
      </div>`;
    jobsEl.appendChild(card);
  }
  jobsEl.querySelectorAll("button[data-a]").forEach((b) =>
    b.addEventListener("click", () => onAction(b.dataset.id, b.dataset.a, b)));
}

async function onAction(id, act, btn) {
  if (act === "edit") { editing = id; refresh(); return; }
  if (act === "canceledit") { editing = null; refresh(); return; }
  if (act === "save") {
    const card = btn.closest(".card");
    const course = card.querySelector(".ed-course").value.trim();
    const name = card.querySelector(".ed-name").value.trim();
    editing = null;
    await fetch(`/api/jobs/${id}/rename`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ course, name }),
    });
    refresh();
    return;
  }
  action(id, act);
}

async function refresh() {
  if (editing) return;   // don't clobber an open editor
  try {
    const r = await fetch("/api/jobs");
    render(await r.json());
  } catch (e) { /* server momentarily busy */ }
}

loadSettings();
refresh();
setInterval(refresh, 1500);
