"""JobManager — runs download jobs off the UI thread and exposes their live state.

A background thread hosts an asyncio loop where Downloaders run. The web server (in its own
threads) calls the thread-safe methods here: submit/pause/resume/remove/list. Each job lives
in downloads/<sco>/ (manifest + chunks + tracks + final mp4), so jobs survive restarts and
resume from their manifest.

Zero extra dependencies (threading + asyncio + stdlib).
"""
from __future__ import annotations
import asyncio
import os
import re
import shutil
import threading
import uuid
from typing import Optional

from ..core.auth import AuthError, get_session_info
from ..core.downloader import Downloader
from .manifest import Manifest
from .store import ChunkStore

# statuses considered "busy" (can't resume/start again)
_BUSY = {"queued", "establishing", "downloading", "composing", "pausing"}


class JobManager:
    def __init__(self, root: str = "downloads", par: int = 24, chunk: int = 300):
        self.root = os.path.abspath(root)
        self.par = par
        self.chunk = chunk
        os.makedirs(self.root, exist_ok=True)
        self._jobs: dict[str, dict] = {}
        self._dls: dict[str, Downloader] = {}
        self._lock = threading.Lock()
        self._loop = asyncio.new_event_loop()
        threading.Thread(target=self._run_loop, daemon=True, name="acdl-jobs").start()
        self._load_existing()

    # ---- internals ----
    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def _set(self, job_id: str, **kw) -> None:
        with self._lock:
            self._jobs.setdefault(job_id, {"id": job_id}).update(kw)

    def _update(self, job_id: str, progress: dict) -> None:
        with self._lock:
            j = self._jobs.setdefault(job_id, {"id": job_id})
            for k, v in progress.items():
                if v is not None:
                    j[k] = v

    def _output_path(self, manifest: Manifest, job_dir: str) -> str:
        base = re.sub(r"[^A-Za-z0-9._-]+", "_", (manifest.title or manifest.sco or "recording")).strip("_")
        return os.path.join(job_dir, (base[:80] or "recording") + ".mp4")

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
            self._set(job_id, url=m.url, title=m.title or m.url, sco=m.sco, dir=job_dir,
                      status="done" if m.status == "done" else "paused",
                      done=done, total=total, duration_s=m.duration_s,
                      pct=round(100 * done / max(1, total), 1), output=self._output_path(m, job_dir))

    # ---- public API (called from server threads) ----
    def list(self) -> list[dict]:
        with self._lock:
            return [dict(j) for j in self._jobs.values()]

    def submit(self, url: str) -> str:
        job_id = uuid.uuid4().hex[:8]
        self._set(job_id, url=url, title="resolving…", status="queued", done=0, total=0, pct=0)
        asyncio.run_coroutine_threadsafe(self._run_job(job_id, url), self._loop)
        return job_id

    def pause(self, job_id: str) -> None:
        with self._lock:
            dl = self._dls.get(job_id)
        if dl:
            dl.request_stop()
            self._set(job_id, status="pausing")

    def resume(self, job_id: str) -> None:
        with self._lock:
            j = dict(self._jobs.get(job_id, {}))
        if not j or j.get("status") in _BUSY:
            return
        asyncio.run_coroutine_threadsafe(self._run_job(job_id, j["url"]), self._loop)

    def remove(self, job_id: str) -> None:
        self.pause(job_id)
        with self._lock:
            j = self._jobs.pop(job_id, None)
            self._dls.pop(job_id, None)
        if j and j.get("dir") and os.path.isdir(j["dir"]):
            shutil.rmtree(j["dir"], ignore_errors=True)

    # ---- the actual job coroutine (runs on the background loop) ----
    async def _run_job(self, job_id: str, url: str) -> None:
        self._set(job_id, status="establishing", title="resolving…", error=None)
        try:
            info = await asyncio.to_thread(get_session_info, url)
        except AuthError as e:
            self._set(job_id, status="error", error=str(e))
            return
        except Exception as e:
            self._set(job_id, status="error", error=f"{type(e).__name__}: {e}")
            return

        job_dir = os.path.join(self.root, info.sco or job_id)
        mpath = os.path.join(job_dir, "manifest.json")
        if os.path.exists(mpath):
            manifest = Manifest.load(mpath)
        else:
            manifest = Manifest(url=url, host=info.host, sco=info.sco, title=info.title,
                                par=self.par, chunk_sec=self.chunk, path=mpath)
        store = ChunkStore(os.path.join(job_dir, "chunks"))
        out = self._output_path(manifest, job_dir)
        self._set(job_id, title=info.title or url, sco=info.sco, dir=job_dir, output=out)

        mint = lambda: get_session_info(url)  # noqa: E731  (fresh ticket each call)
        dl = Downloader(mint, store, manifest, par=self.par, chunk_sec=self.chunk,
                        on_progress=lambda d: self._update(job_id, d))
        with self._lock:
            self._dls[job_id] = dl
        try:
            await dl.run()
        except Exception as e:
            self._set(job_id, status="error", error=f"{type(e).__name__}: {e}")
            return
        finally:
            with self._lock:
                self._dls.pop(job_id, None)

        if dl.status == "done":
            self._set(job_id, status="composing")
            try:
                await asyncio.to_thread(self._compose, manifest, store, job_dir, out)
                self._set(job_id, status="done", output=out)
            except Exception as e:
                self._set(job_id, status="error", error=f"compose: {type(e).__name__}: {e}")

    def _compose(self, manifest: Manifest, store: ChunkStore, job_dir: str, out: str) -> None:
        from ..ffmpeg import find_ffmpeg
        from ..media.compose import compose
        from ..media.flv import build_tracks
        tracks = build_tracks(manifest.streams, manifest.chunks, store, os.path.join(job_dir, "tracks"))
        compose(tracks, out, manifest.duration_s, find_ffmpeg())
