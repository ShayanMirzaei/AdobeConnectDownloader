"""Parallel chunked downloader with reconnect+resume — the engine behind the download manager.

Strategy (docs/PROTOCOL.md §6):
  - Split each stream's own timeline into ~chunk_sec chunks.
  - Run up to `par` concurrent seeked play()s on ONE connection (par=24 is the stable
    sweet spot; higher over-drives and drops). Pace createNetStream ~0.3s apart.
  - Seeked frames carry RELATIVE timestamps and start on a keyframe.
  - On drop: re-establish with a FRESH ticket and resume only unfinished chunks; per-chunk
    ts_offset re-bases replayed frames.

DIFFERENCE FROM PROTOTYPE (acd_fast.py held frames in RAM): here, completed chunk frames are
flushed to the jobs.store on disk and the jobs.manifest records per-chunk completion, so a
crash/restart can resume. The manifest+store are what make this a real download manager.

Reference: acd_fast.py FastDownloader (run/_dispatch/_reader/_build_chunks).
"""
from __future__ import annotations
from typing import Awaitable, Callable

from .auth import SessionInfo


class Downloader:
    def __init__(self, mint: "Callable[[], Awaitable[SessionInfo]]", store, manifest,
                 par: int = 24, chunk_sec: int = 300):
        self.mint = mint          # async () -> SessionInfo (fresh ticket each call)
        self.store = store        # jobs.store.ChunkStore
        self.manifest = manifest  # jobs.manifest.Manifest
        self.par = par
        self.chunk_sec = chunk_sec
        raise NotImplementedError("M1: port from acd_fast.py with disk-backed chunks")

    async def run(self) -> None:
        """Establish, discover (or load from manifest), download all chunks, resuming on drops."""
        raise NotImplementedError
