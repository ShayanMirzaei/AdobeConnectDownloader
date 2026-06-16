"""Parallel chunked downloader with reconnect+resume — the engine behind the download manager.

Strategy (docs/PROTOCOL.md §6):
  - Split each stream's own timeline into ~chunk_sec chunks.
  - Run up to `par` concurrent seeked play()s on ONE connection (par=24 is the stable
    sweet spot). Pace createNetStream ~0.3s apart.
  - Seeked frames carry RELATIVE timestamps and start on a keyframe.
  - On drop: re-establish with a FRESH ticket and resume only unfinished chunks.

Disk-backed (unlike the in-RAM prototype): each chunk's frames stream straight to the
ChunkStore; a chunk is marked done in the Manifest only on Play.Stop. A not-done chunk is
reset and re-downloaded from its (keyframe-aligned) start on resume — so a crash/restart
continues from the last completed chunk. That is what makes this a real download manager.

Ported from acd_fast.py FastDownloader (run/_dispatch/_reader/_build_chunks).
"""
from __future__ import annotations
import asyncio
import math
import time
from collections import defaultdict
from typing import Callable, Optional

from .auth import SessionInfo
from .discover import discover, Inventory
from .gateway import Gateway
from ..jobs.manifest import Chunk, Manifest
from ..jobs.store import ChunkStore

MARGIN = 6.0   # extra seconds per chunk so seams overlap (no gaps); deduped at track build


class Downloader:
    def __init__(self, mint: Callable[[], SessionInfo], store: ChunkStore, manifest: Manifest,
                 par: int = 24, chunk_sec: int = 300, seconds: Optional[float] = None,
                 on_log: Optional[Callable[[str], None]] = None,
                 on_progress: Optional[Callable[[dict], None]] = None):
        self.mint = mint
        self.store = store
        self.manifest = manifest
        self.par = par
        self.chunk_sec = chunk_sec
        self.seconds = seconds
        self.log = on_log or (lambda _m: None)
        self.on_progress = on_progress or (lambda _d: None)
        self.status = "queued"     # queued|establishing|downloading|paused|done|incomplete|error
        self._stop = False         # cooperative pause flag (set from another thread)

        self.gw = Gateway()
        self.gw.media_sink = self._on_frame
        self.gw.status_sink = self._on_status

        self._by_id: dict[int, Chunk] = {}
        self._stype: dict[str, str] = {}     # stream name -> type
        self._active: dict[int, str] = {}    # chunk_id -> nsid (in flight)
        self._nsid2chunk: dict[str, int] = {}
        self._recv: dict[int, int] = {}      # chunk_id -> bytes this session (progress)
        self._maxts: dict[int, int] = {}     # chunk_id -> max relative ts (progress)

    # ---- frame / status sinks (called from the gateway reader) ----
    def _on_frame(self, nsid: str, typ: int, ts: int, payload: bytes) -> None:
        cid = self._nsid2chunk.get(nsid)
        if cid is None:
            return
        self.store.append(cid, typ, ts, payload)
        self._recv[cid] = self._recv.get(cid, 0) + len(payload)
        if ts > self._maxts.get(cid, 0):
            self._maxts[cid] = ts

    def _on_status(self, nsid: str, code: str) -> None:
        cid = self._nsid2chunk.get(nsid)
        if cid is not None and cid in self._by_id:
            self._by_id[cid].done = True

    # ---- chunk planning ----
    def _build_chunks(self, inv: Inventory) -> None:
        by_type: dict[str, list] = defaultdict(list)
        for s in inv.media:
            by_type[s.type].append(s)
        dur_ms = inv.duration_s * 1000.0
        limit_ms = (self.seconds * 1000.0) if self.seconds else None
        cid = 0
        for _stype, lst in by_type.items():
            lst.sort(key=lambda s: s.start_ms)
            for i, s in enumerate(lst):
                start_ms = s.start_ms
                end_ms = lst[i + 1].start_ms if i + 1 < len(lst) else dur_ms
                own = max(0.0, (end_ms - start_ms) / 1000.0)
                if limit_ms is not None:
                    if start_ms >= limit_ms:
                        continue
                    own = min(own, (limit_ms - start_ms) / 1000.0)
                if own <= 0:
                    continue
                nch = max(1, math.ceil(own / self.chunk_sec))
                piece = own / nch
                for j in range(nch):
                    self.manifest.chunks.append(
                        Chunk(id=cid, stream=s.name, start_s=j * piece, len_s=piece))
                    cid += 1

    async def _dispatch(self, chunk: Chunk, nsid: str) -> bool:
        """Start one chunk playing from its start. Returns False on createNetStream timeout
        (per-stream jitter; caller retries without tearing down the connection)."""
        self._nsid2chunk[nsid] = chunk.id
        try:
            await self.gw.create_netstream(nsid)
        except asyncio.TimeoutError:
            self._nsid2chunk.pop(nsid, None)
            return False
        # full-chunk (re)download: clear any partial bytes so resume can't duplicate
        self.store.reset(chunk.id)
        self._recv[chunk.id] = 0
        self._maxts[chunk.id] = 0
        if self._stype.get(chunk.stream) == "cameraVoip":
            await self.gw.send({"type": "NSFunc", "method": "receiveAudio", "nsId": nsid, "action": True})
            await self.gw.send({"type": "NSFunc", "method": "receiveVideo", "nsId": nsid, "action": True})
        await self.gw.play(nsid, chunk.stream, chunk.start_s, chunk.len_s + MARGIN, reset=1, media=True)
        return True

    def _reap_done(self) -> bool:
        done_now = [cid for cid in list(self._active) if self._by_id[cid].done]
        for cid in done_now:
            self._active.pop(cid, None)
        if done_now:
            self.manifest.save()   # persist done flags so a restart resumes here
        return bool(done_now)

    def request_stop(self) -> None:
        """Cooperative pause: the run loop notices and exits gracefully (state is resumable)."""
        self._stop = True

    def _emit(self, **extra) -> None:
        total = len(self.manifest.chunks)
        done = sum(1 for c in self.manifest.chunks if c.done)
        payload = {"status": self.status, "done": done, "total": total,
                   "title": self.manifest.title, "sco": self.manifest.sco,
                   "duration_s": self.manifest.duration_s}
        payload.update(extra)
        try:
            self.on_progress(payload)
        except Exception:
            pass

    async def run(self) -> None:
        retry = lambda a, n: self.log(f"  …registering (attempt {a}/{n}; fresh ticket each try)")  # noqa: E731
        stop = lambda: self._stop  # noqa: E731
        self.status = "establishing"
        self._emit()
        try:
            if not await self.gw.establish(self.mint, on_retry=retry, should_stop=stop):
                self.status = "paused" if self._stop else "error"
                if self.status == "error":
                    raise RuntimeError("Couldn't connect after many attempts (link expired or gateway down).")
                return

            if not self.manifest.chunks:
                inv = await discover(self.gw)
                self.manifest.duration_s = inv.duration_s
                self.manifest.streams = [{"name": s.name, "type": s.type, "start_ms": s.start_ms}
                                         for s in inv.streams]
                self._build_chunks(inv)
                self.manifest.save()

            self._by_id = {c.id: c for c in self.manifest.chunks}
            self._stype = {s["name"]: s["type"] for s in self.manifest.streams}
            n_media = sum(1 for s in self.manifest.streams if s["type"] in ("screenshare", "cameraVoip"))
            total = len(self.manifest.chunks)
            content = sum(c.len_s for c in self.manifest.chunks)
            already = sum(1 for c in self.manifest.chunks if c.done)
            self.log(f"{self.manifest.duration_s:.0f}s, {n_media} media streams → {total} chunks "
                     f"({content:.0f} stream-s) at par {self.par}"
                     + (f"  (resuming: {already}/{total} already done)" if already else ""))

            self.status = "downloading"
            self._emit()
            t0 = time.monotonic()
            nsid_n = 100
            create_fails = 0
            self.gw.last_frame_t = time.monotonic()
            self._active.clear()
            self._nsid2chunk.clear()

            while True:
                if self._stop:
                    self.status = "paused"
                    break
                if all(c.done for c in self.manifest.chunks):
                    self.status = "done"
                    self.log("All chunks complete.")
                    break
                if self.gw.ws_closed:
                    self.log("  ⚠️ connection lost — reconnecting to resume remaining chunks…")
                    await self.gw.close()
                    self._active.clear()
                    self._nsid2chunk.clear()
                    create_fails = 0
                    if not await self.gw.establish(self.mint, on_retry=retry, should_stop=stop):
                        self.status = "paused" if self._stop else "incomplete"
                        self.log("Couldn't reconnect; composing what we have.")
                        break
                    self.gw.last_frame_t = time.monotonic()
                    continue

                for c in self.manifest.chunks:
                    if len(self._active) >= self.par:
                        break
                    if c.done or c.id in self._active:
                        continue
                    nsid_n += 1
                    nsid = f"nsID_{nsid_n}"
                    try:
                        ok = await self._dispatch(c, nsid)
                    except Exception:
                        self.gw.ws_closed = True
                        break
                    if ok:
                        self._active[c.id] = nsid
                        create_fails = 0
                    else:
                        create_fails += 1
                        if create_fails >= 15:
                            self.gw.ws_closed = True
                            break
                    await asyncio.sleep(0.3)   # pace creates — a rapid burst can drop the socket

                await asyncio.sleep(2)
                if self.gw.ws_closed:
                    continue
                if time.monotonic() - self.gw.last_frame_t > 45:
                    self.log("  stalled 45s — recycling connection…")
                    self.gw.ws_closed = True
                    continue

                self._reap_done()
                done = sum(1 for c in self.manifest.chunks if c.done)
                mb = sum(self._recv.values()) / 1e6
                got = sum(self._maxts.get(c.id, 0) / 1000.0 for c in self.manifest.chunks)
                rate = got / max(0.001, time.monotonic() - t0)
                eta = (content - got) / rate if rate > 0 else 0
                pct = got / max(1.0, content) * 100
                self.log(f"  …{done:2d}/{total} chunks  {pct:4.1f}%  "
                         f"{mb:6.1f}MB  {rate:4.1f}× realtime  ~{eta / 60:4.1f} min left")
                self._emit(pct=round(pct, 1), mb=round(mb, 1), rate=round(rate, 1), eta_min=round(eta / 60, 1))
        finally:
            try:
                await self.gw.close()
            except Exception:
                pass
            self.manifest.status = self.status
            try:
                self.manifest.save()
            except Exception:
                pass
            self._emit()
