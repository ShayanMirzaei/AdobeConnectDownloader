"""JobManager — runs download jobs off the UI thread and exposes their live state.

A background thread hosts an asyncio loop where Downloaders run. The web server (in its own
threads) calls the thread-safe methods here: submit/pause/resume/remove/rename/list. Each job
keeps its resumable work (manifest + chunks + tracks + whiteboard) under <root>/<sco>/, so jobs
survive restarts. The finished .mp4 is written to a user-chosen "Save folder" (default: the OS
Downloads folder), organised into a per-course subfolder with a date-ordered file name.

Queueing: only one job *downloads* at a time (a single network slot). The moment a job finishes
downloading and hands off to ffmpeg, the slot frees and the next queued job starts downloading —
so muxing one lecture overlaps fetching the next.

Zero extra dependencies (threading + asyncio + stdlib).
"""
from __future__ import annotations
import asyncio
import json
import os
import re
import shutil
import threading
import uuid

from .. import applog
from ..core.auth import AuthError, get_session_info
from ..core.downloader import Downloader
from .manifest import Manifest
from .store import ChunkStore

# statuses considered "busy" (can't resume/start again)
_BUSY = {"queued", "establishing", "downloading", "composing", "pausing"}

# chars that are illegal in file/folder names on Windows (and awkward elsewhere). We deliberately
# keep non-ASCII letters (Persian titles must survive) — only strip path-hostile characters.
_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
# trailing "session/number/date" markers we peel off a title to guess the course name
_SEP = re.compile(r"\s*[-–—|:]\s*")
_KW = r"session|sess|part|lecture|lec|week|day|جلسه|قسمت|هفته|روز"
# a whole separator-delimited tail part that is just a number, a date, or "<keyword> <n>"
_TAIL_JUNK = re.compile(rf"^(?:(?:{_KW})\b[\s#:_.-]*)?\d{{1,4}}(?:[\s/.\-]\d{{1,4}}){{0,2}}$", re.IGNORECASE)
# a "<keyword> <n>" tail with no separator, e.g. "ریاضی ۱ جلسه 5" -> drop " جلسه 5"
_KW_TAIL = re.compile(rf"\s+(?:{_KW})\s*[#:_.\-]?\s*\d{{0,4}}\s*$", re.IGNORECASE)


def default_downloads_dir() -> str:
    """The OS Downloads folder if it exists, else the home dir."""
    home = os.path.expanduser("~")
    d = os.path.join(home, "Downloads")
    return d if os.path.isdir(d) else home


def derive_course(title: str | None) -> str:
    """Guess a course name from a recording title by peeling trailing session/date/number markers.

    "Algorithms - Session 3" -> "Algorithms";  "ریاضی ۱ جلسه 5" -> "ریاضی ۱".  Heuristic and
    sometimes wrong by design — the UI lets the user edit it before the file is written.
    """
    t = (title or "").strip()
    if not t:
        return ""
    parts = _SEP.split(t)
    while len(parts) > 1 and _TAIL_JUNK.match(parts[-1].strip()):
        parts.pop()
    course = " - ".join(p.strip() for p in parts).strip()
    course = _KW_TAIL.sub("", course).strip()   # peel "… جلسه 5" / "… Session 5" (no separator)
    return course or t


def safe_name(s: str | None) -> str:
    """Filesystem-safe name that preserves Unicode letters; strips only path-hostile chars."""
    s = _ILLEGAL.sub("", (s or "")).strip().strip(".").strip()
    return re.sub(r"\s+", " ", s)


class JobManager:
    def __init__(self, root: str = "downloads", par: int = 24, chunk: int = 300,
                 out_root: str | None = None, max_downloads: int = 1):
        self.root = os.path.abspath(root)          # resumable work dirs live here (one per sco)
        self.par = par
        self.chunk = chunk
        self.max_downloads = max(1, max_downloads)  # concurrent *download* slots (1 = strictly serial)
        os.makedirs(self.root, exist_ok=True)
        applog.setup()
        self.log = applog.get("acdl.jobs")
        self.settings_path = os.path.join(self.root, "settings.json")
        self.out_root = self._init_out_root(out_root)
        self._jobs: dict[str, dict] = {}
        self._dls: dict[str, Downloader] = {}
        self._tasks: dict[str, asyncio.Task] = {}   # in-flight job coroutines (for cancel-while-queued)
        self._lock = threading.Lock()
        self._loop = asyncio.new_event_loop()
        self._gate: asyncio.Semaphore | None = None  # the download slot; created on the loop below
        threading.Thread(target=self._run_loop, daemon=True, name="acdl-jobs").start()
        asyncio.run_coroutine_threadsafe(self._make_gate(), self._loop).result(timeout=5)
        self._load_existing()

    # ---- settings ----
    def _init_out_root(self, override: str | None) -> str:
        if override:
            return os.path.abspath(os.path.expanduser(override))
        try:
            with open(self.settings_path, encoding="utf-8") as f:
                saved = json.load(f).get("out_root")
            if saved:
                return os.path.abspath(os.path.expanduser(saved))
        except Exception:
            pass
        return default_downloads_dir()

    def get_settings(self) -> dict:
        return {"out_root": self.out_root, "default": default_downloads_dir()}

    def set_settings(self, out_root: str) -> dict:
        if out_root and out_root.strip():
            self.out_root = os.path.abspath(os.path.expanduser(out_root.strip()))
            try:
                with open(self.settings_path, "w", encoding="utf-8") as f:
                    json.dump({"out_root": self.out_root}, f, ensure_ascii=False, indent=2)
            except Exception:
                self.log.warning("could not persist settings to %s", self.settings_path)
        return self.get_settings()

    # ---- internals ----
    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    async def _make_gate(self) -> None:
        self._gate = asyncio.Semaphore(self.max_downloads)

    def _set(self, job_id: str, **kw) -> None:
        with self._lock:
            self._jobs.setdefault(job_id, {"id": job_id}).update(kw)

    def _update(self, job_id: str, progress: dict) -> None:
        with self._lock:
            j = self._jobs.setdefault(job_id, {"id": job_id})
            for k, v in progress.items():
                if v is not None:
                    j[k] = v

    def _compute_out_path(self, out_root: str, course: str, prefix: str, name: str) -> str:
        base = f"{prefix} - {name}" if prefix else name
        base = safe_name(base)[:120] or "recording"
        parts = [out_root]
        sc = safe_name(course)
        if sc:
            parts.append(sc)
        parts.append(base + ".mp4")
        return os.path.join(*parts)

    def _seq_prefix(self, course: str) -> str:
        """Add-order fallback (01, 02, …) when no recording date is available."""
        with self._lock:
            n = sum(1 for j in self._jobs.values() if (j.get("course") or "") == course and j.get("prefix"))
        return f"{n + 1:02d}"

    def _load_existing(self) -> None:
        for name in sorted(os.listdir(self.root)):
            mpath = os.path.join(self.root, name, "manifest.json")
            if not os.path.exists(mpath):
                continue
            try:
                m = Manifest.load(mpath)
            except Exception:
                continue
            job_id = uuid.uuid4().hex[:8]
            done = sum(1 for c in m.chunks if c.done)
            total = len(m.chunks)
            job_dir = os.path.join(self.root, name)
            out = m.out_path or self._compute_out_path(
                m.out_root or self.out_root, m.course, m.prefix, m.out_name or safe_name(m.title) or m.sco)
            self._set(job_id, url=m.url, title=m.title or m.url, sco=m.sco, dir=job_dir,
                      status="done" if m.status == "done" else "paused",
                      done=done, total=total, duration_s=m.duration_s,
                      pct=round(100 * done / max(1, total), 1),
                      course=m.course, name=m.out_name, prefix=m.prefix,
                      out=out, out_root=m.out_root or self.out_root,
                      output=out if m.status == "done" else None)

    # ---- public API (called from server threads) ----
    def list(self) -> list[dict]:
        with self._lock:
            return [dict(j) for j in self._jobs.values()]

    def submit_many(self, urls, course: str = "") -> list[str]:
        ids = []
        for u in urls or []:
            u = (u or "").strip()
            if u:
                ids.append(self.submit(u, course))
        return ids

    def submit(self, url: str, course: str = "") -> str:
        job_id = uuid.uuid4().hex[:8]
        self.log.info("submit job %s: %s", job_id, url)
        self._set(job_id, url=url, title="resolving…", status="queued", done=0, total=0, pct=0,
                  course=course or "", out_root=self.out_root)
        asyncio.run_coroutine_threadsafe(self._prepare_then_download(job_id, url, course or ""), self._loop)
        return job_id

    def pause(self, job_id: str) -> None:
        with self._lock:
            dl = self._dls.get(job_id)
            j = self._jobs.get(job_id)
            task = self._tasks.get(job_id)
            status = j.get("status") if j else None
        if dl:                                   # actively downloading → cooperative, resumable stop
            dl.request_stop()
            self._set(job_id, status="pausing")
        elif status == "queued" and task is not None:   # waiting in line → drop it from the queue
            self._set(job_id, status="paused")
            self._loop.call_soon_threadsafe(task.cancel)

    def resume(self, job_id: str) -> None:
        with self._lock:
            j = dict(self._jobs.get(job_id, {}))
        if not j or j.get("status") in _BUSY:
            return
        self._set(job_id, status="queued", error=None)
        asyncio.run_coroutine_threadsafe(
            self._prepare_then_download(job_id, j["url"], j.get("course") or ""), self._loop)

    def remove(self, job_id: str) -> None:
        self.pause(job_id)
        with self._lock:
            j = self._jobs.pop(job_id, None)
            self._dls.pop(job_id, None)
        if j and j.get("dir") and os.path.isdir(j["dir"]):
            shutil.rmtree(j["dir"], ignore_errors=True)

    def rename(self, job_id: str, course: str | None = None, name: str | None = None) -> None:
        """Change a job's course folder and/or file name. Works while queued/paused/error/done;
        refused only mid-mux. For a finished job the existing .mp4 is moved to the new location."""
        with self._lock:
            j = dict(self._jobs.get(job_id, {}))
        if not j or j.get("status") == "composing":
            return
        course = (course if course is not None else j.get("course", "")).strip()
        new_name = safe_name(name) if name else (j.get("name") or "")
        prefix = j.get("prefix") or ""
        out_root = j.get("out_root") or self.out_root
        out_path = self._compute_out_path(out_root, course, prefix, new_name or "recording")

        old = j.get("out")
        if j.get("status") == "done" and old and os.path.isfile(old) and old != out_path:
            try:
                os.makedirs(os.path.dirname(out_path), exist_ok=True)
                shutil.move(old, out_path)
            except Exception as e:
                self.log.warning("rename: couldn't move %s → %s: %s", old, out_path, e)
                return
        mpath = os.path.join(j.get("dir", ""), "manifest.json")
        if os.path.exists(mpath):
            try:
                m = Manifest.load(mpath)
                m.course, m.out_name, m.out_path, m.out_root = course, new_name, out_path, out_root
                m.save()
            except Exception:
                self.log.warning("rename: couldn't update manifest %s", mpath)
        done = j.get("status") == "done"
        self._set(job_id, course=course, name=new_name, out=out_path,
                  output=out_path if done else j.get("output"))

    # ---- job lifecycle (runs on the background loop) ----
    async def _prepare_then_download(self, job_id: str, url: str, course_in: str) -> None:
        """Resolve metadata immediately (cheap, runs concurrently for a whole pasted batch) so the
        card shows its destination right away, then queue for the single download slot."""
        self._tasks[job_id] = asyncio.current_task()
        try:
            self._set(job_id, status="queued", title="resolving…", error=None)
            try:
                info = await asyncio.to_thread(get_session_info, url)
            except AuthError as e:
                self.log.warning("job %s auth failed: %s", job_id, e)
                self._set(job_id, status="error", error=str(e))
                return
            except Exception as e:
                self.log.exception("job %s resolve error", job_id)
                self._set(job_id, status="error", error=f"{type(e).__name__}: {e}")
                return

            title = info.title or url
            work_dir = os.path.join(self.root, info.sco or job_id)
            mpath = os.path.join(work_dir, "manifest.json")
            if os.path.exists(mpath):                       # resume: keep whatever was chosen before
                manifest = Manifest.load(mpath)
                course = manifest.course or course_in or derive_course(title)
                out_name = manifest.out_name or safe_name(title) or (info.sco or "recording")
                prefix = manifest.prefix or info.date or self._seq_prefix(course)
                out_path = manifest.out_path or self._compute_out_path(self.out_root, course, prefix, out_name)
            else:
                course = (course_in or derive_course(title)).strip()
                out_name = safe_name(title) or (info.sco or "recording")
                prefix = info.date or self._seq_prefix(course)
                out_path = self._compute_out_path(self.out_root, course, prefix, out_name)
                manifest = Manifest(url=url, host=info.host, sco=info.sco, title=title,
                                    par=self.par, chunk_sec=self.chunk, path=mpath)
            manifest.course, manifest.out_name = course, out_name
            manifest.prefix, manifest.out_path, manifest.out_root = prefix, out_path, self.out_root
            manifest.save()

            self._set(job_id, title=title, sco=info.sco, dir=work_dir, status="queued",
                      course=course, name=out_name, prefix=prefix, out=out_path,
                      out_root=self.out_root, duration_s=manifest.duration_s or 0)
            self.log.info("job %s resolved: %s → %s", job_id, title, out_path)

            await self._download_phase(job_id, url, work_dir)
        except asyncio.CancelledError:
            with self._lock:
                j = self._jobs.get(job_id)
                if j and j.get("status") not in ("done",):
                    j["status"] = "paused"
            return
        finally:
            self._tasks.pop(job_id, None)

    async def _download_phase(self, job_id: str, url: str, work_dir: str) -> None:
        assert self._gate is not None
        async with self._gate:                       # ← the single download slot; one job at a time
            with self._lock:
                j = self._jobs.get(job_id)
                status = j.get("status") if j else None
            if j is None or status == "paused":       # paused/removed while waiting in line
                return

            self._set(job_id, status="establishing")
            manifest = Manifest.load(os.path.join(work_dir, "manifest.json"))
            store = ChunkStore(os.path.join(work_dir, "chunks"))
            mint = lambda: get_session_info(url)  # noqa: E731  (fresh ticket each call)
            dl = Downloader(mint, store, manifest, par=self.par, chunk_sec=self.chunk,
                            on_progress=lambda d: self._update(job_id, d))
            with self._lock:
                self._dls[job_id] = dl
            try:
                await dl.run()
            except Exception as e:
                self.log.exception("job %s download error", job_id)
                self._set(job_id, status="error", error=f"{type(e).__name__}: {e}")
                return
            finally:
                with self._lock:
                    self._dls.pop(job_id, None)
        # download slot released here → the next queued job can start while we mux this one
        if dl.status != "done":
            self.log.info("job %s ended with status=%s", job_id, dl.status)
            return

        out = manifest.out_path or self._jobs.get(job_id, {}).get("out")
        self.log.info("job %s download complete; composing → %s", job_id, out)
        self._set(job_id, status="composing")
        try:
            await asyncio.to_thread(self._compose, manifest, store, work_dir, out)
            self.log.info("job %s composed → %s", job_id, out)
            self._set(job_id, status="done", output=out, out=out)
        except Exception as e:
            self.log.exception("job %s compose error", job_id)
            self._set(job_id, status="error", error=f"compose: {type(e).__name__}: {e}")

    def _compose(self, manifest: Manifest, store: ChunkStore, work_dir: str, out: str) -> None:
        from ..ffmpeg import find_ffmpeg
        from ..media.compose import compose
        from ..media.flv import build_tracks
        from ..media.whiteboard import whiteboard_video_tracks
        os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
        ffmpeg = find_ffmpeg()
        tracks = build_tracks(manifest.streams, manifest.chunks, store, os.path.join(work_dir, "tracks"))
        video_starts = sorted(t.start_s for t in tracks if t.kind == "video")
        tracks += whiteboard_video_tracks(os.path.join(work_dir, "whiteboard.json"), video_starts,
                                          manifest.duration_s, os.path.join(work_dir, "wb"), ffmpeg)
        compose(tracks, out, manifest.duration_s, ffmpeg)
